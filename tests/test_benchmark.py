"""Checkpoint and planning contract for benchmark campaigns."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.benchmark import (
    _candidate_count,
    _done_path,
    _export,
    _format_duration,
    _load_config,
    _profile,
    _worker,
)
from src.instances import load_instance
from src.routing import RouteResult


class BenchmarkTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(_format_duration(5700.7), "95 min 0.7 s")
        self.assertEqual(_format_duration(59.96), "1 min 0.0 s")
        self.assertEqual(_format_duration(0), "0 min 0.0 s")

    def test_config_and_pilot_grid(self):
        config = _load_config()
        self.assertEqual(len(config["instances"]), 36)
        self.assertEqual(_profile(config)["objective_profile"], "rounded")
        instance = load_instance(config["instances"][0])
        lo, hi = _candidate_count(instance, "ZoomIn", "2d", config["search"])
        self.assertGreater(hi, lo - 1)

    def test_checkpoint_resume_and_export(self):
        config = _load_config()
        config["search"] = config["search"] | {"basic_step_m": 100.0}
        job = (config["instances"][0], "cp_sat", "Basic", "1d")
        calls = []

        def route(instance, depot, limit, solver, **kwargs):
            calls.append(depot)
            return RouteResult(
                depot,
                (instance.requested_locations,),
                (sum(instance.qty_by_location.values()),),
                10.0,
                "FEASIBLE",
                0.01,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("src.routing.solve_routes", side_effect=route):
                first = _worker(str(root), job, config, "test")
            self.assertEqual(len(calls), 1)
            self.assertTrue(_done_path(root, job).exists())
            with patch(
                "src.routing.solve_routes", side_effect=AssertionError("resolved")
            ):
                second = _worker(str(root), job, config, "test")
            self.assertEqual(first["best_total_cost_m"], second["best_total_cost_m"])
            _export(root, [job])
            self.assertEqual(len((root / "candidati.csv").read_text().splitlines()), 2)
            self.assertEqual(len((root / "riepilogo.csv").read_text().splitlines()), 2)

    def test_zoomin_retry_matches_continuous_run(self):
        config = _load_config()
        job = (config["instances"][0], "cp_sat", "ZoomIn", "1d")
        count = 0

        def route(instance, depot, limit, solver, **kwargs):
            return RouteResult(depot, (), (), 10.0 + depot[0], "FEASIBLE", 0.01)

        def interrupted(instance, depot, limit, solver, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("simulated interruption")
            return route(instance, depot, limit, solver, **kwargs)

        with (
            tempfile.TemporaryDirectory() as first_dir,
            tempfile.TemporaryDirectory() as second_dir,
        ):
            with (
                patch("src.routing.solve_routes", side_effect=interrupted),
                self.assertRaisesRegex(RuntimeError, "simulated interruption"),
            ):
                _worker(first_dir, job, config, "test")
            with patch("src.routing.solve_routes", side_effect=route):
                resumed = _worker(first_dir, job, config, "test")
                continuous = _worker(second_dir, job, config, "test")
            self.assertEqual(
                resumed["best_total_cost_m"], continuous["best_total_cost_m"]
            )
            self.assertEqual(resumed["best_depot_x"], continuous["best_depot_x"])
            self.assertEqual(
                resumed["candidates_executed"], continuous["candidates_executed"]
            )
            _export(Path(first_dir), [job])
            self.assertEqual(
                len((Path(first_dir) / "candidati.csv").read_text().splitlines()),
                resumed["candidates_executed"] + 1,
            )


if __name__ == "__main__":
    unittest.main()
