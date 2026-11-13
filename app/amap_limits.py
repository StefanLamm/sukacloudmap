"""Per-service QPS pacing shared by local processes, without usage quotas.

All app instances on this host must use the same file, even with different
business databases or Keys belonging to one AMap account. Only service names
and timestamps are stored; no addresses, credentials or monthly counters.
"""
import logging
from contextlib import closing
from pathlib import Path
import sqlite3
from time import monotonic, sleep, time as wall_time

log = logging.getLogger('suka.amap')
QPS_CODES = frozenset({'10014', '10015', '10019', '10020', '10021', '10029'})


class RateLimitWaitError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class AMapRateLimiter:
    def __init__(self, path, *, web_qps=3, js_qps=10, clock=None, timer=None, sleeper=None, max_wait=10):
        self.path = Path(path)
        self.web_qps, self.js_qps = web_qps, js_qps
        self.clock, self.timer, self.sleep = clock or wall_time, timer or monotonic, sleeper or sleep
        self.max_wait = max_wait

    @classmethod
    def from_settings(cls, settings):
        return cls(settings.amap_rate_limit_path, web_qps=settings.amap_web_qps, js_qps=settings.amap_js_qps)

    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create with restrictive permissions before sqlite opens the file.
        self.path.touch(mode=0o600, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=0.2, isolation_level=None)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS service_pacing (service TEXT PRIMARY KEY, next_at REAL NOT NULL)')
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def validate(service):
        if service not in {'geocode', 'poi_search', 'driving', 'truck', 'js_map', 'district'}:
            raise ValueError('Unknown AMap service')

    def acquire(self, service, *, cancelled=lambda: False):
        self.validate(service)
        # A small margin avoids a fourth request at a rolling-second boundary.
        interval = 1 / (self.js_qps if service == 'js_map' else self.web_qps) + 0.02
        deadline = self.timer() + self.max_wait
        while True:
            if cancelled():
                raise RateLimitWaitError('CANCELLED')
            remaining = deadline - self.timer()
            if remaining <= 0:
                raise RateLimitWaitError('WAIT_TIMEOUT')
            try:
                with closing(self.connection()) as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = self.clock()
                    row = db.execute('SELECT next_at FROM service_pacing WHERE service=?', (service,)).fetchone()
                    delay = max(0, row[0] - current) if row else 0
                    if not delay:
                        db.execute('INSERT INTO service_pacing VALUES (?,?) ON CONFLICT(service) DO UPDATE SET next_at=excluded.next_at',
                                   (service, current + interval))
                    db.commit()
                if not delay:
                    return
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                    raise RateLimitWaitError('STORAGE_UNAVAILABLE') from None
                delay = 0.1
            except (OSError, sqlite3.DatabaseError):
                raise RateLimitWaitError('STORAGE_UNAVAILABLE') from None
            self.sleep(min(delay, 0.1, remaining))

    def defer(self, service, attempt):
        """A short provider-directed pause also applies to other processes."""
        self.validate(service)
        try:
            with closing(self.connection()) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO service_pacing VALUES (?,?) ON CONFLICT(service) DO UPDATE SET next_at=max(next_at,excluded.next_at)',
                           (service, self.clock() + min(2 ** attempt, 4)))
                db.commit()
        except (OSError, sqlite3.DatabaseError):
            raise RateLimitWaitError('STORAGE_UNAVAILABLE') from None


def log_throttle(service, code, attempt):
    # Log only known codes, never provider text (which may echo query or Key).
    log.warning('amap_throttled service=%s code=%s attempt=%s', service, code if code in QPS_CODES else 'HTTP_429', attempt + 1)
