"""Checks for benchmark route measurements and incumbent validation."""

import unittest
from math import dist, isclose

from src import Instance, Layout, solve_routes
from src.routing import validate_routes


class RoutingBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.instance = Instance("small", Layout(2, 2), {1: 7, 5: 6, 9: 5})
        self.depot = (0.65, 0.0)

    def test_common_rounded_objective_and_euclidean_distance(self):
        results = [
            solve_routes(self.instance, self.depot, 3, solver)
            for solver in ("cp_sat", "gurobi")
        ]
        for route in results:
            with self.subTest(solver=route.parameters):
                self.assertEqual(route.status, "OPTIMAL")
                self.assertIsNotNone(route.objective_value)
                self.assertIsNotNone(route.best_bound)
                self.assertIsNotNone(route.picker_distance)
                assert route.objective_value is not None
                assert route.best_bound is not None
                assert route.picker_distance is not None
                self.assertLessEqual(
                    abs(route.objective_value - route.best_bound), 1e-4
                )
                self.assertGreaterEqual(route.elapsed_seconds, route.solve_seconds)
                self.assertTrue(
                    isclose(route.picker_distance, 8.235058837121475, abs_tol=1e-8)
                )
        self.assertEqual(results[0].objective_value, results[1].objective_value)

    def test_validation_rejects_missing_pick_and_false_cost(self):
        route = solve_routes(self.instance, self.depot, 3)
        assert route.objective_value is not None
        assert route.picker_distance is not None
        with self.assertRaises(ValueError):
            validate_routes(
                self.instance,
                self.depot,
                route.tours[:-1],
                route.tour_loads[:-1],
                route.picker_distance,
                route.objective_value,
            )
        with self.assertRaises(ValueError):
            validate_routes(
                self.instance,
                self.depot,
                route.tours,
                route.tour_loads,
                route.picker_distance + dist((0, 0), self.depot),
                route.objective_value,
            )

    def test_euclidean_profile_only_for_gurobi(self):
        with self.assertRaises(ValueError):
            solve_routes(self.instance, self.depot, 3, objective_profile="euclidean")
        route = solve_routes(
            self.instance, self.depot, 3, "gurobi", objective_profile="euclidean"
        )
        self.assertEqual(route.status, "OPTIMAL")
        assert route.objective_value is not None
        assert route.picker_distance is not None
        self.assertTrue(
            isclose(route.objective_value, route.picker_distance, abs_tol=1e-6)
        )


if __name__ == "__main__":
    unittest.main()
