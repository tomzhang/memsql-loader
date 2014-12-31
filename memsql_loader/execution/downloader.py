import pycurl
import select
import sys
import threading
import time
import os

from boto.exception import S3ResponseError
from boto.s3.connection import S3Connection
from memsql_loader.execution.errors import WorkerException, ConnectionException, RequeueTask
from memsql_loader.util import log, webhdfs
from wraptor.decorators import throttle
from memsql_loader.util.attr_dict import AttrDict
from memsql_loader.vendor import glob2
from pywebhdfs.webhdfs import PyWebHdfsClient
import pywebhdfs.errors

DOWNLOAD_TIMEOUT = 30

class DownloadMetrics(object):
    def __init__(self, total_size):
        self._total_size = total_size
        self._current_size = 0
        self._last_snapshot = 0
        self._last_change = time.time()
        self._snapshots = []
        self._avg_len = 30

    def accumulate_bytes(self, current):
        self._current_size = current
        self.snapshot()

    @property
    def last_change(self):
        return self._last_change

    @throttle(1, instance_method=True)
    def ping(self):
        self._last_change = time.time()

    @throttle(1, instance_method=True)
    def snapshot(self):
        # Local copy made here to prevent race conditions
        current = self._current_size
        diff = current - self._last_snapshot

        # update our last change time so long as our speed is above 10 bytes per
        # second or we have only been downloading for 30 seconds
        if diff > 10:
            self.ping()

        self._snapshots.append(diff)
        if len(self._snapshots) > self._avg_len:
            self._snapshots = self._snapshots[(-1 * self._avg_len):]

        self._last_snapshot = current

    def get_stats(self):
        if self._current_size == self._total_size or len(self._snapshots) == 0:
            rate = 0
        else:
            # Not super efficient, but shouldn't really matter with max 30 values
            rate = sum(self._snapshots) / len(self._snapshots)

        time_left = -1 if rate == 0 else (self._total_size - self._current_size) / rate

        return {
            'bytes_downloaded': self._current_size,
            'download_rate': rate,
            'time_left': time_left
        }

