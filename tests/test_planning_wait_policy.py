"""UI-facing budgets come from the same authority as the worker cutoff."""
import pytest

from test_plan_api import frozen, new_job
from test_stage4 import basis


def test_capabilities_expose_finite_default_wait_and_inclusive_scope(basis):
    client, *_ = basis
    cap = client.get('/api/planning/capabilities').json()
    assert cap['default_budget_seconds'] == 300
    assert cap['default_wall_budget_seconds'] == 900
    assert cap['max_calls'] is None
    policy = cap['wall_wait_policy']
    assert policy['extra_compute_multiplier'] == 2
    assert policy['extra_min_seconds'] == 120 and policy['extra_max_seconds'] == 1800
    assert policy['includes_queue'] and policy['includes_road_wait']
    assert policy['authority'] == 'SERVER_DEADLINE'
    assert 'PUBLICATION' in policy['scope']


@pytest.mark.parametrize('budget,explicit,expected', [(300, None, 900), (2000, None, 3800), (300, 90, 90)])
def test_queued_job_metadata_reports_server_default_or_explicit_cutoff(basis, app, budget, explicit, expected):
    client, *_ = basis
    app.state.planning_runner.close()  # Metadata test; never start an optimizer.
    options = {'budget_seconds': budget}
    if explicit is not None:
        options['wall_budget_seconds'] = explicit
    job = new_job(client, frozen(basis), **options)
    assert job['status'] == 'QUEUED'
    assert job['budget_seconds'] == budget and job['wall_budget_seconds'] == expected
    fetched = client.get('/api/planning/jobs/' + job['id']).json()
    assert fetched['wall_budget_seconds'] == expected
