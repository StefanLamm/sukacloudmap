"""Optional, five-minute V3 replay of two complete large historical days.

Preparing this source file does not read data or start calculations. Execute
only after the corrected four-run refinement has completed and the owner has
explicitly released the experiment slot. All four raw outputs are route
recipes for later current-225 repricing, never directly publishable plans.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DAYS = (("history110", "2026-10-06", 110, 2360),
        ("history61", "2026-10-03", 61, 1065))
PROFILE_NAMES = ("gls_p400", "hybrid")
MIN_START_RESERVE = 65 * 60
TOTAL_WALL_LIMIT = 300
FINISH_RESERVE = 5
DEPLOYMENT_RESERVE = 2700
ALGORITHM_SECONDS = 60


def read_json(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.touch(mode=0o600, exist_ok=True)
    temporary.chmod(0o600)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False) + "\n")
    temporary.replace(path)


def stop_worker(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=1)
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def run(args):
    started = time.monotonic()
    started_at = int(time.time())
    case_file = Path(args.case).resolve()
    study_file = Path(args.study_summary).resolve()
    refine_file = Path(args.refine_summary).resolve()
    prepared = read_json(case_file)
    study = read_json(study_file)
    refine = read_json(refine_file)
    output = (Path(args.output).resolve() if args.output else
              case_file.parent / "current-v3-large-days")
    require(output.parent == case_file.parent and output.name == "current-v3-large-days",
            "Output must be the dedicated private child of the prepared case")
    require(not output.exists(), "Refusing to overwrite an existing validation folder")
    require(prepared.get("status") == "PREPARED", "Corrected case is not prepared")
    require(study.get("status") == "COMPLETED" and study.get("source_files_unchanged") is True,
            "Formal study must be complete with unchanged mathematical sources")
    require(refine.get("status") == "COMPLETED" and len(refine.get("runs", [])) == 4,
            "All four corrected 900-second runs must finish before this optional check")
    require(refine.get("case_file_sha256") == file_sha(case_file) and
            refine.get("study_file_sha256") == file_sha(study_file),
            "Corrected refinement belongs to other case/study artifacts")
    require(study.get("deadline_epoch") == args.deadline_epoch,
            "Authorized deadline differs from the completed formal study")
    cutoff = args.deadline_epoch - DEPLOYMENT_RESERVE
    summary = {
        "schema_version": 1, "status": "PREPARING",
        "scope": "CURRENT_V3_TWO_COMPLETE_LARGE_REAL_DAYS_SAME_BUDGET",
        "case_file": str(case_file), "study_summary_file": str(study_file),
        "refine_summary_file": str(refine_file), "started_at": started_at,
        "deadline_epoch": args.deadline_epoch, "cutoff_epoch": cutoff,
        "minimum_start_reserve_seconds": MIN_START_RESERVE,
        "total_wall_limit_seconds": TOTAL_WALL_LIMIT,
        "algorithm_seconds_per_run": ALGORITHM_SECONDS, "seed": 0,
        "warm_policy": "SAME_CURRENT_ALL_COMMON_BASELINE_FOR_BOTH_PROFILES",
        "formal_study_rankings_unchanged": True,
        "source_business_database_written": False, "external_calls": 0,
        "new_graph_created": False, "eligible_for_direct_main_write": False,
        "raw_fee_policy": "RECIPE_ONLY_REPRICE_WITH_CURRENT225_QUANTITIES_BEFORE_MERGE",
        "runs": [], "cases": {},
    }
    if time.time() + MIN_START_RESERVE > cutoff:
        summary.update(status="SKIPPED_INSUFFICIENT_CUTOFF_RESERVE",
                       finished_at=int(time.time()))
        save(output / "validation-summary.json", summary)
        return summary

    from scripts.run_algorithm_study import (
        EXPERIMENT_ENV, profiles, source_gate, validate_sources,
    )
    ready = source_gate(prepared["source_ready_file"])
    require(ready is not None and ready["source_hashes"] == prepared["source_hashes"],
            "Prepared source-ready gate changed")
    require(digest(ready) == prepared["source_ready_hash"],
            "Complete source-ready artifact changed")
    require(file_sha(ROOT / "scripts/prepare_corrected_application.py") ==
            prepared["verification_script_sha256"], "Prepared verifier changed")
    expected = {**ready["source_hashes"],
                str(Path(__file__).resolve().relative_to(ROOT)): file_sha(__file__)}
    validate_sources(expected)
    source_manifest_hash = digest(expected)
    input_file = case_file.parent / "input.json"
    template = read_json(input_file)
    require(digest(template) == prepared["transition"]["corrected_input_hash"],
            "Current V3 template changed")
    fixed_artifacts = {str(p): file_sha(p) for p in
                       (case_file, study_file, refine_file, input_file)}
    graph = Path(prepared["private_graph"]).resolve()
    require(graph == case_file.parent / "frozen-roads.db",
            "Only the prepared private corrected graph may be reused")
    summary.update(
        status="RUNNING", source_hashes=expected,
        source_manifest_hash=source_manifest_hash,
        input_artifacts_sha256=fixed_artifacts,
        template_input_hash=digest(template),
        graph_hash=prepared["private_graph_hash"],
        graph_database_read_only=str(graph),
    )
    save(output / "validation-summary.json", summary)

    stopping = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))
    active = None

    def control():
        require(not stopping[0], "Optional validation was stopped")
        require(time.monotonic() - started < TOTAL_WALL_LIMIT - FINISH_RESERVE,
                "Optional validation exceeded its five-minute wall limit")
        require(time.time() < cutoff, "Preserving the deployment reserve")

    def check_sources():
        validate_sources(expected)
        require(all(file_sha(path) == sha for path, sha in fixed_artifacts.items()),
                "An input artifact changed during optional validation")

    try:
        # All data/matrix imports and reads occur only after the execution gate.
        from scripts.benchmark_algorithms import (
            FrozenEvidenceRoad, compatible_pairs, freeze_historical_case,
        )
        from app.routing_engine import normalize
        selected = {p["name"]: p for p in profiles() if p["name"] in PROFILE_NAMES}
        require(set(selected) == set(PROFILE_NAMES), "Frozen profiles differ")
        for name, day, tickets, pieces in DAYS:
            control()
            folder = output / name
            case = freeze_historical_case(
                template, args.history_database, day, folder, road_database=graph)
            body = read_json(folder / case["body_file"])
            original = study["manifest"]["cases"][name]["scenario"]
            require(len(body["tasks"]) == tickets and
                    body["totals"]["total_pieces"] == pieces,
                    "The full historical day quantities differ from the audited source")
            require(case["scenario"]["source_rows_discarded"] == 0 and
                    case["scenario"]["source_requirement_hash"] ==
                    original["source_requirement_hash"],
                    "Historical source rows changed or were discarded")
            require(case["graph_hash"] == prepared["private_graph_hash"],
                    "Historical replay used another measured graph")
            case["baseline_hash"] = digest(read_json(folder / case["baseline_file"]))
            save(folder / "case.json", case)
            road = FrozenEvidenceRoad(graph)
            try:
                require(road.graph_hash == prepared["private_graph_hash"],
                        "Corrected graph changed")
                for index, (a, b) in enumerate(compatible_pairs(normalize(body))):
                    if index % 128 == 0:
                        control()
                    require(road.known_segment(a, b) is not None,
                            "Corrected historical day lacks a measured direction")
            finally:
                road.close()
            summary["cases"][name] = {
                "day": day, "task_count": tickets, "pieces": pieces,
                "input_hash": case["input_hash"], "graph_hash": case["graph_hash"],
                "source_rows_discarded": 0, "scenario": case["scenario"],
            }
            save(output / "validation-summary.json", summary)
            for profile_name in PROFILE_NAMES:
                control()
                check_sources()
                require(TOTAL_WALL_LIMIT - FINISH_RESERVE -
                        (time.monotonic() - started) > 66,
                        "Not enough wall reserve for another complete 60-second run")
                run_output = folder / "profiles" / profile_name / "b60-s0-warm"
                run_output.mkdir(mode=0o700, parents=True, exist_ok=False)
                hard_seconds = min(70, TOTAL_WALL_LIMIT - FINISH_RESERVE -
                                   (time.monotonic() - started) - 3)
                spec = {
                    "source_hashes": expected,
                    "source_manifest_hash": source_manifest_hash,
                    "profile": selected[profile_name], "case_file": str(folder / "case.json"),
                    "case_name": name, "stage": "CURRENT_V3_LARGE_REAL_DAY_CHECK",
                    "budget_seconds": 60, "seed": 0, "warm_start": True,
                    "run_output": str(run_output), "hard_seconds": hard_seconds,
                    "expected_case_identity": {key: case.get(key) for key in
                                               ("input_hash", "graph_hash", "baseline_hash")},
                }
                save(run_output / "run-spec.json", spec)
                summary["current"] = {"case": name, "profile": profile_name,
                                      "budget_seconds": 60, "seed": 0,
                                      "started_at": int(time.time())}
                save(output / "validation-summary.json", summary)
                log = run_output / "process.log"
                log.touch(mode=0o600, exist_ok=False)
                with log.open("a") as stream:
                    active = subprocess.Popen(
                        [sys.executable, str(ROOT / "scripts/run_algorithm_study.py"),
                         "--run-one", str(run_output / "run-spec.json")],
                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream,
                        stderr=subprocess.STDOUT, start_new_session=True,
                        env={**os.environ, **EXPERIMENT_ENV},
                    )
                    end = time.monotonic() + hard_seconds
                    while active.poll() is None:
                        control()
                        require(time.monotonic() < end, "Optional replay worker timed out")
                        time.sleep(.1)
                    return_code = active.returncode
                active = None
                require(return_code == 0, "Optional replay worker failed; inspect its log")
                row = read_json(run_output / "completed-run.json")
                require(row["status"] == "VALID" and row["audit"]["valid"] and
                        row["audit"]["unassigned_tasks"] == 0 and
                        row["audit"]["dedicated_tasks"] + row["audit"]["common_tasks"] == tickets,
                        "Historical replay has invalid or incomplete allocation")
                require(row["graph_hash"] == prepared["private_graph_hash"] and
                        row["input_hash"] == case["input_hash"] and
                        row["road_stats"]["calls"] == 0 and
                        row["road_stats"]["missing_edges"] == 0,
                        "Historical replay used other input/roads or nonfrozen evidence")
                check_sources()
                summary["runs"].append(row)
                save(output / "validation-summary.json", summary)
                print(json.dumps({"event": "RUN_COMPLETED", "case": name,
                                  "profile": profile_name,
                                  "fee": row["audit"]["total_fee"]}), flush=True)
        control()
        check_sources()
        require(len(summary["runs"]) == 4, "Optional replay is incomplete")
        raw_results = [row["result_file"] for row in summary["runs"]]
        summary.update(
            status="COMPLETED", current=None, valid_runs=4,
            all_historical_source_rows_retained=True,
            source_hashes_unchanged=True,
            raw_result_files_recipe_only=raw_results,
            merge_extra_argv=[part for path in raw_results for part in ("--result", path)],
            global_optimality_proven=False, elapsed_seconds=round(time.monotonic() - started, 4),
            finished_at=int(time.time()),
        )
        save(output / "validation-summary.json", summary)
        return summary
    except Exception as exc:
        stop_worker(active)
        summary.update(status="FAILED_DO_NOT_AUTO_MERGE", failure_type=type(exc).__name__,
                       failure_message=str(exc), current=None,
                       elapsed_seconds=round(time.monotonic() - started, 4),
                       finished_at=int(time.time()))
        save(output / "validation-summary.json", summary)
        raise
    finally:
        stop_worker(active)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True)
    parser.add_argument("--study-summary", required=True)
    parser.add_argument("--refine-summary", required=True)
    parser.add_argument("--history-database",
                        default=str(ROOT / "data/retest-20261112/full-replay.db"))
    parser.add_argument("--output")
    parser.add_argument("--deadline-epoch", type=int, default=1794542875)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({key: result.get(key) for key in
                      ("status", "scope", "valid_runs", "external_calls",
                       "source_business_database_written", "elapsed_seconds")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
