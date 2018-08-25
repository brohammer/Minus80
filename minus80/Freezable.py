#!/usr/bin/env python3
import tempfile
import re
# Suppress the warning until the next wersion
import bcolz as bcz

import apsw as lite
import os as os
import numpy as np
import pandas as pd

from .Config import cf
from contextlib import contextmanager


class Freezable(object):

    '''
    Freezable is an abstract class. Things that inherit from Freezable can
    be loaded and unloaded from the Minus80.

    A freezable object is a persistant object that lives in a known directory
    aimed to make expensive to build objects and databases loadable from
    new runtimes.

    The three main things that a Freezable object supplies are:
    * access to a sqlite database (relational records)
    * access to a bcolz databsase (columnar data)
    * access to a key/val store
    * access to named temp files

    '''

    def __init__(self, name, dtype=None, parent=None):
        '''
        Initialize the Freezable Object.

        Parameters
        ----------
        name : str
            The name of the frozen object.
        dtype : str, default=None
            The type of the frozen object (e.g. Cohort)
            If None, the type will be inferred from the
            object class.
        '''
        # Set the m80 name
        self._m80_name = name
        # Set the m80 dtype
        if dtype is None:
            # Just use the class type as the type
            self._m80_dtype = self.guess_type(self)
        else:
            self._m80_dtype = dtype
        # Keep track of children
        self._children = []
        
        # Set up our base directory
        if parent is None:
            # set as the top level basedir as specified in the config file
            self._basedir = os.path.join(
                cf.options.basedir,
                'databases',
                f'{self._m80_dtype}.{self._m80_name}'
            )
            self._parent = None
        else:
            self._basedir = os.path.join(
                parent._basedir,
                f'{self._m80_dtype}.{self._m80_name}'
            )
            self._parent = parent
        os.makedirs(self._basedir,exist_ok=True)

        # Get a handle to the sql database
        self._db = self._sqlite()

        try:
            cur = self._db.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS globals (
                    key TEXT,
                    val TEXT,
                    type TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uniqkey ON globals(key)
                ''')
        except TypeError:
            raise TypeError('{}.{} does not exist'.format(dtype, name))

    @contextmanager
    def bulk_transaction(self):
        '''
            This is a context manager that handles bulk transaction.
            i.e. this context will handle the BEGIN, END and appropriate
            ROLLBACKS.

            Usage:
            >>> with x.bulk_transaction() as cur:
                     cur.execute('INSERT INTO table XXX VALUES YYY')
        '''
        cur = self._db.cursor()
        cur.execute('PRAGMA synchronous = off')
        cur.execute('PRAGMA journal_mode = memory')
        cur.execute('SAVEPOINT bulk_transaction')
        try:
            yield cur
        except Exception as e:
            cur.execute('ROLLBACK TO SAVEPOINT bulk_transaction')
            raise e
        finally:
            cur.execute('RELEASE SAVEPOINT bulk_transaction')


    def _get_dbpath(self, extension, dtype=None, dbname=None):
        if dbname is None:
            dbname = self._m80_name
        if dtype is None:
            dtype = self._m80_type
        return os.path.expanduser(
            os.path.join(
                self._basedir,
                f'{dtype}.{dbname}',
                f'{extension}'
            )
        )



    def _dbfilename(self, dtype=None, dbname=None):
        '''
        Get the path to a database file.

        Parameters
        ----------
        dbname : str, default=None
            The frozen object name
        dtype : str, default=None
            The datatype of the frozen object

        '''
        return self._get_dbpath('db')

    def _sqlite(self):
        '''
            This is the access point to the sqlite database
        '''
        # return a connection if exists
        filename = os.path.join(self._basedir,"db.sqlite")
        return lite.Connection(filename)

    def _bcolz_array(self, name, array=None, m80name=None,
                     m80type=None):
        '''
            Routines to set/get arrays from the bcolz store
        '''
        # Fill in the defaults if they were not provided
        if m80type is None:
            m80type = self._m80_type
        if m80name is None:
            m80name = self._m80_name
        # function is a getter if df is provided
        path = self._get_dbpath('bcz')
        os.makedirs(path, exist_ok=True)
        if array is None:
            # GETTER
            arr = bcz.open(os.path.join(path, name))
            return arr
        else:
            # SETTER
            bcz.carray(array, mode='w', rootdir=os.path.join(path, name))

    def _bcolz(self, tblname, df=None, m80name=None, m80type=None,
               blaze=False):
        '''
            This is the access point to the bcolz database
        '''
        try:
            import blaze as blz
        except FutureWarning:
            pass
        import warnings
        # from flask.exthook import ExtDeprecationWarning
        # warnings.simplefilter('ignore', ExtDeprecationWarning)
        warnings.simplefilter('ignore', FutureWarning)

        # Fill in the defaults if they were not provided
        if m80type is None:
            m80type = self._m80_type
        if m80name is None:
            m80name = self._m80_name
        path = self._get_dbpath('bcz')
        os.makedirs(path, exist_ok=True)

        # function is a getter if df is provided
        if df is None:
            # return the dataframe if it exists
            try:
                df = bcz.open(os.path.join(path, tblname))
            except IOError:
                raise IOError(
                    f'could not open database for {m80type}:{m80name} '
                )
            else:
                if len(df) == 0:
                    df = pd.DataFrame()
                    if blaze:
                        df = blz.data(df)
                else:
                    if blaze:
                        df = blz.data(df)
                    else:
                        df = df.todataframe()
                if not blaze and 'idx' in df.columns.values:
                    df.set_index('idx', drop=True, inplace=True)
                    df.index.name = None
                return df
        # If df is set, then store the table
        else:
            if not(df.index.dtype_str == 'int64') and not (df.empty):
                df = df.copy()
                df['idx'] = df.index
            if isinstance(df, pd.DataFrame):
                path = os.path.join(path, tblname)
                if df.empty:
                    bcz.fromiter(
                        (), dtype=np.int32, mode='w',
                        count=0, rootdir=path
                    )
                else:
                    bcz.ctable.fromdataframe(df, mode='w', rootdir=path)
            if 'idx' in df.columns.values:
                del df
            return

    @staticmethod
    def _tmpfile(*args, **kwargs):
        # returns a handle to a tmp file
        return tempfile.NamedTemporaryFile(
            'w',
            dir=os.path.expanduser(
                os.path.join(
                    # use the top level basedir
                    cf.options.basedir,
                    "tmp"
                )
            ),
            **kwargs
        )

    def _dict(self, key, val=None):
        '''
            Stores global variables for the freezable object. The
            method will automatically infer in the val type is in
            [int, float, str]. If the value is not one of these, an
            excpetion will be raised.

            Parameters
            ----------
            key : str
                the dictionary key
            val : int, float, or str
                the value corresponding to the key

        '''
        try:
            if val is not None:
                val_type = self.guess_type(val)
                if val_type not in ('int', 'float', 'str'):
                    raise TypeError(
                        f'val must be in [int, float, str], not {val_type}'
                    )
                self._db.cursor().execute(
                    '''
                    INSERT OR REPLACE INTO globals
                    (key, val, type)VALUES (?, ?, ?)''', (key, val, val_type)
                )
            else:
                (valtype, value) = self._db.cursor().execute(
                    '''SELECT type, val FROM globals WHERE key = ?''', (key, )
                ).fetchone()
                if valtype == 'int':
                    return int(value)
                elif valtype == 'float':
                    return float(value)
                elif valtype == 'str':
                    return str(value)
        except TypeError:
            raise ValueError('{} not in database'.format(key))

    @staticmethod
    def guess_type(object):
        '''
            Guess the type of object from the class attribute
        '''
        # retrieve a list of classes
        classes = re.match(
            "<class '(.+)'>",
            str(object.__class__)
        ).groups()[0].split('.')
        # Return the most specific one
        return classes[-1]
