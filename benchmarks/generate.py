"""Rebuild the deterministic pilot selection and preview priors.

Run from the project root with ``uv run python benchmarks/generate.py``.
The generated values are planning assumptions, never solver measurements.
The current estimates file is created only when absent, so regeneration cannot
erase observations from an existing campaign.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import yaml

from src.basic import grid_points
from src.instances import DEFAULT_DATA, Instance, load_instance

HERE = Path(__file__).resolve().parent
PAPER = "Montemanni, Cervino & Lolli (2024), Picking Optimization in U-Shaped Corridors with a Movable Depot"
PAPER_URL = (
    "https://iris.unimore.it/retrieve/4c9fefc1-5590-4b12-993e-d1a9ad5e77d7/Final.pdf"
)

# Median elapsed seconds transcribed from paper tables 1-2. They include the
# complete search on different hardware, software, grids and time limits.
PAPER_ANCHORS = {
    10: {"Basic_1d": 3.3, "ZoomIn_1d": 5.75, "ZoomIn_2d": 3.35},
    15: {"Basic_1d": 36.45, "ZoomIn_1d": 121.5, "ZoomIn_2d": 292.8},
    30: {"Basic_1d": 83.45, "ZoomIn_1d": 163.8, "ZoomIn_2d": 542.4},
    60: {"Basic_1d": 87.7, "ZoomIn_1d": 172.05, "ZoomIn_2d": 547.75},
}


def names() -> list[str]:
    with DEFAULT_DATA.open(newline="", encoding="utf-8") as source:
        return [row["label"] for row in csv.DictReader(source, delimiter="\t")]


def indicators(instance: Instance) -> dict[str, float | int]:
    points = [instance.layout.point(k) for k in instance.requested_locations]
    centroid = tuple(sum(p[i] for p in points) / len(points) for i in (0, 1))
    dispersion = sum(math.dist(p, centroid) for p in points) / len(points)
    demand = sum(instance.qty_by_location.values())
    return {
        "total_demand": demand,
        "minimum_tours": math.ceil(demand / instance.capacity),
        "spatial_dispersion_m": round(dispersion, 6),
    }


def _score(
    instance: Instance, metrics: dict, bounds: dict, keys: tuple[str, ...]
) -> float:
    values = metrics[instance.name]
    return sum(
        (values[key] - bounds[key][0]) / (bounds[key][1] - bounds[key][0])
        if bounds[key][1] > bounds[key][0]
        else 0.0
        for key in keys
    ) / len(keys)


def select_pilot(instances: list[Instance]) -> list[dict]:
    groups: dict[tuple[int, int, int], list[Instance]] = defaultdict(list)
    for instance in instances:
        groups[
            (instance.layout.n, instance.layout.m, len(instance.requested_locations))
        ].append(instance)
    if len(groups) != 12 or any(len(group) != 10 for group in groups.values()):
        raise ValueError("Expected 12 dataset groups of ten instances")
    selected = []
    for group_key, group in sorted(groups.items()):
        metrics = {instance.name: indicators(instance) for instance in group}
        keys = ("total_demand", "minimum_tours", "spatial_dispersion_m")
        bounds = {
            key: (
                min(m[key] for m in metrics.values()),
                max(m[key] for m in metrics.values()),
            )
            for key in keys
        }

        ranked = sorted(
            group,
            key=lambda instance: (
                _score(instance, metrics, bounds, keys),
                instance.name,
            ),
        )
        # Fixed representatives from low [0..2], middle [3..6], high [7..9].
        for rank, stratum in ((1, "facile"), (4, "media"), (8, "difficile")):
            instance = ranked[rank]
            selected.append(
                {
                    "name": instance.name,
                    "stratum": stratum,
                    "indicators": {
                        **metrics[instance.name],
                        "normalized_score": round(
                            _score(instance, metrics, bounds, keys), 6
                        ),
                        "rank_in_group": rank + 1,
                    },
                }
            )
    return selected


def profile(options: dict, scenario: str) -> dict:
    common = {**options["common"], **options["scenarios"][scenario]}
    search = {**options["search"], **options["scenarios"][scenario].get("search", {})}
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


def candidate_range(
    instance: Instance, method: str, mode: str, p: dict
) -> tuple[int, int]:
    bounds = instance.layout.bounds
    if method == "Basic":
        count = len(grid_points(bounds, p["basic_step_m"], mode))
        return count, count
    first = len(grid_points(bounds, p["zoom_initial_step_m"], mode))
    # Upper bound for second scan clipped to the original bounds. A failed
    # first scan has no second scan; the lower bound is therefore first only.
    step = p["zoom_initial_step_m"] / p["zoom_reduction"]
    if step < p["zoom_minimum_step_m"] - 1e-12:
        return first, first
    width = min(bounds[1] - bounds[0], 2 * p["zoom_initial_step_m"])
    height = min(bounds[3] - bounds[2], 2 * p["zoom_initial_step_m"])
    second = (math.floor(width / step + 1e-9) + 1) * (
        1 if mode == "1d" else math.floor(height / step + 1e-9) + 1
    )
    return first, first + second


def paper_candidate_count(instance: Instance, anchor: str) -> int:
    bounds = instance.layout.bounds
    if anchor == "Basic_1d":
        return len(grid_points(bounds, 1.0, "1d"))
    if anchor == "ZoomIn_1d":
        first = len(grid_points(bounds, 1.25, "1d"))
        return first + 4 * 11  # approximate four refinement scans
    first = len(grid_points(bounds, 2.0, "2d"))
    return first + 81  # approximate second 0.5 m scan


def prior(
    instance: Instance,
    solver: str,
    method: str,
    mode: str,
    p: dict,
    all_instances: list[Instance],
) -> dict:
    picks = len(instance.requested_locations)
    anchor = f"{method}_{mode}"
    transferred_method = False
    if anchor == "Basic_2d":
        anchor = "ZoomIn_2d"
        transferred_method = True
    reference_layout = (9, 4) if picks in (10, 15) else (20, 4)
    reference_instance = next(
        i
        for i in all_instances
        if (i.layout.n, i.layout.m, len(i.requested_locations))
        == (*reference_layout, picks)
    )
    count = paper_candidate_count(reference_instance, anchor)
    per_candidate = min(PAPER_ANCHORS[picks][anchor] / count, p["limit_per_depot_s"])
    low, high = candidate_range(instance, method, mode, p)
    estimate = per_candidate * ((low + high) / 2)
    return {
        "seconds": round(max(0.001, estimate), 6),
        "source_type": "paper_extrapolated",
        "source_detail": "CP-SAT result transferred to Gurobi"
        if solver == "gurobi"
        else "CP-SAT paper result adapted to current configuration",
        "reference": PAPER,
        "reference_url": PAPER_URL,
        "location": "Table 2, p. 143" if anchor == "ZoomIn_2d" else "Table 1, p. 142",
        "original_parameters": {
            "solver": "CP-SAT 9.6",
            "hardware": "Intel Core i7-12700F, 32 GB RAM",
            "layout": f"{reference_layout[0]}x{reference_layout[1]}",
            "picks": picks,
            "method": anchor,
            "limit_per_candidate_s": 20 if anchor == "ZoomIn_2d" else 3,
            "grid": "sM=2, sm=0.5, es=5"
            if anchor == "ZoomIn_2d"
            else "sM=1.25, sm=0.01, es=5"
            if anchor == "ZoomIn_1d"
            else "s=1",
        },
        "derivation": f"median paper elapsed {PAPER_ANCHORS[picks][anchor]} s / approximate {count} paper candidates; cap at configured {p['limit_per_depot_s']} s per candidate; multiply by midpoint of {low}..{high} current candidates"
        + ("; transfer ZoomIn 2D rate to Basic 2D" if transferred_method else "")
        + ("; transfer CP-SAT rate to Gurobi" if solver == "gurobi" else ""),
        "profile": p,
        "observations": 0,
        "updated_at": None,
    }


def main() -> None:
    all_instances = [load_instance(name) for name in names()]
    scenarios = {
        "schema_version": 1,
        "active": "pilota",
        "selection": {
            "score": "mean of within-group min-max normalized total demand, minimum tours and mean Euclidean distance to pick centroid; ties by instance name",
            "representative_ranks_one_based": {"facile": 2, "media": 5, "difficile": 9},
        },
        "scenarios": {
            "tecnica": {"instances": [{"name": "44-(9x4)-10-01"}]},
            "pilota": {"instances": select_pilot(all_instances)},
            "basic_2d": {"instances": "all"},
            "completo": {"instances": "all"},
        },
    }
    options = {
        "schema_version": 1,
        "common": {
            "solvers": ["cp_sat", "gurobi"],
            "capacity": 15,
            "forklift_weight": 1 / 3,
            "limit_per_depot_s": 0.5,
            "objective_scale": 10000,
            "solver_threads": 1,
            "seed": 0,
            "estimates": {"update": "average", "reset_generation": 0},
        },
        "scenarios": {
            "tecnica": {"methods": {"Basic": ["1d"]}, "processes": 1},
            "pilota": {
                "methods": {"Basic": ["1d", "2d"], "ZoomIn": ["1d", "2d"]},
                "processes": 2,
            },
            "basic_2d": {"methods": {"Basic": ["2d"]}, "processes": 2},
            "completo": {
                "methods": {"Basic": ["1d", "2d"], "ZoomIn": ["1d", "2d"]},
                "processes": 2,
            },
        },
        "search": {
            "basic_step_m": 1.0,
            "zoom_initial_step_m": 2.0,
            "zoom_minimum_step_m": 0.4,
            "zoom_reduction": 5,
        },
        "second_limit_policy": {
            "trigger": "Decide and record before full runs if pilot shows substantial or solver-asymmetric FEASIBLE / TIME_LIMIT_NO_SOLUTION rates",
            "proposed_limit_per_depot_s": 3.0,
            "enabled": False,
        },
    }
    (HERE / "scenari.yaml").write_text(
        yaml.safe_dump(scenarios, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    (HERE / "opzioni.yaml").write_text(
        yaml.safe_dump(options, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    entries = {}
    for scenario in ("tecnica", "pilota", "basic_2d", "completo"):
        p = profile(options, scenario)
        digest = hashlib.sha256(
            json.dumps(p, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        chosen = (
            all_instances
            if scenarios["scenarios"][scenario]["instances"] == "all"
            else [
                load_instance(item["name"])
                for item in scenarios["scenarios"][scenario]["instances"]
            ]
        )
        for instance in chosen:
            for solver in options["common"]["solvers"]:
                for method, modes in options["scenarios"][scenario]["methods"].items():
                    for mode in modes:
                        key = f"{instance.name}|{solver}|{method}|{mode}|{digest}"
                        entries[key] = prior(
                            instance, solver, method, mode, p, all_instances
                        )
    initial = {
        "schema_version": 1,
        "reset_generation": 0,
        "estimates": dict(sorted(entries.items())),
        "history": [],
    }
    payload = json.dumps(initial, ensure_ascii=False, indent=2) + "\n"
    (HERE / "stime_init.json").write_text(payload, encoding="utf-8")
    current = HERE / "stime.json"
    if not current.exists():
        current.write_text(payload, encoding="utf-8")
    print(f"Selected 36 pilot instances; wrote {len(entries)} priors")


if __name__ == "__main__":
    main()
