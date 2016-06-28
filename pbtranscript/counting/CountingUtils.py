#!/usr/bin/env python
"""
Utils for outputing read status of FL and nFL reads, and making
read mapping abundance file.
"""

import os.path as op
from collections import defaultdict
from cPickle import load
#from csv import DictReader
from pbtranscript.io import GroupReader, MapStatus, ReadStatRecord, \
        ReadStatReader, ReadStatWriter, AbundanceRecord, AbundanceWriter


__author__ = 'etseng@pacificbiosciences.com'

__all__ = ["read_group_file",
           #"output_read_count_IsoSeq_csv",
           "output_read_count_FL",
           #"output_read_count_RoI",
           "output_read_count_nFL",
           "make_abundance_file"]


def read_group_file(group_filename, is_cid=True, sample_prefixes=None):
    """
    Make the connection between partitioned results and final (ex: PB.1.1)
    The partitioned results could either be ICE cluster (ex: i1_c123) or CCS

    Return: dict of seq_or_ice_cluster --> collapsed cluster ID
    """
    cid_info = {} # ex: i1 --> c123 --> PB.1.1, or None --> c123 --> PB.1.1
    if sample_prefixes is not None:
        for sample_prefix in sample_prefixes:
            cid_info[sample_prefix] = {}
    else:
        cid_info[None] = {}

    reader = GroupReader(group_filename)
    for group in reader:
        pbid, members = group.name, group.members
        for cid in members:
            # ex: x is 'i1_c123/f3p0/123 or
            # m131116_014707_42141_c100591062550000001823103405221462_s1_p0/93278/31_1189_CCS
            if sample_prefixes is None:
                if is_cid:
                    cid = cid.split('/')[0]
                cid_info[None][cid] = pbid
            else:
                if any(cid.startswith(sample_prefix + '|') for sample_prefix in sample_prefixes):
                    sample_prefix, cid = cid.split('|', 1)
                    if is_cid:
                        cid = cid.split('/')[0]
                    cid_info[sample_prefix][cid] = pbid
    reader.close()
    return cid_info


#def output_read_count_IsoSeq_csv(cid_info, csv_filename, output_filename, output_mode='w'):
#    """
#    Given an Iso-Seq csv output file w/ format:
#
#    cluster_id,read_id,read_type
#
#    Turn into read_stats.txt format:
#
#    id\tlength\tis_fl\tstat\tpbid
#    """
#    mapped = {}  # nFL seq -> list of (sample_prefix, cluster) it belongs to
#
#    writer = ReadStatWriter(output_filename, output_mode)
#
#    for r in DictReader(open(csv_filename), delimiter=','):
#        cid = str(r['cluster_id'])
#        if not cid.startswith('c'):
#            raise ValueError("Cluster id %s in IsoSeq csv output %s must start with c" %
#                             (str(r), csv_filename))
#        x = r['read_id']
#        if cid in cid_info:
#            # if is FL, must be unique
#            if r['read_type'] == 'FL':
#                record = ReadStatRecord(name=x, is_fl=True, stat=MapStatus.UNIQUELY_MAPPED,
#                                        pbid=cid_info[cid])
#                writer.writeRecord(record)
#            else: # nonFL could be multi-mapped, must wait and see
#                if r['read_type'] != 'NonFL':
#                    raise ValueError("%s read_type must be NonFL" % r)
#                # is only potentially unmapped, add all (movie-restricted) members
#                # to unmapped holder
#                pbid = cid_info[cid]
#                if x not in mapped:
#                    mapped[x] = set()
#                mapped[x].add(pbid)
#        else:
#            # unmapped
#            record = ReadStatRecord(name=x, is_fl=True, stat=MapStatus.UNMAPPED, pbid=None)
#            writer.writeRecord(record)
#
#    # now we can go through the list of mapped to see which are uniquely mapped which are not
#    for seqid, pbids in mapped.iteritems():
#        if len(pbids) == 1: # unique
#            stat = MapStatus.UNIQUELY_MAPPED
#        else:
#            stat = MapStatus.AMBIGUOUSLY_MAPPED
#        for pbid in pbids:
#            record = ReadStatRecord(name=seqid, is_fl=False, stat=stat, pbid=pbid)
#            writer.writeRecord(record)
#
#    writer.close()
#
#
##def output_read_count_RoI(cid_info, roi_filename, output_filename):
#    """
#    Given an CCS FASTA file, and cid_info, compute read status of CCS reads
#    and output ReadStatRecords to output_filename.
#    Parameters:
#        cid_info -- a dict read from group file, seq_or_ice_cluster --> collapsed cluster ID
#        roi_filename -- CCS file containing CCS reads, e.g., movie/zmw/start_end_CCS
#        output_filename -- a tab delimited file containing ReadStatRecord objects
#    """
#    from pbcore.io.FastaIO import FastaReader
#    writer = ReadStatWriter(output_filename, mode='w')
#    for r in FastaReader(roi_filename):
#        if r.name in cid_info:
#            pbid, stat = cid_info[r.name], MapStatus.UNIQUELY_MAPPED
#        else:
#            pbid, stat = None, MapStatus.UNMAPPED
#        record = ReadStatRecord(name=r.name, is_fl=True, stat=stat, pbid=pbdi)
#    writer.close()


