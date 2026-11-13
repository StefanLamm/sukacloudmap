"""Run frozen validation repair with proposals restricted to actually refreshed edges.

The launcher is the producer; the unchanged underlying repair implementation is
explicitly hash-checked and bound in every output. Old graph evidence is used
only for the required separate bound-case quotation, never for proposal bridges.
"""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT)not in sys.path:sys.path.insert(0,str(ROOT))
from app.planning_inputs import content_hash
from scripts.benchmark_algorithms import private_json
from scripts.prepare_corrected_application import require
from scripts.refresh_application_roads import file_sha
import scripts.repair_fresh_candidate as implementation

IMPLEMENTATION_SHA='5fa7204709974f16861c2ed896ff99065b6a757c1f71490e7497b95eb5f49b7e'

def run(case_file,application_report,failed_refresh,output,cutoff):
    source=Path(implementation.__file__).resolve()
    require(file_sha(source)==IMPLEMENTATION_SHA,'Frozen repair implementation changed')
    original=implementation.evaluate_candidate
    def fresh_only(data,vehicle,ids,road,control=None):
        row=original(data,vehicle,ids,road,control)
        if row is None or row.get('errors'):return row
        origin=data['warehouses'][data['vehicles'][vehicle]['warehouse_id']]
        for i in ids:
            destination=data['tasks'][i];segment=road.known_segment(origin,destination)
            if segment is None or segment['received_at']<cutoff:
                row['errors'].append({'code':'NOT_ACTUALLY_REFRESHED_BRIDGE','message':'Validation repair requires a measured refreshed segment','task_id':i})
                break
            origin=destination
        return row
    implementation.evaluate_candidate=fresh_only
    # This separate hash-bound launcher controls proposal admissibility and is
    # truthfully named as the producer. No underlying source file is edited.
    implementation.__file__=__file__
    try:summary=implementation.repair(case_file,application_report,failed_refresh,output,1)
    finally:implementation.evaluate_candidate=original;implementation.__file__=str(source)
    root=Path(summary['report_file']).parent;report=json.loads((root/'latest-input-report.json').read_text())
    proof=json.loads((root/'fresh-graph-repaired-proof.json').read_text())
    require(proof['road_evidence']['oldest_received_at']>=cutoff,'A selected actual-graph edge was not refreshed in this validation round')
    provenance=report['validation_repair'];provenance.update(fresh_only_received_at_minimum=cutoff,
        no_historical_proposal_bridges=True,underlying_repair_script_file=str(source),underlying_repair_script_sha256=IMPLEMENTATION_SHA)
    result=json.loads((root/'latest-input-result.json').read_text());result['algorithm']['validation_repair']=provenance
    report.update(validation_repair=provenance,result_hash=content_hash(result))
    report['origin_files_sha256'][str(source)]=IMPLEMENTATION_SHA
    private_json(root/'latest-input-result.json',result);private_json(root/'latest-input-report.json',report)
    return {**summary,'fresh_only_received_at_minimum':cutoff,'producer_script_sha256':file_sha(__file__)}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--case',required=True);p.add_argument('--application-report',required=True)
    p.add_argument('--failed-refresh',required=True);p.add_argument('--output',required=True);p.add_argument('--cutoff',type=int,default=1794540447)
    a=p.parse_args();print(json.dumps(run(a.case,a.application_report,a.failed_refresh,a.output,a.cutoff),ensure_ascii=False))
