#!/usr/bin/env python3
"""Three small independent Guests: physical RDMA connectivity and data smoke.

This does not run Figure 9 workloads or change their saved high configurations.
Default --plan is read-only; --run owns only the inventory's three offline slots.
"""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import chameleon_fig9_runtime as rt
import chameleon_fig9 as data

HERE = Path(__file__).resolve().parent
R3_BINARY = rt.HA / "tests/chameleon_swap_tracking"
GUEST_SNAPSHOT = r'''
import json,pathlib,platform
root=pathlib.Path('/sys/kernel/debug')
def stats(name):
    values={}
    for line in (root/name/'stats').read_text().splitlines():
        fields=line.split()
        if len(fields)==2:
            key,value=fields
            try:value=int(value,0)
            except ValueError:pass
            values[key]=value
    return values
params=pathlib.Path('/sys/module/rswap_client/parameters')
needed=['chameleon/inject','chameleon/control','chameleon_mm/control','chameleon_shadow/control','chameleon_policy/control','hermit/control']
print(json.dumps({'kernel':platform.release(),'hermit':stats('hermit'),'rdma':stats('hermit_rdma'),
 'parameters':{n:(params/n).read_text().strip() for n in ['backend','sip','sport','pool_mb']},
 'r3_controls_available':all((root/n).exists() for n in needed)}))
'''


def build_plan(inventory, memory_mib=4096, vcpus=2, topology=None, allowed=None, cpu_mix=None):
    rt.validate_inventory(inventory)
    if memory_mib < 2048 or vcpus < 1:
        raise ValueError("Smoke Guests need at least 2048MiB and one vCPU")
    topology = rt.affinity.topology() if topology is None else topology
    remaining = set(os.sched_getaffinity(0) if allowed is None else allowed)
    placement = None
    if cpu_mix:
        qualified = rt.ROOT / 'ae/config/fig9-highs.json'
        if not qualified.is_file():
            qualified = data.DEFAULT_QUALIFIED
        placement = rt.allocate_cpus([data.resolve_high(case, qualified) for case in data.MIXES[cpu_mix]],
                                     inventory['client_numa_node'], topology, remaining)
    slots = []
    for index, slot in enumerate(inventory["slots"]):
        cpu = None
        if placement:
            case = data.MIXES[cpu_mix][index]
            cpu = placement['applications'][case]
        for node in sorted({r["node"] for r in topology}):
            if cpu is not None:
                break
            try:
                cpu = rt.affinity.select_cores(topology, remaining, node, vcpus)
                break
            except ValueError:
                pass
        if cpu is None:
            raise ValueError("Insufficient disjoint physical cores for three smoke Guests")
        # Exclude SMT siblings of selected cores from later VM placements.
        selected = set(cpu["vcpu_host_cpus"] + cpu["qemu_service_cpus"])
        identities = {(r["socket"], r["core"]) for r in topology if r["cpu"] in selected}
        remaining -= {r["cpu"] for r in topology if (r["socket"], r["core"]) in identities}
        slots.append({"slot": dict(slot), "cpu_affinity": cpu, "vm_memory_mib": memory_mib,
                      "application": data.MIXES[cpu_mix][index] if cpu_mix else slot['name'],
                      "workload_configuration": {"vcpus": len(cpu['vcpu_host_cpus']), "args": []}})
    return {"protocol": "fig9-three-vm-rdma-smoke-v1", "measurement_kind": "functional smoke, not performance",
            "template_vm": inventory["template_vm"], "slots": slots,
            "remote_pool_mib_per_vm": 8192,
            "data_probe": "existing chameleon_swap_tracking --without-pebs; order0/4/9 content/PFN round trips",
            "saved_high_configurations_modified": False,
            "cpu_mix": cpu_mix, "placement": placement}


def reconnect(access, slot, server, count, output):
    helper = '/home/' + access['user'] + '/chameleon-tools/hermit-guest.py'
    attempts = []
    for number in range(count):
        rt.remote(access, ['sudo', '-n', 'python3', helper, 'stop'])
        started = time.time()
        result = json.loads(rt.remote(access, ['sudo', '-n', 'python3', helper,
            'start', '--server', server['address'], '--port', str(slot['server_port']),
            '--pool-mib', '8192', '--interface', slot['rdma_interface']]))
        verify_connection(snapshot(access), slot, server)
        attempts.append({'number': number + 1, 'started': started, 'finished': time.time(), 'result': result})
        rt.save(output / (access['name'] + '-reconnections.json'), attempts)
    return attempts


