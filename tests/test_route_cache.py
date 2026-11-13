"""Compact-cache bounds, scarce coverage and immutable-incumbent regressions."""
from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.route_cache import trim_search_cache


def host(task_count=16, mandatory=()):
    ids = [str(i) for i in range(task_count)]
    return SimpleNamespace(tasks={i: {'id': i} for i in ids},
                           mandatory=set(mandatory), common_cost={i: Decimal(0 if i in mandatory else 100) for i in ids},
                           indices={i: 1 << n for n, i in enumerate(ids)},
                           pool=OrderedDict(), memo=OrderedDict(), best_routes=[],
                           best={'total_fee': '10.00', 'full_evidence': ['IMMUTABLE']},
                           raw=OrderedDict([('evidence', {'polyline': [[1, 2], [3, 4]]})]))


def row(vehicle, ids, fee='10', meters=1000, **extra):
    return {'vehicle_id': vehicle, 'task_ids': list(ids), 'fee': f'{Decimal(fee):.2f}',
            'meters': meters, 'seconds': 60, 'pieces': len(ids), **extra}


def add(search, candidate):
    key = candidate['vehicle_id'], sum(search.indices[i] for i in candidate['task_ids'])
    search.pool[key] = candidate
    search.memo[candidate['vehicle_id'], tuple(candidate['task_ids'])] = candidate
    return key


def test_memo_fifo_retains_incumbent_and_recent_keys_with_headroom():
    search = host(task_count=30)
    rows = [row('V', [str(n)]) for n in range(20)]
    for candidate in rows:
        add(search, candidate)
    search.best_routes = [rows[0], rows[-1]]
    before, raw = deepcopy(search.best), deepcopy(search.raw)
    stats = trim_search_cache(search, memo_limit=10, pool_limit=100)
    assert isinstance(search.memo, OrderedDict)
    assert len(search.memo) == 8 and stats['memo_pruned'] == 12
    assert ('V', ('0',)) in search.memo and ('V', ('19',)) in search.memo
    assert ('V', ('18',)) in search.memo and ('V', ('1',)) not in search.memo
    assert search.best == before and search.raw == raw
    for n in range(20, 23):
        add(search, row('V', [str(n)]))
    trim_search_cache(search, memo_limit=10, pool_limit=100)
    assert len(search.memo) == 8
    assert ('V', ('0',)) in search.memo and ('V', ('22',)) in search.memo


def test_pool_keeps_rare_mandatory_and_optional_opportunities_within_cap():
    search = host(task_count=10, mandatory=('8',))
    for mask in range(1, 100):
        ids = [str(i) for i in range(7) if mask & (1 << i)]
        add(search, row('V0', ids, fee=10 + len(ids)))
    rare_hard = add(search, row('V1', ['8'], fee=5000))
    rare_optional = add(search, row('V2', ['9'], fee=5000))
    stats = trim_search_cache(search, memo_limit=1000, pool_limit=20)
    assert len(search.pool) <= 20 and stats['pool_pruned'] > 0
    assert rare_hard in search.pool and rare_optional in search.pool
    assert stats['pool_pruned_changes_proof_scope']
    assert stats['retained_task_coverage'] == 9


def test_both_low_fee_and_high_common_saving_task_representatives_survive():
    search = host(task_count=8)
    cheap = add(search, row('V0', ['0'], fee=1))
    strong = add(search, row('V1', [str(i) for i in range(8)], fee=200))
    for mask in range(1, 80):
        ids = [str(i) for i in range(7) if mask & (1 << i)]
        add(search, row('V2', ids, fee=500 + mask))
    trim_search_cache(search, memo_limit=1000, pool_limit=30)
    assert cheap in search.pool and strong in search.pool
    assert len(search.pool) <= 30


def test_physical_vehicle_diversity_is_not_evicted_by_one_cars_many_orders():
    search = host(task_count=8)
    for vehicle in ('V0', 'V1'):
        for mask in range(1, 100):
            ids = [str(i) for i in range(8) if mask & (1 << i)]
            add(search, row(vehicle, ids, fee=10 + len(ids)))
    add(search, row('V2', ['0', '1'], fee=500))
    trim_search_cache(search, memo_limit=1000, pool_limit=30)
    assert {value['vehicle_id'] for value in search.pool.values()} == {'V0', 'V1', 'V2'}
    assert len(search.pool) <= 30


