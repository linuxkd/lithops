#
# (C) Copyright PyWren Team 2018
# (C) Copyright IBM Corp. 2020
# (C) Copyright Cloudlab URV 2020
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import sys
import zlib
import time
import json
import queue
import base64
import pickle
import logging
import traceback
import multiprocessing as mp
from threading import Thread
from multiprocessing import Process, Pipe
from multiprocessing.managers import SyncManager
from tblib import pickling_support
from types import SimpleNamespace

from lithops.version import __version__
from lithops.config import extract_storage_config
from lithops.storage import InternalStorage
from lithops.worker.jobrunner import JobRunner
from lithops.worker.utils import LogStream, custom_redirection,\
    get_function_and_modules, get_function_data
from lithops.constants import JOBS_PREFIX, LITHOPS_TEMP_DIR, MODULES_DIR
from lithops.utils import setup_lithops_logger, is_unix_system
from lithops.worker.status import create_call_status

pickling_support.install()

logger = logging.getLogger(__name__)


class ShutdownSentinel:
    """Put an instance of this class on the queue to shut it down"""
    pass


def function_handler(payload):
    job = SimpleNamespace(**payload)
    processes = min(job.worker_processes, len(job.call_ids))

    logger.info('Tasks received: {} - Concurrent workers: {}'
                .format(len(job.call_ids), processes))

    storage_config = extract_storage_config(job.config)
    internal_storage = InternalStorage(storage_config)
    job.func = get_function_and_modules(job, internal_storage)
    job_data = get_function_data(job, internal_storage)

    if processes == 1:
        job_queue = queue.Queue()
        for task_id in job.call_ids:
            data = job_data.pop(0)
            job_queue.put((job, task_id, data))
        job_queue.put(ShutdownSentinel())
        process_runner(job_queue, internal_storage)
    else:
        manager = SyncManager()
        manager.start()
        job_queue = manager.Queue()
        job_runners = []

        for runner_id in range(processes):
            p = mp.Process(target=process_runner, args=(job_queue, internal_storage))
            job_runners.append(p)
            p.start()
            logger.info('Worker process {} started'.format(runner_id))

        for task_id in job.call_ids:
            data = job_data.pop(0)
            job_queue.put((job, task_id, data))

        for i in range(processes):
            job_queue.put(ShutdownSentinel())

        for runner in job_runners:
            runner.join()

        manager.shutdown()

    # Delete modules path from syspath
    module_path = os.path.join(MODULES_DIR, job.job_key)
    if module_path in sys.path:
        sys.path.remove(module_path)


def process_runner(job_queue, internal_storage):
    """
    Listens the job_queue and executes the jobs
    """
    while True:
        event = job_queue.get(block=True)
        if isinstance(event, ShutdownSentinel):
            break

        job, task_id, data = event
        job.id = task_id
        job.data = data

        bucket = job.config['lithops']['storage_bucket']
        job.task_dir = os.path.join(LITHOPS_TEMP_DIR, bucket, JOBS_PREFIX, job.job_key, task_id)
        job.log_file = os.path.join(job.task_dir, 'execution.log')
        os.makedirs(job.task_dir, exist_ok=True)

        with open(job.log_file, 'a') as log_strem:
            job.log_stream = LogStream(log_strem)
            with custom_redirection(job.log_stream):
                run_job(job, internal_storage)


def run_job(job, internal_storage):
    """
    Runs a single job within a separate process
    """
    call_status = create_call_status(job, internal_storage)
    setup_lithops_logger(job.log_level)

    backend = os.environ.get('__LITHOPS_BACKEND', '')
    logger.info("Lithops v{} - Starting {} execution".format(__version__, backend))
    logger.info("Execution ID: {}/{}".format(job.job_key, job.id))

    if job.runtime_memory:
        logger.debug('Runtime: {} - Memory: {}MB - Timeout: {} seconds'
                     .format(job.runtime_name, job.runtime_memory, job.execution_timeout))
    else:
        logger.debug('Runtime: {} - Timeout: {} seconds'.format(job.runtime_name, job.execution_timeout))

    env = job.extra_env
    env['LITHOPS_WORKER'] = 'True'
    env['PYTHONUNBUFFERED'] = 'True'
    env['LITHOPS_CONFIG'] = json.dumps(job.config)
    env['__LITHOPS_SESSION_ID'] = '-'.join([job.job_key, job.id])
    os.environ.update(env)

    try:
        # send init status event
        call_status.send_init_event()

        job.stats_file = os.path.join(job.task_dir, 'job_stats.txt')
        handler_conn, jobrunner_conn = Pipe()
        taskrunner = JobRunner(job, jobrunner_conn, internal_storage)
        logger.debug('Starting JobRunner process')
        jrp = Process(target=taskrunner.run) if is_unix_system() else Thread(target=taskrunner.run)
        jrp.start()

        jrp.join(job.execution_timeout)
        logger.debug('JobRunner process finished')

        if jrp.is_alive():
            # If process is still alive after jr.join(job_max_runtime), kill it
            try:
                jrp.terminate()
            except Exception:
                # thread does not have terminate method
                pass
            msg = ('Function exceeded maximum time of {} seconds and was '
                   'killed'.format(job.execution_timeout))
            raise TimeoutError('HANDLER', msg)

        if not handler_conn.poll():
            logger.error('No completion message received from JobRunner process')
            logger.debug('Assuming memory overflow...')
            # Only 1 message is returned by jobrunner when it finishes.
            # If no message, this means that the jobrunner process was killed.
            # 99% of times the jobrunner is killed due an OOM, so we assume here an OOM.
            msg = 'Function exceeded maximum memory and was killed'
            raise MemoryError('HANDLER', msg)

        if os.path.exists(job.stats_file):
            with open(job.stats_file, 'r') as fid:
                for l in fid.readlines():
                    key, value = l.strip().split(" ", 1)
                    try:
                        call_status.add(key, float(value))
                    except Exception:
                        call_status.add(key, value)
                    if key in ['exception', 'exc_pickle_fail', 'result']:
                        call_status.add(key, eval(value))

    except Exception:
        # internal runtime exceptions
        print('----------------------- EXCEPTION !-----------------------')
        traceback.print_exc(file=sys.stdout)
        print('----------------------------------------------------------')
        call_status.add('exception', True)

        pickled_exc = pickle.dumps(sys.exc_info())
        pickle.loads(pickled_exc)  # this is just to make sure they can be unpickled
        call_status.add('exc_info', str(pickled_exc))

    finally:
        call_status.add('worker_end_tstamp', time.time())

        # Flush log stream and save it to the call status
        job.log_stream.flush()
        with open(job.log_file, 'rb') as lf:
            log_str = base64.b64encode(zlib.compress(lf.read())).decode()
            call_status.add('logs', log_str)

        call_status.send_finish_event()

        # Unset specific env vars
        for key in job.extra_env:
            os.environ.pop(key, None)
        os.environ.pop('__LITHOPS_TOTAL_EXECUTORS', None)

        logger.info("Finished")
