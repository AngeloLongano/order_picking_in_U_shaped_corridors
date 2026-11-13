"""Gurobi branch-and-cut routing at a fixed depot."""

from __future__ import annotations

from math import dist

import gurobipy as gp
from gurobipy import GRB

from .instances import Instance, Point
from .routing import RouteResult


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
    instance: Instance, depot: Point, limit_seconds: float = 3.0
) -> RouteResult:
    if not instance.layout.contains(depot):
        raise ValueError(f"Depot {depot} is outside the feasible area")
    if limit_seconds <= 0:
        raise ValueError("Time limit must be positive")
    locations = instance.requested_locations
    if not locations:
        return RouteResult(depot, (), (), 0.0, "OPTIMAL", 0.0)

    count = len(locations)
    nodes = range(count + 1)
    positions = [depot, *(instance.layout.point(k) for k in locations)]
    quantities = [0, *(instance.qty_by_location[k] for k in locations)]
    arcs = [(i, j) for i in nodes for j in nodes if i != j]
    with gp.Model("fixed_depot_routing") as model:
        model.Params.OutputFlag = 0
        model.Params.TimeLimit = limit_seconds
        model.Params.Threads = 1
        model.Params.LazyConstraints = 1
        travel = model.addVars(arcs, vtype=GRB.BINARY, name="travel")
        for i in range(1, count + 1):
            model.addConstr(gp.quicksum(travel[i, j] for j in nodes if j != i) == 1)
            model.addConstr(gp.quicksum(travel[j, i] for j in nodes if j != i) == 1)
        model.setObjective(
            gp.quicksum(
                dist(positions[i], positions[j]) * travel[i, j] for i, j in arcs
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

        model.optimize(separate)
        status = "OPTIMAL" if model.Status == GRB.OPTIMAL else "FEASIBLE"
        if model.SolCount == 0:
            label = (
                "TIME_LIMIT" if model.Status == GRB.TIME_LIMIT else str(model.Status)
            )
            return RouteResult(depot, (), (), None, label, model.Runtime)
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
        return RouteResult(depot, route_tours, loads, distance, status, model.Runtime)
