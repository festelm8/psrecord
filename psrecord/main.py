# Copyright (c) 2013, Thomas P. Robitaille
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import (unicode_literals, division, print_function,
                        absolute_import)

import time
import argparse
import threading
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import Float, Column, Integer, String, Sequence

children = []

Base = declarative_base()
class QueueData(Base):
    __tablename__ = 'QUEUE_DATA'

    id = Column(Integer, Sequence('table_id_seq', metadata=Base.metadata, start=1), primary_key=True, unique=True)
    pid = Column(Integer)
    operation_type = Column(String(36))
    data_type = Column(String(36))
    time = Column(Float)
    value = Column(Float)
    experiment_id = Column(Integer)

def get_percent(process):
    return process.cpu_percent()


def get_memory(process):
    return process.memory_info()


def all_children(pr):

    global children

    try:
        children_of_pr = pr.children(recursive=True)
    except Exception:  # pragma: no cover
        return children

    for child in children_of_pr:
        if child not in children:
            children.append(child)

    return children


def main():

    parser = argparse.ArgumentParser(
        description='Record CPU and memory usage for a process')

    parser.add_argument('process_id_or_command', type=str,
                        help='the process id or command')

    parser.add_argument('--log', type=str,
                        help='output the statistics to a file')

    parser.add_argument('--pg-db-uri', type=str,
                        help='output the statistics to a psql')

    # parser.add_argument('--plot', type=str,
    #                     help='output the statistics to a plot')

    parser.add_argument('--duration', type=float,
                        help='how long to record for (in seconds). If not '
                             'specified, the recording is continuous until '
                             'the job exits.')

    parser.add_argument('--interval', type=float,
                        help='how long to wait between each sample (in '
                             'seconds). By default the process is sampled '
                             'as often as possible.')

    parser.add_argument('--include-children',
                        help='include sub-processes in statistics (results '
                             'in a slower maximum sampling rate).',
                        action='store_true')

    parser.add_argument('--pname', type=str,
                        help='how name of tracking process')

    args = parser.parse_args()

    if args.pg_db_uri:
        try:
            engine = create_engine(args.pg_db_uri)
        except Exception:
            print("Bad psql db URI")
            return

        Session = sessionmaker(bind=engine)
        dbsession = Session()
    else:
        dbsession = None

    if args.duration:
        commit_delay = int(args.duration)+2
    else:
        commit_delay = 7

    threads = {}
    threads_to_delete = []
    stop = False
    pivot = time.time()

    pids = args.process_id_or_command.split(',')
    for target_pid in pids:
        x = threading.Thread(target=pid_handler, args=(target_pid, lambda:stop, dbsession, args))
        x.start()
        threads[target_pid] = x

    try:
        while not stop:
            time.sleep(2)

            if pivot - time.time() < commit_delay:
                pivot = time.time()
                dbsession.commit()

            for thread_id in threads.keys():
                if not threads[thread_id].is_alive():
                    threads_to_delete.append(thread_id)

            if threads_to_delete:
                for x in threads_to_delete:
                    del threads[x]
                    print("Stop working with ", x, " with inactivity reason")
                threads_to_delete = []

            if not threads:
                print("All theads are dead")
                stop = True

    except KeyboardInterrupt:
        stop = True

    print("\nStoppping...")


def pid_handler(target_pid, stop, dbsession, args):
    # Attach to process
    try:
        pid = int(target_pid)
        print("Attaching to process {0}".format(pid))
    except Exception:
        print("PID '{0}' handling problem".format(target_pid))
        return

    monitor(pid, stop, dbsession,
            logfile=args.log, plot=None, duration=args.duration,
            interval=args.interval, include_children=args.include_children, pname=args.pname)


