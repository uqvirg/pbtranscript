#!/usr/bin/env python

"""
For calls 'ice_partial.py one' to process all
PartialChunkTask objects in input pickle.
"""
import os.path as op
import logging
import sys

from pbcore.io import ConsensusReadSet
from pbcommand.cli import pbparser_runner
from pbcommand.utils import setup_log
from pbcommand.models import FileTypes

from pbtranscript.ice.IcePartial import IcePartialOne
from pbtranscript.PBTranscriptOptions import (BaseConstants,
                                              get_base_contract_parser)
from pbtranscript.tasks.TPickles import PartialChunkTask, ChunkTasksPickle

log = logging.getLogger(__name__)


class Constants(BaseConstants):
    """Constants used in pbtranscript.tasks.ice_partial_cluster_bins"""
    TOOL_ID = "pbtranscript.tasks.ice_partial_cluster_bins"
    DRIVER_EXE = "python -m %s --resolved-tool-contract " % TOOL_ID
    PARSER_DESC = __doc__


def get_contract_parser():
    """Tool contract should have the following inputs and outputs.
    input:
        idx 0: partial_chunks.pickle
        idx 1: ccs
        idx 2: sentinel txt file
    output
        idx 0 - partial_chunk_done.txt, a sential file which does nothing
                but connect ice_partial and the subsequence task
                combine_ice_partial
    """
    p = get_base_contract_parser(Constants, default_level="DEBUG")
    p.add_input_file_type(FileTypes.PICKLE, "partial_chunks_pickle", "Pickle In",
                          "Partial chunks pickle file") # input 0
    p.add_input_file_type(FileTypes.TXT, "partial_sentinel_in", "Sentinel In",
                          "Setinel file") # input 1
    p.add_input_file_type(FileTypes.DS_CCS, "ccs_in", "ConsensusReadSet In",
                          "PacBio ConsensusReadSet") # input 2
    p.add_output_file_type(FileTypes.TXT, "partial done txt",
                           name="Partial Done Txt file",
                           description="Partial Done Txt file.",
                           default_name="partial_chunks_done")
    return p


def args_runner(args):
    """args runner"""
    raise NotImplementedError()

def task_runner(task, ccs_file, nproc, tmp_dir):
    """Given PartialChunkTask, run"""
    assert isinstance(task, PartialChunkTask)
    assert op.exists("%s.sensitive.config" % task.consensus_isoforms_file)
    return IcePartialOne(input_fasta=task.nfl_file,
                         ref_fasta=task.consensus_isoforms_file,
                         ccs_fofn=ccs_file,
                         out_pickle=task.nfl_pickle,
                         blasr_nproc=nproc,
                         tmp_dir=tmp_dir).run()


def resolved_tool_contract_runner(rtc):
    """Given resolved tool contract, run"""
    p = ChunkTasksPickle.read(rtc.task.input_files[0])
    assert all([isinstance(task, PartialChunkTask) for task in p])
    dummy_sentinel_file = rtc.task.input_files[1]
    ccs_file = rtc.task.input_files[2]
    nproc = rtc.task.nproc
    tmp_dir = rtc.task.tmpdir_resources[0].path \
            if len(rtc.task.tmpdir_resources) > 0 else None

    log.info("Looking for QVs in CCS input...")
    with ConsensusReadSet(ccs_file) as ds:
        for bam in ds.resourceReaders():
            qvs = bam.pulseFeaturesAvailable()
            if qvs != set(['SubstitutionQV', 'InsertionQV', 'DeletionQV']):
                log.warn("Missing QV fields from %s, will use default probabilities",
                         bam.filename)
                ccs_file = None
                break

    with open(rtc.task.output_files[0], 'w') as writer:
        for task in p:
            log.info("Running ice_partial on cluster bin %s, nfl chunk %s/%s",
                     str(task.cluster_bin_index),
                     str(task.nfl_index), str(task.n_nfl_chunks))
            task_runner(task=task, ccs_file=ccs_file, nproc=nproc, tmp_dir=tmp_dir)
            writer.write("ice_partial of cluster bin %s, nfl chunk %s/%s in %s is DONE: %s\n" %
                         (task.cluster_bin_index, task.nfl_index, task.n_nfl_chunks,
                          task.cluster_out_dir, task.nfl_pickle))


def main():
    """main"""
    mp = get_contract_parser()
    return pbparser_runner(
        argv=sys.argv[1:],
        parser=mp,
        args_runner_func=args_runner,
        contract_runner_func=resolved_tool_contract_runner,
        alog=log,
        setup_log_func=setup_log)


if __name__ == "__main__":
    sys.exit(main())
