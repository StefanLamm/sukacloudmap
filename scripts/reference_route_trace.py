"""Inspect frozen streams against arbitrary reference routes without injection.

Read-only postprocessing. This deliberately distinguishes lack of observation
from a proof that an uninstrumented historical generator never created a route.
"""
from __future__ import annotations
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.route_retention import replay_frozen_stream
from app.routing_engine import normalize
from scripts.benchmark_algorithms import private_json


class ReferenceStageTrace:
    """Opt-in bounded listeners; reference identities are never solver hints.

    Native callers pass identities only. They must not price, query a road,
    format JSON, or write files in their callbacks. Snapshots are serialized
    after the search timer. A missing event proves absence only when the
    corresponding stage is marked COMPLETE for the required scope.
    """
    def __init__(self, reference, *, clock=time.monotonic):
        if 'result' in reference:
            reference=reference['result']
        self.clock=clock
        self.started=clock()
        self.stages={}
        self.counts=Counter()
        self.scope_counts=Counter()
        self.orders={}
        self.coverages={}
        self.targets=[]
        self.lengths=set()
        for index,row in enumerate(reference['routes']):
            order=tuple(row['task_ids']);coverage=frozenset(order)
            self.orders.setdefault(order,[]).append(index)
            self.coverages.setdefault(coverage,[]).append(index)
            self.lengths.add(len(order))
            self.targets.append({'vehicle_id':row['vehicle_id'],'task_ids':list(order),
                'reference_fee':row.get('fee'),'events':{}})

    def scope(self, prefix):
        self.scope_counts[prefix]+=1
        return prefix+'_'+str(self.scope_counts[prefix])

    def mark(self, stage, *, status, reason=None, scope=None):
        self.stages.setdefault(stage,{})[scope or 'RUN']={'status':status,'reason':reason,'scope':scope,
                            'wall_seconds':round(self.clock()-self.started,6)}

    def route(self, stage, spec, *, status='SEEN', reason=None, fee=None, compute_seconds=None, scope=None):
        """O(stops) identity matching with bounded O(targets × stages) memory."""
        self.counts[stage]+=1
        order=tuple(spec['task_ids'])
        if len(order) not in self.lengths:
            return
        exact=set(self.orders.get(order,()))
        coverage=self.coverages.get(frozenset(order),())
        if not exact and not coverage:
            return
        now=round(self.clock()-self.started,6)
        for index in set(coverage)|exact:
            target=self.targets[index]
            key=(stage,status,scope)
            events=target['events']
            if key not in events:
                events[key]={'stage':stage,'status':status,'scope':scope,'count':0,
                    'exact_order_count':0,'same_coverage_count':0,'same_reference_vehicle_count':0,
                    'first_wall_seconds':now,'first_compute_seconds':compute_seconds,
                    'first_stage_observation':self.counts[stage],'last_wall_seconds':now,
                    'reason_codes':set(),'min_fee':None,
                    'first_witness':{'vehicle_id':spec.get('vehicle_id'),'task_ids':list(order)},
                    'first_exact_witness':None}
            event=events[key]
            event['count']+=1
            event['exact_order_count']+=int(index in exact)
            if index in exact and event['first_exact_witness'] is None:
                event['first_exact_witness']={'vehicle_id':spec.get('vehicle_id'),'task_ids':list(order)}
            event['same_coverage_count']+=int(index in coverage)
            event['same_reference_vehicle_count']+=int(spec.get('vehicle_id')==target['vehicle_id'])
            event['last_wall_seconds']=now
            if reason:
                # Call sites pass finite business error codes, not arbitrary messages.
                event['reason_codes'].add(reason)
            if fee is not None:
                from decimal import Decimal
                if event['min_fee'] is None or Decimal(str(fee))<Decimal(event['min_fee']):
                    event['min_fee']=str(fee)

    def snapshot(self):
        return {'scope':'OPT_IN_REFERENCE_IDENTITY_DIAGNOSIS_NOT_A_SOLVER_INPUT',
            'stage_status':self.stages,'observations_by_stage':dict(self.counts),
            'targets':[{**{k:v for k,v in target.items() if k!='events'},
                'events':[{**event,'reason_codes':sorted(event['reason_codes'])}
                          for event in target['events'].values()]}
                       for target in self.targets],
            'fees_evaluated_in_observer':0,'external_calls_in_observer':0,
            'absence_requires_stage_complete_and_matching_scope':True}


