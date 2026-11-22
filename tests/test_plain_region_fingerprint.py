"""Only provable display-region enrichment is harmless to a plain-price snapshot."""
from types import SimpleNamespace
import time

import pytest
from sqlalchemy import func, select

from app import plan_service, regions
from app.models import AuditEvent, InputSnapshot, PlanVersion, Store
from app.planning_inputs import configuration
from test_daily_deep_jobs import install_workflow
from test_demand_groups import create_group, manual, state
from test_plan_api import new_job, plan, waited
from test_region_billing_v2 import rule
from test_stage3 import quote
from test_stage4 import DAY, basis, detail


def tree(code='340100'):
    return {'items': [{'name': '甲省', 'code': '340000', 'children': [
        {'name': '甲市', 'code': code, 'children': []},
    ]}]}


def snapshot_for(client, store):
    group = create_group(client, '隔离输入变更核验')
    snap = manual(client, group, [{'store_id': store['id'], 'pieces': 7}])['snapshot_id']
    return group, snap


@pytest.mark.parametrize('terminal', ['COMPLETED', 'CANCELLED', 'WALL_BUDGET'])
def test_cache_appearance_during_job_respects_publication_and_cutoffs(basis, app, monkeypatch, terminal):
    client, _, _, store, *_ = basis
    cache = None
    monkeypatch.setattr(regions, 'cached_tree', lambda: cache)
    group, snapshot = snapshot_for(client, store)
    original = detail(client, snapshot)
    assert original['input']['stores'][0]['region_code'] == ''
    entered_at_publication = []
    monotonic = time.monotonic
    clock = SimpleNamespace(offset=0.)
    if terminal == 'WALL_BUDGET':
        original_current = plan_service.snapshot_input_is_current

        def expire_after_check(*args, **kwargs):
            current = original_current(*args, **kwargs)
            entered_at_publication.append(current)
            clock.offset = 10000.
            return current

        monkeypatch.setattr(plan_service, 'snapshot_input_is_current', expire_after_check)
        monkeypatch.setattr(plan_service, 'time', SimpleNamespace(monotonic=lambda: monotonic() + clock.offset))
    # The workflow has entered once the release-coordinated stub reports it.
    entry, release, _ = install_workflow(monkeypatch, block=True)
    job = new_job(client, snapshot, group_id=group['id'], wall_budget_seconds=20)
    try:
        assert entry.wait(2)
        cache = tree()
        with app.state.sessions() as db:
            assert configuration(db, DAY, group['id'])['hash'] != original['input']['configuration_hash']
        assert not detail(client, snapshot)['input_changed']
        assert detail(client, snapshot)['input'] == original['input']
        if terminal == 'CANCELLED':
            assert client.post('/api/planning/jobs/'+job['id']+'/cancel').status_code == 200
    finally:
        release.set()
    done = waited(client, job)
    assert done['summary']['total_fee'] == '15.75'
    if terminal == 'WALL_BUDGET':
        assert entered_at_publication == [True]
        assert done['status'] == 'FAILED' and done['error_code'] == 'WALL_BUDGET' and not done['plan_id']
        with app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(PlanVersion)) == 0
            assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'PLAN_DRAFT')) is None
        return

    assert done['status'] == terminal and done['plan_id']
    saved = plan(client, done['plan_id'])
    assert not saved['input_changed'] and saved['input'] == original['input']
    if terminal == 'COMPLETED':
        current = state(client, group)
        refreeze = client.post('/api/planning/days/'+DAY+'/snapshots', params={'group_id': group['id']}, json={
            'expected_revision': current['revision'], 'expected_fingerprint': current['input_fingerprint']})
        assert refreeze.status_code == 200 and refreeze.json()['id'] == snapshot
        confirmed = client.post('/api/planning/plans/'+saved['id']+'/confirm',
                                json={'expected_head': saved['version']})
        assert confirmed.status_code == 201, confirmed.text
        followup = waited(client, new_job(client, snapshot, expected=confirmed.json()['version'],
                                          group_id=group['id']))
        assert followup['status'] == 'COMPLETED' and followup['plan_id']


@pytest.mark.parametrize('change', ['tariff', 'quote', 'receiving', 'region_edit', 'legacy_inferred', 'integrity'])
def test_mismatch_stays_strict_without_proof_of_plain_cache_enrichment(basis, app, monkeypatch, change):
    client, _, original_quote, store, *_ = basis
    cache = tree() if change == 'legacy_inferred' else None
    monkeypatch.setattr(regions, 'cached_tree', lambda: cache)
    if change == 'tariff':
        regional = quote(client, code='REG-GUARD', mode='KM_REGION', region_surcharge=rule('ONCE'))
        updated = client.patch('/api/stores/'+store['id'], json={'expected_revision': store['revision'],
                                                                 'dedicated_quote_id': regional['id']})
        assert updated.status_code == 200, updated.text
    group, snapshot = snapshot_for(client, store)
    original = detail(client, snapshot)['input']
    entry, release, _ = install_workflow(monkeypatch, block=True)
    job = new_job(client, snapshot, group_id=group['id'])
    try:
        assert entry.wait(2)
        cache = tree('340200')
        if change == 'quote':
            updated = client.patch('/api/quotes/'+original_quote['id'],
                json={'expected_revision': original_quote['revision'], 'per_km': '3.00'})
            assert updated.status_code == 200, updated.text
        elif change == 'receiving':
            updated = client.put('/api/planning/days/'+DAY+'/stores/'+store['id'], params={'group_id': group['id']},
                json={'expected_revision': 0, 'latest_arrival_at': DAY+'T12:00:00+08:00'})
            assert updated.status_code == 200, updated.text
        elif change in {'region_edit', 'integrity'}:
            with app.state.sessions() as db:
                if change == 'region_edit':
                    # Even a legacy DB writer omitting the revision cannot
                    # pass by changing a field that the cache could also infer.
                    db.get(Store, store['id']).province = '甲省'
                else:
                    snap = db.get(InputSnapshot, snapshot)
                    snap.content = snap.content.replace('"甲市"', '"篡改标签"')
                db.commit()
        if change != 'integrity':
            assert detail(client, snapshot)['input_changed']
            assert state(client, group)['latest_snapshot']['input_changed']
    finally:
        release.set()
    done = waited(client, job)
    assert done['status'] == 'FAILED' and done['error_code'] == 'INPUT_CHANGED'
    assert done['plan_id'] is None
    if change != 'integrity':
        assert detail(client, snapshot)['input'] == original
        admission = client.post('/api/planning/jobs', json={'snapshot_id': snapshot,
            'group_id': group['id'], 'expected_head': 0})
        assert admission.status_code == 409 and admission.json()['code'] == 'INPUT_CHANGED'
