"""Iterative grid refinement around the best depot in each scan."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from .basic import Candidate, SearchResult, basic_search
from .instances import Instance


@dataclass(frozen=True)
class ZoomResult:
    best: Candidate | None
    scans: tuple[SearchResult, ...]
    stop_reason: str
    elapsed_seconds: float

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return tuple(c for scan in self.scans for c in scan.candidates)

    @property
    def feasible_count(self) -> int:
        return sum(scan.feasible_count for scan in self.scans)

    @property
    def optimal_count(self) -> int:
        return sum(scan.optimal_count for scan in self.scans)


def zoomin_search(
    instance: Instance,
    initial_step: float,
    minimum_step: float,
    reduction: float = 5,
    mode: str = "2d",
    limit_seconds: float = 3.0,
    solver: str = "cp_sat",
) -> ZoomResult:
    if initial_step <= 0 or minimum_step <= 0 or reduction <= 1:
        raise ValueError("Steps must be positive and reduction greater than one")
    region = instance.layout.bounds
    overall = region
    step = initial_step
    scans = []
    best = None
    best_total_cost: float | None = None
    start = perf_counter()
    reason = "minimum_step"
    while step >= minimum_step - 1e-12:
        scan = basic_search(instance, step, mode, limit_seconds, region, solver)
        scans.append(scan)
        if scan.best is None:
            reason = "no_feasible_solution"
            break
        scan_total_cost = scan.best.total_cost
        if scan_total_cost is not None and (
            best_total_cost is None or scan_total_cost < best_total_cost - 1e-9
        ):
            best = scan.best
            best_total_cost = scan_total_cost
        x, y = scan.best.depot
        xmin, xmax, ymin, ymax = overall
        region = (
            max(xmin, x - step),
            min(xmax, x + step),
            max(ymin, y - step),
            min(ymax, y + step),
        )
        step /= reduction
    return ZoomResult(best, tuple(scans), reason, perf_counter() - start)
