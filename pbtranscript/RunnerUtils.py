#!/usr/bin/env python

"""
Job runner utils for both SGE jobs and local jobs.
"""
import subprocess
import logging
import time
import os
from multiprocessing.pool import ThreadPool
from pbcore.util.Process import backticks
from pbtranscript.ClusterOptions import SgeOptions

__author__ = 'etseng|yli@pacificbiosciences.com'

class SgeTimeOutException(Exception):
    """
    SGE Time out exception which can be raised
    when certain SGE jobs is timed out.
    """
    def __init__(self, errmsg):
        super(SgeTimeOutException).__init__(errmsg)


def write_cmd_to_script(cmd, script):
    """
    Write a cmd or a list of cmds to a script file.
    Parameters:
      cmd - a cmd string or a list of cmds
      script - a script file to save cmd/cmds
    """
    with open(script, 'w') as writer:
        writer.write("#!/bin/bash\n")
        if isinstance(cmd, str):
            writer.write(cmd + '\n')
        elif isinstance(cmd, list):
            writer.write("\n".join(cmd))
        else:
            assert False


def local_job_runner(cmds_list, num_threads, throw_error=True):
    """
    Execute a list of cmds locally using thread pool with at most
    num_threads threads, wait for all jobs to finish before exit.

    If throw_error is True, when any job failed, raise RuntimeError.
    If throw_error is False, return a list of cmds that failed.

    Parameters:
      cmds_list - cmds that will be executed in ThreadPool
      num_threads - number of threads that will be used in the ThreadPool
      throw_error - whether or not to throw RuntimeError when any of cmd failed.
      rescue - whether or not to rescue this job
      rescue_times - maximum number of rescue times
    """
    run_cmd_in_shell = lambda x: backticks(x, merge_stderr=True)
    try:
        pool = ThreadPool(processes=num_threads)
        rets = pool.map(run_cmd_in_shell, cmds_list)
        pool.close()
        pool.join()
    except subprocess.CalledProcessError:
        pass

    failed_cmds = [cmds_list[i] for i in range(0, len(cmds_list)) if rets[i][1] != 0]
    failed_cmds_out = [rets[i][0] for i in range(0, len(cmds_list)) if rets[i][1] != 0]

    if throw_error and len(failed_cmds) > 0:
        errmsg = "\n".join(["CMD failed: %s, %s" % (cmd, out)
                            for (cmd, out) in zip(failed_cmds, failed_cmds_out)])
        raise RuntimeError(errmsg)
    else:
        return failed_cmds


def get_active_sge_jobs():
    """Return a dict of active sge job ids and their status by
    calling qstat.

    output - {jid: status}, e.g., {'199':'r', '200':'hqw'}
    """
    try:
        stuff = os.popen("qstat").read().strip().split('\n')
        return dict({x.split()[0]: x.split()[4] for x in stuff[2:]})
    except Exception as e:
        raise RuntimeError("Unable to get active qsub jobs.", str(e))


def sge_submit(qsub_cmd, qsub_try_times=1):
    """
    Submit qsub_cmd to sge and return sge job id as string.
    Keep trying for at most {qsub_try_times} times until qsub succeeded.
    Default, no retry.

    Parameters:
      qsub_cmd - a qsub cmd starting with 'qsub'
      qsub_try_times - maximum try times
    """
    assert qsub_cmd.startswith("qsub")
    try_times = 1
    while try_times <= qsub_try_times:
        out, code, dummy_msg = backticks(qsub_cmd)
        if code == 0: # succeeded, break
            # Your job 596028 ("a.sh") has been submitted
            return str(out).split()[2]
        else:
            # failed, sleep for a little, try again
            time.sleep(5)
            try_times += 1

    raise RuntimeError("Unable to qsub CMD: {cmd}. Abort!:"
                       .format(cmd=qsub_cmd))


