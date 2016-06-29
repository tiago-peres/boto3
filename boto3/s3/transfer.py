# Copyright 2015 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Abstractions over S3's upload/download operations.

This module provides high level abstractions for efficient
uploads/downloads.  It handles several things for the user:

* Automatically switching to multipart transfers when
  a file is over a specific size threshold
* Uploading/downloading a file in parallel
* Progress callbacks to monitor transfers
* Retries.  While botocore handles retries for streaming uploads,
  it is not possible for it to handle retries for streaming
  downloads.  This module handles retries for both cases so
  you don't need to implement any retry logic yourself.

This module has a reasonable set of defaults.  It also allows you
to configure many aspects of the transfer process including:

* Multipart threshold size
* Max parallel downloads
* Socket timeouts
* Retry amounts

There is no support for s3->s3 multipart copies at this
time.


.. _ref_s3transfer_usage:

Usage
=====

The simplest way to use this module is:

.. code-block:: python

    client = boto3.client('s3', 'us-west-2')
    transfer = S3Transfer(client)
    # Upload /tmp/myfile to s3://bucket/key
    transfer.upload_file('/tmp/myfile', 'bucket', 'key')

    # Download s3://bucket/key to /tmp/myfile
    transfer.download_file('bucket', 'key', '/tmp/myfile')

The ``upload_file`` and ``download_file`` methods also accept
``**kwargs``, which will be forwarded through to the corresponding
client operation.  Here are a few examples using ``upload_file``::

    # Making the object public
    transfer.upload_file('/tmp/myfile', 'bucket', 'key',
                         extra_args={'ACL': 'public-read'})

    # Setting metadata
    transfer.upload_file('/tmp/myfile', 'bucket', 'key',
                         extra_args={'Metadata': {'a': 'b', 'c': 'd'}})

    # Setting content type
    transfer.upload_file('/tmp/myfile.json', 'bucket', 'key',
                         extra_args={'ContentType': "application/json"})


The ``S3Transfer`` class also supports progress callbacks so you can
provide transfer progress to users.  Both the ``upload_file`` and
``download_file`` methods take an optional ``callback`` parameter.
Here's an example of how to print a simple progress percentage
to the user:

.. code-block:: python

    class ProgressPercentage(object):
        def __init__(self, filename):
            self._filename = filename
            self._size = float(os.path.getsize(filename))
            self._seen_so_far = 0
            self._lock = threading.Lock()

        def __call__(self, bytes_amount):
            # To simplify we'll assume this is hooked up
            # to a single filename.
            with self._lock:
                self._seen_so_far += bytes_amount
                percentage = (self._seen_so_far / self._size) * 100
                sys.stdout.write(
                    "\\r%s  %s / %s  (%.2f%%)" % (
                        self._filename, self._seen_so_far, self._size,
                        percentage))
                sys.stdout.flush()


    transfer = S3Transfer(boto3.client('s3', 'us-west-2'))
    # Upload /tmp/myfile to s3://bucket/key and print upload progress.
    transfer.upload_file('/tmp/myfile', 'bucket', 'key',
                         callback=ProgressPercentage('/tmp/myfile'))



You can also provide a TransferConfig object to the S3Transfer
object that gives you more fine grained control over the
transfer.  For example:

.. code-block:: python

    client = boto3.client('s3', 'us-west-2')
    config = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        max_concurrency=10,
        num_download_attempts=10,
    )
    transfer = S3Transfer(client, config)
    transfer.upload_file('/tmp/foo', 'bucket', 'key')


