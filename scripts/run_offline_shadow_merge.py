"""Run the unchanged merge helper against an explicit old-V3 shadow guard.

This produces offline route recipes only. The real business database may have
new user changes; latest-input repricing must independently precede publication.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from app.planning_inputs import content_hash
from scripts.benchmark_algorithms import private_json
from scripts.prepare_corrected_application import require
import scripts.merge_application_routes as merge


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True);parser.add_argument('--study-summary',required=True)
    parser.add_argument('--shadow-database',required=True);parser.add_argument('--legacy-manifest',required=True)
    parser.add_argument('--application-report',action='append',required=True)
    parser.add_argument('--result',action='append',default=[]);parser.add_argument('--run-output',default='cross-run-merged-m2-shadow')
    parser.add_argument('--budget-seconds',type=int,default=60);parser.add_argument('--selection-seconds',type=int,default=20)
    args=parser.parse_args();case=json.loads(Path(args.case).read_text())
    original=Path(case['paths']['database']).resolve();shadow=Path(args.shadow_database).resolve()
    require(shadow.is_file() and shadow!=original,'Shadow database must be a separate existing copy')
    original_guard=merge.main_guard;calls=[]
    def shadow_guard(database,body,correction):
        require(Path(database).resolve()==original,'Guard override refused an unexpected source path')
        value=original_guard(shadow,body,correction)
        require(value==case['main_guard'],'Old-V3 shadow state differs from captured case')
        calls.append({'requested_database':str(original),'actual_read_only_database':str(shadow)})
        return value
    merge.main_guard=shadow_guard
    try:
        report=merge.run_merge(args.case,args.study_summary,args.application_report,legacy_manifest=args.legacy_manifest,
            extra_results=args.result,output=args.run_output,budget_seconds=args.budget_seconds,selection_seconds=args.selection_seconds)
    finally:merge.main_guard=original_guard
    root=Path(report['run_output']);report_file=root/'merge-report.json';native=root/'native-merge-report.json'
    require(not native.exists(),'Native report archive already exists')
    native.touch(mode=0o600,exist_ok=False);native.write_bytes(report_file.read_bytes())
    report.update(status='VALIDATED_OFFLINE_RECIPE_ONLY',eligible_for_guarded_append=False,
        scope='OFFLINE_OLD_V3_RECIPE_ONLY',route_selection_scope=merge.SCOPE,
        source_business_database_written=False,guard_override_scope='PRIVATE_OLD_V3_SHADOW_ONLY',
        offline_shadow_database=str(shadow),guard_read_overrides=calls,
                  native_merge_report_file=str(native),native_merge_report_sha256=sha(native))
    private_json(report_file,report)
    manifest={'schema_version':1,'status':'VALIDATED_OFFLINE_RECIPE_ONLY','scope':'OFFLINE_OLD_V3_RECIPE_ONLY',
        'source_business_database_written':False,'external_calls':0,'eligible_for_direct_main_write':False,
        'case_file':str(Path(args.case).resolve()),'case_sha256':sha(args.case),
        'input_hash':case['transition']['corrected_input_hash'],'graph_hash':case['private_graph_hash'],
        'source_report_file':str(report_file),'source_report_sha256':sha(report_file),
        'result_file':report['result_file'],'result_sha256':sha(report['result_file']),'result_hash':report['result_hash'],
        'source_hashes':case['source_hashes'],'guard_override_scope':'PRIVATE_OLD_V3_SHADOW_ONLY',
        'shadow_database':str(shadow),'launcher_file':str(Path(__file__).resolve()),'launcher_sha256':sha(__file__),
        'old_main_guard':case['main_guard'],'actual_main_guard_not_asserted':True,
        'proof':report['proof'],'total_fee':report['merged_fee'],
        'note':'Old-V3 offline recipes only; preserve user city correction in main and independently reprice on latest V4 before fresh/append.'}
    private_json(root/'offline-m2-recipe-manifest.json',manifest)
    print(json.dumps({'status':manifest['status'],'fee':manifest['total_fee'],'result_file':manifest['result_file'],
        'manifest':str(root/'offline-m2-recipe-manifest.json'),'source_business_database_written':False},ensure_ascii=False))


if __name__=='__main__':main()
