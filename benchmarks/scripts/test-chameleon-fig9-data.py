#!/usr/bin/env python3
"""Fig9 config and numeric tests; synthetic rows are not experiment results."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import chameleon_fig9 as fig9


def selected_path(entry):
    path = Path(entry["configuration_file"])
    return path if path.is_absolute() else fig9.DEFAULT_QUALIFIED.parent / path


def write_qualified_fixture(path, value):
    """Keep legacy selected-file lookup local to the temporary fixture."""
    value = copy.deepcopy(value)
    for entry in value.get("user_selected_configurations", {}).values():
        original = selected_path(entry)
        (path.parent / original.name).write_text(original.read_text())
        entry["configuration_file"] = original.name
    path.write_text(json.dumps(value))


class Configurations(unittest.TestCase):
    def test_all_mix_members_resolve_saved_high_unchanged(self):
        index = json.loads(fig9.DEFAULT_QUALIFIED.read_text())
        for case in sorted({c for cases in fig9.MIXES.values() for c in cases}):
            with self.subTest(case=case):
                resolved = fig9.resolve_high(case)
                selected = index.get("user_selected_configurations", {}).get(case)
                app = (json.loads(selected_path(selected).read_text()) if selected
                       else index["applications"][case])
                point = next(p for p in app["points"] if p["role"] == "high")
                self.assertEqual(resolved["configuration"], point["configuration"])
                self.assertEqual(resolved["workload_configuration"], app["workload_configuration"])
                self.assertEqual(resolved["vm_memory_mib"], app["vm_memory_mib"])
                self.assertEqual(resolved["original_observation_count"], 1 if selected else 2)
                self.assertEqual(resolved["all_local"]["tracking_profile"],
                                 {"sampling": 65536, "cooling": 131072, "hhh_interval_ms": 15000})
                self.assertTrue(Path(resolved["all_local"]["report"]).is_absolute())
                for report in resolved["provenance"]["original_reports"]:
                    self.assertTrue(Path(report).is_absolute())

    def test_cassandra_saved_high_100ms_and_single_observation(self):
        resolved = fig9.resolve_high("cassandra")
        self.assertEqual(resolved["candidate_id"], "d12")
        self.assertEqual(resolved["configuration"]["epoch_us"], 100000)
        self.assertEqual(resolved["original_observation_count"], 1)
        self.assertEqual(resolved["all_local"]["performance"]["cost"], 43.106)

    def test_memcached_denominator_is_first_fixed_not_average(self):
        resolved = fig9.resolve_high("memcached")
        self.assertEqual(resolved["all_local"]["performance"]["p95_us"], 335.359)
        self.assertEqual(resolved["all_local"]["trial_id"], "b73728-pin2")

    def test_selected_file_takes_precedence_over_accepted_copy(self):
        data = json.loads(fig9.DEFAULT_QUALIFIED.read_text())
        data["applications"]["cassandra"] = {"status": "ACCEPTED", "points": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qualified.json"
            write_qualified_fixture(path, data)
            self.assertEqual(fig9.resolve_high("cassandra", path)["candidate_id"], "d12")

    def test_missing_high_or_baseline_not_silently_used(self):
        data = json.loads(fig9.DEFAULT_QUALIFIED.read_text())
        data["applications"]["memcached"]["all_local"]["fixed_main_baseline_report"] = "absent"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qualified.json"
            write_qualified_fixture(path, data)
            with self.assertRaisesRegex(ValueError, "all-local"):
                fig9.resolve_high("memcached", path)
        with self.assertRaisesRegex(ValueError, "No accepted"):
            fig9.resolve_high("unavailable-licensed-application")


class References(unittest.TestCase):
    def test_packaged_json_baselines_and_chameleon_excluded(self):
        source = fig9.HERE.parents[1] / "ae/results_baselines/fig9.json"
        reference = fig9.reference_data(source)
        self.assertEqual(reference["kind"], "pre-measured")
        self.assertEqual(list(reference["mixes"]), list(fig9.MIXES))
        self.assertEqual(reference["mixes"]["mix1"]["75%"]["HyperAlloc"]["slowdown_percent"], 28)
        self.assertEqual(reference["mixes"]["mix4"]["50%"]["Static"]["slowdown_percent"], 155)
        for mix in reference["mixes"].values():
            for branch in mix.values():
                self.assertNotIn("Chameleon", branch)
                self.assertEqual(set(branch), set(fig9.BASELINE_SYSTEMS))

    def test_legacy_literals_without_execution_or_chameleon_placeholder(self):
        # Synthetic input keeps the optional legacy parser covered independently
        # of the packaged measurements and deleted plotting archives.
        systems = ["Chameleon", *fig9.BASELINE_SYSTEMS]
        values = {"Mix " + mix[3:]: {level: [999, 10, 20, 30]
                  for level in ("50%", "75%")} for mix in fig9.MIXES}
        errors = {label: {level: [0, 1, 2, 3] for level in branches}
                  for label, branches in values.items()}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plot.py"
            path.write_text(f"SYSTEMS = {systems!r}\nDATA = {values!r}\n"
                            f"ERROR_DATA = {errors!r}\n"
                            'raise RuntimeError("must not execute plot")\n')
            reference = fig9.reference_data(path)
        for mix in reference["mixes"].values():
            for branch in mix.values():
                self.assertNotIn("Chameleon", branch)
                for system, value, error in zip(fig9.BASELINE_SYSTEMS, [10, 20, 30], [1, 2, 3]):
                    self.assertEqual(branch[system]["slowdown_percent"], value)
                    self.assertEqual(branch[system]["error_percent"], error)

    def test_nonliteral_data_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plot.py"
            path.write_text("DATA = __import__('os').abort()\nSYSTEMS=[]\nERROR_DATA={}\n")
            with self.assertRaises(ValueError):
                fig9.reference_data(path)


class Aggregation(unittest.TestCase):
    def rows(self):
        return [{"application": app, "status": "PASS", "slowdown_percent": value}
                for app, value in zip(fig9.MIXES["mix1"], [-3, 6, 9])]

    def test_mean_per_app_percentages_keeps_negative_and_p95_separate(self):
        rows = self.rows()
        rows[0]["p95_slowdown_percent"] = 40
        original = copy.deepcopy(rows)
        result = fig9.aggregate_mix("mix1", reversed(rows))
        self.assertEqual(result["slowdown_percent"], 4)
        self.assertEqual(result["applications"][0]["slowdown_percent"], -3)
        self.assertEqual(result["p95_slowdown_by_application"], {"memcached": 40})
        self.assertEqual(rows, original)

    def test_cost_normalization_is_not_ratio_of_mean_runtimes(self):
        rows = [{"application": app, "status": "PASS", "performance": {"cost": run},
                 "baseline_performance": {"cost": base}}
                for app, run, base in zip(fig9.MIXES["mix1"], [2, 100, 1000], [1, 100, 1000])]
        result = fig9.aggregate_mix("mix1", rows)
        self.assertAlmostEqual(result["slowdown_percent"], 100 / 3)
        self.assertNotAlmostEqual(result["slowdown_percent"], (1102 / 1101 - 1) * 100)

    def test_memcached_uses_inverse_throughput_cost_and_p95(self):
        result = fig9.performance_slowdown({"cost": 1 / .008, "p95_us": 330},
                                          {"cost": 1 / .010, "p95_us": 300})
        self.assertAlmostEqual(result["slowdown_percent"], 25)
        self.assertAlmostEqual(result["p95_slowdown_percent"], 10)

    def test_incomplete_duplicate_failed_and_mismatched_apps_rejected(self):
        for rows in (self.rows()[:2], self.rows() + self.rows()[:1], [self.rows()[0]] * 3,
                     [*self.rows()[:2], {"application": "cassandra", "status": "PASS", "slowdown_percent": 4}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                fig9.aggregate_mix("mix1", rows)
        rows = self.rows()
        rows[0]["status"] = "FAIL"
        with self.assertRaisesRegex(ValueError, "failed"):
            fig9.aggregate_mix("mix1", rows)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            fig9.aggregate_mix("mix5", self.rows())

    def test_missing_nonfinite_and_inconsistent_metrics_rejected(self):
        for value in [None, float("nan"), float("inf"), True]:
            rows = self.rows()
            rows[0]["slowdown_percent"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                fig9.aggregate_mix("mix1", rows)
        rows = self.rows()
        rows[0].update(performance={"cost": 10}, baseline_performance={"cost": 10})
        with self.assertRaisesRegex(ValueError, "Inconsistent"):
            fig9.aggregate_mix("mix1", rows)
        with self.assertRaises(ValueError):
            fig9.performance_slowdown({"cost": 10}, {"cost": 0})

    def test_evaluate_uses_existing_time_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "application/trial"
            app.mkdir(parents=True)
            (app / "train-time.txt").write_text("Elapsed (wall clock) time (h:mm:ss or m:ss): 0:15.00\n")
            (app / "predict-time.txt").write_text("Elapsed (wall clock) time (h:mm:ss or m:ss): 0:05.00\n")
            high = {"application": "liblinear", "all_local": {"report": "/synthetic/report.json", "performance": {"cost": 10}}}
            row = fig9.evaluate_application("liblinear", Path(tmp), high)
            self.assertEqual(row["performance"]["cost"], 20)
            self.assertEqual(row["slowdown_percent"], 100)
            self.assertNotIn("status", row)  # Extraction alone does not prove a valid run.


if __name__ == "__main__":
    unittest.main()
