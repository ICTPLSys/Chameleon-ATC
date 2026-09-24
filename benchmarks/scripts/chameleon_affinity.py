"""Deterministic physical-core placement for benchmark QEMU threads."""
import os
from pathlib import Path
import re
import json
import signal
import subprocess
import time

PROTOCOL = 'physical-core-vcpu-and-guest-taskset-v1'


def select_cores(topology, allowed, node, vcpus):
    cores = {}
    for row in sorted(topology, key=lambda r: r['cpu']):
        if row['cpu'] in allowed and row['node'] == node:
            cores.setdefault((row['socket'], row['core']), row['cpu'])
    cpus = list(cores.values())
    if len(cpus) < vcpus + 2:
        raise ValueError('Need one physical core per vCPU and two separate QEMU service cores')
    return {'protocol': PROTOCOL, 'host_numa_node': node,
            'vcpu_host_cpus': cpus[:vcpus], 'qemu_service_cpus': cpus[vcpus:vcpus+2],
            'guest_application_cpus': list(range(vcpus))}


def cpu_list(cpus):
    return ','.join(map(str, cpus))


def validate_isolation(groups, rows=None):
    """Reject overlap through logical CPU identity or SMT across named groups."""
    rows = topology() if rows is None else rows
    by_cpu = {row['cpu']: row for row in rows}
    owners = {}
    evidence = {}
    for name, cpus in groups.items():
        if not cpus or any(type(cpu) is not int or cpu < 0 for cpu in cpus):
            raise ValueError('Invalid/nonempty CPU allocation required: ' + name)
        cores = []
        for cpu in cpus:
            if cpu not in by_cpu:
                raise ValueError('CPU is absent from online topology: ' + str(cpu))
            row = by_cpu[cpu]
            physical = (row['socket'], row['core'])
            if physical in owners:
                raise ValueError('Physical core shared through CPU/SMT: ' + str(physical) +
                                 ' by ' + owners[physical] + ' and ' + name)
            owners[physical] = name
            cores.append({'cpu': cpu, 'socket': row['socket'], 'core': row['core'], 'node': row['node']})
        evidence[name] = cores
    return {'status': 'PASS', 'groups': evidence}


def validate_client_cpus(value, node, rows=None, allowed=None):
    """Return explicit client CPUs, confined to the declared node and cpuset."""
    if not re.fullmatch(r'[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*', value):
        raise ValueError('Invalid client CPU list')
    cpus=[]
    for part in value.split(','):
        ends=[int(v) for v in part.split('-')]
        first,last=ends[0],ends[-1]
        if last<first or last-first>1048576:
            raise ValueError('Invalid client CPU range')
        cpus.extend(range(first,last+1))
    if len(cpus)!=len(set(cpus)):
        raise ValueError('Client CPU list contains duplicates')
    rows=topology() if rows is None else rows
    allowed=os.sched_getaffinity(0) if allowed is None else allowed
    node_cpus={row['cpu'] for row in rows if row['node']==node}
    if not set(cpus)<=node_cpus or not set(cpus)<=set(allowed):
        raise ValueError('Client CPU list is outside the declared NUMA node or allowed cpuset')
    validate_isolation({'host_client': cpus}, rows)
    return cpus


def topology():
    rows = []
    for p in Path('/sys/devices/system/cpu').glob('cpu[0-9]*'):
        if (p/'online').exists() and (p/'online').read_text().strip() != '1':
            continue
        nodes = list(p.glob('node[0-9]*'))
        if len(nodes) != 1:
            raise ValueError('Cannot identify NUMA node for '+str(p))
        rows.append({'cpu': int(p.name[3:]), 'node': int(nodes[0].name[4:]),
                     'socket': int((p/'topology/physical_package_id').read_text()),
                     'core': int((p/'topology/core_id').read_text())})
    return rows


def snapshot(pid):
    rows = {}
    for p in (Path('/proc')/str(pid)/'task').iterdir():
        try:
            rows[int(p.name)] = sorted(os.sched_getaffinity(int(p.name)))
        except ProcessLookupError:
            pass
    return rows


