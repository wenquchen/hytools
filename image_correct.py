import json
import os
import warnings
import sys
import ray
import numpy as np

import time


import hytools as ht
from hytools.io.envi import *
from hytools.topo import calc_topo_coeffs
from hytools.brdf import calc_brdf_coeffs
from hytools.masks import mask_create


warnings.filterwarnings("ignore")
np.seterr(divide='ignore', invalid='ignore')

def main():

    time_start = time.perf_counter() #process_time_ns()

    config_file = sys.argv[1]

    with open(config_file, 'r') as outfile:
        config_dict = json.load(outfile)

    images= config_dict["input_files"]

    if ray.is_initialized():
        ray.shutdown()
    print("Using %s CPUs." % config_dict['num_cpus'])
    ray.init(num_cpus = config_dict['num_cpus'])

    HyTools = ray.remote(ht.HyTools)
    actors = [HyTools.remote() for image in images]

    if config_dict['file_type'] == 'envi':
        anc_files = config_dict["anc_files"]
        _ = ray.get([a.read_file.remote(image,config_dict['file_type'],
                                        anc_files[image]) for a,image in zip(actors,images)])
    elif config_dict['file_type'] == 'neon':
        _ = ray.get([a.read_file.remote(image,config_dict['file_type']) for a,image in zip(actors,images)])

    #Here is where the outlier detection should probably happen.

    _ = ray.get([a.create_bad_bands.remote(config_dict['bad_bands']) for a in actors])

    for correction in config_dict["corrections"]:
        if correction =='topo':
            time_topo_start = time.perf_counter() #process_time_ns()
            calc_topo_coeffs(actors,config_dict['topo'])
            time_topo_end = time.perf_counter() #process_time_ns()
            print("TOPO Time: {} sec.".format(time_topo_end - time_topo_start))
        elif correction == 'brdf':
            time_brdf_start = time.perf_counter() #.process_time_ns()
            calc_brdf_coeffs(actors,config_dict)
            time_brdf_end = time.perf_counter() #.process_time_ns()
            print("BRDF Time: {} sec.".format(time_brdf_end - time_brdf_start))
        elif correction == 'unsmooth':
            load_unsmooth(actors)

    if config_dict['export']['coeffs'] and len(config_dict["corrections"]) > 0:
        print("Exporting correction coefficients.")
        _ = ray.get([a.do.remote(export_coeffs,config_dict['export']) for a in actors])

    #print('image_correct.py Ln59:',config_dict["corrections"])
    time_export_start = time.perf_counter() #process_time_ns()
    if config_dict['export']['image']:
        print("Exporting corrected images.")
        _ = ray.get([a.do.remote(apply_corrections,config_dict) for a in actors])
    time_export_end = time.perf_counter() #process_time_ns()
    print("{} Export Time: {} sec.".format(images[0],time_export_end - time_export_start))


    ray.shutdown()

    time_end = time.perf_counter() #process_time_ns()
    print("Total Time: {} sec.".format(time_end - time_start))

def export_coeffs(hy_obj,export_dict):
    '''Export correction coefficients to file.
    '''
    for correction in hy_obj.corrections:
        if correction=='unsmooth':
            continue

        coeff_file = export_dict['output_dir']
        coeff_file += os.path.splitext(os.path.basename(hy_obj.file_name))[0]
        coeff_file += "_%s_coeffs_%s.json" % (correction,export_dict["suffix"])

        with open(coeff_file, 'w') as outfile:
            if correction == 'topo':
                corr_dict = hy_obj.topo
            else:
                corr_dict = hy_obj.brdf
            json.dump(corr_dict,outfile)

def apply_corrections(hy_obj,config_dict):
    '''Apply correction to image and export
        to file.
    '''

    header_dict = hy_obj.get_header()
    header_dict['data ignore value'] = hy_obj.no_data
    header_dict['data type'] = 4

    output_name = config_dict['export']['output_dir']
    output_name += os.path.splitext(os.path.basename(hy_obj.file_name))[0]
    output_name +=  "_%s" % config_dict['export']["suffix"]

    #Export all wavelengths
    if len(config_dict['export']['subset_waves']) == 0:

        if config_dict["resample"] == True:
            hy_obj.resampler = config_dict['resampler']
            waves= hy_obj.resampler['out_waves']
        else:
            waves = hy_obj.wavelengths

        header_dict['bands'] = len(waves)
        header_dict['wavelength'] = waves
        header_dict['fwhm'] = hy_obj.fwhm

        writer = WriteENVI(output_name,header_dict)
        iterator = hy_obj.iterate(by = 'line', corrections = hy_obj.corrections,
                                  resample = config_dict['resample'])
        while not iterator.complete:
            line = iterator.read_next()
            writer.write_line(line,iterator.current_line)
        writer.close()

    #Export subset of wavelengths
    else:
        waves = config_dict['export']['subset_waves']
        bands = [hy_obj.wave_to_band(x) for x in waves]
        waves = [round(hy_obj.wavelengths[x],2) for x in bands]
        header_dict['bands'] = len(bands)
        header_dict['wavelength'] = waves
        header_dict['fwhm'] = [hy_obj.fwhm[x] for x in bands]

        writer = WriteENVI(output_name,header_dict)
        for b,band_num in enumerate(bands):
            #print('image_correct.py Ln 123',hy_obj.corrections)
            band = hy_obj.get_band(band_num,
                                   corrections = hy_obj.corrections)
            writer.write_band(band, b)
        writer.close()

    #Export masks
    # does not work for precomputed json coeffs
    if (config_dict['export']['masks']) and (len(config_dict["corrections"]) > 0):
        masks = []
        mask_names = []

        for correction in config_dict["corrections"]:
            if correction=='unsmooth':
                continue
            #for mask_type in config_dict[correction]['calc_mask']:
            for mask_type in config_dict[correction]['apply_mask']:
                mask_names.append(correction + '_' + mask_type[0])
                masks.append(mask_create(hy_obj, [mask_type]))

        header_dict['data type'] = 1
        header_dict['bands'] = len(masks)
        header_dict['band names'] = mask_names
        header_dict['samples'] = hy_obj.columns
        header_dict['lines'] = hy_obj.lines
        header_dict['wavelength'] = []
        header_dict['fwhm'] = []
        header_dict['wavelength units'] = ''
        header_dict['data ignore value'] = 255


        output_name = config_dict['export']['output_dir']
        output_name += os.path.splitext(os.path.basename(hy_obj.file_name))[0]
        output_name +=  "_%s_mask" % config_dict['export']["suffix"]

        writer = WriteENVI(output_name,header_dict)

        for band_num,mask in enumerate(masks):
            mask =mask.astype(int)
            mask[~hy_obj.mask['no_data']] = 255
            writer.write_band(mask,band_num)

        del masks



if __name__== "__main__":
    main()
