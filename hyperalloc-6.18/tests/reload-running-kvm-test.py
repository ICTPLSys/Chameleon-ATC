#!/usr/bin/env python3
"""Exercise module recovery with mocks only; never issue real module operations."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parent.parent / "scripts/reload-running-kvm.py"
SPEC = importlib.util.spec_from_file_location("reload_kvm", SOURCE)
RELOAD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELOAD)
RAW = {"kvm": {"enable_pmu": "Y"},
       "kvm_intel": {"vmentry_l1d_flush": "not required", "guest_pebs_baseline": "N"}}
ORIGINAL = {"kvm": "old-core", "kvm_intel": "old-intel"}
CANDIDATE = {"kvm": "new-core", "kvm_intel": "new-intel"}
PATHS = {"kvm": "/original/kvm.ko", "kvm_intel": "/original/kvm-intel.ko"}
SAVED = {"running_kernel": "test-kernel", "original_modules": PATHS,
         "parameters": RAW, "load_options": RELOAD.load_options(RAW),
         "original_build_ids": ORIGINAL, "candidate_build_ids": CANDIDATE}


class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)
        self.sys = self.root / "sys"
        self.stack.enter_context(patch.object(RELOAD, "SYS_MODULE", self.sys))
        self.stack.enter_context(patch.object(RELOAD, "BUNDLE", self.root))
        self.state = self.root / "activation-state.json"
        self.stack.enter_context(patch.object(RELOAD, "STATE", self.state))
        # Global guard: an unexpected subprocess invocation cannot reach the host.
        self.run = self.stack.enter_context(patch.object(RELOAD.subprocess, "run"))
        self.check_output = self.stack.enter_context(patch.object(
            RELOAD.subprocess, "check_output", side_effect=AssertionError("unexpected subprocess")))

    def mock(self, name, **kw):
        return self.stack.enter_context(patch.object(RELOAD, name, **kw))

    def recovery(self, present, ids):
        self.mock("output", return_value="test-kernel")
        self.files = self.mock("verify_files", return_value=ORIGINAL)
        self.mock("loaded", return_value=present)
        self.mock("live_build_id", side_effect=lambda m: ids[m])
        self.load_one = self.mock("load_one")
        self.load_pair = self.mock("load_pair")
        self.active = self.mock("verify_active")
        self.unload = self.mock("unload_pair")

    def core_refs(self, refs=0, holder=None):
        core = self.sys / "kvm"
        (core / "holders").mkdir(parents=True)
        (core / "refcnt").write_text(str(refs))
        if holder:
            (core / "holders" / holder).touch()

    def test_normalize_derived_flush_states_without_mutating_saved_values(self):
        for derived in ("not required", "EPT disabled"):
            raw = {m: dict(v) for m, v in RAW.items()}
            raw["kvm_intel"]["vmentry_l1d_flush"] = derived
            self.assertEqual(RELOAD.load_options(raw)["kvm_intel"]["vmentry_l1d_flush"], "auto")
            self.assertEqual(raw["kvm_intel"]["vmentry_l1d_flush"], derived)

    def test_broad_baseline_or_unknown_flush_rejected(self):
        for name, value in (("guest_pebs_baseline", "Y"), ("vmentry_l1d_flush", "mystery")):
            raw = {m: dict(v) for m, v in RAW.items()}
            raw["kvm_intel"][name] = value
            with self.assertRaises(RuntimeError):
                RELOAD.load_options(raw)

    def test_check_is_read_only_without_root(self):
        self.mock("prepare_candidate", return_value=(SAVED, PATHS))
        self.stack.enter_context(patch.object(RELOAD.os, "geteuid", return_value=1000))
        self.stack.enter_context(patch.object(RELOAD.sys, "argv", [str(SOURCE), "--check"]))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            RELOAD.main()
        self.assertFalse(json.loads(buffer.getvalue())["mutated"])
        self.assertFalse(self.state.exists())
        self.run.assert_not_called()

    def test_restore_does_not_prepare_or_read_candidate_files(self):
        self.state.write_text(json.dumps(SAVED))
        prepare = self.mock("prepare_candidate", side_effect=AssertionError("candidate preflight on restore"))
        restore = self.mock("restore_original")
        self.stack.enter_context(patch.object(RELOAD.os, "geteuid", return_value=0))
        self.stack.enter_context(patch.object(RELOAD.sys, "argv", [str(SOURCE), "--restore"]))
        with redirect_stdout(io.StringIO()):
            RELOAD.main()
        prepare.assert_not_called()
        restore.assert_called_once_with(SAVED)
        self.assertFalse(self.state.exists())
        self.assertEqual(len(list(self.root.glob("activation-restored-*.json"))), 1)
        self.run.assert_not_called()

    def test_restore_both_absent(self):
        self.recovery(set(), {})
        RELOAD.restore_original(SAVED)
        self.load_pair.assert_called_once_with(PATHS, SAVED["load_options"])
        self.load_one.assert_not_called()
        self.unload.assert_not_called()

    def test_restore_selected_bundle_preserves_default_activation(self):
        self.state.write_text(json.dumps({"unrelated": "default bundle"}))
        selected = self.root / "deployment candidate"
        selected.mkdir()
        selected_state = selected / "activation-state.json"
        selected_state.write_text(json.dumps(SAVED))
        restore = self.mock("restore_original")
        self.stack.enter_context(patch.object(RELOAD.os, "geteuid", return_value=0))
        self.stack.enter_context(patch.object(RELOAD.sys, "argv", [
            str(SOURCE), "--bundle", str(selected), "--restore"]))
        with redirect_stdout(io.StringIO()):
            RELOAD.main()
        restore.assert_called_once_with(SAVED)
        self.assertEqual(json.loads(self.state.read_text()), {"unrelated": "default bundle"})
        self.assertFalse(selected_state.exists())
        self.assertEqual(len(list(selected.glob("activation-restored-*.json"))), 1)
        self.run.assert_not_called()

    def test_restore_existing_original_pair_avoids_reload(self):
        self.recovery(set(ORIGINAL), ORIGINAL)
        RELOAD.restore_original(SAVED)
        self.active.assert_called_once_with(ORIGINAL, RAW)
        self.load_pair.assert_not_called()
        self.unload.assert_not_called()

    def test_restore_only_original_core_loads_backend(self):
        self.recovery({"kvm"}, ORIGINAL)
        RELOAD.restore_original(SAVED)
        self.load_one.assert_called_once_with("kvm_intel", PATHS, SAVED["load_options"])
        self.load_pair.assert_not_called()
        self.run.assert_not_called()

    def test_restore_only_candidate_core_removes_then_loads_original_pair(self):
        self.recovery({"kvm"}, CANDIDATE)
        self.core_refs()
        RELOAD.restore_original(SAVED)
        self.run.assert_called_once_with(["rmmod", "kvm"], check=True)
        self.load_pair.assert_called_once_with(PATHS, SAVED["load_options"])

    def test_restore_busy_candidate_core_preserves_it(self):
        self.recovery({"kvm"}, CANDIDATE)
        self.core_refs(refs=1)
        with self.assertRaisesRegex(RuntimeError, "still busy"):
            RELOAD.restore_original(SAVED)
        self.run.assert_not_called()
        self.load_pair.assert_not_called()

    def test_restore_unknown_core_refuses_removal(self):
        self.recovery({"kvm"}, {"kvm": "unrelated"})
        with self.assertRaisesRegex(RuntimeError, "Unrecognized loaded"):
            RELOAD.restore_original(SAVED)
        self.run.assert_not_called()

    def test_restore_candidate_pair(self):
        self.recovery(set(CANDIDATE), CANDIDATE)
        RELOAD.loaded.side_effect = [set(CANDIDATE), set()]
        RELOAD.restore_original(SAVED)
        self.unload.assert_called_once()
        self.load_pair.assert_called_once_with(PATHS, SAVED["load_options"])

    def test_busy_pair_cannot_be_unloaded(self):
        self.core_refs(refs=1, holder="kvm_intel")
        (self.sys / "kvm_intel").mkdir()
        (self.sys / "kvm_intel/refcnt").write_text("2")
        with self.assertRaisesRegex(RuntimeError, "KVM is in use"):
            RELOAD.unload_pair()
        self.run.assert_not_called()

    def test_apply_failure_rolls_back_and_archives_state(self):
        self.mock("prepare_candidate", return_value=(SAVED, PATHS))
        self.mock("unload_pair")
        self.mock("load_pair", side_effect=RuntimeError("candidate failed"))
        restore = self.mock("restore_original")
        self.stack.enter_context(patch.object(RELOAD.os, "geteuid", return_value=0))
        self.stack.enter_context(patch.object(RELOAD.sys, "argv", [str(SOURCE), "--apply"]))
        with self.assertRaisesRegex(RuntimeError, "candidate failed"):
            RELOAD.main()
        restore.assert_called_once_with(SAVED)
        self.assertFalse(self.state.exists())
        self.assertEqual(len(list(self.root.glob("activation-rolled-back-*.json"))), 1)

    def test_failed_rollback_retains_recovery_state(self):
        self.mock("prepare_candidate", return_value=(SAVED, PATHS))
        self.mock("unload_pair", side_effect=RuntimeError("race"))
        self.mock("restore_original", side_effect=RuntimeError("busy core"))
        self.stack.enter_context(patch.object(RELOAD.os, "geteuid", return_value=0))
        self.stack.enter_context(patch.object(RELOAD.sys, "argv", [str(SOURCE), "--apply"]))
        with self.assertRaisesRegex(RuntimeError, "Recovery state retained"):
            RELOAD.main()
        self.assertEqual(json.loads(self.state.read_text()), SAVED)

    def test_candidate_full_vermagic_mismatch_rejected(self):
        (self.root / "manifest.json").write_text(json.dumps({"running_kernel": "test-kernel"}))
        self.mock("idle_pair")
        self.mock("parameters", side_effect=lambda m: RAW[m])
        self.mock("verify_files", side_effect=[ORIGINAL, CANDIDATE])
        self.mock("verify_active")
        def output(args):
            if args == ["uname", "-r"]:
                return "test-kernel"
            if args[:3] == ["modinfo", "-F", "filename"]:
                return PATHS[args[3]]
            if args[:3] == ["modinfo", "-F", "vermagic"]:
                return "test-kernel SMP preempt" if str(args[3]).startswith("/original/") else "test-kernel SMP"
            self.fail(f"Unexpected command: {args}")
        self.mock("output", side_effect=output)
        with self.assertRaisesRegex(RuntimeError, "Full vermagic differs"):
            RELOAD.prepare_candidate()
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
