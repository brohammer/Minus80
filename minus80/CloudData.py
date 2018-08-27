from .Tools import get_files
from .Config import cf

import os
import lzma
import sys
import threading
import tarfile
from collections import defaultdict


__all__ = ['CloudData']


def CloudData(engine='s3'):
    if engine == 's3':
        return S3CloudData()
    else:
        raise ValueError(f'Cannot use {engine} as a cloud engine.')


class BaseCloudData(object):
    def __init__(self):
        pass

    def push(self, name, dtype, raw=False, compress=False):
        pass

    def pull(self, name, dtype, raw=False):
        pass

    def list(self, name=None, dtype=None, raw=None):
        pass

class ProgressPercentage(object):
    '''
    Borrowed from: https://boto3.readthedocs.io/en/latest/_modules/boto3/s3/transfer.html
    '''
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
                "\r%s  %s / %s  (%.2f%%)" % (
                    self._filename, self._seen_so_far, self._size,
                    percentage))
            sys.stdout.flush()

class ProgressDownloadPercentage(object):
    def __init__(self,filename,total_bytes):
        self._filename = filename
        self._total_bytes = total_bytes
        self._seen_so_far = 0
        self._lock = threading.Lock()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount
            percentage = (self._seen_so_far / self._total_bytes) * 100
            sys.stdout.write(
                "\r%s  %s / %s  (%.2f%%)" % (
                    self._filename, self._seen_so_far, self._total_bytes,
                    percentage)
            )
            sys.stdout.flush()

class S3CloudData(BaseCloudData):

    '''
    CloudData objects allow minus80 to interact with the cloud to store both
    prepared as well as raw datasets.
    '''


    def __init__(self):
        '''
        Create a CloudData object. Once proper S3 credentials are stored in the
        config file (~/.minus80.conf) initialization takes no arguments.
        '''
        try:
            import boto3
            from botocore.client import Config
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                'boto3 must be installed to use this feature'
            )

        if cf.cloud.access_key == 'None' or cf.cloud.secret_key == 'None':
            raise ValueError('Fill in your S3 Credentials in ~/.minus80.conf')

        self.s3 = boto3.client(
            service_name='s3',
            endpoint_url=cf.cloud.endpoint,
            aws_access_key_id=cf.cloud.access_key,
            aws_secret_access_key=cf.cloud.secret_key,
            config=Config(s3={'addressing_style': 'path'})
        )
        self.bucket = f'minus80_{cf.cloud.access_key}'

        # make sure the minus80 bucket exists
        if self.bucket not in [x['Name'] for x in self.s3.list_buckets()['Buckets']]:
            # Append access key to bucket name so multiple users can use host
            self.s3.create_bucket(Bucket=self.bucket)


    def push(self, dtype, name, raw=False, compress=False):
        '''
        Store a minus80 dataset in the cloud using its name and dtype (e.g. Cohort).
        the dtype is the name of the Freezable class or object. See :ref:`freezable`.
        Assume we are storing ``x = Cohort('experiment1')``

        dtype : str
            The type of freezable object (i.e. 'Cohort')
        name : str
            The name of the dataset (i.e. 'experiment1')
        raw : bool, default=False
            If True, raw files can be stored in the cloud. In this case, name changes
            to the file name and dtype changes to a string representing the future dtype
            or anything that describes the type of data that is being stored.
        compress : bool, default=False
            If true, lzma compression will be performed (this is slower)
        '''
        from boto3.s3.transfer import S3Transfer
        transfer = S3Transfer(self.s3)
        if raw == True:
            # The name is a FILENAME
            filename = name
            key = os.path.basename(filename)
            if compress:
                with open(filename, 'rb') as OUT:
                    self.s3.upload_fileobj(lzma.compress(OUT.read()), self.bucket, f'Raw/{dtype}.{key}.xz')
            else:
                transfer.upload_file(filename, self.bucket, f'Raw/{dtype}.{key}',
                    callback=ProgressPercentage(filename)
                )
        else:
            key = f'{dtype}.{name}' 
            data_path = os.path.join(
                cf.options.basedir,
                'databases', key
            )
            if not os.path.exists(data_path):
                raise ValueError('There were no datasets with that name')
            # Tar it up
            tarpath = os.path.join(cf.options.basedir,'tmp',key+'.tar')
            tar = tarfile.open(tarpath,'w')
            tar.add(data_path,recursive=True,arcname=key)
            transfer.upload_file(tarpath, self.bucket, f'databases/{key}',
                callback=ProgressPercentage(tarpath)        
            )
            os.unlink(tarpath)

    def pull(self, dtype, name, raw=False, output=None):
        '''
        Retrive a minus80 dataset in the cloud using its name and dtype (e.g. Cohort).
        the dtype is the name of the Freezable class or object. See :ref:`freezable`.
        Assume we are storing ``x = Cohort('experiment1')``

        dtype : str
            The type of freezable object (i.e. 'Cohort')
        name : str
            The name of the dataset (i.e. 'experiment1')
        raw : bool, default=False
            If True, raw files can be stored in the cloud. In this case, name changes
            to the file name and dtype changes to a string representing the future dtype
            or anything that describes the type of data that is being stored.
        '''

        from boto3.s3.transfer import S3Transfer
        transfer = S3Transfer(self.s3)
        if raw == True:
            key = os.path.basename(name)
            # get the number of bytes in the object
            num_bytes = self.s3.list_objects(
                    Bucket=self.bucket, 
                    Prefix=f'Raw/{dtype}.{key}'
            )['Contents'][0]['Size']
            if output is None:
                output = name
            # download
            transfer.download_file(
                self.bucket,
                f'Raw/{dtype}.{key}',
                output,
                callback = ProgressDownloadPercentage(output,num_bytes)
            )
        else:
            raise NotImplementedError('This functionality is currently only available for raw data')

    def list(self, name=None, dtype=None, raw=False):
        items = defaultdict(set)
        try:
            for item in self.s3.list_objects(Bucket=self.bucket)['Contents']:
                key = item['Key']
                if key.startswith('Raw') and raw == False:
                    pass
                elif not key.startswith('Raw') and raw == True:
                    pass
                else:
                    bucket, key = key.split('/')
                    key_dtype, key_name = key.split('.',maxsplit=1)
                    if dtype != None and key_dtype != dtype:
                        pass
                    elif name != None and not key_name.startswith(name):
                        pass
                    else:
                        items[key_dtype].add(key_name)
            if len(items) == 0:
                print('Nothing here yet!')
            else:
                if raw:
                    print('######   Raw Data:   ######')
                for key, vals in items.items():
                    print(f'-----{key}------')
                    for i,name in enumerate(vals,1):
                        print(f'{i}. {name}')
        except KeyError:
            if len(items) == 0:
                print('Nothing here yet!')

    def remove(self, dtype, name, raw=False):
        if raw:
            key = f'Raw/{dtype}.{name}'
        else:
            key = f'databases/{dtype}.{name}'
        self.s3.delete_object(Bucket=self.bucket,Key=key)
