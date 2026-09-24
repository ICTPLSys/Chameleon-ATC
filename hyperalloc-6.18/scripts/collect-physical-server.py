#!/usr/bin/env python3
"""Read a physical Hermit server's process, NIC and RDMA counters over SSH."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


REMOTE = r'''
import datetime,json,os,subprocess,sys
from pathlib import Path

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError as error:
        return {'error': str(error)}

def command(argv):
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=10)
        return {'argv': argv, 'returncode': result.returncode,
                'stdout': result.stdout, 'stderr': result.stderr}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'argv': argv, 'error': str(error)}

pid = int(sys.argv[1])
proc = Path('/proc') / str(pid)
argv = [item.decode() for item in (proc / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')]
if not argv or Path(argv[0]).name != 'rswap-server':
    raise RuntimeError('Requested PID is not the expected Hermit memory server')
result = {'started_utc': now(), 'hostname': os.uname().nodename,
          'kernel': os.uname().release, 'architecture': os.uname().machine,
          'counter_units': {'port_rcv_data': '4-byte words',
                            'port_xmit_data': '4-byte words',
                            'other_counters': 'native sysfs units; snapshot is sequential'},
          'process': {'pid': pid, 'argv': argv, 'exe': os.readlink(proc / 'exe'),
                      'stdout': os.readlink(proc / 'fd/1'),
                      'limits': read(proc / 'limits'), 'cgroup': read(proc / 'cgroup')},
          'devices': {}, 'counters': {}}
status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines()
              if ':' in line)
result['process']['status'] = {key: status[key].strip() for key in
    ('Name', 'State', 'Uid', 'Gid', 'VmSize', 'VmRSS', 'VmLck', 'VmPin', 'Threads')
    if key in status}
result['process']['lstart'] = command(['ps', '-p', str(pid), '-o', 'lstart='])
meminfo = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
result['memory'] = {key: meminfo[key].strip() for key in ('MemTotal', 'MemAvailable', 'MemFree')}
for device in sorted(Path('/sys/class/infiniband').iterdir()):
    pci = (device / 'device').resolve()
    info = {'sysfs_device': str(pci), 'driver': str((pci / 'driver').resolve()),
            'attributes': {}, 'ports': {}}
    for key in ('fw_ver', 'node_guid', 'sys_image_guid', 'node_type'):
        info['attributes'][key] = read(device / key)
    for key in ('vendor', 'device', 'subsystem_vendor', 'subsystem_device'):
        info['attributes'][key] = read(pci / key)
    for port in sorted((device / 'ports').iterdir()):
        p = {'attributes': {}, 'gids': {}}
        for key in ('state', 'phys_state', 'rate', 'lid', 'sm_lid', 'link_layer'):
            p['attributes'][key] = read(port / key)
        for gid in sorted((port / 'gids').iterdir()):
            p['gids'][gid.name] = read(gid)
        for category in ('counters', 'hw_counters'):
            folder = port / category
            if not folder.is_dir():
                continue
            for item in sorted(folder.iterdir()):
                if item.is_file():
                    value = read(item)
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        pass
                    result['counters'][str(item)] = value
        info['ports'][port.name] = p
    result['devices'][device.name] = info
result['commands'] = {
    'addresses': command(['ip', '-br', 'address']),
    'routes': command(['ip', 'route', 'show']),
    'guest_route': command(['ip', 'route', 'get', '192.0.2.2']),
    'rdma_link': command(['rdma', 'link', 'show']),
    'rdma_statistic': command(['rdma', 'statistic', 'show']),
    'ibv_device': command(['ibv_devinfo', '-d', 'mlx5_0', '-v']),
}
# Retain MR length/owner evidence without storing remote access keys or IOVAs.
mr = command(['rdma', '-j', 'resource', 'show', 'mr'])
if mr.get('returncode') == 0:
    try:
        records = json.loads(mr.pop('stdout'))
        def scrub(value):
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()
                        if key.lower() not in ('rkey', 'lkey', 'iova')}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value
        mr['records'] = scrub(records)
    except (ValueError, TypeError) as error:
        mr.pop('stdout', None)
        mr['parse_error'] = str(error)
else:
    mr.pop('stdout', None)
result['memory_regions'] = mr
result['completed_utc'] = now()
print(json.dumps(result, indent=2))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host', default='memory-server')
    parser.add_argument('--server-pid', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.server_pid <= 0 or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*', args.ssh_host):
        parser.error('Use a positive server PID and a plain SSH hostname or configured alias')
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', args.ssh_host,
         'python3 - ' + shlex.quote(str(args.server_pid))],
        input=REMOTE, text=True, capture_output=True, timeout=60)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        return result.returncode
    evidence = json.loads(result.stdout)
    evidence['ssh_host'] = args.ssh_host
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'hostname': evidence['hostname'],
                      'pid': args.server_pid, 'counter_paths': len(evidence['counters']),
                      'process': evidence['process']['status']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