class Downloader(threading.Thread):
    def __init__(self):
        super(Downloader, self).__init__()
        self.logger = log.get_logger('downloader')
        self._error = None
        self._tb = None
        self._should_exit = False

        self._last_size = -1
        self._last_download_time = 0

    def terminate(self):
        self._should_exit = True

    @property
    def error(self):
        return self._error

    @property
    def traceback(self):
        return self._tb

    def load(self, job, task, fifo):
        self.job = job
        self.task = task
        self.fifo = fifo
        self.key = None

        if task.data['scheme'] == 's3':
            self.is_anonymous = job.spec.source.aws_access_key is None or job.spec.source.aws_secret_key is None
            if self.is_anonymous:
                s3_conn = S3Connection(anon=True)
            else:
                s3_conn = S3Connection(job.spec.source.aws_access_key, job.spec.source.aws_secret_key)
            bucket = s3_conn.get_bucket(task.data['bucket'])

            try:
                self.key = bucket.get_key(task.data['key_name'])
            except S3ResponseError as e:
                raise WorkerException("Received %s %s accessing `%s`, aborting" % (e.status, e.reason, task.data['key_name']))
        elif task.data['scheme'] == 'hdfs':
            fname = task.data['key_name']
            client = PyWebHdfsClient(
                job.spec.source.hdfs_host,
                job.spec.source.webhdfs_port,
                user_name=job.spec.source.hdfs_user)
            try:
                filesize = client.get_file_dir_status(fname)['FileStatus']['length']
            except pywebhdfs.errors.FileNotFound:
                raise WorkerException("File '%s' does not exist on HDFS" % fname)
            self.key = AttrDict({'name': fname, 'size': filesize})
        elif task.data['scheme'] == 'file':
            globber = glob2.Globber()
            fname = globber._normalize_string(task.data['key_name'])

            if not os.path.exists(fname):
                raise WorkerException("File '%s' does not exist on this filesystem" % fname)
            elif not os.path.isfile(fname):
                raise WorkerException("File '%s' exists, but is not a file" % fname)

            self.key = AttrDict({'name': fname, 'size': os.path.getsize(fname)})
        else:
            raise WorkerException('Unsupported job with paths: %s' % [ str(p) for p in self.job.paths ])

        if self.key is None:
            raise WorkerException('Failed to find key associated with task ID %s' % task.task_id)

        self.metrics = DownloadMetrics(self.key.size)

    def run(self):
        try:
            try:
                # This is at the top so that any exceptions that occur will
                # emit a KILL QUERY due to fifo.open()
                with self.fifo.open() as target_file:
                    # allocate an URL for the target file
                    if self.task.data['scheme'] == 's3':
                        if self.is_anonymous:
                            key_url = 'http://%(bucket)s.s3.amazonaws.com/%(path)s' % {
                                'bucket': self.key.bucket.name,
                                'path': self.key.name.encode('utf-8')
                            }
                        else:
                            key_url = self.key.generate_url(expires_in=3600)
                    elif self.task.data['scheme'] == 'hdfs':
                        host = self.job.spec.source.hdfs_host
                        port = self.job.spec.source.webhdfs_port
                        hdfs_user = self.job.spec.source.hdfs_user
                        key_name = self.key.name
                        key_url = webhdfs.get_webhdfs_url(
                            host, port, hdfs_user, 'OPEN', key_name)
                    elif self.task.data['scheme'] == 'file':
                        key_url = 'file://%(path)s' % {'path': self.key.name}
                    else:
                        assert False, 'Unsupported job with paths: %s' % [ str(p) for p in self.job.paths ]

                    self._curl = curl = pycurl.Curl()
                    curl.setopt(pycurl.URL, key_url)
                    curl.setopt(pycurl.NOPROGRESS, 0)
                    curl.setopt(pycurl.PROGRESSFUNCTION, self._progress)
                    curl.setopt(pycurl.SSL_VERIFYPEER, 0)
                    curl.setopt(pycurl.SSL_VERIFYHOST, 0)
                    curl.setopt(pycurl.CONNECTTIMEOUT, 30)
                    curl.setopt(pycurl.WRITEFUNCTION, self._write_to_fifo(target_file))
                    if self.task.data['scheme'] == 'hdfs':
                        curl.setopt(pycurl.FOLLOWLOCATION, True)

                    self.logger.info('Starting download')
                    with self.task.protect():
                        self.task.start_step('download')

                    try:
                        curl.perform()
                        status_code = curl.getinfo(pycurl.HTTP_CODE)
                        # Catch HTTP client errors, e.g. 404:
                        if status_code >= 400 and status_code < 500:
                            raise WorkerException('HTTP status code %s for file %s' % (status_code, self.key.name))
                    finally:
                        with self.task.protect():
                            self.task.stop_step('download')
            except pycurl.error as e:
                errno = e.args[0]
                if errno == pycurl.E_ABORTED_BY_CALLBACK and not self._should_exit:
                    self.logger.warn('Download failed...requeueing')
                    # Caught by the outer `except Exception as e`
                    raise RequeueTask()
                else:
                    # Caught by the outer `except pycurl.error as e`
                    raise
        except pycurl.error as e:
            errno = e.args[0]
            self._set_error(ConnectionException('libcurl error #%d. Lookup error here: http://curl.haxx.se/libcurl/c/libcurl-errors.html' % errno))
        except IOError as e:
            # This is raised sometimes instead of a pycurl error
            self._set_error(ConnectionException('IOError: %s (%d)' % (e.args[1], e.args[0])))
        except Exception as e:
            self._set_error(e)
        except KeyboardInterrupt:
            pass
        finally:
            self.logger.info('Finished downloading')

    def _set_error(self, err):
        self._error = err
        self._tb = sys.exc_info()[2]
        self.logger.debug("Downloader failed: %s." % (err), exc_info=True)

    def _progress(self, dltotal, dlnow, ultotal, ulnow):
        self.metrics.accumulate_bytes(dlnow)

        if self._should_exit or time.time() > self.metrics.last_change + DOWNLOAD_TIMEOUT:
            return 1

    def _write_to_fifo(self, target_file):
        def _write_to_fifo_helper(data):
            to_write = data
            while len(to_write) > 0:
                # First step is to wait until we can write to the FIFO.
                #
                # Wait for half of the download timeout for the FIFO to become open
                # for writing.  While we're doing this, ping the download metrics
                # so that the worker doesn't assume this download has hung.
                is_writable = False
                while not is_writable:
                    self.metrics.ping()
                    timeout = DOWNLOAD_TIMEOUT / 2
                    _, writable_objects, _ = select.select(
                        [ ], [ target_file ], [ ], timeout)
                    is_writable = bool(writable_objects)

                # Then, we write as much as we can within this opportunity to write
                written_bytes = os.write(target_file.fileno(), to_write)
                assert written_bytes >= 0, "Expect os.write() to return non-negative numbers"
                to_write = to_write[written_bytes:]

        return _write_to_fifo_helper