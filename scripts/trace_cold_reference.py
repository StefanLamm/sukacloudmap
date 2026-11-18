"""Opt-in cold-start route diagnosis on an immutable graph, never production.

Reference routes are listener targets only. No reference allocation or route is
passed to the optimizer. Capture overhead is charged to the diagnostic budget;
these timings and fees are not formal paired-performance acceptance results.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.planning_inputs import content_hash
from app.route_experiments import DeepExperiments
from app.source_provenance import collect_source_provenance
from app.routing_engine import normalize
from scripts.benchmark_algorithms import FrozenEvidenceRoad, independent_audit, private_json
from scripts.reference_route_trace import ReferenceStageTrace, read


def compressed(path, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode()
    Path(path).write_bytes(gzip.compress(raw, mtime=0))
    Path(path).chmod(0o600)
    return {'file': str(Path(path).resolve()), 'events':len(value) if isinstance(value,list) else None,
            'content_hash':hashlib.sha256(raw).hexdigest()}


def run_case(case_file, output, *, reference_file=None, seconds=300, wait_seconds=None,
             seed=0, experiments=None, capture_stream=False, no_hints=False,
             cancel_file=None, optimizer_profile='HYBRID', optimizer=None):
    launched=time.monotonic()
    if not math.isfinite(seconds) or seconds<0:
        raise ValueError('Compute seconds must be finite and nonnegative')
    wait_seconds=seconds+min(1800,max(120,seconds*2)) if wait_seconds is None else wait_seconds
    if not math.isfinite(wait_seconds) or wait_seconds<=0:
        raise ValueError('Wait seconds must be finite and positive')
    wait_deadline=launched+wait_seconds
    if experiments is not None:
        experiments=asdict(DeepExperiments.parse(experiments))
    output=Path(output).resolve()
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    case_path=Path(case_file).resolve()
    case=read(case_path)
    body_path=case_path.parent/case['body_file']
    body=read(body_path)
    if content_hash(body)!=case['input_hash']:
        raise ValueError('Frozen input hash mismatch')
    reference=read(reference_file) if reference_file else {'routes':[]}
    trace=ReferenceStageTrace(reference)
    user_cancel=lambda:bool(cancel_file and Path(cancel_file).exists())
    graph_path=Path(case['road_database'])
    if not graph_path.is_absolute():graph_path=case_path.parent/graph_path
    road=FrozenEvidenceRoad(graph_path,cancelled=user_cancel)
    deep_events=[] if capture_stream else None
    native_events=[] if capture_stream else None
    price_events=[] if capture_stream else None
    if road.graph_hash!=case['graph_hash']:
        road.close()
        raise ValueError('Frozen graph hash mismatch')
    if no_hints:road.route_seed_samples={}
    road.settings=SimpleNamespace(planning_optimizer_profile=optimizer_profile)
    road.route_stage_trace=trace
    if capture_stream:
        road.candidate_stream_observer=deep_events.append
        road.native_candidate_stream_observer=native_events.append
        road.priced_candidate_observer=lambda spec:price_events.append({'kind':'priced_candidate',**spec})
    provenance=collect_source_provenance(ROOT)
    report={'schema_version':1,'scope':'COLD_DIAGNOSIS_WITHOUT_REFERENCE_INJECTION',
        'timing_scope':'DIAGNOSTIC_STREAM_CAPTURE' if capture_stream else 'DIAGNOSTIC_TIMING',
        'formal_effectiveness_claimed':False,'reference_injected':False,'initial_result_supplied':False,
        'case_file':str(case_path),'case_manifest_hash':content_hash(case),'input_hash':case['input_hash'],
        'graph_hash':road.graph_hash,'historical_hints_hash':content_hash(road.route_seed_samples),
        'historical_hints_enabled':not no_hints,'reference_hash':content_hash(reference),
        'reference_file':str(Path(reference_file).resolve()) if reference_file else None,
        'seed':seed,'PYTHONHASHSEED':os.environ.get('PYTHONHASHSEED'),
        'hash_randomization':sys.flags.hash_randomization,'compute_budget_seconds':seconds,
        'wait_budget_seconds':wait_seconds,'requested_flags':experiments,
        'actual_flags':None,'optimizer_profile':optimizer_profile,'source_provenance':provenance,
        'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'external_calls':0,'business_database_written':False}
    if optimizer is None:
        from app.route_deep import optimize_deep
        optimizer=optimize_deep
    started=time.monotonic()
    report.update(wait_started_monotonic=launched,wait_deadline_monotonic=wait_deadline,
                  preparation_seconds=round(started-launched,6))
    before=resource.getrusage(resource.RUSAGE_SELF)
    trace.mark('COLD_RUN',status='ENTERED',scope='CURRENT_RUN')
    try:
        result=optimizer(body,road,seconds,seed,user_cancel,initial_result=None,
            experiments=experiments,wall_budget_seconds=max(0.,wait_deadline-time.monotonic()))
        solve_elapsed=time.monotonic()-started
        audit=independent_audit(normalize(body),result,road)
        report.update(status='RETURNED',fee=result['total_fee'],audit=audit,
            algorithm=result.get('algorithm',{}),actual_flags=result.get('algorithm',{}).get('experiments'),
            solve_elapsed_seconds=round(solve_elapsed,6),
            result_file=compressed(output/'result.json.gz',result)['file'])
        trace.mark('FINAL',status='COMPLETE' if audit['valid'] else 'INTERRUPTED',scope='CURRENT_RUN')
        for row in result['routes']:
            trace.route('FINAL',row,status='INDEPENDENTLY_VALIDATED' if audit['valid'] else 'AUDIT_FAILED',
                        fee=row['fee'],scope='CURRENT_RUN')
        trace.mark('COLD_RUN',status='COMPLETE',scope='CURRENT_RUN')
    except Exception as exc:
        report.update(status='ERROR',exception_type=type(exc).__name__,exception=str(exc),
                      traceback=traceback.format_exc(),solve_elapsed_seconds=round(time.monotonic()-started,6))
        trace.mark('COLD_RUN',status='INTERRUPTED',reason=type(exc).__name__,scope='CURRENT_RUN')
    finally:
        after=resource.getrusage(resource.RUSAGE_SELF)
        report.update(cpu_seconds=round(after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,6),
            deadline_exhausted=time.monotonic()>=wait_deadline,user_cancelled=user_cancel(),
            road_stats=road.stats(),source_unchanged=collect_source_provenance(ROOT)['app_source_hash']==provenance['app_source_hash'],
            streams_saved_after_solve_timer=True)
        if report['deadline_exhausted']:report['status']='TIMEOUT'
        if not report['source_unchanged']:report['status']='SOURCE_CHANGED'
        road.close()
        private_json(output/'trace.json',trace.snapshot())
        report['trace_file']=str(output/'trace.json')
        if capture_stream:
            # The unified stream preserves exact live ordering. priced_candidate
            # is a separate channel, not a second identity admission. Offline
            # retention must dispatch events by kind to reproduce live policies.
            report['deep_stream']=compressed(output/'deep-stream.json.gz',deep_events)
            report['native_stream']=compressed(output/'native-stream.json.gz',native_events)
            report['price_stream']=compressed(output/'price-stream.json.gz',price_events)
        private_json(output/'run.json',report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--reference');parser.add_argument('--seconds',type=float,default=300)
    parser.add_argument('--wait-seconds',type=float);parser.add_argument('--seed',type=int,default=0)
    parser.add_argument('--hash-seed',type=int,default=0)
    parser.add_argument('--experiments',type=json.loads)
    parser.add_argument('--capture-stream',action='store_true');parser.add_argument('--no-hints',action='store_true')
    parser.add_argument('--cancel-file');parser.add_argument('--optimizer-profile',default='HYBRID')
    parser.add_argument('--_hash-worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if not 0<=args.hash_seed<2**32:parser.error('hash-seed must be in [0, 2**32)')
    if not args._hash_worker:
        env={**os.environ,'PYTHONHASHSEED':str(args.hash_seed),'OMP_NUM_THREADS':'1',
             'OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','VECLIB_MAXIMUM_THREADS':'1'}
        call=[sys.executable,str(Path(__file__).resolve()),*(sys.argv[1:] if argv is None else argv),'--_hash-worker']
        return subprocess.run(call,env=env,check=False).returncode
    if os.environ.get('PYTHONHASHSEED')!=str(args.hash_seed):parser.error('Worker hash seed differs')
    result=run_case(args.case,args.output,reference_file=args.reference,seconds=args.seconds,
        wait_seconds=args.wait_seconds,seed=args.seed,experiments=args.experiments,
        capture_stream=args.capture_stream,no_hints=args.no_hints,cancel_file=args.cancel_file,
        optimizer_profile=args.optimizer_profile)
    print(json.dumps({k:result.get(k) for k in ('status','fee','timing_scope','reference_injected','trace_file')}))
    return 0 if result['status']=='RETURNED' and result.get('audit',{}).get('valid') else 1


if __name__=='__main__':
    raise SystemExit(main())
