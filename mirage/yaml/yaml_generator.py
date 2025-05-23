# ! /usr/bin/env python

'''
Class to produce yaml files that can be used as input for
the ramp simulator

Use
---
    To generate an observationlist.yaml file and generate all .yamls at once
    ::
        yam = yaml_generator.SimInput(input_xml=apt_file_xml, pointing_file=apt_file_pointing,
                                      catalogs=catalogs, verbose=True, output_dir=out_dir,
                                      simdata_output_dir=out_dir)
        yam.create_inputs()

    NOTE there is currently no way to generate yamls from an existing observationlist.yaml


Inputs
------
    xml file - Name of xml file exported from APT.
    pointing file - Name of associated pointing file exported from APT.

    Optional inputs:

    output_dir - Directory into which the output yaml files are written

    simdata_output_dir - Directory to place in the output_directory field of the yaml files.
                         This is the directory where the simulated ramps will be saved.

    table_file - Ascii table containing observation info. This is the
                 output from apt_inputs.py Use this if you are
                 not providing xml and pointing files from APT.

    datatype - Specifies the type of output data to save. Can be "raw", in which
               case the raw (uncalibrated) file is saved, "linear", where the
               linearized file is saved and ready to be run through the jump
               detection and ramp-fitting steps of the pipeline, or
               "linear, raw", where both versions are saved.

    use_nonstsci_names - set to True to override the use of the standard
                         STScI naming convention for output files

    subarray_def_file - Ascii file containing NIRCam subarray definitions

    readpatt_def_file - Ascii file containing NIRCam readout pattern definitions

    point_source - point source catalog file. Can be a single file, or a list of
                   catalogs. If it is a list, each filename is expected to contain
                   the filter name for which it is to be used. Catalogs and filters
                   will then be matched up in the output yaml files.

    galaxyListFile - galaxy (sersic) source catalog file. Can be a single name, or
                     a list of names. Behavior is identical to point_source above.

    extended - extended source catalog file. Behavior is identical to point_source
                above.

    convolveExtended - Set to True to convolve extended sources with NIRCam PSF

    movingTarg - Moving (point source) target catalog (sources moving through fov) names.
                 Behavior is the same as point_sources above.

    movingTargSersic - Moving galaxy (sersic) target catalog (sources moving through fov)
                       names. Behavior is the same as point_sources above.

    movingTargExtended - Moving extended source target catalog (sources moving through fov)
                         names. Behavior is the same as point_sources above.

    movingTargToTrack - Catalog of non-sidereal targets for non-sidereal tracking observations.
                        Behavior is the same as point_sources above.

    bkgdrate - Uniform background rate (e-/s) to add to observation.

    epoch_list - Ascii table file containing epoch start times and telescope roll angles
                 to use for each observation.


Dependencies
------------
    argparse, astropy, numpy, glob, copy
    apt_inputs.py - Functions for reading and parsing xml and pointing files from APT.

History
-------
    July 2017 - V0: Initial version. Bryan Hilbert
    Feb 2018 - V1: Updates to accomodate multiple filter pairs per
                   observation. Lauren Chambers
    August 2018 - V2: Replaced input SIAF CSV file with pysiaf dependence. Bryan Hilbert

'''

import sys
import os
import argparse
from collections import Counter
import logging
from copy import deepcopy
from glob import glob
import datetime

from astropy.time import Time, TimeDelta
from astropy.table import Table
from astropy.io import ascii, fits
import numpy as np
import pkg_resources
import pysiaf

from ..apt import apt_inputs
from ..catalogs.utils import get_nonsidereal_catalog_name, read_nonsidereal_catalog
from ..logging import logging_functions
from ..reference_files import crds_tools
from ..reference_files.utils import get_transmission_file
from ..seed_image import ephemeris_tools
from ..utils.constants import FGS1_DARK_SEARCH_STRING, FGS2_DARK_SEARCH_STRING
from ..utils.siaf_interface import aperture_xy_to_radec
from ..utils.utils import calc_frame_time, ensure_dir_exists, expand_environment_variable, parse_RA_Dec
from .generate_observationlist import get_observation_dict
from ..constants import NIRISS_PUPIL_WHEEL_ELEMENTS, NIRISS_FILTER_WHEEL_ELEMENTS
from ..utils.constants import CRDS_FILE_TYPES, SEGMENTATION_MIN_SIGNAL_RATE, \
                              LOG_CONFIG_FILENAME, STANDARD_LOGFILE_NAME
from ..utils import siaf_interface, utils

ENV_VAR = 'MIRAGE_DATA'

classpath = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))
log_config_file = os.path.join(classpath, 'logging', LOG_CONFIG_FILENAME)
logging_functions.create_logger(log_config_file, STANDARD_LOGFILE_NAME)


