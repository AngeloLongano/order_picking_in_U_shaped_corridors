"""Reproducible, resumable fixed-depot solver campaigns.

The only positional CLI argument is ``preview`` or ``run``. Configuration lives
in benchmarks/scenari.yaml and benchmarks/opzioni.yaml.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import platform
import resource
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from math import dist
from pathlib import Path
from time import perf_counter
from typing import Any

import yaml

from .basic import grid_points
from .instances import DEFAULT_DATA, Instance, load_instance

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "benchmarks"
RESULTS_DIR = ROOT / "results"
SCENARIOS_FILE = CONFIG_DIR / "scenari.yaml"
OPTIONS_FILE = CONFIG_DIR / "opzioni.yaml"
INITIAL_ESTIMATES = CONFIG_DIR / "stime_init.json"
ESTIMATES = CONFIG_DIR / "stime.json"
CANDIDATE_COLUMNS = (
    "run_id",
    "instance",
    "layout",
    "picks",
    "solver",
    "method",
    "dimension",
    "scan",
    "candidate_index",
    "depot_x",
    "depot_y",
    "status",
    "raw_status",
    "stop_reason",
    "objective_picker_m",
    "best_bound_m",
    "gap",
    "picker_distance_m",
    "weighted_forklift_m",
    "total_cost_m",
    "tours",
    "solver_parameters",
    "build_time_s",
    "solver_time_s",
    "candidate_time_s",
    "attempt",
    "error",
)
SUMMARY_COLUMNS = (
    "run_id",
    "instance",
    "layout",
    "picks",
    "solver",
    "method",
    "dimension",
    "job_status",
    "zoom_stop_reason",
    "candidates_expected",
    "candidates_executed",
    "optimal_count",
    "feasible_count",
    "timeout_count",
    "error_count",
    "best_depot_x",
    "best_depot_y",
    "best_total_cost_m",
    "duration_s",
    "peak_rss_mb",
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dataset_names() -> list[str]:
    with DEFAULT_DATA.open(newline="", encoding="utf-8") as source:
        names = [row["label"] for row in csv.DictReader(source, delimiter="\t")]
    if len(names) != len(set(names)) or not names:
        raise ValueError("Dataset contains missing or duplicate instance names")
    return names


def _load_config() -> dict[str, Any]:
    scenarios = yaml.safe_load(SCENARIOS_FILE.read_text(encoding="utf-8"))
    options = yaml.safe_load(OPTIONS_FILE.read_text(encoding="utf-8"))
    if scenarios.get("schema_version") != 1 or options.get("schema_version") != 1:
        raise ValueError("Unsupported YAML schema version")
    if set(scenarios["scenarios"]) != set(options["scenarios"]):
        raise ValueError("Scenario keys must match between YAML files")
    active = scenarios["active"]
    if active not in scenarios["scenarios"]:
        raise ValueError(f"Unknown active scenario: {active}")
    names = _dataset_names()
    selection = scenarios["scenarios"][active]["instances"]
    if selection == "all":
        selected = names
    elif isinstance(selection, list):
        selected = [
            item["name"] if isinstance(item, dict) else item for item in selection
        ]
    else:
        raise ValueError("Scenario instances must be 'all' or an explicit list")
    if (
        not selected
        or len(selected) != len(set(selected))
        or set(selected) - set(names)
    ):
        raise ValueError("Scenario has missing, duplicate, or unknown instances")
    common = options["common"] | options["scenarios"][active]
    search = options["search"] | options["scenarios"][active].get("search", {})
    if not set(common["solvers"]) <= {"cp_sat", "gurobi"} or not common["solvers"]:
        raise ValueError("Unsupported or empty solver list")
    if not common["methods"] or any(
        method not in {"Basic", "ZoomIn"} or not dims or not set(dims) <= {"1d", "2d"}
        for method, dims in common["methods"].items()
    ):
        raise ValueError("Invalid methods or dimensions")
    if (
        common["capacity"] <= 0
        or common["forklift_weight"] < 0
        or common["limit_per_depot_s"] <= 0
        or common["objective_scale"] <= 0
        or common["solver_threads"] < 1
        or common["processes"] < 1
    ):
        raise ValueError("Invalid common numeric option")
    if (
        search["basic_step_m"] <= 0
        or search["zoom_initial_step_m"] <= 0
        or search["zoom_minimum_step_m"] <= 0
        or search["zoom_reduction"] <= 1
    ):
        raise ValueError("Invalid search grid")
    estimates = common["estimates"]
    if (
        estimates["update"] not in {"average", "replace"}
        or estimates["reset_generation"] < 0
    ):
        raise ValueError("Invalid estimates policy")
    for name in selected:
        load_instance(name, capacity=common["capacity"], nu=common["forklift_weight"])
    return {"active": active, "instances": selected, "common": common, "search": search}


def _profile(config: dict[str, Any]) -> dict[str, Any]:
    common, search = config["common"], config["search"]
    return {
        "limit_per_depot_s": common["limit_per_depot_s"],
        "basic_step_m": search["basic_step_m"],
        "zoom_initial_step_m": search["zoom_initial_step_m"],
        "zoom_minimum_step_m": search["zoom_minimum_step_m"],
        "zoom_reduction": search["zoom_reduction"],
        "objective_scale": common["objective_scale"],
        "objective_profile": "rounded",
        "solver_threads": common["solver_threads"],
        "processes": common["processes"],
    }


def _estimate_key(job: tuple[str, str, str, str], profile: dict[str, Any]) -> str:
    return "|".join((*job, _hash(profile)[:16]))


def _jobs(config: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    return [
        (name, solver, method, dimension)
        for name in config["instances"]
        for method, dims in config["common"]["methods"].items()
        for dimension in dims
        for solver in config["common"]["solvers"]
    ]


def _run_identity(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    code = {
        p.relative_to(ROOT).as_posix(): _file_hash(p)
        for p in sorted((ROOT / "src").glob("*.py"))
    }
    identity = {
        "active": config["active"],
        "instances": config["instances"],
        "common": config["common"],
        "search": config["search"],
        "dataset_sha256": _file_hash(DEFAULT_DATA),
        "code_sha256": _hash(code),
    }
    return _hash(identity)[:16], identity


def _checkpoint_path(
    run_dir: Path, job: tuple[str, str, str, str], scan: int, index: int
) -> Path:
    return run_dir / "checkpoints" / _hash(job)[:16] / f"{scan:02d}-{index:05d}.json"


def _done_path(run_dir: Path, job: tuple[str, str, str, str]) -> Path:
    return run_dir / "checkpoints" / _hash(job)[:16] / "done.json"


def _candidate_count(
    instance: Instance, method: str, dimension: str, search: dict[str, Any]
) -> tuple[int, int]:
    bounds = instance.layout.bounds
    if method == "Basic":
        n = len(grid_points(bounds, search["basic_step_m"], dimension))
        return n, n
    step = search["zoom_initial_step_m"]
    first = grid_points(bounds, step, dimension)
    next_step = step / search["zoom_reduction"]
    if next_step < search["zoom_minimum_step_m"] - 1e-12:
        return len(first), len(first)
    counts = []
    for x, y in first:
        region = (
            max(bounds[0], x - step),
            min(bounds[1], x + step),
            max(bounds[2], y - step),
            min(bounds[3], y + step),
        )
        counts.append(len(grid_points(region, next_step, dimension)))
    return len(first) + min(counts), len(first) + max(counts)


def _estimates_for_preview(config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    init = _read_json(INITIAL_ESTIMATES)
    current = _read_json(ESTIMATES) if ESTIMATES.exists() else init
    generation = config["common"]["estimates"]["reset_generation"]
    if current.get("reset_generation", 0) != generation:
        return init, f"reset pending: generation {generation}"
    return current, "current estimates"


def _estimate(
    job: tuple[str, str, str, str], profile: dict[str, Any], data: dict[str, Any]
) -> tuple[float, str, str]:
    entries = data["estimates"]
    entry = entries.get(_estimate_key(job, profile))
    transferred = False
    if entry is None:
        prefix = "|".join(job) + "|"
        matches = [v for k, v in entries.items() if k.startswith(prefix)]
        if not matches:
            raise ValueError(f"No initial estimate for {job}")
        entry = min(
            matches,
            key=lambda v: abs(
                v["profile"]["limit_per_depot_s"] - profile["limit_per_depot_s"]
            ),
        )
        transferred = True
    source = "local" if entry.get("observations", 0) else entry["source_type"]
    if transferred:
        source = "transferred " + source
    return float(entry["seconds"]), source, entry.get("updated_at") or "never"


def _format_duration(seconds: float) -> str:
    tenths = round(seconds * 10)
    minutes, remaining = divmod(tenths, 600)
    return f"{minutes} min {remaining / 10:.1f} s"


def preview(config: dict[str, Any]) -> None:
    run_id, _ = _run_identity(config)
    run_dir = RESULTS_DIR / run_id
    profile = _profile(config)
    estimates, note = _estimates_for_preview(config)
    pending: list[float] = []
    complete = errors = lower = upper = 0
    sources: dict[str, int] = {}
    last_observation: str | None = None
    for job in _jobs(config):
        if _done_path(run_dir, job).exists():
            complete += 1
            continue
        if (run_dir / "attempts" / f"{_hash(job)[:16]}.json").exists():
            errors += 1
        instance = load_instance(
            job[0],
            capacity=config["common"]["capacity"],
            nu=config["common"]["forklift_weight"],
        )
        lo, hi = _candidate_count(instance, job[2], job[3], config["search"])
        lower += lo
        upper += hi
        seconds, source, observed = _estimate(job, profile, estimates)
        pending.append(seconds)
        sources[source] = sources.get(source, 0) + 1
        if observed != "never" and (
            last_observation is None or observed > last_observation
        ):
            last_observation = observed
    processes = config["common"]["processes"]
    slots = [0.0] * processes
    for seconds in sorted(pending, reverse=True):
        index = min(range(processes), key=slots.__getitem__)
        slots[index] += seconds
    overhead = 0.0
    config_file = run_dir / "config.json"
    if config_file.exists():
        overhead = float(_read_json(config_file).get("measured_overhead_s_per_job", 0))
    print(f"Scenario: {config['active']}  series: {run_id}  ({note})")
    print(
        f"Jobs: {len(_jobs(config))}; complete: {complete}; missing: {len(pending)}; retry errors: {errors}"
    )
    print(
        f"Candidates pending: {lower}-{upper}"
        if lower != upper
        else f"Candidates pending: {lower}"
    )
    parallel = max(slots, default=0) + overhead * len(pending) / processes
    print(
        f"Estimated sequential: {_format_duration(sum(pending))}; "
        f"parallel ({processes} processes): {_format_duration(parallel)}"
    )
    print(f"Sources: {sources}; last local observation: {last_observation or 'none'}")


def _candidate_row(
    instance: Instance,
    job: tuple[str, str, str, str],
    run_id: str,
    scan: int,
    index: int,
    point: tuple[float, float],
    config: dict[str, Any],
    attempt: int,
) -> dict[str, Any]:
    from .routing import solve_routes

    start = perf_counter()
    route = solve_routes(
        instance,
        point,
        config["common"]["limit_per_depot_s"],
        job[1],
        objective_profile="rounded",
        objective_scale=config["common"]["objective_scale"],
        solver_threads=config["common"]["solver_threads"],
        seed=config["common"]["seed"],
    )
    elapsed = perf_counter() - start
    if route.picker_distance is not None:
        weighted = instance.nu * dist((0, 0), point)
        total = route.picker_distance + weighted
    else:
        weighted = total = None
    return dict(
        zip(
            CANDIDATE_COLUMNS,
            (
                run_id,
                instance.name,
                f"{instance.layout.n}x{instance.layout.m}",
                len(instance.requested_locations),
                job[1],
                job[2],
                job[3],
                scan,
                index,
                point[0],
                point[1],
                route.status,
                route.raw_status,
                route.stop_reason,
                route.objective_value,
                route.best_bound,
                route.gap,
                route.picker_distance,
                weighted,
                total,
                json.dumps(route.tours),
                json.dumps(route.parameters, sort_keys=True),
                route.build_seconds,
                route.solve_seconds,
                elapsed,
                attempt,
                "",
            ),
        )
    )


def _worker(
    run_dir_string: str,
    job: tuple[str, str, str, str],
    config: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    run_dir = Path(run_dir_string)
    instance = load_instance(
        job[0],
        capacity=config["common"]["capacity"],
        nu=config["common"]["forklift_weight"],
    )
    bounds = instance.layout.bounds
    region = bounds
    step = (
        config["search"]["basic_step_m"]
        if job[2] == "Basic"
        else config["search"]["zoom_initial_step_m"]
    )
    minimum = step if job[2] == "Basic" else config["search"]["zoom_minimum_step_m"]
    reduction = config["search"]["zoom_reduction"]
    rows: list[dict[str, Any]] = []
    scan = 0
    stop_reason = "minimum_step"
    begin = perf_counter()
    while step >= minimum - 1e-12:
        points = grid_points(region, step, job[3])
        scan_rows = []
        for index, point in enumerate(points):
            checkpoint = _checkpoint_path(run_dir, job, scan, index)
            previous = None
            if checkpoint.exists():
                previous = _read_json(checkpoint)
                if (previous["depot_x"], previous["depot_y"]) != point:
                    raise ValueError(f"Checkpoint grid mismatch: {checkpoint}")
            if previous is not None and previous["status"] != "ERROR":
                row = previous
            else:
                attempt = previous["attempt"] + 1 if previous else 1
                try:
                    row = _candidate_row(
                        instance, job, run_id, scan, index, point, config, attempt
                    )
                except Exception as error:
                    row = dict.fromkeys(CANDIDATE_COLUMNS)
                    row.update(
                        {
                            "run_id": run_id,
                            "instance": instance.name,
                            "layout": f"{instance.layout.n}x{instance.layout.m}",
                            "picks": len(instance.requested_locations),
                            "solver": job[1],
                            "method": job[2],
                            "dimension": job[3],
                            "scan": scan,
                            "candidate_index": index,
                            "depot_x": point[0],
                            "depot_y": point[1],
                            "status": "ERROR",
                            "raw_status": type(error).__name__,
                            "stop_reason": "EXCEPTION",
                            "attempt": attempt,
                            "error": str(error),
                        }
                    )
                    _atomic_json(checkpoint, row)
                    raise
                _atomic_json(checkpoint, row)
            scan_rows.append(row)
            rows.append(row)
        feasible = [r for r in scan_rows if r["total_cost_m"] is not None]
        if not feasible:
            stop_reason = "no_feasible_solution"
            break
        best = min(feasible, key=lambda r: (r["total_cost_m"], r["candidate_index"]))
        x, y = best["depot_x"], best["depot_y"]
        region = (
            max(bounds[0], x - step),
            min(bounds[1], x + step),
            max(bounds[2], y - step),
            min(bounds[3], y + step),
        )
        step /= reduction
        scan += 1
    feasible_all = [r for r in rows if r["total_cost_m"] is not None]
    best_all = (
        min(
            feasible_all,
            key=lambda r: (r["total_cost_m"], r["scan"], r["candidate_index"]),
        )
        if feasible_all
        else None
    )
    summary: dict[str, Any] = dict(
        zip(
            SUMMARY_COLUMNS,
            (
                run_id,
                instance.name,
                f"{instance.layout.n}x{instance.layout.m}",
                len(instance.requested_locations),
                job[1],
                job[2],
                job[3],
                "COMPLETE",
                stop_reason,
                len(rows),
                len(rows),
                sum(r["status"] == "OPTIMAL" for r in rows),
                sum(r["status"] == "FEASIBLE" for r in rows),
                sum(r["status"] == "TIME_LIMIT_NO_SOLUTION" for r in rows),
                sum(r["status"] == "ERROR" for r in rows),
                best_all["depot_x"] if best_all else None,
                best_all["depot_y"] if best_all else None,
                best_all["total_cost_m"] if best_all else None,
                perf_counter() - begin,
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                / (1024 * 1024 if sys.platform == "darwin" else 1024),
            ),
        )
    )
    _atomic_json(_done_path(run_dir, job), summary)
    return summary


def _csv_atomic(
    path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]
) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _export(run_dir: Path, jobs: list[tuple[str, str, str, str]]) -> None:
    candidates: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for job in jobs:
        directory = _done_path(run_dir, job).parent
        if directory.exists():
            candidates.extend(
                _read_json(path)
                for path in sorted(directory.glob("[0-9]*-[0-9]*.json"))
            )
        done = _done_path(run_dir, job)
        if done.exists():
            summaries.append(_read_json(done))
    _csv_atomic(run_dir / "candidati.csv", CANDIDATE_COLUMNS, candidates)
    _csv_atomic(run_dir / "riepilogo.csv", SUMMARY_COLUMNS, summaries)


def _update_estimate(
    job: tuple[str, str, str, str], duration: float, config: dict[str, Any]
) -> None:
    data = _read_json(ESTIMATES)
    key = _estimate_key(job, _profile(config))
    entry = data["estimates"].get(key)
    if entry is None:
        prefix = "|".join(job) + "|"
        matches = [
            value
            for item_key, value in data["estimates"].items()
            if item_key.startswith(prefix)
        ]
        if not matches:
            raise ValueError(f"No estimate entry for {key}")
        entry = copy.deepcopy(
            min(
                matches,
                key=lambda value: abs(
                    value["profile"]["limit_per_depot_s"]
                    - _profile(config)["limit_per_depot_s"]
                ),
            )
        )
        entry["profile"] = _profile(config)
        entry["observations"] = 0
        entry["updated_at"] = None
        entry["source_type"] = "paper_extrapolated"
        entry["derivation"] = (
            f"Transferred from another profile for {job}; replaced by first local observation"
        )
        data["estimates"][key] = entry
    before = entry["seconds"]
    policy = config["common"]["estimates"]["update"]
    entry["seconds"] = (
        (before + duration) / 2
        if entry.get("observations", 0) and policy == "average"
        else duration
    )
    entry["observations"] = entry.get("observations", 0) + 1
    entry["updated_at"] = datetime.now(UTC).isoformat()
    entry["source_type"] = "local"
    data.setdefault("history", []).append(
        {
            "key": key,
            "before_seconds": before,
            "observed_seconds": duration,
            "after_seconds": entry["seconds"],
            "at": entry["updated_at"],
        }
    )
    _atomic_json(ESTIMATES, data)


def run(config: dict[str, Any]) -> None:
    run_id, identity = _run_identity(config)
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    import gurobipy
    import ortools

    config_file = run_dir / "config.json"
    if not config_file.exists():
        _atomic_json(
            config_file,
            identity
            | {
                "run_id": run_id,
                "created_at": datetime.now(UTC).isoformat(),
                "python": sys.version,
                "platform": platform.platform(),
                "gurobi": ".".join(map(str, gurobipy.gurobi.version())),
                "ortools": ortools.__version__,
                "profile": _profile(config),
            },
        )
    estimates = config["common"]["estimates"]
    generation = estimates["reset_generation"]
    if (
        not ESTIMATES.exists()
        or _read_json(ESTIMATES).get("reset_generation") != generation
    ):
        initial = _read_json(INITIAL_ESTIMATES)
        initial["reset_generation"] = generation
        _atomic_json(ESTIMATES, initial)
    jobs = _jobs(config)
    pending = [job for job in jobs if not _done_path(run_dir, job).exists()]
    failed: list[tuple[str, str, str, str]] = []
    print(f"Series {run_id}: {len(pending)} pending of {len(jobs)} jobs")
    start = perf_counter()
    try:
        with ProcessPoolExecutor(max_workers=config["common"]["processes"]) as pool:
            futures = {
                pool.submit(_worker, str(run_dir), job, config, run_id): job
                for job in pending
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    summary = future.result()
                    _update_estimate(job, summary["duration_s"], config)
                    print(
                        f"Completed {job}: {summary['candidates_executed']} candidates"
                    )
                except Exception:  # noqa: BLE001 - record each failed worker and continue
                    failed.append(job)
                    error = traceback.format_exc()
                    path = run_dir / "attempts" / f"{_hash(job)[:16]}.json"
                    attempts = (
                        _read_json(path)
                        if path.exists()
                        else {"job": job, "errors": []}
                    )
                    attempts["errors"].append(
                        {"at": datetime.now(UTC).isoformat(), "traceback": error}
                    )
                    _atomic_json(path, attempts)
                    print(f"Error {job}: {error.splitlines()[-1]}", file=sys.stderr)
                _export(run_dir, jobs)
    finally:
        _export(run_dir, jobs)
        metadata = _read_json(config_file)
        metadata["last_run_elapsed_s"] = perf_counter() - start
        metadata["total_wall_seconds"] = (
            metadata.get("total_wall_seconds", 0.0) + metadata["last_run_elapsed_s"]
        )
        if all(_done_path(run_dir, job).exists() for job in jobs):
            metadata["finished_at"] = datetime.now(UTC).isoformat()
        metadata["measured_overhead_s_per_job"] = max(
            0.0,
            metadata["last_run_elapsed_s"]
            - sum(
                _read_json(_done_path(run_dir, job))["duration_s"]
                for job in pending
                if _done_path(run_dir, job).exists()
            ),
        ) / max(1, len(pending))
        _atomic_json(config_file, metadata)
    if failed:
        raise RuntimeError(
            f"{len(failed)} benchmark jobs failed; inspect {run_dir / 'attempts'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "run"))
    args = parser.parse_args()
    config = _load_config()
    if args.action == "preview":
        preview(config)
    else:
        run(config)


if __name__ == "__main__":
    main()