def client_probe(inventory, plan, output):
    """Short real generator check with small cache, never a performance point."""
    spec = next(s for s in plan['slots'] if s['application'] == 'memcached')
    a = rt.access(spec['slot']['name'])
    p = plan['placement']
    env = dict(os.environ, MEMCACHED_GUEST_NAME=a['name'],
        MEMCACHED_GUEST_DIR=str(a['_path'].parent),
        MEMCACHED_QEMU_RUN_DIR=str(rt.guest.run_dir(a)),
        MEMCACHED_GUEST_USER=a['user'], MEMCACHED_GUEST_HOST=a['host'],
        MEMCACHED_GUEST_SSH_PORT=str(a['port']),
        MEMCACHED_GUEST_CONTROL='/home/' + a['user'] + '/chameleon-benchmarks/scripts/memcached-guest.sh')
    command = ['bash', str(HERE / 'run-memcached.sh'), '--guest-numa-node', str(spec['cpu_affinity']['host_numa_node']),
        '--guest-cpus', rt.affinity.cpu_list(spec['cpu_affinity']['guest_application_cpus']),
        '--client-numa-node', str(p['client_numa_node']), '--client-cpus', rt.affinity.cpu_list(p['client_cpus']),
        '--memory-mib', '128', '--minimum-available-mib', '512', '--runtime', '10', '--rampup', '0',
        '--mpps', '0.001', '--workers', '8', '--rx-threads', '2', '--producer-shards', '2',
        '--output-base', str(output / 'client-probe')]
    with (output / 'client-probe.log').open('w') as log:
        result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120)
    if result.returncode:
        raise RuntimeError('Real generator affinity probe failed; see client-probe.log')
    return {'status': 'PASS', 'command': command,
            'scope': '10-second 128MiB cache connectivity/CPU check; does not alter saved high or measure Fig9'}


def snapshot(access):
    return json.loads(rt.remote(access, ["sudo", "-n", "python3", "-c", GUEST_SNAPSHOT]))


def verify_connection(state, slot, server):
    hermit, rdma, params = state["hermit"], state["rdma"], state["parameters"]
    if hermit.get("backend") != "rdma" or hermit.get("registered") != 1:
        raise ValueError("Guest has no registered physical RDMA Hermit backend")
    if hermit.get("capacity_pages") != (8192 << 20) // 4096:
        raise ValueError("Guest remote pool does not equal 8192MiB")
    expected = {"backend": "rdma", "sip": server["address"], "sport": str(slot["server_port"]), "pool_mb": "8192"}
    if params != expected or rdma.get("broken") != 0:
        raise ValueError("Guest connected to wrong/broken RDMA pool: " + str(params))


def verify_round_trip(before, after, log, qmp, memory_mib):
    marker = re.search(r"^PASS CHAMELEON_R3 checks=(\d+) mode=DETERMINISTIC_ONLY$", log, re.M)
    if not marker:
        raise ValueError("R3 content/PFN verification did not complete")
    delta = {group: {key: after[group][key] - before[group][key] for key in keys}
             for group, keys in {
                 "hermit": ["store_success", "load_success", "bytes_written", "bytes_read", "store_failures", "load_failures"],
                 "rdma": ["write_bytes", "read_bytes", "write_completions", "read_completions", "transfer_errors", "map_failures"]}.items()}
    if any(delta["rdma"][key] <= 0 for key in ("write_bytes", "read_bytes", "write_completions", "read_completions")):
        raise ValueError("No actual completed RDMA writes and reads")
    if any(delta["hermit"][key] <= 0 for key in ("store_success", "load_success", "bytes_written", "bytes_read")):
        raise ValueError("No Hermit data round trip")
    if after["rdma"].get("broken") != 0 or any(delta["rdma"][key] for key in ("transfer_errors", "map_failures")):
        raise ValueError("Physical RDMA transport errors during smoke")
    if any(after["hermit"].get(key) != 0 for key in ("live_slots", "allocated_pages", "inflight")):
        raise ValueError("Guest remote resources did not drain")
    ch = qmp["chameleon"]
    if qmp["actual"] != memory_mib * (1 << 20) or any(ch[key] for key in ("range-records", "retired-pages", "blocked-pages")):
        raise ValueError("Host backing resources did not restore")
    return {"status": "PASS", "checks": int(marker[1]), "counter_deltas": delta,
            "scope": "R3 verified actual saved data and PFN restoration; PEBS sampling is not tested",
            "injected_failure_note": "R3 deliberately injects Hermit store/load failures; raw counters retained separately from RDMA transport errors"}