def output_read_count_FL(cid_info, prefix_pickle_filename_tuples, output_filename,
                         output_mode='w', restricted_movies=None):
    """
    Given cid_info, prefix_pickle_tuples, output read status of FL CCS reads in restricted_movies.

    If restricted_movies is None, all FL reads are output.
    Otherwise (esp. in case where I binned by size then pooled in the end),
    give the list of movies associated with a particular list of cell runs
    (ex: brain_2to3k_phusion FL only)

    Because may have multiple pickles, can ONLY determine which FL reads
    are unmapped at the VERY END.

    Parameters:
        cid_info -- a dict read from group file, seq_or_ice_cluster --> collapsed cluster ID
        prefix_pickle_filename_tuples -- a list of (sample prefix, nfl_uc_pickle filename) tuples
        output_filename -- a tab delimited file reporting FL reads status
        restricted_movies -- if not None, only output status of reads in these movies.
    """
    unmapped_holder = set() # will hold anything that was unmapped in one of the pickles
    mapped_holder = set() # will hold anything that was mapped in (must be exactly) one of
    # the pickles then to get the true unmapped just to {unmapped} - {mapped}
    is_fl = True

    writer = ReadStatWriter(output_filename, mode=output_mode)

    for sample_prefix, pickle_filename in prefix_pickle_filename_tuples:
        if not op.exists(pickle_filename):
            raise IOError("%s does not exist." % pickle_filename)
        with open(pickle_filename) as h:
            uc = load(h)['uc']
        for cid_no_prefix, members in uc.iteritems():
            cid = 'c' + str(cid_no_prefix)
            if cid in cid_info[sample_prefix]:
                # can immediately add all (movie-restricted) members to mapped
                for read_id in members:
                    movie = read_id.split('/')[0]
                    if restricted_movies is None or movie in restricted_movies:
                        mapped_holder.add(read_id)
                        record = ReadStatRecord(name=read_id, is_fl=is_fl,
                                                stat=MapStatus.UNIQUELY_MAPPED,
                                                pbid=cid_info[sample_prefix][cid])
                        writer.writeRecord(record)
            else:
                # is only potentially unmapped, add all (movie-restricted) members to
                # unmapped holder
                for read_id in members:
                    movie = read_id.split('/')[0]
                    if restricted_movies is None or movie in restricted_movies:
                        unmapped_holder.add(read_id)

    # now with all the pickles processed we can determine which of all (movie-restricted) FL reads
    # are not mapped in any of the pickles
    unmapped_holder = unmapped_holder.difference(mapped_holder)
    for read_id in unmapped_holder:
        record = ReadStatRecord(name=read_id, is_fl=is_fl, stat=MapStatus.UNMAPPED, pbid=None)
        writer.writeRecord(record)

    writer.close()


def output_read_count_nFL(cid_info, prefix_pickle_filename_tuples, output_filename,
                          output_mode='w', restricted_movies=None):
    """
    Output read status of nFL CCS reads in restricted_movies.

    Parameters:
        cid_info -- a dict read from group file, seq_or_ice_cluster --> collapsed cluster ID
        prefix_pickle_filename_tuples -- a list of (sample prefix, nfl.partial_uc.pickle) tuples
        output_filename -- a tab delimited file reporting nFL reads status

    If restricted_movies is None, all nonFL reads are output.
    Otherwise (esp. in case where I binned by size then pooled in the end), give the list
    of movies associated with a particular list of cell runs (ex: brain_2to3k_phusion nonFL only)

    There is no guarantee that the non-FL reads are shared between the pickles, they might be or not
    Instead determine unmapped (movie-restricted) non-FL reads at the very end
    """
    unmapped_holder = set() # will hold anything that was unmapped in one of the pickles
    # then to get the true unmapped just to {unmapped} - {mapped}
    mapped = {} # nFL seq -> list of (sample_prefix, cluster) it belongs to
    is_fl = False # nFL read

    writer = ReadStatWriter(output_filename, mode=output_mode)

    for sample_prefix, pickle_filename in prefix_pickle_filename_tuples:
        if not op.exists(pickle_filename):
            raise IOError("%s does not exist." % pickle_filename)
        with open(pickle_filename) as h:
            result = load(h)
            uc = result['partial_uc']
            if restricted_movies is None:
                unmapped_holder.update(result['nohit'])
            else:
                #unmapped_holder.update(filter(lambda x: x.split('/')[0] in restricted_movies,
                #                              result['nohit']))
                unmapped_holder.update([x for x in result['nohit']
                                        if x.split('/')[0] in restricted_movies])

        for cid_no_prefix, members in uc.iteritems():
            cid = 'c' + str(cid_no_prefix)
            if cid in cid_info[sample_prefix]: # is at least mapped
                pbid = cid_info[sample_prefix][cid]
                for read_id in members:
                    movie = read_id.split('/')[0]
                    if restricted_movies is None or movie in restricted_movies:
                        if read_id not in mapped:
                            mapped[read_id] = set()
                        mapped[read_id].add(pbid)
            else: # not entirely sure it is unmapped but put it in the meantime
                for read_id in members:
                    movie = read_id.split('/')[0]
                    if restricted_movies is None or movie in restricted_movies:
                        unmapped_holder.add(read_id)

    # now we can go through the list of mapped to see which are uniquely mapped which are not
    for seqid, pbids in mapped.iteritems():
        if len(pbids) == 1: # unique
            stat = MapStatus.UNIQUELY_MAPPED
        else:
            stat = MapStatus.AMBIGUOUSLY_MAPPED
        for pbid in pbids:
            record = ReadStatRecord(name=seqid, is_fl=is_fl, stat=stat, pbid=pbid)
            writer.writeRecord(record)

    unmapped_holder = unmapped_holder.difference(mapped)
    # write the nohits
    for read_id in unmapped_holder:
        record = ReadStatRecord(name=read_id, is_fl=is_fl, stat=MapStatus.UNMAPPED, pbid=None)
        writer.writeRecord(record)

    writer.close()