def qemu_allowed_cpus(pid, configured_cpus, cpus):
    """Keep the VM allocation within configured and currently observed CPUs.

    A parent may already have pinned vCPU threads individually and restricted
    the main thread to emulator cores. Its main-thread mask alone therefore
    does not describe the allocation available to the complete VM.
    """
    if (not isinstance(configured_cpus, list)
            or any(type(cpu) is not int or cpu < 0 for cpu in configured_cpus)
            or len(configured_cpus) != len(set(configured_cpus))):
        raise ValueError('Invalid configured QEMU host_cpus')
    rows = snapshot(pid)
    required = {pid} | {row['thread-id'] for row in cpus}
    if not required <= rows.keys() or any(not mask for mask in rows.values()):
        raise ValueError('QEMU main/vCPU threads or their affinity masks are missing')
    observed = {cpu for mask in rows.values() for cpu in mask}
    if configured_cpus and not observed <= set(configured_cpus):
        raise ValueError('QEMU thread affinity is outside configured host_cpus')
    # Do not restore CPUs merely because they appear in a stale configuration.
    # Empty host_cpus means the launcher did not request an explicit subset.
    return observed, rows


def expected_threads(cpus, profile):
    ordered = sorted(cpus, key=lambda row: row['cpu-index'])
    if [r['cpu-index'] for r in ordered] != list(range(len(profile['vcpu_host_cpus']))):
        raise ValueError('QMP vCPU topology does not match the affinity plan')
    return {row['thread-id']: [cpu] for row, cpu in zip(ordered, profile['vcpu_host_cpus'])}


def verify(pid, cpus, profile):
    rows = snapshot(pid)
    expected = expected_threads(cpus, profile)
    failures = {tid: actual for tid, actual in rows.items()
                if actual != expected.get(tid, profile['qemu_service_cpus'])}
    missing = sorted(set(expected)-set(rows))
    if failures or missing:
        raise RuntimeError('QEMU affinity mismatch: '+str({'threads': failures, 'missing_vcpus': missing}))
    return {'status': 'PASS', 'threads': rows, 'vcpu_threads': expected}


def apply(pid, cpus, profile):
    before = snapshot(pid)
    expected = expected_threads(cpus, profile)
    try:
        for tid in before:
            try:
                os.sched_setaffinity(tid, expected.get(tid, profile['qemu_service_cpus']))
            except ProcessLookupError:
                if tid in expected:
                    raise
        evidence = verify(pid, cpus, profile)
    except BaseException:
        restore(pid, before)
        raise
    return before, evidence


def restore(pid, before):
    # Threads created after pinning inherit their creator's affinity. Restore
    # them to the original main-thread mask when no original TID is available.
    for tid in snapshot(pid):
        try:
            os.sched_setaffinity(tid, before.get(tid, before[pid]))
        except ProcessLookupError:
            pass


def verify_node(pid, node):
    allowed={row['cpu'] for row in topology() if row['node']==node}
    rows=snapshot(pid)
    if not allowed or not rows or any(not mask or not set(mask)<=allowed for mask in rows.values()):
        raise ValueError('QEMU CPU affinity is outside NUMA node '+str(node))
    return rows


def process_group_members(group, proc=Path('/proc')):
    """Read only this owned process group, including JVM/shell descendants."""
    members = []
    for entry in proc.glob('[0-9]*'):
        try:
            # comm may contain spaces or parentheses; fields after the final
            # closing parenthesis start at state(3), ppid(4), pgrp(5).
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group and fields[0] != 'Z':
                members.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return sorted(members)


def verify_process_masks(pids, allowed):
    """Check every observed thread; generator role-specific singleton masks pass."""
    allowed = set(allowed)
    evidence = {}
    for pid in pids:
        try:
            masks = snapshot(pid)
        except (FileNotFoundError, ProcessLookupError):
            continue
        for tid, mask in masks.items():
            if not mask or not set(mask) <= allowed:
                raise RuntimeError('Host client CPU affinity escaped allocation: ' +
                                   str({'pid': pid, 'tid': tid, 'actual': mask, 'allowed': sorted(allowed)}))
        if masks:
            evidence[str(pid)] = masks
    return evidence


