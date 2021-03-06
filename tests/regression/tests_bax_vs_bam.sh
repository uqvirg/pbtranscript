#!/bin/bash

BAM_OUTDIR=`pwd`/test_bam_in
BAX_BAM_OUTDIR=/pbi/dept/secondary/siv/testdata/pbtranscript-unittest/data/regression/test_bax_in/

#rm -rf ${BAM_OUTDIR}
rm -f results.txt

# * Run pbtranscript on the rat_bax1 data using the current 
#   build taking bam as input.
source run_isoseq_bam_in.sh $BAM_OUTDIR

# * Compare with isoseq 2.3 results on the same dataset.
#   taking bax.h5 as input.
echo Compare isoseq runs:
echo v3.0 taking bam as input: $BAM_OUTDIR
echo v2.3 taking bax.h5 as input: $BAX_BAM_OUTDIR
rm -f results.txt
python -m pbtranscript.testkit.compare_isoseq_runs $BAM_OUTDIR $BAX_BAM_OUTDIR > results.txt
echo $?