class SimInput:
    def __init__(self, input_xml=None, pointing_file=None, datatype='linear', reffile_defaults='crds',
                 reffile_overrides=None, use_JWST_pipeline=True, catalogs=None, cosmic_rays=None,
                 background=None, roll_angle=None, dates=None,
                 observation_list_file=None, verbose=False, output_dir='./', simdata_output_dir='./',
                 dateobs_for_background=False, segmap_flux_limit=None, segmap_flux_limit_units=None,
                 add_ghosts=True, convolve_ghosts_with_psf=False, convolve_extended_with_psf=True,
                 offline=False):
        """Initialize instance. Read APT xml and pointing files if provided.

        Also sets the reference files definitions for all instruments.

        Parameters
        ----------
        input_xml : str
            Filename of xml file exported by APT

        pointing_file : str
            Filename of pointing file exported by APT

        datatype : str
        use_JWST_pipeline : bool

        reffile_defaults : str
            Controls how the CRDS reference file entries are presented in
            the yaml file.

            'crds' - The string 'crds' is used for all entries, and CRDS
                     will be queried at run-time and appropriate files
                     downloaded if they are not already present.

            'crds_full_name' - CRDS will be queried before creating the
                               yaml files, and actual filenames will be
                               placed into the yaml files.

        reffile_overrides : dict
            If the user provides a dictionary of reference file names,
            then the yaml file will be populated with these names. Any
            needed instrument/detector options not present in the
            dictionary will default to ``reffile_defaults``
            With this option, users can use their own, non-CRDS
            reference files, if desired.

            The dictionary structure has many layers of nesting that are
            instrument and reference file type dependent.

            r_dict = {'nircam': {'bad_pix_mask': {'nrca1': 'bpm_a1.fits',
                                                      'nrca2': 'bpm_a2.fits',
                                                      'nrca3': 'crds_full_name'},
                                     'distortion': {'nrca1': {'f070w': 'dist_a1.asdf', 'f090w': 'dist_a1.asdf'},
                                                    'nrca2': {'f070w': 'dist_a2.asdf', 'f090w': 'dist_a2.asdf'}
                                                   }
                                    }
                                    This format seems easiest assuming users
                                    are overriding only one/few types of reference
                                    files at a time.

        use_JWST_pipeline : bool
            Whether or not to use the JWST pipeline when creating data.
            It is highly recommended to leave this True.

        catalogs : dict
            Nested dictionary of source catalogs to use for populating
            the collection of yaml files. See Mirage's online documentation
            for format and options.
            https://mirage-data-simulator.readthedocs.io/en/latest/yaml_generator.html

        cosmic_rays : dict
            Nested dictionary specifyig details on the cosmic ray paramters
            to use. This includes the cosmic ray library and scale to use
            for each observation. See Mirage's online documentation for
            details.
            https://mirage-data-simulator.readthedocs.io/en/latest/yaml_generator.html

        background : str or float
            Background value to use for the simulations. Can be a number,
            which will be interpreted as a constant ADU/sec per pixel.
            Can also be one of "low", "medium", "high", with the same
            definitions as used by the JWST ETC. In this case the background
            will be calculated using the jwst_backgrounds package,
            calculating the background value as a certain percentile of
            the values over the course of a year. If ``dateobs_for_background``
            is True, then this parameter is ignored.

        roll_angle : float or dict
            Value of JWST PAV3 for all observations (float) or per observation
            (dict). See Mirage's online documentation for examples:
            https://mirage-data-simulator.readthedocs.io/en/latest/yaml_generator.html

        dates : str or dict
            Observation dates. Can be a single value, which will be treated as the
            start date for the first observation, with all other observations following
            immediately after, or a dictionary with one date per observation. If
            ``dateobs_for_background`` is True, the background for each
            exposure will be calculated based on these dates.
            See Mirage's online documentation for examples:
            https://mirage-data-simulator.readthedocs.io/en/latest/yaml_generator.html

        times : str or dict
            Observation start time. Can be a single value, which will be treated as the
            start time for the first observation, with all other observations following
            immediately after, or a dictionary with one time per observation. Used in
            conjunction with ``dates``.

        observation_list_file=None
        verbose : bool
            Print more information to the screen while running

        output_dir : str
            Path in which to save the created yaml files

        simdata_output_dir : str
            Path into which the simulated data will eventually be saved. This
            value is placed within each yaml file.

        dateobs_for_background : bool
            If True, the value of ``date_obs`` in the yaml file will be used
            when calculating the background level for the observation.
            If False, the value from ``background`` will be used.

        segmap_flux_limit : float
            Lower signal limit for pixels that will be added to the segmentation
            map. Pixels with signals larger than or equal to this value will be
            added to the segmentation map.

        segmap_flux_limit_units : str
            Units corresponding to the value in ```segmap_flux_limit```. Can be
            'ADU/sec', 'e/sec', 'MJy/sr', 'ergs/cm2/A', 'ergs/cm2/Hz'

        add_ghosts : bool
            If True, optical ghosts will be added to the seed image based on
            source locations. Currently only supported for NIRISS.

        convolve_ghosts_with_psf : bool
            If True, the stamp images used for ghost sources will be convolved
            with the PSF prior to adding to the seed image

        convolve_extended_with_psf : bool
            If True, the stamp images used for astronomical sources will be
            convolved with the PSF prior to adding to the seed image

        offline : bool
            Whether the class is being called with or without access to
            Mirage reference data. Used primarily for testing.
        """
        # Initialize log
        self.logger = logging.getLogger('mirage.yaml.yaml_generator')
        self.logger.info('Running yaml_generator....\n')
        self.logger.info('using APT xml file: {}\n'.format(input_xml))
        self.logger.info('Original log file name: ./{}'.format(STANDARD_LOGFILE_NAME))

        parameter_overrides = {'cosmic_rays': cosmic_rays, 'background': background, 'roll_angle': roll_angle,
                               'dates': dates}

        self.info = {}
        self.input_xml = input_xml
        self.pointing_file = pointing_file
        self.datatype = datatype
        self.use_JWST_pipeline = use_JWST_pipeline
        self.observation_list_file = observation_list_file
        self.verbose = verbose
        self.output_dir = output_dir
        self.simdata_output_dir = simdata_output_dir
        if reffile_defaults in ['crds', 'crds_full_name']:
            self.reffile_defaults = reffile_defaults
        else:
            raise ValueError("reffile_defaults must be 'crds' or 'crds_full_name'")
        self.reffile_overrides = reffile_overrides

        self.catalogs = catalogs
        self.table_file = None
        self.use_nonstsci_names = False
        self.use_linearized_darks = True
        self.psfwfe = 'predicted'
        self.psfwfegroup = 0
        self.resets_bet_ints = 1  # NIRCam should be 1
        self.psf_paths = None
        self.expand_catalog_for_segments = False
        self.dateobs_for_background = dateobs_for_background
        self.add_psf_wings = True
        self.add_ghosts = add_ghosts
        self.convolve_ghosts = convolve_ghosts_with_psf
        self.convolve_extended = convolve_extended_with_psf
        self.offline = offline

        if ((segmap_flux_limit is not None) and (segmap_flux_limit_units is None)):
            raise ValueError("If segmap_flux_limit is provided, segmap_flux_units must also be provided.")

        if segmap_flux_limit is None:
            self.segmentation_threshold = SEGMENTATION_MIN_SIGNAL_RATE
        else:
            self.segmentation_threshold = segmap_flux_limit
        if segmap_flux_limit_units is None:
            self.segmentation_threshold_units = 'ADU/sec'
        else:
            self.segmentation_threshold_units = segmap_flux_limit_units

        # Expand the MIRAGE_DATA environment variable
        self.datadir = expand_environment_variable(ENV_VAR, offline=self.offline)

        # Check that CRDS-related environment variables are set correctly
        self.crds_datadir = crds_tools.env_variables()

        # Get the path to the 'MIRAGE' package
        self.modpath = pkg_resources.resource_filename('mirage', '')

        self.config_information = utils.organize_config_files(offline=self.offline)

        self.path_defs()

        if (input_xml is not None):
            if self.observation_list_file is None:
                self.observation_list_file = os.path.join(self.output_dir, 'observation_list.yaml')
            self.apt_xml_dict, self.xml_skipped_observations = get_observation_dict(self.input_xml, self.observation_list_file, catalogs,
                                                                                    verbose=self.verbose,
                                                                                    parameter_overrides=parameter_overrides)
        else:
            self.logger.error('No input xml file provided. Observation dictionary not constructed.')

        self.reffile_setup()

    def add_catalogs(self):
        """
        Add list(s) of source catalogs to the table containing the
        observation information
        """
        n_exposures = len(self.info['Module'])
        self.info['point_source'] = [None] * n_exposures
        self.info['galaxyListFile'] = [None] * n_exposures
        self.info['extended'] = [None] * n_exposures
        self.info['convolveExtended'] = [False] * n_exposures
        self.info['movingTarg'] = [None] * n_exposures
        self.info['movingTargSersic'] = [None] * n_exposures
        self.info['movingTargExtended'] = [None] * n_exposures
        self.info['movingTargToTrack'] = [None] * n_exposures

        for i in range(n_exposures):
            if int(self.info['detector'][i][-1]) < 5:
                filtkey = 'ShortFilter'
                pupilkey = 'ShortPupil'
            else:
                filtkey = 'LongFilter'
                pupilkey = 'LongPupil'
            filt = self.info[filtkey][i]
            pup = self.info[pupilkey][i]

            if self.point_source[i] is not None:
                # In here, we assume the user provided a catalog to go with each filter
                # so now we need to find the filter for each entry and generate a list that makes sense
                self.info['point_source'][i] = os.path.abspath(os.path.expandvars(
                    self.catalog_match(filt, pup, self.point_source, 'point source')))
            else:
                self.info['point_source'][i] = None
            if self.galaxyListFile[i] is not None:
                self.info['galaxyListFile'][i] = os.path.abspath(os.path.expandvars(
                    self.catalog_match(filt, pup, self.galaxyListFile, 'galaxy')))
            else:
                self.info['galaxyListFile'][i] = None
            if self.extended[i] is not None:
                self.info['extended'][i] = os.path.abspath(os.path.expandvars(
                    self.catalog_match(filt, pup, self.extended, 'extended')))
            else:
                self.info['extended'][i] = None
            if self.movingTarg[i] is not None:
                self.info['movingTarg'][i] = os.path.abspath(os.path.expandvars(
                    self.catalog_match(filt, pup, self.movingTarg, 'moving point source target')))
            else:
                self.info['movingTarg'][i] = None
            if self.movingTargSersic[i] is not None:
                self.info['movingTargSersic'][i] = os.path.abspath(os.path.expandvars(
                    self.catalog_match(filt, pup, self.movingTargSersic, 'moving sersic target')))
            else:
                self.info['movingTargSersic'][i] = None
            if self.movingTargExtended[i] is not None:
                self.info['movingTargExtended'][i] = os.path.abspath(os.path.expandvars(
                    self.catalog_match(filt, pup, self.movingTargExtended, 'moving extended target')))
            else:
                self.info['movingTargExtended'][i] = None
            if self.movingTargToTrack[i] is not None:
                self.info['movingTargToTrack'][i] = os.path.abspath(os.path.expandvars(
                    self.catalog_match(filt, pup, self.movingTargToTrack, 'non-sidereal moving target')))
            else:
                self.info['movingTargToTrack'][i] = None
        if self.convolveExtended is True:
            self.info['convolveExtended'] = [True] * n_exposures

    def add_crds_reffile_names(self):
        """Add specific reference file names to self.info. This should be
        done if self.reffile_defaults is set to 'crds_full_name'. For each
        instrument configuration and observing mode, query CRDS for the
        best reference files. Identify which exposures in self.info
        match the observing configuration, and add the reference file
        names to their records in self.info.
        """
        all_obs_info, unique_obs_info = self.info_for_all_observations()

        # Add empty placeholders for reference file entries
        empty_col = np.array([' ' * 500] * len(self.info['Instrument']))
        superbias_arr = deepcopy(empty_col)
        linearity_arr = deepcopy(empty_col)
        saturation_arr = deepcopy(empty_col)
        gain_arr = deepcopy(empty_col)
        distortion_arr = deepcopy(empty_col)
        photom_arr = deepcopy(empty_col)
        ipc_arr = deepcopy(empty_col)
        ipc_invert = np.array([True] * len(self.info['Instrument']))
        transmission_arr = deepcopy(empty_col)
        badpixmask_arr = deepcopy(empty_col)
        pixelflat_arr = deepcopy(empty_col)

        # Loop over combinations, create metadata dict, and get reffiles
        for status in unique_obs_info:
            updated_status = deepcopy(status)
            (instrument, detector, filtername, pupilname, readpattern, exptype) = status

            # Make sure NIRISS filter and pupil values are in the correct wheels
            if instrument == 'NIRISS':
                filtername, pupilname = utils.check_niriss_filter(filtername, pupilname)

            # Create metadata dictionary
            date = datetime.date.today().isoformat()
            current_date = datetime.datetime.now()
            time = current_date.time().isoformat()
            status_dict = {'INSTRUME': instrument, 'DETECTOR': detector,
                           'FILTER': filtername, 'PUPIL': pupilname,
                           'READPATT': readpattern, 'EXP_TYPE': exptype,
                           'DATE-OBS': date, 'TIME-OBS': time,
                           'SUBARRAY': 'FULL'}
            if instrument == 'NIRCAM':
                if detector in ['NRCA5', 'NRCB5', 'NRCALONG', 'NRCBLONG', 'A5', 'B5']:
                    status_dict['CHANNEL'] = 'LONG'
                else:
                    status_dict['CHANNEL'] = 'SHORT'
            if instrument == 'FGS':
                if detector in ['G1', 'G2']:
                    detector = detector.replace('G', 'GUIDER')
                    status_dict['DETECTOR'] = detector
                    updated_status = (instrument, detector, filtername, pupilname, readpattern, exptype)

            # Query CRDS
            # Exclude transmission file for now
            files_no_transmission = list(CRDS_FILE_TYPES.values())
            files_no_transmission.remove('transmission')
            reffiles = crds_tools.get_reffiles(status_dict, files_no_transmission,
                                               download=not self.offline)

            # If the user entered reference files in self.reffile_defaults
            # use those over what comes from the CRDS query
            if self.reffile_overrides is not None:
                manual_reffiles = self.reffiles_from_dict(updated_status)

                for key in manual_reffiles:
                    if manual_reffiles[key] != 'none':
                        if key == 'badpixmask':
                            crds_key = 'mask'
                        elif key == 'pixelflat':
                            crds_key = 'flat'
                        elif key == 'astrometric':
                            crds_key = 'distortion'
                        else:
                            crds_key = key
                        reffiles[crds_key] = manual_reffiles[key]

            # Transmission image file
            # For the moment, this file is retrieved from NIRCAM_GRISM or NIRISS_GRISM
            # Down the road it will become part of CRDS, at which point
            if 'transmission' not in reffiles.keys():
                reffiles['transmission'] = get_transmission_file(status_dict)
                self.logger.info('Using transmission file: {}'.format(reffiles['transmission']))

            # Check to see if a version of the inverted IPC kernel file
            # exists already in the same directory. If so, use that and
            # avoid having to invert the kernel at run time.
            inverted_file, must_invert = SimInput.inverted_ipc_kernel_check(reffiles['ipc'])
            if not must_invert:
                reffiles['ipc'] = inverted_file
            reffiles['invert_ipc'] = must_invert

            # Identify entries in the original list that use this combination
            match = [i for i, item in enumerate(all_obs_info) if item==status]

            # Populate the reference file names for the matching entries
            superbias_arr[match] = reffiles['superbias']
            linearity_arr[match] = reffiles['linearity']
            saturation_arr[match] = reffiles['saturation']
            gain_arr[match] = reffiles['gain']
            distortion_arr[match] = reffiles['distortion']
            photom_arr[match] = reffiles['photom']
            ipc_arr[match] = reffiles['ipc']
            ipc_invert[match] = reffiles['invert_ipc']
            transmission_arr[match] = reffiles['transmission']
            badpixmask_arr[match] = reffiles['mask']
            pixelflat_arr[match] = reffiles['flat']

        self.info['superbias'] = list(superbias_arr)
        self.info['linearity'] = list(linearity_arr)
        self.info['saturation'] = list(saturation_arr)
        self.info['gain'] = list(gain_arr)
        self.info['astrometric'] = list(distortion_arr)
        self.info['photom'] = list(photom_arr)
        self.info['ipc'] = list(ipc_arr)
        self.info['invert_ipc'] = list(ipc_invert)
        self.info['transmission'] = list(transmission_arr)
        self.info['badpixmask'] = list(badpixmask_arr)
        self.info['pixelflat'] = list(pixelflat_arr)

    def add_reffile_overrides(self):
        """If the user provides a nested dictionary in
        self.reffile_overrides, search through the dictionary, identify
        exposures that match the observing mode and instrument config
        for each dictionary entry, and populate the reference file
        entries in self.info with the file names from
        self.reffile_overrides.
        """
        all_obs_info, unique_obs_info = self.info_for_all_observations()

        # Add empty placeholders for reference file entries
        empty_col = np.array([' ' * 500] * len(self.info['Instrument']))
        superbias_arr = deepcopy(empty_col)
        linearity_arr = deepcopy(empty_col)
        saturation_arr = deepcopy(empty_col)
        gain_arr = deepcopy(empty_col)
        distortion_arr = deepcopy(empty_col)
        photom_arr = deepcopy(empty_col)
        ipc_arr = deepcopy(empty_col)
        transmission_arr = deepcopy(empty_col)
        badpixmask_arr = deepcopy(empty_col)
        pixelflat_arr = deepcopy(empty_col)

        # Loop over combinations, create metadata dict, and get reffiles
        for status in unique_obs_info:
            updated_status = deepcopy(status)
            (instrument, detector, filtername, pupilname, readpattern, exptype) = status

            if instrument == 'FGS':
                if detector in ['G1', 'G2']:
                    detector = detector.replace('G', 'GUIDER')
                    updated_status = (instrument, detector, filtername, pupilname, readpattern, exptype)

            # If the user entered reference files in self.reffile_defaults
            # use those over what comes from the CRDS query
            #sbias, lin, sat, gainfile, dist, ipcfile, pam = self.reffiles_from_dict(status)
            manual_reffiles = self.reffiles_from_dict(updated_status)
            for key in manual_reffiles:
                if manual_reffiles[key] == 'none':
                    manual_reffiles[key] = 'crds'

            # Identify entries in the original list that use this combination
            match = [i for i, item in enumerate(all_obs_info) if item==status]

            # Populate the reference file names for the matching entries
            superbias_arr[match] = manual_reffiles['superbias']
            linearity_arr[match] = manual_reffiles['linearity']
            saturation_arr[match] = manual_reffiles['saturation']
            gain_arr[match] = manual_reffiles['gain']
            distortion_arr[match] = manual_reffiles['distortion']
            photom_arr[match] = manual_reffiles['photom']
            ipc_arr[match] = manual_reffiles['ipc']
            transmission_arr[match] = manual_reffiles['transmission']
            badpixmask_arr[match] = manual_reffiles['badpixmask']
            pixelflat_arr[match] = manual_reffiles['pixelflat']

        self.info['superbias'] = list(superbias_arr)
        self.info['linearity'] = list(linearity_arr)
        self.info['saturation'] = list(saturation_arr)
        self.info['gain'] = list(gain_arr)
        self.info['astrometric'] = list(distortion_arr)
        self.info['photom'] = list(photom_arr)
        self.info['ipc'] = list(ipc_arr)
        self.info['transmission'] = list(transmission_arr)
        self.info['badpixmask'] = list(badpixmask_arr)
        self.info['pixelflat'] = list(pixelflat_arr)

    def catalog_match(self, filter, pupil, catalog_list, cattype):
        """
        Given a filter and pupil value, along with a list of input
        catalogs, find the catalog names that contain each filter/
        pupil name.

        Parameters
        ----------
        filter : str
            Name of a filter element
        pupil : str
            Name of a pupil element
        catalog_list : list
            List of catalog filenames
        cattype : str
            Type of catalog in the list.

        Returns
        -------
        match : str
            Name of catalog that contains the name of the
            input filter/pupil element
        """
        if pupil[0].upper() == 'F':
            match = [s for s in catalog_list if pupil.lower() in s.lower()]
            if len(match) == 0:
                self.no_catalog_match(pupil, cattype)
                return None
            elif len(match) > 1:
                self.multiple_catalog_match(pupil, cattype, match)
            return match[0]
        else:
            match = [s for s in catalog_list if filter.lower() in s.lower()]
            if len(match) == 0:
                self.no_catalog_match(filter, cattype)
                return None
            elif len(match) > 1:
                self.multiple_catalog_match(filter, cattype, match)
            return match[0]

    @logging_functions.log_fail
    def create_inputs(self):
        """Create observation table """
        self.path_defs()

        if ((self.input_xml is not None) &
           (self.pointing_file is not None) &
           (self.observation_list_file is not None)):

            # Define directories and paths
            indir, infile = os.path.split(self.input_xml)
            final_file = os.path.join(self.output_dir,
                                      'Observation_table_for_' + infile +
                                      '_with_yaml_parameters.csv')

            # Read XML file and make observation table
            apt = apt_inputs.AptInput(input_xml=self.input_xml, pointing_file=self.pointing_file,
                                      output_dir=self.output_dir, offline=self.offline)
            # apt.input_xml = self.input_xml
            # apt.pointing_file = self.pointing_file
            apt.observation_list_file = self.observation_list_file
            apt.apt_xml_dict = self.apt_xml_dict

            apt.output_dir = self.output_dir
            apt.create_input_table(skip_observations=self.xml_skipped_observations)
            self.info = apt.exposure_tab

            # If we have a non-sidereal observation, then we need to
            # update the pointing information based on the input
            # ephemeris file or target velocity
            self.nonsidereal_pointing_updates()

            # Get the correct pointing for each aperture
            siaf_dictionary = {}
            for instrument_name in np.unique(self.info['Instrument']):
                siaf_dictionary[instrument_name] = siaf_interface.get_instance(instrument_name)
            self.info = apt_inputs.ra_dec_update(self.info, siaf_dictionary)

            # Add a list of output yaml names to the dictionary
            self.make_output_names()

        elif self.table_file is not None:
            self.logger.info('Reading table file: {}'.format(self.table_file))
            info = ascii.read(self.table_file)
            self.info = self.table_to_dict(info)
            final_file = self.table_file + '_with_yaml_parameters.csv'

        else:
            raise FileNotFoundError(("WARNING. You must include either an ascii table file of observations"
                                     " or xml and pointing files from APT plus the observation list file."
                                     "Aborting."))

        # If self.reffile_defaults is set to 'crds' then print the
        # string 'crds' in the entry for all reference files. The
        # actual reference file names will then be identified by
        # querying CRDS at run-time. In this way, once a yaml
        # file has been generated, it will effectively always be
        # up to date, even as reference files change.
        if self.reffile_defaults == 'crds':
            column_data = ['crds'] * len(self.info['Instrument'])
            self.info['superbias'] = column_data
            self.info['linearity'] = column_data
            self.info['saturation'] = column_data
            self.info['gain'] = column_data
            self.info['astrometric'] = column_data
            self.info['photom'] = column_data
            self.info['ipc'] = column_data
            self.info['invert_ipc'] = np.array([True] * len(self.info['Instrument']))
            self.info['transmission'] = column_data
            self.info['badpixmask'] = column_data
            self.info['pixelflat'] = column_data

            # If the user provided a dictionary of reference files to
            # override some/all of those from CRDS, then enter those
            # into the correct locations in self.info
            if self.reffile_overrides is not None:
                self.add_reffile_overrides()

        # If self.reffile_defaults is set to 'crds_full_name' then
        # we query CRDS now, and add the reference file names to the
        # yaml files. This allows for easier tracing of what
        # reference files were used to create a particular exposure.
        # (Although that info is always saved in the header of the
        # exposure itself, as well.)
        elif self.reffile_defaults == 'crds_full_name':
            self.add_crds_reffile_names()

        else:
            raise ValueError(("self.reffile_defaults is not equal to 'crds' "
                              "nor 'crds_full_name'. Unable to proceed."))

        # Get the list of dark current files to use
        darks = []
        lindarks = []
        for instrument, det in zip([s.lower() for s in self.info['Instrument']], self.info['detector']):
            instrument = instrument.lower()
            darks.append(self.get_dark(instrument, det))
            lindarks.append(self.get_lindark(instrument, det))

        # If linearized darks are to be used, set the darks to None
        if self.use_linearized_darks:
            self.info['dark'] = [None] * len(darks)
            self.info['lindark'] = lindarks
            if set(lindarks) == set([None]):
                raise RuntimeError(("ERROR: Linearized darks requested, but no linearized dark files "
                                    "found. Check: {}").format(os.path.join(os.path.expandvars('$MIRAGE_DATA'), instrument)))
        else:
            self.info['dark'] = darks
            self.info['lindark'] = [None] * len(lindarks)
            if set(darks) == set([None]):
                raise RuntimeError(("ERROR: Raw darks requested, but no raw dark files found. "
                                    "Check: {}").format(os.path.join(os.path.expandvars('$MIRAGE_DATA'), instrument)))

        # Add setting describing whether JWST pipeline will be used
        self.info['use_JWST_pipeline'] = [self.use_JWST_pipeline] * len(darks)

        # add background rate to the table
        # self.info['bkgdrate'] = np.array([self.bkgdrate]*len(self.info['Mode']))

        # grism entries
        grism_source_image = ['False'] * len(self.info['Mode'])
        grism_input_only = ['False'] * len(self.info['Mode'])
        for i in range(len(self.info['Mode'])):
            if self.info['Mode'][i].lower() in ['wfss', 'ts_grism']:
                if self.info['detector'][i] == 'NIS' or self.info['detector'][i][-1] == '5':
                    grism_source_image[i] = 'True'
                    grism_input_only[i] = 'True'
                # SW detectors shouldn't be wfss
                if self.info['Instrument'][i] == 'NIRCAM' and self.info['detector'][i][-1] != '5':
                    if self.info['Mode'][i] == 'wfss':
                        self.info['Mode'][i] = 'imaging'
                    elif self.info['Mode'][i] == 'ts_grism':
                        self.info['Mode'][i] = 'ts_imaging'
        self.info['grism_source_image'] = grism_source_image
        self.info['grism_input_only'] = grism_input_only

        # Grism TSO adjustments. SW detectors should be flagged ts_imaging mode.
        for i in range(len(self.info['Mode'])):
            if self.info['Mode'][i].lower() == 'ts_grism':
                if self.info['detector'][i][-1] != '5':
                    self.info['Mode'][i] = 'ts_imaging'

        # level-3 associated keywords that are not present in APT file.
        # not quite sure how to populate these
        self.info['visit_group'] = ['01'] * len(self.info['Mode'])
        # self.info['sequence_id'] = ['1'] * len(self.info['Mode'])
        seq = []
        for par in self.info['CoordinatedParallel']:
            if par.lower() == 'true':
                seq.append('2')
            if par.lower() == 'false':
                seq.append('1')
        self.info['sequence_id'] = seq

        # Deal with user-provided PSFs that differ across observations/visits/exposures
        self.info['psfpath'] = self.get_psf_path()

        table = Table(self.info)
        table.write(final_file, format='csv', overwrite=True)
        self.logger.info('Updated observation table file saved to {}'.format(final_file))

        # Now go through the lists one element at a time
        # and create a yaml file for each.
        yamls = []
        # for i in range(len(detector_labels)):
        for i, instrument in enumerate(self.info['Instrument']):
            instrument = instrument.lower()
            if instrument not in 'fgs nircam niriss'.split():
                # do not write files for MIRI and NIRSpec
                continue
            elif instrument=='nircam':
                # special case for coronagraphy:
                # only 1 detector at a time is returned to the ground, depending on selected mask.
                # do not write files for the detectors that are not downloaded.
                if self.info['APTTemplate'][i]=='NircamCoron':
                    # do not write files for detectors not read out
                    # the ones not used are tagged with 'n/a' in ReadAPTXML.read_nircam_coronagraphy_template
                    # and recall NIRCam coronagraphy always uses module A
                    if ((self.info['LongPupil'][i]=='n/a' and self.info['detector'][i]=='A5') or
                        (self.info['ShortPupil'][i]=='n/a' and self.info['detector'][i] in ['A1','A2','A3','A4'])):
                        print(f"Skipping {self.info['yamlfile'][i]} because this coronagraphy obs does not use that detector")
                        continue

                    # SW coronagraphy exposures that use MASKRND collect data from A2 only
                    if (self.info['ShortPupil'][i] == 'MASKRND' and self.info['detector'][i] != 'A2'):
                        print(f"Skipping yaml for {self.info['detector'][i]} with {self.info['ShortPupil'][i]}")
                        continue

                    # SW coronagraphy exposures that use MASKSWB collect data from A4 only
                    if (self.info['ShortPupil'][i] == 'MASKSWB' and self.info['detector'][i] != 'A4'):
                        print(f"Skipping yaml for {self.info['detector'][i]} with {self.info['ShortPupil'][i]}")
                        continue

            file_dict = {}
            for key in self.info:
                file_dict[key] = self.info[key][i]

            # break dither number into numbers for primary
            # and subpixel dithers
            tot_dith = int(file_dict['dither'])
            # primarytot = int(file_dict['PrimaryDithers'])
            primarytot = int(file_dict['number_of_dithers'])

            if isinstance(file_dict['SubpixelPositions'], str) and file_dict['SubpixelPositions'].upper() == 'NONE':
                subpixtot = 1
            else:
                try:
                    subpixtot = int(file_dict['SubpixelPositions'])
                except:
                    subpixtot = int(file_dict['SubpixelPositions'][0])
            primary_dither = np.ceil(1. * tot_dith / subpixtot)
            file_dict['primary_dither_num'] = int(primary_dither)
            subpix_dither = (tot_dith-1) % subpixtot
            file_dict['subpix_dither_num'] = subpix_dither + 1

            file_dict['subarray_def_file'] = self.config_information['global_subarray_definition_files'][instrument]
            file_dict['readpatt_def_file'] = self.config_information['global_readout_pattern_files'][instrument]
            file_dict['crosstalk_file'] = self.config_information['global_crosstalk_files'][instrument]
            file_dict['filtpupilcombo_file'] = self.config_information['global_filtpupilcombo_files'][instrument]
            file_dict['filter_position_file'] = self.config_information['global_filter_position_files'][instrument]
            file_dict['flux_cal_file'] = self.config_information['global_flux_cal_files'][instrument]
            file_dict['psf_wing_threshold_file'] = self.config_information['global_psf_wing_threshold_file'][instrument]
            fname = self.write_yaml(file_dict)
            yamls.append(fname)
        self.yaml_files = yamls

        # Write out summary of all written yaml files
        filenames = [y.split('/')[-1] for y in yamls]
        mosaic_numbers = sorted(list(set([f.split('_')[0] for f in filenames])))
        obs_ids = sorted(list(set([m[7:10] for m in mosaic_numbers])))

        self.logger.info('\n')

        total_exposures = 0
        for obs in obs_ids:
            visit_list = list(set([m[10:] for m in mosaic_numbers
                                   if m[7:10] == obs]))
            n_visits = len(visit_list)
            activity_list = list(set([vf[17:29] for vf in self.info['yamlfile']
                                      if vf[7:10] == obs]))
            n_activities = len(activity_list)
            exposure_list = list(set([vf[20:25] for vf in self.info['yamlfile']
                                      if vf[7:10] == obs]))
            n_exposures = len(exposure_list) * n_activities
            total_exposures += n_exposures
            all_obs_files = [m for m in self.info['yamlfile'] if m[7:10] == obs]
            total_files = len(all_obs_files)

            obs_id_int = np.array([int(ele) for ele in self.info['ObservationID']])
            obs_indexes = np.where(obs_id_int == int(obs))[0]
            obs_entries = np.array(self.info['ParallelInstrument'])[obs_indexes]
            coord_par = self.info['CoordinatedParallel'][obs_indexes[0]]
            if coord_par:
                par_indexes = np.where(obs_entries)[0]
                if len(par_indexes) > 0:
                    parallel_instrument = self.info['Instrument'][obs_indexes[par_indexes[0]]]
                else:
                    parallel_instrument = 'NONE'
            else:
                parallel_instrument = 'NONE'

            # Note that changing the == below to 'is' results in an error
            pri_indexes = np.where(obs_entries == False)[0]
            prime_instrument = self.info['Instrument'][obs_indexes[pri_indexes[0]]]

            if prime_instrument.upper() == 'NIRCAM':
                module = self.info['Module'][obs_indexes[pri_indexes[0]]]
                detectors_used = np.array(self.info['detector'])[obs_indexes[pri_indexes]]
            elif parallel_instrument.upper() == 'NIRCAM':
                module = self.info['Module'][obs_indexes[par_indexes[0]]]
                detectors_used = np.array(self.info['detector'])[obs_indexes[par_indexes]]

            if ((prime_instrument.upper() == 'NIRCAM') or (parallel_instrument.upper() == 'NIRCAM')):
                if module == 'ALL':
                    module = 'A and B'
                n_det = len(set(detectors_used))
            else:
                n_det = 1

            if coord_par == 'true':
                instrument_string = '    Prime: {}, Parallel: {}'.format(prime_instrument, parallel_instrument)
            else:
                instrument_string = '    Prime: {}'.format(prime_instrument)

            self.logger.info('Observation {}:'.format(obs))
            self.logger.info(instrument_string)
            self.logger.info('    {} visit(s)'.format(n_visits))
            self.logger.info('    {} activity(ies)'.format(n_activities))
            #self.logger.info('    {} exposure(s)'.format(n_exposures))
            if ((prime_instrument.upper() == 'NIRCAM') or (parallel_instrument.upper() == 'NIRCAM')):
                self.logger.info('    {} NIRCam detector(s) in module {}'.format(n_det, module))
            self.logger.info('    {} file(s)'.format(total_files))

        # self.logger.info('\n{} exposures total.'.format(total_exposures))
        self.logger.info('{} output files written to: {}'.format(len(yamls), self.output_dir))
        self.logger.info('Yaml generator complete')
        log_outdir = os.path.dirname(self.input_xml)
        logging_functions.move_logfile_to_standard_location(self.input_xml, STANDARD_LOGFILE_NAME,
                                                            yaml_outdir=log_outdir, log_type='yaml_generator')

    def create_output_name(self, input_obj, index=0):
        """Put together the JWST formatted fits file name based on observation parameters

        Parameters
        ----------
        input_obj : dict
            Dictionary from apt_inputs giving information for each exposure

        Returns
        -------
        base : str
            JWST formatted filename base (excluding pipeline step suffix and ".fits")
        """
        proposal_id = '{0:05d}'.format(int(input_obj['ProposalID'][index]))
        observation = input_obj['obs_num'][index]
        visit_number = input_obj['visit_num'][index]
        visit_group = input_obj['visit_group'][index]
        parallel_sequence_id = input_obj['sequence_id'][index]
        activity_id = input_obj['act_id'][index]
        exposure = input_obj['exposure'][index]

        base = 'jw{}{}{}_{}{}{}_{}_'.format(proposal_id, observation, visit_number,
                                            visit_group, parallel_sequence_id, activity_id,
                                            exposure)
        return base

    def find_ipc_file(self, inputipc):
        """Given a list of potential IPC kernel files for a given
        detector, select the most appropriate one, and check to see
        whether the kernel needs to be inverted, in order to populate
        the invertIPC field. This is not intended to be terribly smart.
        The first inverted kernel found will be used. If none are found,
        the first kernel will be used and set to be inverted.

        Parameters
        ----------
        inputipc : list
           List of fits files containing IPC kernels for a single detector

        Returns
        -------
        (ipcfile, invstatus) : tup
           ipcfile is the name of the IPC kernel file to use, and invstatus
           lists whether the kernel needs to be inverted or not.
        """
        for ifile in inputipc:
            kernel = fits.getdata(ifile)
            kshape = kernel.shape

            # If kernel is 4 dimensional, extract the 3x3 kernel associated
            # with a single pixel
            if len(kernel.shape) == 4:
                kernel = kernel[:, :, int(kshape[2]/2), int(kshape[2]/2)]

            if kernel[1, 1] < 1.0:
                return (ifile, False)
        # If no inverted kernel was found, just return the first file
        return (inputipc[0], True)

    def get_dark(self, instrument, detector):
        """Return the name of a dark current file to use as input
        based on the detector being used

        Parameters
        ----------
        detector : str
            Name of detector being used
        Returns
        -------
        files : str
            Name of a dark current file to use for this detector
        """
        files = self.dark_list[instrument][detector]
        if len(files) == 1:
            return files[0]
        elif len(files) > 1:
            rand_index = np.random.randint(0, len(files) - 1)
            return files[rand_index]
        else:
            return None

    def get_lindark(self, instrument, detector):
        """
        Return the name of a linearized dark current file to
        use as input based on the detector being used

        Parameters
        ----------
        detector : str
            Name of detector being used

        Returns
        -------
        files : str
            Name of a linearized dark current file to use for this detector
        """
        files = self.lindark_list[instrument][detector]
        if len(files) == 1:
            return files[0]
        elif len(files) > 1:
            rand_index = np.random.randint(0, len(files) - 1)
            return files[rand_index]
        else:
            return None

    def get_readpattern_defs(self, filename=None):
        """Read in the readpattern definition file and return table.

        Returns
        -------
        tab : obj
            astropy.table.Table containing readpattern definitions
        filename : str
            Path to input file name
        """
        if filename is not None:
            return ascii.read(filename)

        tab = ascii.read(self.readpatt_def_file)
        return tab

    def get_reffile(self, refs, detector):
        """
        Return the appropriate reference file for detector
        and given reference file dictionary.

        Parameters
        ----------
        refs : dict
            dictionary in the form of:
             {'A1':'filenamea1.fits', 'A2':'filenamea2.fits'...}
             Containing reference file names
        detector : str
            Name of detector

        Returns
        -------
        refs: str
            Name of reference file appropriate for given detector
        """
        for key in refs:
            if detector in key:
                return refs[key]
        self.logger.error("WARNING: no file found for detector {} in {}"
              .format(detector, refs))

    def get_subarray_defs(self, filename=None):
        """Read in subarray definition file and return table

        Returns
        -------
        sub : obj
            astropy.table.Table containing subarray definition information
        filename : str
            Path to input file name
        """
        if filename is not None:
            return ascii.read(filename)

        sub = ascii.read(self.subarray_def_file)
        return sub

    def info_for_all_observations(self):
        """For a given dictionary of observation information, pull out the
        information needed by CRDS from each exposure, and save it as a
        tuple. Place the tuples in a list. Also, return a list of only the
        unique combinations of observation information.

        Returns
        -------
        all_combinations : list
            List of tuples. Each tuple contains the instrument, detector,
            filer, pupil, readout pattern, and exposure type for one exposure.

        unique_combinations : list
            List of tuples similar to ``all_combinations``, but containing
            only one copy of each unique tuple.
        """
        # Get all combinations of instrument, detector, filter, exp_type,
        all_combinations = []
        for i in range(len(self.info['Instrument'])):
            # Get instrument information for the exposure
            instrument = self.info['Instrument'][i]
            detector = self.info['detector'][i]
            if instrument == 'NIRCAM':
                detector = 'NRC{}'.format(detector)
                if '5' in detector:
                    filtername = self.info['LongFilter'][i]
                    pupilname = self.info['LongPupil'][i]
                    detector = detector.replace('5', 'LONG')
                else:
                    filtername = self.info['ShortFilter'][i]
                    pupilname = self.info['ShortPupil'][i]
            elif instrument == 'NIRISS':
                filtername = self.info['ShortFilter'][i]
                pupilname = self.info['ShortPupil'][i]
            elif instrument == 'FGS':
                filtername = 'N/A'
                pupilname = 'N/A'
            readpattern = self.info['ReadoutPattern'][i]

            if instrument == 'NIRCAM':
                exptype = 'NRC_IMAGE'
            elif instrument == 'NIRISS':
                exptype = 'NIS_IMAGE'
            elif instrument == 'FGS':
                exptype = 'FGS_IMAGE'

            entry = (instrument, detector, filtername, pupilname, readpattern, exptype)
            all_combinations.append(entry)
        unique_combinations = list(set(all_combinations))
        return all_combinations, unique_combinations

    @staticmethod
    def inverted_ipc_kernel_check(filename):
        """Given a CRDS-named IPC reference file, check for the
        existence of a file containing the inverse kernel, which
        is what Mirage really needs. This is based solely on filename,
        using the prefix that Mirage prepends to a kernel filename.

        Parameters
        ----------
        filename : str
            Fits file containing IPC correction kernel

        Returns
        -------
        inverted_filename : str
            Name of fits file containing the inverted IPC kernel

        exists : bool
            True if the file already exists, False if not
        """
        dirname, basename = os.path.split(filename)
        inverted_name = os.path.join(dirname, "Kernel_to_add_IPC_effects_from_{}".format(basename))
        return inverted_name, not os.path.isfile(inverted_name)

    def make_output_names(self):
        """Create output yaml file names to go with all of the
        entries in the dictionary
        """
        yaml_names = []
        fits_names = []

        if self.use_nonstsci_names:
            for i in range(len(self.info['Module'])):
                act = str(self.info['act_id'][i]).zfill(2)
                if self.info['Instrument'][i].lower() == 'niriss':
                    det = 'NIS'
                elif self.info['Instrument'][i].lower() == 'fgs':
                    det = 'FGS'
                else:
                    det = self.info['detector'][i]
                mode = self.info['Mode'][i]
                dither = str(self.info['dither'][i]).zfill(2)

                yaml_names.append(os.path.abspath(os.path.join(self.output_dir, 'Act{}_{}_{}_Dither{}.yaml'
                                                                            .format(act, det, mode, dither))))
                fits_names.append('Act{}_{}_{}_Dither{}_uncal.fits'.format(act, det, mode, dither))

        else:
            for i in range(len(self.info['Module'])):
                if self.info['Instrument'][i].upper() == 'NIRCAM':
                    fulldetector = 'nrc{}'.format(self.info['detector'][i].lower())
                else:
                    fulldetector = self.info['detector'][i].lower()
                outfilebase = self.create_output_name(self.info, index=i)
                outfile = "{}{}{}".format(outfilebase, fulldetector, '_uncal.fits')
                yamlout = "{}{}{}".format(outfilebase, fulldetector, '.yaml')

                yaml_names.append(yamlout)
                fits_names.append(outfile)

        self.info['yamlfile'] = yaml_names
        self.info['outputfits'] = fits_names
        # Table([self.info['yamlfile']]).pprint()

    def nonsidereal_pointing_updates(self):
        """Update the pointing info for non-sidereal observations using the
        requested observation date/time.
        """
        obs = np.array(self.info['ObservationID'])
        all_date_obs = np.array(self.info['date_obs'])
        all_time_obs = np.array(self.info['time_obs'])
        all_exposure = np.array(self.info['exposure'])
        all_apertures = np.array(self.info['aperture'])
        tracking = np.array(self.info['Tracking'])
        targs = np.array(self.info['TargetID'])
        inst = np.array(self.info['Instrument'])
        ra_from_pointing_file = np.array(self.info['ra'])
        dec_from_pointing_file = np.array(self.info['dec'])
        nonsidereal_index = np.where(np.array(tracking) == 'non-sidereal')
        all_nonsidereal_targs = targs[nonsidereal_index]
        all_nonsidereal_instruments = inst[nonsidereal_index]

        # Get a list of the unique (target, instrument) combinations for
        # non-sidereal observations
        inst_targs = [[t, i] for t, i in zip(all_nonsidereal_targs, all_nonsidereal_instruments)]
        ctr = Counter(tuple(x) for x in inst_targs)
        unique = [list(key) for key in ctr.keys()]

        # Check that all non-sidereal targets have catalogs associated with them
        #for targ, inst in zip(nonsidereal_targs, nonsidereal_instruments):
        for element in unique:
            targ, inst = element

            # Skip unsupported instruments
            if inst in ['NIRSpec', 'MIRI']:
                continue

            ns_catalog = get_nonsidereal_catalog_name(self.catalogs, targ, inst)
            catalog_table, pos_in_xy, vel_in_xy = read_nonsidereal_catalog(ns_catalog)

            # If the ephemeris_file column is present but equal to 'none', then
            # remove the column
            if 'ephemeris_file' in catalog_table.colnames:
                if catalog_table['ephemeris_file'][0].lower() == 'none':
                    catalog_table.remove_column('ephemeris_file')

            if 'ephemeris_file' in catalog_table.colnames:
                ephemeris_file = catalog_table['ephemeris_file'][0]
                ra_ephem, dec_ephem = ephemeris_tools.get_ephemeris(ephemeris_file)

            # Find the observations that use this target
            exp_index_this_target = targs == targ
            obs_this_target = np.unique(obs[exp_index_this_target])

            # Loop over observations
            for obs_name in obs_this_target:

                # Get date/time for every exposure
                obs_exp_indexes = np.where(obs == obs_name)
                obs_dates = all_date_obs[obs_exp_indexes]
                obs_times = all_time_obs[obs_exp_indexes]
                exposures = all_exposure[obs_exp_indexes]
                apertures = all_apertures[obs_exp_indexes]
                unique_apertures = np.unique(apertures)

                start_dates = []
                for date_obs, time_obs in zip(obs_dates, obs_times):
                    ob_time = '{}T{}'.format(date_obs, time_obs)
                    try:
                        start_dates.append(datetime.datetime.strptime(ob_time, '%Y-%m-%dT%H:%M:%S'))
                    except ValueError:
                        start_dates.append(datetime.datetime.strptime(ob_time, '%Y-%m-%dT%H:%M:%S.%f'))

                if 'ephemeris_file' in catalog_table.colnames:
                    all_times = [ephemeris_tools.to_timestamp(elem) for elem in start_dates]

                    # Create list of positions for all frames
                    try:
                        ra_target = ra_ephem(all_times)
                        dec_target = dec_ephem(all_times)

                    except ValueError:
                        raise ValueError(("Observation dates ({} - {}) are not present within the ephemeris file {}"
                                          .format(start_dates[0], start_dates[-1], ephemeris_file)))
                else:
                    if not pos_in_xy:
                        # Here we assume that the source (and aperture reference location)
                        # is located at the given RA, Dec at the start of the first exposure
                        base_ra, base_dec = parse_RA_Dec(catalog_table['x_or_RA'].data[0], catalog_table['y_or_Dec'].data[0])
                        ra_target = [base_ra]
                        dec_target = [base_dec]

                        ra_vel = catalog_table['x_or_RA_velocity'].data[0]
                        dec_vel = catalog_table['y_or_Dec_velocity'].data[0]

                        # If the source velocity is given in units of pixels/hour, then we need
                        # to multiply this by the appropriate pixel scale.
                        if vel_in_xy:
                            if len(unique_apertures) > 1:
                                if inst.lower() == 'nircam':
                                    det_ints = [int(ele.split('_')[0][-1]) for ele in unique_apertures]
                                    # If the observation contains NIRCam exposures in both the LW and
                                    # SW channels, then the source velocity is ambiguous due to the
                                    # different pixel scales. In that case, raise an exception.
                                    if np.min(det_ints) < 5 and np.max(det_ints) == 5:
                                        raise ValueError(('Non-sidereal source {} has no ephemeris file, and a velocity that '
                                                          'is specified in units of pixels/hour in the source catalog. '
                                                          'Since observation {} contains NIRCam apertures within both the '
                                                          'SW and LW channels (which have different pixel scales), '
                                                          'Mirage does not know which pixel scale to use '
                                                          'when placing the source.'.format(targ, obs_name)))

                            # In this case, there is a well-defined pixel scale, so we can translate
                            # velocities to units of arcsec/hour
                            siaf = pysiaf.Siaf(inst)[unique_apertures[0]]
                            ra_vel *= siaf.XSciScale
                            dec_vel *= siaf.XSciScale

                        # Calculate RA, Dec for each exposure given the velocities
                        for ob_date in start_dates[1:]:
                            delta_time = ob_date - start_dates[0]
                            delta_ra = ra_vel * delta_time.total_seconds() / 3600.
                            delta_dec = dec_vel * delta_time.total_seconds() / 3600.
                            ra_target.append(base_ra + delta_ra)
                            dec_target.append(base_dec + delta_dec)

                    else:
                        # Source location comes from the source catalog and is in units of pixels.
                        # This can't really be supported, since we don't know which detector the
                        # location is for. We could proceed, but Mirage would then put the source
                        # pixel (x, y) in every aperture/detector.
                        if len(unique_apertures) > 1:
                            raise ValueError(('Non-sidereal source {} has no ephemeris file, and a location that '
                                              'is specified in units of detector pixels in the source catalog. '
                                              'Since observation {} contains multiple apertures (implying different '
                                              'coordinate systems), Mirage does not know which coordinate system '
                                              'to use when placing the source.'.format(targ, obs_name)))

                        # If there is only a single aperture associated with the observation,
                        # then we can proceed. We first need to translate the given x, y position
                        # to RA, Dec
                        base_ra, base_dec = aperture_xy_to_radec(catalog_table['x_or_RA'].data[0],
                                                                 catalog_table['y_or_Dec'].data[0],
                                                                 inst, aperture, fiducial_ra, fiducial_dec, pav3)


                ra_from_pointing_file[obs_exp_indexes] = ra_target
                dec_from_pointing_file[obs_exp_indexes] = dec_target

        self.info['TargetRA'] = ra_from_pointing_file
        self.info['TargetDec'] = dec_from_pointing_file

        # Need to update these values (which come from the pointing file)
        # so that below we can adjust them for the different detectors/apertures
        self.info['ra'] = [np.float64(ele) for ele in ra_from_pointing_file]
        self.info['dec'] = [np.float64(ele) for ele in dec_from_pointing_file]

        # These go into the pointing in the yaml file
        self.info['ra_ref'] = ra_from_pointing_file
        self.info['dec_ref'] = dec_from_pointing_file

    def set_global_definitions(self):
        """Store the subarray definitions of all supported instruments."""
        # TODO: Investigate how this could be combined with the creation of
        #  self.configfiles in reffile_setup()

        self.global_subarray_definitions = {}
        self.global_readout_patterns = {}
        self.global_subarray_definition_files = {}
        self.global_readout_pattern_files = {}

        self.global_crosstalk_files = {}
        self.global_filtpupilcombo_files = {}
        self.global_filter_position_files = {}
        self.global_flux_cal_files = {}
        self.global_psf_wing_threshold_file = {}
        self.global_psfpath = {}
        # self.global_filter_throughput_files = {} ?

        for instrument in 'niriss fgs nircam miri nirspec'.split():
            if instrument.lower() == 'niriss':
                readout_pattern_file = 'niriss_readout_pattern.txt'
                subarray_def_file = 'niriss_subarrays.list'
                crosstalk_file = 'niriss_xtalk_zeros.txt'
                filtpupilcombo_file = 'niriss_dual_wheel_list.txt'
                filter_position_file = 'niriss_filter_and_pupil_wheel_positions.txt'
                flux_cal_file = 'niriss_zeropoints.list'
                psf_wing_threshold_file = 'niriss_psf_wing_rate_thresholds.txt'
                psfpath = os.path.join(self.datadir, 'niriss/gridded_psf_library')
            elif instrument.lower() == 'fgs':
                readout_pattern_file = 'guider_readout_pattern.txt'
                subarray_def_file = 'guider_subarrays.list'
                crosstalk_file = 'guider_xtalk_zeros.txt'
                filtpupilcombo_file = 'guider_filter_dummy.list'
                filter_position_file = 'dummy.txt'
                flux_cal_file = 'guider_zeropoints.list'
                psf_wing_threshold_file = 'fgs_psf_wing_rate_thresholds.txt'
                psfpath = os.path.join(self.datadir, 'fgs/gridded_psf_library')
            elif instrument.lower() == 'nircam':
                readout_pattern_file = 'nircam_read_pattern_definitions.list'
                subarray_def_file = 'NIRCam_subarray_definitions.list'
                crosstalk_file = 'xtalk20150303g0.errorcut.txt'
                filtpupilcombo_file = 'nircam_filter_pupil_pairings.list'
                filter_position_file = 'nircam_filter_and_pupil_wheel_positions.txt'
                flux_cal_file = 'NIRCam_zeropoints.list'
                psf_wing_threshold_file = 'nircam_psf_wing_rate_thresholds.txt'
                psfpath = os.path.join(self.datadir, 'nircam/gridded_psf_library')
            else:
                readout_pattern_file = 'N/A'
                subarray_def_file = 'N/A'
                crosstalk_file = 'N/A'
                filtpupilcombo_file = 'N/A'
                filter_position_file = 'N/A'
                flux_cal_file = 'N/A'
                psf_wing_threshold_file = 'N/A'
                psfpath = 'N/A'
            if instrument in 'niriss fgs nircam'.split():
                self.global_subarray_definitions[instrument] = self.get_subarray_defs(filename=os.path.join(self.modpath, 'config', subarray_def_file))
                self.global_readout_patterns[instrument] = self.get_readpattern_defs(filename=os.path.join(self.modpath, 'config', readout_pattern_file))
            self.global_subarray_definition_files[instrument] = os.path.join(self.modpath, 'config', subarray_def_file)
            self.global_readout_pattern_files[instrument] = os.path.join(self.modpath, 'config', readout_pattern_file)
            self.global_crosstalk_files[instrument] = os.path.join(self.modpath, 'config', crosstalk_file)
            self.global_filtpupilcombo_files[instrument] = os.path.join(self.modpath, 'config', filtpupilcombo_file)
            self.global_filter_position_files[instrument] = os.path.join(self.modpath, 'config', filter_position_file)
            self.global_flux_cal_files[instrument] = os.path.join(self.modpath, 'config', flux_cal_file)
            self.global_psf_wing_threshold_file[instrument] = os.path.join(self.modpath, 'config', psf_wing_threshold_file)
            self.global_psfpath[instrument] = psfpath

    def lowercase_dict_keys(self):
        """To make reference file override dictionary creation easier for
        users, allow the input keys to be case insensitive. Take the user
        input dictionary and translate all the keys to be lower case.
        """
        lower1 = {}
        for key1, val1 in self.reffile_overrides.items():
            if isinstance(val1, dict):
                lower2 = {}
                for key2, val2 in val1.items():
                    if isinstance(val2, dict):
                        lower3 = {}
                        for key3, val3 in val2.items():
                            if isinstance(val3, dict):
                                lower4 = {}
                                for key4, val4 in val3.items():
                                    if isinstance(val4, dict):
                                        lower5 = {}
                                        for key5, val5 in val4.items():
                                            if isinstance(val5, dict):
                                                lower6 = {}
                                                for key6, val6 in val5.items():
                                                    lower6[key6.lower()] = val6
                                                lower5[key5.lower()] = deepcopy(lower6)
                                            else:
                                                lower5[key5.lower()] = val5
                                        lower4[key4.lower()] = deepcopy(lower5)
                                    else:
                                        lower4[key4.lower()] = val4
                                lower3[key3.lower()] = deepcopy(lower4)
                            else:
                                lower3[key3.lower()] = val3
                        lower2[key2.lower()] = deepcopy(lower3)
                    else:
                        lower2[key2.lower()] = val2
                lower1[key1.lower()] = deepcopy(lower2)
            else:
                lower1[key1.lower()] = val1
        self.reffile_overrides = lower1

    def multiple_catalog_match(self, filter, cattype, matchlist):
        """
        Alert the user if more than one catalog matches the filter/pupil

        Parameters
        ----------
        filter : str
          Name of filter element
        cattype : str
          Type of catalog (e.g. pointsource)
        matchlist : list
          Matching catalog names
        """
        self.logger.warning("WARNING: multiple {} catalogs matched! Using the first.".format(cattype))
        self.logger.warning("Observation filter: {}".format(filter))
        self.logger.warning("Matched point source catalogs: {}".format(matchlist))

    def no_catalog_match(self, filter, cattype):
        """
        Alert user if no catalog match was found.

        Parameters
        ----------
        filter : str
          Name of filter element
        cattype : str
          Type of catalog (e.g. pointsource)

        """
        self.logger.warning("WARNING: unable to find filter ({}) name".format(filter))
        self.logger.warning("in any of the given {} inputs".format(cattype))
        self.logger.warning("Using the first input for now. Make sure input catalog names have")
        self.logger.warning("the appropriate filter name in the filename to get matching to work.")

    def path_defs(self):
        """Expand input files to have full paths"""
        if self.input_xml is not None:
            self.input_xml = os.path.abspath(os.path.expandvars(self.input_xml))
        if self.pointing_file is not None:
            self.pointing_file = os.path.abspath(os.path.expandvars(self.pointing_file))
        self.output_dir = os.path.abspath(os.path.expandvars(self.output_dir))
        self.simdata_output_dir = os.path.abspath(os.path.expandvars(self.simdata_output_dir))
        if self.table_file is not None:
            self.table_file = os.path.abspath(os.path.expandvars(self.table_file))

        ensure_dir_exists(self.output_dir)
        ensure_dir_exists(self.simdata_output_dir)

        if self.observation_list_file is not None:
            self.observation_list_file = os.path.abspath(os.path.expandvars(self.observation_list_file))

    def reffile_setup(self):
        """Create lists of reference files associate with each detector.

        Parameters
        ----------
        instrument : str
            Name of instrument
        """
        # Prepare to find files listed as 'config'
        # and set up PSF path

        # set up as dictionary of dictionaries
        self.configfiles = {}
        self.psfpath = {}
        self.psfbasename = {}
        self.psfpixfrac = {}
        self.reference_file_dir = {}

        for instrument in 'nircam niriss fgs'.split():
            self.configfiles[instrument] = {}
            self.psfpath[instrument] = os.path.join(self.datadir, instrument, 'gridded_psf_library')
            self.psfbasename[instrument] = instrument
            self.reference_file_dir[instrument] = os.path.join(self.datadir, instrument, 'reference_files')

            # Set instrument-specific file paths
            if instrument == 'nircam':
                self.psfpixfrac[instrument] = 0.25
            elif instrument == 'niriss':
                self.psfpixfrac[instrument] = 0.1
            elif instrument == 'fgs':
                self.psfpixfrac[instrument] = 0.1

            # Set global file paths
            self.configfiles[instrument]['filter_throughput'] = os.path.join(self.modpath, 'config', 'placeholder.txt')

        for instrument in 'miri nirspec'.split():
            self.configfiles[instrument] = {}
            self.psfpixfrac[instrument] = 0
            self.psfbasename[instrument] = 'N/A'

        # create empty dictionaries
        list_names = 'superbias linearity gain saturation ipc astrometric photom pam dark lindark'.split()
        for list_name in list_names:
            setattr(self, '{}_list'.format(list_name), {})

        self.det_list = {}
        self.det_list['nircam'] = ['A1', 'A2', 'A3', 'A4', 'A5', 'B1', 'B2', 'B3', 'B4', 'B5']
        self.det_list['niriss'] = ['NIS']
        self.det_list['fgs'] = ['G1', 'G2']
        self.det_list['nirspec'] = ['NRS']
        self.det_list['miri'] = ['MIR']

        for instrument in 'nircam niriss fgs miri nirspec'.split():
            for list_name in list_names:
                getattr(self, '{}_list'.format(list_name))[instrument] = {}

            if self.offline:
                # no access to central store. Set all files to none.
                for list_name in list_names:
                    if list_name in 'dark lindark'.split():
                        default_value = ['None']
                    else:
                        default_value = 'None'
                    for det in self.det_list[instrument]:
                        getattr(self, '{}_list'.format(list_name))[instrument][det] = default_value

            elif instrument == 'nircam':
                rawdark_dir = os.path.join(self.datadir, 'nircam/darks/raw')
                lindark_dir = os.path.join(self.datadir, 'nircam/darks/linearized')
                for det in self.det_list[instrument]:
                    self.dark_list[instrument][det] = glob(os.path.join(rawdark_dir, det, '*.fits'))
                    self.lindark_list[instrument][det] = glob(os.path.join(lindark_dir, det, '*.fits'))

            elif instrument in ['nirspec', 'miri']:
                for key in 'subarray_def_file fluxcal filtpupil_pairs readpatt_def_file crosstalk ' \
                           'dq_init_config saturation_config superbias_config refpix_config ' \
                           'linearity_config filter_throughput'.split():
                    self.configfiles[instrument][key] = 'N/A'
                default_value = 'none'
                for list_name in list_names:
                    for det in self.det_list[instrument]:
                        getattr(self, '{}_list'.format(list_name))[instrument][det] = default_value

            else:  # niriss and fgs
                for det in self.det_list[instrument]:
                    if det == 'G1':
                        self.dark_list[instrument][det] = glob(os.path.join(self.datadir, 'fgs/darks/raw', FGS1_DARK_SEARCH_STRING))
                        self.lindark_list[instrument][det] = glob(os.path.join(self.datadir, 'fgs/darks/linearized', FGS1_DARK_SEARCH_STRING))

                    elif det == 'G2':
                        self.dark_list[instrument][det] = glob(os.path.join(self.datadir, 'fgs/darks/raw', FGS2_DARK_SEARCH_STRING))
                        self.lindark_list[instrument][det] = glob(os.path.join(self.datadir, 'fgs/darks/linearized', FGS2_DARK_SEARCH_STRING))

                    elif det == 'NIS':
                        self.dark_list[instrument][det] = glob(os.path.join(self.datadir, 'niriss/darks/raw',
                                                                            '*uncal.fits'))
                        self.lindark_list[instrument][det] = glob(os.path.join(self.datadir, 'niriss/darks/linearized',
                                                                               '*linear_dark_prep_object.fits'))

    def set_config(self, file, prop):
        """
        If a given file is listed as 'config'
        then set it in the yaml output as being in
        the config subdirectory.

        Parameters
        ----------
        file : str
            Name of the input file
        prop : str
            Type of file that file is.

        Returns:
        --------
        file : str
            Full path name to the input file
        """
        if file.lower() not in ['config']:
            file = os.path.abspath(file)
        elif file.lower() == 'config':
            file = os.path.join(self.modpath, 'config', self.configfiles[prop])
        return file

    def get_psf_path(self):
        """ Create a list of the path to the PSF library directory for
        each observation/visit/exposure in the APT program.

        Parameters:
        -----------
        psf_paths : list, str, or None
            Either a list of the paths to the PSF library(ies), with a
            length equal to the number of activities in the APT program,
            a string containing the path to one PSF library,
            or None. If a list, each path will be written
            chronologically into each yaml file. If a string, that path
            will be written into every yaml file. If None, the
            default PSF library path will be used for all yamls.

        Returns:
        --------
        paths_out : list
            The list of paths to the PSF library(ies), with a length
            equal to the number of activities in the APT program.
        """
        exp_ids = sorted(list(set(self.info['entry_number'])))
        exp_id_indices = []
        for exp_id in self.info['entry_number']:
            exp_id_indices.append(exp_ids.index(exp_id))
        n_activities = len(exp_ids)

        # If no path explicitly provided, use the default path.
        if self.psf_paths is None:
            self.logger.info('No PSF path provided. Using default path as PSF path for all yamls.')
            paths_out = []
            for instrument in self.info['Instrument']:
                default_path = self.config_information['global_psfpath'][instrument.lower()]
                paths_out.append(default_path)
            return paths_out

        elif isinstance(self.psf_paths, str):
            self.logger.info('Using provided PSF path.')
            paths_out = [self.psf_paths] * len(self.info['act_id'])
            return paths_out

        elif isinstance(self.psf_paths, list) and len(self.psf_paths) != n_activities:
            raise ValueError('Invalid PSF paths parameter provided. Please '
                             'provide the psf_paths in the form of a list of '
                             'strings with a length equal to the number of '
                             'activities in the APT program ({}), not equal to {}.'
                             .format(n_activities, len(self.psf_paths)))

        elif isinstance(self.psf_paths, list):
            self.logger.info('Using provided PSF paths.')
            paths_out = [self.psf_paths[i] for i in exp_id_indices]
            return paths_out

        elif not isinstance(self.psf_paths, list) or not isinstance(self.psf_paths, str):
            raise TypeError('Invalid PSF paths parameter provided. Please '
                            'provide the psf_paths in the form of a list or string, not'
                            '{}'.format(type(self.psf_paths)))

    def reffiles_from_dict(self, obs_params):
        """For a given set of observing parameters (instrument, detector,
        filter, etc), check to see if the user has provided any reference
        files to use. These will override the results of the CRDS query.

        Parameters
        ----------
        obs_params : tup
            (instrument, detector, filter, pupil, readpattern, exptype)

        Returns
        -------
        files : dict
            Dictionary containing the reference files that match the
            observing parameters
        """
        files = {}
        (instrument, detector, filtername, pupilname, readpattern, exptype) = obs_params
        instrument = instrument.lower()
        detector = detector.lower()
        filtername = filtername.lower()
        pupilname = pupilname.lower()
        readpattern = readpattern.lower()
        exptype = exptype.lower()

        # Make all of the nested dictionaries such that they have case-
        # insensitive keys. This will allow the user to use upper or lower
        # case for keys in their input dictionaries.
        self.lowercase_dict_keys()

        # CRDS uses NRCALONG rather than NRCA5.
        if 'long' in detector:
            detector = detector.replace('long', '5')

        # For NIRISS, set filter and pupil names according to where they
        # really are.
        if instrument == 'niriss':
            if filtername.upper() in NIRISS_PUPIL_WHEEL_ELEMENTS:
                temp = deepcopy(filtername)
                filtername = deepcopy(pupilname)
                pupilname = temp
                if pupilname == 'clear':
                    pupilname = 'clearp'

        # superbias
        try:
            if instrument in ['nircam', 'fgs']:
                files['superbias'] = self.reffile_overrides[instrument]['superbias'][detector][readpattern]
            elif instrument == 'niriss':
                files['superbias'] = self.reffile_overrides[instrument]['superbias'][readpattern]
        except KeyError:
            files['superbias'] = 'none'

        # linearity
        try:
            if instrument in ['nircam', 'fgs']:
                files['linearity'] = self.reffile_overrides[instrument]['linearity'][detector]
            elif instrument == 'niriss':
                files['linearity'] = self.reffile_overrides[instrument]['linearity']
        except KeyError:
            files['linearity'] = 'none'

        # saturation
        try:
            if instrument in ['nircam', 'fgs']:
                files['saturation'] = self.reffile_overrides[instrument]['saturation'][detector]
            elif instrument == 'niriss':
                files['saturation'] = self.reffile_overrides[instrument]['saturation']
        except KeyError:
            files['saturation'] = 'none'

        # gain
        try:
            if instrument in ['nircam', 'fgs']:
                files['gain'] = self.reffile_overrides[instrument]['gain'][detector]
            elif instrument == 'niriss':
                files['gain'] = self.reffile_overrides[instrument]['gain']
        except KeyError:
            files['gain'] = 'none'

        # distortion
        try:
            if instrument == 'nircam':
                files['distortion'] = self.reffile_overrides[instrument]['distortion'][detector][filtername][exptype]
            elif instrument == 'niriss':
                files['distortion'] = self.reffile_overrides[instrument]['distortion'][pupilname][exptype]
            elif instrument == 'fgs':
                files['distortion'] = self.reffile_overrides[instrument]['distortion'][detector][exptype]
        except KeyError:
            files['distortion'] = 'none'

        # ipc
        try:
            if instrument in ['nircam', 'fgs']:
                files['ipc'] = self.reffile_overrides[instrument]['ipc'][detector]
            elif instrument == 'niriss':
                files['ipc'] = self.reffile_overrides[instrument]['ipc']
        except KeyError:
            files['ipc'] = 'none'

        # transmission image
        try:
            if instrument == 'nircam':
                files['transmission'] = self.reffile_overrides[instrument]['transmission'][detector][filtername][pupilname]
            elif instrument == 'niriss':
                files['transmission'] = self.reffile_overrides[instrument]['transmission'][filtername][pupilname]
            elif instrument == 'fgs':
                files['transmission'] = self.reffile_overrides[instrument]['transmission'][detector]
        except KeyError:
            files['transmission'] = 'none'

        # bad pixel map
        try:
            if instrument == 'nircam':
                files['badpixmask'] = self.reffile_overrides[instrument]['badpixmask'][detector]
            elif instrument == 'niriss':
                files['badpixmask'] = self.reffile_overrides[instrument]['badpixmask']
            elif instrument == 'fgs':
                files['badpixmask'] = self.reffile_overrides[instrument]['badpixmask'][detector][exptype]
        except KeyError:
            files['badpixmask'] = 'none'

        # flat field file
        try:
            if instrument == 'nircam':
                files['pixelflat'] = self.reffile_overrides[instrument]['pixelflat'][detector][filtername][pupilname]
            elif instrument == 'niriss':
                files['pixelflat'] = self.reffile_overrides[instrument]['pixelflat'][filtername][pupilname]
            elif instrument == 'fgs':
                files['pixelflat'] = self.reffile_overrides[instrument]['pixelflat'][detector][exptype]
        except KeyError:
            files['pixelflat'] = 'none'

        # photom reference file
        try:
            files['photom'] = self.reffile_overrides[instrument]['photom'][detector]
        except KeyError:
            files['photom'] = 'none'

        return files

    def table_to_dict(self, tab):
        """
        Convert the ascii table of observations to a dictionary

        Parameters
        ----------
        tab : obj
            astropy.table.Table containing observation information

        Returns
        -------
        dict : dict
            Dictionary of observation information
        """
        dict = {}
        for colname in tab.colnames:
            dict[colname] = tab[colname].data
        return dict

    def write_yaml(self, input):
        """
        Create yaml file for a single exposure/detector

        Parameters
        ----------
        input : dict
            dictionary containing all needed exposure
            information for one exposure
        """
        instrument = input['Instrument']
        # select the right filter
        if input['detector'] in ['NIS']:
            # if input['APTTemplate'] == 'NirissExternalCalibration': 'NirissImaging':
            filtkey = 'FilterWheel'
            pupilkey = 'PupilWheel'
            # set the FilterWheel and PupilWheel for NIRISS
            if input['APTTemplate'] in ['NirissAmi']:
                filter_name = input['Filter']

                # TA and direct images will be set to imaging mode
                # rather than ami mode
                if input['Mode'].lower() == 'imaging':
                    if filter_name in NIRISS_PUPIL_WHEEL_ELEMENTS:
                        input[pupilkey] = filter_name
                        input[filtkey] = 'CLEAR'
                    elif filter_name in NIRISS_FILTER_WHEEL_ELEMENTS:
                        input[pupilkey] = 'CLEARP'
                        input[filtkey] = filter_name
                else:
                    input[filtkey] = filter_name
                    input[pupilkey] = 'NRM'

            elif input['APTTemplate'] not in ['NirissExternalCalibration', 'NirissWfss']:
                filter_name = input['Filter']
                if filter_name in NIRISS_PUPIL_WHEEL_ELEMENTS:
                    input[pupilkey] = filter_name
                    input[filtkey] = 'CLEAR'
                elif filter_name in NIRISS_FILTER_WHEEL_ELEMENTS:
                    input[pupilkey] = 'CLEARP'
                    input[filtkey] = filter_name
                else:
                    raise RuntimeError('Filter {} not valid'.format(filter_name))
            catkey = ''
        elif input['detector'] in ['FGS']:
            filtkey = 'FilterWheel'
            pupilkey = 'PupilWheel'
            catkey = ''
        elif input['detector'] in ['NRS', 'MIR']:
            filtkey = 'FilterWheel'
            pupilkey = 'PupilWheel'
            catkey = ''
        else:
            if int(input['detector'][-1]) < 5:
                filtkey = 'ShortFilter'
                pupilkey = 'ShortPupil'
                catkey = 'sw'
            else:
                filtkey = 'LongFilter'
                pupilkey = 'LongPupil'
                catkey = 'lw'

        outfile = input['outputfits']
        yamlout = input['yamlfile']

        yamlout = os.path.join(self.output_dir, yamlout)
        with open(yamlout, 'w') as f:
            f.write('Inst:\n')
            f.write('  instrument: {}          # Instrument name\n'.format(instrument))
            f.write('  mode: {}                # Observation mode (e.g. imaging, WFSS)\n'.format(input['Mode']))
            f.write('  use_JWST_pipeline: {}   # Use pipeline in data transformations\n'.format(input['use_JWST_pipeline']))
            f.write('\n')
            f.write('Readout:\n')
            f.write('  readpatt: {}        # Readout pattern (RAPID, BRIGHT2, etc) overrides nframe, nskip unless it is not recognized\n'.format(input['ReadoutPattern']))
            f.write('  ngroup: {}              # Number of groups in integration\n'.format(input['Groups']))
            f.write('  nint: {}          # Number of integrations per exposure\n'.format(input['Integrations']))
            f.write('  namp: {}          # Number of amplifiers used to read out detector\n'.format(input['namp']))
            f.write('  resets_bet_ints: {} #Number of detector resets between integrations\n'.format(self.resets_bet_ints))

            if instrument.lower() == 'nircam':
                # if input['aperture'] in ['NRCA3_DHSPIL', 'NRCB4_DHSPIL']:
                if 'NRCA3_DHSPIL' in input['aperture'] or 'NRCB4_DHSPIL' in input['aperture']: # in ['NRCA3_DHSPIL', 'NRCB4_DHSPIL']:
                    full_ap = input['aperture']
                else:
                    apunder = input['aperture'].find('_')
                    full_ap = 'NRC' + input['detector'] + '_' + input['aperture'][apunder + 1:]
            if instrument.lower() in ['niriss', 'fgs']:
                full_ap = input['aperture']

            possible_apertures = pysiaf.Siaf(instrument).apernames
            if full_ap not in possible_apertures:
                raise ValueError('Unrecognized aperture name: {}'.format(full_ap))

            f.write('  array_name: {}    # Name of array (FULL, SUB160, SUB64P, etc)\n'.format(full_ap))
            f.write('  intermediate_aperture: {}   # Name of intermediate aperture used in NIRCam Grism time series obs.\n'.format(input['grismts_intermediate_aperture']))
            f.write('  PPS_aperture: {}  # Original aperture value supplied by PPS.\n'.format(input['pps_aperture']))
            f.write('  filter: {}       # Filter of simulated data (F090W, F322W2, etc)\n'.format(input[filtkey]))
            f.write('  pupil: {}        # Pupil element for simulated data (CLEAR, GRISMC, etc)\n'.format(input[pupilkey]))
            f.write('\n')
            f.write('Reffiles:                                 # Set to None or leave blank if you wish to skip that step\n')
            f.write('  dark: {}   # Dark current integration used as the base\n'.format(input['dark']))
            f.write('  linearized_darkfile: {}   # Linearized dark ramp to use as input. Supercedes dark above\n'.format(input['lindark']))
            f.write('  badpixmask: {}   # If linearized dark is used, populate output DQ extensions using this file\n'.format(input['badpixmask']))
            f.write('  superbias: {}     # Superbias file. Set to None or leave blank if not using\n'.format(input['superbias']))
            f.write('  linearity: {}    # linearity correction coefficients\n'.format(input['linearity']))
            f.write('  saturation: {}    # well depth reference files\n'.format(input['saturation']))
            f.write('  gain: {} # Gain map\n'.format(input['gain']))
            f.write('  pixelflat: {}    # Flat field file to use for un-flattening output\n'.format(input['pixelflat']))
            f.write('  illumflat: None                               # Illumination flat field file\n')
            f.write('  astrometric: {}  # Astrometric distortion file (asdf)\n'.format(input['astrometric']))
            f.write('  photom: {}   # cal pipeline photom reference file\n'.format(input['photom']))
            f.write('  ipc: {} # File containing IPC kernel to apply\n'.format(input['ipc']))
            f.write(('  invertIPC: {}      # Invert the IPC kernel before the convolution. True or False. Use True if the kernel is '
                     'designed for the removal of IPC effects, like the JWST reference files are.\n'.format(input['invert_ipc'])))
            f.write('  occult: None                                    # Occulting spots correction image\n')
            f.write(('  transmission: {}      # Transmission image containing fractional throughput map. (e.g. to imprint occulters into fov\n'
                     .format(input['transmission'])))
            f.write(('  subarray_defs: {} # File that contains a list of all possible subarray names and coordinates\n'
                     .format(input['subarray_def_file'])))
            f.write(('  readpattdefs: {}  # File that contains a list of all possible readout pattern names and associated '
                     'NFRAME/NSKIP values\n'.format(input['readpatt_def_file'])))
            f.write('  crosstalk: {}   # File containing crosstalk coefficients\n'.format(input['crosstalk_file']))
            f.write(('  filtpupilcombo: {}   # File that lists the filter wheel element / pupil wheel element combinations. '
                     'Used only in writing output file\n'.format(input['filtpupilcombo_file'])))
            f.write(('  filter_wheel_positions: {}  # File containing resolver wheel positions for each filter/pupil\n'.format(input['filter_position_file'])))
            f.write(('  flux_cal: {} # File that lists flux conversion factor and pivot wavelength for each filter. Only '
                     'used when making direct image outputs to be fed into the grism disperser code.\n'.format(input['flux_cal_file'] )))
            f.write('  filter_throughput: {} #File containing filter throughput curve\n'.format(self.configfiles[instrument.lower()]['filter_throughput']))
            f.write('  ')
            f.write('\n')
            f.write('nonlin:\n')
            f.write('  limit: 60000.0                           # Upper singal limit to which nonlinearity is applied (ADU)\n')
            f.write('  accuracy: 0.000001                        # Non-linearity accuracy threshold\n')
            f.write('  maxiter: 10                              # Maximum number of iterations to use when applying non-linearity\n')
            f.write('  robberto:  False                         # Use Massimo Robberto type non-linearity coefficients\n')
            f.write('\n')
            f.write('cosmicRay:\n')
            cosmic_ray_path = os.path.join(self.datadir, instrument.lower(), 'cosmic_ray_library')
            f.write('  path: {}               # Path to CR library\n'.format(cosmic_ray_path))
            f.write('  library: {}    # Type of cosmic rayenvironment (SUNMAX, SUNMIN, FLARE)\n'.format(input['CosmicRayLibrary']))
            f.write('  scale: {}     # Cosmic ray scaling factor\n'.format(input['CosmicRayScale']))
            # temporary tweak here to make it work with NIRISS
            detector_label = input['detector']

            if instrument.lower() in ['nircam', 'wfsc']:
                # detector_label = input['detector']
                f.write('  suffix: IPC_NIRCam_{}    # Suffix of library file names\n'.format(
                    detector_label))
            elif instrument.lower() == 'niriss':
                f.write('  suffix: IPC_NIRISS_{}    # Suffix of library file names\n'.format(
                    detector_label))
            elif instrument.lower() == 'fgs':
                if detector_label == 'G1':
                    detector_string = 'GUIDER1'
                elif detector_label == 'G2':
                    detector_string = 'GUIDER2'
                f.write('  suffix: IPC_FGS_{}    # Suffix of library file names\n'.format(
                    detector_string))
            f.write('  seed: {}                 # Seed for random number generator\n'.format(np.random.randint(1, 2**32-2)))
            f.write('\n')
            f.write('simSignals:\n')
            if instrument.lower() in ['nircam', 'wfsc']:
                PointSourceCatalog = input['{}_ptsrc'.format(catkey)]
                GalaxyCatalog = input['{}_galcat'.format(catkey)]
                ExtendedCatalog = input['{}_ext'.format(catkey)]
                ExtendedScale = input['{}_extscl'.format(catkey)]
                ExtendedCenter = input['{}_extcent'.format(catkey)]
                MovingTargetList = input['{}_movptsrc'.format(catkey)]
                MovingTargetSersic = input['{}_movgal'.format(catkey)]
                MovingTargetExtended = input['{}_movext'.format(catkey)]
                MovingTargetConvolveExtended = input['{}_movconv'.format(catkey)]
                MovingTargetToTrack = input['{}_solarsys'.format(catkey)]
                ImagingTSOCatalog = input['{}_img_tso'.format(catkey)]
                GrismTSOCatalog = input['{}_grism_tso'.format(catkey)]
                BackgroundRate = input['{}_bkgd'.format(catkey)]
            elif instrument.lower() in ['niriss', 'fgs']:
                PointSourceCatalog = input['PointSourceCatalog']
                GalaxyCatalog = input['GalaxyCatalog']
                ExtendedCatalog = input['ExtendedCatalog']
                ExtendedScale = input['ExtendedScale']
                ExtendedCenter = input['ExtendedCenter']
                MovingTargetList = input['MovingTargetList']
                MovingTargetSersic = input['MovingTargetSersic']
                MovingTargetExtended = input['MovingTargetExtended']
                MovingTargetConvolveExtended = input['MovingTargetConvolveExtended']
                MovingTargetToTrack = input['MovingTargetToTrack']
                ImagingTSOCatalog = input['ImagingTSOCatalog']
                GrismTSOCatalog = input['GrismTSOCatalog']
                BackgroundRate = input['BackgroundRate']

            f.write(('  pointsource: {}   #File containing a list of point sources to add (x, y locations and magnitudes)\n'
                     .format(PointSourceCatalog)))
            f.write('  gridded_psf_library_row_padding: 4  # Number of outer rows and columns to avoid when evaluating library. RECOMMEND 4.\n')
            f.write('  psf_wing_threshold_file: {}   # File defining PSF sizes versus magnitude\n'.format(input['psf_wing_threshold_file']))
            f.write('  add_psf_wings: {}  # Whether or not to place the core of the psf from the gridded library into an image of the wings before adding.\n'.format(self.add_psf_wings))
            f.write('  psfpath: {}   #Path to PSF library\n'.format(input['psfpath']))
            f.write('  psfwfe: {}   #PSF WFE value (predicted or requirements)\n'.format(self.psfwfe))
            f.write('  psfwfegroup: {}      #WFE realization group (0 to 4)\n'.format(self.psfwfegroup))
            f.write(('  galaxyListFile: {}    #File containing a list of positions/ellipticities/magnitudes of galaxies '
                     'to simulate\n'.format(GalaxyCatalog)))
            f.write('  extended: {}          #Extended emission count rate image file name\n'.format(ExtendedCatalog))
            f.write('  extendedscale: {}                          #Scaling factor for extended emission image\n'.format(ExtendedScale))
            f.write(('  extendedCenter: {}                   #x, y pixel location at which to place the extended image '
                     'if it is smaller than the output array size\n'.format(ExtendedCenter)))
            f.write(('  PSFConvolveExtended: {} #Convolve the extended image with the PSF before adding to the output '
                     'image (True or False)\n'.format(self.convolve_extended)))
            f.write(('  movingTargetList: {}          #Name of file containing a list of point source moving targets (e.g. '
                     'KBOs, asteroids) to add.\n'.format(MovingTargetList)))
            f.write(('  movingTargetSersic: {}  #ascii file containing a list of 2D sersic profiles to have moving through '
                     'the field\n'.format(MovingTargetSersic)))
            f.write(('  movingTargetExtended: {}      #ascii file containing a list of stamp images to add as moving targets '
                     '(planets, moons, etc)\n'.format(MovingTargetExtended)))
            f.write(('  movingTargetToTrack: {} #File containing a single moving target which JWST will track during '
                     'observation (e.g. a planet, moon, KBO, asteroid)	This file will only be used if mode is set to '
                     '"moving_target" \n'.format(MovingTargetToTrack)))
            f.write(('  tso_imaging_catalog: {} #Catalog of (generally one) source for Imaging Time Series observation\n'.format(ImagingTSOCatalog)))
            f.write(('  tso_grism_catalog: {} #Catalog of (generally one) source for Grism Time Series observation\n'.format(GrismTSOCatalog)))
            f.write('  zodiacal:  None                          #Zodiacal light count rate image file \n')
            f.write('  zodiscale:  1.0                            #Zodi scaling factor\n')
            f.write('  scattered:  None                          #Scattered light count rate image file\n')
            f.write('  scatteredscale: 1.0                        #Scattered light scaling factor\n')
            f.write(('  bkgdrate: {}                         #Constant background count rate (ADU/sec/pixel in an undispersed image) or '
                     '"high","medium","low" similar to what is used in the ETC\n'.format(BackgroundRate)))
            f.write(('  poissonseed: {}                  #Random number generator seed for Poisson simulation)\n'
                     .format(np.random.randint(1, 2**32-2))))
            f.write('  photonyield: True                         #Apply photon yield in simulation\n')
            f.write('  pymethod: True                            #Use double Poisson simulation for photon yield\n')
            f.write('  expand_catalog_for_segments: {}                     # Expand catalog for 18 segments and use distinct PSFs\n'
                    .format(self.expand_catalog_for_segments))
            f.write('  use_dateobs_for_background: {}          # Use date_obs below to deternine background. If False, bkgdrate is used.\n'
                    .format(self.dateobs_for_background))
            f.write('  signal_low_limit_for_segmap: {}         # Lower signal limit to include pix in segmentation map. See below for units.\n'
                    .format(self.segmentation_threshold))
            f.write(('  signal_low_limit_for_segmap_units: {}  # Units of signal_low_limit_for_segmap. Can be: [ADU/sec, e/sec, MJy/sr, '
                     'ergs/cm2/a, ergs/cm2/hz]\n'.format(self.segmentation_threshold_units)))
            f.write('  add_ghosts: {}  # Add optical ghosts associated with astronomical sources\n'.format(self.add_ghosts))
            f.write('  PSFConvolveGhosts: {}  # Convolve ghost stamp images with instrument PSF before adding\n'.format(self.convolve_ghosts))
            f.write('\n')
            f.write('Telescope:\n')
            f.write('  ra: {}                      # RA of simulated pointing\n'.format(input['ra_ref']))
            f.write('  dec: {}                    # Dec of simulated pointing\n'.format(input['dec_ref']))
            if 'pav3' in input.keys():
                pav3_value = input['pav3']
            else:
                pav3_value = input['PAV3']
            f.write('  rotation: {}                    # PA_V3 in degrees, i.e. the position angle of the V3 axis at V1 (V2=0, V3=0) measured from N to E.\n'.format(pav3_value))
            f.write('  tracking: {}   #Telescope tracking. Can be sidereal or non-sidereal\n'.format(input['Tracking']))
            f.write('\n')
            f.write('Output:\n')
            # f.write('  use_stsci_output_name: {} # Output filename should follow STScI naming conventions (True/False)\n'.format(outtf))
            f.write('  directory: {}  # Output directory\n'.format(self.simdata_output_dir))
            f.write('  file: {}   # Output filename\n'.format(outfile))
            f.write(("  datatype: {} # Type of data to save. 'linear' for linearized ramp. 'raw' for raw ramp. 'linear, "
                     "raw' for both\n".format(self.datatype)))
            f.write('  format: DMS          # Output file format Options: DMS, SSR(not yet implemented)\n')
            f.write('  save_intermediates: False   # Save intermediate products separately (point source image, etc)\n')
            f.write('  grism_source_image: {}   # grism\n'.format(input['grism_source_image']))
            f.write('  unsigned: True   # Output unsigned integers? (0-65535 if true. -32768 to 32768 if false)\n')
            f.write('  dmsOrient: True    # Output in DMS orientation (vs. fitswriter orientation).\n')
            f.write('  program_number: {}    # Program Number\n'.format(input['ProposalID']))
            f.write('  title: {}   # Program title\n'.format(input['Title'].replace(':', ', ')))
            f.write('  PI_Name: {}  # Proposal PI Name\n'.format(input['PI_Name']))
            f.write('  Proposal_category: {}  # Proposal category\n'.format(input['Proposal_category']))
            f.write('  Science_category: {}  # Science category\n'.format(input['Science_category']))
            f.write('  target_name: {}  # Name of target\n'.format(input['TargetID']))

            # For now, skip populating the target RA and Dec in WFSC data.
            # The read_xxxxx funtions for these observation types will have
            # to be updated to make use of the proposal_parameter_dictionary
            if np.isreal(input['TargetRA']):
                input['TargetRA'] = str(input['TargetRA'])
                input['TargetDec'] = str(input['TargetDec'])
            if input['TargetRA'] != '0':
                ra_degrees, dec_degrees = utils.parse_RA_Dec(input['TargetRA'], input['TargetDec'])
            else:
                ra_degrees = 0.
                dec_degrees = 0.

            f.write('  target_ra: {}  # RA of the target, from APT file.\n'.format(ra_degrees))
            f.write('  target_dec: {}  # Dec of the target, from APT file.\n'.format(dec_degrees))
            f.write("  observation_number: '{}'    # Observation Number\n".format(input['obs_num']))
            f.write('  observation_label: {}    # User-generated observation Label\n'.format(input['obs_label'].strip()))
            f.write("  visit_number: '{}'    # Visit Number\n".format(input['visit_num']))
            f.write("  visit_group: '{}'    # Visit Group\n".format(input['visit_group']))
            f.write("  visit_id: '{}'    # Visit ID\n".format(input['visit_id']))
            f.write("  sequence_id: '{}'    # Sequence ID\n".format(input['sequence_id']))
            f.write("  activity_id: '{}'    # Activity ID. Increment with each exposure.\n".format(input['act_id']))
            f.write("  exposure_number: '{}'    # Exposure Number\n".format(input['exposure']))
            f.write("  obs_id: '{}'   # Observation ID number\n".format(input['observation_id']))
            f.write("  date_obs: '{}'  # Date of observation\n".format(input['date_obs']))
            f.write("  time_obs: '{}'  # Time of observation\n".format(input['time_obs']))
            # f.write("  obs_template: '{}'  # Observation template\n".format(input['obs_template']))
            f.write("  primary_dither_type: {}  # Primary dither pattern name\n".format(input['PrimaryDitherType']))
            f.write("  total_primary_dither_positions: {}  # Total number of primary dither positions\n".format(input['PrimaryDithers']))
            f.write("  primary_dither_position: {}  # Primary dither position number\n".format(int(input['primary_dither_num'])))
            f.write("  subpix_dither_type: {}  # Subpixel dither pattern name\n".format(input['SubpixelDitherType']))
            # For WFSS we need to strip out the '-Points' from
            # the number of subpixel positions entry
            dash = -1
            if isinstance(input['SubpixelPositions'], str):
                dash = input['SubpixelPositions'].find('-')
            if (dash == -1):
                val = input['SubpixelPositions']
            else:
                val = input['SubpixelPositions'][0:dash]
            if val == 'None':
                val = 1
            # try:
            #     dash = input['SubpixelPositions'].find('-')
            #     val = input['SubpixelPositions'][0:dash]
            # except:
            #     val = input['SubpixelPositions']
            f.write("  total_subpix_dither_positions: {}  # Total number of subpixel dither positions\n".format(val))
            f.write("  subpix_dither_position: {}  # Subpixel dither position number\n".format(int(input['subpix_dither_num'])))
            f.write("  xoffset: {}  # Dither pointing offset in x (arcsec)\n".format(input['idlx']))
            f.write("  yoffset: {}  # Dither pointing offset in y (arcsec)\n".format(input['idly']))

        return yamlout

    def add_options(self, parser=None, usage=None):
        if parser is None:
            parser = argparse.ArgumentParser(usage=usage, description='Simulate JWST ramp')
        parser.add_argument("--input_xml", help='XML file from APT describing the observations.')
        parser.add_argument("--pointing_file", help='Pointing file from APT describing observations.')
        parser.add_argument("--datatype", help='Type of data to save. Can be "linear", "raw" or "linear, raw"', default="linear")
        parser.add_argument("--output_dir", help='Directory into which the yaml files are output', default='./')
        parser.add_argument("--table_file", help='Ascii table containing observation info. Use this or xml + pointing files.', default=None)
        parser.add_argument("--use_nonstsci_names", help="Use STScI naming convention for output files", action='store_true')
        parser.add_argument("--subarray_def_file", help="Ascii file containing subarray definitions", default='config')
        parser.add_argument("--readpatt_def_file", help='Ascii file containing readout pattern definitions', default='config')
        parser.add_argument("--crosstalk", help="Crosstalk coefficient file", default='config')
        parser.add_argument("--filtpupil_pairs", help="List of paired filter/pupil elements", default='config')
        parser.add_argument("--fluxcal", help="File with zeropoints per filter", default='config')
        parser.add_argument("--dq_init_config", help="DQ Initialization config file", default='config')
        parser.add_argument("--saturation_config", help="Saturation config file", default='config')
        parser.add_argument("--superbias_config", help="Superbias subtraction config file", default='config')
        parser.add_argument("--refpix_config", help="Refpix subtraction config file", default='config')
        parser.add_argument("--linearity_config", help="Linearity config file", default='config')
        parser.add_argument("--observation_list_file", help="Table file containing epoch start times, telescope roll angles, catalogs for each observation", default=None)
        parser.add_argument("--use_JWST_pipeline", help='True/False', action='store_true')
        parser.add_argument("--use_linearized_darks", help='True/False', action='store_true')
        parser.add_argument("--simdata_output_dir", help='Output directory for simulated exposure files', default='./')
        parser.add_argument("--psfpath", help='Directory containing PSF library',
                            default=os.path.join(self.datadir, 'gridded_psf_library'))
        parser.add_argument("--psfbasename", help="Basename of the files in the PSF library", default='nircam')
        parser.add_argument("--psfpixfrac", help="Subpixel centering resolution of files in PSF library", default=0.25)
        parser.add_argument("--psfwfe", help="Wavefront error value to use for PSFs", default='predicted')
        parser.add_argument("--psfwfegroup", help="Realization index number for PSF files", default=0)
        parser.add_argument("--resets_bet_ints", help="Number of detector resets between integrations", default=1)
        parser.add_argument("--tracking", help="Type of telescope tracking: 'sidereal' or 'non-sidereal'", default='sidereal')

        return parser


