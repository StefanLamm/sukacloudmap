"""Read-only frozen-stream pricing-gap probes; no search or provider requests.

An absent successful price receipt is not, by itself, proof that a route was
never sent to evaluation. This command reports that exact boundary and its
limits, separately from candidate-retention or full-allocation improvements.
"""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
from decimal import Decimal
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.planning_inputs import content_hash
from app.route_metrics import evaluate_candidate
from app.routing_engine import assemble,common_fee,evaluate_route,normalize
from scripts.benchmark_algorithms import FrozenEvidenceRoad,independent_audit,private_json
from scripts.reference_route_trace import read


def analyze(case_file,native_stream,price_stream,*,deep_stream=None,run_report=None,proof_top=5):
    case_path=Path(case_file).resolve();case=read(case_path)
    body=read(case_path.parent/case['body_file'])
    if content_hash(body)!=case['input_hash']:raise ValueError('Frozen input changed')
    data=normalize(body)
    native=read(native_stream);prices=read(price_stream)
    run=read(run_report) if run_report else None
    if run is not None:
        if run.get('input_hash')!=case['input_hash'] or run.get('graph_hash')!=case['graph_hash']:
            raise ValueError('Stream run input/graph differs from frozen case')
        for name,events in [('native_stream',native),('price_stream',prices)]:
            expected=run.get(name,{}).get('content_hash')
            if expected is not None and expected!=content_hash(events):raise ValueError('Stream receipt hash changed: '+name)
    if deep_stream:
        deep=read(deep_stream)
        expected=run.get('deep_stream',{}).get('content_hash') if run else None
        if expected is not None and expected!=content_hash(deep):raise ValueError('Deep stream receipt hash changed')
        prices=[*prices,*(event for event in deep if event.get('kind') in {'candidate','priced_candidate'}
                        and event.get('retention_fee',event.get('fee')) is not None)]
    models=defaultdict(dict);native_counts=Counter();assignments=defaultdict(list)
    assignment_channel_captured=False
    for event in native:
        if event.get('kind')=='native_assignment':
            assignment_channel_captured=True
            for row in event['routes']:assignments[tuple(row['task_ids'])].append(event.get('slice'))
            continue
        if event.get('kind','candidate')!='candidate' or not event.get('task_ids'):
            continue
        order=tuple(event['task_ids']);vid=event['vehicle_id']
        models[order].setdefault(vid,{'vehicle_id':vid,'task_ids':list(order)})
        native_counts[order]+=1
    priced_orders=set();priced_physical=set();coverage_prices={}
    for event in prices:
        fee=event.get('retention_fee',event.get('fee'))
        if fee is None or not event.get('task_ids'):continue
        amount=Decimal(fee)
        if not amount.is_finite() or amount<0:continue
        order=tuple(event['task_ids']);coverage=frozenset(order)
        priced_orders.add(order);priced_physical.add((event.get('vehicle_id'),order))
        coverage_prices[coverage]=min(coverage_prices.get(coverage,amount),amount)
    graph=Path(case['road_database'])
    if not graph.is_absolute():graph=case_path.parent/graph
    road=FrozenEvidenceRoad(graph)
    if road.graph_hash!=case['graph_hash']:
        road.close();raise ValueError('Frozen graph changed')
    counts=Counter();opportunities=[]
    try:
        for order,options in models.items():
            if order in priced_orders:
                counts['native_orders_with_successful_price_receipt']+=1
                continue
            counts['native_orders_without_successful_price_receipt']+=1
            best=None
            for spec in options.values():
                row=evaluate_candidate(data,spec['vehicle_id'],spec['task_ids'],road)
                if row is None:
                    counts['missing_metric_vehicle_orders']+=1;continue
                if row['errors'] or row['fee'] is None:
                    counts['currently_invalid_vehicle_orders']+=1;continue
                if best is None or Decimal(row['fee'])<Decimal(best['fee']):best=row
            if best is None:continue
            counts['currently_feasible_unpriced_orders']+=1
            mandatory=sum(data['tasks'][item]['common_unit_price'] is None for item in order)
            common=sum((Decimal(common_fee(data['tasks'][item]['pieces'],data['tasks'][item]['common_unit_price']) or '0')
                        for item in order),Decimal(0))
            net=Decimal(best['fee'])-common
            known=coverage_prices.get(frozenset(order))
            if known is not None and known<=Decimal(best['fee']):
                counts['dominated_by_already_priced_same_coverage']+=1;continue
            if not mandatory and net>=0:
                counts['not_economic_against_own_common_alternative']+=1;continue
            opportunities.append({'vehicle_id':best['vehicle_id'],'task_ids':list(order),
                'actual_route_fee':best['fee'],'actual_optional_common_opportunity':str(common),
                'actual_net_fee':str(net),'mandatory_tickets':mandatory,
                'native_observations':native_counts[order],
                'explicit_returned_assignment_slices':assignments.get(order,[]) if assignment_channel_captured else None,
                'evidence_boundary':'NATIVE_PROPOSED_WITHOUT_MATCHING_SUCCESSFUL_PRICED_ORDER',
                'already_priced_same_coverage_fee':str(known) if known is not None else None,
                'scope':'CURRENT_FROZEN_CANDIDATE_ONLY_NOT_AN_INCUMBENT_OR_WHOLE_PLAN_SAVING'})
        opportunities.sort(key=lambda row:(-row['mandatory_tickets'],Decimal(row['actual_net_fee']),row['task_ids']))
        for row in opportunities[:max(0,proof_top)]:
            actual=evaluate_route(data,row['vehicle_id'],row['task_ids'],road)
            covered=set(row['task_ids'])
            common={i for i,task in data['tasks'].items() if i not in covered and task['common_unit_price'] is not None}
            proposed=assemble(data,[actual],common,set(data['tasks'])-covered-common)
            row['independent_audit_against_common_background']=independent_audit(data,proposed,road)
            row['whole_background_feasible']=not proposed['unassigned'] and proposed['fees_complete']
        return {'scope':'FROZEN_STREAM_PRICING_RECEIPT_GAP_DIAGNOSIS','search_performed':False,
            'input_hash':case['input_hash'],'graph_hash':road.graph_hash,
            'native_stream_hash':content_hash(native),'price_events_hash':content_hash(prices),
            'native_unique_orders':len(models),'priced_unique_orders':len(priced_orders),
            'returned_assignment_channel_captured':assignment_channel_captured,
            'native_physical_aliases_with_order_already_priced':sum((vid,order) not in priced_physical
                for order,options in models.items() if order in priced_orders for vid in options),
            'counts':dict(counts),'economic_or_required_opportunities':opportunities,
            'run_binding_matches':None if run is None else run.get('input_hash')==case['input_hash'] and run.get('graph_hash')==road.graph_hash,
            'source_unchanged':run.get('source_unchanged') if run else None,
            'recorded_run_status':run.get('status') if run else None,
            'timing_scope':run.get('timing_scope') if run else None,
            'external_calls':0,'business_database_written':False,
            'limits':['A missing successful receipt does not distinguish not submitted from interrupted pricing.',
                'Previously rejected/cache entries require full PRICE_REQUEST/REJECTED identities to disambiguate.',
                'A priced equivalent/better task-coverage order is excluded from opportunity claims.',
                'Native best-assignment membership is not inferred from unmarked callback order.',
                'Per-route net value is not a whole-plan improvement and may overlap the current incumbent.',
                'These probes do not show that complete_assignment_pricing improves this case.']}
    finally:road.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True);parser.add_argument('--native-stream',required=True)
    parser.add_argument('--price-stream',required=True);parser.add_argument('--deep-stream')
    parser.add_argument('--run-report');parser.add_argument('--proof-top',type=int,default=5)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    value=analyze(args.case,args.native_stream,args.price_stream,deep_stream=args.deep_stream,
        run_report=args.run_report,proof_top=args.proof_top)
    private_json(args.output,value)
    print({'native_orders':value['native_unique_orders'],
           'opportunities':len(value['economic_or_required_opportunities']),'external_calls':0})
