"""Offline reference-route probes. Every result is injection diagnostic only."""
from __future__ import annotations
import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.routing_engine import normalize
from app.route_columns import select_portfolio
from app.route_metrics import evaluate_candidate
from app.route_verification import recipe
from scripts.benchmark_algorithms import FrozenEvidenceRoad, independent_audit, private_json


def diagnose(freeze_dir, output, seconds=5):
    folder = Path(freeze_dir)
    output = Path(output)
    output.mkdir(mode=0o700, exist_ok=False)
    body = json.loads((folder/'input.json').read_text())
    data = normalize(body)
    results = [json.loads((folder/f'rebound-{i}.json').read_text()) for i in range(3)]
    manifest = json.loads((folder/'case.json').read_text())
    road = FrozenEvidenceRoad(folder/'frozen-roads.db')
    try:
        costs = []
        for index, result in enumerate(results):
            groups = defaultdict(lambda: {'fee':Decimal(0), 'route_fee':Decimal(0),
                'dedicated':[], 'common_ids':[]})
            for row in result['routes']:
                task = data['tasks'][row['task_ids'][0]]
                group = groups[(task['warehouse_id'], task['signature'])]
                group['route_fee'] += Decimal(row['fee'])
                group['fee'] += Decimal(row['fee'])
                group['dedicated'].append({k:row[k] for k in
                    ('vehicle_id','task_ids','fee','pieces','meters','seconds')})
            for row in result['common']:
                task = data['tasks'][row['task_id']]
                group = groups[(task['warehouse_id'], task['signature'])]
                group['fee'] += Decimal(row['fee'])
                group['common_ids'].append(row['task_id'])
            costs.append({'name':manifest['plans'][index]['name'], 'total_fee':result['total_fee'],
                'groups':[{'warehouse_id':key[0],'quote_signature':key[1],
                    **{k:str(v) if isinstance(v,Decimal) else v for k,v in group.items()}}
                    for key, group in sorted(groups.items(), key=lambda item:str(item[0]))]})
        private_json(output/'route-diff.json', costs)
        probes = []
        for target_index in (1,2):
            incumbent = results[target_index]
            for mode in ('no_reference_injection', 'reference_columns_only', 'reference_complete_allocation'):
                before = time.monotonic()
                proposed = select_portfolio(body, road, [incumbent], seconds=min(5., seconds),
                    candidate_recipes=(recipe(results[0])['routes'] if mode=='reference_columns_only' else ()),
                    complete_recipes=([recipe(results[0])] if mode=='reference_complete_allocation' else ()))
                audit = independent_audit(data, proposed, road)
                row = {'target':manifest['plans'][target_index]['name'], 'mode':mode,
                    'diagnostic_only':True, 'cold_start':False,
                    'before_fee':incumbent['total_fee'],'after_fee':proposed['total_fee'],
                    'reduction':str(Decimal(incumbent['total_fee'])-Decimal(proposed['total_fee'])),
                    'elapsed_seconds':round(time.monotonic()-before,5),
                    'audit':audit,'algorithm':proposed['algorithm']}
                probes.append(row)
                private_json(output/f'probe-{target_index}-{mode}.json', proposed)
        reference = []
        for row in results[0]['routes']:
            evaluated = evaluate_candidate(data,row['vehicle_id'],row['task_ids'],road)
            reference.append({'vehicle_id':row['vehicle_id'],'task_ids':row['task_ids'],
                'fee':evaluated['fee'],'errors':evaluated['errors'],
                'same_order_in_targets':[any(x['task_ids']==row['task_ids'] for x in result['routes'])
                                         for result in results[1:]],
                'same_coverage_in_targets':[any(set(x['task_ids'])==set(row['task_ids']) for x in result['routes'])
                                            for result in results[1:]]})
        summary = {'scope':'REFERENCE_INJECTION_DIAGNOSTIC_NOT_COLD_START',
            'frozen_input_hash':manifest['input_hash'],'graph_hash':road.graph_hash,
            'probes':probes,'reference_route_feasibility':reference,
            'business_database_written':False, 'external_calls':0,
            'trace_limitations':'Saved aggregate diagnostics do not include a complete per-route generation/retention trace. Injection can separate model/selection from missing columns but cannot prove historical generation versus retention.'}
        private_json(output/'summary.json', summary)
        return summary
    finally:
        road.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze-dir',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--seconds',type=float,default=5)
    args=parser.parse_args()
    result=diagnose(args.freeze_dir,args.output,args.seconds)
    print(json.dumps([{k:v for k,v in row.items() if k not in {'algorithm','audit'}}
        |{'valid':row['audit']['valid'],'solver_status':row['algorithm'].get('solver_status'),
          'pool_optimal':row['algorithm'].get('finite_pool_cost_optimal')}
        for row in result['probes']],ensure_ascii=False))
