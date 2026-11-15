"""Compare retention policies on one frozen candidate stream and road graph.

This command reads immutable experiment files, never a business database or
provider API. Every policy gets the same independent selection allowance and
the same protected complete plans. Cold-start performance requires a separate
equal-total-budget experiment and is deliberately not inferred here.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.planning_inputs import content_hash
from app.route_columns import select_portfolio
from app.route_retention import replay_frozen_stream
from app.route_verification import complete
from app.routing_engine import assemble, normalize
from scripts.benchmark_algorithms import FrozenEvidenceRoad, independent_audit, private_json


def compare(stream_path, body_path, graph_path, *, output, protected_path=None,
            incumbent_result=None, limit=10000, batch_size=512, seconds=10, explicit_boundaries=False):
    raw = Path(stream_path).read_bytes()
    decoded = gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw
    if '.jsonl' in Path(stream_path).suffixes:
        events = [json.loads(line) for line in decoded.decode().splitlines() if line.strip()]
    else:
        loaded = json.loads(decoded)
        events = loaded['events'] if isinstance(loaded, dict) else loaded
    checkpoint = next((i for i, event in enumerate(events) if event.get('kind') == 'checkpoint'
                       and event.get('stage') == 'PRE_COLUMN_SELECTION'), None)
    if checkpoint is not None:
        events = events[:checkpoint]
    def read_json(path):
        value = Path(path).read_bytes()
        return json.loads(gzip.decompress(value) if value[:2] == b'\x1f\x8b' else value)
    body = read_json(body_path)
    data = normalize(body)
    common = {task_id for task_id, task in data['tasks'].items() if task['common_unit_price'] is not None}
    protected = [assemble(data, [], common, set(data['tasks']) - common)]
    if protected_path is not None:
        loaded = read_json(protected_path)
        protected.extend(loaded if isinstance(loaded, list) else [loaded])
    if incumbent_result is not None:
        loaded = read_json(incumbent_result)
        # Benchmark files may wrap the actual full result.
        protected.append(loaded.get('result', loaded) if isinstance(loaded, dict) else loaded)
    if any(not complete(data, row) for row in protected[1:]):
        raise ValueError('Supplied protected plans must pass the independent complete-plan check')
    replay = replay_frozen_stream(events, data=data, limit=limit, batch_size=batch_size,
                                  explicit_boundaries=explicit_boundaries)
    report = {'scope': 'FROZEN_STREAM_RETENTION_ONLY', 'cold_start_performance_claimed': False,
              'hash_environment': {'PYTHONHASHSEED': os.environ.get('PYTHONHASHSEED'),
                                   'hash_randomization': sys.flags.hash_randomization},
              'stream_scope': 'PRE_COLUMN_SELECTION' if checkpoint is not None else 'FULL_TRACE_POSTHOC',
              'input_hash': content_hash(body), 'stream_sha256': hashlib.sha256(raw).hexdigest(),
              'selection_budget_seconds_per_policy': seconds, 'limit': limit,
              'stream_observations': replay['stream_observations'],
              'stream_unique_identities': replay['stream_unique_identities'],
              'stream_duplicate_observations': replay['stream_duplicate_observations'],
              'complete_recipe_count': replay['complete_recipe_count'],
              'trace_allocations_independently_validated': False,
              'protected_plan_hash': content_hash(protected), 'policies': {}}
    for policy, row in replay['policies'].items():
        road = FrozenEvidenceRoad(graph_path)
        started = time.perf_counter()
        try:
            result = select_portfolio(body, road, protected, seconds=seconds,
                                      candidate_recipes=row['candidates'],
                                      complete_recipes=replay['complete_recipes'])
            audit = independent_audit(data, result, road)
            report['graph_hash'] = road.graph_hash
            details = {key: value for key, value in row.items() if key != 'candidates'}
            report['policies'][policy] = {**details, 'candidate_hash': content_hash(row['candidates']),
                                          'fee': result['total_fee'], 'audit': audit,
                                          'selection': result.get('algorithm', {}),
                                          'selection_elapsed_seconds': round(time.perf_counter() - started, 6),
                                          'road_stats': road.stats()}
            private_json(Path(output).with_name(Path(output).stem + '-' + policy + '-result.json'), result)
        finally:
            road.close()
    private_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stream', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--graph', required=True)
    parser.add_argument('--protected-plans')
    parser.add_argument('--incumbent-result')
    parser.add_argument('--limit', type=int, default=10000)
    parser.add_argument('--batch-size', type=int, default=512)
    parser.add_argument('--selection-seconds', type=float, default=10)
    parser.add_argument('--explicit-boundaries', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = compare(args.stream, args.input, args.graph, output=args.output,
                     protected_path=args.protected_plans, incumbent_result=args.incumbent_result,
                     limit=args.limit, batch_size=args.batch_size,
                     seconds=args.selection_seconds, explicit_boundaries=args.explicit_boundaries)
    print(json.dumps({name: {'fee': row['fee'], 'audit': row['audit']} for name, row in report['policies'].items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