def data_probe(access, spec, output, timeout, mode):
    before = snapshot(access)
    record = {"before": before}
    if mode == "off" or not before["r3_controls_available"] or not R3_BINARY.is_file():
        reason = "explicitly disabled" if mode == "off" else "R3 test binary or Guest test controls unavailable"
        if mode == "required":
            raise ValueError(reason)
        record.update(status="SKIPPED", reason=reason)
        return record
    binary = "/tmp/chameleon-fig9-smoke-r3"
    rt.guest.transfer(access, "upload", R3_BINARY, binary)
    rt.remote(access, ["chmod", "+x", binary])
    rt.guest.qmp(access, "chameleon-configure", {"config": {"batch-pages": 1, "watermark-bytes": 0, "ept-mode": "deferred"}})
    command = ["sudo", "-n", "timeout", str(timeout), "taskset", "-c",
               ",".join(map(str, spec["cpu_affinity"]["guest_application_cpus"])), "env",
               "CHAMELEON_TEST_RAM_BYTES=" + str(spec["vm_memory_mib"] * (1 << 20)), binary, "--without-pebs"]
    record["command"] = command
    try:
        log = rt.remote(access, command, timeout=timeout + 15)
    except subprocess.CalledProcessError as error:
        (output / (access["name"] + "-r3.log")).write_text((error.stdout or "") + (error.stderr or ""))
        raise
    (output / (access["name"] + "-r3.log")).write_text(log)
    after = snapshot(access)
    qmp = rt.guest.qmp(access, "query-llfree-balloon")
    record.update(after=after, qmp_after=qmp, **verify_round_trip(before, after, log, qmp, spec["vm_memory_mib"]))
    return record


def _start_guard(access, output, timeout, guards, streams):
    if not Path("/run/numad.pid").exists():
        return
    stream = (output / (access["name"] + "-numad.log")).open("w")
    streams.append(stream)
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    child = subprocess.Popen([*prefix, sys.executable, str(HERE / "guard-qemu-numad.py"),
                              "--run-dir", str(rt.guest.run_dir(access)), "--stop-file", str(output / "STOP_GUARDS"),
                              "--duration", str(timeout + 1800)], stdout=stream, stderr=subprocess.STDOUT)
    guards.append(child)
    deadline = time.monotonic() + 15
    while "READY" not in Path(stream.name).read_text():
        if child.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("NUMA guard did not initialize")
        time.sleep(.1)