"""
from botocore.exceptions import ClientError
from s3transfer.exceptions import RetriesExceededError as \
    S3TransferRetriesExceededError
from s3transfer.manager import TransferConfig as S3TransferConfig
from s3transfer.manager import TransferManager
from s3transfer.subscribers import BaseSubscriber
from s3transfer.utils import OSUtils as S3TransferOSUtils

from boto3.exceptions import RetriesExceededError, S3UploadFailedError


MB = 1024 * 1024


class OSUtils(S3TransferOSUtils):
    def open_file_chunk_reader(self, filename, start_byte, size, callback):
        callbacks = None
        if callback:
            # We need to wrap the callback in the ProgressCallbackInvoker
            # because the read callbacks will always be invoked with
            # the keyword argument ``bytes_transferred`` which would break
            # if the callback relied on a positional argument of a different
            # name, which most likely did.
            callbacks = [ProgressCallbackInvoker(callback).on_progress]
        return super(OSUtils, self).open_file_chunk_reader(
            filename, start_byte, size, callbacks)


class TransferConfig(S3TransferConfig):
    ALIAS = {
        'max_concurrency': 'max_request_concurrency',
        'max_io_queue': 'max_io_queue_size'
    }

    def __init__(self,
                 multipart_threshold=8 * MB,
                 max_concurrency=10,
                 multipart_chunksize=8 * MB,
                 num_download_attempts=5,
                 max_io_queue=100):
        super(TransferConfig, self).__init__(
            multipart_threshold=multipart_threshold,
            max_request_concurrency=max_concurrency,
            multipart_chunksize=multipart_chunksize,
            num_download_attempts=num_download_attempts,
            max_io_queue_size=max_io_queue
        )
        # Some of the argument names are not the same as the inherited
        # S3TransferConfig so we add aliases so you can still access the
        # old version of the names.
        for alias in self.ALIAS:
            setattr(self, alias, getattr(self, self.ALIAS[alias]))

    def __setattr__(self, name, value):
        # If the alias name is used, make sure we set the name that it points
        # to as that is what actually is used in governing the TransferManager.
        if name in self.ALIAS:
            super(TransferConfig, self).__setattr__(self.ALIAS[name], value)
        # Always set the value of the actual name provided.
        super(TransferConfig, self).__setattr__(name, value)


class S3Transfer(object):
    ALLOWED_DOWNLOAD_ARGS = TransferManager.ALLOWED_DOWNLOAD_ARGS
    ALLOWED_UPLOAD_ARGS = TransferManager.ALLOWED_UPLOAD_ARGS

    def __init__(self, client, config=None, osutil=None):
        if config is None:
            config = TransferConfig()
        if osutil is None:
            osutil = OSUtils()
        self._manager = TransferManager(client, config, osutil)

    @classmethod
    def from_transfer_manager(cls, transfer_manager):
        """Instantiate S3Transfer from s3transfer.TransferManager instance"""
        cls_instance = super(S3Transfer, cls).__new__(cls)
        cls_instance._manager = transfer_manager
        return cls_instance

    def upload_file(self, filename, bucket, key,
                    callback=None, extra_args=None):
        """Upload a file to an S3 object.

        Variants have also been injected into S3 client, Bucket and Object.
        You don't have to use S3Transfer.upload_file() directly.
        """
        subscribers = self._get_subscribers(callback)
        future = self._manager.upload(
            filename, bucket, key, extra_args, subscribers)
        try:
            future.result()
        # If a client error was raised, add the backwards compatibility layer
        # that raises a S3UploadFailedError. These specific errors were only
        # ever thrown for upload_parts but now can be thrown for any related
        # client error.
        except ClientError as e:
            raise S3UploadFailedError(
                "Failed to upload %s to %s: %s" % (
                    filename, '/'.join([bucket, key]), e))

    def download_file(self, bucket, key, filename, extra_args=None,
                      callback=None):
        """Download an S3 object to a file.

        Variants have also been injected into S3 client, Bucket and Object.
        You don't have to use S3Transfer.download_file() directly.
        """
        subscribers = self._get_subscribers(callback)
        future = self._manager.download(
            bucket, key, filename, extra_args, subscribers)
        try:
            future.result()
        # This is for backwards compatibility where when retries are
        # exceeded we need to throw the same error from boto3 instead of
        # s3transfer's built in RetriesExceededError as current users are
        # catching the boto3 one instead of the s3transfer exception to do
        # their own retries.
        except S3TransferRetriesExceededError as e:
            raise RetriesExceededError(e.last_exception)

    def _get_subscribers(self, callback):
        if not callback:
            return None
        return [ProgressCallbackInvoker(callback)]


class ProgressCallbackInvoker(BaseSubscriber):
    """A back-compat wrapper to invoke a provided callback via a subscriber

    :param callback: A callable that takes a single positional argument for
        how many bytes were transferred.
    """
    def __init__(self, callback):
        self._callback = callback

    def on_progress(self, bytes_transferred, **kwargs):
        self._callback(bytes_transferred)
