"""Durable, serial address lookup outside upload and database write locks."""
import logging
import re
import threading
import unicodedata

from sqlalchemy import func, select, update

from app.api import ApiError
from app.database import start_write
from app.models import LocationTask, new_id, now

log = logging.getLogger('suka.locations')


def address_query(entity):
    address = ' '.join(entity.address.split())
    city = getattr(entity, 'city', '').strip()
    return (city + ' ' if city and city not in address else '') + address


def enqueue_location(db, kind, entity, actor_id):
    if kind not in {'warehouses', 'stores'}:
        return False
    task = db.get(LocationTask, (kind, entity.id))
    if entity.longitude is not None and entity.latitude is not None:
        if task:
            db.delete(task)
        return False
    if task is None:
        task = LocationTask(kind=kind, entity_id=entity.id)
        db.add(task)
    task.source_revision = entity.revision
    task.query = address_query(entity)
    task.token = new_id()
    task.status, task.reason, task.lease_until = 'QUEUED', '', 0
    task.created_by, task.created_at = actor_id, now()
    return True


def location_metadata(db, data):
    tasks = {(t.kind, t.entity_id): t for t in db.scalars(select(LocationTask))}
    for kind in ['warehouses', 'stores']:
        for row in data[kind]:
            task = tasks.get((kind, row['id']))
            if row['longitude'] is not None and row['latitude'] is not None:
                row.update(location_status='LINKED', location_note='已关联位置')
            elif task and task.status in {'QUEUED', 'RUNNING'}:
                row.update(location_status='PENDING', location_note='正在根据地址自动关联')
            else:
                row.update(location_status='MANUAL', location_note=task.reason if task else '请在地图上选择位置')


def compact(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value).lower()).replace('号', '')


def exact_candidates(query, items):
    """Accept unique precise matches, never city centroids or a blind first hit."""
    normalized = compact(query)
    candidates = {}
    precise = [item for item in items if item.get('provider') == 'AMAP' and item.get('automatic_eligible')]
    if precise:
        # Prefer a precise entrance/unit over the provider's older house-number
        # centroid. Equally precise alternatives remain ambiguous.
        rank = max(item.get('automatic_rank', 0) for item in precise)
        items = [item for item in precise if item.get('automatic_rank', 0) == rank]
    for item in items:
        if item.get('automatic_scope_ok') is False:
            continue
        if item.get('provider') == 'AMAP' and (item.get('automatic_eligible') or item.get('match_level') != 'POI'):
            if item.get('automatic_eligible'):
                point = (round(float(item['longitude']), 6), round(float(item['latitude']), 6))
                candidates.setdefault(point, item)
            continue
        name, address = compact(item.get('name', '')), compact(item.get('address', ''))
        parts = item.get('address_parts', {})
        city, street, number = (compact(str(parts.get(k, ''))) for k in ['city', 'street', 'housenumber'])
        district = compact(str(parts.get('district', '')))
        district_ok = not re.search(r'[^\W\d_]{2,}(区|县)', query) or bool(district and district in normalized)
        precise_name = len(name) >= 4 and not re.search(r'(省|市|县|区|镇|街道|路|街|道)$', name)
        name_match = precise_name and normalized == name
        full_address_match = (len(normalized) >= 6 and normalized in address and
                              bool(re.search(r'(路|街|道|巷|弄).*\d', query)))
        structured_match = (district_ok and city and street and number and city in normalized and street in normalized and
                            bool(re.search(r'(?<!\d)' + re.escape(number) + r'(?!\d)', normalized)))
        if name_match or full_address_match or structured_match:
            # Multiple equivalent OSM objects at the same point are one match.
            point = (round(float(item['longitude']), 6), round(float(item['latitude']), 6))
            candidates.setdefault(point, item)
    return list(candidates.values())


