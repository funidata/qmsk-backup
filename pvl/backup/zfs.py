import contextlib
import datetime
import logging

import pvl.invoke

log = logging.getLogger('pvl.backup.zfs')

ZFS = '/sbin/zfs'

class Error (Exception):
    pass

class ZFSError (Error):
    """
        An error occured
    """

    pass

class CommandError (Error):
    """
        Invalid command line options were specified
    """

    pass

def zfs(*args, sudo=None):
    try:
        stdout = pvl.invoke.invoke(ZFS, pvl.invoke.optargs(*args), sudo=sudo)
    except pvl.invoke.InvokeError as error:
        if error.exit == 1:
            raise ZFSError(error.stderr)
        elif error.exit == 2:
            raise CommandError(error.stderr)
        else:
            raise Error(error.stderr)

    return [line.strip().split('\t') for line in stdout]

@contextlib.contextmanager
def snapshot(zfs, snapshot_name=None, **opts):
    """
        With ZFS snapshot.

        Generates a temporary pvl-backup_* snapshot by default.
    """

    if snapshot_name is None:
        snapshot_name = 'pvl-backup_{timestamp}'.format(timestamp=datetime.datetime.now().isoformat())

    snapshot = zfs.snapshot(snapshot_name, **opts)

    try:
        yield snapshot
    finally:
        snapshot.destroy()

def open(name, **opts):
    """
        Return Filesystem for pool/zfs name.
    """

    zfs = Filesystem(name, **opts)
    zfs.check()

    return zfs

class Filesystem (object):
    @classmethod
    def list(cls):
        for name, in zfs('list', '-H', '-tfilesystem', '-oname'):
            yield cls(name)

    def __init__(self, name, noop=None, sudo=None):
        self.name = str(name)
        self.noop = noop
        self.sudo = sudo

        # cache
        self._snapshots = None

    def __str__(self):
        return self.name

    def zfs_read (self, *args):
        """
            ZFS wrapper for sudo+noop

            Run read-only commands that are also executed when --noop.
        """

        return zfs(*args, sudo=self.sudo)

    def zfs_write (self, *args):
        """
            ZFS wrapper for sudo+noop

            Run commands that are not executed when --noop.
        """

        if self.noop:
            return log.warning("noop: zfs %v", args)
        else:
            return zfs(*args, sudo=self.sudo)

    def check(self):
        """
            Raises ZFSError if unable to list the zfs filesystem.
        """

        self.zfs_read('list', '-tfilesystem', self.name)

    def get(self, property_name):
        """
            Get property value.

            Returns None if the property does not exist or is not set.
        """

        for fs, property_name, value, source in self.zfs_read('get', '-H', property_name, self.name):
            if value == '-' and source == '-':
                return None
            else:
                return value

    def set(self, property, value):
        self.zfs_write('set', '{property}={value}'.format(property=property, value=value), self.name)        

    @property
    def mountpoint(self):
        mountpoint = self.get('mountpoint')

        if mountpoint == 'none':
            return None
        else:
            return mountpoint

    def create(self, properties={}):
        options = ['-o{property}={value}'.format(property=key, value=value) for key, value in properties.items() if value is not None]
        args = options + [self.name]
        
        self.zfs_write('create', *args)

    def parse_snapshot(self, name, **opts):
        filesystem, snapshot = name.split('@', 1)

        return Snapshot(self, snapshot,
            noop    = self.noop,
            **opts
        )

    def list_snapshots(self, *properties):
        o = ','.join(('name', 'userrefs') + properties)

        for name, userrefs, *propvalues in self.zfs_read('list', '-H', '-tsnapshot', '-o' + o, '-r', self.name):
            snapshot = self.parse_snapshot(name,
                    userrefs    = int(userrefs),
                    properties  = {name: (None if value == '-' else value) for name, value in zip(properties, propvalues)},
            )

            log.debug("%s: snapshot %s", self, snapshot)

            yield snapshot

    @property
    def snapshots(self):
        if not self._snapshots:
            self._snapshots = {snapshot.name: snapshot for snapshot in self.list_snapshots()}

        return self._snapshots

    def snapshot(self, name, properties=None):
        """
            Create and return a new Snapshot()

            Raises ZFSError if the snapshot already exists.
        """
        
        options = ['-o{property}={value}'.format(property=key, value=value) for key, value in properties.items() if value is not None]

        snapshot = Snapshot(self, name, properties, noop=self.noop)
        args = options + [snapshot]

        self.zfs_write('snapshot', *args)
            
        if self._snapshots:
            self._snapshots[name] = snapshot

        return snapshot

    def holds (self, *snapshots):
        """
            List snapshot holds.

            Yields (Snapshot, hold_tag).
        """

        if not snapshots:
            snapshots = list(self.list_snapshots())

        for name, tag, timestamp in self.zfs_read('holds', '-H', *snapshots):
            snapshot = self.parse_snapshot(name.strip())

            yield snapshot, tag.strip()

    def bookmark(self, snapshot, bookmark):
        self.zfs_write('bookmark', '{snapshot}@{filesystem}'.format(snapshot=snapshot, filesystem=self.name), bookmark)

class Snapshot (object):
    @classmethod
    def parse(cls, name, **opts):
        filesystem, snapshot = name.split('@', 1)

        return cls(filesystem, snapshot, **opts)

    def __init__ (self, filesystem, name, properties={}, noop=None, userrefs=None):
        self.filesystem = filesystem
        self.name = name
        self.properties = properties

        self.noop = noop
        self.userrefs = userrefs

    def __str__ (self):
        return '{filesystem}@{name}'.format(name=self.name, filesystem=self.filesystem)

    def __getitem__ (self, name):
        return self.properties[name]

    # XXX: invalidate ZFS._snapshots cache
    def destroy (self):
        self.filesystem.zfs_write('destroy', self)

    def hold (self, tag):
        self.filesystem.zfs_write('hold', tag, self)

    def holds (self):
        for name, tag, timestamp in self.filesystem.zfs_read('holds', self):
            yield tag

    def release(self, tag):
        self.filesystem.zfs_write('release', tag, self)