def read(path):
    raw=Path(path).read_bytes()
    return json.loads(gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw)


def identity(row):
    return row['vehicle_id'], tuple(row['task_ids'])


def route_digest(row):
    raw=json.dumps(row,sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()


def inspect(body, reference, events, *, native_events=(), policy='first', limit=10000):
    if 'result' in reference:
        reference=reference['result']
    targets=reference['routes']
    checkpoint=next((i for i,event in enumerate(events) if event.get('kind')=='checkpoint'
                     and event.get('stage')=='PRE_COLUMN_SELECTION'),None)
    before=events[:checkpoint] if checkpoint is not None else events
    identity_events=[event for event in before if event.get('kind')!='priced_candidate']
    replay=replay_frozen_stream(identity_events,policies=(policy,),limit=limit,data=normalize(body),
                                explicit_boundaries=True)
    retained=replay['policies'][policy]['candidates']
    protected=[row for allocation in replay['complete_recipes'] for row in allocation['routes']]
    generated=[event for event in before if event.get('kind')=='candidate']
    native=[event for event in native_events if event.get('kind','candidate')=='candidate']
    selected=reference['routes']
    def matches(rows,target,coverage=False,physical=False):
        target_ids=frozenset(target['task_ids']) if coverage else tuple(target['task_ids'])
        return [index for index,row in enumerate(rows)
                if (frozenset(row['task_ids']) if coverage else tuple(row['task_ids']))==target_ids
                and (not physical or row['vehicle_id']==target['vehicle_id'])]
    rows=[]
    for target in targets:
        stages={}
        for name,items in [('native_observed',native),('evaluated_observed',generated),
                           ('retained',retained),('protected_complete_allocation',protected)]:
            exact=matches(items,target)
            coverage=matches(items,target,True)
            physical=matches(items,target,False,True)
            stages[name]={'exact_order_count':len(exact),'same_coverage_count':len(coverage),
                'same_physical_order_count':len(physical),
                'first_exact_order_position':exact[0]+1 if exact else None,
                'first_same_coverage_position':coverage[0]+1 if coverage else None}
        earlier=stages['evaluated_observed']['exact_order_count']
        admitted=(stages['retained']['exact_order_count'] or
                  stages['protected_complete_allocation']['exact_order_count'])
        if earlier and not admitted:
            finding='OBSERVED_BEFORE_GLOBAL_RETENTION_BUT_ABSENT_AT_POOL_BOUNDARY'
        elif admitted:
            finding='AVAILABLE_TO_POOL_ORDER_REPRICING'
        elif stages['native_observed']['exact_order_count']:
            finding='NATIVE_OBSERVED_BUT_NOT_IN_EVALUATED_STREAM'
        else:
            finding='NOT_OBSERVED_IN_SUPPLIED_STREAMS'
        rows.append({'route_hash':route_digest({'task_ids':target['task_ids']}),
            'reference_vehicle_id':target['vehicle_id'],'task_ids':target['task_ids'],
            'reference_fee':target['fee'],'stops':len(target['task_ids']),
            'stages':stages,'first_visible_boundary':finding})
    return {'scope':'POSTHOC_REFERENCE_OBSERVATION_NOT_INJECTION',
        'policy':policy,'pool_limit':limit,'pre_selection_checkpoint_present':checkpoint is not None,
        'evaluated_stream_events':len(events),'native_stream_events':len(native_events),
        'retention':replay['policies'][policy]['diagnostics'],
        'reference_routes':rows,
        'limits':['No historical generation claim when that stage was not recorded.',
                  'Exact order absence does not rule out an equivalent or better coverage order.',
                  'Native stream capture may add callback overhead; use diagnostic timing label.',
                  'Availability is at order-repricing boundary; exact portfolio pruning needs its own trace.']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True);parser.add_argument('--reference',required=True)
    parser.add_argument('--stream',required=True);parser.add_argument('--native-stream')
    parser.add_argument('--policy',default='first');parser.add_argument('--limit',type=int,default=10000)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result=inspect(read(args.input),read(args.reference),read(args.stream),
        native_events=read(args.native_stream) if args.native_stream else (),
        policy=args.policy,limit=args.limit)
    private_json(args.output,result)
    print(json.dumps(Counter(row['first_visible_boundary'] for row in result['reference_routes'])))