def test_protected_key_keeps_better_alternative_order_without_mutating_best():
    search = host(task_count=8)
    incumbent = row('V0', ['0', '1'], fee=10)
    alternative = row('V0', ['1', '0'], fee=8, meters=800)
    search.best_routes = [incumbent]
    key = add(search, alternative)
    search.memo[('V0', ('0', '1'))] = None
    for n in range(2, 8):
        add(search, row('V1', [str(n)], fee=100 + n))
    saved = deepcopy(search.best_routes)
    trim_search_cache(search, memo_limit=2, pool_limit=2)
    assert key in search.pool and search.pool[key] is alternative
    assert search.memo['V0', ('0', '1')] == incumbent
    assert search.best_routes == saved


@pytest.mark.parametrize('limit', [0, 1, 2])
def test_incumbent_larger_than_limit_is_never_discarded(limit):
    search = host(task_count=6)
    search.best_routes = [row(f'V{i}', [str(i)], fee=10) for i in range(4)]
    for candidate in search.best_routes + [row('OTHER', ['5'], fee=1)]:
        add(search, candidate)
    before_best, before_routes = deepcopy(search.best), deepcopy(search.best_routes)
    stats = trim_search_cache(search, memo_limit=limit, pool_limit=limit)
    assert len(search.memo) == len(search.pool) == 4
    assert all((r['vehicle_id'], tuple(r['task_ids'])) in search.memo for r in search.best_routes)
    assert all((r['vehicle_id'], search.indices[r['task_ids'][0]]) in search.pool for r in search.best_routes)
    assert search.best == before_best and search.best_routes == before_routes
    assert stats['memo_over_limit_for_incumbent'] and stats['pool_over_limit_for_incumbent']


def test_zero_limit_without_incumbent_releases_all_compact_entries():
    search = host()
    for n in range(8):
        add(search, row('V', [str(n)]))
    stats = trim_search_cache(search, memo_limit=0, pool_limit=0)
    assert not search.memo and not search.pool
    assert stats['memo_pruned'] == stats['pool_pruned'] == 8
    assert search.best['full_evidence'] == ['IMMUTABLE']


def test_pruned_counts_are_exact_when_missing_incumbent_cache_rows_are_restored():
    search = host()
    search.best_routes = [row(f'V{i}', [str(i)]) for i in range(4)]
    add(search, row('OTHER', ['8']))
    stats = trim_search_cache(search, memo_limit=0, pool_limit=0)
    assert len(search.memo) == len(search.pool) == 4
    assert stats['memo_pruned'] == stats['pool_pruned'] == 1
    assert stats['pool_pruned_changes_proof_scope']


def test_repeated_pruning_cycles_stay_bounded_and_do_not_touch_full_evidence():
    search = host(task_count=12)
    incumbent = row('V0', ['0'], fee=1)
    search.best_routes = [incumbent]
    add(search, incumbent)
    original_best, original_raw = deepcopy(search.best), deepcopy(search.raw)
    for mask in range(2, 800):
        ids = [str(i) for i in range(12) if mask & (1 << i)]
        add(search, row(f'V{mask % 4}', ids, fee=len(ids) * 10))
        trim_search_cache(search, memo_limit=100, pool_limit=50)
        assert len(search.memo) <= 100 and len(search.pool) <= 50
        assert ('V0', ('0',)) in search.memo and ('V0', 1) in search.pool
    assert search.best == original_best and search.raw == original_raw


def test_nonoverflowing_cache_is_not_copied_or_reordered():
    search = host()
    add(search, row('V', ['0']))
    memo, pool = search.memo, search.pool
    stats = trim_search_cache(search, memo_limit=10, pool_limit=10)
    assert search.memo is memo and search.pool is pool
    assert stats['memo_pruned'] == stats['pool_pruned'] == 0


@pytest.mark.parametrize('bad', [None, float('inf'), 'not-a-limit'])
def test_invalid_limits_fail_before_cache_mutation(bad):
    search = host()
    add(search, row('V', ['0']))
    original = deepcopy(search.pool)
    with pytest.raises(ValueError):
        trim_search_cache(search, memo_limit=bad)
    assert search.pool == original