#def wait_for_sge_jobs(cmd, jids, timeout):
#    """
#    This replaces the original qsub -sync y -hold_jid j1,j2..... command
#    which can still be hung if certain jobs got stuck.
#
#    If timeout occurs, simply qdel all jids (ignoring whether they exist or not)
#    and let the main function that calls it handle what to do
#
#    Parameters:
#      cmd - command waiting for
#      jids - job ids that we are waiting for
#      timeout - time out in seconds, delete all input jobs.
#    """
#    p = multiprocessing.Process(target=sge_submit, args=((cmd, 1),))
#    p.start()
#    p.join(timeout)
#    if p.is_alive(): # timed out
#        active_jids = get_active_sge_jobs().keys()
#        while len(active_jids) > 0:
#            for jid in active_jids:
#                kill_cmd = "qdel " + str(jid)
#                backticks(kill_cmd) # don't care whether it worked
#            time.sleep(3) # wait for qdel to take effect...
#            active_jids = get_active_sge_jids().keys()
#        raise SgeException("TIMEOUT")


def kill_sge_jobs(jids):
    """Kill given sge jobs."""
    for jid in jids:
        kill_cmd = "qdel {jid}".format(jid=jid)
        backticks(kill_cmd) # don't care whether it worked.
        time.sleep(3) # wiat for qdel to take effect...


def wait_for_sge_jobs(jids, wait_timeout=None, run_timeout=None):
    """
    Wait for all sge job ids {jids} to complete before exiting.
    Return sge job ids that have been killed by qdel.

    If wait_timeout is set, qdel all jobs regardless job status after
    {wait_timeout} seconds have passed.
    If wait_timeout is None, jobs can qw or held for a long time
    when cluster is busy. If sge died and restarted, jobs will
    no longer be active and wait_for_sge_jobs should be OK to exit,
    however, in this case, upstream calls may not be aware of
    jobs are not completed.

    If run_timeout is set, qdel a job after it has been running for
    {run_timeout} seconds.
    If run_timeout is None, jobs can run forever unless wait_timeout is set.

    Note that if both wait_timeout and run_timeout are set, qdel a job
    when the earliest time out is reached.

    Parameters:
      jids - sge job ids that we are waiting for
      wait_timeout - maximum time in seconds waiting for sge jobs,
                    regardless of their statuses. qdel it otherwise.
                    If is None, no cap.
      run_timeout - maximum time in seconds that a sge job can be running,
                   not counting qw or hold time. qdel it otherwise.
                   If is None, no cap.
    """
    count = 0
    check_sge_every_n_seconds = 10 # check sge every n seconds.
    time_passed = 0
    runtime_passed = dict({jid: 0 for jid in jids})
    killed_jobs = [] # jobs that have been killed.

    while True:
        active_d = get_active_sge_jobs()
        not_done_jids = list(set(jids).intersection(set(active_d.keys())))
        if len(not_done_jids) != 0:
            # some sge jobs are still running or qw, or held
            time.sleep(check_sge_every_n_seconds)
            time_passed += check_sge_every_n_seconds
            count += 1
            if count % 100 == 0:
                logging.debug("Waiting for sge job to complete: %s.",
                              ",".join(not_done_jids))

            if wait_timeout is not None and time_passed >= wait_timeout:
                kill_sge_jobs(jids=not_done_jids)
                killed_jobs.extend(not_done_jids)
                break

            if run_timeout is not None:
                # update runtime_passed
                for jid in not_done_jids:
                    if active_d[jid].startswith('r'):
                        runtime_passed[jid] += check_sge_every_n_seconds

                to_kill_jids = [jid for jid in not_done_jids
                                if runtime_passed[jid] >= run_timeout]
                kill_sge_jobs(jids=to_kill_jids)
                killed_jobs.extend(to_kill_jids)
        else:
            break

    return list(set(killed_jobs))


