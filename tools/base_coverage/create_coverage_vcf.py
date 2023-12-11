import gzip
import os
import json
import sys
import pysam
import argparse
import numpy as np
import collections
import logging
from contextlib import closing

argparser = argparse.ArgumentParser(description = 'Generate BRAVO coverage file from variant VCF. The coverage for base pair positions between two consequitive variants in VCF is inferred using linear interpolation.')
argparser.add_argument('-i', '--input', metavar = 'file', dest = 'vcf_file',  type = str, required = True, help = 'Name of the input VCF file. VCF file must contain AVGDP INFO field.')
args = argparser.parse_args()

# try to read chr, position, and AVGDP from vcf files
def read_avdp(filename):
    with pysam.VariantFile(filename) as ifile:
        if not 'AVGDP' in ifile.header.info:
            raise Exception(f'Missing AVGDP INFO field meta-information.')
        prev_position = None
        prev_avgdp = None
        for record in ifile.fetch():
            chromosome = record.chrom
            chromosome_1 = chromosome.replace('chr', '')
            position = record.pos
            depths = record.info['AVGDP']
            if prev_position is None:
                # output
                sys.stdout.write('%s\t%d\t%d\t{"chrom":"%s","start":%d,"end":%d,"mean":%g' % (chromosome_1, position, position, chromosome, position, position, np.mean(depths)))
                sys.stdout.write('}\n')
                prev_position = position
                prev_avgdp = depths
            elif position == prev_position:
                continue
            else:
                if position == prev_position + 1:
                    sys.stdout.write('%s\t%d\t%d\t{"chrom":"%s","start":%d,"end":%d,"mean":%g' % (chromosome_1, position, position, chromosome, position, position, np.mean(depths)))
                    sys.stdout.write('}\n')
                    prev_position = position
                    prev_avgdp = depths
                else:
                    for i in range(position - prev_position):
                        depths_add = (depths - prev_avgdp)/(position - prev_position)
                        sys.stdout.write('%s\t%d\t%d\t{"chrom":"%s","start":%d,"end":%d,"mean":%g' % (chromosome_1, prev_position+i+1, prev_position+i+1, chromosome, prev_position+i+1, prev_position+i+1, np.mean((i+1)*depths_add+prev_avgdp)))
                        sys.stdout.write('}\n')
                    prev_position = position
                    prev_avgdp = depths



read_avdp(args.vcf_file)
