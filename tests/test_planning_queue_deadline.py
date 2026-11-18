"""Acceptance-to-finish deadlines on temporary jobs; optimizer is a stub."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.database import start_write
from app.models import PlanningJob, PlanVersion, now
from app.routing_engine import assemble, normalize
from test_plan_api import frozen, new_job
from test_stage4 import basis


def prepare_claimed(basis, app, *, created_at=1000, wall_budget=20, started_at=None):
    client, *_ = basis
    runner = app.state.planning_runner
    runner.close()
    runner.stop_event.clear()  # Direct run below, without restarting the loop.
    app.state.settings.planning_route_mode = 'TRUCK'
    app.state.settings.planning_road_source = 'LIVE'
    options = {'budget_seconds': 3}
    if wall_budget is not None:
        options['wall_budget_seconds'] = wall_budget
    job = new_job(client, frozen(basis), **options)
    with app.state.sessions() as db:
        start_write(db, app.state.engine)
        row = db.get(PlanningJob, job['id'])
        row.status = 'RUNNING'
        row.created_at = created_at
        row.started_at = created_at + 7 if started_at is None else started_at
        row.lease_until = now() + 60
        db.commit()
    return runner, job['id']


def stored_job(app, job_id):
    with app.state.sessions() as db:
        row = db.get(PlanningJob, job_id)
        return row.status, row.error_code, json.loads(row.result), db.scalar(select(func.count()).select_from(PlanVersion))


def common_result(body):
    data = normalize(body)
    result = assemble(data, [], set(data['tasks']), set())
    result['algorithm'] = {'mode': 'DAILY_DEEP_PORTFOLIO', 'stop_reason': 'SEARCH_COMPLETED',
                           'final_validation': {'mode': 'COMMON_ONLY_VALIDATED', 'fresh': False,
                                                'selected_segments': 0}}
    return result


def inert_adapter(seen):
    class Road:
        def __init__(self, settings, *args, **options):
            seen.append(options)
        def close(self):
            pass
        def stats(self):
            return {'calls': 0}
    return Road


@pytest.mark.parametrize('wall_budget', [None, 20])
def test_expired_queue_stops_before_adapter_or_optimizer(basis, app, monkeypatch, wall_budget):
    import app.plan_service as service
    runner, job_id = prepare_claimed(basis, app, wall_budget=wall_budget)
    allowed = 123 if wall_budget is None else wall_budget
    monkeypatch.setattr(service, 'epoch_now', lambda: 1000 + allowed + 10.25)
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: 500.))
    def forbidden(*args, **kwargs):
        raise AssertionError('Expired queue started road or optimization work')
    monkeypatch.setattr(service, 'TruckRoad', forbidden)
    monkeypatch.setattr(service, 'SyntheticRoad', forbidden)
    monkeypatch.setattr(service, 'optimize_deep', forbidden)
    runner.run(job_id)
    status, error, result, plans = stored_job(app, job_id)
    assert status == 'FAILED' and error == 'WALL_BUDGET' and plans == 0
    timing = result['algorithm']
    assert timing['queue_wait_seconds'] == timing['total_wait_elapsed_seconds'] == allowed + 10.25
    assert timing['deadline_overrun_seconds'] == 10.25
    assert timing['requested_compute_budget_seconds'] == 3
    assert 'total_fee' not in result


@pytest.mark.parametrize('wall_budget', [None, 20])
def test_queue_reduces_wall_but_never_compute_allowance(basis, app, monkeypatch, wall_budget):
    import app.plan_service as service
    runner, job_id = prepare_claimed(basis, app, wall_budget=wall_budget)
    monkeypatch.setattr(service, 'epoch_now', lambda: 1007.25)
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: 500.))
    adapters, optimizers = [], []
    monkeypatch.setattr(service, 'TruckRoad', inert_adapter(adapters))
    def optimize(body, road, budget, seed, cancelled, *, initial_result=None, progress=None,
                 wall_budget_seconds=None):
        optimizers.append((budget, wall_budget_seconds))
        return common_result(body)
    monkeypatch.setattr(service, 'optimize_deep', optimize)
    runner.run(job_id)
    status, error, result, plans = stored_job(app, job_id)
    allowed = 123 if wall_budget is None else wall_budget
    assert status == 'COMPLETED' and error is None and plans == 1
    assert optimizers == [(3, allowed - 7.25)]
    assert adapters[0]['wall_deadline'] == 500 + allowed - 7.25
    timing = result['algorithm']
    assert timing['queue_wait_seconds'] == timing['total_wait_elapsed_seconds'] == 7.25
    assert timing['job_elapsed_seconds'] == timing['deadline_overrun_seconds'] == 0
    assert timing['job_wall_deadline_epoch'] == 1000 + allowed


def test_writer_wait_is_counted_and_verified_plan_stays_in_job_only(basis, app, monkeypatch):
    import app.plan_service as service
    runner, job_id = prepare_claimed(basis, app, started_at=1001)
    clock = SimpleNamespace(value=500.)
    monkeypatch.setattr(service, 'epoch_now', lambda: 1001.25)
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(service, 'TruckRoad', inert_adapter([]))
    monkeypatch.setattr(service, 'optimize_deep', lambda body, *args, **kwargs: common_result(body))
    original = service.start_write
    writes = []
    def delayed_final_writer(db, engine):
        original(db, engine)
        writes.append(1)
        if len(writes) == 2:  # SAVING progress, then final publication lock.
            clock.value = 530.
    monkeypatch.setattr(service, 'start_write', delayed_final_writer)
    runner.run(job_id)
    status, error, result, plans = stored_job(app, job_id)
    assert status == 'FAILED' and error == 'WALL_BUDGET' and plans == 0
    assert result['fees_complete'] and not result['unassigned']
    timing = result['algorithm']
    assert timing['job_elapsed_seconds'] == 30
    assert timing['total_wait_elapsed_seconds'] == 31.25
    assert timing['deadline_overrun_seconds'] == 11.25
    assert timing['publication'] == 'NOT_PUBLISHED_WALL_DEADLINE'