class LocationRunner:
    def __init__(self, engine, sessions, search, *, enabled=True, clock=now):
        self.engine, self.sessions, self.search, self.enabled, self.clock = engine, sessions, search, enabled, clock
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.enabled:
            self.thread = threading.Thread(target=self.loop, name='suka-locations', daemon=True)
            self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                log.error('location_worker_failed exception_type=%s', type(exc).__name__)
            self.stop_event.wait(1)

    def claim(self):
        at = self.clock()
        with self.sessions() as db:
            possible = db.scalar(select(LocationTask.entity_id).where(
                (LocationTask.status == 'QUEUED') | ((LocationTask.status == 'RUNNING') & (LocationTask.lease_until <= at))).limit(1))
            if possible is None:
                return None
        with self.sessions() as db:
            start_write(db, self.engine)
            # One global lookup at a time, including when several app workers run.
            running = db.scalar(select(LocationTask.entity_id).where(LocationTask.status == 'RUNNING', LocationTask.lease_until > at).limit(1))
            latest = db.scalar(select(func.max(LocationTask.last_attempt_at))) or 0
            if running is not None or at - latest < 2:
                db.commit()
                return None
            db.execute(update(LocationTask).where(LocationTask.status == 'RUNNING', LocationTask.lease_until <= at).values(status='QUEUED'))
            task = db.scalar(select(LocationTask).where(LocationTask.status == 'QUEUED').order_by(LocationTask.created_at, LocationTask.entity_id).limit(1))
            if task is None:
                db.commit()
                return None
            task.status, task.token = 'RUNNING', new_id()
            task.lease_until, task.last_attempt_at = at + 60, at
            key = (task.kind, task.entity_id, task.token, task.query)
            db.commit()
            return key

    def run_once(self):
        claimed = self.claim()
        if not claimed:
            return False
        kind, entity_id, token, query = claimed
        match, reason, unavailable, retry = None, '', False, False
        if not 2 <= len(query) <= 200:
            reason = '地址不完整或过长，需手动关联'
        else:
            try:
                response = self.search.search(query)
                matches = exact_candidates(query, response['items'])
                if len(matches) == 1:
                    match = matches[0]
                else:
                    reason = '匹配到多个位置，需手动关联' if matches else '未找到精确地址，需手动关联'
                    if not matches and response.get('notice'):
                        reason = (response['notice'] + ' 需手动关联')[:200]
            except ApiError as exc:
                # Provider QPS exhaustion has already had three paced attempts.
                # Do not spin forever or mark every other queued record failed.
                retry = exc.status == 429 and exc.code != 'AMAP_QPS_LIMIT'
                unavailable = exc.status in {502, 503}
                reason = (exc.message + ' 需手动关联')[:200] if unavailable or exc.status == 429 else '地址匹配未完成，需手动关联'
        if self.stop_event.is_set():
            return True  # Persisted lease permits another worker/restart to resume.
        from app.master_service import MODELS, save_entity
        with self.sessions() as db:
            start_write(db, self.engine)
            task = db.get(LocationTask, (kind, entity_id))
            if task is None or task.status != 'RUNNING' or task.token != token or task.lease_until <= self.clock():
                db.commit()
                return True
            entity = db.get(MODELS[kind], entity_id)
            if entity is None:
                db.delete(task)
            elif entity.revision != task.source_revision or address_query(entity) != query or entity.longitude is not None:
                task.status, task.reason = 'SKIPPED', '资料已修改，请核对后重新关联'
            elif retry:
                task.status, task.reason, task.lease_until = 'QUEUED', '', 0
            elif match:
                save_entity(db, kind, {k: match[k] for k in ['longitude', 'latitude', 'crs']} |
                            {'location_source': '地址自动关联 · ' + ('高德地图' if match.get('provider') == 'AMAP' else 'Photon / OpenStreetMap')}, task.created_by, entity, task.source_revision, automatic_location=True)
                task.status, task.reason = 'LINKED', '已根据导入地址自动关联'
            else:
                task.status, task.reason = 'MANUAL', reason
            if unavailable:
                # Fail closed on provider outage, without sending thousands of
                # unsuccessful business address requests or freezing the page.
                db.execute(update(LocationTask).where(LocationTask.status == 'QUEUED').values(status='MANUAL', reason=reason))
            db.commit()
        return True
