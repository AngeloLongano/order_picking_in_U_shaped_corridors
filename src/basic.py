"""Uniform-grid search for the movable depot."""

from __future__ import annotations

from dataclasses import dataclass
from math import dist, floor
from time import perf_counter

from .instances import Instance, Point
from .routing import RouteResult, solve_routes

Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class Candidate:
    depot: Point
    total_cost: float | None
    status: str
    route: RouteResult


@dataclass(frozen=True)
class SearchResult:
    best: Candidate | None
    candidates: tuple[Candidate, ...]
    step: float
    bounds: Bounds
    elapsed_seconds: float

    @property
    def feasible_count(self) -> int:
        return sum(c.total_cost is not None for c in self.candidates)

    @property
    def optimal_count(self) -> int:
        return sum(c.status == "OPTIMAL" for c in self.candidates)


def _axis(low: float, high: float, step: float) -> tuple[float, ...]:
    # Anchored to the lower bound. Include the upper bound only when it lies on the grid.
    count = floor((high - low) / step + 1e-9)
    return tuple(round(low + i * step, 10) for i in range(count + 1))


def grid_points(bounds: Bounds, step: float, mode: str = "2d") -> tuple[Point, ...]:
    if step <= 0 or mode not in ("1d", "2d"):
        raise ValueError("Step must be positive and mode must be '1d' or '2d'")
    xmin, xmax, ymin, ymax = bounds
    if xmin > xmax or ymin > ymax or (mode == "1d" and not ymin <= 0 <= ymax):
        raise ValueError("Invalid search bounds")
    ys = (0.0,) if mode == "1d" else _axis(ymin, ymax, step)
    return tuple((x, y) for y in ys for x in _axis(xmin, xmax, step))


def basic_search(
    instance: Instance,
    step: float,
    mode: str = "2d",
    limit_seconds: float = 3.0,
    bounds: Bounds | None = None,
    solver: str = "cp_sat",
) -> SearchResult:
    region = instance.layout.bounds if bounds is None else bounds
    lx, hx, ly, hy = instance.layout.bounds
    x0, x1, y0, y1 = region
    if x0 < lx - 1e-9 or x1 > hx + 1e-9 or y0 < ly - 1e-9 or y1 > hy + 1e-9:
        raise ValueError("Search bounds exceed the feasible area")
    start = perf_counter()
    candidates = []
    best = None
    best_total_cost: float | None = None
    for point in grid_points(region, step, mode):
        route = solve_routes(instance, point, limit_seconds, solver)
        total_cost = (
            None
            if route.picker_distance is None
            else route.picker_distance + instance.nu * dist((0, 0), point)
        )
        candidate = Candidate(point, total_cost, route.status, route)
        candidates.append(candidate)
        if total_cost is not None and (
            best_total_cost is None or total_cost < best_total_cost - 1e-9
        ):
            best = candidate
            best_total_cost = total_cost
    return SearchResult(best, tuple(candidates), step, region, perf_counter() - start)
