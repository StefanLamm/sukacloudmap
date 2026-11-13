"""Shared ordinary-driving reference graph with a strict proof boundary.

Small metric rows may guide candidate search even after their TTL. They are
never execution evidence. Full paths are loaded, hashed, checked and age-tested
only by ``get_recent``. The graph contains no API keys or HTTP client.
"""
from collections import OrderedDict
from copy import deepcopy
import gzip
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import zlib

from app.planning_inputs import content_hash, encode


SCHEMA = '''CREATE TABLE IF NOT EXISTS metrics(
provider_key TEXT PRIMARY KEY,from_id TEXT,to_id TEXT,
meters INTEGER,seconds INTEGER,received_at INTEGER,evidence_hash TEXT,content BLOB)'''
HASH = re.compile(r'^[0-9a-f]{64}$')
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


class ReferenceGraph:
    def __init__(self, path, *, ttl_seconds=3600, geometry_cache_size=96):
        self.path = Path(path).resolve() if path is not None else None
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.geometry_cache_size = max(1, int(geometry_cache_size))
        self.metrics = {}
        self._geometry = OrderedDict()
        self._reader = None
        self._lock = threading.RLock()
        self._closed = False
        self.refresh()

    @staticmethod
    def _metric(row):
        try:
            values = {key: row[key] for key in ('meters', 'seconds', 'received_at', 'evidence_hash')}
            if any(isinstance(values[key], bool) or not isinstance(values[key], int) or values[key] < 0
                   for key in ('meters', 'seconds', 'received_at')):
                return None
            if not isinstance(values['evidence_hash'], str) or not HASH.fullmatch(values['evidence_hash']):
                return None
            return values
        except (KeyError, TypeError, IndexError):
            return None

    def _connect_reader(self):
        if self._closed or self.path is None or not self.path.is_file():
            return None
        if self._reader is None:
            self._reader = sqlite3.connect(f'{self.path.as_uri()}?mode=ro', uri=True,
                                          timeout=.1, check_same_thread=False)
            self._reader.row_factory = sqlite3.Row
            self._reader.execute('PRAGMA busy_timeout=100')
        return self._reader

    def _discard_reader(self):
        if self._reader is not None:
            try:self._reader.close()
            except sqlite3.Error:pass
            self._reader = None

    def refresh(self):
        """Reload only metadata. Failure leaves an empty, safe hint graph."""
        with self._lock:
            if self._closed:
                return 0
            try:
                self._discard_reader()
                connection = self._connect_reader()
                if connection is None:
                    self.metrics = {}
                    return 0
                rows = connection.execute('SELECT provider_key,meters,seconds,received_at,evidence_hash FROM metrics')
                metrics = {}
                for row in rows:
                    value = self._metric(row)
                    if value is not None and isinstance(row['provider_key'], str) and HASH.fullmatch(row['provider_key']):
                        metrics[row['provider_key']] = value
                self.metrics = metrics
                self._geometry.clear()
                return len(metrics)
            except (OSError, sqlite3.Error, ValueError, TypeError):
                self.metrics = {}
                self._geometry.clear()
                self._discard_reader()
                return 0

    def get_metrics(self, provider_key):
        """Nonbinding measured search hints; no disk or geometry reads here."""
        with self._lock:
            if self._closed or not isinstance(provider_key, str):
                return None
            value = self.metrics.get(provider_key)
            if value is None:
                return None
            age = int(time.time()) - value['received_at']
            return {**value, 'heuristic_only': True, 'fresh': 0 <= age < self.ttl_seconds,
                    'source': 'DRIVING_REFERENCE_GRAPH'}

    @staticmethod
    def _decode(content):
        if isinstance(content, str):
            if len(content) > MAX_EVIDENCE_BYTES:raise ValueError('Oversized reference evidence')
            return json.loads(content)
        if isinstance(content, memoryview):
            content = content.tobytes()
        if bytes(content[:2]) == b'\x1f\x8b':
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                content = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(content) > MAX_EVIDENCE_BYTES:raise ValueError('Oversized reference evidence')
        return json.loads(content)

    @staticmethod
    def _valid(key, item, instant, ttl):
        try:
            # Delay the road-service import: DrivingRoad uses this graph too.
            from app.road_service import DrivingRoad, DRIVING_ENDPOINT
            if not DrivingRoad.valid_reference(item, allow_expired=True):
                return False
            if any(isinstance(item[name], bool) or not isinstance(item[name], int) for name in ('meters', 'seconds', 'received_at')):
                return False
            age = instant - item['received_at']
            if not 0 <= age < ttl:
                return False
            raw = item['request']
            if not isinstance(raw,dict):return False
            if raw.get('source') == 'DRIVING_PROVIDER_REFERENCE':
                canonical = DrivingRoad.reference_request(raw['origin'], raw['destination'])
                if canonical != raw:
                    return False
            elif raw.get('source') == 'DRIVING' and raw.get('endpoint') == DRIVING_ENDPOINT and raw.get('strategy') == 32:
                canonical = DrivingRoad.reference_request(raw['origin'], raw['destination'])
            else:
                return False
            return content_hash(canonical) == key
        except (KeyError, TypeError, ValueError, OverflowError):
            return False

    def get_recent(self, provider_key, now=None, ttl_seconds=None):
        """Return complete fresh bound source evidence, or None on any doubt."""
        instant = int(time.time()) if now is None else int(now)
        ttl = self.ttl_seconds if ttl_seconds is None else max(0, int(ttl_seconds))
        with self._lock:
            if self._closed or not isinstance(provider_key, str) or not HASH.fullmatch(provider_key):
                return None
            try:
                if provider_key in self._geometry:
                    cached = self._geometry[provider_key]
                    if self._valid(provider_key, cached, instant, ttl):
                        self._geometry.move_to_end(provider_key)
                        return deepcopy(cached)
                    del self._geometry[provider_key]
                connection = self._connect_reader()
                if connection is None:
                    return None
                row = connection.execute('SELECT * FROM metrics WHERE provider_key=?', (provider_key,)).fetchone()
                metric = self._metric(row)
                if metric is None:
                    return None
                item = self._decode(row['content'])
                if not self._valid(provider_key, item, instant, ttl):
                    return None
                if any(item[key] != metric[key] for key in ('meters', 'seconds', 'received_at', 'evidence_hash')):
                    return None
                self.metrics[provider_key] = metric
                self._geometry[provider_key] = item
                self._geometry.move_to_end(provider_key)
                while len(self._geometry) > self.geometry_cache_size:
                    self._geometry.popitem(last=False)
                return deepcopy(item)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, EOFError, gzip.BadGzipFile, zlib.error):
                self._discard_reader()
                return None

    def put_reference(self, provider_key, item):
        """Store actual fresh API evidence without extending its receive time.

        A short WAL write transaction permits multiple app processes to share
        the sidecar. An older response cannot replace a newer valid reference.
        Cache contention or an unavailable path simply skips the optional write.
        """
        instant = int(time.time())
        if self.path is None or not self._valid(provider_key, item, instant, self.ttl_seconds):
            return False
        with self._lock:
            if self._closed:
                return False
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.exists() and not self.path.is_file():
                    return False
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                except FileExistsError:
                    pass
                self.path.chmod(0o600)
                with sqlite3.connect(self.path, timeout=.1) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute('PRAGMA busy_timeout=100')
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    # Never add a cache table to an unrelated pre-existing DB.
                    if tables and 'metrics' not in tables:
                        return False
                    connection.execute('PRAGMA journal_mode=WAL')
                    connection.execute(SCHEMA)
                    connection.execute('BEGIN IMMEDIATE')
                    prior = connection.execute('SELECT * FROM metrics WHERE provider_key=?', (provider_key,)).fetchone()
                    prior_metric=self._metric(prior)
                    if prior_metric is not None and prior_metric['received_at'] >= item['received_at']:
                        try:
                            previous = self._decode(prior['content'])
                            metric = self._metric(prior)
                            if (metric is not None and self._valid(provider_key, previous, instant, self.ttl_seconds)
                                    and all(previous[key] == metric[key] for key in metric)):
                                self.metrics[provider_key] = metric
                                self._geometry[provider_key] = previous
                                self._geometry.move_to_end(provider_key)
                                while len(self._geometry) > self.geometry_cache_size:self._geometry.popitem(last=False)
                                connection.rollback()
                                return True
                        except (ValueError, TypeError, KeyError, IndexError, EOFError, gzip.BadGzipFile, zlib.error):
                            pass
                    request = item['request']
                    connection.execute('INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?)',
                                       (provider_key, request.get('from_id'), request.get('to_id'),
                                        item['meters'], item['seconds'], item['received_at'], item['evidence_hash'],
                                        gzip.compress(encode(item).encode(), mtime=0)))
                    connection.commit()
                self.metrics[provider_key] = {key: item[key] for key in ('meters', 'seconds', 'received_at', 'evidence_hash')}
                self._geometry[provider_key] = deepcopy(item)
                self._geometry.move_to_end(provider_key)
                while len(self._geometry) > self.geometry_cache_size:self._geometry.popitem(last=False)
                return True
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
                return False

    def close(self):
        with self._lock:
            self._closed = True
            self._discard_reader()
            self._geometry.clear()
            self.metrics.clear()
