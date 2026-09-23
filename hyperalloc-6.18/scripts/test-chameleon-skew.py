#!/usr/bin/env python3
"""Run a real skewed workload with PEBS and a separate SoftRoCE memory VM."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('skew_vm', ROOT / 'scripts/test-vm.py')
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)
MIB = 1 << 20
GIB = 1 << 30
BAD = r'BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state|general protection fault'
PATHS = {'tracker': 'chameleon', 'manager': 'chameleon_mm',
         'shadow': 'chameleon_shadow', 'policy': 'chameleon_policy', 'hermit': 'hermit',
         'rdma': 'hermit_rdma'}


def require(test, message):
    if not test:
        raise AssertionError(message)


def rss(pid):
    text = Path(f'/proc/{pid}/smaps_rollup').read_text()
    return {k: int(v) * 1024 for k, v in re.findall(r'^(\w+):\s+(\d+) kB$', text, re.M)}


def fields(text):
    result = {}
    for key, value in re.findall(r'^(\w+) (\S+)$', text, re.M):
        try:
            result[key] = int(value, 0)
        except ValueError:
            result[key] = value
    return result


def command(console, directory, value, timeout=120):
    return console.command('echo ' + shlex.quote(value) + ' > /sys/kernel/debug/' +
                           PATHS[directory] + '/control', timeout=timeout)


def common(kernel, initrd, memory, cpus, qmp, netdev, pebs=False):
    return [str(ROOT / 'build/qemu/qemu-system-x86_64'), '-L', '/usr/share/qemu',
        '-machine', 'pc,mem-merge=off', '-accel', 'kvm,hyperalloc-pebs-meminfo=on' if pebs else 'kvm',
        '-cpu', 'host,migratable=off,pmu=on', '-m', str(memory), '-smp', str(cpus),
        '-nodefaults', '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
        '-kernel', str(kernel), '-initrd', str(initrd), '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr',
        '-qmp', 'unix:' + str(qmp) + ',server=on,wait=off', '-netdev', netdev,
        '-device', 'virtio-net-pci,netdev=rdma,romfile=,mac=52:54:00:12:00:' + ('02' if pebs else '01')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--gib', type=int, default=16)
    parser.add_argument('--guest-mib', type=int, default=24576)
    parser.add_argument('--server-mib', type=int, default=12288)
    parser.add_argument('--pool-mib', type=int, default=8192)
    parser.add_argument('--reclaim-mib', type=int, default=4096)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--hot-access-ppm', type=int, default=990000)
    parser.add_argument('--sampling-period', type=int, default=8192)
    parser.add_argument('--cold-folios', type=int, default=4)
    parser.add_argument('--network-mtu', type=int, default=9000)
    parser.add_argument('--warmup-seconds', type=int, default=30)
    parser.add_argument('--run-seconds', type=int, default=60)
    parser.add_argument('--reclaim-timeout', type=int, default=1800)
    parser.add_argument('--verify-timeout', type=int, default=1800)
    parser.add_argument('--sample-seconds', type=int, default=5)
    parser.add_argument('--psi-ppm', type=int, default=1000000)
    parser.add_argument('--guest-initramfs', type=Path, default=ROOT / 'build/skew-guest.cpio.gz')
    parser.add_argument('--server-initramfs', type=Path, default=ROOT / 'build/skew-server.cpio.gz')
    args = parser.parse_args()
    require(args.guest_mib * MIB > args.gib * GIB and args.pool_mib >= args.reclaim_mib,
            'VM memory must exceed workload, and pool must cover requested cold-page retirement')
    output = ROOT / 'results' / ('vm-' + args.name)
    output.mkdir(parents=True, exist_ok=True)
    require(not (output / 'report.json').exists(), 'choose a new result name for each run')
    # A reliable local stream carries QEMU's framed Ethernet packets. This
    # avoids UDP socket drops during large RXE WR bursts and TCP Nagle delays,
    # while both VMs still execute their complete IP/SoftRoCE/RDMA stacks.
    network = dict(zip(('server', 'guest'), socket.socketpair()))
    server_qmp_path, guest_qmp_path = output / 'server-qmp.sock', output / 'guest-qmp.sock'
    server_command = common(ROOT / 'build/host/arch/x86/boot/bzImage', args.server_initramfs.resolve(),
        args.server_mib, 4, server_qmp_path,
        f'socket,id=rdma,fd={network["server"].fileno()}')
    guest_command = common(ROOT / 'build/guest/arch/x86/boot/bzImage', args.guest_initramfs.resolve(),
        args.guest_mib, args.threads, guest_qmp_path,
        f'socket,id=rdma,fd={network["guest"].fileno()}', True)
    for name in ['auto'] + ['install' + str(i) for i in range(4)]:
        guest_command += ['-object', 'iothread,id=' + name]
    guest_command += ['-device', json.dumps({'driver': 'virtio-llfree-balloon', 'id': 'ha',
        'auto-mode': False, 'chameleon': True, 'chameleon-policy': True,
        'chameleon-batch-pages': 512, 'chameleon-watermark-bytes': 0,
        'auto-mode-iothread': 'auto', 'iothread-vq-mapping':
        [{'iothread': 'install' + str(i)} for i in range(4)]})]
    report = {'status': 'RUNNING', 'configuration': vars(args).copy(),
        'physical_host': subprocess.check_output(['uname', '-r'], text=True).strip(),
        'scope': 'direct KVM PEBS compute Guest plus independent Unix stream-Ethernet SoftRoCE storage VM',
        'host_range_limit': 4096, 'samples': [], 'phases': {}}
    report['configuration'] = {k: str(v) if isinstance(v, Path) else v for k, v in report['configuration'].items()}
    inputs = [ROOT / 'build/guest/arch/x86/boot/bzImage', ROOT / 'build/host/arch/x86/boot/bzImage',
              ROOT / 'build/qemu/qemu-system-x86_64', args.guest_initramfs.resolve(),
              args.server_initramfs.resolve(), ROOT / 'tests/chameleon_skew.c', Path(__file__).resolve()]
    report['input_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    (output / 'commands.json').write_text(json.dumps({'server': server_command, 'guest': guest_command}, indent=2) + '\n')
    started = time.monotonic()
    processes, qmps, logs = [], [], []
    console = server = qmp = None
    phase = 'boot'
    last_log = ''

    def save():
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    def sample(label, query=False):
        nonlocal last_log
        shell = '; '.join('echo SKEW_STATS_' + key + '; cat /sys/kernel/debug/' + directory + '/stats'
                          for key, directory in PATHS.items())
        shell += '; echo SKEW_WORKLOAD_BEGIN; tail -n 12 /tmp/skew.log; echo SKEW_WORKLOAD_END'
        text = console.command(shell, timeout=120)
        groups = re.split(r'^SKEW_STATS_(\w+)\s*$', text, flags=re.M)
        row = {'seconds': round(time.monotonic() - started, 3), 'phase': label,
               'compute_memory': rss(processes[1].pid), 'server_memory': rss(processes[0].pid)}
        for i in range(1, len(groups), 2):
            row[groups[i]] = fields(groups[i + 1].split('SKEW_WORKLOAD_BEGIN')[0])
        match = re.search(r'^SKEW_WORKLOAD_BEGIN\n(.*?)^SKEW_WORKLOAD_END', text, re.M | re.S)
        require(match, 'workload log must be available')
        last_log = match[1]
        row['workload_tail'] = last_log
        events = [json.loads(line) for line in last_log.splitlines() if line.startswith('{"event":')]
        if events:
            row['workload_event'] = events[-1]
        require(not re.search(r'SKEW_ERROR|SKEW_FAIL|SKEW_CORRUPTION|SKEW_LEDGER_MISMATCH|status=FAIL|"(?:verify_)?errors":\s*[1-9]|SKEW_EXIT=[1-9]', last_log),
                'workload must keep data correct and remain healthy')
        if query:
            row['pmu'] = console.command('cat /sys/kernel/debug/chameleon/pmu')
            full = qmp.execute('query-llfree-balloon')
            ch = full['chameleon']
            (output / ('qmp-' + label + '.json')).write_text(json.dumps(full, indent=2) + '\n')
            row['qmp'] = {k: v for k, v in full.items() if k != 'chameleon'}
            row['qmp']['chameleon'] = {k: v for k, v in ch.items() if k != 'ranges'}
            row['qmp']['range_orders'] = {str(order): sum(r['order'] == order for r in ch['ranges'])
                                        for order in range(10)}
            row['qmp']['retired_resident_pages'] = sum(r['resident-pages'] for r in ch['ranges'] if r['state'] == 5)
            require(not ch['error-pages'], 'Host range transactions must not have ownership errors')
            require(all(r['residency-status'] == 0 for r in ch['ranges']), 'actual Host backing must be queryable')
            require(full['actual'] == args.guest_mib * MIB - ch['retired-pages'] * 4096 and
                    ch['blocked-pages'] == ch['retired-pages'], 'Host capacity must agree with actual retirement')
            require(all(r['flags'] == 2 for r in ch['ranges'] if r['state'] == 5),
                    'every retired range must have saved real data')
        report['samples'].append(row)
        save()
        print(json.dumps({'phase': label, 'seconds': row['seconds'],
            'rss_gib': round(row['compute_memory']['Rss'] / GIB, 3),
            'remote_gib': round(row.get('shadow', {}).get('host_reclaimed_pages', 0) * 4096 / GIB, 3),
            'read_gib': round(row.get('hermit', {}).get('bytes_read', 0) / GIB, 3),
            'hardware_samples': row.get('tracker', {}).get('hardware_samples', 0)}), flush=True)
        return row

    def work(value, marker, timeout):
        boundary = 'SKEW_COMMAND_BOUNDARY_' + str(time.monotonic_ns())
        console.command('echo ' + boundary + ' >> /tmp/skew.log; echo ' + shlex.quote(value) + ' >&3')
        end = time.monotonic() + timeout
        while True:
            row = sample(phase)
            if re.search(marker, last_log.rsplit(boundary, 1)[-1], re.M):
                return row
            require(time.monotonic() < end, 'workload phase timed out: ' + value)
            time.sleep(args.sample_seconds)

    try:
        for name, cmd in [('server', server_command), ('guest', guest_command)]:
            log = (output / (name + '-qemu.log')).open('w')
            logs.append(log)
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                       pass_fds=(network[name].fileno(),))
            network[name].close()
            processes.append(process)
            active = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / (name + '-serial.log'))
            if name == 'server':
                server = active
                server.wait(r'^SKEW_SERVER_READY .*$', timeout=240)
                server.command('dmesg -n 1; ip link set dev eth0 mtu ' + str(args.network_mtu))
                qmps.append(vm.QMP(str(server_qmp_path)))
            else:
                console = active
                console.wait(r'^HYPERALLOC_READY$', timeout=180)
                qmp = vm.QMP(str(guest_qmp_path))
                qmps.append(qmp)
        require(qmp.execute('query-kvm')['enabled'], 'compute VM requires real KVM')
        console.command('dmesg -n 1; export PATH=/opt/hermit-rdma/bin:/usr/bin:/usr/sbin:/sbin:/bin; '
            'export LD_LIBRARY_PATH=/opt/hermit-rdma/lib; ulimit -l unlimited; '
            '/sbin/hermit-network-setup eth0 192.0.2.2 rxe; '
            'ip link set dev eth0 mtu ' + str(args.network_mtu), timeout=60)
        console.command('insmod /tests/rswap-client.ko backend=rdma pool_mb=' + str(args.pool_mib) +
                        ' sip=192.0.2.1 sport=9400', timeout=300)
        console.command('test ! -d /sys/module/chameleon_psi_load; '
            'echo never > /sys/kernel/mm/transparent_hugepage/defrag; '
            'for f in /sys/kernel/mm/transparent_hugepage/hugepages-*kB/enabled; do echo never > "$f"; done; '
            'echo always > /sys/kernel/mm/transparent_hugepage/hugepages-2048kB/enabled; '
            'echo 600000 > /sys/kernel/mm/transparent_hugepage/khugepaged/scan_sleep_millisecs')
        console.command('mkfifo /tmp/skew.commands; '
            '(/tests/chameleon_skew --gib ' + str(args.gib) + ' --threads ' + str(args.threads) +
            ' --hot-access-ppm ' + str(args.hot_access_ppm) +
            ' < /tmp/skew.commands >> /tmp/skew.log 2>&1; echo SKEW_EXIT=$? >> /tmp/skew.log) & '
            'skew_pid=$!; exec 3>/tmp/skew.commands', timeout=30)
        phase = 'initialize'
        deadline = time.monotonic() + 300
        while True:
            initial = sample(phase)
            ready = re.search(r'^SKEW_READY pid=(\d+) address=(0x[0-9a-f]+) bytes=(\d+) threads=(\d+)', last_log, re.M)
            if ready:
                break
            require(time.monotonic() < deadline, 'full working-set initialization timed out')
            time.sleep(args.sample_seconds)
        pid, address, size, threads = int(ready[1]), ready[2], int(ready[3]), int(ready[4])
        require(size == args.gib * GIB and threads == args.threads, 'actual benchmark dimensions must match request')
        ready_line = re.search(r'^SKEW_READY .*$', last_log, re.M)[0]
        actual = dict(re.findall(r'(\w+)=([^ ]+)', ready_line))
        require(int(actual['hot_access_ppm']) == args.hot_access_ppm and
                float(actual['hot_percent']) == 1 and int(actual['write_percent']) == 10 and
                int(actual['seed']) == 1, 'actual workload distribution must match the experiment')
        report['workload'] = {'pid': pid, 'address': address, 'bytes': size, 'threads': threads,
                              'hot_pages_percent': 1, 'hot_access_ppm': args.hot_access_ppm,
                              'write_percent': 10, 'actual_ready': actual}
        ram = args.guest_mib * MIB
        for value in ['reset', f'capacity {ram} {ram}', f'sampling fixed {args.sampling_period}', 'cooling fixed 131072', 'enable']:
            command(console, 'tracker', value)
        for value in [f'target {pid} {address} {size}', 'split_mode hhh', 'selector mixed_cost',
                      'batch 32 6400 128 4 10']:
            command(console, 'manager', value)
        for order in [0] + list(range(2, 10)):
            command(console, 'manager', f'cost {order} {1 << order}')
        command(console, 'manager', 'enable')
        phase = 'warmup'
        work(f'warmup {args.warmup_seconds}', r'^SKEW_DONE phase=warmup status=PASS\b', args.warmup_seconds + 120)
        report['phases']['baseline'] = baseline = sample('baseline', True)
        require(baseline['tracker']['hardware_samples'] > 0 and baseline['tracker']['accepted_samples'] > 0 and
                not baseline['tracker']['synthetic_samples'],
                'hotness must come from actual hardware sampling')
        require(baseline['compute_memory']['Rss'] > 0.9 * size, 'all application data must initially occupy real backing')
        target = args.reclaim_mib * MIB
        for value in ['set epoch_us 10000', f'set threshold_ppm {args.psi_ppm}', 'set psi_full 0',
                      'set free_pages 0', f'set cold_folios {args.cold_folios}', f'set minimum_local_bytes {ram - target}',
                      'set discard_test 0', f'target {pid} {address} {size}', 'enable']:
            command(console, 'policy', value)
        phase = 'skew_reclaim'
        deadline = time.monotonic() + args.reclaim_timeout
        while True:
            # Clear only prior workload output after its stage finished;
            # the binary keeps its data and checksums throughout all stages.
            console.command('echo SKEW_PHASE_BOUNDARY >> /tmp/skew.log')
            row = work(f'run {args.run_seconds}', r'^SKEW_DONE phase=run status=PASS\b', args.run_seconds + 300)
            current = sample('reclaim_checkpoint', True)
            retired = current['qmp']['chameleon']['retired-pages'] * 4096
            if retired >= target - (64 * MIB):
                break
            if time.monotonic() >= deadline:
                break
        # Workload is between timed stages. Let outstanding stores finish
        # under the unchanged capacity floor, then inspect real retirement.
        # HHH can leave a READY tail smaller than the 512-page active batch.
        # Explicitly flush that tail; it does not count toward the separate
        # target check made while random accesses were still running.
        qmp.execute('chameleon-configure', {'config': {'batch-pages': 1}})
        report['tail_drain_batch_pages'] = 1
        deadline = time.monotonic() + 300
        while True:
            row = sample('drain_pending')
            if not row['shadow']['shadow_pages'] and not row['hermit']['inflight']:
                break
            require(time.monotonic() < deadline, 'pending save pipeline must drain')
            time.sleep(args.sample_seconds)
        report['phases']['retired'] = retired = sample('retired', True)
        reclaimed = retired['qmp']['chameleon']['retired-pages'] * 4096
        reduction = baseline['compute_memory']['Rss'] - retired['compute_memory']['Rss']
        report['checks'] = {
            'retirement_target': reclaimed >= target - 64 * MIB,
            'retirement_during_random_access': any(
                s['phase'] == 'skew_reclaim' and s.get('workload_event', {}).get('event') == 'progress' and
                s['shadow']['host_reclaimed_pages'] * 4096 >= target - 64 * MIB for s in report['samples']),
            'rss_reduction': reduction >= reclaimed * 0.7,
            'sampling_reclaim': retired['tracker']['hardware_samples'] > baseline['tracker']['hardware_samples'],
        }
        save()
        require(not retired['qmp']['retired_resident_pages'], 'retired source backing is actually absent')
        require(not retired['policy']['hard_reclaimed_bytes'], 'free-page ballooning must not account for this reduction')
        phase = 'shifted_hotspot'
        console.command("echo 'shift 50' >&3")
        time.sleep(0.1)
        work(f'run {args.run_seconds}', r'^SKEW_DONE phase=run status=PASS\b', args.run_seconds + 300)
        report['phases']['shifted'] = sample('shifted', True)
        shifted = report['phases']['shifted']
        report['checks']['demand_readback'] = (
            shifted['shadow']['load_demand_attempts'] > retired['shadow']['load_demand_attempts'] and
            shifted['shadow']['data_fault_restores'] > retired['shadow']['data_fault_restores'] and
            shifted['hermit']['bytes_read'] > retired['hermit']['bytes_read'])
        report['checks']['sampling_shifted'] = (report['phases']['shifted']['tracker']['hardware_samples'] >
                                                retired['tracker']['hardware_samples'])
        # The current control ABI freezes configuration while the lease is
        # active. Disable explicitly restores outstanding policy-owned data;
        # account this as background restoration, separately from the real
        # demand reads measured during both random-access phases.
        phase = 'policy_restore'
        command(console, 'manager', 'disable')
        command(console, 'policy', 'disable', timeout=args.verify_timeout)
        report['phases']['policy_restored'] = sample('policy_restored', True)
        report['restoration'] = {
            'demand_phase_load_attempts': shifted['shadow']['load_demand_attempts'] - retired['shadow']['load_demand_attempts'],
            # The shifted phase can also run COMMIT rollback loads. Its byte
            # total must not be presented as demand-only traffic.
            'shift_phase_background_load_attempts': shifted['shadow']['load_background_attempts'] - retired['shadow']['load_background_attempts'],
            'shift_phase_total_bytes_read': shifted['hermit']['bytes_read'] - retired['hermit']['bytes_read'],
            'disable_background_load_attempts': report['phases']['policy_restored']['shadow']['load_background_attempts'] - shifted['shadow']['load_background_attempts'],
            'disable_bytes_read': report['phases']['policy_restored']['hermit']['bytes_read'] - shifted['hermit']['bytes_read'],
        }
        phase = 'verify_all_data'
        work('verify', r'^SKEW_VERIFIED status=PASS bytes=' + str(size) + r' errors=0 cumulative_errors=0\b', args.verify_timeout)
        command(console, 'tracker', 'disable')
        command(console, 'tracker', 'drain')
        deadline = time.monotonic() + 120
        while True:
            restored = sample('restored', True)
            if not restored['shadow']['live_objects'] and not restored['hermit']['allocated_pages']:
                break
            require(time.monotonic() < deadline, 'all restored ownership must drain')
            time.sleep(args.sample_seconds)
        report['phases']['restored'] = restored
        require(restored['qmp']['actual'] == ram and not restored['qmp']['chameleon']['range-records'] and
                not restored['qmp']['chameleon']['retired-pages'] and not restored['shadow']['reservation_pages'] and
                not restored['shadow']['owned_pages'], 'Host/Guest capacity and ownership fully restore')
        require(restored['compute_memory']['Rss'] >= baseline['compute_memory']['Rss'] * 0.9,
                'reading the whole working set must populate its compute backing again')
        report['checks']['readback_after_retirement'] = (
            restored['shadow']['demand_fault_successes'] > retired['shadow']['demand_fault_successes'] and
            restored['hermit']['bytes_read'] > retired['hermit']['bytes_read'])
        console.command("echo 'quit' >&3")
        full_log = console.command('wait "$skew_pid"; cat /tmp/skew.log', timeout=120)
        (output / 'workload.log').write_text(full_log)
        require('SKEW_EXIT=0' in full_log, 'microbenchmark must exit successfully')
        for name, active in [('guest', console), ('server', server)]:
            diagnostic = active.command('dmesg')
            (output / (name + '-dmesg.log')).write_text(diagnostic)
            require(not re.search(BAD, diagnostic), name + ' kernel diagnostics must be clean')
        report['summary'] = {'workload_bytes': size, 'threads': threads,
            'retired_bytes': reclaimed, 'compute_rss_reduction_bytes': reduction,
            'baseline_rss': baseline['compute_memory']['Rss'], 'retired_rss': retired['compute_memory']['Rss'],
            'restored_rss': restored['compute_memory']['Rss'], 'data_verified': True,
            'hardware_samples': restored['tracker']['hardware_samples'],
            'demand_faults': restored['shadow']['demand_faults'],
            'bytes_written': restored['hermit']['bytes_written'], 'bytes_read': restored['hermit']['bytes_read']}
        # A nonzero warmup count does not establish ongoing sampling. Check
        # every full 30-second window observed while random accesses run.
        windows = []
        for phase_name in ('warmup', 'skew_reclaim', 'shifted_hotspot'):
            anchor = None
            for point in report['samples']:
                if point['phase'] != phase_name:
                    continue
                if anchor is None:
                    anchor = point
                elif point['seconds'] - anchor['seconds'] >= 30:
                    windows.append({'phase': phase_name, 'start': anchor['seconds'],
                        'end': point['seconds'], 'samples': point['tracker']['hardware_samples'] -
                        anchor['tracker']['hardware_samples']})
                    anchor = point
        report['sampling_windows'] = windows
        report['checks']['sampling_no_stall'] = all(w['samples'] > 0 for w in windows)
        phase = 'acceptance'
        require(all(report['checks'].values()), 'experiment acceptance checks: ' + str(report['checks']))
        report['status'] = 'PASS'
    except (Exception, KeyboardInterrupt) as error:
        report.update(status='FAIL', error=repr(error), failed_phase=phase)
        if console:
            try:
                (output / 'failure-diagnostics.log').write_text(console.command('cat /tmp/skew.log; dmesg', timeout=20))
            except Exception:
                pass
    finally:
        for endpoint in network.values():
            endpoint.close()
        for item in reversed(qmps):
            try:
                item.execute('quit')
            except Exception:
                pass
            item.file.close()
            item.sock.close()
        for process in reversed(processes):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
        save()
        print(json.dumps({k: report[k] for k in ['status', 'summary', 'failed_phase', 'error'] if k in report}, indent=2), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
