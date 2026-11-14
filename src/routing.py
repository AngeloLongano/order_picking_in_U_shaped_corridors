"""Capacitated multiple-tour routing at a fixed depot."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import dist, isclose
from time import perf_counter

from ortools.sat.python import cp_model

from .instances import Instance, Point

DISTANCE_SCALE = 10_000


@dataclass(frozen=True)
class RouteResult:
    depot: Point
    tours: tuple[tuple[int, ...], ...]  # depot is implicit at both ends
    tour_loads: tuple[int, ...]
    picker_distance: float | None
    status: str
    elapsed_seconds: float
    objective_value: float | None = None
    best_bound: float | None = None
    gap: float | None = None
    raw_status: str = ""
    stop_reason: str | None = None
    build_seconds: float = 0.0
    solve_seconds: float = 0.0
    parameters: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def feasible(self) -> bool:
        return (
            self.status in ("OPTIMAL", "FEASIBLE") and self.picker_distance is not None
        )


def validate_routes(
    instance: Instance,
    depot: Point,
    tours: tuple[tuple[int, ...], ...],
    tour_loads: tuple[int, ...],
    picker_distance: float,
    objective_value: float,
    objective_profile: str = "rounded",
    objective_scale: int = DISTANCE_SCALE,
) -> None:
    """Check each saved incumbent against requests, capacity and arc costs."""
    if len(tours) != len(tour_loads):
        raise ValueError("Tour/load count mismatch")
    visited = [location for tour in tours for location in tour]
    if sorted(visited) != list(instance.requested_locations) or any(
        not t for t in tours
    ):
        raise ValueError("Tours do not visit every requested location exactly once")
    if any(
        load != sum(instance.qty_by_location[k] for k in tour)
        or load > instance.capacity
        for tour, load in zip(tours, tour_loads)
    ):
        raise ValueError("Invalid tour load")
    arcs = [
        (a, b)
        for tour in tours
        for a, b in zip(
            (depot, *(instance.layout.point(k) for k in tour)),
            (*(instance.layout.point(k) for k in tour), depot),
        )
    ]
    euclidean = sum(dist(a, b) for a, b in arcs)
    modeled = (
        sum(round(dist(a, b) * objective_scale) for a, b in arcs) / objective_scale
        if objective_profile == "rounded"
        else euclidean
    )
    if not isclose(picker_distance, euclidean, rel_tol=0, abs_tol=1e-6):
        raise ValueError("Picker distance disagrees with selected arcs")
    tolerance = 1 / objective_scale if objective_profile == "rounded" else 1e-6
    if not isclose(objective_value, modeled, rel_tol=0, abs_tol=tolerance):
        raise ValueError("Objective disagrees with selected arcs")


def solve_routes(
    instance: Instance,
    depot: Point,
    limit_seconds: float = 3.0,
    solver: str = "cp_sat",
    *,
    objective_profile: str = "rounded",
    objective_scale: int = DISTANCE_SCALE,
    solver_threads: int = 1,
    seed: int = 0,
) -> RouteResult:
    if objective_profile not in ("rounded", "euclidean"):
        raise ValueError("Unknown objective profile")
    if objective_scale <= 0 or solver_threads <= 0:
        raise ValueError("Objective scale and solver threads must be positive")
    if solver == "gurobi":
        from .gurobi_routing import solve_routes_gurobi

        return solve_routes_gurobi(
            instance,
            depot,
            limit_seconds,
            objective_profile=objective_profile,
            objective_scale=objective_scale,
            solver_threads=solver_threads,
            seed=seed,
        )
    if solver != "cp_sat":
        raise ValueError("Solver must be 'cp_sat' or 'gurobi'")
    if objective_profile != "rounded":
        raise ValueError("CP-SAT requires the rounded objective")
    if not instance.layout.contains(depot):
        raise ValueError(f"Depot {depot} is outside the feasible area")
    if limit_seconds <= 0:
        raise ValueError("Time limit must be positive")
    start_time = perf_counter()
    locations = instance.requested_locations
    params: dict[str, float | int | str] = {
        "objective_profile": objective_profile,
        "objective_scale": objective_scale,
        "solver_threads": solver_threads,
        "seed": seed,
        "limit_seconds": limit_seconds,
    }
    if not locations:
        return RouteResult(
            depot,
            (),
            (),
            0.0,
            "OPTIMAL",
            0.0,
            0.0,
            0.0,
            0.0,
            "OPTIMAL",
            build_seconds=0.0,
            parameters=params,
        )
    positions = [depot] + [instance.layout.point(k) for k in locations]
    model = cp_model.CpModel()
    arcs = []
    travel = {}
    for i in range(len(positions)):
        for j in range(len(positions)):
            if i == j:
                continue
            var = model.new_bool_var(f"travel_{i}_{j}")
            travel[i, j] = var
            arcs.append((i, j, var))
    model.add_multiple_circuit(arcs)
    loads = {
        i: model.new_int_var(
            instance.qty_by_location[locations[i - 1]], instance.capacity, f"load_{i}"
        )
        for i in range(1, len(positions))
    }
    for j in range(1, len(positions)):
        qty = instance.qty_by_location[locations[j - 1]]
        model.add(loads[j] == qty).only_enforce_if(travel[0, j])
        for i in range(1, len(positions)):
            if i != j:
                model.add(loads[j] == loads[i] + qty).only_enforce_if(travel[i, j])
    model.minimize(
        sum(
            round(dist(positions[i], positions[j]) * objective_scale) * var
            for (i, j), var in travel.items()
        )
    )
    cp_solver = cp_model.CpSolver()
    cp_solver.parameters.max_time_in_seconds = limit_seconds
    cp_solver.parameters.num_search_workers = solver_threads
    cp_solver.parameters.random_seed = seed
    build_seconds = perf_counter() - start_time
    status = cp_solver.solve(model)
    solve_seconds = cp_solver.wall_time
    raw_status = cp_solver.status_name(status)
    elapsed = perf_counter() - start_time
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        label = "TIME_LIMIT_NO_SOLUTION" if status == cp_model.UNKNOWN else "ERROR"
        reason = "TIME_LIMIT" if status == cp_model.UNKNOWN else raw_status
        return RouteResult(
            depot,
            (),
            (),
            None,
            label,
            elapsed,
            raw_status=raw_status,
            stop_reason=reason,
            build_seconds=build_seconds,
            solve_seconds=solve_seconds,
            parameters=params,
        )
    successor = {i: j for (i, j), var in travel.items() if cp_solver.value(var)}
    starts = sorted(
        j for (i, j), var in travel.items() if i == 0 and cp_solver.value(var)
    )
    tours = []
    for first in starts:
        tour = []
        current = first
        while current != 0:
            tour.append(locations[current - 1])
            current = successor[current]
        tours.append(tuple(tour))
    route_tours = tuple(tours)
    tour_loads = tuple(sum(instance.qty_by_location[k] for k in tour) for tour in tours)
    picker_distance = sum(
        dist(positions[i], positions[j])
        for (i, j), var in travel.items()
        if cp_solver.value(var)
    )
    objective_value = cp_solver.objective_value / objective_scale
    best_bound = cp_solver.best_objective_bound / objective_scale
    validate_routes(
        instance,
        depot,
        route_tours,
        tour_loads,
        picker_distance,
        objective_value,
        objective_profile,
        objective_scale,
    )
    difference = abs(cp_solver.objective_value - cp_solver.best_objective_bound)
    certified = status == cp_model.OPTIMAL and difference <= 1 + 1e-7
    label = "OPTIMAL" if certified else "FEASIBLE"
    reason = (
        "BOUND_MISMATCH"
        if status == cp_model.OPTIMAL and not certified
        else ("TIME_LIMIT" if status == cp_model.FEASIBLE else None)
    )
    gap = difference / max(abs(cp_solver.objective_value), 1)
    return RouteResult(
        depot,
        route_tours,
        tour_loads,
        picker_distance,
        label,
        elapsed,
        objective_value,
        best_bound,
        gap,
        raw_status,
        reason,
        build_seconds,
        solve_seconds,
        params,
    )
