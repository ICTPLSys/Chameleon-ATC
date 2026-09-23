#!/usr/bin/env python3
"""Synthetic resource/cleanup checks; never boots VMs or creates RDMA pools."""
import contextlib
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chameleon_fig9_runtime as rt

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("smoke", HERE / "smoke-chameleon-fig9-rdma.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def inventory():
    value = json.loads((HERE.parent / "config/chameleon-fig9-host.example.json").read_text())
    for i, slot in enumerate(value["slots"]):
        slot["vfio"] = [f"0000:42:0{i}.1"]
    return value


def plan(value):
    rows = [{"cpu": core + 24 * sibling, "node": 0, "socket": 0, "core": core}
            for core in range(24) for sibling in range(2)]
    return smoke.build_plan(value, topology=rows, allowed=set(range(48)))


def state(slot=None):
    slot = slot or inventory()["slots"][0]
    return {"r3_controls_available": True, "hermit": {
        "backend": "rdma", "registered": 1, "capacity_pages": 2097152,
        "store_success": 0, "load_success": 0, "bytes_written": 0, "bytes_read": 0,
        "store_failures": 0, "load_failures": 0, "live_slots": 0, "allocated_pages": 0, "inflight": 0},
        "rdma": {"write_bytes": 0, "read_bytes": 0, "write_completions": 0,
                 "read_completions": 0, "transfer_errors": 0, "map_failures": 0, "broken": 0},
        "parameters": {"backend": "rdma", "sip": inventory()["server"]["address"],
                       "sport": str(slot["server_port"]), "pool_mb": "8192"}}


class SmokeTests(unittest.TestCase):
    def test_small_plan_is_separate_from_high_parameters_and_disjoint(self):
        value = inventory()
        before = copy.deepcopy(value)
        result = plan(value)
        self.assertEqual(value, before)
        self.assertFalse(result["saved_high_configurations_modified"])
        selected = []
        for row in result["slots"]:
            self.assertEqual(row["vm_memory_mib"], 4096)
            self.assertEqual(row["workload_configuration"]["vcpus"], 2)
            selected.extend(row["cpu_affinity"]["vcpu_host_cpus"] + row["cpu_affinity"]["qemu_service_cpus"])
        self.assertEqual(len(selected), 12)
        self.assertEqual(len({cpu % 24 for cpu in selected}), 12)
        self.assertEqual(result["remote_pool_mib_per_vm"], 8192)

    def test_connection_rejects_wrong_backend_pool_or_endpoint(self):
        value = inventory()
        smoke.verify_connection(state(), value["slots"][0], value["server"])
        for group, key, replacement in [("hermit", "backend", "dram"), ("hermit", "registered", 0),
                                         ("hermit", "capacity_pages", 1024), ("rdma", "broken", 1),
                                         ("parameters", "sport", "9999")]:
            current = state()
            current[group][key] = replacement
            with self.subTest(key=key), self.assertRaises(ValueError):
                smoke.verify_connection(current, value["slots"][0], value["server"])

    def roundtrip(self):
        before, after = state(), state()
        after["rdma"].update(write_bytes=8192, read_bytes=8192, write_completions=2, read_completions=2)
        after["hermit"].update(store_success=2, load_success=2, bytes_written=8192, bytes_read=8192,
                               store_failures=1, load_failures=2)
        qmp = {"actual": 4096 << 20, "chameleon": {"range-records": 0, "retired-pages": 0, "blocked-pages": 0}}
        return before, after, "PASS CHAMELEON_R3 checks=123 mode=DETERMINISTIC_ONLY\n", qmp

    def test_injected_hermit_failures_preserved_but_transport_must_pass(self):
        before, after, log, qmp = self.roundtrip()
        result = smoke.verify_round_trip(before, after, log, qmp, 4096)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["counter_deltas"]["hermit"]["load_failures"], 2)
        after["rdma"]["transfer_errors"] = 1
        with self.assertRaisesRegex(ValueError, "transport"):
            smoke.verify_round_trip(before, after, log, qmp, 4096)

    def test_no_reads_missing_marker_or_retained_resources_rejected(self):
        for failure in ("no-read", "no-marker", "guest-resources", "host-resources"):
            before, after, log, qmp = self.roundtrip()
            if failure == "no-read": after["rdma"]["read_bytes"] = 0
            if failure == "no-marker": log = "not finished\n"
            if failure == "guest-resources": after["hermit"]["live_slots"] = 1
            if failure == "host-resources": qmp["actual"] -= 4096
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                smoke.verify_round_trip(before, after, log, qmp, 4096)

    def test_auto_skip_and_required_fail_for_missing_test_controls(self):
        current = state()
        current["r3_controls_available"] = False
        with mock.patch.object(smoke, "snapshot", return_value=current):
            result = smoke.data_probe({}, {}, Path("/unused"), 30, "auto")
            self.assertEqual(result["status"], "SKIPPED")
            with self.assertRaises(ValueError):
                smoke.data_probe({}, {}, Path("/unused"), 30, "required")

    def run_scenario(self, fail_start=False, fail_data=False):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            value = inventory()
            planned = plan(value)
            accesses, slots = {}, {slot["name"]: slot for slot in value["slots"]}
            for name in slots:
                directory = output / name
                directory.mkdir()
                (directory / "vm.json").write_text('{"original": true}\n')
                (directory / "qemu.pid").write_text("123")
                accesses[name] = {"name": name, "user": "ubuntu", "vm_config": str(directory / "vm.json")}
            running, stopped, probes = set(), [], []
            def start(access, **kwargs):
                running.add(access["name"])
                if fail_start and len(running) == 2:
                    raise RuntimeError("synthetic boot failure")
            def stop(access, **kwargs):
                stopped.append(access["name"])
                running.remove(access["name"])
            def probe(access, *args):
                self.assertEqual(len(running), 3)  # All three are connected before any data test.
                probes.append(access["name"])
                if fail_data and access["name"] == value["slots"][1]["name"]:
                    raise RuntimeError("synthetic data error")
                return {"status": "PASS"}
            @contextlib.contextmanager
            def servers(*args):
                try:
                    yield [mock.Mock(poll=mock.Mock(return_value=None)) for _ in range(3)]
                finally:
                    self.assertFalse(running)  # Stop owned Guests before their remote memory servers.
            with mock.patch.object(rt, "servers", side_effect=servers), \
                 mock.patch.object(rt, "access", side_effect=lambda name: accesses[name]), \
                 mock.patch.object(rt.guest, "control_lock", side_effect=lambda _: contextlib.nullcontext()), \
                 mock.patch.object(rt.guest, "active", side_effect=lambda a: a["name"] in running), \
                 mock.patch.object(rt.guest, "launcher_idle", return_value=True), \
                 mock.patch.object(rt.guest, "start", side_effect=start), \
                 mock.patch.object(rt, "stop_guest", side_effect=stop), \
                 mock.patch.object(rt.guest, "run_dir", side_effect=lambda a: Path(a["vm_config"]).parent), \
                 mock.patch.object(rt.guest, "qmp", return_value=[]), \
                 mock.patch.object(rt, "setup_guest", return_value={"status": "PASS"}), \
                 mock.patch.object(rt.affinity, "apply", return_value=({}, {"status": "PASS"})), \
                 mock.patch.object(rt, "remote", return_value='{"status":"PASS"}'), \
                 mock.patch.object(smoke, "_start_guard"), \
                 mock.patch.object(smoke, "snapshot", side_effect=lambda a: state(slots[a["name"]])), \
                 mock.patch.object(smoke, "data_probe", side_effect=probe):
                if fail_start or fail_data:
                    with self.assertRaises(RuntimeError):
                        smoke.run_owned(value, planned, {"disk": "/unused"}, output, 30, "required")
                else:
                    self.assertEqual(smoke.run_owned(value, planned, {"disk": "/unused"}, output, 30, "required")["status"], "PASS")
            for access in accesses.values():
                self.assertEqual(Path(access["vm_config"]).read_text(), '{"original": true}\n')
            self.assertEqual(len(stopped), 2 if fail_start else 3)
            self.assertEqual(len(probes), 0 if fail_start else 3)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "FAIL" if fail_start or fail_data else "PASS")
            self.assertEqual(report["cleanup_errors"], [])

    def test_success_cleans_three_vms_then_pools_and_restores_slot_configs(self):
        self.run_scenario()

    def test_partial_boot_failure_cleans_only_owned_vms(self):
        self.run_scenario(fail_start=True)

    def test_data_failure_still_collects_and_cleans_three_vms(self):
        self.run_scenario(fail_data=True)


if __name__ == "__main__":
    unittest.main()