def make_abundance_file(read_stat_filename, output_filename, given_total=None,
                        restricted_movies=None, write_header_comments=True):
    """
    Make read mapping abundance file.
    If given_total is not None, use it instead of the total count based on <read_stat_filename>
    given_total should be dict of {fl, nfl, nfl_amb}
    Parameters:
      read_stat_filename - path to a read status file each line of which is a ReadStatRecord
      output_filename - path to output abundance file.
    """
    total_ids = {'fl':set(), 'nfl':set(), 'nfl_amb':set()}

    # pbid, could be NA or None --> # of FL reads mapped to it
    tally = defaultdict(lambda: {'fl':0, 'nfl':0, 'nfl_amb':0})
    amb_count = defaultdict(lambda: []) # non-fl id --> list of pbid matches

    reader = ReadStatReader(read_stat_filename)
    for r in reader:
        movie = r.name.split('/')[0]
        if restricted_movies is None or movie in restricted_movies:
            if r.pbid is not None:
                if r.is_fl: # FL, must be uniquely mapped
                    assert r.is_uniquely_mapped
                    tally[r.pbid]['fl'] += 1
                    total_ids['fl'].add(r.name)
                else: # non-FL, can be ambiguously mapped
                    if r.is_uniquely_mapped:
                        tally[r.pbid]['nfl'] += 1
                        total_ids['nfl'].add(r.name)
                    else:
                        assert r.is_ambiguously_mapped
                        amb_count[r.name].append(r.pbid)
                        total_ids['nfl_amb'].add(r.name)
            else: # even if it is unmapped it still counts in the abundance total!
                if r.is_fl:
                    total_ids['fl'].add(r.name)
                else:
                    total_ids['nfl'].add(r.name)

    # put the ambiguous back in tally weighted
    for dummy_seqid, pbids in amb_count.iteritems():
        weight = 1. / len(pbids)
        for pbid in pbids:
            tally[pbid]['nfl_amb'] += weight

    if given_total is not None:
        use_total_fl = given_total['fl']
        use_total_nfl = given_total['fl'] + given_total['nfl']
        # ToDo: the below is NOT EXACTLY CORRECT!! Fix later!
        use_total_nfl_amb = given_total['fl'] + given_total['nfl'] + given_total['nfl_amb']
    else:
        use_total_fl = len(total_ids['fl'])
        use_total_nfl = len(total_ids['fl']) + len(total_ids['nfl'])
        use_total_nfl_amb = len(total_ids['fl']) + len(total_ids['nfl']) + len(total_ids['nfl_amb'])

    comments = None
    if write_header_comments:
        comments = AbundanceWriter.make_comments(total_fl=use_total_fl, total_nfl=use_total_nfl,
                                                 total_nfl_amb=use_total_nfl_amb)
    writer = AbundanceWriter(output_filename, comments=comments)

    #("pbid\tcount_fl\tcount_nfl\tcount_nfl_amb\tnorm_fl\tnorm_nfl\tnorm_nfl_amb\n")
    keys = tally.keys()
    keys.sort(key=lambda x: map(int, x.split('.')[1:])) # sort by PB.1, PB.2....
    for pbid in keys:
        v = tally[pbid]
        count_fl = v['fl']
        count_nfl = count_fl + v['nfl']
        count_nfl_amb = count_nfl + v['nfl_amb']
        norm_fl = count_fl*1./use_total_fl
        norm_nfl = count_nfl*1./use_total_nfl
        norm_nfl_amb = count_nfl_amb*1./use_total_nfl_amb
        record = AbundanceRecord(pbid=pbid, count_fl=count_fl, count_nfl=count_nfl,
                                 count_nfl_amb=count_nfl_amb, norm_fl=norm_fl,
                                 norm_nfl=norm_nfl, norm_nfl_amb=norm_nfl_amb)
        writer.writeRecord(record)
    writer.close()
