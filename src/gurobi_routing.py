"""Gurobi branch-and-cut routing at a fixed depot."""

from __future__ import annotations

from math import dist
from time import perf_counter

import gurobipy as gp
from gurobipy import GRB

from .instances import Instance, Point
from .routing import DISTANCE_SCALE, RouteResult, validate_routes


def _groups(
    successor: dict[int, int], starts: list[int], count: int
) -> tuple[list[list[int]], list[list[int]]]:
    """Separate depot tours from isolated cycles in an integer solution."""
    visited: set[int] = set()
    tours: list[list[int]] = []
    for start in starts:
        tour: list[int] = []
        current = start
        while current != 0:
            tour.append(current)
            visited.add(current)
            current = successor[current]
        tours.append(tour)
    cycles: list[list[int]] = []
    for start in range(1, count + 1):
        if start in visited:
            continue
        cycle: list[int] = []
        current = start
        while current not in visited:
            cycle.append(current)
            visited.add(current)
            current = successor[current]
        cycles.append(cycle)
    return tours, cycles


def solve_routes_gurobi(
    instance: Instance,
    depot: Point,
    limit_seconds: float = 3.0,
    *,
    objective_profile: str = "rounded",
    objective_scale: int = DISTANCE_SCALE,
    solver_threads: int = 1,
    seed: int = 0,
) -> RouteResult:
    if not instance.layout.contains(depot):
        raise ValueError(f"Depot {depot} is outside the feasible area")
    if limit_seconds <= 0:
        raise ValueError("Time limit must be positive")
    if objective_profile not in ("rounded", "euclidean"):
        raise ValueError("Unknown objective profile")
    if objective_scale <= 0 or solver_threads <= 0:
        raise ValueError("Objective scale and solver threads must be positive")
    start_time = perf_counter()
    locations = instance.requested_locations
    if not locations:
        return RouteResult(
            depot,
            (),
            (),
            0.0,
            "OPTIMAL",
            0.0,
            objective_value=0.0,
            best_bound=0.0,
            gap=0.0,
            raw_status="OPTIMAL",
            parameters={
                "objective_profile": objective_profile,
                "objective_scale": objective_scale,
                "solver_threads": solver_threads,
                "seed": seed,
                "limit_seconds": limit_seconds,
            },
        )

    count = len(locations)
    nodes = range(count + 1)
    positions = [depot, *(instance.layout.point(k) for k in locations)]
    quantities = [0, *(instance.qty_by_location[k] for k in locations)]
    arcs = [(i, j) for i in nodes for j in nodes if i != j]
    with gp.Model("fixed_depot_routing") as model:
        model.Params.OutputFlag = 0
        model.Params.TimeLimit = limit_seconds
        model.Params.Threads = solver_threads
        model.Params.Seed = seed
        model.Params.MIPGap = 0
        model.Params.MIPGapAbs = 0
        model.Params.LazyConstraints = 1
        parameters: dict[str, float | int | str] = {
            "objective_profile": objective_profile,
            "objective_scale": objective_scale,
            "solver_threads": model.Params.Threads,
            "seed": model.Params.Seed,
            "limit_seconds": model.Params.TimeLimit,
            "mip_gap": model.Params.MIPGap,
            "mip_gap_abs": model.Params.MIPGapAbs,
            "lazy_constraints": model.Params.LazyConstraints,
        }
        travel = model.addVars(arcs, vtype=GRB.BINARY, name="travel")
        for i in range(1, count + 1):
            model.addConstr(gp.quicksum(travel[i, j] for j in nodes if j != i) == 1)
            model.addConstr(gp.quicksum(travel[j, i] for j in nodes if j != i) == 1)
        model.setObjective(
            gp.quicksum(
                (
                    round(dist(positions[i], positions[j]) * objective_scale)
                    if objective_profile == "rounded"
                    else dist(positions[i], positions[j])
                )
                * travel[i, j]
                for i, j in arcs
            ),
            GRB.MINIMIZE,
        )

        def separate(model: gp.Model, where: int) -> None:
            if where != GRB.Callback.MIPSOL:
                return
            values = model.cbGetSolution(travel)
            selected = {(i, j) for i, j in arcs if values[i, j] > 0.5}
            successor = {i: j for i, j in selected if i != 0}
            starts = sorted(j for i, j in selected if i == 0)
            tours, cycles = _groups(successor, starts, count)
            for group in [*tours, *cycles]:
                demand = sum(quantities[i] for i in group)
                if group in tours and demand <= instance.capacity:
                    continue
                members = set(group)
                rhs = (demand + instance.capacity - 1) // instance.capacity
                outgoing = sum(i in members and j not in members for i, j in selected)
                if outgoing < rhs:
                    model.cbLazy(
                        gp.quicksum(
                            travel[i, j]
                            for i in members
                            for j in nodes
                            if j not in members
                        )
                        >= rhs
                    )

        build_seconds = perf_counter() - start_time
        model.optimize(separate)
        raw_status = str(model.Status)
        solve_seconds = model.Runtime
        elapsed = perf_counter() - start_time
        if model.SolCount == 0:
            label = (
                "TIME_LIMIT_NO_SOLUTION" if model.Status == GRB.TIME_LIMIT else "ERROR"
            )
            reason = (
                "TIME_LIMIT"
                if model.Status == GRB.TIME_LIMIT
                else f"GUROBI_{model.Status}"
            )
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
                parameters=parameters,
            )
        selected = {(i, j) for i, j in arcs if travel[i, j].X > 0.5}
        successor = {i: j for i, j in selected if i != 0}
        starts = sorted(j for i, j in selected if i == 0)
        tours, cycles = _groups(successor, starts, count)
        if cycles or any(
            sum(quantities[i] for i in tour) > instance.capacity for tour in tours
        ):
            raise RuntimeError("Gurobi returned a route violating a lazy constraint")
        route_tours = tuple(tuple(locations[i - 1] for i in tour) for tour in tours)
        loads = tuple(sum(quantities[i] for i in tour) for tour in tours)
        distance = sum(dist(positions[i], positions[j]) for i, j in selected)
        multiplier = objective_scale if objective_profile == "rounded" else 1
        objective_value = model.ObjVal / multiplier
        best_bound = model.ObjBound / multiplier
        validate_routes(
            instance,
            depot,
            route_tours,
            loads,
            distance,
            objective_value,
            objective_profile,
            objective_scale,
        )
        difference = abs(model.ObjVal - model.ObjBound)
        tolerance = 1 if objective_profile == "rounded" else 1e-6
        certified = model.Status == GRB.OPTIMAL and difference <= tolerance + 1e-7
        status = "OPTIMAL" if certified else "FEASIBLE"
        if model.Status == GRB.OPTIMAL:
            reason = None if certified else "BOUND_MISMATCH"
        elif model.Status == GRB.TIME_LIMIT:
            reason = "TIME_LIMIT"
        else:
            reason = f"GUROBI_{model.Status}"
        gap = difference / max(abs(model.ObjVal), 1)
        return RouteResult(
            depot,
            route_tours,
            loads,
            distance,
            status,
            elapsed,
            objective_value,
            best_bound,
            gap,
            raw_status,
            reason,
            build_seconds,
            solve_seconds,
            parameters,
        )
