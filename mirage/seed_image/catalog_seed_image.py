#! /usr/bin env python

'''
Reorganization of ramp simulator code. This class is used to construct
a "seed image" from a point source catalog. This seed image is a
noiseless countrate image containing only sources. No noise, no
cosmic rays.
'''

import argparse
import datetime
import sys
import glob
import logging
import os
import copy
import pickle
import re
import shutil
from yaml.scanner import ScannerError

import math
import yaml
import time
import pkg_resources
import asdf
import scipy.signal as s1
import scipy.special as sp
from scipy.ndimage import rotate
import numpy as np
from photutils.segmentation import detect_sources
from astropy.coordinates import SkyCoord
from astropy.io import fits, ascii
from astropy.table import Table, Column, vstack
from astropy.modeling.models import Shift, Sersic2D, Sersic1D, Polynomial2D, Mapping
from astropy.stats import sigma_clipped_stats
import astropy.units as u
import pysiaf

from . import moving_targets
from . import segmentation_map as segmap
import mirage
from mirage.catalogs.catalog_generator import ExtendedCatalog, TSO_GRISM_INDEX
from mirage.catalogs.utils import catalog_index_check, determine_used_cats
from mirage.reference_files.downloader import download_file
from mirage.seed_image import tso, ephemeris_tools
from ..ghosts.niriss_ghosts import determine_ghost_stamp_filename, get_ghost, source_mags_to_ghost_mags
from ..logging import logging_functions
from ..reference_files import crds_tools
from ..utils import backgrounds
from ..utils import rotations, polynomial, read_siaf_table, utils
from ..utils import set_telescope_pointing_separated as set_telescope_pointing
from ..utils import siaf_interface, file_io
from ..utils.constants import CRDS_FILE_TYPES, MEAN_GAIN_VALUES, SERSIC_FRACTIONAL_SIGNAL, \
                              SEGMENTATION_MIN_SIGNAL_RATE, SUPPORTED_SEGMENTATION_THRESHOLD_UNITS, \
                              LOG_CONFIG_FILENAME, STANDARD_LOGFILE_NAME, TSO_MODES, NIRISS_GHOST_GAP_FILE, \
                              NIRISS_GHOST_GAP_URL, NIRCAM_SW_GRISMTS_APERTURES, NIRCAM_LW_GRISMTS_APERTURES, \
                              DISPERSED_MODES
from ..utils.flux_cal import fluxcal_info, sersic_fractional_radius, sersic_total_signal
from ..utils.timer import Timer
from ..utils.utils import flatten_nested_list
from ..psf.psf_selection import get_gridded_psf_library, get_psf_wings
from mirage.utils.file_splitting import find_file_splits, SplitFileMetaData
from ..psf.segment_psfs import (get_gridded_segment_psf_library_list,
                                get_segment_offset, get_segment_library_list)


INST_LIST = ['nircam', 'niriss', 'fgs']
MODES = {'nircam': ["imaging", "ts_imaging", "wfss", "ts_grism"],
         'niriss': ["imaging", "ami", "pom", "wfss"],
         'fgs': ["imaging"]}
TRACKING_LIST = ['sidereal', 'non-sidereal']

inst_abbrev = {'nircam': 'NRC',
               'niriss': 'NIS',
               'fgs': 'FGS'}

ALLOWEDOUTPUTFORMATS = ['DMS']
WFE_OPTIONS = ['predicted', 'requirements']
WFEGROUP_OPTIONS = np.arange(5)


classdir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))
log_config_file = os.path.join(classdir, 'logging', LOG_CONFIG_FILENAME)
logging_functions.create_logger(log_config_file, STANDARD_LOGFILE_NAME)