def _gtvt_v3pa_on_date(ra, dec, date=None, return_range=False):
    """Call JWST GTVT to retrieve nominal observatory position angle (V3 PA) for given coordinates and date

    This is a lower-level util function; most MIRAGE users should typically use default_obs_v3pa_on_date or
    all_obs_v3pa_on_date instead.

    Parameters
    ----------
    ra : str
        RA in sexagesimal format e.g. '00:00:00.0'

    dec : str
        Dec in sexagesimal format e.g. '00:00:00.0'

    date : string
        Desired date of observation, in YYYY-MM-DD format. If not supplied, the current date will be used.

    return_range : bool
        Return tuple including the range accessible via observatory roll, i.e. (v3pa, min_v3pa, max_v3pa)

    Returns
    -------
    pa_v3 : float
        V3PA in degrees
    """
    from jwst_gtvt.jwst_tvt import Ephemeris

    if date is None:
        start_date_obj = datetime.date.today()
        start_date = start_date_obj.isoformat()
        start_date = Time(start_date)
    else:
        start_date_obj = datetime.datetime.strptime(date, '%Y-%m-%d')
        start_date = start_date_obj.isoformat().split('T')[0]
        start_date = Time(start_date)

    # The gtvt seems to return NaN for the first row of the resulting
    # dataframe no matter what. So let's move the starting date back
    # one day so that we can get good values for the day of interest
    start_date = start_date - TimeDelta(1, format='jd')

    # jwst_gtvt.get_fixed_target_positions often returns NaN for the first entry in
    # the table, so make sure we have an end date that is later than the start date
    end_date_obj = start_date_obj + datetime.timedelta(days=1)
    end_date = end_date_obj.isoformat().split('T')[0]
    end_date = Time(end_date)

    ephem = Ephemeris(start_date=start_date, end_date=end_date)
    ephem_df = ephem.get_fixed_target_positions(ra, dec)

    nominal_pa = ephem.dataframe['V3PA_nominal_angle'].iloc[1]
    min_pa = ephem.dataframe['V3PA_min_pa_angle'].iloc[1]
    max_pa = ephem.dataframe['V3PA_max_pa_angle'].iloc[1]

    if return_range:
        return nominal_pa, min_pa, max_pa
    return nominal_pa


