#!/usr/bin/env python3
"""Fig9 orchestration tests with synthetic topology/processes, never real VMs."""
import contextlib
import copy
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import chameleon_fig9 as data
import chameleon_fig9_runtime as rt

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fig9_runner_test", HERE / "run-chameleon-fig9.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def inventory(remote=False):
    value = json.loads((HERE.parent / "config/chameleon-fig9-host.example.json").read_text())
    if not remote:
        value['server'].update(manage_local=True,manage_remote=False)
    for i, slot in enumerate(value["slots"]):
        slot["vfio"] = [f"0000:42:0{i}.1"]
    return value


def topology():
    # Two sockets, 40 physical cores each, two SMT siblings per core.
    return [{"cpu": socket * 40 + core + sibling * 80,
             "socket": socket, "node": socket, "core": core}
            for socket in range(2) for core in range(40) for sibling in range(2)]


def apps(mix="mix1"):
    return [data.resolve_high(case) for case in data.MIXES[mix]]


class Resources(unittest.TestCase):
    def test_all_mixes_use_exact_saved_highs_for_three_vms(self):
        rows = topology()
        with mock.patch.object(rt.affinity, "topology", return_value=rows), \
             mock.patch.object(rt.os, "sched_getaffinity", return_value=set(range(160))):
            plan = runner.build_plan(inventory(), list(data.MIXES), data.DEFAULT_QUALIFIED,
                                     HERE.parents[1] / "ae/results_baselines/fig9.json")
        for mix, entry in plan["mixes"].items():
            self.assertEqual([a["application"] for a in entry["applications"]], data.MIXES[mix])
            self.assertEqual(len(entry["applications"]), 3)
            for high in entry["applications"]:
                self.assertEqual(high["configuration"], data.resolve_high(high["application"])["configuration"])
            self.assertEqual(entry["total_vm_memory_mib"], sum(a["vm_memory_mib"] for a in entry["applications"]))

    def test_vcpu_service_and_clients_are_disjoint_physical_cores(self):
        rows = topology()
        identities = {r["cpu"]: (r["socket"], r["core"]) for r in rows}
        nodes = {r["cpu"]: r["node"] for r in rows}
        for mix in data.MIXES:
            with self.subTest(mix=mix):
                placements = rt.allocate_cpus(apps(mix), 1, rows, set(range(160)))
                used = list(placements["client_cpus"])
                self.assertTrue(all(nodes[c] == 1 for c in used))
                for case, cpu in placements["applications"].items():
                    n = data.resolve_high(case)["workload_configuration"]["vcpus"]
                    self.assertEqual(len(cpu["vcpu_host_cpus"]), n)
                    self.assertEqual(len(cpu["qemu_service_cpus"]), 2)
                    self.assertEqual(cpu["guest_application_cpus"], list(range(n)))
                    selected = cpu["vcpu_host_cpus"] + cpu["qemu_service_cpus"]
                    self.assertTrue(all(nodes[c] == cpu["host_numa_node"] for c in selected))
                    if case in ("memcached", "cassandra"):
                        self.assertNotEqual(cpu["host_numa_node"], placements["client_numa_node"])
                    used.extend(selected)
                self.assertEqual(len(used), len(set(used)))
                self.assertEqual(len(used), len({identities[c] for c in used}))

    def test_allowed_cpuset_and_insufficient_cores(self):
        rows = topology()
        allowed = set(range(80, 160))
        placement = rt.allocate_cpus(apps(), 1, rows, allowed)
        selected = placement["client_cpus"] + [cpu for p in placement["applications"].values()
                                                     for cpu in p["vcpu_host_cpus"] + p["qemu_service_cpus"]]
        self.assertTrue(set(selected) <= allowed)
        with self.assertRaisesRegex(ValueError, "Insufficient"):
            rt.allocate_cpus(apps(), 1, rows, set(range(8)))

    def test_duplicate_vf_port_address_and_template_slot_rejected(self):
        rt.validate_inventory(inventory())
        for field in ("name", "ssh_port", "server_port", "rdma_address", "vfio"):
            value = inventory()
            value["slots"][1][field] = value["slots"][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                rt.validate_inventory(value)
        for address in ("192.0.2.11/16", "192.0.2.1/24"):
            value = inventory()
            value["slots"][1]["rdma_address"] = address
            with self.subTest(address=address), self.assertRaises(ValueError):
                rt.validate_inventory(value)
        value = inventory()
        value["slots"][0]["name"] = value["template_vm"]
        with self.assertRaises(ValueError):
            rt.validate_inventory(value)

    def test_exactly_three_slots_and_valid_ports_required(self):
        for n in (2, 4):
            value = inventory()
            value["slots"] = (value["slots"] * 2)[:n]
            with self.subTest(n=n), self.assertRaises(ValueError):
                rt.validate_inventory(value)
        for port in (0, 65536, "9400", True):
            value = inventory()
            value["slots"][0]["server_port"] = port
            with self.subTest(port=port), self.assertRaises(ValueError):
                rt.validate_inventory(value)


class CommandsAndOverlays(unittest.TestCase):
    def test_cassandra_leaf_preserves_high_100ms_and_all_knobs(self):
        high = data.resolve_high("cassandra")
        slot = inventory()["slots"][0]
        placement = {"client_numa_node": 1, "client_cpus": [60, 61]}
        command = runner.leaf_command("synthetic", slot, high, "affinity.json", "workload.json",
                                      "memory.json", "barrier", placement, 3600)
        for key in ("minimum_local_mib", "psi_ppm", "epoch_us", "cold_folios", "sample_period",
                    "cooling_samples", "hhh_interval_ms", "free_pages",
                    "pre_reclaim_headroom_mib", "pre_reclaim_epoch_us"):
            self.assertEqual(command[command.index("--" + key.replace("_", "-")) + 1], str(int(high["configuration"][key])))
        self.assertEqual(command[command.index("--epoch-us") + 1], "100000")
        self.assertIn("--cpu-pinning", command)
        self.assertIn("--all-local-tracking", command)
        self.assertEqual(command[command.index("--client-cpus") + 1], "60,61")
        self.assertEqual(command[command.index("--barrier-member") + 1], "cassandra")
        self.assertEqual(command[command.index("--vm") + 1], slot["name"])

    def test_overlay_configs_are_distinct_and_template_unchanged(self):
        value = inventory()
        high_apps = apps()
        placements = rt.allocate_cpus(high_apps, 1, topology(), set(range(160)))
        template = {"name": "original", "disk": "/synthetic/installed.qcow2", "disk_format": "qcow2",
                    "memory_mib": 1024, "ssh_port": 5123, "vfio": ["0000:41:00.1"],
                    "mac": "52:54:00:11:22:33", "nested": {"keep": [1]}}
        before = copy.deepcopy(template)
        configs = [rt.slot_config(template, slot, high, placements["applications"][high["application"]])
                   for slot, high in zip(value["slots"], high_apps)]
        self.assertEqual(template, before)
        for key in ("name", "disk", "ssh_port"):
            self.assertEqual(len({c[key] for c in configs}), 3)
        self.assertTrue(all(c["disk"] != template["disk"] and c["name"] != template["name"] for c in configs))
        for c, high, slot in zip(configs, high_apps, value["slots"]):
            self.assertEqual(c["memory_mib"], high["vm_memory_mib"])
            self.assertEqual(c["cpus"], high["workload_configuration"]["vcpus"])
            self.assertEqual(c["vfio"], slot["vfio"])
        configs[0]["nested"]["keep"].append(2)
        self.assertEqual(template, before)

    def test_preparation_creates_only_independent_overlays_and_access_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source_disk = directory / "source.qcow2"
            source_disk.write_bytes(b"synthetic-template-marker")
            template = {"disk": str(source_disk), "disk_format": "qcow2"}
            config_file = directory / "template.json"
            config_file.write_text(json.dumps(template))
            identity = directory / "synthetic-key"
            identity.write_text("not-a-real-key")
            access = {"name": "template", "vm_config": str(config_file), "identity_file": str(identity), "user": "ubuntu"}
            value = inventory()
            before = config_file.read_bytes(), source_disk.read_bytes()
            commands = []
            def create_overlay(argv, **kwargs):
                commands.append(argv)
                Path(argv[-1]).write_bytes(b"synthetic-overlay")
            with mock.patch.object(rt, "HA", directory / "ha"), \
                 mock.patch.object(rt.guest, "active", return_value=False), \
                 mock.patch.object(rt.guest, "launcher_idle", return_value=True), \
                 mock.patch.object(rt.subprocess, "run", side_effect=create_overlay):
                rt.prepare_slots(value, access, [{"name": s["name"]} for s in value["slots"]])
                for slot in value["slots"]:
                    own = directory / "ha/build/guests" / slot["name"]
                    saved = json.loads((own / "access.json").read_text())
                    self.assertEqual(saved["port"], slot["ssh_port"])
                    self.assertTrue((own / "disk.qcow2").exists())
                rt.prepare_slots(value, access, [{"name": s["name"]} for s in value["slots"]])
                changed = copy.deepcopy(value)
                changed["slots"][0]["ssh_port"] += 100
                with self.assertRaisesRegex(ValueError, "SSH identity/port differs"):
                    rt.prepare_slots(changed, access, [{"name": s["name"]} for s in changed["slots"]])
                saved = json.loads((directory / "ha/build/guests" / value["slots"][0]["name"] / "access.json").read_text())
                self.assertEqual(saved["port"], value["slots"][0]["ssh_port"])
            self.assertEqual(len(commands), 3)  # Reusing the same overlays does not create/overwrite disks.
            self.assertTrue(all(command[command.index("-b") + 1] == str(source_disk) for command in commands))
            self.assertEqual((config_file.read_bytes(), source_disk.read_bytes()), before)


class BarrierAndCleanup(unittest.TestCase):
    def test_release_only_after_all_three_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            barrier = Path(tmp)
            members = data.MIXES["mix1"]
            children = {case: mock.Mock(poll=mock.Mock(return_value=None)) for case in members}
            for case in members[:2]:
                (barrier / (case + ".ready.json")).write_text(json.dumps({"case": case}))
            def ready_third(_):
                self.assertFalse((barrier / "RELEASE").exists())
                (barrier / (members[2] + ".ready.json")).write_text(json.dumps({"case": members[2]}))
            with mock.patch.object(runner.time, "sleep", side_effect=ready_third) as sleep:
                record = runner.wait_ready(barrier, children, 10)
            self.assertEqual(sleep.call_count, 1)
            self.assertEqual(set(record["ready"]), set(members))
            self.assertEqual(set(json.loads((barrier / "RELEASE").read_text())["members"]), set(members))

    def test_exited_child_aborts_even_if_all_ready_files_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            barrier = Path(tmp)
            children = {case: mock.Mock(poll=mock.Mock(return_value=None)) for case in data.MIXES["mix1"]}
            for case in children:
                (barrier / (case + ".ready.json")).write_text("{}")
            next(iter(children.values())).poll.return_value = 7
            with self.assertRaisesRegex(RuntimeError, "before synchronized start"):
                runner.wait_ready(barrier, children, 10)
            self.assertFalse((barrier / "RELEASE").exists())

    def test_missing_member_times_out_and_stop_does_not_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            barrier = Path(tmp)
            children = {case: mock.Mock(poll=mock.Mock(return_value=None)) for case in data.MIXES["mix1"]}
            with mock.patch.object(runner.time, "monotonic", side_effect=[0, 1]):
                with self.assertRaises(TimeoutError):
                    runner.wait_ready(barrier, children, 0)
            with self.assertRaises(KeyboardInterrupt):
                runner.wait_ready(barrier, children, 10, stopped=lambda: True)
            self.assertFalse((barrier / "RELEASE").exists())

    def test_partial_boot_failure_stops_owned_vm_restores_config_and_records_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = inventory()
            high_apps = apps()
            placement = rt.allocate_cpus(high_apps, 1, topology(), set(range(160)))
            records = {}
            for slot in value["slots"]:
                f = root / (slot["name"] + ".json")
                f.write_text('{"original": true}\n')
                records[slot["name"]] = {"name": slot["name"], "vm_config": str(f)}
            exists = Path.exists
            def test_exists(p):
                return False if str(p) == "/run/numad.pid" else exists(p)
            with mock.patch.object(rt, "access", side_effect=lambda name: records[name]), \
                 mock.patch.object(rt.guest, "control_lock", side_effect=lambda _: contextlib.nullcontext()), \
                 mock.patch.object(rt.guest, "start", side_effect=RuntimeError("synthetic boot failure")), \
                 mock.patch.object(rt.guest, "active", return_value=True), \
                 mock.patch.object(rt, "stop_guest") as stop, \
                 mock.patch.object(Path, "exists", test_exists), \
                 mock.patch.object(runner.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "synthetic boot failure"):
                    runner.run_mix("mix1", 1, {"applications": high_apps, "cpus": placement}, value,
                                   {"disk": "/synthetic/disk", "disk_format": "qcow2"}, root, "synthetic", 60)
            popen.assert_not_called()
            stop.assert_called_once_with(records[value["slots"][0]["name"]], timeout=180)
            for access in records.values():
                self.assertEqual(Path(access["vm_config"]).read_text(), '{"original": true}\n')
            output = root / "mix1/repeat-01"
            self.assertTrue((output / "barrier/ABORT").exists())
            self.assertFalse((output / "barrier/RELEASE").exists())
            self.assertEqual(json.loads((output / "report.json").read_text())["status"], "FAIL")

    def test_overlap_excludes_pre_barrier_time_and_requires_common_interval(self):
        records = [{"workload_start_unix_seconds": a, "workload_end_unix_seconds": b}
                   for a, b in [(90, 130), (95, 120), (105, 140)]]
        self.assertEqual(runner.overlap(records, 100)["common_seconds"], 15)
        records[0]["workload_end_unix_seconds"] = 103
        with self.assertRaisesRegex(ValueError, "No common"):
            runner.overlap(records, 100)

    def test_client_timed_interval_overrides_longer_launcher_envelope(self):
        records = [{"workload_start_unix_seconds": 90, "workload_end_unix_seconds": 200},
                   {"workload_start_unix_seconds": 90, "workload_end_unix_seconds": 140},
                   {"workload_start_unix_seconds": 100, "workload_end_unix_seconds": 145}]
        self.assertEqual(runner.overlap(records, 100)["common_seconds"], 40)
        # The first launcher's load/warmup creates an apparent overlap, but its
        # actual timed READ/generator begins after the two batch jobs end.
        records[0].update(timed_phase_start_unix_seconds=150, timed_phase_end_unix_seconds=180)
        with self.assertRaisesRegex(ValueError, "No common"):
            runner.overlap(records, 100)
        records[0].update(timed_phase_start_unix_seconds=110, timed_phase_end_unix_seconds=130)
        result = runner.overlap(records, 100)
        self.assertEqual(result["common_seconds"], 20)
        self.assertEqual(result["start_unix_seconds"], 110)
        self.assertEqual(result["end_unix_seconds"], 130)


class ServerLifecycle(unittest.TestCase):
    def test_three_ready_servers_have_distinct_ports_8gib_and_all_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = inventory()
            binary = root / "synthetic-rdma-server"
            binary.touch()
            value["server"]["binary"] = str(binary)
            children, commands, streams = [], [], []
            def start(command, stdout, **kwargs):
                commands.append(command)
                streams.append(stdout)
                child = mock.Mock(poll=mock.Mock(return_value=None))
                children.append(child)
                stdout.write("READY Hermit RDMA synthetic-test-only\n")
                stdout.flush()
                return child
            with mock.patch.object(rt.subprocess, "Popen", side_effect=start):
                with rt.servers(value, root) as running:
                    self.assertEqual(running, children)
                    self.assertEqual(len(running), 3)
                    for child in children:
                        child.terminate.assert_not_called()
                for child in children:
                    child.terminate.assert_called_once_with()
                    child.wait.assert_called_once_with(timeout=20)
            self.assertEqual({command[-2] for command in commands}, {str(s["server_port"]) for s in value["slots"]})
            self.assertTrue(all(command[-1] == "8192" and command[-3] == value["server"]["address"] for command in commands))
            self.assertTrue(all(stream.closed for stream in streams))

    def test_startup_process_failure_cleans_started_servers_and_streams(self):
        for failure in ("exited-before-ready", "spawn-exception"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                value = inventory()
                binary = root / "synthetic-rdma-server"
                binary.touch()
                value["server"]["binary"] = str(binary)
                children, streams = [], []
                def start(command, stdout, **kwargs):
                    streams.append(stdout)
                    if len(streams) == 3 and failure == "spawn-exception":
                        raise OSError("synthetic spawn failure")
                    failed = len(streams) == 3
                    child = mock.Mock(poll=mock.Mock(return_value=7 if failed else None))
                    children.append(child)
                    if not failed:
                        stdout.write("READY Hermit RDMA synthetic-test-only\n")
                        stdout.flush()
                    return child
                with mock.patch.object(rt.subprocess, "Popen", side_effect=start), \
                     mock.patch.object(rt, "stop_process", wraps=rt.stop_process) as cleanup:
                    with self.assertRaises((RuntimeError, OSError)):
                        with rt.servers(value, root):
                            self.fail("Failed startup must never yield a running-server context")
                self.assertEqual(cleanup.call_count, len(children))
                for child in children[:2]:
                    child.terminate.assert_called_once_with()
                    child.wait.assert_called_once_with(timeout=20)
                if failure == "exited-before-ready":
                    children[2].terminate.assert_not_called()  # It already exited.
                self.assertTrue(all(stream.closed for stream in streams))

    def test_workload_exception_still_stops_all_three_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = inventory()
            binary = root / "synthetic-rdma-server"
            binary.touch()
            value["server"]["binary"] = str(binary)
            children = []
            def start(command, stdout, **kwargs):
                child = mock.Mock(poll=mock.Mock(return_value=None))
                children.append(child)
                stdout.write("READY Hermit RDMA synthetic-test-only\n")
                stdout.flush()
                return child
            with mock.patch.object(rt.subprocess, "Popen", side_effect=start):
                with self.assertRaisesRegex(RuntimeError, "synthetic workload failure"):
                    with rt.servers(value, root):
                        raise RuntimeError("synthetic workload failure")
            self.assertEqual(len(children), 3)
            for child in children:
                child.terminate.assert_called_once_with()
                child.wait.assert_called_once_with(timeout=20)


class RemoteServers(unittest.TestCase):
    def test_remote_location_is_exclusive_and_requires_absolute_binary(self):
        rt.validate_inventory(inventory(remote=True))
        for change in ({'manage_local':True},{'binary':'relative/server'},
                       {'ssh_host':'-oProxyCommand=bad'},{'command_prefix':['sudo','-n']}):
            value=inventory(remote=True);value['server'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):
                rt.validate_inventory(value)

    def test_ssh_quotes_remote_paths_and_program_as_literal_arguments(self):
        config=inventory(remote=True)['server']
        args=['/tmp/server with spaces;literal','192.0.2.1','9401']
        argv=rt.server_ssh_command(config,'print("literal $ and backticks `")',args)
        self.assertEqual(argv[-2],'memory-server')
        remote=shlex.split(argv[-1])
        self.assertEqual(remote[-3:],args)
        self.assertEqual(remote[:3],['python3','-u','-c'])

    def test_remote_preflight_checks_three_pools_on_remote_host(self):
        value=inventory(remote=True)
        with mock.patch.object(rt.subprocess,'run',return_value=mock.Mock(stdout='{"status":"PASS"}')) as run:
            self.assertEqual(rt.check_remote_server(value),{'status':'PASS'})
        command=run.call_args.args[0]
        remote=shlex.split(command[-1])
        self.assertEqual(remote[-3:],[value['server']['binary'],'192.0.2.1',str(3*8192+1024)])
        self.assertIn('memory-server',command)

    def test_real_supervisor_stops_only_its_child_on_eof_or_lease_expiry(self):
        unrelated=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        try:
            for eof in (True,False):
                with self.subTest(eof=eof),tempfile.TemporaryDirectory() as tmp:
                    marker=Path(tmp)/'owned.pid'
                    program='import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)'
                    supervisor=subprocess.Popen([sys.executable,'-u','-c',rt.REMOTE_SUPERVISOR,
                        json.dumps([sys.executable,'-c',program,str(marker)]),'.5'],
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    try:
                        deadline=time.monotonic()+2
                        while not marker.exists():
                            if time.monotonic()>deadline:self.fail('child did not start')
                            time.sleep(.01)
                        owned=int(marker.read_text())
                        if eof:
                            supervisor.stdin.close();supervisor.stdin=None
                        supervisor.wait(timeout=3)
                        output=supervisor.stdout.read().decode()
                        self.assertIn('STOPPED Fig9 remote',output)
                        self.assertIn('ssh_stdin_closed' if eof else 'heartbeat_expired',output)
                        self.assertFalse(Path('/proc',str(owned)).exists())
                        self.assertIsNone(unrelated.poll())
                    finally:
                        if supervisor.poll() is None:supervisor.kill();supervisor.wait()
                        for stream in [supervisor.stdin,supervisor.stdout,supervisor.stderr]:
                            if stream and not stream.closed:stream.close()
        finally:
            unrelated.terminate();unrelated.wait()

    def test_remote_context_three_real_fake_servers_stop_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            binary=directory/'fake server'
            binary.write_text('#!'+sys.executable+'\nimport time\nprint("READY Hermit RDMA synthetic",flush=True)\ntime.sleep(30)\n')
            binary.chmod(0o700)
            value=inventory(remote=True);value['server']['binary']=str(binary)
            commands=[]
            def local_supervisor(config,program,args):
                commands.append(args)
                return [sys.executable,'-u','-c',program,*map(str,args)]
            with mock.patch.object(rt,'server_ssh_command',side_effect=local_supervisor):
                with self.assertRaisesRegex(RuntimeError,'synthetic workload failure'):
                    with rt.servers(value,directory) as children:
                        self.assertEqual(len(children),3)
                        rt.assert_servers(children)
                        raise RuntimeError('synthetic workload failure')
            self.assertEqual({json.loads(c[0])[-2] for c in commands},{'9401','9402','9403'})
            for child in children:self.assertEqual(child.returncode,0)
            for logfile in directory.glob('*-rdma-server.log'):
                self.assertIn('requested_stop',logfile.read_text())

    def test_server_exit_aborts_readiness_before_releasing_workloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            failed=mock.Mock(pid=123,poll=mock.Mock(return_value=7))
            with self.assertRaisesRegex(RuntimeError,'Owned RDMA server/session exited'):
                runner.wait_ready(directory,{},10,server_processes=[failed])
            self.assertFalse((directory/'RELEASE').exists())


if __name__ == "__main__":
    unittest.main()
