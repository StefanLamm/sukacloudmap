"""Explicit test-only daily entrypoint switches; no routing solver or HTTP."""
from copy import deepcopy
import json

from pydantic import ValidationError
import pytest

from app.config import Settings
from app.routing_engine import assemble, normalize
from scripts.serve_delivery_qa import parse_experiments_json, settings_from_file, write_config
from test_plan_api import frozen, new_job, waited
from test_stage4 import basis


FLAGS = {'joint_reoptimization': True, 'scalable_joint_search': True,
         'stage_budget_reservation': True}


def complete_common_result(body):
    data = normalize(body)
    assert all(t['common_unit_price'] is not None for t in data['tasks'].values())
    result = assemble(data, [], set(data['tasks']), set())
    result['algorithm'] = {'mode': 'DAILY_DEEP_PORTFOLIO', 'stop_reason': 'SEARCH_COMPLETED',
                           'final_validation': {'mode': 'COMMON_ONLY_VALIDATED', 'fresh': False,
                                                'selected_segments': 0}}
    return result


@pytest.mark.parametrize('environment', ['development', 'production'])
@pytest.mark.parametrize('flags', [{}, FLAGS])
def test_non_test_environment_rejects_even_empty_override(environment, flags):
    with pytest.raises(ValidationError, match='仅允许隔离 test'):
        Settings(_env_file=None, environment=environment, planning_deep_experiments=flags,
                 public_origin='https://fixture.example.invalid', secure_cookie=True)


@pytest.mark.parametrize('flags', [{'unknown_feature': True}, {'joint_reoptimization': 1},
                                 {'joint_reoptimization': 'true'}, {'candidate_retention': 'unknown'}])
def test_test_environment_still_validates_names_types_and_policies(flags):
    with pytest.raises(ValidationError, match='名称、类型或策略无效'):
        Settings(_env_file=None, environment='test', planning_deep_experiments=flags)


def test_none_and_explicit_test_flags_have_distinct_configuration_contracts():
    assert Settings(_env_file=None).planning_deep_experiments is None
    settings = Settings(_env_file=None, environment='test', planning_deep_experiments=FLAGS)
    assert settings.planning_deep_experiments == FLAGS


def test_default_daily_worker_preserves_optimizer_stub_without_experiments_keyword(basis, monkeypatch):
    import app.plan_service as service
    seen = []
    def workflow(body, road, budget, seed, cancelled, *, initial_result=None, progress=None,
                 wall_budget_seconds=None):
        seen.append((budget, wall_budget_seconds))
        return complete_common_result(body)
    monkeypatch.setattr(service, 'optimize_deep', workflow)
    client, *_ = basis
    done = waited(client, new_job(client, frozen(basis), budget_seconds=3))
    assert done['status'] == 'COMPLETED', done
    assert seen and seen[0][0] == 3 and seen[0][1] > 0
    assert 'test_experiments_override' not in done['summary']['algorithm']


def test_real_daily_job_passes_only_explicit_override_and_records_source(basis, app, monkeypatch):
    import app.plan_service as service
    seen = []
    app.state.settings.planning_deep_experiments = deepcopy(FLAGS)
    def workflow(body, road, budget, seed, cancelled, *, initial_result=None, progress=None,
                 wall_budget_seconds=None, experiments):
        seen.append(deepcopy(experiments))
        return complete_common_result(body)
    monkeypatch.setattr(service, 'optimize_deep', workflow)
    client, *_ = basis
    done = waited(client, new_job(client, frozen(basis), budget_seconds=3))
    assert done['status'] == 'COMPLETED', done
    assert seen == [FLAGS] and app.state.settings.planning_deep_experiments == FLAGS
    algorithm = done['summary']['algorithm']
    assert algorithm['test_experiments_override'] == FLAGS
    assert algorithm['source_provenance']['app_source_hash']
    assert algorithm['source_provenance']['git_discovery_used'] is False


def test_worker_rejects_non_test_override_even_if_settings_validation_was_bypassed(basis, app, monkeypatch):
    import app.plan_service as service
    app.state.settings.environment = 'development'
    app.state.settings.planning_deep_experiments = deepcopy(FLAGS)
    def forbidden(*args, **kwargs):
        raise AssertionError('Non-test override reached optimizer')
    monkeypatch.setattr(service, 'optimize_deep', forbidden)
    client, *_ = basis
    done = waited(client, new_job(client, frozen(basis), budget_seconds=3))
    assert done['status'] == 'FAILED' and done['error_code'] == 'TEST_EXPERIMENTS_FORBIDDEN'
    assert done['plan_id'] is None


def test_qa_json_accepts_literal_or_file_and_writes_only_new_private_config(tmp_path):
    literal = json.dumps(FLAGS)
    path = tmp_path / 'flags.json'
    path.write_text(literal)
    assert parse_experiments_json(literal) == parse_experiments_json(str(path)) == FLAGS
    for malformed in ('[]', '{"unknown_feature":true}', '{"scalable_joint_search":1}'):
        with pytest.raises(ValueError):
            parse_experiments_json(malformed)
    source = Settings(_env_file=None, environment='test')
    target = tmp_path / 'new-runtime'
    (target / 'data').mkdir(parents=True)
    write_config(target, source, FLAGS)
    actual = settings_from_file(target / '.env')
    assert actual.environment == 'test' and actual.planning_deep_experiments == FLAGS
    assert actual.cookie_namespace == 'test_delivery_qa' and actual.public_origin == 'http://127.0.0.1:8003'
    assert source.planning_deep_experiments is None
    assert (target / '.env').stat().st_mode & 0o777 == 0o600