def monitor(pid, stop, dbsession,
            logfile=None, plot=None, duration=None, interval=5,
            include_children=False, pname=None):

    # We import psutil here so that the module can be imported even if psutil
    # is not present (for example if accessing the version)
    import psutil

    try:
        pr = psutil.Process(pid)
    except Exception:
        print("No process found with pid ", pid)
        return

    # Record start time
    start_time = time.time()

    tracked_process_name = pname if pname is not None else "Process "+str(pid)

    if logfile:
        logfile_name = logfile+str(pid)
        f = open(logfile_name, 'w')
        f.write("# {0:12s} {1:12s} {2:12s} {3:12s}\n".format(
            'Elapsed time'.center(12),
            'CPU (%)'.center(12),
            'Real (MB)'.center(12),
            'Virtual (MB)'.center(12))
        )

    log = {}
    log['times'] = []
    log['cpu'] = []
    log['mem_real'] = []
    log['mem_virtual'] = []

    print("Working with process {0}".format(pid))

    # Start main event loop
    while not stop():

        # Find current time
        current_time = time.time()

        try:
            pr_status = pr.status()
        except TypeError:  # psutil < 2.0
            pr_status = pr.status
        except psutil.NoSuchProcess:  # pragma: no cover
            break

        # Check if process status indicates we should exit
        if pr_status in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]:
            print("Process finished ({0:.2f} seconds)"
                  .format(current_time - start_time))
            break

        # Check if we have reached the maximum time
        if duration is not None and current_time - start_time > duration:
            break

        # Get current CPU and memory
        try:
            current_cpu = get_percent(pr)
            current_mem = get_memory(pr)
        except Exception:
            break
        current_mem_real = current_mem.rss / 1024.
        current_mem_virtual = current_mem.vms / 1024.

        # Get information for children
        if include_children:
            for child in all_children(pr):
                try:
                    current_cpu += get_percent(child)
                    current_mem = get_memory(child)
                except Exception:
                    continue
                current_mem_real += current_mem.rss / 1024.
                current_mem_virtual += current_mem.vms / 1024.

        if dbsession is not None:
            new_cpu_entry = QueueData(
                pid = pid,
                operation_type=tracked_process_name,
                data_type = "CPU_USAGE",
                time = current_time,
                value = current_cpu,
                experiment_id = 0
            )
            new_mem_real_entry = QueueData(
                pid = pid,
                operation_type=tracked_process_name,
                data_type = "MEMORY_KB_REAL",
                time = current_time,
                value = current_mem_real,
                experiment_id = 0
            )
            new_mem_virt_entry = QueueData(
                pid = pid,
                operation_type=tracked_process_name,
                data_type = "MEMORY_KB_VIRTUAL",
                time = current_time,
                value = current_mem_virtual,
                experiment_id = 0
            )

            dbsession.add_all([new_cpu_entry, new_mem_real_entry, new_mem_virt_entry])

        if logfile:
            f.write("{0:12.3f} {1:12.3f} {2:12.3f} {3:12.3f}\n".format(
                current_time - start_time,
                current_cpu,
                current_mem_real,
                current_mem_virtual))
            f.flush()

        if interval is not None:
            time.sleep(interval)

        # If plotting, record the values
        if plot:
            log['times'].append(current_time - start_time)
            log['cpu'].append(current_cpu)
            log['mem_real'].append(current_mem_real)
            log['mem_virtual'].append(current_mem_virtual)

    if logfile:
        f.close()

    if plot:
        # Use non-interactive backend, to enable operation on headless machines
        import matplotlib.pyplot as plt
        with plt.rc_context({'backend': 'Agg'}):

            fig = plt.figure()
            ax = fig.add_subplot(1, 1, 1)

            ax.plot(log['times'], log['cpu'], '-', lw=1, color='r')

            ax.set_ylabel('CPU (%)', color='r')
            ax.set_xlabel('time (s)')
            ax.set_ylim(0., max(log['cpu']) * 1.2)

            ax2 = ax.twinx()

            ax2.plot(log['times'], log['mem_real'], '-', lw=1, color='b')
            ax2.set_ylim(0., max(log['mem_real']) * 1.2)

            ax2.set_ylabel('Real Memory (MB)', color='b')

            ax.grid()

            fig.savefig(plot)

# if __name__ == '__main__':
#     main()