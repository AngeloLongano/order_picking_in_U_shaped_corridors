"""Capacitated multiple-tour routing at a fixed depot."""

from __future__ import annotations

from dataclasses import dataclass
from math import dist

from ortools.sat.python import cp_model

from .instances import Instance, Point

DISTANCE_SCALE = 10_000  # CP-SAT needs integer objective coefficients.


@dataclass(frozen=True)
class RouteResult:
    depot: Point
    tours: tuple[tuple[int, ...], ...]  # depot is implicit at both ends
    tour_loads: tuple[int, ...]
    picker_distance: float | None
    status: str
    elapsed_seconds: float

    @property
    def feasible(self) -> bool:
        return self.picker_distance is not None


def solve_routes(
    instance: Instance,
    depot: Point,
    limit_seconds: float = 3.0,
    solver: str = "cp_sat",
) -> RouteResult:
    if solver == "gurobi":
        from .gurobi_routing import solve_routes_gurobi

        return solve_routes_gurobi(instance, depot, limit_seconds)
    if solver != "cp_sat":
        raise ValueError("Solver must be 'cp_sat' or 'gurobi'")
    if not instance.layout.contains(depot):
        raise ValueError(f"Depot {depot} is outside the feasible area")
    if limit_seconds <= 0:
        raise ValueError("Time limit must be positive")
    locations = instance.requested_locations
    if not locations:
        return RouteResult(depot, (), (), 0.0, "OPTIMAL", 0.0)
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
            round(dist(positions[i], positions[j]) * DISTANCE_SCALE) * var
            for (i, j), var in travel.items()
        )
    )
    cp_solver = cp_model.CpSolver()
    cp_solver.parameters.max_time_in_seconds = limit_seconds
    cp_solver.parameters.num_search_workers = 1
    status = cp_solver.solve(model)
    label = cp_solver.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return RouteResult(depot, (), (), None, label, cp_solver.wall_time)
    successor = {i: j for (i, j), var in travel.items() if cp_solver.value(var)}
    starts = sorted(
        j for (i, j), var in travel.items() if i == 0 and cp_solver.value(var)
    )
    tours = []
    for start in starts:
        tour = []
        current = start
        while current != 0:
            tour.append(locations[current - 1])
            current = successor[current]
        tours.append(tuple(tour))
    tour_loads = tuple(sum(instance.qty_by_location[k] for k in tour) for tour in tours)
    picker_distance = sum(
        dist(positions[i], positions[j])
        for (i, j), var in travel.items()
        if cp_solver.value(var)
    )
    return RouteResult(
        depot, tuple(tours), tour_loads, picker_distance, label, cp_solver.wall_time
    )