def default_obs_v3pa_on_date(pointing_filename, obs_num, date=None, verbose=False, pointing_table=None):
    """Find the nominal/default V3PA for an observation on a given date.

    Note, this relies on the JWST Generalized Target Visibility Tool (GTVT), and as such it
    just considers the basic spherical geometry of the sky. It does *NOT* take into account
    any special requirements defined in APT.

    Parameters
    ----------
    pointing_filename : string
        Pointing file filename exported by APT
    obs_num : int
        Observation number.
    date : str or None
        Desired date of observation, in YYYY-MM-DD format. If not supplied, the current date will be used.
    pointing_table : dict, optional
        Dictionary of info read from pointing filename; alternate input for this in case it's already been read from disk

    Returns
    -------
    pa : float
        V3PA in decimal degrees. Provide this to the `roll_angle` argument to mirage.yaml_generator()
    """

    if pointing_table is None:
        pointing_table = apt_inputs.AptInput().get_pointing_info(pointing_filename, 0)
    for i in range(len(pointing_table['obs_num'])):
        if pointing_table['obs_num'][i] == f"{obs_num:03d}":
            ra_deg, dec_deg = pointing_table['ra'][i], pointing_table['dec'][i]
            if verbose:
                self.logger.info(f"Pointing table row {i} is for obs {obs_num}")
                self.logger.info(f" Coords from APT pointing file: {ra_deg} {dec_deg} deg")
            break
    else:
        raise RuntimeError(f"Could not find any info for an observation number {obs_num} in the pointing table.")

    result = _gtvt_v3pa_on_date(ra_deg, dec_deg, date=date)
    if np.isnan(result):
        raise RuntimeError("Obs {obs_num} is not observable on date {date}. Target not in the field of regard.")
    return result