def run_owned(inventory, plan, source, output, timeout, read_write, reconnects=0, with_client=False):
    """Own VM startup through teardown inside rt.servers (local or remote)."""
    record = {"status": "RUNNING", "plan": plan, "slots": {}, "started_unix_seconds": time.time()}
    owned, originals, guards, streams, cleanup_errors = [], {}, [], [], []
    try:
        with rt.servers(inventory, output) as pools:
            try:
                for spec in plan["slots"]:
                    slot = spec["slot"]
                    access = rt.access(slot["name"])
                    with rt.guest.control_lock(access):
                        if rt.guest.active(access) or not rt.guest.launcher_idle(access):
                            raise ValueError("Smoke refuses an active slot: " + slot["name"])
                        originals[slot["name"]] = (Path(access["vm_config"]), Path(access["vm_config"]).read_text())
                        rt.save(Path(access["vm_config"]), rt.slot_config(source, slot, spec, spec["cpu_affinity"]))
                        _start_guard(access, output, timeout, guards, streams)
                        owned.append(access)  # Partial boots also belong to this invocation.
                        rt.guest.start(access, timeout=300)
                        connection = rt.setup_guest(access, slot, inventory["server"])
                        pid = int((rt.guest.run_dir(access) / "qemu.pid").read_text())
                        _, placement = rt.affinity.apply(pid, rt.guest.qmp(access, "query-cpus-fast"), spec["cpu_affinity"])
                    helper = "/home/" + access["user"] + "/chameleon-tools/hermit-guest.py"
                    native = json.loads(rt.remote(access, ["sudo", "-n", "python3", helper, "check", "--server", inventory["server"]["address"],
                                                          "--port", str(slot["server_port"]), "--pool-mib", "8192", "--interface", slot["rdma_interface"]]))
                    if native.get("status") != "PASS":
                        raise ValueError("Physical mlx5 interface validation failed")
                    state = snapshot(access)
                    verify_connection(state, slot, inventory["server"])
                    record["slots"][slot["name"]] = {"status": "CONNECTED", "native_device": native,
                                                         "hermit_connection": connection,
                                                         "connection": state, "cpu_affinity": placement}
                    rt.save(output / "report.json", record)
                # Keep all three independent VMs/pools connected while their
                # existing correctness probes execute concurrently.
                if any(pool.poll() is not None for pool in pools):
                    raise RuntimeError("An RDMA server exited before the data probes")
                if reconnects:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                        jobs = {executor.submit(reconnect, access, spec['slot'], inventory['server'], reconnects, output): access['name']
                                for access, spec in zip(owned, plan['slots'])}
                        for job in concurrent.futures.as_completed(jobs):
                            record['slots'][jobs[job]]['reconnections'] = job.result()
                guests = {spec['application']: access for spec, access in zip(plan['slots'], owned)}
                if plan.get('placement'):
                    record['cpu_before'] = rt.verify_vm_cpu_isolation(plan['placement'], guests)
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {executor.submit(data_probe, access, spec, output, timeout, read_write): access["name"]
                               for access, spec in zip(owned, plan["slots"])}
                    if with_client:
                        futures[executor.submit(client_probe, inventory, plan, output)] = 'client_probe'
                    failed = []
                    pending = set(futures)
                    while pending:
                        if plan.get('placement'):
                            record['cpu_latest'] = rt.verify_vm_cpu_isolation(plan['placement'], guests)
                            with (output / 'cpu-isolation.jsonl').open('a') as stream:
                                stream.write(json.dumps({'time': time.time(), **record['cpu_latest']}) + '\n')
                        ready, pending = concurrent.futures.wait(pending, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED)
                        for future in ready:
                            name = futures[future]
                            try:
                                result = future.result()
                                if name == 'client_probe':
                                    record[name] = result
                                else:
                                    record['slots'][name].update(status='PASS' if result['status'] == 'PASS' else 'CONNECTED_ONLY', data_probe=result)
                            except Exception as error:
                                if name == 'client_probe': record[name] = {'status': 'FAIL', 'error': repr(error)}
                                else: record['slots'][name].update(status='FAIL', error=repr(error))
                                failed.append(name)
                            rt.save(output / 'report.json', record)
                    if failed:
                        raise RuntimeError("Data probes failed: " + ",".join(failed))
                if any(pool.poll() is not None for pool in pools) or any(g.poll() is not None for g in guards):
                    raise RuntimeError("RDMA server or NUMA guard exited during smoke")
                record["status"] = "PASS" if all(v["status"] == "PASS" for v in record["slots"].values()) else "CONNECTED_ONLY"
            finally:
                for access in reversed(owned):
                    try:
                        with rt.guest.control_lock(access):
                            if rt.guest.active(access):
                                rt.stop_guest(access, timeout=180)
                    except Exception as error:
                        cleanup_errors.append(access["name"] + ": " + repr(error))
                for path, text in originals.values():
                    try:
                        path.write_text(text)
                    except Exception as error:
                        cleanup_errors.append(str(path) + ": " + repr(error))
                for access in owned:
                    diagnosis = rt.guest.run_dir(access) / 'hermit-connection.json'
                    if diagnosis.exists():
                        (output / (access['name'] + '-hermit-connection.json')).write_text(diagnosis.read_text())
                (output / "STOP_GUARDS").write_text("done\n")
                for guard in guards:
                    try:
                        guard.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        cleanup_errors.append("NUMA guard did not stop")
                for stream in streams:
                    stream.close()
    except BaseException as error:
        record.update(status="CANCELLED" if isinstance(error, KeyboardInterrupt) else "FAIL", error=repr(error))
        raise
    finally:
        record["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            record["status"] = "FAIL"
        record["finished_unix_seconds"] = time.time()
        rt.save(output / "report.json", record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--memory-mib", type=int, default=4096)
    parser.add_argument("--vcpus", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--directory", type=Path, default=rt.ROOT / "benchmarks/results/chameleon" / ("fig9-rdma-smoke-" + time.strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--read-write", choices=("auto", "required", "off"), default="auto")
    parser.add_argument('--cpu-mix', choices=list(data.MIXES), help='Use this mix CPU placement with small smoke RAM')
    parser.add_argument('--reconnects', type=int, default=0, help='Reconnect each owned Guest pool this many times before data checks')
    parser.add_argument('--client-probe', action='store_true', help='Run a short real Memcached generator with the mix client CPU assignment')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout < 30:
        parser.error("--timeout must be at least30 seconds")
    if not 0 <= args.reconnects <= 10:
        parser.error('--reconnects must be 0..10')
    if args.client_probe and args.cpu_mix not in ('mix1', 'mix2', 'mix3'):
        parser.error('--client-probe requires a Memcached --cpu-mix')
    inventory = json.loads(args.inventory.read_text())
    plan = build_plan(inventory, args.memory_mib, args.vcpus, cpu_mix=args.cpu_mix)
    plan.update(reconnects=args.reconnects, client_probe=args.client_probe)
    if not args.run:
        print(json.dumps(plan, indent=2)); return
    if args.read_write == "required" and not R3_BINARY.is_file():
        raise ValueError("Build hyperalloc-6.18/tests/chameleon_swap_tracking before required data smoke")
    rt.validate_inventory(inventory, hardware=True)
    template = rt.access(inventory["template_vm"])
    with rt.guest.control_lock(template):
        if rt.guest.active(template) or not rt.guest.launcher_idle(template):
            raise ValueError("Template VM must be offline before using its overlay backing")
        source = json.loads(Path(template["vm_config"]).read_text())
        if inventory["server"].get("manage_remote"):
            plan["remote_server_preflight"] = rt.check_remote_server(inventory)
        available_mib = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                                 if line.startswith("MemAvailable:"))) // 1024
        required_mib = sum(spec["vm_memory_mib"] for spec in plan["slots"]) + 4096
        if inventory["server"].get("manage_local"):
            required_mib += 3 * 8192
        if available_mib < required_mib:
            raise ValueError(f"Smoke needs{required_mib}MiB Host available memory; has{available_mib}")
        for spec in plan["slots"]:
            slot = spec["slot"]
            if (rt.HA / "build/guests" / slot["name"] / "access.json").exists():
                access = rt.access(slot["name"])
                if rt.guest.active(access) or not rt.guest.launcher_idle(access):
                    raise ValueError("Smoke refuses an already active slot: " + slot["name"])
            rt.free_tcp_port(slot["ssh_port"])
        configs = [rt.slot_config(source, spec["slot"], spec, spec["cpu_affinity"]) for spec in plan["slots"]]
        for config in configs:
            check = dict(config, disk=source["disk"], disk_format=source["disk_format"])
            rt.deploy.preflight(rt.deploy.config(check))
        rt.prepare_slots(inventory, template, configs)
        args.directory.mkdir(parents=True, exist_ok=False)
        rt.save(args.directory / "plan.json", plan)
        def interrupted(signum, frame):
            raise KeyboardInterrupt("signal " + str(signum))
        signal.signal(signal.SIGTERM, interrupted)
        result = run_owned(inventory, plan, source, args.directory, args.timeout, args.read_write,
                           args.reconnects, args.client_probe)
        print(json.dumps({"status": result["status"], "report": str((args.directory / "report.json").resolve())}))
        if result["status"] == "FAIL":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
