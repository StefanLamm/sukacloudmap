"""Offline only. Synthetic grid follows S1 road fixture; never sent to real APIs."""
import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import ROOT
from app.planning_inputs import content_hash
from app.road_service import evidence,road_request
from app.routing_engine import normalize,optimize


class GridRoad:
    def __init__(self,points,cache):self.points=points;self.cache=cache;self.hits=self.reads=0
    def segment(self,v,a,b,t):
        request=road_request(v,a,b,t,'FIXTURE');request['fixture_version']='directed-grid-v1'
        key=content_hash(request)
        if key in self.cache:self.hits+=1;return self.cache[key]
        ax,ay=self.points[a['id']];bx,by=self.points[b['id']]
        meters=0 if a['id']==b['id'] else 100+100*(abs(ax-bx)+abs(ay-by))+(0 if b['id']>a['id'] else 25)
        assert (meters*3)%25==0
        item=evidence(request,meters,meters*3//25,'FIXTURE',restriction='VERIFIED_FIXTURE_ONLY',fixture_id='SCALE_4W_800S_24000P_50V')
        self.cache[key]=item;self.reads+=1;return item
    def stats(self):return {'source':'FIXTURE','calls':0,'cache_hits':self.hits,'fixture_reads':self.reads,'call_limit':0}


def scale_input():
    source=json.loads((ROOT/'产品交付/第一阶段需求与开发交接/规模固定数据集.json').read_text())
    assert source['api_use_prohibited'] is True
    demand={d['store_id']:d for d in source['demand']}
    points={p['id']:p['test_grid'] for p in source['warehouses']+source['stores']}
    body={'delivery_date':source['date'],'warehouses':source['warehouses'],'vehicles':source['vehicles'],'quotes':source['quotes'],
          'tasks':[{**s,'pieces':demand[s['id']]['pieces'],'longitude':str(s['test_grid'][0]),'latitude':str(s['test_grid'][1]),'crs':'TEST_GRID'} for s in source['stores']]}
    return source,body,points


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--runs',type=int,default=20);parser.add_argument('--budget',type=int,default=15);parser.add_argument('--p95-limit',type=float);parser.add_argument('--output',default='docs/stage5-8-performance.json');args=parser.parse_args()
    source,body,points=scale_input();cache={};runs=[]
    # One cold fixture warmup, not one of the 20 measured hot samples.
    optimize(body,GridRoad(points,cache),args.budget)
    for n in range(args.runs):
        provider=GridRoad(points,cache);started=time.perf_counter();result=optimize(body,provider,args.budget);elapsed=time.perf_counter()-started
        proof=result['check'];assert proof['valid'] and proof['task_count']==800 and proof['total_pieces']==24000 and proof['used_vehicles']<=50
        assert float(result['total_fee'])<=float(result['algorithm']['all_common_baseline'])
        assert not result['confirmable'] and result['road_source']=='FIXTURE'
        runs.append({'run':n+1,'seconds':round(elapsed,5),'total_fee':result['total_fee'],'baseline':result['algorithm']['all_common_baseline'],
                     'used_vehicles':proof['used_vehicles'],'tasks':proof['task_count'],'pieces':proof['total_pieces'],'valid':proof['valid'],
                     'algorithm':result['algorithm'],'plan_hash':content_hash({k:v for k,v in result.items() if k!='algorithm'})})
        print(json.dumps(runs[-1],ensure_ascii=False),flush=True)
    ordered=sorted(r['seconds'] for r in runs);p95=ordered[math.ceil(.95*len(ordered))-1]
    report={'date':'2026-11-11','source':'FIXTURE','dataset_id':source['id'],'dataset_hash':content_hash(source),'road_version':source['road_fixture']['version'],
            'environment':{'python':platform.python_version(),'platform':platform.platform(),'database':'none, pure solver benchmark'},
            'warmup_runs':1,'hot_runs':args.runs,'budget_seconds':args.budget,'p95_method':'nearest-rank','p95_seconds':p95,
            'p95_limit_seconds':args.p95_limit,'performance_status':'PASS' if args.p95_limit is not None and p95<=args.p95_limit else 'NOT_FROZEN' if args.p95_limit is None else 'FAIL',
            'all_constraints_passed':True,'unique_plan_count':len({r['plan_hash'] for r in runs}),'real_api_calls':0,
            'real_cold_runs':{'status':'DEFERRED_BY_USER','requested_count':3,'actual_count':0},'runs':runs}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2));print('P95',p95,'status',report['performance_status'])


if __name__=='__main__':main()
