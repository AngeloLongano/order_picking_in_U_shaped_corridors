"""Run and validate the four search configurations."""

from __future__ import annotations

from dataclasses import dataclass
from math import dist, isclose

import pandas as pd

from .basic import SearchResult, basic_search
from .instances import Instance
from .zoomin import ZoomResult, zoomin_search


@dataclass(frozen=True)
class Comparison:
    results: dict[str, SearchResult | ZoomResult]
    table: pd.DataFrame
    improvement_percent: dict[str, float | None]


def _validate(instance: Instance, result: SearchResult | ZoomResult) -> None:
    if result.best is None:
        return
    candidate = result.best
    route = candidate.route
    assert route.picker_distance is not None
    assert candidate.total_cost is not None
    assert sorted(k for tour in route.tours for k in tour) == list(
        instance.requested_locations
    )
    assert all(
        sum(instance.qty_by_location[k] for k in tour) <= instance.capacity
        for tour in route.tours
    )
    assert isclose(
        candidate.total_cost,
        route.picker_distance + instance.nu * dist((0, 0), candidate.depot),
        abs_tol=1e-8,
    )


def compare(
    instance: Instance,
    basic_step: float = 1.0,
    initial_step: float = 2.0,
    minimum_step: float = 0.5,
    reduction: float = 5,
    limit_seconds: float = 3.0,
    solver: str = "cp_sat",
) -> Comparison:
    results = {}
    for mode in ("1d", "2d"):
        results[f"Basic {mode}"] = basic_search(
            instance, basic_step, mode, limit_seconds, solver=solver
        )
        results[f"ZoomIn {mode}"] = zoomin_search(
            instance, initial_step, minimum_step, reduction, mode, limit_seconds, solver
        )
    rows = []
    for name, result in results.items():
        _validate(instance, result)
        candidate = result.best
        route = candidate.route if candidate else None
        weighted_forklift_distance = (
            instance.nu * dist((0, 0), candidate.depot) if candidate else None
        )
        rows.append(
            {
                "method": name,
                "depot": candidate.depot if candidate else None,
                "tours": len(route.tours) if route else None,
                "picker_distance_m": route.picker_distance if route else None,
                "weighted_forklift_distance_m": weighted_forklift_distance,
                "total_cost_m": candidate.total_cost if candidate else None,
                "candidates": len(result.candidates),
                "feasible": result.feasible_count,
                "optimal": result.optimal_count,
                "elapsed_s": result.elapsed_seconds,
                "limit_per_candidate_s": limit_seconds,
                "basic_step_m": basic_step if name.startswith("Basic") else None,
                "initial_step_m": initial_step if name.startswith("ZoomIn") else None,
                "minimum_step_m": minimum_step if name.startswith("ZoomIn") else None,
                "reduction": reduction if name.startswith("ZoomIn") else None,
            }
        )
    table = pd.DataFrame(rows).set_index("method")
    improvement = {}
    for method in ("Basic", "ZoomIn"):
        one, two = results[f"{method} 1d"].best, results[f"{method} 2d"].best
        improvement[method] = (
            None
            if one is None
            or two is None
            or one.total_cost is None
            or two.total_cost is None
            else 100 * (one.total_cost - two.total_cost) / one.total_cost
        )
    return Comparison(results, table, improvement)