def sge_job_runner(cmds_list, script_files,
                   #done_script,
                   num_threads_per_job, sge_opts, qsub_try_times=3,
                   wait_timeout=600, run_timeout=600,
                   rescue=None, rescue_times=3):
    """
    Write commands in cmds_list each to a file in script_files.
    Qsub all scripts to sge, then qsub done_script which depends
    on all previously submitted jobs to complete before it starts.

    Parameters:
      cmds_list - a list of commands to run
      script_files - a list of script_files each saving a command in cmds_list
      #done_script - run this script locall when all sge jobs are complete.

      num_threads_per_job - number of cores required for each job
                            (e.g., qsub -pe smp {n})
      sge_opts - sge options to submit sge jobs.
      qsub_try_time - Retry if qsub failed

      wait_timeout - maximum time in seconds passed before qdel all jobs.
      run_timeout - maximum time in seconds allowing a sge job to be running
                   before qdel this job

      rescue - whether or not to rescue a qdel-ed job.
               None - no rescue
               locally - yes, run it locally exactly once
               sge - yes, run it through sge, try multiple times until suceed
      rescue_times - maximum times of rescuing a qdel-ed job.

    ToDo:
    (1) if SGE fails at qsub, -- resubmit? wait? run local?
    (2) add in ways to monitor if certain qsub jobs died or hung --- resubmit? kill? run local?
    """
    assert isinstance(sge_opts, SgeOptions)
    if len(cmds_list) != len(script_files):
        raise ValueError("Number of commands and script files "
                         "passed to sge_job_runner must be the same.")
    jids = []
    jids_to_cmds = {}
    jids_to_scripts = {}
    for cmd, script in zip(cmds_list, script_files):
        if run_timeout is not None and not cmd.startswith("timeout"):
            cmd = "timeout %d %s" % (run_timeout, cmd)
        write_cmd_to_script(cmd=cmd, script=script)
        qsub_cmd = sge_opts.qsub_cmd(script=script, num_threads=num_threads_per_job,
                                     elog=script+".elog", olog=script+".olog")
        jid = sge_submit(qsub_cmd=qsub_cmd, qsub_try_times=qsub_try_times)
        jids.append(jid)
        jids_to_cmds[jid] = cmd
        jids_to_scripts[jid] = script

    # We used to submit a done job which waits for all previous submitted
    # sge jobs to complete using 'qsub -hold_jid'. This is deprecated because:
    # 1. some non-SGE clusters may not support -hold_jid option
    # 2. we prefer a timeout schema. Sometimes one job may be indefinitley
    # stuck on a node (becuz that node is zombied or used up by another job),
    # in this case, the job simply sits there FOREVER. We would rather it kill
    # off the qsub jobs that goes over the timeout and retry.
    #
    # Replace 'qsub -hold_jid' by wait_for_sge_jobs with timeout.

    killed_jobs = wait_for_sge_jobs(jids=jids, wait_timeout=wait_timeout,
                                    run_timeout=run_timeout)
    killed_cmds = [jids_to_cmds[jid] for jid in killed_jobs]
    killed_scripts = [jids_to_scripts[jid] for jid in killed_jobs]

    if rescue is None or rescue_times <= 0:
        return zip(killed_cmds, killed_scripts)
    elif rescue == "locally": # retry at most once if running locally
        ret = []
        for killed_cmd, killed_script in zip(killed_cmds, killed_scripts):
            failed = (len(local_job_runner(cmds_list=[killed_cmd],
                                           num_threads=num_threads_per_job,
                                           throw_error=False)) != 0)
            if failed:
                ret.append((killed_cmd, killed_script))
        return ret
    elif rescue == "sge":
        return sge_job_runner(cmds_list=[], script_files=[],
                              num_threads_per_job=num_threads_per_job,
                              sge_opts=sge_opts, qsub_try_times=qsub_try_times,
                              wait_timeout=wait_timeout, run_timeout=run_timeout,
                              rescue=rescue, rescue_times=(rescue_times-1))
    else:
        raise ValueError("Unable to recognize rescue type {r}.".format(r=rescue))
