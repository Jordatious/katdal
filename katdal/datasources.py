################################################################################
# Copyright (c) 2017-2018, National Research Foundation (Square Kilometre Array)
#
# Licensed under the BSD 3-Clause License (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy
# of the License at
#
#   https://opensource.org/licenses/BSD-3-Clause
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

"""Various sources of correlator data and metadata."""

import urlparse
import os
import tempfile

import katsdptelstate
import redis
import numpy as np

from .sensordata import TelstateSensorData
from .chunkstore_rados import RadosChunkStore


class DataSourceNotFound(Exception):
    """File associated with DataSource not found or server not responding."""


class AttrsSensors(object):
    """Metadata in the form of attributes and sensors.

    Parameters
    ----------
    attrs : mapping from string to object
        Metadata attributes
    sensors : mapping from string to :class:`SensorData` objects
        Metadata sensor cache mapping sensor names to raw sensor data
    name : string, optional
        Identifier that describes the origin of the metadata (backend-specific)

    """
    def __init__(self, attrs, sensors, name='custom'):
        self.attrs = attrs
        self.sensors = sensors
        self.name = name


class VisFlagsWeights(object):
    """Correlator data in the form of visibilities, flags and weights.

    Parameters
    ----------
    vis : array-like of complex64, shape (*T*, *F*, *B*)
        Complex visibility data as a function of time, frequency and baseline
    flags : array-like of uint8, shape (*T*, *F*, *B*)
        Flags as a function of time, frequency and baseline
    weights : array-like of float32, shape (*T*, *F*, *B*)
        Visibility weights as a function of time, frequency and baseline
    name : string, optional
        Identifier that describes the origin of the data (backend-specific)

    """
    def __init__(self, vis, flags, weights, name='custom'):
        if not (vis.shape == flags.shape == weights.shape):
            raise ValueError("Shapes of vis %s, flags %s and weights %s differ"
                             % (vis.shape, flags.shape, weights.shape))
        self.vis = vis
        self.flags = flags
        self.weights = weights
        self.name = name

    @property
    def shape(self):
        return self.vis.shape


class ChunkStoreVisFlagsWeights(VisFlagsWeights):
    """Correlator data stored in a chunk store.

    Parameters
    ----------
    store : :class:`ChunkStore` object
        Chunk store
    base_name : string
        Name of dataset in store, as array name prefix (akin to a filename)
    chunk_info : dict mapping array name to info dict
        Dict specifying dtype, shape and chunks per array
    """
    def __init__(self, store, base_name, chunk_info):
        self.store = store
        da = {}
        for array, info in chunk_info.iteritems():
            array_name = store.join(base_name, array)
            da[array] = store.get_dask_array(array_name, info['chunks'],
                                             info['dtype'])
        vis = da['correlator_data']
        flags = da['flags']
        # Combine low-resolution weights and high-resolution weights_channel
        weights = da['weights'] * da['weights_channel'][..., np.newaxis]
        VisFlagsWeights.__init__(self, vis, flags, weights, base_name)


class DataSource(object):
    """A generic data source presenting both correlator data and metadata.

    Parameters
    ----------
    metadata : :class:`AttrsSensors` object
        Metadata attributes and sensors
    timestamps : array-like of float, length *T*
        Timestamps at centroids of visibilities in UTC seconds since Unix epoch
    data : :class:`VisFlagsWeights` object, optional
        Correlator data (visibilities, flags and weights)

    """
    def __init__(self, metadata, timestamps, data=None):
        self.metadata = metadata
        self.timestamps = timestamps
        self.data = data

    @property
    def name(self):
        name = self.metadata.name
        if self.data and self.data.name != name:
            name += ' | ' + self.data.name
        return name


class TelstateDataSource(DataSource):
    """A data source based on :class:`katsdptelstate.TelescopeState`."""
    def __init__(self, telstate, capture_block_id=None, stream_name=None,
                 source_name='telstate'):
        # Create telstate view based on capture block ID and/or stream name
        if stream_name:
            telstate = telstate.view(stream_name)
        if capture_block_id:
            telstate = telstate.view(capture_block_id)
        if stream_name and capture_block_id:
            cb_stream = telstate.SEPARATOR.join((capture_block_id, stream_name))
            telstate = telstate.view(cb_stream)
        self.telstate = telstate
        # Collect sensors
        sensors = {}
        for key in telstate.keys():
            if not telstate.is_immutable(key):
                sensors[key] = TelstateSensorData(telstate, key)
        metadata = AttrsSensors(telstate, sensors, name=source_name)
        try:
            base_name = telstate['chunk_name']
            chunk_info = telstate['chunk_info']
        except KeyError:
            # Metadata without data
            DataSource.__init__(self, metadata, None)
        else:
            # Extract VisFlagsWeights and timestamps from telstate
            with tempfile.NamedTemporaryFile() as f:
                f.write(telstate['ceph_conf'])
                f.flush()
                pool = telstate['ceph_pool']
                store = RadosChunkStore.from_config(f.name, pool)
            ts_name = store.join(base_name, 'timestamps')
            ts_chunks = chunk_info['timestamps']['chunks']
            ts_dtype = chunk_info['timestamps']['dtype']
            timestamps = store.get_dask_array(ts_name, ts_chunks, ts_dtype)
            # Make timestamps explicit, mutable (to be removed from store soon)
            timestamps = timestamps.compute().copy()
            data = ChunkStoreVisFlagsWeights(store, base_name, chunk_info)
            DataSource.__init__(self, metadata, timestamps, data)

    @classmethod
    def from_url(cls, url):
        """Construct TelstateDataSource from URL (RDB file / REDIS server)."""
        url_parts = urlparse.urlparse(url, scheme='file')
        kwargs = dict(urlparse.parse_qsl(url_parts.query))
        # Extract Redis database number if provided
        db = int(kwargs.pop('db', '0'))
        kwargs['source_name'] = url_parts.geturl()
        if url_parts.scheme == 'file':
            # RDB dump file
            telstate = katsdptelstate.TelescopeState()
            try:
                telstate.load_from_file(url_parts.path)
            except OSError as err:
                raise DataSourceNotFound(str(err))
            return cls(telstate, **kwargs)
        elif url_parts.scheme == 'redis':
            # Redis server
            try:
                telstate = katsdptelstate.TelescopeState(url_parts.netloc, db)
            except (redis.ConnectionError, redis.TimeoutError) as e:
                raise DataSourceNotFound(str(e))
            return cls(telstate, **kwargs)


def open_data_source(url):
    """Construct the data source described by the given URL."""
    try:
        return TelstateDataSource.from_url(url)
    except DataSourceNotFound as err:
        # Amend the error message for the case of an IP address without scheme
        url_parts = urlparse.urlparse(url, scheme='file')
        if url_parts.scheme == 'file' and not os.path.isfile(url_parts.path):
            raise DataSourceNotFound(
                '{} (add a URL scheme if {!r} is not meant to be a file)'
                .format(err, url_parts.path))