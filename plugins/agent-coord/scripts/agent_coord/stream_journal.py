"""Single-writer, fsynced event spool for a retained transport generation.

The caller serializes access. Records become visible only after fsync; incomplete
last records are discarded on recovery. Complete corrupt records fail closed.
Offsets are memory-only; SQLite is never involved in event append or replay.
"""
from collections import deque
import hashlib
import json
import os
from pathlib import Path


class StreamJournal:
    def __init__(self, directory, *, segment_bytes=4 * 1024 * 1024):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_fd = os.open(self.directory.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        self.segment_bytes = segment_bytes
        self.records = deque()
        self.sequence = 0
        self.event_bytes = 0
        self.acknowledged = self.retained_after = 0
        self.fd = None
        self.path = None
        bounds = self.directory / 'bounds.json'
        if bounds.exists():
            value = json.loads(bounds.read_bytes())
            self.acknowledged, self.retained_after = value['acknowledged'], value['retained_after']
            self.sequence = self.retained_after
        files = sorted(self.directory.glob('*.jsonl'))
        for index, path in enumerate(files):
            with path.open('r+b') as source:
                while True:
                    offset = source.tell()
                    line = source.readline()
                    if not line:
                        break
                    if not line.endswith(b'\n'):
                        if index != len(files) - 1:
                            raise ValueError('Incomplete interior stream segment')
                        source.truncate(offset)
                        source.flush()
                        os.fsync(source.fileno())
                        break
                    envelope = json.loads(line)
                    payload = envelope['payload'].encode()
                    if hashlib.sha256(payload).hexdigest() != envelope['digest']:
                        raise ValueError('Stream journal checksum mismatch')
                    event = json.loads(payload)
                    sequence = event['sequence']
                    if sequence <= self.retained_after:
                        continue
                    if sequence != self.sequence + 1:
                        raise ValueError('Stream journal sequence discontinuity')
                    self.sequence = sequence
                    self.records.append((sequence, path, offset, len(line), event['size']))
                    self.event_bytes += event['size']
        if files:
            self.path = files[-1]
            self.fd = os.open(self.path, os.O_RDWR)
        if self.acknowledged > self.sequence:
            raise ValueError('Stream journal acknowledgement exceeds durable tail')

    def _sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def append(self, channel, stream, data, size):
        sequence = self.sequence + 1
        payload = json.dumps(dict(sequence=sequence, channel=channel, stream=stream,
                                  data=data, size=size), separators=(',', ':'))
        encoded = (json.dumps(dict(payload=payload, digest=hashlib.sha256(payload.encode()).hexdigest()),
                              separators=(',', ':')) + '\n').encode()
        if self.fd is None or os.fstat(self.fd).st_size >= self.segment_bytes:
            self.close()
            self.path = self.directory / f'{sequence:020d}.jsonl'
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            self._sync_directory()
        offset = os.lseek(self.fd, 0, os.SEEK_END)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(self.fd, view)
                if written <= 0:
                    raise OSError('Stream journal write made no progress')
                view = view[written:]
            os.fsync(self.fd)
        except BaseException:
            os.ftruncate(self.fd, offset)
            os.fsync(self.fd)
            raise
        self.records.append((sequence, self.path, offset, len(encoded), size))
        self.sequence = sequence
        self.event_bytes += size
        return sequence

    def events(self, after, limit, max_bytes):
        result, size = [], 0
        for sequence, path, offset, length, count in self.records:
            if sequence <= after:
                continue
            if len(result) >= limit or (result and size + count > max_bytes):
                break
            fd = os.open(path, os.O_RDONLY)
            try:
                envelope = json.loads(os.pread(fd, length, offset))
            finally:
                os.close(fd)
            if hashlib.sha256(envelope['payload'].encode()).hexdigest() != envelope['digest']:
                raise ValueError('Stream journal checksum mismatch')
            event = json.loads(envelope['payload'])
            result.append({key: event[key] for key in ('sequence', 'channel', 'stream', 'data')})
            size += count
        return result

    def acknowledge(self, cursor, retain_after):
        # Publish the floor before removing any record or segment. Recovery can
        # safely ignore old segments when interrupted during cleanup.
        temporary = self.directory / 'bounds.pending'
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            data = json.dumps(dict(acknowledged=cursor, retained_after=retain_after)).encode()
            with os.fdopen(fd, 'wb') as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.directory / 'bounds.json')
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)
        self.acknowledged, self.retained_after = cursor, retain_after
        retired_paths = set()
        while self.records and self.records[0][0] <= retain_after:
            _, path, _, _, count = self.records.popleft()
            self.event_bytes -= count
            retired_paths.add(path)
        retained_paths = {row[1] for row in self.records}
        for path in retired_paths - retained_paths:
            if path == self.path:
                self.close()
                self.path = None
            path.unlink(missing_ok=True)
        if retired_paths:
            self._sync_directory()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
