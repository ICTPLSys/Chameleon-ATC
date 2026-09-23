#!/usr/bin/env python3
"""Synthetic Fig9 chart checks; no synthetic data are saved as experiment runs."""
import copy
import importlib.util
import json
import math
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from chameleon_fig9 import MIXES, reference_data

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fig9_plot", HERE / "plot-chameleon-fig9.py")
plot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plot)


class PlotTests(unittest.TestCase):
    def fixture(self):
        reference = reference_data(HERE.parents[1] / "ae/results_baselines/fig9.json")
        repeats = [{"status": "PASS", "slowdown_percent": value,
                    "applications": [{"application": app, "status": "PASS", "slowdown_percent": value}
                                     for app in MIXES["mix1"]]} for value in [-6, -2]]
        return {"status": "PARTIAL", "reference": reference, "configuration": {"marker": "synthetic test only"},
                "mixes": {"mix1": {"status": "PASS", "repetitions": repeats}}}

    def test_repeat_mean_sample_sd_and_negative_values(self):
        data = plot.plot_data(self.fixture())
        chameleon = data["mixes"]["mix1"]["Chameleon"]
        self.assertEqual(chameleon["slowdown_percent"], -4)
        self.assertAlmostEqual(chameleon["sample_sd_percent"], math.sqrt(8))
        self.assertEqual(chameleon["n"], 2)
        self.assertEqual(data["mixes"]["mix2"]["Chameleon"]["status"], "NA")
        self.assertIsNone(data["mixes"]["mix2"]["Chameleon"]["slowdown_percent"])

    def test_one_repetition_has_no_fabricated_error(self):
        report = self.fixture()
        report["mixes"]["mix1"]["repetitions"] = report["mixes"]["mix1"]["repetitions"][:1]
        point = plot.plot_data(report)["mixes"]["mix1"]["Chameleon"]
        self.assertEqual(point["n"], 1)
        self.assertIsNone(point["sample_sd_percent"])

    def test_failed_repetition_is_not_silently_dropped(self):
        report = self.fixture()
        report["mixes"]["mix1"]["repetitions"][1]["status"] = "FAIL"
        point = plot.plot_data(report)["mixes"]["mix1"]["Chameleon"]
        self.assertEqual(point["status"], "NA")
        self.assertEqual(point["n"], 0)
        self.assertIsNone(point["slowdown_percent"])

    def test_missing_nonfinite_or_inconsistent_per_app_values_are_na(self):
        for kind in ("nan", "mismatch", "missing", "failed"):
            report = self.fixture()
            rep = report["mixes"]["mix1"]["repetitions"][0]
            if kind == "nan":
                rep["slowdown_percent"] = float("nan")
            elif kind == "mismatch":
                rep["slowdown_percent"] = 50
            elif kind == "missing":
                rep["applications"].pop()
            else:
                rep["applications"][0]["status"] = "FAIL"
            with self.subTest(kind=kind):
                self.assertEqual(plot.plot_data(report)["mixes"]["mix1"]["Chameleon"]["status"], "NA")

    def test_baseline_branch_does_not_modify_configuration_or_measurements(self):
        report = self.fixture()
        original = copy.deepcopy(report)
        a, b = plot.plot_data(report, "50"), plot.plot_data(report, "75")
        self.assertEqual(a["mixes"]["mix1"]["Chameleon"], b["mixes"]["mix1"]["Chameleon"])
        self.assertEqual(a["configuration"], b["configuration"])
        self.assertEqual(a["mixes"]["mix1"]["baselines"]["HyperAlloc"]["slowdown_percent"], 70.1)
        self.assertEqual(b["mixes"]["mix1"]["baselines"]["HyperAlloc"]["slowdown_percent"], 28.0)
        self.assertNotIn("Chameleon", a["mixes"]["mix1"]["baselines"])
        self.assertEqual(report, original)

    def test_standalone_svg_pdf_and_provenance_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "report.json").write_text(json.dumps(self.fixture()))
            plot.main(["--directory", tmp])
            svg = ET.parse(directory / "fig9-measured.svg").getroot()
            self.assertEqual(svg.attrib["width"], "504pt")
            self.assertEqual(svg.attrib["height"], "168pt")
            self.assertGreater((directory / "fig9-measured.pdf").stat().st_size, 1000)
            exported = json.loads((directory / "plot-data.json").read_text())
            self.assertEqual(exported["source_report"], str(directory / "report.json"))
            self.assertEqual(exported["reference"]["kind"], "pre-measured")
            self.assertEqual(exported["mixes"]["mix1"]["Chameleon"]["slowdown_percent"], -4)


if __name__ == "__main__":
    unittest.main()