class Catalog_seed():
    def __init__(self, offline=False):
        """Instantiate the Catalog_seed class

        Parameters
        ----------
        offline : bool
            If True, the check for the existence of the MIRAGE_DATA
            directory is skipped. This is primarily for Travis testing
        """
        # Initialize the log using dictionary from the yaml file
        self.logger = logging.getLogger(__name__)

        self.offline = offline

        # Locate the module files, so that we know where to look
        # for config subdirectory
        self.modpath = pkg_resources.resource_filename('mirage', '')

        # Get the location of the MIRAGE_DATA environment
        # variable, so we know where to look for darks, CR,
        # PSF files, etc later
        self.env_var = 'MIRAGE_DATA'
        datadir = utils.expand_environment_variable(self.env_var, offline=offline)

        # Check that CRDS-related environment variables are set correctly
        self.crds_datadir = crds_tools.env_variables()

        # self.coord_adjust contains the factor by which the
        # nominal output array size needs to be increased
        # (used for WFSS mode), as well as the coordinate
        # offset between the nominal output array coordinates,
        # and those of the expanded array. These are needed
        # mostly for WFSS observations, where the nominal output
        # array will not sit centered in the expanded output image.
        self.coord_adjust = {'x': 1., 'xoffset': 0, 'y': 1., 'yoffset': 0}

        # Set the spatial frequency, in pixel units, for moving source
        # simulations. This value controls how often a PSF or stamp
        # image is placed into the scene when building the trail of a
        # moving target. e.g. 0.3 means that the PSF is placed into the
        # scene each time the source moves by 0.3 pixels. Testing has
        # shown a value of 0.3 seems to produce realistic results without
        # taking too long to create the scene
        self.source_spatial_frequency_pix = 0.3  # Units are pixels

        # NIRCam rough noise values. Used to make educated guesses when
        # creating segmentation maps
        self.single_ron = 6.  # e-/read
        self.grism_background = 0.25  # e-/sec

        # keep track of the maximum index number of sources
        # in the various catalogs. We don't want to end up
        # with multiple sources having the same index numbers
        self.maxindex = 0

        # Number of sources on the detector
        self.n_pointsources = 0
        self.n_galaxies = 0
        self.n_extend = 0

        # Names of Mirage-created source catalogs containing ghost sources
        self.ghost_catalogs = []

        # Initialize timer
        self.timer = Timer()

    @logging_functions.log_fail
    def make_seed(self):
        """MAIN FUNCTION"""

        # Read in input parameters and quality check
        self.seed_files = []
        self.readParameterFile()

        # Get the log caught up on what's already happened
        self.logger.info('\n\nRunning catalog_seed_image..\n')
        self.logger.info('Reading parameter file: {}\n'.format(self.paramfile))
        self.logger.info('Original log file name: ./{}'.format(STANDARD_LOGFILE_NAME))

        # Quick source catalog index number check. If there are multiple
        # catalogs, raise a warning if there are overlapping source
        # indicies. It may not be a big deal for imaging mode sims,
        # but for WFSS sims where there is an associated hdf5 file,
        # it would mean trouble.
        used_cats = determine_used_cats(self.params['Inst']['mode'], self.params['simSignals'])
        overlapping_indexes, self.max_source_index = catalog_index_check(used_cats)
        if overlapping_indexes:
            error_list = [key for key in used_cats]
            raise ValueError(('At least two of the input source catalogs have overlapping index values. '
                              'Each source across all catalogs should have a unique index value.\n'
                              'Catalogs checked: {}'.format(error_list)))

        # Make filter/pupil values respect the filter/pupil wheel they are in
        self.params['Readout']['filter'], self.params['Readout']['pupil'] = \
            utils.normalize_filters(self.params['Inst']['instrument'], self.params['Readout']['filter'], self.params['Readout']['pupil'])

        # Create dictionary to use when looking in CRDS for reference files
        self.crds_dict = crds_tools.dict_from_yaml(self.params)

        # Expand param entries to full paths where appropriate
        self.params = utils.full_paths(self.params, self.modpath, self.crds_dict, offline=self.offline)
        self.filecheck()
        self.basename = os.path.join(self.params['Output']['directory'],
                                     self.params['Output']['file'][0:-5].split('/')[-1])
        self.params['Output']['file'] = self.basename + self.params['Output']['file'][-5:]
        self.subdict = utils.read_subarray_definition_file(self.params['Reffiles']['subarray_defs'])
        self.check_params()
        self.params = utils.get_subarray_info(self.params, self.subdict)
        self.coord_transform = self.read_distortion_reffile()
        self.expand_catalog_for_segments = bool(self.params['simSignals']['expand_catalog_for_segments'])
        self.add_psf_wings = self.params['simSignals']['add_psf_wings']

        # Read in the transmission file so it can be used later
        self.prepare_transmission_file()

        # Find the factor by which the transmission image is larger than full frame
        self.grism_factor()

        # If the output is a direct image to be dispersed, expand the size
        # of the nominal FOV so the disperser can account for sources just
        # outside whose traces will fall into the FOV
        if (self.params['Output']['grism_source_image']) or (self.params['Inst']['mode'] in ["pom"]):
            self.calcCoordAdjust()

        # Image dimensions
        self.nominal_dims = np.array([self.subarray_bounds[3] - self.subarray_bounds[1] + 1,
                                      self.subarray_bounds[2] - self.subarray_bounds[0] + 1])
        self.output_dims = self.transmission_image.shape
        self.logger.info('XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX')
        self.logger.info('Seed image output dimensions (x, y) are: ({}, {})'.format(self.output_dims[1], self.output_dims[0]))
        self.logger.info('XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX')

        # calculate the exposure time of a single frame, based on the size of the subarray
        self.frametime = utils.calc_frame_time(self.params['Inst']['instrument'],
                                               self.params['Readout']['array_name'],
                                               self.nominal_dims[1], self.nominal_dims[0],
                                               self.params['Readout']['namp'])
        self.logger.info("Frametime is {}".format(self.frametime))

        # Get information on the number of frames
        self.get_frame_count_info()

        # If the total number of frames/pixels is larger than the file
        # size cap can handle, then we need to break the exposure up
        # into multiple chunks
        frames_per_group = self.frames_per_integration / self.params['Readout']['ngroup']
        if self.params['Inst']['mode'] in ['ts_imaging']:
            split_seed, group_segment_indexes, integration_segment_indexes = find_file_splits(self.output_dims[1],
                                                                                              self.output_dims[0],
                                                                                              self.frames_per_integration,
                                                                                              self.params['Readout']['nint'],
                                                                                              frames_per_group=frames_per_group)

            # If the file needs to be split, check to see what the splitting
            # would be in the case of groups rather than frames. This will
            # help align the split files between the seed image and the dark
            # object later (which is split by groups).
            if split_seed:
                delta_index = integration_segment_indexes[1] - integration_segment_indexes[0]
                forced_ints_per_file = int(self.frames_per_integration / self.params['Readout']['ngroup']) * delta_index
                split_seed_g, self.group_segment_indexes_g, self.file_segment_indexes = find_file_splits(self.output_dims[1],
                                                                                                    self.output_dims[0],
                                                                                                    self.params['Readout']['ngroup'],
                                                                                                    self.params['Readout']['nint'],
                                                                                                    force_delta_int=forced_ints_per_file)

                # In order to avoid the case of having a single integration
                # in the final file, which leads to rate rather than rateints
                # files in the pipeline, check to be sure that the integration
                # splitting indexes indicate the last split isn't a single
                # integration
                if len(self.file_segment_indexes) > 2:
                    delta_int = self.file_segment_indexes[1:] - self.file_segment_indexes[0: -1]
                    if delta_int[-1] == 1 and delta_int[0] != 1:
                        self.file_segment_indexes[-2] -= 1
                        self.logger.debug('Adjusted final seed to avoid single integration: {}'.format(self.file_segment_indexes))

                # More adjustments related to segment numbers. We need to compare
                # the integration indexes for the seed images vs those for the final
                # data and make sure that there are no segments in the final data that
                # have no corresponding integrations from the seed images
                # Example: integration_segment_indexes = [0, 7, 8], and
                # self.file_segment_indexes = [0, 6, 8] - due to applying the adjustment
                # above. In this case as you step through integration_segment_indexes,
                # you see that (skipping 0), 7 and 8 both fall in the 6-8 bin in
                # self.file_segment_indexes. Nothing falls into the 0-7 bin, which
                # corresponds to segment 1. In this case, we need to adjust
                # integration_segment_indexes to make sure that all segments have data
                # associated with them.
                segnum_check = []
                for intnum in integration_segment_indexes[1:]:
                    segnum_check.append(np.where(intnum <= self.file_segment_indexes)[0][0])
                maxseg = max(segnum_check)
                for i in range(1, maxseg + 1):
                    if i not in segnum_check:
                        integration_segment_indexes = copy.deepcopy(self.file_segment_indexes)

            else:
                self.file_segment_indexes = integration_segment_indexes

        else:
            split_seed = False
            group_segment_indexes = [0, self.params['Readout']['ngroup']]
            integration_segment_indexes = [0, self.params['Readout']['nint']]
            self.file_segment_indexes = copy.deepcopy(integration_segment_indexes)
            self.total_seed_segments = 1
            self.segment_number = 1
            self.segment_part_number = 1

        self.total_seed_segments = len(self.file_segment_indexes) - 1
        self.total_seed_segments_and_parts = (len(integration_segment_indexes) - 1) * (len(group_segment_indexes) - 1)

        # Read in the PSF library file corresponding to the detector and filter
        # For WFSS simulations, use the PSF libraries with the appropriate CLEAR element
        self.prepare_psf_entries()

        if not self.expand_catalog_for_segments:
            self.psf_library = get_gridded_psf_library(
                self.params['Inst']['instrument'], self.detector, self.psf_filter, self.psf_pupil,
                self.params['simSignals']['psfwfe'],
                self.params['simSignals']['psfwfegroup'],
                self.params['simSignals']['psfpath'])
            self.psf_library_core_y_dim, self.psf_library_core_x_dim = self.psf_library.data.shape[-2:]

            # Versions of photutils prior to 1.10.0 use an integer for the oversampling factor, while
            # newer versions use a numpy array with one value for each of the x and y dimensions.
            # Make sure we can handle both of these options. For the moment, Mirage assumes that the
            # oversampling factor is the same in both dimensions.
            if isinstance(self.psf_library.oversampling, int):
                self.psf_library_oversamp = self.psf_library.oversampling
            elif isinstance(self.psf_library.oversampling, list) or isinstance(self.psf_library.oversampling, np.ndarray):
                self.psf_library_oversamp = self.psf_library.oversampling[0]

        # If reading in segment PSFs, use get_gridded_segment_psf_library_list
        # to get a list of photutils.griddedPSFModel objects
        else:
            self.psf_library = get_gridded_segment_psf_library_list(
                self.params['Inst']['instrument'], self.detector, self.psf_filter,
                self.params['simSignals']['psfpath'], pupilname=self.psf_pupil)
            self.psf_library_core_y_dim, self.psf_library_core_x_dim = self.psf_library[0].data.shape[-2:]
            self.psf_library_oversamp = 1

        # Set the psf core dimensions to actually be 2 rows and columns
        # less than the dimensions in the library file. This is because
        # we will later evaluate the library using these core dimensions.
        # If we were to evaluate a library that is 51x51 pixels using a
        # 51x51 pixel grid, then if the source is centered close to the
        # edge of the central pixel, you can end up with an zeroed out
        # edge row or column in the evaluated array. So we do this to be
        # sure that we are evaluating the library with a slightly smaller
        # array than the array in the library.
        self.psf_library_core_x_dim = int(self.psf_library_core_x_dim / self.psf_library_oversamp) - \
            self.params['simSignals']['gridded_psf_library_row_padding']
        self.psf_library_core_y_dim = int(self.psf_library_core_y_dim / self.psf_library_oversamp) - \
            self.params['simSignals']['gridded_psf_library_row_padding']

        if self.add_psf_wings is True:
            self.psf_wings = get_psf_wings(self.params['Inst']['instrument'], self.detector,
                                           self.psf_filter, self.psf_pupil,
                                           self.params['simSignals']['psfwfe'],
                                           self.params['simSignals']['psfwfegroup'],
                                           os.path.join(self.params['simSignals']['psfpath'], 'psf_wings'))

            # Read in the file that defines PSF array sizes based on magnitude
            self.psf_wing_sizes = ascii.read(self.params['simSignals']['psf_wing_threshold_file'])
            max_wing_size = self.psf_wings.shape[0]
            too_large = np.where(np.array(self.psf_wing_sizes['number_of_pixels']) > max_wing_size)[0]
            if len(too_large) > 0:
                self.logger.info(('Some PSF sizes in {} are larger than the PSF library file dimensions. '
                                  'Resetting these values in the table to be equal to the PSF dimensions'
                                  .format(os.path.basename(self.params['simSignals']['psf_wing_threshold_file']))))
                self.psf_wing_sizes['number_of_pixels'][too_large] = max_wing_size

        # Prepare for saving seed images. Set default values for the
        # keywords that must appear in the headers of any seed image
        # file

        # Attributes related to file splitting, which is done in cases
        # where the data volume is too large. Currently this is only
        # implemented for imaging TSO. The only other mode where it
        # will need to be implemented is moving tagets
        self.segment_number = 1
        self.segment_part_number = 1
        self.segment_frames = self.frames_per_integration  # self.seedimage.shape[1]
        self.segment_ints = self.params['Readout']['nint']  # self.seedimage.shape[0]
        self.segment_frame_start_number = 0
        self.segment_int_start_number = 0
        self.part_int_start_number = 0
        self.part_frame_start_number = 0

        # For imaging mode, generate the countrate image using the catalogs
        if self.params['Telescope']['tracking'].lower() != 'non-sidereal':
            self.logger.info('Creating signal rate image of sidereal synthetic inputs.')
            self.seedimage, self.seed_segmap = self.create_sidereal_image()
            outapp = ''

        # If we are tracking a non-sidereal target, then
        # everything in the catalogs needs to be streaked across
        # the detector
        if self.params['Telescope']['tracking'].lower() == 'non-sidereal':
            self.logger.info('Creating signal ramp of non-sidereal synthetic inputs')
            self.seedimage, self.seed_segmap = self.non_sidereal_seed()
            outapp = '_nonsidereal_target'

        # If non-sidereal targets are requested (KBOs, asteroids, etc,
        # create a RAPID integration which includes those targets
        mov_targs_ramps = []
        if (self.runStep['movingTargets'] | self.runStep['movingTargetsSersic']
                | self.runStep['movingTargetsExtended']):
            self.logger.info(("Creating signal ramp of sources that are moving with "
                              "respect to telescope tracking."))
            trailed_ramp, trailed_segmap = self.make_trailed_ramp()
            outapp += '_trailed_sources'

            # Now we need to expand frameimage into a ramp
            # so we can add the trailed objects
            self.logger.info('Combining trailed object ramp with that containing tracked targets')
            if self.params['Telescope']['tracking'].lower() != 'non-sidereal':
                self.seedimage = self.combineSimulatedDataSources('countrate', self.seedimage, trailed_ramp)
            else:
                self.seedimage = self.combineSimulatedDataSources('ramp', self.seedimage, trailed_ramp)
            self.seed_segmap += trailed_segmap

        # MASK IMAGE
        # Create a mask so that we don't add signal to masked pixels
        # Initially this includes only the reference pixels
        # Keep the mask image equal to the true subarray size, since this
        # won't be used to make a requested grism source image
        if self.params['Inst']['mode'] not in ['wfss', 'ts_grism']:
            self.maskimage = np.zeros((self.ffsize, self.ffsize), dtype=int)
            self.maskimage[4:self.ffsize-4, 4:self.ffsize-4] = 1.

            # crop the mask to match the requested output array
            ap_suffix = self.params['Readout']['array_name'].split('_')[1]
            if ap_suffix not in ['FULL', 'CEN']:
                self.maskimage = self.maskimage[self.subarray_bounds[1]:self.subarray_bounds[3] + 1,
                                                self.subarray_bounds[0]:self.subarray_bounds[2] + 1]

        # TSO imaging mode here
        if self.params['Inst']['mode'] in ['ts_imaging']:
            self.create_tso_seed(split_seed=split_seed, integration_splits=integration_segment_indexes,
                                 frame_splits=group_segment_indexes)
        else:

            # For seed images to be dispersed in WFSS mode,
            # embed the seed image in a full frame array. The disperser
            # tool does not work on subarrays
            aperture_suffix = self.params['Readout']['array_name'].split('_')[-1]
            #if ((self.params['Inst']['mode'] in ['wfss', 'ts_grism']) & \
            #   (aperture_suffix not in ['FULL', 'CEN'])):
            #    self.seedimage, self.seed_segmap = self.pad_wfss_subarray(self.seedimage, self.seed_segmap)

            # For NIRISS POM data, extract the central 2048x2048
            if self.params['Inst']['mode'] in ["pom"]:
                # Expose the full-sized pom seed image
                self.logger.info('Multiplying seed by POM transmission image.')
                self.seedimage *= self.transmission_image
                self.pom_seed = copy.deepcopy(self.seedimage)
                self.pom_segmap = copy.deepcopy(self.seed_segmap)

                # Save the full-sized pom seed image to a file
                if 'clear' in self.params['Readout']['filter'].lower():
                    usefilt = self.params['Readout']['pupil']
                else:
                    usefilt = self.params['Readout']['filter']
                self.pom_file = os.path.join(self.basename + '_' + usefilt + '_pom_seed_image.fits')
                self.saveSeedImage(self.pom_seed, self.pom_segmap, self.pom_file)
                self.logger.info('Full POM seed image saved as: {}'.format(self.pom_file))
                self.seedimage, self.seed_segmap = self.extract_full_from_pom(self.seedimage, self.seed_segmap)

            if self.params['Inst']['mode'] not in ['wfss', 'ts_grism']:
                # Multiply the mask by the seed image and segmentation map in
                # order to reflect the fact that reference pixels have no signal
                # from external sources. Seed images to be dispersed do not have
                # this step applied.
                self.seedimage *= self.maskimage
                self.seed_segmap *= self.maskimage

            # For data that will not be dispersed, multiply by the transmission image
            # pom data have already been multiplied by the transmission image above
            if self.params['Inst']['mode'] not in ['wfss', 'ts_grism', 'pom']:
                self.logger.info('Multiplying seed by POM transmission image.')
                self.seedimage *= self.transmission_image

            # Save the combined static + moving targets ramp
            if self.params['Inst']['instrument'].lower() != 'fgs':
                self.seed_file = '{}_{}_{}_final_seed_image.fits'.format(self.basename, self.params['Readout']['filter'],
                                                                         self.params['Readout']['pupil'])
            else:
                self.seed_file = '{}_final_seed_image.fits'.format(self.basename)

            # Define self.seed_files for non-TSO observations
            if len(self.seed_files) == 0:
                self.seed_files = [self.seed_file]

            self.saveSeedImage(self.seedimage, self.seed_segmap, self.seed_file)
            self.logger.info("Final seed image and segmentation map saved as {}".format(self.seed_file))
            self.logger.info("Seed image, segmentation map, and metadata available as:")
            self.logger.info("self.seedimage, self.seed_segmap, self.seedinfo.\n\n")

        # Return info in a tuple
        # return (self.seedimage, self.seed_segmap, self.seedinfo)

        # In the case where the user is running only catalog_seed_image,
        # we want to make a copy of the log file, rename
        # it to something unique and move it to a logical location.
        #
        # If the user has called (e.g. imaging_simulator), in which case
        # dark_prep will be run next, dark_prep will re-initialize the
        # logger with the same handler and file destination, so it will
        # append to the original log file created here.
        logging_functions.move_logfile_to_standard_location(self.paramfile, STANDARD_LOGFILE_NAME,
                                                            yaml_outdir=self.params['Output']['directory'],
                                                            log_type='catalog_seed')

    def create_tso_seed(self, split_seed=False, integration_splits=-1, frame_splits=-1):
        """Create a seed image for time series sources. Just like for moving
        targets, the seed image will be 4-dimensional

        Parameters
        ----------
        split_seed : bool
            Whether or not the seed image will be split between multiple files

        integration_splits : list
            List containing the first integration number within each of the
            split files. In addition, the final number in the list should be
            the index of the final integration.

        frame_splits : list
            Similar to ``integration_splits`` but for frame numbers. This will
            be used in the case where the readout pattern contains many frames
            per group

        Returns
        -------
        seed : numpy.ndarray
            4D array seed image.

        segmap : numpy.ndarray
            2D segmentation map
        """
        # Read in the TSO catalog. This is only for TSO mode, not
        # grism TSO, which will need a different catalog format.
        if self.params['Inst']['mode'].lower() == 'ts_imaging':
            tso_cat, _ = self.get_point_source_list(self.params['simSignals']['tso_imaging_catalog'], source_type='ts_imaging')

            # Create lists of seed images and segmentation maps for all
            # TSO objects
            tso_seeds = []
            tso_segs = []
            tso_lightcurves = []
            for source in tso_cat:
                # Let's set the TSO source to be a unique index number.
                # Otherwise the index number will be modified to be one
                # greater than the max value after working on the background
                # sources
                source['index'] = TSO_GRISM_INDEX

                # Place row in an empty table
                t = tso_cat[:0].copy()
                t.add_row(source)

                # Create the seed image contribution from each source
                ptsrc_seed, ptsrc_seg = self.make_point_source_image(t)

                # Add to the list of sources (even though the list will
                # always have only one item)
                tso_seeds.append(copy.deepcopy(ptsrc_seed))
                tso_segs.append(copy.deepcopy(ptsrc_seg))

                # Under the assumption that there will always be only one
                # TSO source, let's assume that the dataset number in the
                # lightcurve file is always 1. This seems easiest for the
                # users when creating the file.
                lightcurve = tso.read_lightcurve(source['lightcurve_file'], 1)
                tso_lightcurves.append(lightcurve)

            if not split_seed:
                # Add the TSO sources to the seed image and segmentation map
                seed, segmap = tso.add_tso_sources(self.seedimage, self.seed_segmap, tso_seeds, tso_segs,
                                                   tso_lightcurves, self.frametime, self.total_frames,
                                                   self.total_frames, self.frames_per_integration,
                                                   self.params['Readout']['nint'],
                                                   self.params['Readout']['resets_bet_ints'],
                                                   samples_per_frametime=5)

                self.segment_number = 1
                self.segment_part_number = 1
                self.segment_frames = seed.shape[1]
                self.segment_ints = seed.shape[0]
                self.segment_frame_start_number = 0
                self.segment_int_start_number = 0
                self.part_int_start_number = 0
                self.part_frame_start_number = 0

                # Zero out the reference pixels
                seed *= self.maskimage
                segmap *= self.maskimage

                # Multiply by the transmission image
                self.logger.info('Multiplying seed by POM transmission image.')
                seed *= self.transmission_image

                # Save the seed image segment to a file
                if self.total_seed_segments_and_parts == 1:
                    self.seed_file = '{}_{}_{}_seed_image.fits'.format(self.basename, self.params['Readout']['filter'],
                                                                       self.params['Readout']['pupil'])
                else:
                    raise ValueError("ERROR: TSO seed file should not be split at this point.")
                    seg_string = str(self.segment_number).zfill(3)
                    part_string = str(self.segment_part_number).zfill(3)
                    self.seed_file = '{}_{}_{}_seg{}_part{}_seed_image.fits'.format(self.basename, self.params['Readout']['filter'],
                                                                                    self.params['Readout']['pupil'], seg_string, part_string)

                self.seed_files.append(self.seed_file)
                self.saveSeedImage(seed, segmap, self.seed_file)

            else:
                split_meta = SplitFileMetaData(integration_splits, frame_splits,
                                               self.file_segment_indexes, self.group_segment_indexes_g,
                                               self.frames_per_integration, self.frames_per_group, self.frametime)

                counter = 0
                i = 1
                for int_start in integration_splits[:-1]:
                    int_end = integration_splits[i]

                    j = 1
                    previous_frame = np.zeros(self.seedimage.shape)
                    for initial_frame in frame_splits[:-1]:
                        total_frames = split_meta.total_frames[counter]
                        total_ints = split_meta.total_ints[counter]
                        time_start = split_meta.time_start[counter]
                        frame_start = split_meta.frame_start[counter]
                        seed, segmap = tso.add_tso_sources(self.seedimage, self.seed_segmap,
                                                                           tso_seeds, tso_segs,
                                                                           tso_lightcurves, self.frametime,
                                                                           total_frames, self.total_frames,
                                                                           self.frames_per_integration,
                                                                           total_ints,
                                                                           self.params['Readout']['resets_bet_ints'],
                                                                           starting_time=time_start,
                                                                           starting_frame=frame_start,
                                                                           samples_per_frametime=5)
                        seed += previous_frame
                        previous_frame = seed[-1, -1, :, :]

                        # Zero out the reference pixels
                        seed *= self.maskimage
                        segmap *= self.maskimage

                        # Multiply by the transmission image
                        self.logger.info('Multiplying seed by POM transmission image.')
                        seed *= self.transmission_image

                        # Get metadata values to save in seed image header
                        self.segment_number = split_meta.segment_number[counter]
                        self.segment_ints = split_meta.segment_ints[counter]
                        self.segment_frames = split_meta.segment_frames[counter]
                        self.segment_part_number = split_meta.segment_part_number[counter]
                        self.segment_frame_start_number = split_meta.segment_frame_start_number[counter]
                        self.segment_int_start_number = split_meta.segment_int_start_number[counter]
                        self.part_int_start_number = split_meta.part_int_start_number[counter]
                        self.part_frame_start_number = split_meta.part_frame_start_number[counter]
                        counter += 1

                        # Save the seed image segment to a file
                        self.logger.debug('\n\n\nTotal_seed_segments_and_parts: {}'.format(self.total_seed_segments_and_parts))
                        seg_string = str(self.segment_number).zfill(3)
                        part_string = str(self.segment_part_number).zfill(3)
                        self.seed_file = '{}_{}_{}_seg{}_part{}_seed_image.fits'.format(self.basename, self.params['Readout']['filter'],
                                                                                        self.params['Readout']['pupil'], seg_string, part_string)

                        self.saveSeedImage(seed, segmap, self.seed_file)
                        self.seed_files.append(self.seed_file)
                        self.logger.info('Adding file {} to self.seed_files\n'.format(self.seed_file))
                        j += 1

                    i += 1

            self.seedimage = seed
            self.seed_segmap = segmap
        else:
            raise ValueError('Grism TSO simulations should be done with grism_tso_simulator.py!')

    def get_frame_count_info(self):
        """Calculate information on the number of frames per group and
        per integration
        """
        numints = self.params['Readout']['nint']
        numgroups = self.params['Readout']['ngroup']
        numframes = self.params['Readout']['nframe']
        numskips = self.params['Readout']['nskip']
        numresets = self.params['Readout']['resets_bet_ints']

        self.frames_per_group = numframes + numskips
        self.frames_per_integration = numgroups * self.frames_per_group
        self.total_frames = numgroups * self.frames_per_group

        if numints > 1:
            # Frames for all integrations
            self.total_frames *= numints
            # Add the resets for all but the first integration
            self.total_frames += (numresets * (numints - 1))

    def extract_full_from_pom(self, seedimage, seed_segmap):
        """ Given the seed image and segmentation images for the NIRISS POM field of view,
        extract the central 2048x2048 pixel area where the detector sits.  The routine is only
        called when the mode is "pom".  The mode is set to "imaging" in these routine, as after
        the routine is called the subsequent results are the same as when the mode is set to
        "imaging" in the parameter file.

        Parameters:
        -----------

        seedimage : numpy.ndarray (float)   dimension 2322x2322 pixels
        seed_segmap : numpy.ndarray (int)    dimension 2322x2322 pixels

        Returns:
        ---------

        newseedimage : numpy.ndarray (float)  dimension 2048x2048
        newseed_segmap : numpy.ndarray (int)   dimension 2048x2048

        """
        # For the NIRISS POM mode, extact the central 2048x2048 pixels for the
        # ramp simulation.  Set the mode back to "imaging".
        newseedimage = np.copy(seedimage[self.coord_adjust['yoffset']:self.coord_adjust['yoffset']+2048,
                                         self.coord_adjust['xoffset']:self.coord_adjust['xoffset']+2048])
        newseed_segmap = np.copy(seed_segmap[self.coord_adjust['yoffset']:self.coord_adjust['yoffset']+2048,
                                             self.coord_adjust['xoffset']:self.coord_adjust['xoffset']+2048])
        return newseedimage, newseed_segmap

    def add_detector_to_zeropoints(self, detector):
        """Manually add detector dependence to the zeropoint table for
        NIRCam and NIRISS simualtions. This is being done as a placeholder
        for the future, where we expect zeropoints to be detector-dependent.

        Parameters:
        -----------
        detector : str
            Name of detector to add to the table

        Returns:
        --------
        base_table : astropy.table.table
            Modified table of zeropoint info
        """
        # Add "Detector" to the list of column names
        base_table = copy.deepcopy(self.zps)
        num_entries = len(self.zps)
        det_column = Column(np.repeat(detector, num_entries), name="Detector")
        base_table.add_column(det_column, index=0)
        return base_table

    def basic_get_image(self, filename):
        """
        Read in image from a fits file

         Parameters:
        -----------
        filename : str
            Name of fits file to be read in

         Returns:
        --------
        data : obj
            numpy array of data within file

        header : obj
            Header from 0th extension of data file
        """
        data, header = fits.getdata(filename, header=True)
        if len(data.shape) != 2:
            data, header = fits.getdata(filename, 1)
        return data, header

    def grism_factor(self):
        """Find the factor by which grism images are oversized compared
        to full frame images
        """
        gy, gx = self.transmission_image.shape
        self.grism_direct_factor_x = gx / 2048.
        self.grism_direct_factor_y = gy / 2048.

    def prepare_psf_entries(self):
        """Get the correct filter and pupil values to use when searching
        for gridded psf libraries
        """
        self.psf_pupil = self.params['Readout']['pupil']
        self.psf_filter = self.params['Readout']['filter']
        if self.params['Readout']['pupil'].lower() in ['grismr', 'grismc']:
            self.psf_pupil = 'CLEAR'
        if self.params['Readout']['filter'].lower() in ['gr150r', 'gr150c']:
            self.psf_filter = 'CLEAR'

    def prepare_transmission_file(self):
        """Read in the transmission file and get into the correct shape in
        order to apply it to the seed image
        """
        filename = self.params['Reffiles']['transmission']

        if isinstance(filename, str):
            if filename.lower() == 'none':
                filename = None

        if filename is not None:
            # Read in file
            try:
                transmission, header = fits.getdata(filename, header=True)
            except:
                raise IOError('WARNING: unable to read in {}'.format(filename))
        else:
            self.logger.info(('No transmission file given. Assuming full transmission for all pixels, '
                              'not including flat field effects. (no e.g. occulters blocking any pixels).'))
            if self.params['Inst']['mode'].lower() in ['imaging', 'ami', 'ts_imaging']:
                transmission = np.ones((2048, 2048))
                header = {'NOMXSTRT': 0, 'NOMYSTRT': 0}
            elif self.params['Inst']['mode'].lower() == 'pom':
                # For pom mode, we treat the situation similar to imaging
                # mode, but use a larger array of 1s, so that we can create
                # a larger seed image
                transmission = np.ones((2322, 2322))
                header = {'NOMXSTRT': 137, 'NOMYSTRT': 137}
            else:
                raise ValueError(('POM transmission file is None, but the observing mode is not '
                                  '"imaging" or "pom". Not sure what to do for POM transmission.'))

        yd, xd = transmission.shape

        # Get the coordinates in the transmission file that correspond to (0,0)
        # on the detector (full frame aperture)
        self.trans_ff_xmin = int(header['NOMXSTRT'])
        self.trans_ff_ymin = int(header['NOMYSTRT'])

        # Check that the transmission image contains the entire detector
        if ((xd < 2048) or (yd < 2048)):
            raise ValueError("Transmission image is not large enough to contain the entire detector.")
        if (((self.trans_ff_xmin + 2048) > xd) or ((self.trans_ff_ymin + 2048) > yd)):
            raise ValueError(("NOMXSTRT, NOMYSTRT in transmission image indicate the entire detector is "
                              "not contained in the image."))

        if (self.params['Output']['grism_source_image']) or (self.params['Inst']['mode'] in ["pom", "wfss", "ts_grism"]):
            pass
        else:
            # Imaging modes here. Cut the transmission file down to full frame,
            # and then down to the requested aperture
            transmission = transmission[self.trans_ff_ymin: self.trans_ff_ymin+2048, self.trans_ff_xmin: self.trans_ff_xmin+2048]

            # Crop to expected subarray
            try:
                transmission = transmission[self.subarray_bounds[1]:self.subarray_bounds[3]+1,
                                            self.subarray_bounds[0]:self.subarray_bounds[2]+1]
            except:
                raise ValueError("Unable to crop transmission image to expected subarray.")

        self.transmission_image = transmission

    def pad_wfss_subarray(self, seed, seg):
        """
        WFSS seed images that are to go into the disperser
        must be full frame (or larger). The disperser cannot
        work on subarray images. So embed the subarray seed image
        in a full-frame sized array.

        Parameters:
        -----------
        None

        Returns:
        --------
        Padded seed image and segmentation map
        """
        seeddim = seed.shape
        nx = int(2048 * self.grism_direct_factor_x)
        ny = int(2048 * self.grism_direct_factor_y)
        ffextrax = int((nx - 2048) / 2)
        ffextray = int((ny - 2048) / 2)
        bounds = np.array(self.subarray_bounds)

        subextrax = int((seeddim[-1] - bounds[2]) / 2)
        subextray = int((seeddim[-2] - bounds[3]) / 2)

        extradiffy = ffextray - subextray
        extradiffx = ffextrax - subextrax
        exbounds = [extradiffx, extradiffy, extradiffx+seeddim[-1]-1, extradiffy+seeddim[-2]-1]

        if len(seeddim) == 2:
            padded_seed = np.zeros((ny, nx))
            padded_seed[exbounds[1]:exbounds[3] + 1, exbounds[0]:exbounds[2] + 1] = seed
            padded_seg = np.zeros((ny, nx), dtype=int)
            padded_seg[exbounds[1]:exbounds[3] + 1, exbounds[0]:exbounds[2] + 1] = seg
        elif len(seeddim) == 4:
            padded_seed = np.zeros((seeddim[0], seeddim[1], ny, nx))
            padded_seed[:, :, exbounds[1]:exbounds[3] + 1, exbounds[0]:exbounds[2] + 1] = seed
            padded_seg = np.zeros((seeddim[0], seeddim[1], ny, nx), dtype=int)
            padded_seg[:, :, exbounds[1]:exbounds[3] + 1, exbounds[0]:exbounds[2] + 1] = seg
        else:
            raise ValueError("Seed image is not 2D or 4D. It should be.")
        return padded_seed, padded_seg

    def saveSeedImage(self, seed_image, segmentation_map, seed_file_name):
        """Save the seed image and accompanying segmentation map to a
        fits file.

        Parameters
        ----------
        seed_image : numpy.ndarray
            Array containing the seed image

        segmentation_map : numpy.ndimage
            Array containing the segmentation map

        seed_file_name : str
            Name of FITS file to save ``seed_image`` and `segmentation_map``
            into
        """
        arrayshape = seed_image.shape
        if len(arrayshape) == 2:
            units = 'ADU/sec'
            yd, xd = arrayshape
            grps = 0
            integ = 0
            tgroup = 0.
            arraygroup = 0
            arrayint = 0
            self.logger.info('Seed image is 2D.')
        elif len(arrayshape) == 3:
            units = 'ADU'
            grps, yd, xd = arrayshape
            integ = 0
            tgroup = self.frametime * (self.params['Readout']['nframe'] + self.params['Readout']['nskip'])
            self.logger.info('Seed image is 3D.')
        elif len(arrayshape) == 4:
            units = 'ADU'
            integ, grps, yd, xd = arrayshape
            tgroup = self.frametime * (self.params['Readout']['nframe'] + self.params['Readout']['nskip'])
            self.logger.info('Seed image is 4D.')

        xcent_fov = xd / 2
        ycent_fov = yd / 2

        kw = {}
        kw['XCENTER'] = xcent_fov
        kw['YCENTER'] = ycent_fov
        kw['UNITS'] = units
        kw['TGROUP'] = tgroup

        # Set FGS filter to "N/A" in the output file
        # as this is the value DMS looks for.
        if self.params['Readout']['filter'] == "NA":
            self.params['Readout']['filter'] = "N/A"
        if self.params['Readout']['pupil'] == "NA":
            self.params['Readout']['pupil'] = "N/A"
        kw['FILTER'] = self.params['Readout']['filter']
        kw['PUPIL'] = self.params['Readout']['pupil']
        kw['PHOTFLAM'] = self.photflam
        kw['PHOTFNU'] = self.photfnu
        kw['PHOTPLAM'] = self.pivot * 1.e4  # put into angstroms
        kw['NOMXDIM'] = self.nominal_dims[1]
        kw['NOMYDIM'] = self.nominal_dims[0]
        kw['NOMXSTRT'] = int(self.coord_adjust['xoffset'] + 1)
        kw['NOMXEND'] = int(self.nominal_dims[1] + self.coord_adjust['xoffset'])
        kw['NOMYSTRT'] = int(self.coord_adjust['yoffset'] + 1)
        kw['NOMYEND'] = int(self.nominal_dims[0] + self.coord_adjust['yoffset'])

        # Files/inputs used during seed image production
        kw['YAMLFILE'] = self.paramfile
        kw['GAINFILE'] = self.params['Reffiles']['gain']
        kw['DISTORTN'] = self.params['Reffiles']['astrometric']
        kw['IPC'] = self.params['Reffiles']['ipc']
        kw['CROSSTLK'] = self.params['Reffiles']['crosstalk']
        kw['FLUX_CAL'] = self.params['Reffiles']['flux_cal']
        kw['FTHRUPUT'] = self.params['Reffiles']['filter_throughput']
        kw['PTSRCCAT'] = self.params['simSignals']['pointsource']
        kw['GALAXCAT'] = self.params['simSignals']['galaxyListFile']
        kw['EXTNDCAT'] = self.params['simSignals']['extended']
        kw['MTPTSCAT'] = self.params['simSignals']['movingTargetList']
        kw['MTSERSIC'] = self.params['simSignals']['movingTargetSersic']
        kw['MTEXTEND'] = self.params['simSignals']['movingTargetExtended']
        kw['NONSDRAL'] = self.params['simSignals']['movingTargetToTrack']
        kw['BKGDRATE'] = self.params['simSignals']['bkgdrate']
        kw['TRACKING'] = self.params['Telescope']['tracking']
        kw['POISSON'] = self.params['simSignals']['poissonseed']
        kw['PSFWFE'] = self.params['simSignals']['psfwfe']
        kw['PSFWFGRP'] = self.params['simSignals']['psfwfegroup']
        kw['MRGEVRSN'] = mirage.__version__

        # Observations with high data volumes (e.g. moving targets, TSO)
        # can be split into multiple "segments" in order to cap the
        # maximum file size
        kw['EXSEGTOT'] = self.total_seed_segments  # Total number of segments in exp
        kw['EXSEGNUM'] = self.segment_number       # Segment number of this file
        kw['PART_NUM'] = self.segment_part_number  # Segment part number of this file

        # Total number of integrations and groups in the current segment
        # (combining all parts of the segment)
        kw['SEGINT'] = self.segment_ints
        kw['SEGGROUP'] = self.segment_frames

        # Total number of integrations and groups in the exposure
        kw['EXPINT'] = self.params['Readout']['nint']
        kw['EXPGRP'] = self.params['Readout']['ngroup']

        # Indexes of the ints and groups where the data in this file go
        # Frame and integration indexes of the segment within the exposure
        kw['SEGFRMST'] = self.segment_frame_start_number
        kw['SEGFRMED'] = self.segment_frame_start_number + grps - 1
        kw['SEGINTST'] = self.segment_int_start_number
        kw['SEGINTED'] = self.segment_int_start_number + self.segment_ints - 1

        # Frame and integration indexes of the part within the segment
        kw['PTINTSRT'] = self.part_int_start_number
        kw['PTFRMSRT'] = self.part_frame_start_number

        # Seed images provided to disperser are always embedded in an array
        # that is larger than full frame. These keywords describe where the
        # detector is within this oversized array
        if self.params['Inst']['mode'] in ['wfss', 'ts_grism', 'pom']:
            kw['NOMXDIM'] = self.ffsize
            kw['NOMYDIM'] = self.ffsize
            kw['NOMXSTRT'] = self.trans_ff_xmin
            kw['NOMXEND'] = kw['NOMXSTRT'] + self.ffsize - 1
            kw['NOMYSTRT'] = self.trans_ff_ymin
            kw['NOMYEND'] = kw['NOMYSTRT'] + self.ffsize - 1

        # TSO-specific header keywords
        if self.params['Inst']['mode'] in ['ts_imaging', 'ts_grism']:
            kw['TSOVISIT'] = True
        else:
            kw['TSOVISIT'] = False

        kw['XREF_SCI'] = self.siaf.XSciRef
        kw['YREF_SCI'] = self.siaf.YSciRef

        kw['GRISMPDX'] = self.grism_direct_factor_x
        kw['GRISMPDY'] = self.grism_direct_factor_y
        self.seedinfo = kw
        self.saveSingleFits(seed_image, seed_file_name, key_dict=kw, image2=segmentation_map, image2type='SEGMAP')

    def combineSimulatedDataSources(self, inputtype, input1, mov_tar_ramp):
        """Combine the exposure containing the trailed sources with the
        countrate image containing the static sources
        inputtype can be 'countrate' in which case input needs to be made
        into a ramp before combining with mov_tar_ramp, or 'ramp' in which
        case you can combine directly. Use 'ramp' with
        non-sidereal TRACKING data, and 'countrate' with sidereal TRACKING data
        """
        if inputtype == 'countrate':
            # First change the countrate image into a ramp
            yd, xd = input1.shape
            numints = self.params['Readout']['nint']
            num_frames = self.params['Readout']['ngroup'] * \
                (self.params['Readout']['nframe'] + self.params['Readout']['nskip'])
            self.logger.info("Countrate image of synthetic signals being converted to "
                             "RAPID/NISRAPID integration with {} frames.".format(num_frames))
            input1_ramp = np.zeros((numints, num_frames, yd, xd))
            for i in range(num_frames):
                input1_ramp[0, i, :, :] = input1 * self.frametime * (i + 1)
            if numints > 1:
                for integ in range(1, numints):
                    input1_ramp[integ, :, :, :] = input1_ramp[0, :, :, :]

        else:
            # If input1 is a ramp rather than a countrate image
            input1_ramp = input1

        # Combine the input1 ramp and the moving target ramp, which are
        # now both RAPID mode
        totalinput = input1_ramp + mov_tar_ramp
        return totalinput

    def make_trailed_ramp(self):
        # Create a ramp for objects that are trailing through
        # the field of view during the integration
        mov_targs_ramps = []
        mov_targs_segmap = None

        # Only attempt to add ghosts to NIRISS observations where the
        # user has requested it.
        add_ghosts = False
        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            add_ghosts = True

        if self.params['Telescope']['tracking'].lower() != 'non-sidereal':
            tracking = False
            ra_vel = None
            dec_vel = None
            vel_flag = False
            ra_interp_fncn = None
            dec_interp_fncn = None
        else:
            tracking = True
            ra_vel = self.ra_vel
            dec_vel = self.dec_vel
            vel_flag = self.nonsidereal_pix_vel_flag
            ra_interp_fncn = self.nonsidereal_ra_interp
            dec_interp_fncn = self.nonsidereal_dec_interp

        if self.runStep['movingTargets']:
            self.logger.info("Adding moving point sources to seed image.")

            mov_targs_ptsrc, mt_ptsrc_segmap = self.movingTargetInputs(self.params['simSignals']['movingTargetList'],
                                                                       'point_source',
                                                                       MT_tracking=tracking,
                                                                       tracking_ra_vel=ra_vel,
                                                                       tracking_dec_vel=dec_vel,
                                                                       trackingPixVelFlag=vel_flag,
                                                                       non_sidereal_ra_interp_function=ra_interp_fncn,
                                                                       non_sidereal_dec_interp_function=dec_interp_fncn,
                                                                       add_ghosts=add_ghosts
                                                                       )

            mov_targs_ramps.append(mov_targs_ptsrc)
            mov_targs_segmap = np.copy(mt_ptsrc_segmap)

        # moving target using a sersic object
        if self.runStep['movingTargetsSersic']:
            self.logger.info("Adding moving galaxies to seed image.")
            mov_targs_sersic, mt_galaxy_segmap = self.movingTargetInputs(self.params['simSignals']['movingTargetSersic'],
                                                                         'galaxies',
                                                                         MT_tracking=tracking,
                                                                         tracking_ra_vel=ra_vel,
                                                                         tracking_dec_vel=dec_vel,
                                                                         trackingPixVelFlag=vel_flag,
                                                                         non_sidereal_ra_interp_function=ra_interp_fncn,
                                                                         non_sidereal_dec_interp_function=dec_interp_fncn,
                                                                         add_ghosts=add_ghosts
                                                                         )

            mov_targs_ramps.append(mov_targs_sersic)
            if mov_targs_segmap is None:
                mov_targs_segmap = np.copy(mt_galaxy_segmap)
            else:
                mov_targs_segmap += mt_galaxy_segmap

        # moving target using an extended object
        if self.runStep['movingTargetsExtended']:
            self.logger.info("Adding moving extended sources to seed image.")
            mov_targs_ext, mt_ext_segmap = self.movingTargetInputs(self.params['simSignals']['movingTargetExtended'],
                                                                   'extended',
                                                                   MT_tracking=tracking,
                                                                   tracking_ra_vel=ra_vel,
                                                                   tracking_dec_vel=dec_vel,
                                                                   trackingPixVelFlag=vel_flag,
                                                                   non_sidereal_ra_interp_function=ra_interp_fncn,
                                                                   non_sidereal_dec_interp_function=dec_interp_fncn,
                                                                   add_ghosts=add_ghosts
                                                                   )

            mov_targs_ramps.append(mov_targs_ext)
            if mov_targs_segmap is None:
                mov_targs_segmap = np.copy(mt_ext_segmap)
            else:
                mov_targs_segmap += mt_ext_segmap

        mov_targs_integration = None
        if self.runStep['movingTargets'] or self.runStep['movingTargetsSersic'] or self.runStep['movingTargetsExtended']:
            # Combine the ramps of the moving targets if there is more than one type
            mov_targs_integration = mov_targs_ramps[0]
            if len(mov_targs_ramps) > 1:
                for i in range(1, len(mov_targs_ramps)):
                    mov_targs_integration += mov_targs_ramps[i]
        return mov_targs_integration, mov_targs_segmap

    def calcCoordAdjust(self):
        # Calculate the factors by which to expand the output array size, as well as the coordinate
        # offsets between the nominal output array and the input lists if the observation being
        # modeled is wfss

        instrument = self.params['Inst']['instrument']

        # Normal imaging with grism image requested
        if (instrument.lower() == 'nircam' and self.params['Output']['grism_source_image']) or \
           (instrument.lower() == 'niriss' and (self.params['Inst']['mode'] in ["pom", "wfss"] or self.params['Output']['grism_source_image'])):
            self.coord_adjust['x'] = self.grism_direct_factor_x
            self.coord_adjust['y'] = self.grism_direct_factor_y
            self.coord_adjust['xoffset'] = self.trans_ff_xmin + self.subarray_bounds[0]
            self.coord_adjust['yoffset'] = self.trans_ff_ymin + self.subarray_bounds[1]

    def non_sidereal_seed(self):
        """
        Create a seed EXPOSURE in the case where the instrument is tracking
        a non-sidereal target
        """
        # Only attempt to add ghosts to NIRISS observations where the
        # user has requested it.
        add_ghosts = False
        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            add_ghosts = True

        # Create a count rate image containing only the non-sidereal target(s)
        # These will be stationary in the fov
        nonsidereal_countrate, nonsidereal_segmap, self.ra_vel, self.dec_vel, self.nonsidereal_pix_vel_flag, self.nonsidereal_ra_interp, \
           self.nonsidereal_dec_interp = self.nonsidereal_CRImage(self.params['simSignals']['movingTargetToTrack'])

        # Expand into a RAPID exposure and convert from signal rate to signals
        ns_yd, ns_xd = nonsidereal_countrate.shape
        ns_int = self.params['Readout']['nint']
        ns_group = self.params['Readout']['ngroup']
        ns_nframe = self.params['Readout']['nframe']
        ns_nskip = self.params['Readout']['nskip']
        totframes = ns_group * (ns_nframe + ns_nskip)
        tmptimes = self.frametime * np.arange(1, totframes + 1)

        non_sidereal_ramp = np.zeros((ns_int, totframes, ns_yd, ns_xd))
        for i in range(totframes):
            for integ in range(ns_int):
                non_sidereal_ramp[integ, i, :, :] = nonsidereal_countrate * tmptimes[i]

        # Now we need to collect all the other sources (point sources,
        # galaxies, extended) in the other input files, and treat them
        # as targets which will move across the field of view during
        # the exposure.
        mtt_data_list = []
        mtt_data_segmap = None

        if self.runStep['pointsource']:
            # Now ptsrc is a list, which we need to provide to
            # movingTargetInputs
            self.logger.info("Adding moving background point sources to seed image.")
            mtt_ptsrc, mtt_ptsrc_segmap = self.movingTargetInputs(self.params['simSignals']['pointsource'],
                                                                  'point_source',
                                                                  MT_tracking=True,
                                                                  tracking_ra_vel=self.ra_vel,
                                                                  tracking_dec_vel=self.dec_vel,
                                                                  trackingPixVelFlag=self.nonsidereal_pix_vel_flag,
                                                                  non_sidereal_ra_interp_function=self.nonsidereal_ra_interp,
                                                                  non_sidereal_dec_interp_function=self.nonsidereal_dec_interp,
                                                                  add_ghosts=add_ghosts)
            mtt_data_list.append(mtt_ptsrc)
            if mtt_data_segmap is None:
                mtt_data_segmap = np.copy(mtt_ptsrc_segmap)
            else:
                mtt_data_segmap += mtt_ptsrc_segmap
            self.logger.info(("Done with creating moving targets from {}"
                              .format(self.params['simSignals']['pointsource'])))

        if self.runStep['galaxies']:
            self.logger.info("Adding moving background galaxies to seed image.")
            mtt_galaxies, mtt_galaxies_segmap = self.movingTargetInputs(self.params['simSignals']['galaxyListFile'],
                                                                        'galaxies',
                                                                        MT_tracking=True,
                                                                        tracking_ra_vel=self.ra_vel,
                                                                        tracking_dec_vel=self.dec_vel,
                                                                        trackingPixVelFlag=self.nonsidereal_pix_vel_flag,
                                                                        non_sidereal_ra_interp_function=self.nonsidereal_ra_interp,
                                                                        non_sidereal_dec_interp_function=self.nonsidereal_dec_interp,
                                                                        add_ghosts=add_ghosts)
            mtt_data_list.append(mtt_galaxies)
            if mtt_data_segmap is None:
                mtt_data_segmap = np.copy(mtt_galaxies_segmap)
            else:
                mtt_data_segmap += mtt_galaxies_segmap

            self.logger.info(("Done with creating moving targets from {}".
                              format(self.params['simSignals']['galaxyListFile'])))

        if self.runStep['extendedsource']:
            self.logger.info("Adding moving background extended sources to seed image.")
            mtt_ext, mtt_ext_segmap = self.movingTargetInputs(self.params['simSignals']['extended'],
                                                              'extended',
                                                              MT_tracking=True,
                                                              tracking_ra_vel=self.ra_vel,
                                                              tracking_dec_vel=self.dec_vel,
                                                              trackingPixVelFlag=self.nonsidereal_pix_vel_flag,
                                                              non_sidereal_ra_interp_function=self.nonsidereal_ra_interp,
                                                              non_sidereal_dec_interp_function=self.nonsidereal_dec_interp,
                                                              add_ghosts=add_ghosts)
            mtt_data_list.append(mtt_ext)
            if mtt_data_segmap is None:
                mtt_data_segmap = np.copy(mtt_ext_segmap)
            else:
                mtt_data_segmap += mtt_ext_segmap

            self.logger.info(("Done with creating moving targets from {}".
                              format(self.params['simSignals']['extended'])))

        # Add in the other objects which are not being tracked on
        # (i.e. the sidereal targets)
        # For the moment, we consider all non-sidereal targets to be opaque.
        # That is, if they occult any background sources, the signal from those
        # background sources will not be added to the scene. This is controlled
        # using the segmentation map. Any pixels there classified as containing
        # the non-sidereal target will not have signal from background sources
        # added. I can't think of any non-sidereal targets that would not be
        # opaque, so for now we will leave this hardwired to be on. If there is
        # some issue down the road, we can turn this into a user input in the
        # yaml file.
        opaque_target = True
        if len(mtt_data_list) > 0:
            for i in range(len(mtt_data_list)):
                #non_sidereal_ramp += mtt_data_list[i]

                # If the tracked target is opaque (e.g. a planet, astroid, etc),
                # the background (trailed) objects will only be added to the pixels that
                # do not contain the tracked target. (i.e. we shouldn't see background
                # stars that are behind Jupiter).
                if opaque_target:
                    # Find pixels that do not contain the tracked target
                    # (Note that the segmap here is 2D but the seed images are 4D)
                    self.logger.info(('Tracked non-sidereal target is opaque. Background sources behind the target will not be added. '
                                      'If background sources are being suppressed in too many pixels (e.g. within the diffraction '
                                      'spikes of the primary target), try setting "signal_low_limit_for_segmap" in the input yaml '
                                      'file to a larger value. This will reduce the number of pixels associated with the primary '
                                      'target in the segmentation map, which defines the pixels where background targets will not '
                                      'be added.'))
                    outside_target = nonsidereal_segmap == 0

                    # Add signal from background targets only to those pixels that
                    # are not part of the tracked target
                    for integ in range(ns_int):
                        for framenum in range(non_sidereal_ramp.shape[1]):
                            ns_tmp_frame = non_sidereal_ramp[integ, framenum, :, :]
                            bcgd_srcs_frame = mtt_data_list[i][integ, framenum, :, :]
                            ns_tmp_frame[outside_target] = ns_tmp_frame[outside_target] + bcgd_srcs_frame[outside_target]
                            non_sidereal_ramp[integ, framenum, :, :] = ns_tmp_frame

        if mtt_data_segmap is not None:
            nonsidereal_segmap[outside_target] += mtt_data_segmap[outside_target]
        return non_sidereal_ramp, nonsidereal_segmap

    def movingTargetInputs(self, filename, input_type, MT_tracking=False,
                           tracking_ra_vel=None, tracking_dec_vel=None,
                           trackingPixVelFlag=False, non_sidereal_ra_interp_function=None,
                           non_sidereal_dec_interp_function=None, add_ghosts=True):
        """Read in listfile of moving targets and perform needed
        calculations to get inputs for moving_targets.py. Note that for non-sidereal
        exposures (MT_tracking == True), we potentially flip the coordinate system, and
        have the non-sidereal target remain at a single RA, Dec, while other sources all
        have changing RA, Dec with time. This flip is only used for long enough to calculate
        the detector x, y position of each target in each frame.

        Parameters
        ----------

        filename : str
            Name of catalog contining moving target sources

        input_type : str
            Specifies type of sources. Can be 'point_source','galaxies', or 'extended'

        MT_tracking : bool
            If True, observation is non-sidereal (i.e. telescope is tracking the moving target)

        tracking_ra_vel : float
            Velocity of the moving target in the detector x or right ascension direction.
            Units are pixels/hour or arcsec/hour depending on trackingPixVelFlag.

        tracking_dec_vel : float
            Velocity of moving target in the detector y or declination direction.
            Units are pixels/hour or arcsec/hour depending on trackingPixVelFlag.

        trackingPixVelFlag : bool
            If True, tracking_ra_vel and tracking_dec_vel are in units of pixels/hour, and
            velocities are in the detector x and y directions, respectively.
            If False, velocity untis are arcsec/hour and directions are RA, and Dec.

        non_sidereal_ra_interp_function : scipy.interpolate.interp1d
            Interpolation function giving the RA of the non-sidereal source versus calendar
            timestamp

        non_sidereal_dec_interp_function : scipy.interpolate.interp1d
            Interpolation function giving the Dec of the non-sidereal source versus calendar
            timestamp

        add_ghosts : bool
            If True, add ghost sources corresponding to the sources in the input catalog

        Returns
        -------

        mt_integration : numpy.ndarray
            4D array containing moving target seed image

        moving_segmap.segmap : numpy.ndarray
            2D array containing segmentation map that goes with the seed image.
            Segmentation map is based on the final frame in the seed image.
        """
        # Read input file - should be able to use for all modes
        mtlist, pixelFlag, pixvelflag, magsys = file_io.readMTFile(filename)

        # If there is no ephemeris file given and no x_or_RA_velocity
        # column (i.e. we have a catalog of sidereal sources), then
        # set add the velocity columns and set the velocities to zero.
        # Also set the pixel velocity flag to the same value as the
        # pixel flag, to minimize coordinate transforms later.
        if 'x_or_RA_velocity' not in mtlist.colnames and 'ephemeris_file' not in mtlist.colnames:
            self.logger.info('Sidereal catalog. Setting velocities to zero before proceeding.')
            nelem = len(mtlist['x_or_RA'])
            mtlist['x_or_RA_velocity'] = [0.] * nelem
            mtlist['y_or_Dec_velocity'] = [0.] * nelem
            pixelvelflag = pixelFlag

        # Get catalog index numbers
        indexes = mtlist['index']

        # Exposure times for all frames
        numints = self.params['Readout']['nint']
        numgroups = self.params['Readout']['ngroup']
        numframes = self.params['Readout']['nframe']
        numskips = self.params['Readout']['nskip']
        numresets = self.params['Readout']['resets_bet_ints']

        frames_per_group = numframes + numskips
        frames_per_integration = numgroups * frames_per_group
        total_frames = numgroups * frames_per_group
        # If only one integration per exposure, then total_frames
        # above is correct. For >1 integration, we need to add the reset
        # frame to each integration (except the first), and sum the number of
        # frames for all integrations

        if numints > 1:
            # Frames for all integrations
            total_frames *= numints
            # Add the resets for all but the last integrations
            total_frames += (numresets * (numints - 1))
        frameexptimes = self.frametime * np.arange(-1, total_frames)

        # output image dimensions
        dims = self.nominal_dims
        newdimsx = int(dims[1] * self.coord_adjust['x'])
        newdimsy = int(dims[0] * self.coord_adjust['y'])

        # Set up seed integration
        mt_integration = np.zeros((numints, frames_per_integration, newdimsy, newdimsx))

        # Corresponding (2D) segmentation map
        moving_segmap = segmap.SegMap()
        moving_segmap.xdim = newdimsx
        moving_segmap.ydim = newdimsy
        moving_segmap.initialize_map()

        # Check the source list and remove any sources that are well outside the
        # field of view of the detector. These sources cause the coordinate
        # conversion to hang.
        indexes, mtlist = self.remove_outside_fov_sources(indexes, mtlist, pixelFlag, 4096)

        # Determine the name of the column to use for source magnitudes
        mag_column = self.select_magnitude_column(mtlist, filename)

        # Get the calendar dates associated with each frame
        ob_time = '{}T{}'.format(self.params['Output']['date_obs'], self.params['Output']['time_obs'])

        # Allow time_obs to have an integer or fractional number of seconds
        try:
            start_date = datetime.datetime.strptime(ob_time, '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            start_date = datetime.datetime.strptime(ob_time, '%Y-%m-%dT%H:%M:%S.%f')
        all_times = [ephemeris_tools.to_timestamp(start_date + datetime.timedelta(seconds=elem)) for elem in frameexptimes]

        # If the ephemeris_file column is not present, add it and populate it with
        # 'none' for all entries. This will make for fewer possibilities when looping
        # over sources later
        if 'ephemeris_file' not in mtlist.colnames:
            mtlist['ephemeris_file'] = ['None'] * len(mtlist['x_or_RA'])

        # If there is an interpolation function for the non-sidereal source's position,
        # get the position of the source at all times. The catalog may have different
        # ephemeris files for different sources, so we can't get background source
        # positions here.
        delta_non_sidereal_x = None
        delta_non_sidereal_y = None
        delta_non_sidereal_ra = None
        delta_non_sidereal_dec = None
        if non_sidereal_ra_interp_function is not None:
            self.logger.info(("Finding non-sidereal source's positions at each frame in order to "
                              "calculate the offsets to apply to the background sources."))
            ra_non_sidereal = non_sidereal_ra_interp_function(all_times)
            dec_non_sidereal = non_sidereal_dec_interp_function(all_times)
            delta_non_sidereal_ra = ra_non_sidereal - ra_non_sidereal[0]
            delta_non_sidereal_dec = dec_non_sidereal - dec_non_sidereal[0]
        elif tracking_ra_vel is not None:
            # If the non-sidereal source velocity is given using manual inputs rather
            # than an ephemeris file, use that to get the source location vs time
            self.logger.info(("Using the provided non-sidereal velocity values to determine "
                               "offsets to apply to the background sources."))
            if trackingPixVelFlag:
                # Here the non-sidereal source velocity is in units of pix/hour.
                # Convert to pixels per second and multply by frame times
                delta_non_sidereal_x = (tracking_ra_vel / 3600.) * frameexptimes
                delta_non_sidereal_y = (tracking_dec_vel / 3600.) * frameexptimes
            else:
                # Here the non-sidereal source velocity is in units of arcsec/hour.
                # Convert to degrees per hour and multply by frame times
                delta_non_sidereal_ra = (tracking_ra_vel / 3600. / 3600.) * frameexptimes
                delta_non_sidereal_dec = (tracking_dec_vel / 3600. / 3600.) * frameexptimes

        # Loop over sources in the catalog
        times = []
        obj_counter = 0
        time_reported = False
        source_spatial_frequency_angular = self.source_spatial_frequency_pix * self.siaf.XSciScale # arcsec
        for index, entry in zip(indexes, mtlist):
            start_time = time.time()

            # Initialize variables that will hold source locations
            ra_frames = None
            dec_frames = None
            x_frames = None
            y_frames = None

            # Get the RA, Dec or x,y for the source in all frames
            # not including any effects from non-sidereal tracking.
            # If an ephemeris file is given read it in
            if entry['ephemeris_file'].lower() != 'none':
                self.logger.info(("Using ephemeris file {} to find the location of source #{} in {}."
                                  .format(entry['ephemeris_file'], index, filename)))
                ra_eph, dec_eph = ephemeris_tools.get_ephemeris(entry['ephemeris_file'])

                # Create list of positions for all frames
                ra_frames = ra_eph(all_times)
                dec_frames = dec_eph(all_times)

                # Calculate the source locations at sub-frametimes, based on the requested spatial frequency
                ra_frames_nested, dec_frames_nested, subframe_times_nested =  ephemeris_tools.calculate_nested_positions(ra_frames, dec_frames, all_times,
                                                                                                                         source_spatial_frequency_angular,
                                                                                                                         ra_ephemeris=ra_eph, dec_ephemeris=dec_eph,
                                                                                                                         position_units='angular')

            else:
                self.logger.info(("Using provided velocities to find the location of source #{} in {}.".format(index, filename)))
                if pixvelflag:
                    delta_x_frames = (entry['x_or_RA_velocity'] / 3600.) * frameexptimes
                    delta_y_frames = (entry['y_or_Dec_velocity'] / 3600.) * frameexptimes
                else:
                    delta_ra_frames = (entry['x_or_RA_velocity'] / 3600. / 3600.) * frameexptimes
                    delta_dec_frames = (entry['y_or_Dec_velocity'] / 3600. / 3600.) * frameexptimes

                if pixelFlag:
                    # Moving target position given in x, y pixel units. Add delta x, y
                    # to get target location at each frame
                    if pixvelflag:
                        x_frames = entry['x_or_RA'] + delta_x_frames
                        y_frames = entry['y_or_Dec'] + delta_y_frames
                    else:
                        # Here we have source locations in x,y but velocities
                        # in delta RA, Dec. Translate locations to RA, Dec
                        # and then add the deltas
                        _, _, entry_ra, entry_dec, pra_str, pdec_str = self.get_positions(entry['x_or_RA'], entry['y_or_Dec'], True, 4096)
                        ra_frames = entry_ra + delta_ra_frames
                        dec_frames = entry_dec + delta_dec_frames
                        x_frames = None
                        y_frames = None
                else:
                    if pixvelflag:
                        # Here locations are in RA, Dec, and velocities are in x, y
                        # So translate locations to x, y first.
                        entry_x, entry_y, _, _, _, _ = self.get_positions(entry['x_or_RA'], entry['y_or_Dec'], False, 4096)
                        x_frames = entry_x + delta_x_frames
                        y_frames = entry_y + delta_y_frames
                        ra_frames = None
                        dec_frames = None
                    else:
                        # Here locations are in RA, Dec, and velocities are in RA, Dec
                        ra_frames = entry['x_or_RA'] + delta_ra_frames
                        dec_frames = entry['y_or_Dec'] + delta_dec_frames

                # Calculate the source locations at sub-frametimes, based on the requested spatial frequency
                if ra_frames is not None:
                    ra_frames_nested, dec_frames_nested, subframe_times_nested =  ephemeris_tools.calculate_nested_positions(ra_frames, dec_frames, all_times,
                                                                                                                             source_spatial_frequency_angular,
                                                                                                                             ra_ephemeris=None, dec_ephemeris=None,
                                                                                                                             position_units='angular')
                elif x_frames is not None:
                    x_frames_nested, y_frames_nested, subframe_times_nested =  ephemeris_tools.calculate_nested_positions(x_frames, y_frames, all_times,
                                                                                                                          self.source_spatial_frequency_pix,
                                                                                                                          ra_ephemeris=None, dec_ephemeris=None,
                                                                                                                          position_units='pixels')
            # Non-sidereal observation: in this case, if we are working with RA, Dec
            # values, the coordinate sytem flips such that the non-sidereal target
            # that is being tracked will stay at the same RA', Dec' for the duration
            # of the exposure, while background sources will have RA', Dec' values that
            # change frame-to-frame. If working in x, y pixel units, the same applies
            # with the background targets changing position with time
            if MT_tracking:

                self.logger.info(f"Updating source #{index} location based on non-sidereal source motion.")
                if delta_non_sidereal_ra is None:
                    # Here the non-sidereal target's offsets are in units of pixels
                    if ra_frames is not None:
                        # Here the background target position is in units of RA, Dec.
                        # So we need to first convert it to x, y

                        x_frames, y_frames = self.radec_list_to_xy_list(ra_frames, dec_frames)
                        ra_frames = None
                        dec_frames = None

                    # Now that the background source's positions are guaranteed to be in units
                    # of x,y, add the non-sidereal offsets
                    x_frames -= delta_non_sidereal_x
                    y_frames -= delta_non_sidereal_y

                    # Calculate the source locations at sub-frametimes, based on the requested spatial frequency
                    x_frames_nested, y_frames_nested, subframe_times_nested =  ephemeris_tools.calculate_nested_positions(x_frames, y_frames, all_times,
                                                                                                                          self.source_spatial_frequency_pix,
                                                                                                                          ra_ephemeris=None, dec_ephemeris=None,
                                                                                                                          position_units='pixels')

                else:
                    # Here the non-sidereal target's offsets are in units of RA, Dec
                    if x_frames is not None:
                        # Here the background target position is in units of x, y
                        # so we need to first convert it to RA, Dec
                        ra_frames, dec_frames = self.xy_list_to_radec_list(x_frames, y_frames)
                        x_frames = None
                        y_frames = None

                    # Now that the background source's positions are guaranteed to be in
                    # units of RA, Dec, add the non-sidereal offsets
                    ra_frames -= delta_non_sidereal_ra
                    dec_frames -= delta_non_sidereal_dec

                    # Calculate the source locations at sub-frametimes, based on the requested spatial frequency
                    ra_frames_nested, dec_frames_nested, subframe_times_nested =  ephemeris_tools.calculate_nested_positions(ra_frames, dec_frames, all_times,
                                                                                                                             source_spatial_frequency_angular,
                                                                                                                             ra_ephemeris=None, dec_ephemeris=None,
                                                                                                                             position_units='angular')

            # Make sure that ra_frames and x_frames are both populated
            if x_frames is None:
                x_frames, y_frames = self.radec_list_to_xy_list(ra_frames, dec_frames)
                x_frames_nested, y_frames_nested = self.radec_list_to_xy_list(ra_frames_nested, dec_frames_nested)

            if ra_frames is None:
                ra_frames, dec_frames = self.xy_list_to_radec_list(x_frames, y_frames)
                ra_frames_nested, dec_frames_nested = self.xy_list_to_radec_list(x_frames_nested, y_frames_nested)

            # Get countrate and PSF size info
            if entry[mag_column] is not None:
                rate = utils.magnitude_to_countrate(self.instrument, self.params['Readout']['filter'],
                                                    magsys, entry[mag_column],
                                                    photfnu=self.photfnu,
                                                    photflam=self.photflam, vegamag_zeropoint=self.vegazeropoint)
            else:
                rate = 1.0

            psf_x_dim = self.find_psf_size(rate)
            psf_dimensions = (psf_x_dim, psf_x_dim)

            # If we have a point source, we can easily determine whether
            # it completely misses the detector, since we know the size
            # of the stamp already. For galaxies and extended sources,
            # we have to get the stamp image first to see if any part of
            # the stamp lands on the detector.
            status = 'on'
            if input_type == 'point_source':

                status = self.on_detector(np.array(x_frames), np.array(y_frames), psf_dimensions,
                                          (newdimsx, newdimsy))
            if status == 'off':
                continue

            # Create a nested list of PSFs to go along with the nested position lists
            psf_frames_nested = []
            aper_x_min_of_stamp_nested = []
            aper_y_min_of_stamp_nested = []
            for x_fr, y_fr in zip(x_frames_nested, y_frames_nested):
                psf_subframe = []
                aper_x_min_subframe = []
                aper_y_min_subframe = []
                for x_subframe, y_subframe in zip(x_fr, y_fr):
                    eval_psf, aperxmin, aperymin, minx, miny, wings_added = self.create_psf_stamp(x_subframe, y_subframe, psf_x_dim, psf_x_dim,
                                                                                                  ignore_detector=True)
                    psf_subframe.append(eval_psf)
                    aper_x_min_subframe.append(aperxmin)
                    aper_y_min_subframe.append(aperymin)

                psf_frames_nested.append(psf_subframe)
                aper_x_min_of_stamp_nested.append(aper_x_min_subframe)
                aper_y_min_of_stamp_nested.append(aper_y_min_subframe)

            if psf_frames_nested[0][0] is None:
                continue

            # If we want to keep track of ghosts associated with the input sources,
            # determine the ghosts' locations here. Note that ghost locations do not
            # move in the same magnitude/direction as the actual sources, so we need
            # to determine source locations from the real source's location in each
            # frame individually. It also does not make sense to convolve the ghost
            # stamp image with the same subpixel centered PSFs used for the real source,
            # so we'll just convolve with the same PSF for all ghost positions.
            if add_ghosts:
                ghost_x_frames_nested = []
                ghost_y_frames_nested = []
                ghost_stamp_nested = []
                raise NotImplementedError('Need to update to work with gridded PSF evaluations and nested ra_frames, dec_frames.')
                for tmp_x_frames, tmp_y_frames in zip(x_frames_nested, y_frames_nested):
                    ghost_x_frames, ghost_y_frames, ghost_mags, ghost_countrate, ghost_file = self.locate_ghost(tmp_x_frames, tmp_y_frames, rate,
                                                                                                                magsys, entry, input_type,
                                                                                                                log_skipped_filters=False)
                    ghost_x_frames_nested.append(ghost_x_frames)
                    ghost_y_frames_nested.append(ghost_y_frames)

                if ghost_file is not None:
                    ghost_stamp, ghost_header = self.basic_get_image(ghost_file)

                    # Normalize the ghost stamp image to match ghost_countrate
                    ghost_stamp = ghost_stamp / np.sum(ghost_stamp) * ghost_countrate

                    # Create a nested list of stamp images, to match the nested lists of x and y positions
                    ghost_stamp_nested = []
                    for sublist in ghost_x_frames_nested:
                        tmp = [ghost_stamp] * len(sublist)
                        ghost_stamp_nested.append(tmp)

                    # Convolve with PSF if requested
                    if self.params['simSignals']['PSFConvolveExtended']:
                        # eval_psf should be close to 1.0, but not exactly. For the purposes
                        # of convolution, we want the total signal to be exactly 1.0
                        conv_psf = eval_psf / np.sum(eval_psf)
                        convolved_ghost_stamp = s1.fftconvolve(ghost_stamp, conv_psf, mode='same')

                        convolved_ghost_stamp_nested = []
                        for sublist in ghost_stamp_nested:
                            tmp = [convolved_ghost_stamp] * len(sublist)
                            convolved_ghost_stamp_nested.append(tmp)

                        ghost_stamp_nested = convolved_ghost_stamp_nested

            if input_type == 'point_source':
                # Multiply the nested PSFs by the signal rate per second for the source
                stamp_nested = []
                for psf_sublist in psf_frames_nested:
                    stamp_sublist = []
                    for psf_entry in psf_sublist:
                        stamp_sublist.append(psf_entry * rate)
                    stamp_nested.append(stamp_sublist)

            elif input_type == 'extended':
                raise NotImplementedError('Moving extended objects not yet supported.')
                stamp, header = self.basic_get_image(entry['filename'])
                # Rotate the stamp image if requested, but don't do so if the specified pos angle is None
                stamp = self.rotate_extended_image(stamp, entry['pos_angle'])

                # If no magnitude is given, use the extended image as-is
                if rate != 1.0:
                    stamp /= np.sum(stamp)
                    stamp *= rate

                # Convolve with instrument PSF if requested
                if self.params['simSignals']['PSFConvolveExtended']:
                    stamp_dims = stamp.shape
                    # If the stamp image is smaller than the PSF in either
                    # dimension, embed the stamp in an array that matches
                    # the psf size. This is so the upcoming convolution will
                    # produce an output that includes the wings of the PSF
                    psf_shape = eval_psf.shape
                    if ((stamp_dims[0] < psf_shape[0]) or (stamp_dims[1] < psf_shape[1])):
                        stamp = self.enlarge_stamp(stamp, psf_shape)
                        stamp_dims = stamp.shape

                    # Convolve stamp with PSF
                    stamp_nested = []
                    for psf_sublist in psf_frames_nested:
                        stamp_sublist = []
                        for psf_entry in psf_sublist:
                            stamp_sublist.append(s1.fftconvolve(stamp, psf_entry, mode='same'))
                        stamp_nested.append(stamp_sublist)


            elif input_type == 'galaxies':
                raise NotImplementedError('Moving 2D sersic objects not yet supported.')
                xposang = self.calc_x_position_angle(entry)

                # First create the galaxy
                stamp = self.create_galaxy(entry['radius'], entry['ellipticity'], entry['sersic_index'],
                                           xposang*np.pi/180., rate, 0., 0.)

                # If the stamp image is smaller than the PSF in either
                # dimension, embed the stamp in an array that matches
                # the psf size. This is so the upcoming convolution will
                # produce an output that includes the wings of the PSF
                galdims = stamp.shape
                psf_shape = eval_psf.shape
                if ((galdims[0] < psf_shape[0]) or (galdims[1] < psf_shape[1])):
                    stamp = self.enlarge_stamp(stamp, psf_shape)
                    galdims = stamp.shape

                # Convolve the galaxy with the instrument PSF
                stamp_nested = []
                for psf_sublist in psf_frames_nested:
                    stamp_sublist = []
                    for psf_entry in psf_sublist:
                        stamp_sublist.append(s1.fftconvolve(stamp, psf_entry, mode='same'))
                    stamp_nested.append(stamp_sublist)

            # Now that we have stamp images for galaxies and extended
            # sources, check to see if they overlap the detector or not.
            # NOTE: this will only catch sources that never overlap the
            # detector for any of their positions.
            if input_type != 'point_source':
                status = self.on_detector(x_frames, y_frames, stamp_nested[0][0].shape,
                                          (newdimsx, newdimsy))
            if status == 'off':
                continue

            # Need to feed info into moving_targets one integration at a time.
            # No need to feed in the reset frames, but they are necessary
            # before this point in order to get the timing and positions
            # correct.
            for integ in range(numints):

                # We add integ to framestart to account for the reset frame that occurs
                # between each integration. This means that the reset frame is the
                # last frame of the set, except for the final integration, where the
                # reset frame is not present
                framestart = integ * frames_per_integration + integ
                frameend = framestart + frames_per_integration

                # Now check to see if the stamp image overlaps the output
                # aperture for this integration only. Above we removed sources
                # that never overlap the aperture. Here we want to get rid
                # of sources that overlap the detector in some integrations,
                # but not this particular integration
                status = 'on'
                status = self.on_detector(x_frames[integ],
                                          y_frames[integ],
                                          stamp_nested[0][0].shape, (newdimsx, newdimsy))

                if status == 'off':
                    continue

                # Now create the moving target ramp for this source
                mt = moving_targets.MovingTarget()

                mt_source = mt.create(stamp_nested[framestart:frameend], x_frames_nested[framestart:frameend],
                                      y_frames_nested[framestart:frameend], aper_x_min_of_stamp_nested[framestart:frameend],
                                      aper_y_min_of_stamp_nested[framestart:frameend], subframe_times_nested[framestart:frameend],
                                      self.frametime, newdimsx, newdimsy)

                mt_integration[integ, :, :, :] += mt_source

                # Add object to segmentation map
                moving_segmap.add_object_threshold(mt_source[-1, :, :], 0, 0, index, self.segmentation_threshold)

                if add_ghosts and ghost_file is not None:
                    # Check if the ghost lands on the detector
                    ghost_status = self.on_detector(ghost_x_frames_nested[integ][0],
                                                    ghost_y_frames_nested[integ][0],
                                                    ghost_stamp.shape, (newdimsx, newdimsy))

                    if ghost_status == 'off':
                        continue

                    mt = moving_targets.MovingTarget()
                    mt_ghost_source = mt.create(ghost_stamp_nested[framestart:frameend], ghost_x_frames_nested[framestart:frameend],
                                                ghost_y_frames_nested[framestart:frameend],
                                                self.frametime, newdimsx, newdimsy)

                    mt_integration[integ, :, :, :] += mt_ghost_source

                    # Add object to segmentation map
                    moving_segmap.add_object_threshold(mt_ghost_source[-1, :, :], 0, 0, index, self.segmentation_threshold)

            # Check the elapsed time for creating each object
            elapsed_time = time.time() - start_time
            times.append(elapsed_time)
            if obj_counter > 3 and not time_reported:
                avg_time = np.mean(times)
                total_time = len(indexes) * avg_time
                self.logger.info(("Expected time to process {} sources: {:.2f} seconds "
                                  "({:.2f} minutes)".format(len(indexes), total_time, total_time/60)))
                time_reported = True
            obj_counter += 1
        return mt_integration, moving_segmap.segmap

    def radec_list_to_xy_list(self, ra_list, dec_list):
        """Transform lists of RA, Dec positions to lists of detector x, y positions

        Parameters
        ----------
        ra_list : list
            List of RA values in decimal degrees

        dec_list : list
            List of Dec values in decimal degrees

        Returns
        -------
        x_list : numpy.ndarray
            1D array of x pixel values

        y_list : numpy.ndarray
            1D array of y pixel values
        """
        x_list = []
        y_list = []
        for in_ra, in_dec in zip(ra_list, dec_list):
            if isinstance(in_ra, np.ndarray):
                inner_x_list = []
                inner_y_list = []
                for inner_ra, inner_dec in zip(in_ra, in_dec):
                    x, y, ra, dec, ra_str, dec_str = self.get_positions(inner_ra, inner_dec, False, 4096)
                    inner_x_list.append(x)
                    inner_y_list.append(y)
                x_list.append(inner_x_list)
                y_list.append(inner_y_list)
            else:
                # Calculate the x,y position at each frame
                x, y, ra, dec, ra_str, dec_str = self.get_positions(in_ra, in_dec, False, 4096)
                x_list.append(x)
                y_list.append(y)
        return x_list, y_list

    def xy_list_to_radec_list(self, x_list, y_list):
        """Transform lists of x, y pixel positions to lists of RA, Dec positions

        Parameters
        ----------
        x_list : list
            List of x pixel values in decimal degrees

        y_list : list
            List of y pixel values in decimal degrees

        Returns
        -------
        ra_list : numpy.ndarray
            1D array of RA values

        dec_list : numpy.ndarray
            1D array of Dec values
        """
        ra_list = []
        dec_list = []
        for in_x, in_y in zip(x_list, y_list):
            if isinstance(in_x, np.ndarray):
                inner_ra_list = []
                inner_dec_list = []
                for inner_x, inner_y in zip(in_x, in_y):
                    x, y, ra, dec, ra_str, dec_str = self.get_positions(inner_x, inner_y, True, 4096)
                    inner_ra_list.append(ra)
                    inner_dec_list.append(dec)
                ra_list.append(inner_ra_list)
                dec_list.append(inner_dec_list)
            else:
                x, y, ra, dec, ra_str, dec_str = self.get_positions(in_x, in_y, True, 4096)
                ra_list.append(ra)
                dec_list.append(dec)
        #ra_list = np.array(ra_list)
        #dec_list = np.array(dec_list)
        return ra_list, dec_list

    def on_detector(self, xloc, yloc, stampdim, finaldim):
        """Given a set of x, y locations, stamp image dimensions,
        and final image dimensions, determine whether the stamp
        image will overlap at all with the final image, or
        completely miss it.

        Parameters
        ---------

        xloc : list
            X-coordinate locations of source

        yloc : list
            Y-coordinate locations of source

        stampdim : tuple
            (x,y) dimension lengths of stamp image

        finaldim : tuple
            (x,y) dimension lengths of final image

        Returns
        -------

        status : str
            state of stamp image:
            "on" -- stamp image fully or partially overlaps the final
                 image for at least one xloc, yloc pair
            "off" -- stamp image never overlaps with final image
        """
        status = 'on'
        stampx, stampy = stampdim
        stampminx = np.min(xloc - (stampx / 2))
        stampminy = np.min(yloc - (stampy / 2))
        stampmaxx = np.max(xloc + (stampx / 2))
        stampmaxy = np.max(yloc + (stampy / 2))
        finalx, finaly = finaldim
        if ((stampminx > finalx) or (stampmaxx < 0) or
           (stampminy > finaly) or (stampmaxy < 0)):
            status = 'off'
        return status

    def get_positions(self, input_x, input_y, pixel_flag, max_source_distance):
        """ Given an input position ( (x,y) or (RA,Dec) ) for a source, calculate
        the corresponding detector (x,y), RA, Dec, and provide RA and Dec strings.

        Parameters
        ----------

        input_x : str
            Detector x coordinate or RA of source. RA can be in decimal degrees or
            (e.g 10:23:34.2 or 10h23m34.2s)

        input_y : str
            Detector y coordinate or Dec of source. Dec can be in decimal degrees
            or (e.g. 10d:23m:34.2s)

        pixel_flag : bool
            True if input_x and input_y are in units of pixels. False if they are
            in the RA, Dec coordinate system.

        max_source_distance : float
            Maximum number of pixels from the aperture's reference location to keep
            a source. Sources very far off the detector will cause the calculation of
            RA, Dec to hang.

        Returns
        -------

        pixelx : float
            Detector x coordinate of source

        pixely : float
            Detector y coordinate of source

        ra : float
            RA of source (degrees)

        dec : float
            Dec of source (degrees)

        ra_string : str
            String representation of RA

        dec_string : str
            String representation of Dec
        """
        try:
            entry0 = float(input_x)
            entry1 = float(input_y)
            if not pixel_flag:
                ra_string, dec_string = self.makePos(entry0, entry1)
                ra_number = entry0
                dec_number = entry1
        except:
            # if inputs can't be converted to floats, then
            # assume we have RA/Dec strings. Convert to floats.
            ra_string = input_x
            dec_string = input_y
            ra_number, dec_number = utils.parse_RA_Dec(ra_string, dec_string)

        # Case where point source list entries are given with RA and Dec
        if not pixel_flag:

            # If distortion is to be included - either with or without the full set of coordinate
            # translation coefficients
            pixel_x, pixel_y = self.RADecToXY_astrometric(ra_number, dec_number)

        else:
            # Case where the point source list entry locations are given in units of pixels
            # In this case we have the source position, and RA/Dec are calculated only so
            # they can be written out into the output source list file.
            pixel_x = entry0
            pixel_y = entry1
            ra_number, dec_number, ra_string, dec_string = self.XYToRADec(pixel_x, pixel_y)

        return pixel_x, pixel_y, ra_number, dec_number, ra_string, dec_string

    def nonsidereal_CRImage(self, file):
        """
        Create countrate image of non-sidereal sources
        that are being tracked.

        Arguments:
        ----------
        file : str
            name of the file containing the tracked moving targets.

        Returns:
        --------
        totalCRImage : numpy.ndarray
            Countrate image containing the tracked non-sidereal targets.

        totalSegmap : numpy.ndarray
            Segmentation map of the non-sidereal targets

        track_ra_vel : float
            The RA velocity of the source. Set to None if
            an ephemeris file is used to get the target's locations

        track_dec_vel : float
            The Dec velocity of the source. Set to None if
            an ephemeris file is used to get the target's locations

        velFlag : str
            If 'velocity_pixels', then ```track_ra_vel``` and
            ```track_dec_vel``` are assumed to be in units of
            pixels per hour. Otherwise units are assumed to be
            arcsec per hour.

        ra_interpol_function : scipy.interpolate.interp1d
            If an ephemeris file is present in the source catalog, this
            is a function giving the RA of the target versus calendar
            timestamp. Otherwise set to None.

        dec_interpol_function : scipy.interpolate.interp1d
            If an ephemeris file is present in the source catalog, this
            is a function giving the Dec of the target versus calendar
            timestamp. Otherwise set to None.

        """
        totalCRList = []
        totalSegList = []

        # Read in file containing targets
        targs, pixFlag, velFlag, magsys = file_io.readMTFile(file)

        # We can only track one moving target at a time
        if len(targs) != 1:
            raise ValueError(("Catalog of non-sidereal sources to track in {} contains "
                              "{} sources. This catalog should contain a single source."
                              .format(file, len(targs))))

        # If the ephemeris column is there but unpopulated, remove it
        if 'ephemeris_file' in targs.colnames:
            if targs['ephemeris_file'][0].lower() == 'none':
                targs.remove_column('ephemeris_file')

        # If an ephemeris file is given, update the RA, Dec based
        # on the ephemeris. (i.e. the input RA, Dec values will be ignored)
        if 'ephemeris_file' in targs.colnames:
            self.logger.info(("Setting non-sidereal source intial RA, Dec based on ephemeris "
                              "file: {} and observation date/time.".format(file)))
            targs, ra_interpol_function, dec_interpol_function = self.ephemeris_radec_value_at_obstime(targs)
            track_ra_vel = None
            track_dec_vel = None
        else:
            # We need to keep track of the proper motion of the
            # target being tracked, because all other objects in
            # the field of view will be moving at the same rate
            # in the opposite direction. Start by using the values
            # from the catalog. If an ephemeris file is present,
            # these will be changed in favor of the RA and Dec
            # interpolation functions
            track_ra_vel = targs[0]['x_or_RA_velocity']
            track_dec_vel = targs[0]['y_or_Dec_velocity']
            ra_interpol_function = None
            dec_interpol_function = None

        # Sort the targets by whether they are point sources,
        # galaxies, extended
        ptsrc_rows = []
        galaxy_rows = []
        extended_rows = []
        for i, line in enumerate(targs):
            if 'point' in line['object'].lower():
                ptsrc_rows.append(i)
            elif 'sersic' in line['object'].lower():
                galaxy_rows.append(i)
            else:
                extended_rows.append(i)

        # Re-use functions for the sidereal tracking situation
        if len(ptsrc_rows) > 0:
            ptsrc = targs[ptsrc_rows]
            if pixFlag:
                meta0 = 'position_pixels'
            else:
                meta0 = ''
            if velFlag:
                meta1 = 'velocity_pixels'
            else:
                meta1 = ''
            meta2 = magsys

            meta3 = ('Point sources with non-sidereal tracking. '
                     'File produced by catalog_seed_image.py')
            meta4 = ('from run using non-sidereal moving target '
                     'list {}.'.format(self.params['simSignals']['movingTargetToTrack']))
            ptsrc.meta['comments'] = [meta0, meta1, meta2, meta3, meta4]
            temp_ptsrc_filename = os.path.join(self.params['Output']['directory'],
                                               'temp_non_sidereal_point_sources.list')
            self.logger.info(("Catalog with non-sidereal source transformed to point source catalog for the "
                              "purposes of placing the source at the requested location. New catalog saved to: "
                              "{}".format(temp_ptsrc_filename)))
            ptsrc.write(temp_ptsrc_filename, format='ascii', overwrite=True)

            ptsrc_list, ps_ghosts_file = self.get_point_source_list(temp_ptsrc_filename)
            ptsrcCRImage, ptsrcCRSegmap = self.make_point_source_image(ptsrc_list)
            totalCRList.append(ptsrcCRImage)
            totalSegList.append(ptsrcCRSegmap.segmap)

            # If ghost sources are present, then create a count rate image using
            # that extended image and add it to the list
            if ps_ghosts_file is not None:
                self.logger.info('Adding optical ghost from non-sidereal point source.')
                ps_ghosts_cat, ps_ghosts_stamps, _ = self.getExtendedSourceList(ps_ghosts_file, ghost_search=False)
                ps_ghosts_convolve = [self.params['simSignals']['PSFConvolveGhosts']] * len(ps_ghosts_cat)
                ghost_cr_image, ghost_segmap = self.make_extended_source_image(ps_ghosts_cat, ps_ghosts_stamps, ps_ghosts_convolve)
                totalCRList.append(ghost_cr_image)
                totalSegList.append(ghost_segmap)


        if len(galaxy_rows) > 0:
            galaxies = targs[galaxy_rows]
            if pixFlag:
                meta0 = 'position_pixels'
            else:
                meta0 = ''
            if velFlag:
                meta1 = 'velocity_pixels'
            else:
                meta1 = ''
            meta2 = magsys
            meta3 = ('Galaxies (2d sersic profiles) with non-sidereal '
                     'tracking. File produced by ramp_simulator.py')
            meta4 = ('from run using non-sidereal moving target '
                     'list {}.'.format(self.params['simSignals']['movingTargetToTrack']))
            galaxies.meta['comments'] = [meta0, meta1, meta2, meta3, meta4]
            temp_gal_filename = os.path.join(self.params['Output']['directory'], 'temp_non_sidereal_sersic_sources.list')
            self.logger.info(("Catalog with non-sidereal source transformed to galaxy catalog for the "
                              "purposes of placing the source at the requested location. New catalog saved to: "
                              "{}".format(temp_gal_filename)))
            galaxies.write(temp_gal_filename, format='ascii', overwrite=True)

            galaxyCRImage, galaxySegmap, gal_ghosts_file = self.make_galaxy_image(temp_gal_filename)
            totalCRList.append(galaxyCRImage)
            totalSegList.append(galaxySegmap)

            # If ghost sources are present, then create a count rate image using
            # that extended image and add it to the list
            if gal_ghosts_file is not None:
                self.logger.info('Adding optical ghost from non-sidereal galaxy source.')
                gal_ghosts_cat, gal_ghosts_stamps, _ = self.getExtendedSourceList(gal_ghosts_file, ghost_search=False)
                gal_ghosts_convolve = [self.params['simSignals']['PSFConvolveGhosts']] * len(gal_ghosts_cat)
                ghost_cr_image, ghost_segmap = self.make_extended_source_image(gal_ghosts_cat, gal_ghosts_stamps, gal_ghosts_convolve)
                totalCRList.append(ghost_cr_image)
                totalSegList.append(ghost_segmap)

        if len(extended_rows) > 0:
            extended = targs[extended_rows]

            if pixFlag:
                meta0 = 'position_pixels'
            else:
                meta0 = ''
            if velFlag:
                meta1 = 'velocity_pixels'
            else:
                meta1 = ''
            meta2 = magsys
            meta3 = 'Extended sources with non-sidereal tracking. File produced by ramp_simulator.py'
            meta4 = 'from run using non-sidereal moving target list {}.'.format(self.params['simSignals']['movingTargetToTrack'])
            extended.meta['comments'] = [meta0, meta1, meta2, meta3, meta4]
            temp_ext_filename = os.path.join(self.params['Output']['directory'],
                                               'temp_non_sidereal_extended_sources.list')
            self.logger.info(("Catalog with non-sidereal source transformed to extended source catalog for the "
                              "purposes of placing the source at the requested location. New catalog saved to: "
                              "{}".format(temp_ext_filename)))
            extended.write(temp_ext_filename, format='ascii', overwrite=True)

            extlist, extstamps, ext_ghosts_file = self.getExtendedSourceList(temp_ext_filename, ghost_search=True)
            if len(extlist) > 0:
                conv = [self.params['simSignals']['PSFConvolveExtended']] * len(extlist)
                extCRImage, extSegmap = self.make_extended_source_image(extlist, extstamps, conv)
            else:
                yd, xd = self.output_dims
                #extCRImage = np.zeros((self.params['Readout']['nint'], self.frames_per_integration, yd, xd))
                extCRImage = np.zeros((yd, xd))
                extSegmap = np.zeros((yd, xd)).astype(np.int64)

            totalCRList.append(extCRImage)
            totalSegList.append(extSegmap)

            # If ghost sources are present, then create a count rate image using
            # that extended image and add it to the list
            if ext_ghosts_file is not None:
                self.logger.info('Adding optical ghost from non-sidereal extended source.')
                ext_ghosts_cat, ext_ghosts_stamps, _ = self.getExtendedSourceList(ext_ghosts_file, ghost_search=False)
                ext_ghosts_convolve = [self.params['simSignals']['PSFConvolveGhosts']] * len(ext_ghosts_cat)
                ghost_cr_image, ghost_segmap = self.make_extended_source_image(ext_ghosts_cat, ext_ghosts_stamps, ext_ghosts_convolve)
                totalCRList.append(ghost_cr_image)
                totalSegList.append(ghost_segmap)

        # Now combine into a final countrate image of non-sidereal sources (that are being tracked)
        if len(totalCRList) > 0:
            totalCRImage = totalCRList[0]
            totalSegmap = totalSegList[0]
            if len(totalCRList) > 1:
                for i in range(1, len(totalCRList)):
                    totalCRImage += totalCRList[i]
                    totalSegmap += totalSegList[i] + i
        else:
            raise ValueError(("No non-sidereal countrate targets produced."
                              "You shouldn't be here."))
        return totalCRImage, totalSegmap, track_ra_vel, track_dec_vel, velFlag, ra_interpol_function, dec_interpol_function

    def ephemeris_radec_value_at_obstime(self, src_catalog):
        """Calculate the RA, Dec of the target at the observation time
        from the yaml file

        Parameters
        ----------
        src_catalog : astropy.table.Table
            Source catalog table or row

        Returns
        -------
        src_catalog : astropy.table.Table
            Modified source catalog table or row with RA, Dec values
            corresponding to the observation date

        ra_eph : scipy.interpolate.interp1d
            Interpolation function of source's RA (in degrees) versus
            calendar timestamp

        dec_eph : scipy.interpolate.interp1d
            Interpolation function of source's Dec (in degrees) versus
            calendar timestamp
        """
        # In this block we now assume a single target in the catalog
        # (or that ```src_catalog``` is a single row)
        ob_time = '{}T{}'.format(self.params['Output']['date_obs'], self.params['Output']['time_obs'])

        # Allow time_obs to have an integer or fractional number of seconds
        try:
            start_date = [ephemeris_tools.to_timestamp(datetime.datetime.strptime(ob_time, '%Y-%m-%dT%H:%M:%S'))]
        except ValueError:
            start_date = [ephemeris_tools.to_timestamp(datetime.datetime.strptime(ob_time, '%Y-%m-%dT%H:%M:%S.%f'))]

        self.logger.info(('Calculating target RA, Dec at the observation time using ephemeris file: {}'
                          .format(src_catalog['ephemeris_file'][0])))
        ra_eph, dec_eph = ephemeris_tools.get_ephemeris(src_catalog['ephemeris_file'][0])

        # If the input x_or_RA and y_or_Dec columns have values of 'none', then
        # populating them with the values calculated here results in truncated
        # values being used. So let's remove the old columns and re-add them
        src_catalog.remove_column('x_or_RA')
        src_catalog.remove_column('y_or_Dec')
        src_catalog.add_column(ra_eph(start_date), name='x_or_RA', index=1)
        src_catalog.add_column(dec_eph(start_date), name='y_or_Dec', index=2)
        return src_catalog, ra_eph, dec_eph

    def create_sidereal_image(self):
        # Generate a signal rate image from input sources
        if (self.params['Output']['grism_source_image'] == False) and (not self.params['Inst']['mode'] in ["pom", "wfss"]):
            signalimage = np.zeros(self.nominal_dims)
            segmentation_map = np.zeros(self.nominal_dims)
        else:
            signalimage = np.zeros(self.output_dims, dtype=float)
            segmentation_map = np.zeros(self.output_dims)


        instrument_name = self.params['Inst']['instrument'].lower()
        # yd, xd = signalimage.shape
        arrayshape = signalimage.shape

        # POINT SOURCES
        # Read in the list of point sources to add
        # Adjust point source locations using astrometric distortion
        # Translate magnitudes to counts in a single frame
        if self.runStep['pointsource'] is True:
            if not self.expand_catalog_for_segments:

                # CHECK IN INPUT PSF FILE IF THERE'S A BORESIGHT OFFSET TO BE APPLIED:
                offset_vector = None
                infile = glob.glob(os.path.join(self.params['simSignals']['psfpath'], "{}_{}_{}*.fits".format(self.params['Inst']['instrument'].lower(), self.detector.lower(), self.psf_filter.lower())))
                if len(infile) > 0:
                    header = fits.getheader(infile[0])
                    if ('BSOFF_V2' in header) and ('BSOFF_V3' in header):
                        offset_vector = header['BSOFF_V2']*60., header['BSOFF_V3']*60. #convert to arcseconds
                else:
                    self.logger.info("No PSF library matching '{}_{}_{}.fits'; ignoring boresight offset (if any)".format(self.params['Inst']['instrument'].lower(), self.detector.lower(), self.psf_filter.lower()))

                # Translate the point source list into an image
                self.logger.info('Creating point source lists')
                pslist, ps_ghosts_cat = self.get_point_source_list(self.params['simSignals']['pointsource'],
                                                                   segment_offset=offset_vector)

                psfimage, ptsrc_segmap = self.make_point_source_image(pslist)

                # If ghost sources are present, then make sure Mirage will retrieve
                # sources from an extended source catalog
                if ps_ghosts_cat is not None:
                    self.runStep['extendedsource'] = True
                    # Currently self.ghosts_catalogs is only used later for WFSS observations
                    self.ghost_catalogs.append(ps_ghosts_cat)

            elif self.expand_catalog_for_segments:
                # Expand the point source list for each mirror segment, and add together
                # the 18 point source images that used different PSFs.
                self.logger.info('Expanding the source catalog for 18 mirror segments')

                # Create empty image and segmentation map
                ptsrc_segmap = segmap.SegMap()
                ptsrc_segmap.ydim, ptsrc_segmap.xdim = self.output_dims
                ptsrc_segmap.initialize_map()
                psfimage = np.zeros(self.output_dims)

                library_list = get_segment_library_list(
                    self.params['Inst']['instrument'].lower(), self.detector, self.psf_filter,
                    self.params['simSignals']['psfpath'], pupil=self.psf_pupil
                )
                for i_segment in np.arange(1, 19):
                    self.logger.info('\nCalculating point source lists for segment {}'.format(i_segment))
                    # Get the RA/Dec offset that matches the given segment
                    offset_vector = get_segment_offset(i_segment, self.detector, library_list)

                    # need to add a new offset option to get_point_source_list:
                    pslist, _ = self.get_point_source_list(self.params['simSignals']['pointsource'],
                                                           segment_offset=offset_vector)

                    # Create a point source image, using the specific point
                    # source list and PSF for the given segment
                    seg_psfimage, ptsrc_segmap = self.make_point_source_image(pslist, segment_number=i_segment,
                                                                              ptsrc_segmap=ptsrc_segmap)

                    if self.params['Output']['save_intermediates'] is True:
                        seg_psfImageName = self.basename + '_pointSourceRateImage_seg{:02d}.fits'.format(i_segment)
                        h0 = fits.PrimaryHDU(seg_psfimage)
                        h0.writeto(seg_psfImageName, overwrite=True)
                        self.logger.info("    Segment {} point source image and segmap saved as {}".format(i_segment,
                                                                                                           seg_psfImageName))

                    psfimage += seg_psfimage

            ptsrc_segmap = ptsrc_segmap.segmap

            # Add the point source image to the overall image
            signalimage = signalimage + psfimage
            segmentation_map += ptsrc_segmap

            # To avoid problems with overlapping sources between source
            # types in observations to be dispersed, make the point
            # source-only segmentation map available as a class variable
            self.point_source_seed = psfimage
            self.point_source_seg_map = ptsrc_segmap

            # For seed images to be dispersed in WFSS mode,
            # embed the seed image in a full frame array. The disperser
            # tool does not work on subarrays
            aperture_suffix = self.params['Readout']['array_name'].split('_')[-1]

            # Save the point source seed image
            if instrument_name != 'fgs':
                self.ptsrc_seed_filename = '{}_{}_{}_ptsrc_seed_image.fits'.format(self.basename, self.params['Readout']['filter'],
                                                                                   self.params['Readout']['pupil'])
            else:
                self.ptsrc_seed_filename = '{}_ptsrc_seed_image.fits'.format(self.basename)
            self.saveSeedImage(self.point_source_seed, self.point_source_seg_map, self.ptsrc_seed_filename)
            self.logger.info("Point source image and segmap saved as {}".format(self.ptsrc_seed_filename))

        else:
            self.point_source_seed = None
            self.point_source_seg_map = None
            self.ptsrc_seed_filename = None
            ps_ghosts_cat = None

        # Simulated galaxies
        # Read in the list of galaxy positions/magnitudes to simulate
        # and create a countrate image of those galaxies.
        if self.runStep['galaxies'] is True:
            galaxyCRImage, galaxy_segmap, gal_ghosts_cat = self.make_galaxy_image(self.params['simSignals']['galaxyListFile'])

            # If ghost sources are present, then make sure Mirage will retrieve
            # sources from an extended source catalog
            if gal_ghosts_cat is not None:
                self.runStep['extendedsource'] = True
                # Currently self.ghosts_catalogs is only used later for WFSS observations
                self.ghost_catalogs.append(gal_ghosts_cat)

            # To avoid problems with overlapping sources between source
            # types in observations to be dispersed, make the galaxy-
            # only segmentation map available as a class variable
            self.galaxy_source_seed = galaxyCRImage
            self.galaxy_source_seg_map = galaxy_segmap

            # For seed images to be dispersed in WFSS mode,
            # embed the seed image in a full frame array. The disperser
            # tool does not work on subarrays
            aperture_suffix = self.params['Readout']['array_name'].split('_')[-1]

            # Save the galaxy source seed image
            if instrument_name != 'fgs':
                self.galaxy_seed_filename = '{}_{}_{}_galaxy_seed_image.fits'.format(self.basename, self.params['Readout']['filter'],
                                                                                     self.params['Readout']['pupil'])
            else:
                self.galaxy_seed_filename = '{}_galaxy_seed_image.fits'.format(self.basename)
            self.saveSeedImage(self.galaxy_source_seed, self.galaxy_source_seg_map, self.galaxy_seed_filename)
            self.logger.info("Simulated galaxy image and segmap saved as {}".format(self.galaxy_seed_filename))

            # Add galaxy segmentation map to the master copy
            segmentation_map = self.add_segmentation_maps(segmentation_map, galaxy_segmap)

            # add the galaxy image to the signalimage
            signalimage = signalimage + galaxyCRImage

        else:
            self.galaxy_source_seed = None
            self.galaxy_source_seg_map = None
            self.galaxy_seed_filename = None
            gal_ghosts_cat = None

        # read in extended signal image and add the image to the overall image
        if self.runStep['extendedsource'] is True:
            # Extended sources from user-provided catalog
            if self.params['simSignals']['extended'] != 'None':
                extended_list, extended_stamps, ext_ghosts_cat = self.getExtendedSourceList(self.params['simSignals']['extended'])
                extended_convolve = [self.params['simSignals']['PSFConvolveExtended']] * len(extended_list)
            else:
                extended_list = None
                extended_stamps = None
                ext_ghosts_cat = None
                extended_convolve = None

            # Currently self.ghosts_catalogs is only used later for WFSS observations
            if ext_ghosts_cat is not None:
                self.ghost_catalogs.append(ext_ghosts_cat)

            # Ghosts associated with point source catalog
            # Don't search for ghosts from ghosts
            if ps_ghosts_cat is not None:
                self.logger.info('Reading in optical ghost list from sidereal point source catalog.')
                extlist_from_ps_ghosts, extstamps_from_ps_ghosts, _ = self.getExtendedSourceList(ps_ghosts_cat, ghost_search=False)
                ps_ghosts_convolve = [self.params['simSignals']['PSFConvolveGhosts']] * len(extlist_from_ps_ghosts)
            else:
                extlist_from_ps_ghosts = None
                extstamps_from_ps_ghosts = None
                ps_ghosts_convolve = None

            # Ghosts associated with galaxy catalog
            # Don't search for ghosts from ghosts
            if gal_ghosts_cat is not None:
                self.logger.info('Reading in optical ghost list from sidereal galaxy source catalog.')
                extlist_from_gal_ghosts, extstamps_from_gal_ghosts, _ = self.getExtendedSourceList(gal_ghosts_cat, ghost_search=False)
                gal_ghosts_convolve = [self.params['simSignals']['PSFConvolveGhosts']] * len(extlist_from_gal_ghosts)
            else:
                extlist_from_gal_ghosts = None
                extstamps_from_gal_ghosts = None
                gal_ghosts_convolve = None

            # Ghosts associated with the extended source catalog
            # Don't search for ghosts from ghosts
            if ext_ghosts_cat is not None:
                self.logger.info('Reading in optical ghost list from sidereal extended source catalog.')
                extlist_from_ext_ghosts, extstamps_from_ext_ghosts, _ = self.getExtendedSourceList(ext_ghosts_cat, ghost_search=False)
                ext_ghosts_convolve = [self.params['simSignals']['PSFConvolveGhosts']] * len(extlist_from_ext_ghosts)
            else:
                extlist_from_ext_ghosts = None
                extstamps_from_ext_ghosts = None
                ext_ghosts_convolve = None

            possible_cats = [extended_list, extlist_from_ps_ghosts, extlist_from_gal_ghosts, extlist_from_ext_ghosts]
            extended_cats = [ele for ele in possible_cats if ele is not None]
            extlist = vstack(extended_cats)

            possible_stamps = [extended_stamps, extstamps_from_ps_ghosts, extstamps_from_gal_ghosts, extstamps_from_ext_ghosts]
            extended_stamps = [ele for ele in possible_stamps if ele is not None]
            extstamps = [item for ele in extended_stamps for item in ele]

            possible_convolutions = [extended_convolve, ps_ghosts_convolve, gal_ghosts_convolve, ext_ghosts_convolve]
            convolutions = [ele for ele in possible_convolutions if ele is not None]
            convols = [item for ele in convolutions for item in ele]

            # translate the extended source list into an image
            extimage, ext_segmap = self.make_extended_source_image(extlist, extstamps, convols)

            # To avoid problems with overlapping sources between source
            # types in observations to be dispersed, make the point
            # source-only segmentation map available as a class variable
            self.extended_source_seed = extimage
            self.extended_source_seg_map = ext_segmap

            # For seed images to be dispersed in WFSS mode,
            # embed the seed image in a full frame array. The disperser
            # tool does not work on subarrays
            aperture_suffix = self.params['Readout']['array_name'].split('_')[-1]

            # Save the extended source seed image
            if instrument_name != 'fgs':
                self.extended_seed_filename = '{}_{}_{}_extended_seed_image.fits'.format(self.basename, self.params['Readout']['filter'],
                                                                                         self.params['Readout']['pupil'])
            else:
                self.extended_seed_filename = '{}_extended_seed_image.fits'.format(self.basename)
            self.saveSeedImage(self.extended_source_seed, self.extended_source_seg_map, self.extended_seed_filename)
            self.logger.info("Extended object image and segmap saved as {}".format(self.extended_seed_filename))

            # Add galaxy segmentation map to the master copy
            segmentation_map = self.add_segmentation_maps(segmentation_map, ext_segmap)

            # add the extended image to the synthetic signal rate image
            signalimage = signalimage + extimage

        else:
            self.extended_source_seed = None
            self.extended_source_seg_map = None
            self.extended_seed_filename = None

        # ZODIACAL LIGHT
        if self.runStep['zodiacal'] is True:
            self.logger.warning(("\n\nWARNING: A file has been provided for a zodiacal light contribution "
                   "but zodi is included in the background addition in imaging/imaging TSO modes "
                   "if bkgdrate is set to low/medium/high, or for any WFSS/Grism TSO observations.\n\n"))
            # zodiangle = self.eclipticangle() - self.params['Telescope']['rotation']
            zodiangle = self.params['Telescope']['rotation']
            zodiacalimage, zodiacalheader = self.getImage(self.params['simSignals']['zodiacal'], arrayshape, True, zodiangle, arrayshape/2)

            # If the zodi image is not the same shape as the transmission
            # image, raise an exception. In the future we can make this more
            # flexible
            if zodiacalimage.shape != self.transmission_image.shape:
                raise IndexError("Zodiacal light image must have the same shape as the transmission image: {}".format(self.transmission_image.shape))

            signalimage = signalimage + zodiacalimage*self.params['simSignals']['zodiscale']

        # SCATTERED LIGHT - no rotation here.
        if self.runStep['scattered']:
            scatteredimage, scatteredheader = self.getImage(self.params['simSignals']['scattered'],
                                                            arrayshape, False, 0.0, arrayshape/2)

            # If the scattered light image is not the same shape as the transmission
            # image, raise an exception. In the future we can make this more
            # flexible
            if scatteredimage.shape != self.transmission_image.shape:
                raise IndexError("Scattered light image must have the same shape as the transmission image: {}".format(self.transmission_image.shape))

            signalimage = signalimage + scatteredimage*self.params['simSignals']['scatteredscale']

        # CONSTANT BACKGROUND - multiply by transmission image
        signalimage = signalimage + self.params['simSignals']['bkgdrate']

        # Save the final rate image of added signals
        if self.params['Output']['save_intermediates'] is True:
            rateImageName = self.basename + '_AddedSources_adu_per_sec.fits'
            self.saveSingleFits(signalimage, rateImageName)
            self.logger.info("Signal rate image of all added sources saved as {}".format(rateImageName))

        return signalimage, segmentation_map

    @staticmethod
    def add_segmentation_maps(map1, map2):
        """Add two segmentation maps together. In the case of overlapping
        objects, the object in map1 is kept and the object in map2 is
        ignored.

        Parameters
        ----------
        map1 : numpy.ndarray
            2D segmentation map

        map2 : numpy.ndarray
            2D segmentation map

        Returns
        -------
        combined : numpy.ndarray
            Summed segmentation map
        """
        map1_zeros = map1 == 0
        combined = copy.deepcopy(map1)
        combined[map1_zeros] += map2[map1_zeros]
        return combined

    def get_point_source_list(self, filename, source_type='pointsources', segment_offset=None):
        """Read in the list of point sources to add, calculate positions
        on the detector, filter out sources outside the detector, and
        calculate countrates corresponding to the given magnitudes

        Parameters
        ----------
        filename : str
            Name of catalog file to examine

        source_type : str
            Flag specifying exactly what type of catalog is being read.
            This is because there are small differences between catalog
            types. Options are ``pointsources`` which is the default,
            ``ts_imaging`` for time series, and ``ts_grism`` for grism
            time series.

        Returns
        -------
        pointSourceList : astropy.table.Table
            Table containing source information

        ghosts_from_ptsrc : str
            Name of a Mirage-formatted extended source catalog containing
            ghost sources associated with the point sources in ```pointSourceList```
        """
        # Make sure that a valid PSF path has been provided
        if not os.path.isdir(self.params['simSignals']['psfpath']):
            raise ValueError('Invalid PSF path provided in YAML:',
                             self.params['simSignals']['psfpath'])

        pointSourceList = Table(names=('index', 'pixelx', 'pixely', 'RA', 'Dec', 'RA_degrees',
                                       'Dec_degrees', 'magnitude', 'countrate_e/s',
                                       'counts_per_frame_e', 'lightcurve_file'),
                                dtype=('i', 'f', 'f', 'S14', 'S14', 'f', 'f', 'f', 'f', 'f', 'S50'))

        try:
            lines, pixelflag, magsys = self.read_point_source_file(filename)
            if pixelflag:
                self.logger.info("Point source list input positions assumed to be in units of pixels.")
            else:
                self.logger.info("Point list input positions assumed to be in units of RA and Dec.")
        except:
            raise NameError("WARNING: Unable to open the point source list file {}".format(filename))

        # Create table of point source countrate versus psf size
        if self.add_psf_wings is True:
            self.translate_psf_table(magsys)

        # File to save adjusted point source locations
        psfile = self.params['Output']['file'][0:-5] + '_{}.list'.format(source_type)
        pslist = open(psfile, 'w')

        # Get source index numbers
        indexes = lines['index']

        dtor = math.radians(1.)
        nx = (self.subarray_bounds[2] - self.subarray_bounds[0]) + 1
        ny = (self.subarray_bounds[3] - self.subarray_bounds[1]) + 1
        xc = (self.subarray_bounds[2] + self.subarray_bounds[0]) / 2.
        yc = (self.subarray_bounds[3] + self.subarray_bounds[1]) / 2.

        # Define the min and max source locations (in pixels) that fall onto the subarray
        # Include the effects of a requested grism_direct image, and also keep sources that
        # will only partially fall on the subarray
        # pixel coords here can still be negative and kept if the grism image is being made

        # First, coord limits for just the subarray
        miny = 0
        maxy = self.subarray_bounds[3] - self.subarray_bounds[1]
        minx = 0
        maxx = self.subarray_bounds[2] - self.subarray_bounds[0]

        # Expand the limits if a grism direct image is being made
        if (self.params['Output']['grism_source_image'] == True) or (self.params['Inst']['mode'] in ["pom", "wfss"]):
            transmission_ydim, transmission_xdim = self.transmission_image.shape
            miny = miny - self.subarray_bounds[1] - self.trans_ff_ymin
            minx = minx - self.subarray_bounds[0] - self.trans_ff_xmin
            maxx = minx + transmission_xdim
            maxy = miny + transmission_ydim
            nx = transmission_xdim
            ny = transmission_ydim

        # Write out the RA and Dec of the field center to the output file
        # Also write out column headers to prepare for source list
        pslist.write(("# Field center (degrees): %13.8f %14.8f y axis rotation angle "
                      "(degrees): %f  image size: %4.4d %4.4d\n" %
                      (self.ra, self.dec, self.params['Telescope']['rotation'], nx, ny)))
        pslist.write('#\n')
        pslist.write(("#    Index   RA_(hh:mm:ss)   DEC_(dd:mm:ss)   RA_degrees      "
                      "DEC_degrees     pixel_x   pixel_y    magnitude   counts/sec    counts/frame    TSO_lightcurve_catalog\n"))

        # If creating a segment-wise simulation, shift all of the RAs/Decs in
        # the list by the given offset
        if segment_offset is not None:
            lines = self.shift_sources_by_offset(lines, segment_offset, pixelflag)

        # Check the source list and remove any sources that are well outside the
        # field of view of the detector. These sources cause the coordinate
        # conversion to hang.
        self.logger.info('Filtering point sources to keep only those on the detector')
        indexes, lines = self.remove_outside_fov_sources(indexes, lines, pixelflag, 4096)

        # Determine the name of the column to use for source magnitudes
        mag_column = self.select_magnitude_column(lines, filename)

        # For NIRISS observations where ghosts will be added, create a table to hold
        # the ghost entries
        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            self.logger.info("Creating a source list of optical ghosts from point sources.")
            ghost_source_index = []  # Maps index number of original source to ghost source
            ghost_x = []
            ghost_y = []
            ghost_filename = []
            ghost_mag = []
            ghost_mags = None
        else:
            ghost_x = None

        skipped_non_niriss = False
        ghost_i = 0
        for index, values in zip(indexes, lines):
            # If the filter/pupil pair are not in the ghost summary file, log that only
            # for the first source, so that it's not repeated for all sources.
            if ghost_i == 0:
                log_ghost_err = True
            else:
                log_ghost_err = False

            pixelx, pixely, ra, dec, ra_str, dec_str = self.get_positions(values['x_or_RA'],
                                                                          values['y_or_Dec'],
                                                                          pixelflag, 4096)

            # Get the input magnitude and countrate of the point source
            mag = float(values[mag_column])
            countrate = utils.magnitude_to_countrate(self.instrument, self.params['Readout']['filter'],
                                                     magsys, mag, photfnu=self.photfnu, photflam=self.photflam,
                                                     vegamag_zeropoint=self.vegazeropoint)

            # If this is a NIRISS simulation and the user wants to add ghosts,
            # do that here.
            if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
                gx, gy, gmag, gcounts, gfile = self.locate_ghost(pixelx, pixely, countrate, magsys, values, 'point_source',
                                                                 log_skipped_filters=log_ghost_err)
                if np.isfinite(gx) and gfile is not None:
                    ghost_source_index.append(index)
                    ghost_x.append(gx)
                    ghost_y.append(gy)
                    ghost_mag.append(gmag)
                    ghost_filename.append(gfile)

                    ghost_src, skipped_non_niriss = source_mags_to_ghost_mags(values, self.params['Reffiles']['flux_cal'],
                                                                              magsys, NIRISS_GHOST_GAP_FILE, self.params['Readout']['filter'], log_skipped_filters=False)

                    if ghost_mags is None:
                        ghost_mags = copy.deepcopy(ghost_src)
                    else:
                        ghost_mags = vstack([ghost_mags, ghost_src])

                # Increment the counter to control the logging regardless of whether the source
                # is on the detector or not.
                ghost_i += 1

            psf_len = self.find_psf_size(countrate)
            edgex = int(psf_len // 2)
            edgey = int(psf_len // 2)

            if pixely > (miny-edgey) and pixely < (maxy+edgey) and pixelx > (minx-edgex) and pixelx < (maxx+edgex):
                # set up an entry for the output table
                entry = [index, pixelx, pixely, ra_str, dec_str, ra, dec, mag]

                # Calculate the countrate for the source
                framecounts = countrate * self.frametime

                # add the countrate and the counts per frame to pointSourceList
                # since they will be used in future calculations
                entry.append(countrate)
                entry.append(framecounts)

                # Add the TSO catalog name if present
                if source_type == 'ts_imaging':
                    tso_catalog = values['lightcurve_file']
                else:
                    tso_catalog = 'None'
                entry.append(tso_catalog)

                # add the good point source, including location and counts, to the pointSourceList
                pointSourceList.add_row(entry)

                # write out positions, distances, and counts to the output file
                pslist.write("%i %s %s %14.8f %14.8f %9.3f %9.3f  %9.3f  %13.6e   %13.6e  %s\n" %
                             (index, ra_str, dec_str, ra, dec, pixelx, pixely, mag, countrate, framecounts, tso_catalog))

        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts'] and skipped_non_niriss:
            self.logger.info("Skipped the calculation of ghost source magnitudes for the non-NIRISS magnitude columns in {}".format(filename))

        self.n_pointsources = len(pointSourceList)
        if self.n_pointsources > 0:
            self.logger.info("Number of point sources found within or close to the requested aperture: {}".format(self.n_pointsources))
        else:
            self.logger.info("\nNo point sources present on the detector.")

        # close the output file
        pslist.close()

        # If any ghost sources were found, create an extended catalog object to hold them
        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            ghosts_from_ptsrc = self.save_ghost_catalog(ghost_x, ghost_y, ghost_filename, ghost_mags, filename, ghost_source_index)
        else:
            ghosts_from_ptsrc = None

        return pointSourceList, ghosts_from_ptsrc

    def translate_psf_table(self, magnitude_system):
        """Given a magnitude system, translate the table of PSF sizes
        versus magnitudes into PSF sizes versus countrates

        Parameters
        ----------
        magnitude_system : str
            Magnitude system of the sources: 'abmag', 'stmag', 'vegamag'
        """
        magnitudes = self.psf_wing_sizes[magnitude_system].data

        # Place table in order of ascending magnitudes
        sort = np.argsort(magnitudes)
        for colname in self.psf_wing_sizes.colnames:
            self.psf_wing_sizes[colname] = self.psf_wing_sizes[colname][sort]
        magnitudes = self.psf_wing_sizes[magnitude_system].data

        # Calculate corresponding countrates
        countrates = utils.magnitude_to_countrate(self.instrument, self.params['Readout']['filter'],
                                                  magnitude_system, magnitudes, photfnu=self.photfnu,
                                                  photflam=self.photflam,
                                                  vegamag_zeropoint=self.vegazeropoint)
        self.psf_wing_sizes['countrate'] = countrates

    def find_psf_size(self, countrate):
        """Determine the dimentions of the PSF to use based on an object's
        countrate.

        Parameters
        ----------
        countrate : float
            Source countrate

        Returns
        -------
        xdim : int
            Size of PSF in pixels in the x direction

        ydim : int
            Size of PSF in pixels in the y direction
        """
        if self.add_psf_wings is False:
            return self.psf_library_core_x_dim

        brighter = np.where(countrate >= self.psf_wing_sizes['countrate'])[0]
        if len(brighter) == 0:
            # Dimmest bin == size of pf library
            dimension = self.psf_library_core_x_dim
            #dim = np.max(self.psf_wing_sizes['number_of_pixels'])
        else:
            dimension = self.psf_wing_sizes['number_of_pixels'][brighter[0]]
        return dimension

    def shift_sources_by_offset(self, lines, segment_offset, pixelflag):
        self.logger.info('    Shifting point source locations by arcsecond offset {}'.format(segment_offset))

        shifted_lines = lines.copy()
        shifted_lines.remove_rows(np.arange(0, len(shifted_lines)))

        V2ref_arcsec = self.siaf.V2Ref
        V3ref_arcsec = self.siaf.V3Ref
        position_angle = self.params['Telescope']['rotation']
        self.logger.info(' Position angle = {}'.format(position_angle))
        attitude_ref = pysiaf.utils.rotations.attitude(V2ref_arcsec, V3ref_arcsec, self.ra, self.dec, position_angle)

        # Shift every source by the appropriate offset
        x_displacement_arcsec, y_displacement_arcsec = segment_offset
        for line in lines:
            x_or_RA, y_or_Dec = line['x_or_RA', 'y_or_Dec']
            # Convert the input source locations to V2/V3
            if not pixelflag:
                # Convert RA/Dec (sky frame) to V2/V3 (telescope frame)
                v2, v3 = pysiaf.utils.rotations.getv2v3(attitude_ref, x_or_RA, y_or_Dec)
            else:
                # Convert X/Y (detector frame) to V2/V3 (telescope frame)
                v2, v3 = self.siaf.det_to_tel(x_or_RA, y_or_Dec)

            # Add the arcsecond displacement to each V2/V3 source position
            v2 -= x_displacement_arcsec
            v3 += y_displacement_arcsec

            # Translate back to RA/Dec and save
            ra, dec = pysiaf.utils.rotations.pointing(attitude_ref, v2, v3)

            mag_cols = [col for col in line.colnames if 'magnitude' in col]
            collist = [line['index']]
            collist.extend([ra, dec])
            collist.extend(line[mag_cols])
            shifted_lines.add_row(collist)

        return shifted_lines

    def remove_outside_fov_sources(self, index, source, pixflag, delta_pixels):
        """Filter out entries in the source catalog that are located well outside the field of
        view of the detector. This can be a fairly rough cut. We just need to remove sources
        that are very far from the detector.

        Parameters:
        -----------
        index : list
            List of index numbers corresponding to the sources

        source : Table
            astropy Table containing catalog information

        pixflag : bool
            Flag indicating whether catalog positions are given in units of
            pixels (True), or RA, Dec (False)

        delta_pixels : int
            Number of columns/rows outside of the nominal detector size (2048x2048)
            to keep sources in the source list. (e.g. delta_pixels=2048 will keep all
            sources located at -2048 to 4096.)

        Returns:
        --------
        index : list
            List of filtered index numbers corresponding to sources
            within or close to the field of view

        source : Table
            astropy Table containing filtered list of sources
        """
        catalog_x = source['x_or_RA']
        catalog_y = source['y_or_Dec']

        if pixflag:
            minx = 0 - delta_pixels
            maxx = self.output_dims[1] + delta_pixels
            miny = 0 - delta_pixels
            maxy = self.output_dims[0] + delta_pixels
            good = ((catalog_x > minx) & (catalog_x < maxx) & (catalog_y > miny) & (catalog_y < maxy))
        else:
            delta_degrees = (delta_pixels * self.siaf.XSciScale) / 3600. * u.deg
            reference = SkyCoord(ra=self.ra * u.deg, dec=self.dec * u.deg)

            # Need to determine the units of the RA values.
            # Dec units should always be degrees whether or not they are in decimal
            # or DD:MM:SS or DDd:MMm:SSs formats.
            dec_unit = u.deg
            try:
                # if it can be converted to a float, assume decimal degrees
                entry0 = float(catalog_x[0])
                ra_unit = u.deg
            except ValueError:
                # if it cannot be converted to a float, then the unit is 'hour'
                ra_unit = 'hour'

            # Temporarily replace any input positions that are None or N/A
            # with dummy values. This will allow users to supply an ephemeris
            # file and not have to add in RA, Dec numbers, which could be
            # confusing and which are ignored by Mirage anyway.
            allowed_dummy_values = ['none', 'n/a']
            for i, row in enumerate(source):
                #if isinstance(row['x_or_RA'], str) and isinstance(row['y_or_Dec'], str):
                if row['x_or_RA'] == np.nan or row['y_or_Dec'] == np.nan:
                    if 'ephemeris_file' in row.colnames:
                        if row['ephemeris_file'].lower != 'none':
                            catalog_x[i] = 0.
                            catalog_y[i] = 0.
                        else:
                            raise ValueError('Source catalog contains x, y or RA, Dec positions that are not numbers.')
                    else:
                        raise ValueError('Source catalog contains x, y or RA, Dec positions that are not numbers.')

            # Assume that units are consisent within each column. (i.e. no mixing of
            # 12h:23m:34.5s and 189.87463 degrees within a column)
            catalog = SkyCoord(ra=catalog_x, dec=catalog_y, unit=(ra_unit, dec_unit))
            good = np.where(reference.separation(catalog) < delta_degrees)[0]

            # If an ephemeris column is present, mark any rows that contain
            # an ephemeris file as good. Regardless of the RA, Dec values in
            # the catalog at this point, the true RA, Dec values will be calculated
            # from the ephemeris
            if 'ephemeris_file' in source.colnames:
                good_eph = np.array([i for i, row in enumerate(source) if row['ephemeris_file'].lower() != 'none'])
                good = np.array(list(set(np.append(good, good_eph))))
                good = [int(ele) for ele in good]

        filtered_sources = source[good]
        filtered_indexes = index[good]

        return filtered_indexes, filtered_sources

    def make_point_source_image(self, pointSources, segment_number=None, ptsrc_segmap=None):
        """Create a seed image containing all of the point sources
        provided by the source catalog

        Parameters
        ----------
        pointSources : astropy.table.Table
            Table of point sources
        segment_number : int, optional
            The number of the mirror segment to make an image for
        ptsrc_segmap : optional
            The point source segmentation map to keep adding to

        Returns
        -------
        psfimage : numpy.ndarray
            2D array containing the seed image with point sources

        seg.segmap : numpy.ndarray
            2D array containing the segmentation map that
            corresponds to ``psfimage``
        """
        dims = np.array(self.nominal_dims)

        # Create the empty image
        psfimage = np.zeros(self.output_dims)

        # Create empty seed cube for possible WFSS dispersion
        seed_cube = {}

        if ptsrc_segmap is None:
            # Create empty segmentation map
            ptsrc_segmap = segmap.SegMap()
            ptsrc_segmap.ydim, ptsrc_segmap.xdim = self.output_dims
            ptsrc_segmap.initialize_map()

        # Loop over the entries in the point source list
        for i, entry in enumerate(pointSources):
            # Start timer
            self.timer.start()

            # Find the PSF size to use based on the countrate
            psf_x_dim = self.find_psf_size(entry['countrate_e/s'])

            # Assume same PSF size in x and y
            psf_y_dim = psf_x_dim

            scaled_psf, _, _, min_x, min_y, wings_added = self.create_psf_stamp(
                entry['pixelx'], entry['pixely'], psf_x_dim, psf_y_dim,
                segment_number=segment_number, ignore_detector=True
            )

            # Skip sources that fall completely off the detector
            if scaled_psf is None:
                self.timer.stop()
                continue

            scaled_psf *= entry['countrate_e/s']

            # PSF may not be centered in array now if part of the array falls
            # off of the aperture
            stamp_x_loc = psf_x_dim // 2 - min_x
            stamp_y_loc = psf_y_dim // 2 - min_y
            updated_psf_dimensions = scaled_psf.shape

            # If the source subpixel location is beyond 0.5 (i.e. the edge
            # of the pixel), then we shift the wing->core offset by 1.
            # We also need to shift the location of the wing array on the
            # detector by 1
            if wings_added:
                x_delta = int(np.modf(entry['pixelx'])[0] > 0.5)
                y_delta = int(np.modf(entry['pixely'])[0] > 0.5)
            else:
                x_delta = 0
                y_delta = 0

            # Get the coordinates that describe the overlap between the
            # PSF image and the output aperture
            xap, yap, xpts, ypts, (i1, i2), (j1, j2), (k1, k2), \
                (l1, l2) = self.create_psf_stamp_coords(entry['pixelx']+x_delta, entry['pixely']+y_delta,
                                                        updated_psf_dimensions,
                                                        stamp_x_loc, stamp_y_loc,
                                                        coord_sys='aperture')

            # Skip sources that fall completely off the detector
            if None in [i1, i2, j1, j2, k1, k2, l1, l2]:
                self.timer.stop()
                continue

            self.logger.info("******************************* %s" % (self.basename))

            try:
                psf_to_add = scaled_psf[l1:l2, k1:k2]
                psfimage[j1:j2, i1:i2] += psf_to_add

                # Add source to segmentation map
                ptsrc_segmap.add_object_threshold(psf_to_add, j1, i1, entry['index'], self.segmentation_threshold)

                if self.params['Inst']['mode'] in DISPERSED_MODES:
                    # Add source to seed cube file
                    stamp = np.zeros(psf_to_add.shape)
                    flag = psf_to_add >= self.segmentation_threshold
                    stamp[flag] = entry['index']
                    seed_cube[entry['index']] = [i1, j1, psf_to_add*1, stamp*1]
            except IndexError:
                # In here we catch sources that are off the edge
                # of the detector. These may not necessarily be caught in
                # getpointsourcelist because if the PSF is not centered
                # in the webbpsf stamp, then the area to be pulled from
                # the stamp may shift off of the detector.
                pass

            # Stop timer
            self.timer.stop(name='ptsrc_{}'.format(str(i).zfill(6)))

            # If there are more than 100 point sources, provide an estimate of processing time
            if len(pointSources) > 100:
                if ((i == 20) or ((i > 0) and (np.mod(i, 100) == 0))):
                    time_per_ptsrc = self.timer.sum(key_str='ptsrc_') / (i+1)
                    estimated_remaining_time = time_per_ptsrc * (len(pointSources) - (i+1)) * u.second
                    time_remaining = np.around(estimated_remaining_time.to(u.minute).value, decimals=2)
                    finish_time = datetime.datetime.now() + datetime.timedelta(minutes=time_remaining)
                    self.logger.info(('Working on source #{}. Estimated time remaining to add all point sources to the stamp image: {} minutes. '
                                      'Projected finish time: {}'.format(i, time_remaining, finish_time)))

        if self.params['Inst']['mode'] in DISPERSED_MODES:
            # Save the seed cube file of point sources
            pickle.dump(seed_cube, open("%s_star_seed_cube.pickle" % (self.basename), "wb"), protocol=pickle.HIGHEST_PROTOCOL)

        return psfimage, ptsrc_segmap

    def create_psf_stamp(self, x_location, y_location, psf_dim_x, psf_dim_y,
                         ignore_detector=False, segment_number=None):
        """From the gridded PSF model, location within the aperture, and
        dimensions of the stamp image (either the library PSF image, or
        the galaxy/extended stamp image with which the PSF will be
        convolved), evaluate the GriddedPSFModel at
        the appropriate location on the detector and return the PSF stamp

        Parameters
        ----------
        x_location : float
            X-coordinate of the PSF in the coordinate system of the
            aperture being simulated.

        y_location : float
            Y-coordinate of the PSF in the coordinate system of the
            aperture being simulated.

        psf_dim_x : int
            Number of columns of the array containing the PSF

        psf_dim_y : int
            Number of rows of the array containing the PSF

        ignore_detector : bool
            If True, the returned coordinates can have values outside the
            size of the subarray/detector (i.e. coords can be negative or
            larger than full frame). If False, coordinates are constrained
            to be on the detector.

        Returns
        -------
        full_psf : numpy.ndarray
            2D array containing the normalized PSF image. Total signal should
            be close to 1.0 (not exactly 1.0 due to asymmetries and distortion)
            Array will be cropped based on how much falls on or off the detector

        k1 : int
            Column number on the PSF/stamp image corresponding to the left-most
            column that overlaps the detector/aperture

        l1 : int
            row number on the PSF/stamp image corresponding to the bottom-most
            row that overlaps the detector/aperture

        add_wings : bool
            Whether or not PSF wings are to be added to the PSF core
        """
        # PSF will always be centered
        psf_x_loc = psf_dim_x // 2
        psf_y_loc = psf_dim_y // 2

        # Translation needed to go from PSF core (self.psf_library)
        # coordinate system to the PSF wing coordinate system (i.e.
        # center the PSF core in the wing image)
        psf_wing_half_width_x = int(psf_dim_x // 2)
        psf_wing_half_width_y = int(psf_dim_y // 2)
        psf_core_half_width_x = int(self.psf_library_core_x_dim // 2)
        psf_core_half_width_y = int(self.psf_library_core_y_dim // 2)
        delta_core_to_wing_x = psf_wing_half_width_x - psf_core_half_width_x
        delta_core_to_wing_y = psf_wing_half_width_y - psf_core_half_width_y

        # This assumes a square PSF shape!!!!
        # If no wings are to be added, then we can skip all the wing-
        # and pixel phase-related work below.
        if ((self.add_psf_wings is False) or (delta_core_to_wing_x <= 0)):
            add_wings = False

            if segment_number is not None:
                library = self.psf_library[segment_number - 1]
            else:
                library = self.psf_library

            # Get coordinates decribing overlap between the evaluated psf
            # core and the full frame of the detector. We really only need
            # the xpts_core and ypts_core from this in order to know how
            # to evaluate the library
            # Note that we don't care about the pixel phase here.

            psf_core_dims = (self.psf_library_core_y_dim, self.psf_library_core_x_dim)
            xc_core, yc_core, xpts_core, ypts_core, (i1c, i2c), (j1c, j2c), (k1c, k2c), \
                (l1c, l2c) = self.create_psf_stamp_coords(x_location, y_location, psf_core_dims,
                                                          psf_core_half_width_x, psf_core_half_width_y,
                                                          coord_sys='full_frame',
                                                          ignore_detector=ignore_detector)

            # Skip sources that fall completely off the detector
            if None in [i1c, i2c, j1c, j2c, k1c, k2c, l1c, l2c]:
                return None, None, None, False

            # Step 4
            full_psf = library.evaluate(x=xpts_core, y=ypts_core, flux=1.0,
                                        x_0=xc_core, y_0=yc_core)
            k1 = k1c
            l1 = l1c

            i1 = i1c
            j1 = j1c

        else:
            add_wings = True
            # If the source subpixel location is beyond 0.5 (i.e. the edge
            # of the pixel), then we shift the wing->core offset by 1.
            # We also need to shift the location of the wing array on the
            # detector by 1
            x_phase = np.modf(x_location)[0]
            y_phase = np.modf(y_location)[0]
            x_location_delta = int(x_phase > 0.5)
            y_location_delta = int(y_phase > 0.5)
            if x_phase > 0.5:
                delta_core_to_wing_x -= 1
            if y_phase > 0.5:
                delta_core_to_wing_y -= 1

            # offset_x, and y below will not change because that is
            # the offset between the full wing array and the user-specified
            # wing array size

            # Get the psf wings array - first the nominal size
            # Later we may crop if the source is only partially on the detector
            full_wing_y_dim, full_wing_x_dim = self.psf_wings.shape
            offset_x = int((full_wing_x_dim - psf_dim_x) / 2)
            offset_y = int((full_wing_y_dim - psf_dim_y) / 2)

            full_psf = copy.deepcopy(self.psf_wings[offset_y:offset_y+psf_dim_y, offset_x:offset_x+psf_dim_x])

            # Get coordinates describing overlap between PSF image and the
            # full frame of the detector
            # Step 1
            xcenter, ycenter, xpts, ypts, (i1, i2), (j1, j2), (k1, k2), \
                (l1, l2) = self.create_psf_stamp_coords(x_location+x_location_delta, y_location+y_location_delta,
                                                        (psf_dim_y, psf_dim_x), psf_x_loc, psf_y_loc,
                                                        coord_sys='full_frame', ignore_detector=ignore_detector)

            if None in [i1, i2, j1, j2, k1, k2, l1, l2]:
                return None, None, None, False

            # Step 2
            # If the core of the psf lands at least partially on the detector
            # then we need to evaluate the psf library
            if ((k1 < (psf_wing_half_width_x + psf_core_half_width_x)) and
               (k2 > (psf_wing_half_width_x - psf_core_half_width_x)) and
               (l1 < (psf_wing_half_width_y + psf_core_half_width_y)) and
               (l2 > (psf_wing_half_width_y - psf_core_half_width_y))):

                # Step 3
                # Get coordinates decribing overlap between the evaluated psf
                # core and the full frame of the detector. We really only need
                # the xpts_core and ypts_core from this in order to know how
                # to evaluate the library
                # Note that we don't care about the pixel phase here.
                psf_core_dims = (self.psf_library_core_y_dim, self.psf_library_core_x_dim)
                xc_core, yc_core, xpts_core, ypts_core, (i1c, i2c), (j1c, j2c), (k1c, k2c), \
                    (l1c, l2c) = self.create_psf_stamp_coords(x_location, y_location, psf_core_dims,
                                                              psf_core_half_width_x, psf_core_half_width_y,
                                                              coord_sys='full_frame', ignore_detector=ignore_detector)

                if None in [i1c, i2c, j1c, j2c, k1c, k2c, l1c, l2c]:
                    return None, None, None, False

                # Step 4
                psf = self.psf_library.evaluate(x=xpts_core, y=ypts_core, flux=1.,
                                                x_0=xc_core, y_0=yc_core)

                # Step 5
                wing_start_x = k1c + delta_core_to_wing_x
                wing_end_x = k2c + delta_core_to_wing_x
                wing_start_y = l1c + delta_core_to_wing_y
                wing_end_y = l2c + delta_core_to_wing_y

                full_psf[wing_start_y:wing_end_y, wing_start_x:wing_end_x] = psf

            # Whether or not the core is on the detector, crop the PSF
            # to the proper shape based on how much is on the detector
            full_psf = full_psf[l1:l2, k1:k2]

        return full_psf, i1, j1, k1, l1, add_wings

    def create_psf_stamp_coords(self, aperture_x, aperture_y, stamp_dims, stamp_x, stamp_y,
                                coord_sys='full_frame', ignore_detector=False):
        """Calculate the coordinates in the aperture coordinate system
        where the stamp image wil be placed based on the location of the
        stamp image in the aperture and the size of the stamp image.

        Parameters
        ----------
        aperture_x : float
            X-coordinate of the PSF in the coordinate system of the
            aperture being simulated.

        aperture_y : float
            Y-coordinate of the PSF in the coordinate system of the
            aperture being simulated.

        stamp_dims : tup
            (x, y) dimensions of the stamp image that will be placed
            into the final seed image. This stamp image can be either the
            PSF image itself, or the stamp image of the galaxy/extended
            source that the PSF is going to be convolved with.

        stamp_x : float
            Location in x of source within the stamp image

        stamp_y : float
            Location in y of source within the stamp image

        coord_sys : str
            Inidicates which coordinate system to return coordinates for.
            Options are 'full_frame' for full frame coordinates, or
            'aperture' for aperture coordinates (including any expansion
            for grism source image)

        ignore_detector : bool
            If True, the returned coordinates can have values outside the
            size of the subarray/detector (i.e. coords can be negative or
            larger than full frame). If False, coordinates are constrained
            to be on the detector.

        Returns
        -------
        x_points : numpy.ndarray
            2D array of x-coordinates in the aperture coordinate system
            where the stamp image will fall.

        y_points : numpy.ndarray
            2D array of y-coordinates in the aperture coordinate system
            where the stamp image will fall.

        (i1, i2) : tup
            Beginning and ending x coordinates (in the aperture coordinate
            system) where the stamp image will fall

        (j1, j2) : tup
            Beginning and ending y coordinates (in the aperture coordinate
            system) where the stamp image will fall

        (k1, k2) : tup
            Beginning and ending x coordinates (in the stamp's coordinate
            system) that overlap the aperture

        (l1, l2) : tup
            Beginning and ending y coordinates (in the stamp's coordinate
            system) that overlap the aperture
        """
        if coord_sys == 'full_frame':
            xpos = aperture_x + self.subarray_bounds[0]
            ypos = aperture_y + self.subarray_bounds[1]
            out_dims_x = self.ffsize
            out_dims_y = self.ffsize
        elif coord_sys == 'aperture':
            xpos = aperture_x + self.coord_adjust['xoffset']
            ypos = aperture_y + self.coord_adjust['yoffset']
            out_dims_x = self.output_dims[1]
            out_dims_y = self.output_dims[0]

        stamp_y_dim, stamp_x_dim = stamp_dims

        # Get coordinates that describe the overlap between the stamp
        # and the aperture
        (i1, i2, j1, j2, k1, k2, l1, l2) = self.cropped_coords(xpos, ypos, (out_dims_y, out_dims_x),
                                                               stamp_x, stamp_y, stamp_dims,
                                                               ignore_detector=ignore_detector)

        # If the stamp is completely off the detector, use dummy arrays
        # for x_points and y_points
        if j1 is None or j2 is None or i1 is None or i2 is None:
            x_points = np.zeros((2, 2))
            y_points = x_points
        else:
            y_points, x_points = np.mgrid[j1:j2, i1:i2]

        return xpos, ypos, x_points, y_points, (i1, i2), (j1, j2), (k1, k2), (l1, l2)

    def ensure_odd_lengths(self, x_dim, y_dim, x_center, y_center):
        """Given the dimensions and the coordinates of the center of an
        array, ensure the array has an odd number of rows and columns,
        calculate the updated half-width, and return the minimum and
        maximum row and column indexes.

        Parameters
        ----------
        x_dim : int
            Length of the array in the x-dimension

        y_dim : int
            Length of the array in the y-dimension

        x_center : float
            Coordinate of the center of the array, usually
            in some other coordinate system (e.g. full frame
            coords, while the array is a subarray)

        y_center : float
            Coordinate of the center of the array in the y
            direction, usually in some other coordinate
            system

        Returns
        -------
        x_min : int
            Minimum index in the x direction of the array
            in the coordinate system of ``x_center, y_center``.

        x_max : int
            Maximum index in the x direction of the array
            in the coordinate system of ``x_center, y_center``.

        y_min : int
            Minimum index in the y direction of the array
            in the coordinate system of ``x_center, y_center``.

        y_max : int
            Maximum index in the y direction of the array
            in the coordinate system of ``x_center, y_center``.
        """
        if x_dim % 2 == 0:
            x_dim -= 1
        if y_dim % 2 == 0:
            y_dim -= 1
        x_half_width = x_dim // 2
        y_half_width = y_dim // 2
        x_min = int(x_center) - x_half_width
        x_max = int(x_center) + x_half_width + 1
        y_min = int(y_center) - y_half_width
        y_max = int(y_center) + y_half_width + 1
        return x_min, x_max, y_min, y_max

    def cropped_coords(self, aperture_x, aperture_y, aperture_dims, stamp_x, stamp_y, stamp_dims,
                       ignore_detector=False):
        """Given the location of a source on the detector along with the size of
        the PSF/stamp image for that object, calcuate the limits of the detector
        coordinates onto which the object will fall.

        Parameters:
        -----------
        aperture_x : float
            Column location of source on detector (aperture coordinate system
            including any padding for WFSS seed image)

        aperture_y : float
            Row location of source on detector (aperture coordinate system
            including any padding for WFSS seed image)

        aperture_dims : tup
            (y, x) dimensions of the aperture on which the sources will be placed
            (e.g. full frame, full_frame+extra, subarray)

        stamp_x : float
            Location in x of source within the stamp image

        stamp_y : float
            Location in y of source within the stamp image

        stamp_dims : tup
            (y, x) dimensions of the source's stamp image. (e.g. the size of the PSF
            or galaxy image stamp)

        ignore_detector: bool
            If True, the returned coordinates can have values outside the
            size of the subarray/detector (i.e. coords can be negative or
            larger than full frame). If False, coordinates are constrained
            to be on the detector.

        Returns:
        --------
        i1 : int
            Column number on the detector/aperture corresponding to the left
            edge of the PSF/stamp image.

        i2 : int
            Column number on the detector/aperture corresponding to the right
            edge of the PSF/stamp image.

        j1 : int
            Row number on the detector/aperture corresponding to the bottom
            edge of the PSF/stamp image.

        j2 : int
            Row number on the detector/aperture corresponding to the top
            edge of the PSF/stamp image.

        l1 : int
            Column number on the PSF/stamp image corresponding to the left-most
            column that overlaps the detector/aperture

        l2 : int
            Column number on the PSF/stamp image corresponding to the right-most
            column that overlaps the detector/aperture

        k1 : int
            Row number on the PSF/stamp image corresponding to the bottom-most
            row that overlaps the detector/aperture

        k2 : int
            Row number on the PSF/stamp image corresponding to the top-most
            row that overlaps the detector/aperture
        """
        stamp_y_dim, stamp_x_dim = stamp_dims
        aperture_y_dim, aperture_x_dim = aperture_dims

        stamp_x = math.floor(stamp_x)
        stamp_y = math.floor(stamp_y)
        aperture_x = math.floor(aperture_x)
        aperture_y = math.floor(aperture_y)

        i1 = aperture_x - stamp_x
        j1 = aperture_y - stamp_y

        if ((i1 > (aperture_x_dim)) or (j1 > (aperture_y_dim))) and not ignore_detector:
            # In this case the stamp does not overlap the aperture at all
            return tuple([None]*8)
        delta_i1 = 0
        delta_j1 = 0

        if not ignore_detector:
            if i1 < 0:
                delta_i1 = copy.deepcopy(i1)
                i1 = 0
            if j1 < 0:
                delta_j1 = copy.deepcopy(j1)
                j1 = 0

        i2 = i1 + (stamp_x_dim + delta_i1)
        j2 = j1 + (stamp_y_dim + delta_j1)

        if ((i2 < 0) or (j2 < 0)) and not ignore_detector:
            # Stamp does not overlap the aperture at all
            return tuple([None]*8)

        delta_i2 = 0
        delta_j2 = 0
        if not ignore_detector:
            if i2 > aperture_x_dim:
                delta_i2 = i2 - aperture_x_dim
                i2 = aperture_x_dim
            if j2 > aperture_y_dim:
                delta_j2 = j2 - aperture_y_dim
                j2 = aperture_y_dim

        k1 = 0 - delta_i1
        k2 = stamp_x_dim - delta_i2
        l1 = 0 - delta_j1
        l2 = stamp_y_dim - delta_j2
        return (i1, i2, j1, j2, k1, k2, l1, l2)

    def cropPSF(self, psf):
        '''take an array containing a psf and crop it such that the brightest
        pixel is in the center of the array'''
        nyshift, nxshift = np.where(psf == np.max(psf))
        nyshift = nyshift[0]
        nxshift = nxshift[0]
        py, px = psf.shape

        xl = nxshift - 0
        xr = px - nxshift - 1
        if xl <= xr:
            xdist = xl
        if xr < xl:
            xdist = xr

        yl = nyshift - 0
        yr = py - nyshift - 1
        if yl <= yr:
            ydist = yl
        if yr < yl:
            ydist = yr

        return psf[nyshift - ydist:nyshift + ydist + 1, nxshift - xdist:nxshift + xdist + 1]

    def read_point_source_file(self, filename):
        """Read in the point source catalog file

         Parameters:
        -----------
        filename : str
            Filename of catalog file to be read in

         Returns:
        --------
        gtab : Table
            astropy Table containing catalog

         pflag : bool
            Flag indicating units of source locations. True for detector
            pixels, False for RA, Dec

         msys : str
            Magnitude system of the source brightnesses (e.g. 'abmag')
        """
        try:
            gtab = ascii.read(filename)
            # Look at the header lines to see if inputs
            # are in units of pixels or RA, Dec
            pflag = False
            try:
                if 'position_pixels' in gtab.meta['comments'][0:4]:
                    pflag = True
            except:
                pass
            # Check to see if magnitude system is specified
            # in the comments. If not default to AB mag
            msys = 'abmag'
            condition = ('stmag' in gtab.meta['comments'][0:4]) | ('vegamag' in gtab.meta['comments'][0:4])
            if condition:
                msys = [l for l in gtab.meta['comments'][0:4] if 'mag' in l][0]
                msys = msys.lower()

        except:
            raise IOError("WARNING: Unable to open the source list file {}".format(filename))

        return gtab, pflag, msys

    def select_magnitude_column(self, catalog, catalog_file_name):
        """Select the appropriate column to use for source magnitudes from the input source catalog. If there
        is a specific column name constructed as <instrument>_<filter>_magnitude to use for source magnitudes
        (e.g. nircam_f200w_magnitude) then use that. (NOTE: for FGS we search for a column name of
        'fgs_magnitude'). If not, check for a generic 'magnitude' column. If neither are present, raise an
        error.

        Parameters
        ----------

        catalog : astropy.table.Table
            Source catalog

        catalog_file_name : str
            Name of the catalog file. Used only when raising errors.

        Returns
        -------

        specific_mag_col : str
            The name of the catalog column to use for source magnitudes
        """
        # Determine the filter name to look for
        if self.params['Inst']['instrument'].lower() == 'nircam':
            actual_pupil_name = self.params['Readout']['pupil'].lower()
            actual_filter_name = self.params['Readout']['filter'].lower()

            # If a grism is in the pupil wheel, replace it with a filter
            if actual_pupil_name.lower() in ['grismr', 'grismc']:
                actual_pupil_name = 'clear'

            comb_str = '{}_{}'.format(actual_filter_name.lower(), actual_pupil_name.lower())

            # Construct the column header to look for
            specific_mag_col = "nircam_{}_magnitude".format(comb_str)

            # In order to be backwards compatible, if the newer column
            # name format (above) is not present, look for a column name
            # that follows the old format, which uses just the filter name
            # for cases where a filter is paired with CLEAR in the pupil
            # wheel, or where only the name of the narrower filter is used
            # for cases where a narrow filter in the pupil wheel is crossed
            # with a wide filter in the filter wheel
            if specific_mag_col not in catalog.colnames:
                if actual_pupil_name == 'clear':
                    specific_mag_col = "nircam_{}_magnitude".format(actual_filter_name)
                elif actual_pupil_name[0] == 'f' and actual_filter_name[0] == 'f':
                    specific_mag_col = "nircam_{}_magnitude".format(actual_pupil_name)
                elif actual_pupil_name in ['wlp8', 'wlm8']:
                    # Weak lenses were not supported with the old column
                    # name format, so if the new format column name is not
                    # present, then we raise an exception here. While WLP4
                    # has a very small effect on throughput, WLP8 and WLM8
                    # do, so falling back to looking for a <filter>+CLEAR
                    # column seems like the wrong thing to do.
                    raise ValueError(("WARNING: Catalog {} has no magnitude column for {} specifically called {}. "
                                      "Unable to continue.".format(os.path.split(catalog_file_name)[1],
                                                                   self.params['Inst']['instrument'],
                                                                   specific_mag_col)))
        elif self.params['Inst']['instrument'].lower() == 'niriss':
            if self.params['Readout']['pupil'][0].upper() == 'F':
                specific_mag_col = "{}_{}_magnitude".format('niriss', self.params['Readout']['pupil'].lower())
            else:
                specific_mag_col = "{}_{}_magnitude".format('niriss', self.params['Readout']['filter'].lower())

        elif self.params['Inst']['instrument'].lower() == 'fgs':
            specific_mag_col = "{}_magnitude".format(self.params['Readout']['array_name'].split('_')[0].lower())

        # Search catalog column names.
        if specific_mag_col in catalog.colnames:
            self.logger.info("Using {} column in {} for magnitudes".format(specific_mag_col,
                                                                           os.path.split(catalog_file_name)[1]))
            return specific_mag_col

        elif 'magnitude' in catalog.colnames:
            self.logger.warning(("WARNING: Catalog {} does not have a magnitude column called {}, "
                                 "but does have a generic 'magnitude' column. Continuing simulation using that."
                                 .format(os.path.split(catalog_file_name)[1], specific_mag_col)))
            return "magnitude"
        else:
            raise ValueError(("WARNING: Catalog {} has no magnitude column for {} specifically called {}, "
                              "nor a generic 'magnitude' column. Unable to proceed."
                              .format(os.path.split(catalog_file_name)[1], self.params['Inst']['instrument'],
                              specific_mag_col)))

    def makePos(self, alpha1, delta1):
        # given a numerical RA/Dec pair, convert to string
        # values hh:mm:ss
        if ((alpha1 < 0) or (alpha1 >= 360.)):
            alpha1 = alpha1 % 360
        if delta1 < 0.:
            sign = "-"
            d1 = abs(delta1)
        else:
            sign = "+"
            d1 = delta1
        decd = int(d1)
        value = 60. * (d1 - float(decd))
        decm = int(value)
        decs = 60. * (value - decm)
        a1 = alpha1 / 15.0
        radeg = int(a1)
        value = 60. * (a1 - radeg)
        ramin = int(value)
        rasec = 60. * (value - ramin)
        alpha2 = "%2.2d:%2.2d:%7.4f" % (radeg, ramin, rasec)
        delta2 = "%1s%2.2d:%2.2d:%7.4f" % (sign, decd, decm, decs)
        alpha2 = alpha2.replace(" ", "0")
        delta2 = delta2.replace(" ", "0")

        return alpha2, delta2

    def RADecToXY_astrometric(self, ra, dec):
        """Translate backwards, RA, Dec to V2, V3. If a distortion reference file is
        provided, use that. Otherwise fall back to pysiaf.

        Parameters:
        -----------
        ra : float
            Right ascention value, in degrees, to be translated.

        dec : float
            Declination value, in degrees, to be translated.

        Returns:
        --------
        pixelx : float
            X coordinate value in the aperture corresponding to the input location

        pixely : float
            Y coordinate value in the aperture corresponding to the input location
        """
        if not self.use_intermediate_aperture:
            loc_v2, loc_v3 = pysiaf.utils.rotations.getv2v3(self.attitude_matrix, ra, dec)
        else:
            loc_v2, loc_v3 = pysiaf.utils.rotations.getv2v3(self.intermediate_attitude_matrix, ra, dec)

        if self.coord_transform is not None:
            # Use the distortion reference file to translate from V2, V3 to RA, Dec
            pixelx, pixely = self.coord_transform.inverse(loc_v2, loc_v3)
            pixelx -= self.subarray_bounds[0]
            pixely -= self.subarray_bounds[1]
        else:
            self.logger.debug('SIAF: using {} to transform from tel to sci'.format(self.siaf.AperName))
            pixelx, pixely = self.siaf.tel_to_sci(loc_v2, loc_v3)
            # Subtract 1 from SAIF-derived results since SIAF works in a 1-indexed coord system
            pixelx -= 1
            pixely -= 1

        return pixelx, pixely

    def object_separation(self, radec1, radec2, wcs):
        """
        Calculate the distance between two points on the sky given their
        RA, Dec values. Also calculate the angle (east of north?) between
        the two points.

        Parameters:
        -----------
        radec1 : list
            2-element list giving the RA, Dec (in decimal degrees) for
            the first object

        radec2 : list
            2-element list giving the RA, Dec (in decimal degrees) for
            the second object

        Returns:
        --------
        distance : float
            Angular separation (in degrees) between the two objects

        angle : float
            Angle (east of north?) separating the two sources
        """
        c1 = SkyCoord(radec1[0]*u.degree, radec1[1]*u.degree, frame='icrs')
        c2 = SkyCoord(radec2[0]*u.degree, radec2[1]*u.degree, frame='icrs')
        sepra, sepdec = c1.spherical_offsets_to(c2).to_pixel(wcs)
        return sepra, sepdec

    def XYToRADec(self, pixelx, pixely):
        """Translate a given x, y location on the detector to RA, Dec. If a
        distortion reference file is provided, use that. Otherwise fall back to
        using pysiaf.

        Parameters:
        -----------
        pixelx : float
            X coordinate value in the aperture

        pixely : float
            Y coordinate value in the aperture

        Returns:
        --------
        ra : float
            Right ascention value in degrees

        dec : float
            Declination value in degrees

        ra_str : str
            Right ascention value in HH:MM:SS

        dec_str : str
            Declination value in DD:MM:SS
        """
        if self.coord_transform is not None:
            pixelx += self.subarray_bounds[0]
            pixely += self.subarray_bounds[1]
            loc_v2, loc_v3 = self.coord_transform(pixelx, pixely)
        else:
            # Use SIAF to do the calculations if the distortion reffile is
            # not present. In this case, add 1 to the input pixel values
            # since SIAF works in a 1-indexed coordinate system.
            loc_v2, loc_v3 = self.siaf.sci_to_tel(pixelx + 1, pixely + 1)

        if not self.use_intermediate_aperture:
            ra, dec = pysiaf.utils.rotations.pointing(self.attitude_matrix, loc_v2, loc_v3)
        else:
            ra, dec = pysiaf.utils.rotations.pointing(self.intermediate_attitude_matrix, loc_v2, loc_v3)

        # Translate the RA/Dec floats to strings
        ra_str, dec_str = self.makePos(ra, dec)

        return ra, dec, ra_str, dec_str

    def readGalaxyFile(self, filename):
        # Read in the galaxy source list
        try:
            # read table
            gtab = ascii.read(filename)

            # Look at the header lines to see if inputs
            # are in units of pixels or RA, Dec
            pflag = False
            rpflag = False
            try:
                if 'position_pixels' in gtab.meta['comments'][0:4]:
                    pflag = True
            except:
                pass
            try:
                if 'radius_pixels' in gtab.meta['comments'][0:4]:
                    rpflag = True
            except:
                pass
            # Check to see if magnitude system is specified in the comments
            # If not assume AB mags
            msys = 'abmag'
            condition = ('stmag' in gtab.meta['comments'][0:4]) | ('vegamag' in gtab.meta['comments'][0:4])
            if condition:
                msys = [l for l in gtab.meta['comments'][0:4] if 'mag' in l][0]
                msys = msys.lower()

        except:
            raise IOError("WARNING: Unable to open the galaxy source list file {}".format(filename))

        return gtab, pflag, rpflag, msys

    def filterGalaxyList(self, galaxylist, pixelflag, radiusflag, magsystem, catfile):
        # given a list of galaxies (location, size, orientation, magnitude)
        # keep only those which will fall fully or partially on the output array

        filteredList = Table(names=('index', 'pixelx', 'pixely', 'RA', 'Dec',
                                    'RA_degrees', 'Dec_degrees', 'V2', 'V3',
                                    'radius', 'ellipticity', 'pos_angle',
                                    'sersic_index', 'magnitude', 'countrate_e/s',
                                    'counts_per_frame_e'),
                             dtype=('i', 'f', 'f', 'S14', 'S14', 'f', 'f', 'f',
                                    'f', 'f', 'f', 'f', 'f', 'f', 'f', 'f'))

        # Each entry in galaxylist is:
        # index x_or_RA  y_or_Dec  radius  ellipticity  pos_angle  sersic_index  magnitude
        # remember that x/y are interpreted as coordinates in the output subarray
        # NOT full frame coordinates. This is the same as the point source list coords

        # First, begin to define the pixel limits beyond which a galaxy will be completely
        # outside of the field of view
        # First, coord limits for just the subarray
        miny = 0
        maxy = self.subarray_bounds[3] - self.subarray_bounds[1]
        minx = 0
        maxx = self.subarray_bounds[2] - self.subarray_bounds[0]
        ny = self.subarray_bounds[3] - self.subarray_bounds[1] + 1
        nx = self.subarray_bounds[2] - self.subarray_bounds[0] + 1

        # Expand the limits if a grism direct image is being made
        if (self.params['Output']['grism_source_image'] == True) or (self.params['Inst']['mode'] in ["pom", "wfss"]):
            """
            extrapixy = int((maxy + 1)/2 * (self.grism_direct_factor_y - 1.))
            miny -= extrapixy
            maxy += extrapixy
            extrapixx = int((maxx + 1)/2 * (self.grism_direct_factor_x - 1.))
            minx -= extrapixx
            maxx += extrapixx

            nx = int(nx * self.grism_direct_factor_x)
            ny = int(ny * self.grism_direct_factor_y)

            this needs to change to be the entire POM.
            don't multiply by grism_direct_factor. that's too small.
            ff - minx, maxx = 0, 2047
            """
            transmission_ydim, transmission_xdim = self.transmission_image.shape

            miny = miny - self.subarray_bounds[1] - self.trans_ff_ymin
            minx = minx - self.subarray_bounds[0] - self.trans_ff_xmin
            maxx = minx + transmission_xdim
            maxy = miny + transmission_ydim
            nx = transmission_xdim
            ny = transmission_ydim

        # Get source index numbers
        indexes = galaxylist['index']

        # Check the source list and remove any sources that are well outside the
        # field of view of the detector. These sources cause the coordinate
        # conversion to hang.
        indexes, galaxylist = self.remove_outside_fov_sources(indexes, galaxylist, pixelflag, 4096)

        # Determine the name of the column to use for source magnitudes
        mag_column = self.select_magnitude_column(galaxylist, catfile)

        # For NIRISS observations where ghosts will be added, create a table to hold
        # the ghost entries
        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            ghost_source_index = []
            ghost_x = []
            ghost_y = []
            ghost_filename = []
            ghost_mag = []
            ghost_mags = None
        else:
            ghost_x = None

        # Loop over galaxy sources
        skipped_non_niriss = False
        ghost_i = 0
        for index, source in zip(indexes, galaxylist):

            # If the filter/pupil combination does not have an entry
            # in the ghost summary file, log that only for the first
            # source. No need to repeat for all sources.
            log_ghost_err = True
            if ghost_i > 0:
                log_ghost_err = False

            # If galaxy radii are given in units of arcseconds, translate to pixels
            if radiusflag is False:
                source['radius'] /= self.siaf.XSciScale

            # how many pixels beyond the nominal subarray edges can a source be located and
            # still have it fall partially on the subarray? Galaxy stamps are nominally set to
            # have a length and width equal to 100 times the requested radius.
            edgex = source['radius'] * 100 / 2 - 1
            edgey = source['radius'] * 100 / 2 - 1

            # reset the field of view limits for the size of the current stamp image
            outminy = miny - edgey
            outmaxy = maxy + edgey
            outminx = minx - edgex
            outmaxx = maxx + edgex

            pixelx, pixely, ra, dec, ra_str, dec_str = self.get_positions(source['x_or_RA'],
                                                                          source['y_or_Dec'],
                                                                          pixelflag, 4096)

            # Calculate count rate
            mag = float(source[mag_column])
            # Convert magnitudes to countrate (ADU/sec) and counts per frame
            rate = utils.magnitude_to_countrate(self.instrument, self.params['Readout']['filter'],
                                                magsystem, mag, photfnu=self.photfnu, photflam=self.photflam,
                                                vegamag_zeropoint=self.vegazeropoint)

            # If this is a NIRISS simulation and the user wants to add ghosts,
            # do that here.
            if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
                gx, gy, gmag, gcounts, gfile = self.locate_ghost(pixelx, pixely, rate, magsystem, source, 'galaxies',
                                                                 log_skipped_filters=log_ghost_err)
                if np.isfinite(gx) and gfile is not None:
                    ghost_source_index.append(index)
                    ghost_x.append(gx)
                    ghost_y.append(gy)
                    ghost_mag.append(gmag)
                    ghost_filename.append(gfile)

                    ghost_src, skipped_non_niriss = source_mags_to_ghost_mags(source, self.params['Reffiles']['flux_cal'], magsystem,
                                                                              NIRISS_GHOST_GAP_FILE, self.params['Readout']['filter'], log_skipped_filters=False)

                    if ghost_mags is None:
                        ghost_mags = copy.deepcopy(ghost_src)
                    else:
                        ghost_mags = vstack([ghost_mags, ghost_src])

                # Increment the counter to control the logging regardless of whether the source
                # is on the detector or not.
                ghost_i += 1

            # only keep the source if the peak will fall within the subarray
            if pixely > outminy and pixely < outmaxy and pixelx > outminx and pixelx < outmaxx:

                pixelv2, pixelv3 = pysiaf.utils.rotations.getv2v3(self.attitude_matrix, ra, dec)
                entry = [index, pixelx, pixely, ra_str, dec_str, ra, dec, pixelv2, pixelv3,
                         source['radius'], source['ellipticity'], source['pos_angle'], source['sersic_index']]

                # Now look at the input magnitude of the point source
                # append the mag and pixel position to the list of ra, dec
                entry.append(mag)
                framecounts = rate * self.frametime

                # add the countrate and the counts per frame to pointSourceList
                # since they will be used in future calculations
                entry.append(rate)
                entry.append(framecounts)

                # add the good point source, including location and counts, to the pointSourceList
                filteredList.add_row(entry)

        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts'] and skipped_non_niriss:
            self.logger.info(("Skipped the calculation of ghost source magnitudes for the non-NIRISS magnitude columns in "
                              "galaxy source catalog."))

        # Write the results to a file
        self.n_galaxies = len(filteredList)
        if self.n_galaxies > 0:
            self.logger.info(("Number of galaxies found within or close to the requested aperture: {}".format(self.n_galaxies)))
        else:
            self.logger.info("\nINFO: No galaxies present within the aperture.")

        filteredList.meta['comments'] = [("Field center (degrees): %13.8f %14.8f y axis rotation angle "
                                          "(degrees): %f  image size: %4.4d %4.4d\n" %
                                          (self.ra, self.dec, self.params['Telescope']['rotation'], nx, ny))]
        filteredOut = self.basename + '_galaxySources.list'
        filteredList.write(filteredOut, format='ascii', overwrite=True)

        # If any ghost sources were found, create an extended catalog object to hold them
        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            ghosts_from_galaxies = self.save_ghost_catalog(ghost_x, ghost_y, ghost_filename, ghost_mags, catfile, ghost_source_index)
        else:
            ghosts_from_galaxies = None

        return filteredList, ghosts_from_galaxies

    def create_galaxy(self, r_Sersic, ellipticity, sersic_index, position_angle, total_counts, subpixx, subpixy,
                      signal_matching_threshold=0.02):
        """Create a model 2d sersic image with a given radius, eccentricity,
        position angle, and total counts.

        Parameters
        ----------
        r_Sersic : float
            Half light radius of the sersic profile, in units of pixels

        ellipticity : float
            Ellipticity of sersic profile

        sersic_index : float
            Sersic index

        position_angle : float
            Position angle in units of radians

        total_counts : float
            Total summed signal of the output image

        subpixx : float
            Subpixel x-coordinate of the galaxy center.

        subpixy : float
            Subpixel y-coordinate of the galaxy center.

        signal_matching_threshold : float
            Maximum allowed fractional difference between the requested
            total signal and the total signal in the model. If the model
            signal is incorrect by more than this threshold, fall back to
            manual scaling of the galaxy stamp.

        Returns
        -------
        img : numpy.ndarray
            2D array containing the 2D sersic profile
        """
        # Calculate the total signal associated with the source
        sersic_total = sersic_total_signal(r_Sersic, sersic_index)

        # Amplitude to be input into Sersic2D to properly scale the source
        amplitude = total_counts / sersic_total

        # Find the effective radius, semi-major, and semi-minor axes sizes
        # needed to encompass SERSIC_FRACTIONAL_SIGNAL of the total flux
        sersic_rad, semi_major_axis, semi_minor_axis = sersic_fractional_radius(r_Sersic, sersic_index,
                                                                                SERSIC_FRACTIONAL_SIGNAL,
                                                                                ellipticity)

        # Using the position angle, calculate the size of the source in the
        # x and y directions
        x_full_length = int(np.ceil(np.max([2 * semi_major_axis * np.absolute(np.cos(position_angle)), 2 * semi_minor_axis])))
        y_full_length = int(np.ceil(np.max([2 * semi_major_axis * np.absolute(np.sin(position_angle)), 2 * semi_minor_axis])))

        num_pix = x_full_length * y_full_length

        delta_limit = 0.005
        limit = SERSIC_FRACTIONAL_SIGNAL
        # Limit the maximum size of the stamp in order to save computation time
        while num_pix > (2500.**2):
            limit -= delta_limit

            # Find the effective radius, semi-major, and semi-minor axes sizes
            # needed to encompass SERSIC_FRACTIONAL_SIGNAL of the total flux
            sersic_rad, semi_major_axis, semi_minor_axis = sersic_fractional_radius(r_Sersic, sersic_index,
                                                                                    limit,
                                                                                    ellipticity)

            # Using the position angle, calculate the size of the source in the
            # x and y directions
            x_full_length = int(np.ceil(np.max([2 * semi_major_axis * np.absolute(np.cos(position_angle)), 2 * semi_minor_axis])))
            y_full_length = int(np.ceil(np.max([2 * semi_major_axis * np.absolute(np.sin(position_angle)), 2 * semi_minor_axis])))

            num_pix = x_full_length * y_full_length

        # Make sure the dimensions are odd, so that the galaxy center will
        # be in the center pixel
        if y_full_length % 2 == 0:
            y_full_length += 1
        if x_full_length % 2 == 0:
            x_full_length += 1

        # Center the galaxy within (0, 0) at the requested subpixel location
        if ((subpixx > 1) or (subpixx < -1) or (subpixy > 1) or (subpixy < -1)):
            raise ValueError('Subpixel x and y poistions must be -1 < subpix < 1')

        # Create model. Add 90 degrees to the position_angle because the definition
        # Mirage has been using is that the PA is degrees east of north of the
        # semi-major axis
        mod = Sersic2D(amplitude=amplitude, r_eff=r_Sersic, n=sersic_index, x_0=subpixx, y_0=subpixy,
                       ellip=ellipticity, theta=position_angle)

        x_half_length = x_full_length // 2
        xmin = int(0 - x_half_length)
        xmax = int(0 + x_half_length + 1)

        y_half_length = y_full_length // 2
        ymin = int(0 - y_half_length)
        ymax = int(0 + y_half_length + 1)

        # Create grid to hold model instance
        x_stamp, y_stamp = np.meshgrid(np.arange(xmin, xmax), np.arange(ymin, ymax))

        # Create model instance
        stamp = mod(x_stamp, y_stamp)

        # Check the total signal in the stamp. In some cases (high ellipticity, high sersic index)
        # a source centered at or close to the pixel center results in a bad scaling from Sersic2D.
        # The bad model may have significantly less or more signal than requested. If this is true,
        # rescale manually to the requested signal level. As long as the stamp is large
        # enough (which it should be given the size calculations above), then the signal
        # outside the stamp should be negligible and scaling the stamp to the requested signal
        # level should be correct.
        sig_diff = np.absolute(1. - np.sum(stamp) / (total_counts * limit))
        if sig_diff > signal_matching_threshold:
            stamp = stamp / np.sum(stamp) * (total_counts * limit)

        return stamp

    def crop_galaxy_stamp(self, stamp, threshold):
        """Crop an input stamp image containing a galaxy to a size that
        contains only threshold times the total signal. This is an
        attempt to speed up the simulator a bit, since the galaxy stamp
        images are often very large. Note that galaxy stamp images being
        fed into this function are currently always square.

        Parameters
        ----------
        stamp : numpy.ndarray
            2D stamp image of galaxy

        threshold : float
            Fraction of total flux to keep in the cropped image
            (e.g. 0.999 = 99.9%)

        Returns
        -------
        stamp : numpy.ndarray
            2D cropped image
        """
        totsignal = np.sum(stamp)
        # In the case of no signal, return the original stamp image.
        # This can happen for some Sersic profile parameters when the galaxy
        # is very small compared to the pixel size.
        if totsignal == 0.:
            return stamp
        yd, xd = stamp.shape
        mid = int(xd / 2)
        for rad in range(mid):
            signal = np.sum(stamp[mid - rad:mid + rad + 1, mid - rad:mid + rad + 1]) / totsignal
            if signal >= threshold:
                return stamp[mid - rad:mid + rad + 1, mid - rad:mid + rad + 1]
        # If we make it all the way through the stamp without
        # hitting the threshold, then return the full stamp image
        return stamp

    def make_galaxy_image(self, file):
        """Using the entries in the ``simSignals:galaxyList`` file, create a countrate image
        of model galaxies (2D sersic profiles)

        Parameters
        ----------
        catalog_file : str
            Name of ascii catalog file containing galaxy sources

        Returns
        -------
        galimage : numpy.ndarray
            2D array containing countrate image of galaxy sources

        segmentation.segmap : numpy.ndarray
            Segmentation map corresponding to ``galimage``

        ghost_sources_from_galaxies : str
            Name of an extended source catalog file written out and
            containing ghost sources due to the galaxies in the galaxy
            catalog. Currently only done for NIRISS
        """
        # Read in the list of galaxies (positions and magnitides)
        glist, pixflag, radflag, magsys = self.readGalaxyFile(file)
        if pixflag:
            self.logger.info("Galaxy list input positions assumed to be in units of pixels.")
        else:
            self.logger.info("Galaxy list input positions assumed to be in units of RA and Dec.")

        if radflag:
            self.logger.info("Galaxy list input radii assumed to be in units of pixels.")
        else:
            self.logger.info("Galaxy list input radii assumed to be in units of arcsec.")

        # Extract and save only the entries which will land (fully or partially) on the
        # aperture of the output
        galaxylist, ghost_sources_from_galaxies = self.filterGalaxyList(glist, pixflag, radflag, magsys, file)

        # galaxylist is a table with columns:
        # 'pixelx', 'pixely', 'RA', 'Dec', 'RA_degrees', 'Dec_degrees', 'radius', 'ellipticity',
        # 'pos_angle', 'sersic_index', 'magnitude', 'countrate_e/s', 'counts_per_frame_e'

        # final output image
        yd, xd = self.output_dims

        # Seed cube for disperser
        seed_cube = {}

        # create the final galaxy countrate image
        galimage = np.zeros((yd, xd))

        # Create corresponding segmentation map
        segmentation = segmap.SegMap()
        segmentation.xdim = xd
        segmentation.ydim = yd
        segmentation.initialize_map()

        # Create table of point source countrate versus psf size
        if self.add_psf_wings is True:
            self.translate_psf_table(magsys)

        for entry_index, entry in enumerate(galaxylist):
            # Start timer
            self.timer.start()

            # Get position angle in the correct units. Inputs for each
            # source are degrees east of north. So we need to find the
            # angle between north and V3, and then the angle between
            # V3 and the y-axis on the detector. The former can be found
            # using rotations.posang(attitude_matrix, v2, v3). The latter
            # is just V3SciYAngle in the SIAF (I think???)
            # v3SciYAng is measured in degrees, from V3 towards the Y axis,
            # measured from V3 towards V2.
            xposang = self.calc_x_position_angle(entry)
            sub_x = 0.
            sub_y = 0.

            # First create the galaxy
            stamp = self.create_galaxy(entry['radius'], entry['ellipticity'], entry['sersic_index'],
                                       xposang*np.pi/180., entry['countrate_e/s'], sub_x, sub_y)

            # If the stamp image is smaller than the PSF in either
            # dimension, embed the stamp in an array that matches
            # the psf size. This is so the upcoming convolution will
            # produce an output that includes the wings of the PSF
            galdims = stamp.shape

            # Using the PSF "core" normalized to 1 will keep more light near
            # the core of the galaxy, compared to the more rigorous
            # approach that uses the full convolution including the wings.
            # Whether this is a problem or not will depend on the relative
            # sizes of the photometry aperture versus the extended source.
            psf_dimensions = np.array(self.psf_library.data.shape[-2:])
            psf_shape = np.array((psf_dimensions / self.psf_library_oversamp) -
                                 self.params['simSignals']['gridded_psf_library_row_padding']).astype(int)

            if ((galdims[0] < psf_shape[0]) or (galdims[1] < psf_shape[1])):
                stamp = self.enlarge_stamp(stamp, psf_shape)
                galdims = stamp.shape

            # Get the PSF which will be convolved with the galaxy profile
            # The PSF should be centered in the pixel containing the galaxy center
            psf_image, _, _, min_x, min_y, wings_added = self.create_psf_stamp(entry['pixelx'], entry['pixely'], psf_shape[1], psf_shape[0],
                                                                               ignore_detector=True)

            # Skip sources that fall completely off the detector
            if psf_image is None:
                self.timer.stop()
                continue

            # Normalize the signal in the PSF stamp so that the final galaxy
            # signal will match the requested value
            psf_image = psf_image / np.sum(psf_image)

            # If the source subpixel location is beyond 0.5 (i.e. the edge
            # of the pixel), then we shift the wing->core offset by 1.
            # We also need to shift the location of the wing array on the
            # detector by 1
            if wings_added:
                x_delta = int(np.modf(entry['pixelx'])[0] > 0.5)
                y_delta = int(np.modf(entry['pixely'])[0] > 0.5)
            else:
                x_delta = 0
                y_delta = 0

            # Calculate the coordinates describing the overlap between
            # the PSF image and the galaxy image
            xap, yap, xpts, ypts, (i1, i2), (j1, j2), (k1, k2), \
                (l1, l2) = self.create_psf_stamp_coords(entry['pixelx']+x_delta, entry['pixely']+y_delta,
                                                        galdims, galdims[1] // 2, galdims[0] // 2,
                                                        coord_sys='aperture')

            # Make sure the stamp is at least partially on the detector
            if i1 is not None and i2 is not None and j1 is not None and j2 is not None:
                # Convolve the galaxy image with the PSF image
                stamp = s1.fftconvolve(stamp, psf_image, mode='same')

                # Now add the stamp to the main image
                if ((j2 > j1) and (i2 > i1) and (l2 > l1) and (k2 > k1) and (j1 < yd) and (i1 < xd)):
                    stamp_to_add = stamp[l1:l2, k1:k2]
                    galimage[j1:j2, i1:i2] += stamp_to_add

                    # Add source to segmentation map
                    segmentation.add_object_threshold(stamp_to_add, j1, i1, entry['index'], self.segmentation_threshold)

                    if self.params['Inst']['mode'] in DISPERSED_MODES:
                        # Add source to the seed cube
                        stamp = np.zeros(stamp_to_add.shape)
                        flag = stamp_to_add >= self.segmentation_threshold
                        stamp[flag] = entry['index']
                        seed_cube[entry['index']] = [i1, j1, stamp_to_add*1, stamp*1]

                else:
                    pass
            else:
                pass

            self.timer.stop(name='gal_{}'.format(str(entry_index).zfill(6)))

            # If there are more than 30 galaxies, provide an estimate of processing time
            if len(galaxylist) > 100:
                if ((entry_index == 20) or ((entry_index > 0) and (np.mod(entry_index, 100) == 0))):
                    time_per_galaxy = self.timer.sum(key_str='gal_') / (entry_index+1)
                    estimated_remaining_time = time_per_galaxy * (len(galaxylist) - (entry_index+1)) * u.second
                    time_remaining = np.around(estimated_remaining_time.to(u.minute).value, decimals=2)
                    finish_time = datetime.datetime.now() + datetime.timedelta(minutes=time_remaining)
                    self.logger.info(('Working on galaxy #{}. Estimated time remaining to add all galaxies to the stamp image: {} minutes. '
                                      'Projected finish time: {}'.format(entry_index, time_remaining, finish_time)))

        if self.params['Inst']['mode'] in DISPERSED_MODES:
            # Save the seed cube file of galaxy sources
            pickle.dump(seed_cube, open("%s_galaxy_seed_cube.pickle" % (self.basename), "wb"), protocol=pickle.HIGHEST_PROTOCOL)

        return galimage, segmentation.segmap, ghost_sources_from_galaxies

    def calc_x_position_angle(self, galaxy_entry):
        """For Sersic2D galaxies, calcuate the position angle of the source
        relative to the x axis of the detector given the user-input position
        angle (degrees east of north).

        Parameters
        ----------
        galaxy_entry : astropy.table.Row
            Row of galaxy info from astropy Table of entries.
            e.g. output from readGalaxyFile

        Returns
        -------
        x_posang : float
            Position angle of source relative to detector x
            axis, in units of degrees
        """
        # Note that this method also works for cases that make use of
        # an intermediate aperture (e.g. grism time series, although
        # grism time series obs at the moment only support point sources
        # currently.)
        ra_center = galaxy_entry["RA_degrees"]
        dec_center = galaxy_entry["Dec_degrees"]
        center = SkyCoord(ra_center, dec_center, unit=u.deg)
        offset = center.directional_offset_by(galaxy_entry['pos_angle'] * u.deg, 1. * u.arcsec)
        offset_x, offset_y = self.RADecToXY_astrometric(offset.ra, offset.dec)
        dx = offset_x - galaxy_entry['pixelx']
        dy = offset_y - galaxy_entry['pixely']
        return np.degrees(np.arctan2(dy, dx))

    def calc_x_position_angle_extended(self, position_angle):
        """For extended sources from fits files with no WCS, calcuate the position
        angle of the source relative to the x axis of the detector given the source's
        v2, v3 location and the user-input position angle (degrees east of north).

        Parameters
        ----------
        position_angle : float
            Position angle of source in degrees east of north

        Returns
        -------
        x_posang : float
            Position angle of source relative to detector x
            axis, in units of degrees
        """
        x_posang = self.local_roll + position_angle

        # If we are using a Grism Time series aperture, then we need to use the intermediate
        # aperture to determine the correct rotation. Currently, we should never be in
        # here since grism TSO observations only support the use of point sources.
        if self.use_intermediate_aperture:
            x_posang = self.intermediate_local_roll + position_angle
        return x_posang

    def locate_ghost(self, pixel_x, pixel_y, count_rate, magnitude_system, source_row, obj_type, log_skipped_filters=True):
        """Calculate the ghost location, brightness, and stamp image file name
        for the input source.

        Parameters
        ----------
        pixel_x : float
            X-coordinate of the astronomical source on the detector

        pixel_y : float
            Y-coordinate of the astronomical source on the detector

        count_rate : float
            Count rate (ADU/sec) of the astronomical source

        magnitude_system : str
            Magnitude system of the sources (e.g. 'abmag')

        source_row : astropy.table.row.Row
            Row from source catalog giving information on the source

        obj_type : str
            Type of object the source is (e.g. 'point_source')

        Returns
        -------
        ghost_pixelx : float
            X-coordinate of the associated ghost on the detector

        ghost_pixely : float
            Y-coordinate of the associated ghost on the detector

        ghost_mag : float
            Magnitude of the associated ghost

        ghost_countrate : float
            Countrate of the associated ghost

        ghost_file : str
            Name of fits file containing the stamp image to use for the ghost source

        log_skipped_filters : bool
            If True, any filter magnitudes that are not converted because
            the filter is not in the gap summary file will be logged.
        """
        allowed_types = ['point_source', 'galaxies', 'extended']
        if obj_type not in allowed_types:
            raise ValueError('Unknown object type: {}. Must be one of: {}'.format(obj_type, allowed_types))

        # For WFSS simulations, we ignore the grism
        search_filter = self.params['Readout']['filter']
        if self.params['Readout']['filter'].upper() in ['GR150R', 'GR150C']:
            search_filter = 'GR150'

        ghost_pixelx, ghost_pixely, ghost_countrate = get_ghost(pixel_x, pixel_y,
                                                                count_rate,
                                                                search_filter,
                                                                self.params['Readout']['pupil'],
                                                                NIRISS_GHOST_GAP_FILE,
                                                                log_skipped_filters=log_skipped_filters
                                                                )
        if isinstance(ghost_pixelx, np.float):
            if not np.isfinite(ghost_pixelx):
                return np.nan, np.nan, np.nan, np.nan, np.nan

        # Convert ghost countrate back into a magnitude
        ghost_mag = utils.countrate_to_magnitude(self.instrument, self.params['Readout']['filter'],
                                                 magnitude_system, ghost_countrate, photfnu=self.photfnu,
                                                 photflam=self.photflam,
                                                 vegamag_zeropoint=self.vegazeropoint)

        # Determine the name of the file containing the stamp image
        # to use for the ghost
        ghost_file = determine_ghost_stamp_filename(source_row, obj_type)

        return ghost_pixelx, ghost_pixely, ghost_mag, ghost_countrate, ghost_file

    def save_ghost_catalog(self, x_loc, y_loc, filenames, magnitudes, orig_source_catalog, orig_source_mapping):
        """From lists of ghost source positions and magnitudes, create an extended source
        catalog and save to a file

        Parameters
        ----------
        x_loc : list
            X-coordinate positions of ghost sources

        y_loc : list
            Y-coordinate positions of ghost sources

        filenames : list
            Fits file stamp images associated with the ghost sources

        magnitudes : list
            Magnitudes associated with the ghost sources

        orig_source_catalog : str
            Name of the catalog file from which the ghosts were derived.
            The output catalog will be saved into a 'ghost_source_catalogs'
            subdirectory under the directory of this file.

        orig_source_mapping : list
            Indexes of the real sources (in ``orig_source_catalog``) corresponding
            to the ghosts

        Returns
        -------
        catalog_filename : str
            Name of ascii file to which the catalog is saved
        """
        if len(x_loc) == 0:
            self.logger.info(('Ghost catalog from the catalog {} has no sources.'.format(orig_source_catalog)))
            return None

        pa = [0.] * len(x_loc)
        ghost_sources = ExtendedCatalog(x=x_loc, y=y_loc, filenames=filenames, position_angle=pa,
                                        starting_index=self.max_source_index+1, corresponding_source_index_for_ghost=orig_source_mapping)

        for colname in magnitudes.colnames:
            ghost_sources.add_magnitude_column(magnitudes[colname], column_name=colname)

        self.max_source_index += len(ghost_sources.table['x_or_RA'])

        # Write out the ghost catalog to a new file, to be used later. Let's create
        # a subdirectory under the directory where the point source catalog is.
        source_cat_dir, source_cat_file = os.path.split(orig_source_catalog)
        ghost_cat_dir = os.path.join(source_cat_dir, 'ghost_source_catalogs')
        paramfile_name_only = os.path.basename(self.paramfile).strip('.yaml')
        utils.ensure_dir_exists(ghost_cat_dir)
        ghost_cat_filename = 'ghosts_from_{}_for_{}.cat'.format(source_cat_file, paramfile_name_only)
        ghosts_cat_file = os.path.join(ghost_cat_dir, ghost_cat_filename)

        ghost_sources.save(ghosts_cat_file)
        self.logger.info(('Catalog of ghost sources from the catalog {} has been saved to: {}'
                          .format(source_cat_file, ghosts_cat_file)))
        return ghosts_cat_file

    def getExtendedSourceList(self, filename, ghost_search=True):
        """Read in the list of extended sources from a catalog file, calculate locations
        on the detector, and countrates. Calculate positions of associated ghosts if
        requested

        Parameters
        ----------
        filename : str
            Name of ascii catalog file containing extended sources

        ghost_search : bool
            If True, positions and countrates for ghosts associated with the sources in
            ```filename``` are calculated. This currently is only done for NIRISS

        Returns
        -------
        extSourceList : astropy.table.Table
            Table of extended sources

        all_stamps : list
            List of stamp files to use for the extended sources

        ghost_catalog_file : str
            Name of ascii file containing the catalog of ghost sources associated with
            the input catalog
        """
        extSourceList = Table(names=('index', 'pixelx', 'pixely', 'RA', 'Dec',
                                     'RA_degrees', 'Dec_degrees', 'magnitude',
                                     'countrate_e/s', 'counts_per_frame_e'),
                              dtype=('i', 'f', 'f', 'S14', 'S14', 'f', 'f', 'f', 'f', 'f'))

        try:
            lines, pixelflag, magsys = self.read_point_source_file(filename)
            if pixelflag:
                self.logger.info("Extended source list input positions assumed to be in units of pixels.")
            else:
                self.logger.info("Extended list input positions assumed to be in units of RA and Dec.")
        except:
            raise FileNotFoundError("WARNING: Unable to open the extended source list file {}".format(filename))

        # Create table of point source countrate versus psf size
        if self.add_psf_wings is True:
            self.translate_psf_table(magsys)

        # File to save adjusted point source locations
        eoutcat = self.params['Output']['file'][0:-5] + '_extendedsources.list'
        eslist = open(eoutcat, 'w')

        dtor = math.radians(1.)
        nx = (self.subarray_bounds[2] - self.subarray_bounds[0]) + 1
        ny = (self.subarray_bounds[3] - self.subarray_bounds[1]) + 1
        xc = (self.subarray_bounds[2] + self.subarray_bounds[0]) / 2.
        yc = (self.subarray_bounds[3] + self.subarray_bounds[1]) / 2.

        # Write out the RA and Dec of the field center to the output file
        # Also write out column headers to prepare for source list
        eslist.write(("# Field center (degrees): %13.8f %14.8f y axis rotation angle "
                      "(degrees): %f  image size: %4.4d %4.4d\n" %
                      (self.ra, self.dec, self.params['Telescope']['rotation'], nx, ny)))
        eslist.write('# \n')
        eslist.write(("#    Index   RA_(hh:mm:ss)   DEC_(dd:mm:ss)   RA_degrees      "
                      "DEC_degrees     pixel_x   pixel_y    magnitude   counts/sec    counts/frame\n"))

        # Get source index numbers
        indexes = lines['index']

        # Check the source list and remove any sources that are well outside the
        # field of view of the detector. These sources cause the coordinate
        # conversion to hang.
        indexes, lines = self.remove_outside_fov_sources(indexes, lines, pixelflag, 4096)

        # Determine the name of the column to use for source magnitudes
        mag_column = self.select_magnitude_column(lines, filename)

        # For NIRISS observations where ghosts will be added, create a table to hold
        # the ghost entries
        if ghost_search and self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            ghost_source_index = []
            ghost_x = []
            ghost_y = []
            ghost_filename = []
            ghost_mag = []
            ghost_mags = None
        else:
            ghost_x = None

        # Loop over input lines in the source list
        skipped_non_niriss = False
        all_stamps = []
        ghost_i = 0
        for indexnum, values in zip(indexes, lines):
            if not os.path.isfile(values['filename']):
                raise FileNotFoundError('{} from extended source catalog does not exist.'.format(values['filename']))

            # If the filter/pupil pair is not in the ghost summary file, log that only for the
            # first source. No need to repeat for all sources.
            if ghost_i == 0:
                log_ghost_err = True
            else:
                log_ghost_err = False

            pixelx, pixely, ra, dec, ra_str, dec_str = self.get_positions(values['x_or_RA'],
                                                                          values['y_or_Dec'],
                                                                          pixelflag, 4096)
            # Get the input magnitude
            try:
                mag = float(values[mag_column])
            except ValueError:
                mag = None

            # Now find out how large the extended source image is, so we
            # know if all, part, or none of it will fall in the field of view
            ext_stamp = fits.getdata(values['filename'])
            if len(ext_stamp.shape) != 2:
                ext_stamp = fits.getdata(values['filename'], 1)

            # Rotate the stamp image if requested, but don't do so if the specified pos angle is None
            ext_stamp = self.rotate_extended_image(ext_stamp, values['pos_angle'])

            eshape = np.array(ext_stamp.shape)
            if len(eshape) == 2:
                edgey, edgex = eshape // 2
            else:
                raise ValueError(("WARNING, extended source image {} is not 2D! "
                                  "This is not supported.".format(values['filename'])))

            # Define the min and max source locations (in pixels) that fall onto the subarray
            # Inlude the effects of a requested grism_direct image, and also keep sources that
            # will only partially fall on the subarray
            # pixel coords here can still be negative and kept if the grism image is being made

            # First, coord limits for just the subarray
            miny = 0
            maxy = self.subarray_bounds[3] - self.subarray_bounds[1]
            minx = 0
            maxx = self.subarray_bounds[2] - self.subarray_bounds[0]

            # Expand the limits if a grism direct image is being made
            if (self.params['Output']['grism_source_image'] == True) or (self.params['Inst']['mode'] in ["pom", "wfss"]):
                transmission_ydim, transmission_xdim = self.transmission_image.shape
                miny = miny - self.subarray_bounds[1] - self.trans_ff_ymin
                minx = minx - self.subarray_bounds[0] - self.trans_ff_xmin
                maxx = minx + transmission_xdim
                maxy = miny + transmission_ydim
                nx = transmission_xdim
                ny = transmission_ydim

            # Now, expand the dimensions again to include point sources that fall only partially on the
            # subarray
            miny -= edgey
            maxy += edgey
            minx -= edgex
            maxx += edgex

            # Calculate count rate
            norm_factor = np.sum(ext_stamp)
            if mag is not None:
                countrate = utils.magnitude_to_countrate(self.instrument, self.params['Readout']['filter'],
                                                         magsys, mag, photfnu=self.photfnu, photflam=self.photflam,
                                                         vegamag_zeropoint=self.vegazeropoint)
            else:
                countrate = norm_factor * self.params['simSignals']['extendedscale']

            # Calculate the location and brightness of any ghost, if requested
            # This is done outside the if statement below because sources outside the
            # detector can potentially produce ghosts on the detector
            if ghost_search and self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
                gx, gy, gmag, gcounts, gfile = self.locate_ghost(pixelx, pixely, countrate, magsys, values, 'extended',
                                                                 log_skipped_filters=log_ghost_err)
                if np.isfinite(gx) and gfile is not None:
                    ghost_source_index.append(indexnum)
                    ghost_x.append(gx)
                    ghost_y.append(gy)
                    ghost_mag.append(gmag)
                    ghost_filename.append(gfile)

                    ghost_src, skipped_non_niriss = source_mags_to_ghost_mags(values, self.params['Reffiles']['flux_cal'],
                                                                              magsys, NIRISS_GHOST_GAP_FILE, self.params['Readout']['filter'], log_skipped_filters=False)

                    if ghost_mags is None:
                        ghost_mags = copy.deepcopy(ghost_src)
                    else:
                        ghost_mags = vstack([ghost_mags, ghost_src])

                # Increment the counter to control the logging regardless of whether the source
                # is on the detector or not.
                ghost_i += 1

            # Keep only sources within the appropriate bounds
            if pixely > miny and pixely < maxy and pixelx > minx and pixelx < maxx:

                # Set up an entry for the output table
                entry = [indexnum, pixelx, pixely, ra_str, dec_str, ra, dec, mag]

                # save the stamp image after normalizing to a total signal of 1.
                ext_stamp /= norm_factor
                all_stamps.append(ext_stamp)

                # If a magnitude is given then adjust the countrate to match it
                if mag is not None:
                    # Convert magnitudes to countrate (ADU/sec) and counts per frame
                    framecounts = countrate * self.frametime
                    magwrite = mag

                else:
                    # In this case, no magnitude is given in the extended input list
                    # Assume the input stamp image is in units of e/sec then.
                    self.logger.warning("No magnitude given for extended source in {}.".format(values['filename']))
                    self.logger.warning("Assuming the original file is in units of counts per sec.")
                    self.logger.warning("Multiplying original file values by 'extendedscale'.")
                    framecounts = countrate * self.frametime
                    magwrite = 99.99999

                # add the countrate and the counts per frame to pointSourceList
                # since they will be used in future calculations
                # entry.append(scale)
                entry.append(countrate)
                entry.append(framecounts)

                # add the good point source, including location and counts, to the pointSourceList
                # self.pointSourceList.append(entry)
                extSourceList.add_row(entry)

                # Write out positions, distances, and counts to the output file
                eslist.write(("%i %s %s %14.8f %14.8f %9.3f %9.3f  %9.3f  %13.6e   %13.6e\n" %
                             (indexnum, ra_str, dec_str, ra, dec, pixelx, pixely, magwrite, countrate,
                              framecounts)))

        if ghost_search and self.params['Inst']['instrument'].lower() == 'niriss' and \
           self.params['simSignals']['add_ghosts'] and skipped_non_niriss:
            self.logger.info("Skipped the calculation of ghost source magnitudes for the non-NIRISS magnitude columns in {}".format(filename))

        self.logger.info("Number of extended sources found within or close to the requested aperture: {}".format(len(extSourceList)))
        # close the output file
        eslist.close()

        # If no good point sources were found in the requested array, alert the user
        if len(extSourceList) < 1:
            self.logger.info("Warning: no extended sources within the requested array.")
            self.logger.info("The extended source image option is being turned off")

        if ghost_search and self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            ghost_catalog_file = self.save_ghost_catalog(ghost_x, ghost_y, ghost_filename, ghost_mags, filename, ghost_source_index)
        else:
            ghost_catalog_file = None

        return extSourceList, all_stamps, ghost_catalog_file

    def rotate_extended_image(self, stamp_image, pos_angle):
        """Given the user-input position angle for the extended source
        image, calculate the appropriate angle of the stamp image
        relative to the detector x axis, and rotate the stamp image.
        TO DO: if the stamp image contains a WCS, use that to
        determine rotation angle

        Parameters
        ----------
        stamp_image : numpy.ndarray
            2D stamp image of the extended source

        pos_angle : float
            Position angle of stamp image relative to north in degrees. A value of None
            or string 'None' will result in no rotation, i.e. the input stamp is returned
            without modification.

        Returns
        -------
        rotated : numpy.ndarray
            Rotated stamp image
        """

        # Don't rotate if the specified position angle is None.
        # check for both Python None and string 'None' or 'none'
        if pos_angle is None or str(pos_angle).lower() == 'none':
            return stamp_image

        # Add later: check for WCS and use that
        x_pos_ang = self.calc_x_position_angle_extended(pos_angle)

        rotated = rotate(stamp_image, x_pos_ang, mode='constant', cval=0.)
        return rotated

    def make_extended_source_image(self, extSources, extStamps, extConvolutions):
        # Create the empty image
        yd, xd = self.output_dims
        extimage = np.zeros(self.output_dims)

        # Prepare seed cube for extended sources
        seed_cube = {}

        # Create corresponding segmentation map
        segmentation = segmap.SegMap()
        segmentation.xdim = xd
        segmentation.ydim = yd
        segmentation.initialize_map()

        if len(extConvolutions) > 0:
            if extConvolutions[0]:
                self.logger.info('Convolving extended sources with PSF prior to adding to seed image.')
            else:
                self.logger.info('Extended sources will not be convolved with the PSF.')

        # Loop over the entries in the source list
        for entry, stamp, convolution in zip(extSources, extStamps, extConvolutions):
            stamp_dims = stamp.shape

            stamp *= entry['countrate_e/s']

            # If the stamp needs to be convolved with the NIRCam PSF,
            # create the correct PSF  here and read it in
            if convolution:
                # If the stamp image is smaller than the PSF in either
                # dimension, embed the stamp in an array that matches
                # the psf size. This is so the upcoming convolution will
                # produce an output that includes the wings of the PSF
                psf_dimensions = np.array(self.psf_library.data.shape[-2:])
                psf_shape = np.array((psf_dimensions / self.psf_library_oversamp) -
                                     self.params['simSignals']['gridded_psf_library_row_padding']).astype(int)
                if ((stamp_dims[0] < psf_shape[0]) or (stamp_dims[1] < psf_shape[1])):
                    stamp = self.enlarge_stamp(stamp, psf_shape)
                    stamp_dims = stamp.shape

                # Create the PSF
                # Using the PSF "core" normalized to 1 will keep more light near
                # the core of the galaxy, compared to the more rigorous
                # approach that uses the full convolution including the wings.
                # Whether this is a problem or not will depend on the relative
                # sizes of the photometry aperture versus the extended source.
                psf_image, _, _, min_x, min_y, wings_added = self.create_psf_stamp(entry['pixelx'], entry['pixely'],
                                                                                   psf_shape[1], psf_shape[0], ignore_detector=True)

                # Skip sources that fall completely off the detector
                if psf_image is None:
                    continue

                # Normalize the PSF so that the final signal in the extended
                # source mathces the requested signal
                psf_image = psf_image / np.sum(psf_image)

                # If the source subpixel location is beyond 0.5 (i.e. the edge
                # of the pixel), then we shift the wing->core offset by 1.
                # We also need to shift the location of the wing array on the
                # detector by 1
                if wings_added:
                    x_delta = int(np.modf(entry['pixelx'])[0] > 0.5)
                    y_delta = int(np.modf(entry['pixely'])[0] > 0.5)
                else:
                    x_delta = 0
                    y_delta = 0

                # Calculate the coordinates describing the overlap
                # between the extended image and the PSF image
                xap, yap, xpts, ypts, (i1, i2), (j1, j2), (k1, k2), \
                    (l1, l2) = self.create_psf_stamp_coords(entry['pixelx']+x_delta, entry['pixely']+y_delta,
                                                            stamp_dims, stamp_dims[1] // 2, stamp_dims[0] // 2,
                                                            coord_sys='aperture')

                if None in [i1, i2, j1, j2, k1, k2, l1, l2]:
                    continue

                # Convolve the extended image with the stamp image
                stamp = s1.fftconvolve(stamp, psf_image, mode='same')
            else:
                # If no PSF convolution is to be done, find the
                # coordinates describing the overlap between the
                # original stamp image and the aperture
                xap, yap, xpts, ypts, (i1, i2), (j1, j2), (k1, k2), \
                    (l1, l2) = self.create_psf_stamp_coords(entry['pixelx'], entry['pixely'],
                                                            stamp_dims, stamp_dims[1] // 2, stamp_dims[0] // 2,
                                                            coord_sys='aperture')

            # Make sure the stamp is at least partially on the detector
            if i1 is not None and i2 is not None and j1 is not None and j2 is not None:

                # Now add the stamp to the main image
                if ((j2 > j1) and (i2 > i1) and (l2 > l1) and (k2 > k1) and (j1 < yd) and (i1 < xd)):
                    stamp_to_add = stamp[l1:l2, k1:k2]
                    extimage[j1:j2, i1:i2] += stamp_to_add

                # Add source to segmentation map
                segmentation.add_object_threshold(stamp_to_add, j1, i1, entry['index'],
                                                  self.segmentation_threshold)

                if self.params['Inst']['mode'] in DISPERSED_MODES:
                    # Add source to seed cube
                    stamp = np.zeros(stamp_to_add.shape)
                    flag = stamp_to_add >= self.segmentation_threshold
                    stamp[flag] = entry['index']
                    seed_cube[entry['index']] = [i1, j1, stamp_to_add*1, stamp*1]

                self.n_extend += 1

        if self.params['Inst']['mode'] in DISPERSED_MODES:
            # Save the seed cube
            pickle.dump(seed_cube, open("%s_extended_seed_cube.pickle" % (self.basename), "wb"), protocol=pickle.HIGHEST_PROTOCOL)

        if self.n_extend == 0:
            self.logger.info("No extended sources present within the aperture.")
        else:
            self.logger.info('Number of extended sources present within the aperture: {}'.format(self.n_extend))
        return extimage, segmentation.segmap

    def enlarge_stamp(self, image, dims):
        """Place the given image within an enlarged array of zeros. If the
        requested dimension lengths are odd while ``image``'s dimension
        lengths are even, then the new array is expanded by one to also have
        even dimensions. This ensures that ``image`` will be centered
        within ``array``.

        Parameters
        ----------
        image : numpy.ndarray
            2D image

        dims : list
            2-element list of (y-dimension, x-dimension) to embed ``image``
            within

        Returns
        -------
        array : numpy.ndarray
            Expanded image
        """
        dim_y, dim_x = dims
        image_size_y, image_size_x = image.shape

        if image_size_x < dim_x:
            if dim_x % 2 != image_size_x % 2:
                dim_x += 1
            dx = dim_x - image_size_x
            dx = int(dx / 2)
        else:
            dx = 0
            dim_x = image_size_x

        if image_size_y < dim_y:
            if dim_y % 2 != image_size_y % 2:
                dim_y += 1
            dy = dim_y - image_size_y
            dy = int(dy / 2)
        else:
            dy = 0
            dim_y = image_size_y

        array = np.zeros((dim_y, dim_x))
        array[dy:dim_y-dy, dx:dim_x-dx] = image
        return array

    def seg_from_photutils(self, image, number, noise):
        # Create a segmentation map for the input image
        # using photutils. In this case, the input noise
        # represents the single frame noise for the appropriate
        # observing mode
        map = detect_sources(image, noise * 3., 8).data + number
        return map

    def makeFilterTable(self):
        # Create the table that contains the possible filter list, quantum yields, and countrates for a
        # star with vega magnitude of 15 in each filter. Do this by reading in phot_file
        # listed in the parameter file.

        # FUTURE WORK: If the countrates are left as 0, then pysynphot will
        # be used to calculate them later
        try:
            cvals_tab = ascii.read(self.params['Reffiles']['phot'])
            instrumentfilternames = cvals_tab['filter'].data
            stringcountrates = cvals_tab['countrate_for_vegamag15'].data
            instrumentmag15countrates = [float(s) for s in stringcountrates]
            strinstrumentqy = cvals_tab['quantum_yield'].data
            qy = [float(s) for s in strinstrumentqy]
            self.countvalues = dict(zip(instrumentfilternames, instrumentmag15countrates))
            self.qydict = dict(zip(instrumentfilternames, qy))
        except:
            raise IOError("WARNING: Unable to read in {}.".format(self.params['Reffiles']['phot']))

    def readParameterFile(self):
        """Read in the parameter file"""
        # List of fields in the yaml file to check for extra colons
        search_cats = ['title:', 'PI_Name:', 'Science_category:', 'observation_label:']

        # Open the yaml file and check for the presence of extra colons
        adjust_file = False
        with open(self.paramfile) as infile:
            read_data = infile.readlines()
            for i, line in enumerate(read_data):
                for search_term in search_cats:
                    if search_term in line:
                        idx = []
                        hashidx = [200]
                        for m in re.finditer(':', line):
                            idx.append(m.start())
                        for mm in re.finditer('#', line):
                            hashidx.append(mm.start())
                        num = np.sum(np.array(idx) < min(hashidx))
                        if num > 1:
                            adjust_file = True
                            later_string = line[idx[0]+1:]
                            later_string = later_string.replace(':', ',')
                            newline = line[0: idx[0]+1] + later_string
                            read_data[i] = newline

        if adjust_file:
            # Make a copy of the original file and then delete it
            param_dir, param_file = os.path.split(self.paramfile)
            yaml_copy = os.path.join(param_dir, 'orig_{}'.format(param_file))
            shutil.copy2(self.paramfile, yaml_copy)
            os.remove(self.paramfile)

            # Write the adjusted lines to a new copy of the input file
            with open(self.paramfile, 'w') as f:
                for item in read_data:
                    f.write("{}".format(item))

        # Load the yaml file
        try:
            with open(self.paramfile, 'r') as infile:
                self.params = yaml.safe_load(infile)
        except (ScannerError, FileNotFoundError, IOError) as e:
            self.logger.info(e)

    def check_params(self):
        """Check input parameters for expected datatypes, values"""
        # Check instrument name
        if self.params['Inst']['instrument'].lower() not in INST_LIST:
            raise NotImplementedError("WARNING: {} instrument not implemented within ramp simulator")

        # Check entered mode:
        possibleModes = MODES[self.params['Inst']['instrument'].lower()]
        self.params['Inst']['mode'] = self.params['Inst']['mode'].lower()
        if self.params['Inst']['mode'] in possibleModes:
            pass
        else:
            raise ValueError(("WARNING: unrecognized mode {} for {}. Must be one of: {}"
                              .format(self.params['Inst']['mode'],
                                      self.params['Inst']['instrument'], possibleModes)))

        # Check telescope tracking entry
        self.params['Telescope']['tracking'] = self.params['Telescope']['tracking'].lower()
        if self.params['Telescope']['tracking'] not in TRACKING_LIST:
            raise ValueError(("WARNING: telescope tracking set to {}, but must be one "
                              "of {}.".format(self.params['Telescope']['tracking'],
                                              TRACKING_LIST)))

        # Non-sidereal WFSS observations are not yet supported
        if self.params['Telescope']['tracking'] == 'non-sidereal' and \
           self.params['Inst']['mode'] in ['wfss', 'ts_grism']:
            raise ValueError(("WARNING: wfss observations with non-sidereal "
                              "targets not yet supported."))

        # Set nframe and nskip according to the values in the
        # readout pattern definition file
        self.read_pattern_check()

        # Check for entries in the parameter file that are None or blank,
        # indicating the step should be skipped. Create a dictionary of steps
        # and populate with True or False
        self.runStep = {}
        self.runStep['pixelflat'] = self.checkRunStep(self.params['Reffiles']['pixelflat'])
        self.runStep['illuminationflat'] = self.checkRunStep(self.params['Reffiles']['illumflat'])
        self.runStep['astrometric'] = self.checkRunStep(self.params['Reffiles']['astrometric'])
        # self.runStep['distortion_coeffs'] = self.checkRunStep(self.params['Reffiles']['distortion_coeffs'])
        self.runStep['ipc'] = self.checkRunStep(self.params['Reffiles']['ipc'])
        self.runStep['crosstalk'] = self.checkRunStep(self.params['Reffiles']['crosstalk'])
        self.runStep['occult'] = self.checkRunStep(self.params['Reffiles']['occult'])
        self.runStep['pointsource'] = self.checkRunStep(self.params['simSignals']['pointsource'])
        self.runStep['galaxies'] = self.checkRunStep(self.params['simSignals']['galaxyListFile'])
        self.runStep['extendedsource'] = self.checkRunStep(self.params['simSignals']['extended'])
        self.runStep['movingTargets'] = self.checkRunStep(self.params['simSignals']['movingTargetList'])
        self.runStep['movingTargetsSersic'] = self.checkRunStep(self.params['simSignals']['movingTargetSersic'])
        self.runStep['movingTargetsExtended'] = self.checkRunStep(self.params['simSignals']['movingTargetExtended'])
        self.runStep['MT_tracking'] = self.checkRunStep(self.params['simSignals']['movingTargetToTrack'])
        self.runStep['zodiacal'] = self.checkRunStep(self.params['simSignals']['zodiacal'])
        self.runStep['scattered'] = self.checkRunStep(self.params['simSignals']['scattered'])
        # self.runStep['fwpw'] = self.checkRunStep(self.params['Reffiles']['filtpupilcombo'])

        # Notify user if no catalogs are provided
        if self.params['simSignals']['pointsource'] == 'None':
            self.logger.info('No point source catalog provided in yaml file.')

        if self.params['simSignals']['galaxyListFile'] == 'None':
            self.logger.info('No galaxy catalog provided in yaml file.')

        # Determine the instrument module and detector from the aperture name
        aper_name = self.params['Readout']['array_name']
        self.instrument = self.params["Inst"]["instrument"].lower()
        try:
            # previously detector was e.g. 'A1'. Let's make it NRCA1 to be more in
            # line with other instrument formats
            # detector = self.subdict[self.subdict['AperName'] == aper_name]['Detector'][0]
            # module = detector[0]
            detector = aper_name.split('_')[0]
            self.detector = detector
            if self.instrument == 'nircam':
                module = detector[3]
            elif self.instrument == 'niriss':
                module = detector[0]
            elif self.instrument == 'fgs':
                detector = detector.replace("FGS", "GUIDER")
                module = detector[0]
        except IndexError:
            raise ValueError('Unable to determine the detector/module in aperture {}'.format(aper_name))

        # If instrument is FGS, then force filter to be 'NA' for the purposes
        # of constructing the correct PSF input path name. Then change to be
        # the DMS-required "N/A" when outputs are saved
        if self.instrument == 'fgs':
            self.params['Readout']['filter'] = 'NA'
            self.params['Readout']['pupil'] = 'NA'

        # Get basic flux calibration information
        self.vegazeropoint, self.photflam, self.photfnu, self.pivot = \
            fluxcal_info(self.params['Reffiles']['flux_cal'], self.instrument, self.params['Readout']['filter'],
                         self.params['Readout']['pupil'], detector, module)

        # Get the threshold signal value for pixels to be included in the
        # segmentation map. Pixels with signals greater than or equal to
        # this level will be included in the segmap
        self.set_segmentation_threshold()

        # Convert the input RA and Dec of the pointing position into floats
        # Check to see if the inputs are in decimal units or hh:mm:ss strings
        try:
            self.ra = float(self.params['Telescope']['ra'])

            self.dec = float(self.params['Telescope']['dec'])
        except:
            self.ra, self.dec = utils.parse_RA_Dec(self.params['Telescope']['ra'],
                                                   self.params['Telescope']['dec'])

        #if abs(self.dec) > 90. or self.ra < 0. or self.ra > 360. or \
        if abs(self.dec) > 90. or \
           self.ra is None or self.dec is None:
            raise ValueError("WARNING: bad requested RA and Dec {} {}".format(self.ra, self.dec))

        # make sure the rotation angle is a float
        try:
            self.params['Telescope']["rotation"] = float(self.params['Telescope']["rotation"])
        except ValueError:
            self.logger.error(("ERROR: bad rotation value {}, setting to zero."
                               .format(self.params['Telescope']["rotation"])))
            self.params['Telescope']["rotation"] = 0.

        siaf_inst = self.params['Inst']['instrument']
        if siaf_inst.lower() == 'nircam':
            siaf_inst = 'NIRCam'
        instrument_siaf = siaf_interface.get_instance(siaf_inst)
        self.siaf = instrument_siaf[self.params['Readout']['array_name']]
        self.local_roll, self.attitude_matrix, self.ffsize, \
            self.subarray_bounds = siaf_interface.get_siaf_information(instrument_siaf,
                                                                       self.params['Readout']['array_name'],
                                                                       self.ra, self.dec,
                                                                       self.params['Telescope']['rotation'])

        # If the exposure uses one of the grism time series apertures, then read/determine
        # the intermediate aperture to use, and get associated SIAF information
        self.determine_intermediate_aperture(instrument_siaf)

        self.logger.info('SIAF: Requested {}   got {}'.format(self.params['Readout']['array_name'], self.siaf.AperName))

        # If optical ghosts are to be added, make sure the ghost gap file is present in the
        # config directory. This will be downloaded from the niriss_ghost github repo regardless of whether
        # the file is already present, in order to ensure we have the latest copy.
        if self.params['Inst']['instrument'].lower() == 'niriss' and self.params['simSignals']['add_ghosts']:
            self.logger.info('Downloading NIRISS ghost gap file...')
            config_dir, ghost_file = os.path.split(NIRISS_GHOST_GAP_FILE)
            download_file(NIRISS_GHOST_GAP_URL, ghost_file, output_directory=config_dir, force=True)

        # Set the background value if the high/medium/low settings
        # are used
        bkgdrate_options = ['high', 'medium', 'low']

        # For WFSS observations, we want the background in the direct
        # seed image to be zero. The dispersed background will be created
        # and added as part of the dispersion process
        if self.params['Inst']['mode'].lower() == 'wfss':
            self.params['simSignals']['bkgdrate'] = 0.

        if np.isreal(self.params['simSignals']['bkgdrate']):
            self.params['simSignals']['bkgdrate'] = float(self.params['simSignals']['bkgdrate'])
        else:
            if self.params['simSignals']['bkgdrate'].lower() in bkgdrate_options:
                self.logger.info(("Calculating background rate using jwst_background "
                                  "based on {} level".format(self.params['simSignals']['bkgdrate'])))

                # Find the appropriate filter throughput file
                if os.path.split(self.params['Reffiles']['filter_throughput'])[1] == 'placeholder.txt':
                    filter_file = utils.get_filter_throughput_file(self.params['Inst']['instrument'].lower(),
                                                                   self.params['Readout']['filter'],
                                                                   self.params['Readout']['pupil'],
                                                                   fgs_detector=detector, nircam_module=module)
                else:
                    filter_file = self.params['Reffiles']['filter_throughput']

                self.logger.info(("Using {} filter throughput file for background calculation."
                                  .format(filter_file)))

                # To translate background signals from MJy/sr to e-/sec to
                # ADU/sec, we need a mean gain value
                if self.params['Inst']['instrument'].lower() == 'nircam':
                    if '5' in detector:
                        shorthand = 'lw{}'.format(module.lower())
                    else:
                        shorthand = 'sw{}'.format(module.lower())
                    self.gain_value = MEAN_GAIN_VALUES[self.params['Inst']['instrument'].lower()][shorthand]
                elif self.params['Inst']['instrument'].lower() == 'niriss':
                    self.gain_value = MEAN_GAIN_VALUES[self.params['Inst']['instrument'].lower()]
                elif self.params['Inst']['instrument'].lower() == 'fgs':
                    self.gain_value = MEAN_GAIN_VALUES[self.params['Inst']['instrument'].lower()][detector.lower()]

                if self.params['simSignals']['use_dateobs_for_background']:
                    bkgd_wave, bkgd_spec = backgrounds.day_of_year_background_spectrum(self.params['Telescope']['ra'],
                                                                                       self.params['Telescope']['dec'],
                                                                                       self.params['Output']['date_obs'])
                    self.params['simSignals']['bkgdrate'] = backgrounds.calculate_background(self.ra, self.dec,
                                                                                             filter_file, True,
                                                                                             self.gain_value, self.siaf,
                                                                                             back_wave=bkgd_wave,
                                                                                             back_sig=bkgd_spec)
                    self.logger.info("Background rate determined using date_obs: {}".format(self.params['Output']['date_obs']))
                else:
                    # Here the background level is based on high/medium/low rather than date
                    orig_level = copy.deepcopy(self.params['simSignals']['bkgdrate'])
                    self.params['simSignals']['bkgdrate'] = backgrounds.calculate_background(self.ra, self.dec,
                                                                                             filter_file, False,
                                                                                             self.gain_value, self.siaf,
                                                                                             level=self.params['simSignals']['bkgdrate'].lower())
                    self.logger.info("Background rate determined using {} level: {}".format(orig_level, self.params['simSignals']['bkgdrate']))
            else:
                raise ValueError(("WARNING: unrecognized background rate value. "
                                  "Must be either a number or one of: {}"
                                  .format(bkgdrate_options)))

        # Check that the various scaling factors are floats and within a reasonable range
        # self.params['cosmicRay']['scale'] = self.checkParamVal(self.params['cosmicRay']['scale'], 'cosmicRay', 0, 100, 1)
        self.params['simSignals']['extendedscale'] = self.checkParamVal(self.params['simSignals']['extendedscale'],
                                                                        'extendedEmission', 0, 10000, 1)
        self.params['simSignals']['zodiscale'] = self.checkParamVal(self.params['simSignals']['zodiscale'],
                                                                    'zodi', 0, 10000, 1)
        self.params['simSignals']['scatteredscale'] = self.checkParamVal(self.params['simSignals']['scatteredscale'],
                                                                         'scatteredLight', 0, 10000, 1)

        # make sure the requested output format is an allowed value
        if self.params['Output']['format'] not in ALLOWEDOUTPUTFORMATS:
            raise ValueError(("WARNING: unsupported output format {} requested. "
                              "Possible options are {}.".format(self.params['Output']['format'],
                                                                ALLOWEDOUTPUTFORMATS)))

        # Entries for creating the grism input image
        if not isinstance(self.params['Output']['grism_source_image'], bool):
            if self.params['Output']['grism_source_image'].lower() == 'none':
                self.params['Output']['grism_source_image'] = False
            else:
                raise ValueError("WARNING: grism_source_image needs to be True or False")

        # Location of extended image on output array, pixel x, y values.
        try:
            self.params['simSignals']['extendedCenter'] = np.fromstring(self.params['simSignals']['extendedCenter'],
                                                                        dtype=int, sep=", ")
        except:
            raise RuntimeError(("WARNING: not able to parse the extendedCenter list {}. "
                                "It should be a comma-separated list of x and y pixel positions."
                                .format(self.params['simSignals']['extendedCenter'])))

        # Check for consistency between the mode and grism_source_image value
        if ((self.params['Inst']['mode'] in ['wfss', 'ts_grism']) and (not self.params['Output']['grism_source_image'])):
            raise ValueError('Input yaml file has WFSS or TSO grism mode, but Output:grism_source_image is set to False. Must be True.')

    def checkRunStep(self, filename):
        # check to see if a filename exists in the parameter file.
        if ((len(filename) == 0) or (filename.lower() == 'none')):
            return False
        else:
            return True

    def read_pattern_check(self):
        # Check the readout pattern that's entered and set nframe and nskip
        # accordingly
        self.params['Readout']['readpatt'] = self.params['Readout']['readpatt'].upper()

        # Read in readout pattern definition file
        # and make sure the possible readout patterns are in upper case
        self.readpatterns = ascii.read(self.params['Reffiles']['readpattdefs'])
        self.readpatterns['name'] = [s.upper() for s in self.readpatterns['name']]

        # If the requested readout pattern is in the table of options,
        # then adopt the appropriate nframe and nskip
        if self.params['Readout']['readpatt'] in self.readpatterns['name']:
            mtch = self.params['Readout']['readpatt'] == self.readpatterns['name']
            self.params['Readout']['nframe'] = self.readpatterns['nframe'][mtch].data[0]
            self.params['Readout']['nskip'] = self.readpatterns['nskip'][mtch].data[0]
            self.logger.info(('Requested readout pattern {} is valid. '
                              'Using the nframe = {} and nskip = {}'
                              .format(self.params['Readout']['readpatt'],
                                      self.params['Readout']['nframe'],
                                      self.params['Readout']['nskip'])))
        else:
            # If the read pattern is not present in the definition file
            # then quit.
            raise ValueError(("WARNING: the {} readout pattern is not defined in {}."
                              .format(self.params['Readout']['readpatt'],
                                      self.params['Reffiles']['readpattdefs'])))

    def filecheck(self):
        # Make sure the requested input files exist
        # For reference files, assume first that they are located in
        # the directory tree under the directory specified by the MIRAGE_DATA
        # environment variable. If not, assume the input is a full path
        # and check there.
        rlist = [['Reffiles', 'astrometric']]
        plist = [['simSignals', 'psfpath']]
        ilist = [['simSignals', 'pointsource'],
                 ['simSignals', 'galaxyListFile'],
                 ['simSignals', 'extended'],
                 ['simSignals', 'movingTargetList'],
                 ['simSignals', 'movingTargetSersic'],
                 ['simSignals', 'movingTargetExtended'],
                 ['simSignals', 'movingTargetToTrack']]
        # for ref in rlist:
        #    self.ref_check(ref)
        for path in plist:
            self.path_check(path)
        for inp in ilist:
            self.input_check(inp)

    def ref_check(self, rele):
        """
        Check for the existence of the input reference file
        Assume first that the file is in the directory tree
        specified by the MIRAGE_DATA environment variable.

        Parameters:
        -----------
        rele -- Tuple containing the nested keys that point
                to the refrence file of interest. These come
                from the yaml input file

        Reutrns:
        --------
        Nothing
        """
        rfile = self.params[rele[0]][rele[1]]
        if rfile.lower() != 'none':
            c1 = os.path.isfile(rfile)
            if not c1:
                raise FileNotFoundError(("WARNING: Unable to locate the {}, {} "
                                         "input file! Not present in {}"
                                         .format(rele[0], rele[1], rfile)))

    def path_check(self, p):
        """
        Check for the existence of the input path.
        Assume first that the path is in relation to
        the directory tree specified by the MIRAGE_DATA
        environment variable

        Parameters:
        -----------
        p -- Tuple containing the nested keys that point
             to a directory in self.params

        Returns:
        --------
        Nothing
        """
        pth = self.params[p[0]][p[1]]
        c1 = os.path.exists(pth)
        if not c1:
            raise NotADirectoryError(("WARNING: Unable to find the requested path "
                                      "{}. Not present in directory tree specified by "
                                      "the {} environment variable."
                                      .format(pth, self.env_var)))

    def get_surface_brightness_fluxcal(self):
        """Get the conversion value for MJy/sr to ADU/sec from the jwst
        calibration pipeline photom reference file. The value is based
        on the filter and pupil value of the observation.
        """
        photom_data = fits.getdata(self.params['Reffiles']['photom'])
        photom_filters = np.array([elem['filter'] for elem in photom_data])
        photom_pupils = np.array([elem['pupil'] for elem in photom_data])
        photom_values = np.array([elem['photmjsr'] for elem in photom_data])

        good = np.where((photom_filters == self.params['Readout']['filter'].upper()) & (photom_pupils == self.params['Readout']['pupil'].upper()))[0]
        if len(good) > 1:
            raise ValueError("More than one matching row in the photom reference file for {} and {}".format(self.params['Readout']['filter'], self.params['Readout']['pupil']))
        elif len(good) == 0:
            raise ValueError("No matching row in the photom reference file for {} and {}".format(self.params['Readout']['filter'], self.params['Readout']['pupil']))

        surf_bright_fluxcal = photom_values[good[0]]
        return surf_bright_fluxcal

    def set_segmentation_threshold(self):
        """Determine the threshold value to use when determining which pixels
        to include in the segmentation map. Final units for the threshold
        should be ADU/sec, but inputs (in the input yaml file) can be:
        ADU/sec, electrons/sec, or MJy/str
        """
        # First, set the default, in case the input yaml does not have the
        # threshold entry
        self.segmentation_threshold = SEGMENTATION_MIN_SIGNAL_RATE
        segmentation_threshold_units = 'adu/s'

        #Now see if there is an entry in the yaml file
        try:
            self.segmentation_threshold = self.params['simSignals']['signal_low_limit_for_segmap']
            segmentation_threshold_units = self.params['simSignals']['signal_low_limit_for_segmap_units'].lower()
        except KeyError:
            self.logger.info(('simSignals:signal_low_limit_for_segmap and/or simSignals:signal_low_limit_for_segmap_units '
                              'not present in input yaml file. Using the default value of: {} ADU/sec'.format(self.segmentation_threshold)))

        if segmentation_threshold_units not in SUPPORTED_SEGMENTATION_THRESHOLD_UNITS:
            raise ValueError(('Unsupported unit for the segmentation map lower signal limit: {}.\n'
                              'Supported units are: {}'.format(segmentation_threshold_units, SUPPORTED_SEGMENTATION_THRESHOLD_UNITS)))
        if segmentation_threshold_units in ['adu/s', 'adu/sec']:
            pass
        elif segmentation_threshold_units == ['e/s', 'e/sec']:
            self.segmentation_threshold /= self.gain_value
        elif segmentation_threshold_units == ['mjy/sr', 'mjy/str']:
            surface_brightness_fluxcal = self.get_surface_brightness_fluxcal()
            self.segmentation_threshold /= surface_brightness_fluxcal
        elif segmentation_threshold_units in ['erg/cm2/a']:
            self.segmentation_threshold /= self.photflam
        elif segmentation_threshold_units in ['erg/cm2/hz']:
            self.segmentation_threshold /= self.photfnu

    def input_check(self, inparam):
        # Check for the existence of the input file. In
        # this case we do not check the directory tree
        # specified by the MIRAGE_DATA environment variable.
        # This is intended primarily for user-generated inputs like
        # source catalogs
        ifile = self.params[inparam[0]][inparam[1]]
        if ifile.lower() != 'none':
            c = os.path.isfile(ifile)
            if not c:
                raise FileNotFoundError(("WARNING: Unable to locate {} Specified "
                                         "by the {}:{} field in the input yaml file."
                                         .format(ifile, inparam[0], inparam[1])))

    def checkParamVal(self, value, typ, vmin, vmax, default):
        # make sure the input value is a float and between min and max
        try:
            value = float(value)
        except:
            raise ValueError("WARNING: {} for {} is not a float.".format(value, typ))

        if ((value >= vmin) & (value <= vmax)):
            return value
        else:
            self.logger.warning(("ERROR: {} for {} is not within reasonable bounds. "
                                 "Setting to {}".format(value, typ, default)))
            return default

    def determine_intermediate_aperture(self, siaf_info):
        """If we are using one of the Grism time series apertures, then we need
        to pay attention to the intermediate aperture that APT uses. This aperture
        is not listed in the APT pointing file, but the V2, V3 reference values in
        the pointing file refer to the intermediate aperture. This aperture is used
        to place the target at the proper location such that once the grism is placed
        in the beam, the trace will land at the reference location of the aperture
        that is specified in the pointing file. For Mirage's purposes, we need to keep
        track of this intermediate aperture and its V2, V3 reference location so that
        we can create the correct undispersed seed image. The shift from the intermediate
        aperture source locations to the dispersed locations is contained in the
        configuration files in the GRISM_NIRCAM and GRISM_NIRISS repos.

        This function determines what the intermediate aperture name is, if any,
        and gets the associated SIAF information.

        Parameters
        ----------
        siaf_info : pysiaf.Siaf
            Instrument-level SIAF object
        """
        self.use_intermediate_aperture = False
        try:
            if self.params['Readout']['intermediate_aperture'].lower() != 'none':
                self.logger.info(('Grism time series aperture in use. Using the intermediate aperture {} '
                                  'to determine source locations on the detector for the undispersed seed image.'
                                  .format(self.params['Readout']['intermediate_aperture'])))
                self.use_intermediate_aperture = True
        except KeyError as exc:
            # Protect against running older yaml files where the intermediate_aperture field
            # is not present.
            if self.params['Readout']['array_name'] in NIRCAM_LW_GRISMTS_APERTURES:
                self.params['Readout']['intermediate_aperture'] = get_lw_grism_tso_intermeidate_aperture(self.params['Readout']['array_name'])
                self.logger.info(('Readout:intermediate_aperture field is not present in the input yaml file. Since '
                                  'this is a LW channel exposure, Mirage can determine the intermediate aperture and '
                                  'use it going forward.'))
                self.use_intermediate_aperture = True

            elif self.params['Readout']['array_name'] in NIRCAM_SW_GRISMTS_APERTURES:
                self.logger.error('Readout:intermediate_aperture field is not present in the input yaml file.')
                exc.args = ((('Readout:intermediate_aperture field is not present in the input yaml file. '
                                  'For SW channel simulations that use grism time series apertures, there is no '
                                  'way to determine the intermediate aperture without this yaml entry. Please add '
                                  'this field to your yaml file with the appropriate '
                                  'aperture name, or re-run the yaml_generator with the latest version of Mirage.')), )
                raise exc

        if self.use_intermediate_aperture:
            self.intermediate_siaf = siaf_info[self.params['Readout']['intermediate_aperture']]
            self.intermediate_local_roll, self.intermediate_attitude_matrix, _, \
            _ = siaf_interface.get_siaf_information(siaf_info,
                                                    self.params['Readout']['intermediate_aperture'],
                                                    self.ra, self.dec,
                                                    self.params['Telescope']['rotation'])

    def read_distortion_reffile(self):
        """Read in the CRDS-format distortion reference file and save
        the coordinate transformation model
        """
        coord_transform = None
        if self.runStep['astrometric']:
            with asdf.open(self.params['Reffiles']['astrometric']) as dist_file:
                coord_transform = dist_file.tree['model']
        # else:
        #    coord_transform = self.simple_coord_transform()
        return coord_transform

    def saveSingleFits(self, image, name, key_dict=None, image2=None, image2type=None):
        # Save an array into the first extension of a fits file
        h0 = fits.PrimaryHDU()
        h1 = fits.ImageHDU(image, name='DATA')
        if image2 is not None:
            h2 = fits.ImageHDU(image2)
            if image2type is not None:
                h2.header['EXTNAME'] = image2type

        # if a keyword dictionary is provided, put the
        # keywords into the 0th and 1st extension headers
        if key_dict is not None:
            for key in key_dict:
                h0.header[key] = key_dict[key]
                h1.header[key] = key_dict[key]

        if image2 is None:
            hdulist = fits.HDUList([h0, h1])
        else:
            hdulist = fits.HDUList([h0, h1, h2])
        hdulist.writeto(name, overwrite=True)

    def add_options(self, parser=None, usage=None):
        if parser is None:
            parser = argparse.ArgumentParser(usage=usage, description='Create seed image via catalogs')
        parser.add_argument("paramfile", help=('File describing the input parameters and instrument '
                                               'settings to use. (YAML format).'))
        parser.add_argument("--param_example", help='If used, an example parameter file is output.')
        return parser


if __name__ == '__main__':

    usagestring = 'USAGE: catalog_seed_image.py inputs.yaml'

    seed = Catalog_seed()
    parser = seed.add_options(usage=usagestring)
    args = parser.parse_args(namespace=seed)
    seed.make_seed()
