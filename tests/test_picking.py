import itertools
import unittest
from math import dist, isclose

from matplotlib import pyplot as plt
from matplotlib.patches import Rectangle

from src import (
    Instance,
    Layout,
    basic_search,
    load_instance,
    solve_routes,
    zoomin_search,
)
from src.basic import grid_points
from src.visualization import plot_layout


class PickingTests(unittest.TestCase):
    def test_dataset_and_geometry(self):
        instance = load_instance()
        self.assertEqual(
            (instance.layout.n, instance.layout.m, instance.layout.count), (9, 4, 44)
        )
        self.assertEqual(len(instance.requested_locations), 10)
        self.assertEqual(instance.layout.point(1), instance.layout.point(2))
        self.assertEqual(instance.layout.point(43), instance.layout.point(44))
        self.assertEqual(instance.layout.point(19)[1], instance.layout.point(20)[1])
        self.assertEqual(instance.layout.point(1)[0], 0)
        self.assertEqual(instance.layout.point(44)[0], 0)
        self.assertEqual(instance.layout.bounds[2], -instance.layout.bounds[3])

    def test_drawn_stillages_do_not_overlap(self):
        instance = load_instance()
        figure, ax = plt.subplots()
        try:
            plot_layout(instance, ax=ax)
            rectangles = [patch for patch in ax.patches if isinstance(patch, Rectangle)]
            self.assertEqual(len(rectangles), instance.layout.count)
            for i, left in enumerate(rectangles):
                for right in rectangles[i + 1 :]:
                    overlap_width = min(
                        left.get_x() + left.get_width(),
                        right.get_x() + right.get_width(),
                    ) - max(left.get_x(), right.get_x())
                    overlap_height = min(
                        left.get_y() + left.get_height(),
                        right.get_y() + right.get_height(),
                    ) - max(left.get_y(), right.get_y())
                    self.assertFalse(overlap_width > 1e-9 and overlap_height > 1e-9)
        finally:
            plt.close(figure)

    def test_routes_match_enumeration_and_capacity(self):
        layout = Layout(2, 2)
        instance = Instance("small", layout, {1: 7, 5: 6, 9: 5})
        depot = (0.65, 0)
        # Enumerate every possible order and every possible cut between tours.
        costs = []
        for order in itertools.permutations(instance.requested_locations):
            for cuts in itertools.product((False, True), repeat=2):
                groups = []
                current = [order[0]]
                for item, cut in zip(order[1:], cuts):
                    if cut:
                        groups.append(current)
                        current = []
                    current.append(item)
                groups.append(current)
                if any(
                    sum(instance.qty_by_location[k] for k in group) > instance.capacity
                    for group in groups
                ):
                    continue
                total = 0
                for group in groups:
                    points = [depot] + [layout.point(k) for k in group] + [depot]
                    total += sum(dist(a, b) for a, b in itertools.pairwise(points))
                costs.append(total)
        for solver in ("cp_sat", "gurobi"):
            with self.subTest(solver=solver):
                result = solve_routes(instance, depot, 3, solver)
                self.assertEqual(result.status, "OPTIMAL")
                assert result.picker_distance is not None
                self.assertEqual(
                    sorted(k for tour in result.tours for k in tour), [1, 5, 9]
                )
                self.assertEqual(len(result.tours), 2)
                self.assertTrue(all(load <= 15 for load in result.tour_loads))
                self.assertTrue(
                    isclose(result.picker_distance, min(costs), abs_tol=1e-8)
                )

    def test_gurobi_rejects_isolated_cycle(self):
        # Colocated picks make a disconnected cycle attractive unless cut off.
        instance = Instance("cycle", Layout(2, 2), {1: 1, 2: 1, 5: 1})
        result = solve_routes(instance, (0.65, 0), 3, "gurobi")
        self.assertEqual(result.status, "OPTIMAL")
        self.assertEqual(sorted(k for tour in result.tours for k in tour), [1, 2, 5])
        self.assertEqual(len(result.tours), 1)

    def test_basic_and_zoomin_records(self):
        instance = Instance("small", Layout(2, 2), {1: 7, 5: 6, 9: 5})
        bounds = instance.layout.bounds
        self.assertEqual(
            grid_points((0.65, 1.65, -0.5, 0.5), 0.5, "1d"),
            ((0.65, 0), (1.15, 0), (1.65, 0)),
        )
        basic = basic_search(instance, 0.5, "1d", 2)
        assert basic.best is not None
        assert basic.best.total_cost is not None
        self.assertEqual(len(basic.candidates), len(grid_points(bounds, 0.5, "1d")))
        self.assertEqual(
            basic.best.total_cost,
            min(c.total_cost for c in basic.candidates if c.total_cost is not None),
        )
        zoom = zoomin_search(instance, 0.5, 0.1, 5, "2d", 2)
        assert zoom.best is not None
        assert zoom.best.total_cost is not None
        self.assertEqual([scan.step for scan in zoom.scans], [0.5, 0.1])
        self.assertEqual(
            zoom.best.total_cost,
            min(c.total_cost for c in zoom.candidates if c.total_cost is not None),
        )
        self.assertTrue(all(instance.layout.contains(c.depot) for c in zoom.candidates))


if __name__ == "__main__":
    unittest.main()
