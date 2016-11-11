#!/usr/bin/env python

"""
Polish consensus isoforms created by ICE, using
quiver for RS2 data, and Arrow for Sequel data.
"""

import os.path as op
import logging
import shutil
import cPickle
import json
from math import ceil
from collections import defaultdict

from pbtranscript.ClusterOptions import IceQuiverOptions
from pbtranscript.PBTranscriptOptions import  add_fofn_arguments, \
    add_sge_arguments, add_cluster_root_dir_as_positional_argument
from pbtranscript.Utils import mkdir, real_upath, nfs_exists, \
    get_files_from_file_or_fofn, guess_file_format, FILE_FORMATS, \
    use_samtools_v_1_3_1
from pbtranscript.ice.IceUtils import get_the_only_fasta_record, \
    is_blank_sam, concat_sam, blasr_for_quiver, trim_subreads_and_write, \
    is_blank_bam, concat_bam
from pbtranscript.ice.IceFiles import IceFiles
from pbtranscript.io import MetaSubreadFastaReader, BamCollection, \
    FastaRandomReader
from pbcore.io import FastaWriter


class IceQuiver(IceFiles):

    """Ice Quiver."""

    desc = "After assigning all non-full-length reads to unpolished " + \
           "consensus isoforms created by ICE, polish these consensus " + \
           "isoforms, using quiver for RS2 data and arrow for Sequel data."

    def __init__(self, root_dir, bas_fofn, fasta_fofn, sge_opts,
                 tmp_dir=None, prog_name=None):
        # Initialize super class IceFiles.
        prog_name = "IceQuiver" if prog_name is None else prog_name
        super(IceQuiver, self).__init__(prog_name=prog_name,
                                        root_dir=root_dir, bas_fofn=bas_fofn,
                                        fasta_fofn=fasta_fofn, tmp_dir=tmp_dir)
        self.sge_opts = sge_opts
        self.use_samtools_v_1_3_1 = use_samtools_v_1_3_1()

    def validate_inputs(self):
        """Validate input fofns, and root_dir, log_dir, tmp_dir,
        create quivered_dir and quivered_log_dir"""
        self.add_log("Validating inputs.")

        # Create directories: root_dir/quivered and root_dir/log_dir/quivered
        try:
            mkdir(self.quivered_dir)
            mkdir(self.quivered_log_dir)
        except OSError:
            # Multiple ice_quiver_i jobs may run at the same time and try to
            # mkdir, race condition may happen, so ignore OSError here.
            pass

        errMsg = ""

        if not nfs_exists(self.log_dir) or not op.isdir(self.log_dir):
            errMsg = "Log dir {l} is not an existing directory.".\
                format(l=self.log_dir)
        elif self.bas_fofn is None:
            errMsg = "Please specify subreads file (e.g., --bas_fofn=input.fofn|subreadset.xml)."
        elif not nfs_exists(self.bas_fofn):
            errMsg = "Specified subreads file (bas_fofn={f}) does not exist.".format(f=self.bas_fofn)
        elif not nfs_exists(self.nfl_all_pickle_fn):
            #"output/map_noFL/noFL.ALL.partial_uc.pickle"):
            errMsg = "Pickle file {f} ".format(f=self.nfl_all_pickle_fn) + \
                     "which assigns all non-full-length reads to isoforms " + \
                     "does not exist. Please check 'ice_partial.py *' are " + \
                     "all done."
        elif not nfs_exists(self.final_pickle_fn):
            errMsg = "Pickle file {f} ".format(f=self.final_pickle_fn) + \
                     "which assigns full-length non-chimeric reads to " + \
                     "isoforms does not exist."

        if self.bas_fofn is not None and \
            guess_file_format(self.bas_fofn) is not FILE_FORMATS.BAM:
            # No need to convert subreads.bam to fasta
            if self.fasta_fofn is None:
                errMsg = "Please make sure ice_make_fasta_fofn has " + \
                         "been called, and specify fasta_fofn."
            elif not nfs_exists(self.fasta_fofn):
                errMsg = "Input fasta_fofn {f} does not exists.".\
                         format(f=self.fasta_fofn)
                fasta_files = get_files_from_file_or_fofn(self.fasta_fofn)
                for fasta_file in fasta_files:
                    if not nfs_exists(fasta_file):
                        errMsg = "A file {f} in fasta_fofn does not exist.".\
                                 format(f=fasta_file)

        if errMsg != "":
            self.add_log(errMsg, level=logging.ERROR)
            raise IOError(errMsg)

    def _quivered_bin_prefix(self, first, last):
        """Return $quivered_dir/c{first}to{last}"""
        return self.quivered_dir + "/c{first}to{last}".format(
            first=first, last=last)

    def sam_of_quivered_bin(self, first, last):
        """Return $_quivered_bin_prefix.sam"""
        return self._quivered_bin_prefix(first, last) + ".sam"

    def bam_of_quivered_bin(self, first, last, is_sorted=False):
        """
        Return $_quivered_bin_prefix.unsorted.bam if not sorted;
        return $_quivered_bin_prefix.bam if sorted.
        """
        if not is_sorted:
            return self._quivered_bin_prefix(first, last) + ".unsorted.bam"
        else:
            return self._quivered_bin_prefix(first, last) + ".bam"

    def ref_fa_of_quivered_bin(self, first, last):
        """Return $_quivered_bin_prefix.ref.fasta
        this is reference fasta for quiver to use as input.
        """
        return self._quivered_bin_prefix(first, last) + ".ref.fasta"

    def cmph5_of_quivered_bin(self, first, last):
        """Return $_quivered_bin_prefix.cmp.h5"""
        return self._quivered_bin_prefix(first, last) + ".cmp.h5"

    def fq_of_quivered_bin(self, first, last):
        """Return $_quivered_bin_prefix.quivered.fastq
        this is quivered fq output. Whenever this is changed, change
        IceQuiverPostprocess accordingly.
        """
        return self._quivered_bin_prefix(first, last) + ".quivered.fastq"

    def script_of_quivered_bin(self, first, last):
        """Return $_quivered_bin_prefix.sh"""
        return self._quivered_bin_prefix(first, last) + ".sh"

    def reconstruct_ref_fa_for_clusters_in_bin(self, cids, refs):
        """
        Reconstruct ref_fa of the cluster in the new tmp_dir
        e.g.,
            self.g_consensus_ref_fa_of_cluster(cid)

        cids --- list[int(cid)], e.g., [10, 11, 12, ..., 20]
        refs --- dict{int(cid): ref_fa of cluster(cid)}
        """
        # Check existence when first time it is read.
        if not nfs_exists(self.final_consensus_fa):
            raise IOError("Final consensus FASTA file {f}".format(
                f=self.final_consensus_fa) + "does not exist.")

        self.add_log("Reconstructing g consensus files for clusters "
                     "[%d, %d] in %s" % (cids[0], cids[-1], self.tmp_dir),
                     level=logging.INFO)

        final_consensus_d = FastaRandomReader(self.final_consensus_fa)
        for ref_id in final_consensus_d.d.keys():
            cid = int(ref_id.split('/')[0].replace('c', ''))
            # e.g., ref_id = c103/1/3708, cid = 103,
            #       refs[cid] = ...tmp/0/c103/g_consensus_ref.fasta
            if cid in cids:
                mkdir(self.cluster_dir(cid))
                ref_fa = op.join(self.cluster_dir(cid),
                                 op.basename(refs[cid]))
                refs[cid] = ref_fa
                with FastaWriter(ref_fa) as writer:
                    self.add_log("Writing ref_fa %s" % refs[cid])
                    writer.writeRecord(ref_id,
                                       final_consensus_d[ref_id].sequence[:])

        self.add_log("Reconstruct of g consensus files completed.",
                     level=logging.INFO)

    def create_raw_files_for_clusters_in_bin(self, cids, d, uc, partial_uc,
                                             bam=False):
        """
        Create raw subreads fasta/bam files for clusters in cids.
        For each cluster k in cids,
        * Collect raw subreads of zmws associated with cluster k
          in either uc or partial_uc.

        cids --- cluster ids
        d --- MetaSubreadFastaReader or BamCollection
        uc --- uc[k] returns fl ccs reads associated with cluster k
        partial_uc --- partial_uc[k] returns nfl ccs reads associated with cluster k
        """

        file_func = self.raw_bam_of_cluster if bam \
                    else self.raw_fa_of_cluster

        for k in cids:  # for each cluster k
            # write cluster k's associated raw subreads to raw_fa
            # Trim both ends of subreads (which contain primers and polyAs)
            trim_subreads_and_write(reader=d,
                                    in_seqids=uc[k] + partial_uc[k],
                                    out_file=file_func(k),
                                    trim_len=IceQuiverOptions.trim_subread_flank_len,
                                    min_len=IceQuiverOptions.min_trimmed_subread_len,
                                    ignore_keyerror=True,
                                    bam=bam)

    def create_sams_for_clusters_in_bin(self, cids, refs, bam=False):
        """
        Create sam files for clusters in cids.
        For each cluster k in cids,
        * Call blasr to align its associated subreads to its consensus
          sequence as reference.

        cids --- cluster ids
        refs --- refs[k] -> consensus seq of cluster k

        This function has to be called after raw_fa_of_cluster for clusters
        in cids are created.

        """
        raw_file_func  = self.raw_fa_of_cluster if not bam else \
                         self.raw_bam_of_cluster
        out_file_func = self.sam_of_cluster if not bam else \
                        self.bam_of_cluster

        for k in cids:  # for each cluster k

            # $root_dir/tmp/?/c{k}/in.raw_with_partial.fasta
            raw_fn = raw_file_func(k)
            out_fn = out_file_func(k)

            if not op.exists(raw_fn):
                raise IOError("{f} does not exist. ".format(f=raw_fn) +
                              "Please check raw subreads of this bin is created.")
            blasr_for_quiver(
                query_fn=raw_fn,
                ref_fasta=refs[k],
                out_fn=out_fn,
                bam=bam,
                run_cmd=True,
                blasr_nproc=self.sge_opts.blasr_nproc)


    def concat_valid_sams_and_refs_for_bin(self, cids, refs, bam=False):
        """
        Concat sam files and reference sequences of all valid clusters
        in bin to create a big sam and a big ref.
        A cluser is not valid if (1) or (2)
            (1) identical sequences already exists in another cluster
                (rare, but happens)
            (2) the alignment is empty (also rare, but happens)
        Return valid_cids, a list of valid cluster ids
        """
        first, last = cids[0], cids[-1]
        bin_ref_fa = self.ref_fa_of_quivered_bin(first, last)

        bin_sam_file = self.sam_of_quivered_bin(first, last)
        file_func  = self.sam_of_cluster
        is_blank_file = is_blank_sam
        concat_sambam = concat_sam

        if bam:
            bin_sam_file = self.bam_of_quivered_bin(first, last)
            file_func  = self.bam_of_cluster
            is_blank_file = is_blank_bam
            concat_sambam = concat_bam

        self.add_log("Concatenating reference files between " +
                     "{first} and {last}.".format(first=first, last=last))
        valid_sam_files = []
        valid_cids = []
        seqs_seen = {}
        with open(bin_ref_fa, 'w') as bin_ref_fa_writer:
            for cid in cids:
                fname = file_func(cid)
                if not is_blank_file(fname):
                    ref_rec = get_the_only_fasta_record(refs[cid])
                    name = ref_rec.name.strip()
                    seq = ref_rec.sequence.strip()
                    if seq not in seqs_seen:
                        valid_sam_files.append(fname)
                        valid_cids.append(cid)
                        seqs_seen[seq] = cid
                        # concate valid ref files, avoid 'cat ...' hundreds
                        # or even thousands of files due to linux cmd line
                        # length limits
                        bin_ref_fa_writer.write(">{0}\n{1}\n".
                                                format(name, seq))
                    else:
                        self.add_log("ignoring {0} because identical " +
                                     "sequence!".format(cid))
                else:
                    self.add_log(
                        "ignoring {0} because no alignments!".format(cid))

        if len(valid_sam_files) == 0:
            self.add_log("No alignments were found for clusters between " +
                         "{first} and {last}.".format(first=first, last=last),
                         level=logging.WARNING)
            assert(len(valid_cids) == 0)
        else:
            self.add_log("Concatenating sam files between " +
                         "{first} and {last}.".format(first=first, last=last))
            # concat valid sam files
            concat_sambam(valid_sam_files, bin_sam_file)
            self.add_log("Concatenation done")

        return valid_cids

    def quiver_cmds_for_bin(self, cids, quiver_nproc=2, bam=False):
        """
        Return a list of quiver related cmds. Input format can be FASTA or BAM.
        If inputs are in FASTA format, call samtoh5, loadPulses, comph5tools.py,
        samtools, loadChemistry, quiver...
        If inputs are in BAM format, call quiver directly.
        """
        first, last = cids[0], cids[-1]
        self.add_log("Creating quiver cmds for c{first} to c{last}".
                     format(first=first, last=last))

        bin_ref_fa = self.ref_fa_of_quivered_bin(first, last)
        bin_sam_file = self.sam_of_quivered_bin(first, last)
        bin_cmph5 = self.cmph5_of_quivered_bin(first, last)
        bin_fq = self.fq_of_quivered_bin(first, last)

        bin_unsorted_bam_file = self.bam_of_quivered_bin(first, last, is_sorted=False)
        bin_bam_file = self.bam_of_quivered_bin(first, last, is_sorted=True)
        bin_bam_prefix = self._quivered_bin_prefix(first, last)

        quiver_input = bin_cmph5 if not bam else bin_bam_file

        cmds = []
        if not bam:
            raise IOError("conversion to cmp.h5 no longer supported")
            cmds.append("samtoh5 {sam} {ref} {cmph5} -smrtTitle".format(
                sam=real_upath(bin_sam_file),
                ref=real_upath(bin_ref_fa),
                cmph5=real_upath(bin_cmph5)))
            cmds.append("gzip {sam}".format(sam=real_upath(bin_sam_file)))
            metrics = ["QualityValue", "InsertionQV", "MergeQV", "DeletionQV",
                       "DeletionTag", "SubstitutionTag", "SubstitutionQV"]
            cmds.append("loadPulses {bas_fofn} ".
                        format(bas_fofn=real_upath(self.bas_fofn)) +
                        "{cmph5} ".format(cmph5=real_upath(bin_cmph5)) +
                        "-byread -metrics " + ",".join(metrics))
            cmds.append("cmph5tools.py sort {cmph5}".
                        format(cmph5=real_upath(bin_cmph5)))
            cmds.append("loadChemistry.py {bas_fofn} {cmph5}".
                        format(bas_fofn=real_upath(self.bas_fofn),
                               cmph5=real_upath(bin_cmph5)))
        else:
            if not self.use_samtools_v_1_3_1:
                # SA2.*, SA3.0, SA3.1 and SA3.2 use v0.1.19
                cmds.append("samtools sort {f} {d}".format(
                    f=real_upath(bin_unsorted_bam_file),
                    d=real_upath(bin_bam_prefix)))
            else:
                # SA3.3 and up use v1.3.1
                cmds.append("samtools sort {f} -o {d}.bam".format(
                    f=real_upath(bin_unsorted_bam_file),
                    d=real_upath(bin_bam_prefix)))

            cmds.append("samtools index {f}".format(f=real_upath(bin_bam_file)))

        cmds.append("samtools faidx {ref}".format(ref=real_upath(bin_ref_fa)))
        cmds.append("pbindex {f}".format(f=real_upath(bin_bam_file)))
        cmds.append("variantCaller --algorithm=best " +
                    "{f} ".format(f=real_upath(quiver_input)) +
                    "--verbose -j{n} ".format(n=quiver_nproc) +
                    "--referenceFilename={ref} ".format(ref=real_upath(bin_ref_fa)) +
                    "-o {fq}".format(fq=real_upath(bin_fq)))
        return cmds

    def create_quiver_sh_for_bin(self, cids, cmds):
        """
        Write quiver cmds to a bash script, e.g., quivered/c{}to{}.sh,
        return script file path.
        """
        first, last = cids[0], cids[-1]
        bin_sh = self.script_of_quivered_bin(first, last)
        self.add_log("Creating quiver bash script {f} for c{first} to c{last}.".
                     format(f=bin_sh, first=first, last=last))
        with open(bin_sh, 'w') as f:
            f.write("#!/bin/bash\n")
            f.write("\n".join(cmds))

        return bin_sh

    def submit_todo_quiver_jobs(self, todo, submitted, sge_opts):
        """
        todo --- a list of sh scripts to run
        submitted --- a list of sh scripts which have been submitted
        sge_opts --- SGE options, including
                     use_sge, whether or not to use sge
                     max_sge_jobs, maximum number sge jobs to submit
                     quiver_nproc, number of nproc per job
                     unique_id, unique id to name qsub jobs
        """
        self.add_log("Submitting todo quiver jobs.")
        if sge_opts.use_sge is not True or \
           sge_opts.max_sge_jobs == 0:  # don't use SGE
            for job in todo:
                elog = op.join(self.quivered_log_dir,
                               op.basename(job) + ".elog")
                olog = op.join(self.quivered_log_dir,
                               op.basename(job) + ".olog")
                cmd = "bash " + real_upath(job) + " 1>{olog} 2>{elog}".\
                      format(olog=real_upath(olog), elog=real_upath(elog))
                self.run_cmd_and_log(cmd, olog=olog, elog=elog,
                                     description="Failed to run Quiver")
                submitted.append(("local", job))
            todo = []
        else:
            while len(todo) > 0:
                n = min(sge_opts.max_sge_jobs, len(todo))
                for job in todo[:n]:
                    # ex: Your job 8613116 ("c20to70.sh") has been submitted
                    elog = op.join(self.quivered_log_dir,
                                   op.basename(job) + ".elog")
                    olog = op.join(self.quivered_log_dir,
                                   op.basename(job) + ".olog")
                    jid = "ice_quiver_{unique_id}_{name}".format(
                        unique_id=self.sge_opts.unique_id,
                        name=op.basename(job))
                    qsub_cmd = "qsub " + \
                               "-pe smp {n} ".\
                               format(n=sge_opts.quiver_nproc) + \
                               "-cwd -S /bin/bash -V " + \
                               "-e {elog} ".format(elog=real_upath(elog)) +\
                               "-o {olog} ".format(olog=real_upath(olog)) +\
                               "-N {jid} ".format(jid=jid) + \
                               "{job}".format(job=real_upath(job))
                    job_id = self.qsub_cmd_and_log(qsub_cmd)

                    submitted.append((job_id, job))
                    todo.remove(job)
                # end of for job in todo[:n]
            # end of while len(todo) > 0
        # end of else (use sge)

    def create_a_quiver_bin(self, cids, d, uc, partial_uc, refs, sge_opts):
        """Put clusters in cids together into a bin. In order to polish
        consensus of clusters in the bin, prepare inputs and create a quiver
        bash script to run later.

        (1) For each cluster k in cids, obtain subreads of all zmws
            belonging to this cluster, and save in raw_fa_of_cluster(k)
        (2) For each cluster k in cids, call blasr to align raw_fa_of_cluster to
            its consensus sequence and create sam_of_cluster(k).
        (3) Concat all sam files of `valid` clusters to sam_of_quivered_bin, and
            concat ref seqs of all `valid` clusters to ref_fa_of_quivered_bin
        (4) Make commands including
                samtoh5, loadPulses, cmph5tools.py, loadChemistry, ..., quiver
            in order to convert sam_of_quivered_bin to cmph5_of_quivered_bin.
            Write these commands to script_of_quivered_bin
              * qsub all jobs later when scripts of all quivered bins are done.
              * or execute scripts sequentially on local machine
        """
        if not isinstance(d, BamCollection) and \
           not isinstance(d, MetaSubreadFastaReader):
            raise TypeError("%s.create_a_quiver_bin, does not support %s" %
                            (self.__class__.__name__, type(d)))

        self.add_log("Creating a quiver job bin for clusters "
                     "[%s, %s]" % (cids[0], cids[-1]), level=logging.INFO)

        bam = True if isinstance(d, BamCollection) else False

        # For each cluster in bin, create its raw subreads fasta file.
        self.create_raw_files_for_clusters_in_bin(cids=cids, d=d, uc=uc,
                                                  partial_uc=partial_uc,
                                                  bam=bam)

        # For each cluster in bin, align its raw subreads to ref to build a sam
        self.create_sams_for_clusters_in_bin(cids=cids, refs=refs, bam=bam)

        # Concatenate sam | ref files of 'valid' clusters in this bin to create
        # a big sam | ref file.
        valid_cids = self.concat_valid_sams_and_refs_for_bin(cids=cids,
                                                             refs=refs,
                                                             bam=bam)

        # quiver cmds for this bin
        cmds = []
        if len(valid_cids) != 0:
            cmds = self.quiver_cmds_for_bin(cids=cids,
                                            quiver_nproc=sge_opts.quiver_nproc,
                                            bam=bam)
        else:
            cmds = ["echo no valid clusters in this bin, skip..."]

        # Write quiver cmds for this bin to $root_dir/quivered/c{}_{}.sh
        return self.create_quiver_sh_for_bin(cids=cids, cmds=cmds)

    def create_quiver_bins(self, d, uc, partial_uc, refs, keys, start, end,
                           sge_opts):
        """
        Create quiver bins by putting every 100 clusters into a bin.
        For each bin, create a bash script (e.g., script_of_quivered_bin).
        Return a list of scripts to run.
        """
        bin_scripts = []
        for i in xrange(start, end, 100):  # Put every 100 clusters to a bin
            cids = keys[i:min(end, i + 100)]
            bin_sh = self.create_a_quiver_bin(cids=cids, d=d, uc=uc,
                                              partial_uc=partial_uc,
                                              refs=refs, sge_opts=sge_opts)
            bin_scripts += bin_sh
        return bin_scripts

    def create_quiver_bins_and_submit_jobs(self, d, uc, partial_uc, refs, keys,
                                           start, end, submitted, sge_opts):
        """
        Put every 100 clusters together and create bins. Create a bash script
        (e.g., script_of_quivered_bin), for each bin, and submit the script
        either using qsub or running it locally.
        return all bash scripts in a list.
        """
        if start >= end or start < 0 or start > len(keys) or end > len(keys):
            return []

        # Update refs
        new_refs = {cid: op.join(self.cluster_dir(cid), op.basename(refs[cid])) for cid in keys[start:end]}
        refs = new_refs

        # Reconstruct refs if not exist.
        if not nfs_exists(refs[keys[start]]):
            self.reconstruct_ref_fa_for_clusters_in_bin(cids=keys[start:end],
                                                        refs=refs)

        all_todo = []
        for i in xrange(start, end, 100):  # Put every 100 clusters to a bin
            cids = keys[i:min(end, i + 100)]
            bin_sh = self.create_a_quiver_bin(cids=cids, d=d, uc=uc,
                                              partial_uc=partial_uc,
                                              refs=refs, sge_opts=sge_opts)
            all_todo.append(bin_sh)
            # assert bin_sh == self.script_of_quivered_bin(first, last)
            # submit the created script of this quiver bin
            self.submit_todo_quiver_jobs(todo=[bin_sh], submitted=submitted,
                                         sge_opts=sge_opts)
        # end of for i in xrange(start, end, 100):
        return all_todo

    @property
    def report_fn(self):
        """Return a csv report with cluster_id, read_id, read_type."""
        return op.join(self.out_dir, "cluster_report.FL_nonFL.csv")

    def load_pickles(self):
        """Load uc and refs from final_pickle_fn, load partial uc from
        nfl_all_pickle_fn, return (uc, partial_uc. refs).
        """
        def _load_pickle(fn):
            """Load *.json or *.pickle file."""
            with open(fn) as f:
                if fn.endswith(".json"):
                    return json.loads(f.read())
                else:
                    return cPickle.load(f)
        self.add_log("Loading uc from {f}.".format(f=self.final_pickle_fn))
        a = _load_pickle(self.final_pickle_fn)
        uc = a['uc']
        refs = a['refs']

        self.add_log("Loading partial uc from {f}.".
                     format(f=self.nfl_all_pickle_fn))
        partial_uc = _load_pickle(self.nfl_all_pickle_fn)['partial_uc']
        partial_uc2 = defaultdict(lambda: [])
        partial_uc2.update(partial_uc)
        return (uc, partial_uc2, refs)

    def index_input_subreads(self):
        """Index input subreads in self.fasta_fofn or self.bas_fofn.
        """
        if guess_file_format(self.bas_fofn) == FILE_FORMATS.BAM:
            msg = "Indexing files in %s, please wait." % self.bas_fofn
            self.add_log(msg)
            d = BamCollection(self.bas_fofn)
        else:
            msg = "Indexing files in %s, please wait." % self.fasta_fofn
            self.add_log(msg)
            d = MetaSubreadFastaReader(get_files_from_file_or_fofn(self.fasta_fofn))

        self.add_log("File indexing done.")
        return d

    def submitted_quiver_jobs_log_of_chunk_i(self, i, num_chunks):
        """A txt file to save all submitted quiver jobs of the
        (i / num_chunks)-th workload. Format:
            job_id\tscript_path
        Return $root_dir/log/submitted_quiver_jobs.{i}of{num_chunks}.txt
        """
        return op.join(self.log_dir, "submitted_quiver_jobs.{i}of{N}.txt".
                                     format(i=i, N=num_chunks))

    def quiver_jobs_sh_of_chunk_i(self, i, num_chunks):
        """A bash file to save all quiver jobs of the
        (i / num_chunks)-th workload.
        Return $root_dir/log/quiver_jobs.{i}of{N}.sh
        """
        return op.join(self.log_dir, "quiver_jobs.{i}of{N}.sh".
                                     format(i=i, N=num_chunks))

    def process_chunk_i(self, i, num_chunks):
        """
        In order to distribute IceQuiver jobs by SMRTPipe using a fixed
        number of nodes, we divide quiver jobs into num_chunks workloads
        of roughly the same size, and are processing the i-th workload
        now.
        (1) load uc, partial_uc and refs from pickles and index subreads
            in fasta and save to d
        (2) write report if this is the first chunk (e.g, i==0)
        (3) Assume clusters are divided into num_chunks parts, process
            the i-th part.
        """
        if (i >= num_chunks):
            raise ValueError("Chunk index {i} should be less than {N}.".
                             format(i=i, N=num_chunks))

        # load uc, partial_uc and refs from pickles,
        uc, partial_uc, refs = self.load_pickles()

        # Write report to quivered/cluster_report.FL_nonFL.csv
        if i == 0:
            self.write_report(report_fn=self.report_fn,
                              uc=uc, partial_uc=partial_uc)

        # Index input subreads in fasta_fofn or bas_fofn,
        d = self.index_input_subreads()

        # good = [x for x in uc if len(uc[x]) > 1 or len(partial_uc2[x]) >= 10]
        # bug 24984, call quiver on everything, no selection is needed.
        keys = sorted([x for x in uc])  # sort cluster ids

        # Compute number of clusters in i-th chunk
        num_clusters_per_chunk = int(ceil(len(keys) / float(num_chunks)))
        num_clusters_in_chunk_i = max(0, min(len(keys) - i * num_clusters_per_chunk,
                                             num_clusters_per_chunk))
        start = i * num_clusters_per_chunk
        end = start + num_clusters_in_chunk_i

        submitted = []
        # Create quiver bins and submit jobs
        all_todo = self.create_quiver_bins_and_submit_jobs(d=d, uc=uc,
                                                           partial_uc=partial_uc, refs=refs, keys=keys, start=start,
                                                           end=end, submitted=submitted, sge_opts=self.sge_opts)

        # Write submitted quiver jobs to
        # $root_dir/log/submitted_quiver_jobs.{i}of{num_chunks}.txt
        log_name = self.submitted_quiver_jobs_log_of_chunk_i(
            i=i, num_chunks=num_chunks)
        self.add_log("Writing submitted quiver jobs to {f}".format(f=log_name))
        with open(log_name, 'w') as f:
            f.write("\n".join(str(x[0]) + '\t' + str(x[1]) for x in submitted))

        # Write all quiver jobs of this workload to
        # $root_dir/log/quiver_jobs.{i}of{num_chunks}.sh
        sh_name = self.quiver_jobs_sh_of_chunk_i(i=i, num_chunks=num_chunks)
        self.add_log("Writing all quiver jobs to {f}".format(f=sh_name))
        with open(sh_name, 'w') as f:
            assert isinstance(all_todo, list)
            f.write("\n".join(["bash " + str(x) for x in all_todo]))

    def run(self):
        """Run quiver to polish all consensus isoforms predicted by ICE."""
        # Validate inputs
        self.validate_inputs()

        # One workload in total
        self.process_chunk_i(i=0, num_chunks=1)

        # Copy $root_dir/log/submitted_quiver_jobs.0of1.txt
        # to $root_dir/log/submitted_quiver_jobs.txt
        src = self.submitted_quiver_jobs_log_of_chunk_i(i=0, num_chunks=1)
        shutil.copyfile(src=src, dst=self.submitted_quiver_jobs_log)

        self.close_log()
        return 0


def add_ice_quiver_arguments(parser):
    """Add arguments for IceQuiver, not including IceQuiverPostprocess."""
    parser = add_cluster_root_dir_as_positional_argument(parser)
    parser = add_fofn_arguments(parser, bas_fofn=True)
    parser = add_sge_arguments(parser, quiver_nproc=True, blasr_nproc=True)
    return parser