def all_obs_v3pa_on_date(pointing_filename, date=None, verbose=False):
    """Find the nominal/default V3PA for all observations in a program.

    Parameters
    ----------
    pointing_filename : string
        Pointing file filename exported by APT
    date : str or None
        Desired date of observation, in YYYY-MM-DD format. If not supplied, the current date will be used.


    Returns
    -------
    pas : dict
        Dict of V3PAs, in the format for passing to the `roll_angle` argument to mirage.yaml_generator()

    """
    results = {}
    pointing_table = apt_inputs.AptInput().get_pointing_info(pointing_filename, 0)
    obsnums = sorted(list(set(pointing_table['obs_num'])))
    for obs_num in obsnums:
        results[obs_num] = default_obs_v3pa_on_date(pointing_filename, int(obs_num), date=date, verbose=verbose,
                                                    pointing_table=pointing_table)
    return results


def yaml_from_params(input, yamlout=None):
    """Generate a YAML parameter file from a dict of parameters

    Parameters
    ----------
    input: dict
        The dict of parameters
    yamlout: str
        The full path of the new file
    """
    # Values to put in quotes in the YAML file
    add_quotes = ['date_obs', 'time_obs']

    # Filename
    yamlout = yamlout or os.path.join(input['Output']['directory'], 'paramfile.yaml')

    # Write all the data
    with open(yamlout, 'w') as f:
        for section, info in input.items():
            f.write("\n{}:\n".format(section))
            for k, v in info.items():
                f.write("  {}: '{}'\n".format(k, v)) if k in add_quotes else f.write("  {}: {}\n".format(k, v))

    return yamlout



if __name__ == '__main__':

    usagestring = 'USAGE: yaml_generator.py NIRCam_obs.xml NIRCam_obs.pointing'

    input = SimInput()
    parser = input.add_options(usage=usagestring)
    args = parser.parse_args(namespace=input)
    input.reffile_setup()
    input.create_inputs()