def run_confined(command, cpus, evidence_path, interval=1.0, also_pids=()):
    """Run a Host client in an owned group and verify its live thread masks.

    The monitor and children inherit the explicit CPU subset. A child that
    changes its affinity outside that subset fails the run. Each one-second
    snapshot includes the JVM/generator descendants and optional SSH tunnel.
    This provides sampled evidence; it does not claim CPU reservation from
    unrelated Host jobs or memory/cache/interrupt isolation.
    """
    if interval <= 0 or not command:
        raise ValueError('A command and a positive monitoring interval are required')
    original = os.sched_getaffinity(0)
    if not set(cpus) <= original:
        raise ValueError('Client allocation exceeds monitor allowed CPU set')
    validate_isolation({'host_client': cpus})
    path = Path(evidence_path)
    report = {'status': 'RUNNING', 'allowed_cpus': list(cpus), 'command': list(command),
              'interval_seconds': interval, 'sample_count': 0, 'also_pids': list(also_pids),
              'observed_pids': [], 'maximum_threads': 0, 'started_unix_seconds': time.time()}
    samples_path = path.with_suffix('.samples.jsonl')
    report['samples_file'] = str(samples_path)
    child = None
    def save():
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text(json.dumps(report, indent=2) + '\n'); temp.replace(path)
    try:
        os.sched_setaffinity(0, cpus)
        child = subprocess.Popen(command, start_new_session=True)
        report['pid'] = child.pid
        save()
        with samples_path.open('w') as samples:
            while True:
                masks = verify_process_masks(process_group_members(child.pid), cpus)
                peers = verify_process_masks(also_pids, cpus)
                if any(str(pid) not in peers for pid in also_pids):
                    raise RuntimeError('An explicitly monitored Host client peer exited')
                masks.update(peers)
                report['sample_count'] += 1
                report['observed_pids'] = sorted(set(report['observed_pids']) | {int(pid) for pid in masks})
                report['maximum_threads'] = max(report['maximum_threads'], sum(len(rows) for rows in masks.values()))
                samples.write(json.dumps({'unix_seconds': time.time(), 'process_threads': masks}) + '\n')
                samples.flush()
                code = child.poll()
                if code is not None:
                    break
                try:
                    child.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
        report['returncode'] = code
        report['status'] = 'PASS' if code == 0 and report['observed_pids'] else 'FAIL'
        if not report['observed_pids']:
            report['error'] = 'No live client thread mask was observed'
        return code if code != 0 else (0 if report['status'] == 'PASS' else 1)
    except BaseException as error:
        report.update(status='FAIL', error=repr(error))
        raise
    finally:
        if child is not None and (child.poll() is None or process_group_members(child.pid)):
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
        os.sched_setaffinity(0, original)
        report['finished_unix_seconds'] = time.time()
        save()


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description='Verify all QEMU thread CPU masks remain inside the specified NUMA node')
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--pid',type=int)
    group.add_argument('--client-cpus')
    p.add_argument('--node',type=int,required=True)
    p.add_argument('--run-command', action='store_true', help='Run and monitor a Host client inside the requested CPU subset')
    p.add_argument('--evidence', type=Path)
    p.add_argument('--also-pid', type=int, action='append', default=[])
    p.add_argument('--poll-seconds', type=float, default=1)
    p.add_argument('command', nargs=argparse.REMAINDER)
    args=p.parse_args()
    if args.client_cpus is not None:
        cpus = validate_client_cpus(args.client_cpus,args.node)
        if args.run_command:
            command = args.command[1:] if args.command and args.command[0] == '--' else args.command
            if not args.evidence or not command: p.error('--run-command requires --evidence and a command after --')
            def interrupted(signum, frame): raise KeyboardInterrupt('signal ' + str(signum))
            signal.signal(signal.SIGTERM, interrupted)
            raise SystemExit(run_confined(command, cpus, args.evidence, args.poll_seconds, args.also_pid))
        print(cpu_list(cpus))
    else:
        verify_node(args.pid,args.node)
