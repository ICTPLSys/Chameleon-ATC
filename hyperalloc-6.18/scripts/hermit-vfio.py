#!/usr/bin/env python3
"""Inspect and prepare explicitly selected RDMA VFs for a Chameleon guest.

No sysfs or module operation is performed without --apply. PF drivers are never
unbound, and changing an existing VF population is deliberately not supported.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
SYS = Path('/sys')
PROC = Path('/proc')
DEV = Path('/dev')
DEFAULT_STATE = ROOT / 'build/hardware/vfio-state.json'
BDF_RE = re.compile(r'(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$')


def bdf(value):
    value = value.lower()
    if not BDF_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f'Invalid PCI BDF: {value}')
    return value if value.count(':') == 2 else '0000:' + value


def device(address):
    path = SYS / 'bus/pci/devices' / bdf(address)
    if not path.is_dir():
        raise RuntimeError(f'PCI function does not exist: {address}')
    return path


def read(path, default=None):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return default


def driver(path):
    return (path / 'driver').resolve().name if (path / 'driver').exists() else None


def override(path):
    value = read(path / 'driver_override', '')
    return '' if value == '(null)' else value


def physical_function(path):
    return (path / 'physfn').resolve().name if (path / 'physfn').exists() else None


def group_members(path):
    group = path / 'iommu_group'
    if not group.exists():
        raise RuntimeError(f'{path.name} has no IOMMU group; enable IOMMU before VFIO')
    return group.resolve().name, sorted(p.name for p in (group / 'devices').iterdir())


def identity(path):
    return {name: read(path / name) for name in
            ('vendor', 'device', 'subsystem_vendor', 'subsystem_device')}


def describe(path):
    group = path / 'iommu_group'
    interfaces = []
    for interface in sorted((path / 'net').glob('*')):
        interfaces.append({'name': interface.name,
                           'up': bool(int(read(interface / 'flags', '0'), 0) & 1),
                           'operstate': read(interface / 'operstate')})
    return {'bdf': path.name, 'driver': driver(path), 'driver_override': override(path),
            'pf': physical_function(path), **identity(path),
            'iommu_group': group.resolve().name if group.exists() else None,
            'group_members': group_members(path)[1] if group.exists() else [],
            'net': interfaces,
            'rdma': sorted(p.name for p in (path / 'infiniband').glob('*')),
            'sriov_totalvfs': read(path / 'sriov_totalvfs'),
            'sriov_numvfs': read(path / 'sriov_numvfs'),
            'vfs': {p.name: p.resolve().name for p in sorted(path.glob('virtfn*'))}}


def inspect(pf=None, vfs=()):
    if pf or vfs:
        paths = [device(x) for x in ([pf] if pf else []) + list(vfs)]
        if pf:
            paths += [p.resolve() for p in device(pf).glob('virtfn*')]
    else:
        paths = [p for p in (SYS / 'bus/pci/devices').iterdir()
                 if (p / 'sriov_totalvfs').exists() or (p / 'physfn').exists()
                 or (p / 'infiniband').is_dir()]
    return [describe(p) for p in sorted(set(paths))]


def require_pf(address):
    path = device(address)
    if physical_function(path) or not (path / 'sriov_totalvfs').exists():
        raise RuntimeError(f'{address} is not an SR-IOV physical function')
    if driver(path) is None or driver(path) == 'vfio-pci':
        raise RuntimeError(f'PF {address} must retain its native driver')
    return path


def node_users(nodes):
    """Only fds for these devices, not the shared /dev/vfio/vfio control node."""
    numbers = set()
    for path in nodes:
        try:
            info = path.stat()
            if stat.S_ISCHR(info.st_mode):
                numbers.add(info.st_rdev)
        except FileNotFoundError:
            pass
    if not numbers:
        return []
    users = set()
    for process in PROC.glob('[0-9]*'):
        try:
            for fd in (process / 'fd').iterdir():
                try:
                    info = fd.stat()
                    if stat.S_ISCHR(info.st_mode) and info.st_rdev in numbers:
                        users.add(int(process.name))
                except (FileNotFoundError, ProcessLookupError):
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            raise RuntimeError(f'Cannot inspect {process}/fd; run the check as root')
    return sorted(users)


def require_idle(path):
    up = [n['name'] for n in describe(path)['net'] if n['up']]
    if up:
        raise RuntimeError(f'{path.name} has active network interfaces: {up}; stop VF use first')
    nodes = []
    group, _ = group_members(path)
    nodes.append(DEV / 'vfio' / group)
    for child in (path / 'vfio-dev').glob('*'):
        nodes.append(DEV / 'vfio/devices' / child.name)
    for verbs in (SYS / 'class/infiniband_verbs').glob('uverbs*'):
        if (verbs / 'device').resolve() == path.resolve():
            nodes.append(DEV / 'infiniband' / verbs.name)
    users = node_users(nodes)
    if users:
        raise RuntimeError(f'{path.name} is open by processes {users}; stop the VM/RDMA clients first')


def bind_plan(pf, addresses):
    parent = require_pf(pf)
    selected = {bdf(x) for x in addresses}
    if not selected:
        raise RuntimeError('At least one --vf must be selected')
    records = []
    for address in sorted(selected):
        path = device(address)
        if physical_function(path) != parent.name:
            raise RuntimeError(f'{address} is not a VF of {parent.name}; PF binding is not supported')
        group, members = group_members(path)
        outside = set(members) - selected
        if outside:
            raise RuntimeError(f'IOMMU group {group} contains unselected functions {sorted(outside)}; '
                               'all group members must be explicitly selected VFs of this PF')
        if driver(path) not in (None, 'mlx5_core', 'mlx4_core', 'vfio-pci'):
            raise RuntimeError(f'{address} has unexpected driver {driver(path)}')
        if override(path) not in ('', driver(path), 'vfio-pci'):
            raise RuntimeError(f'{address} has an unrelated driver_override: {override(path)}')
        require_idle(path)
        records.append({'bdf': address, 'pf': parent.name, 'identity': identity(path),
                        'driver': driver(path), 'override': override(path), 'iommu_group': group})
    return {'version': 1, 'status': 'prepared',
            'boot_id': read(PROC / 'sys/kernel/random/boot_id'), 'devices': records}


def write(path, value):
    path.write_text(str(value) + '\n')


def wait_driver(path, expected):
    for _ in range(100):
        if driver(path) == expected:
            return
        time.sleep(.02)
    raise RuntimeError(f'{path.name}: expected driver {expected}, found {driver(path)}')


def rebind(path, target, final_override):
    current = driver(path)
    if current != target:
        # Override before unbind prevents an unrelated native reprobe from winning.
        write(path / 'driver_override', target or '')
        if current:
            write(path / 'driver/unbind', path.name)
        if target:
            write(SYS / 'bus/pci/drivers_probe', path.name)
        wait_driver(path, target)
    write(path / 'driver_override', final_override)


def restore_plan(saved):
    if saved.get('version') != 1 or not saved.get('devices'):
        raise RuntimeError('Invalid VFIO recovery state')
    if saved.get('boot_id') != read(PROC / 'sys/kernel/random/boot_id'):
        raise RuntimeError('VFIO recovery state belongs to another boot; inspect current devices first')
    for entry in saved['devices']:
        path = device(entry['bdf'])
        if physical_function(path) != entry['pf'] or identity(path) != entry['identity']:
            raise RuntimeError(f"{path.name}: PCI identity/PF differs from recovery state")
        require_pf(entry['pf'])
        if group_members(path)[0] != entry['iommu_group']:
            raise RuntimeError(f'{path.name}: IOMMU group changed')
        if driver(path) not in (None, 'vfio-pci', entry['driver']):
            raise RuntimeError(f'{path.name}: unexpected current driver {driver(path)}')
        if override(path) not in ('', 'vfio-pci', entry['override'], entry['driver']):
            raise RuntimeError(f'{path.name}: unexpected current driver_override')
        require_idle(path)
    return saved


def restore_devices(saved):
    restore_plan(saved)
    for entry in reversed(saved['devices']):
        if entry['driver']:
            subprocess.run(['modprobe', entry['driver']], check=True)
        rebind(device(entry['bdf']), entry['driver'], entry['override'])


def save_state(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def state_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def apply_bind(pf, addresses, state_path):
    with state_lock(state_path):
        if state_path.exists() and json.loads(state_path.read_text()).get('status') not in ('restored', 'rolled-back'):
            raise RuntimeError(f'Existing recovery state must be restored first: {state_path}')
        saved = bind_plan(pf, addresses)
        subprocess.run(['modprobe', 'vfio-pci'], check=True)
        subprocess.run(['modprobe', 'vfio_iommu_type1'], check=True)
        save_state(state_path, saved)
        try:
            for entry in saved['devices']:
                path = device(entry['bdf'])
                require_idle(path)
                rebind(path, 'vfio-pci', 'vfio-pci')
            saved['status'] = 'active'
            save_state(state_path, saved)
        except BaseException as error:
            try:
                restore_devices(saved)
                saved['status'] = 'rolled-back'
                save_state(state_path, saved)
            except BaseException as recovery_error:
                raise RuntimeError(f'Binding failed: {error}; rollback failed: {recovery_error}. '
                                   f'Recovery state retained at {state_path}') from error
            raise
        return saved


def enable_plan(pf, count):
    path = require_pf(pf)
    total = int(read(path / 'sriov_totalvfs'))
    current = int(read(path / 'sriov_numvfs'))
    if not 1 <= count <= total:
        raise RuntimeError(f'VF count must be in [1, {total}]')
    if current not in (0, count):
        raise RuntimeError(f'PF already has {current} VFs; changing that population can destroy assigned VFs')
    if current != count:
        # Some PF drivers reset resources when first enabling SR-IOV. Do not
        # create VFs on an interface or RDMA device currently serving traffic.
        require_idle(path)
    return {'pf': path.name, 'current': current, 'requested': count,
            'write': None if current == count else str(path / 'sriov_numvfs')}


def guid(value):
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){7}[0-9a-fA-F]{2}', value):
        raise argparse.ArgumentTypeError('GUID must contain eight colon-separated hex bytes')
    if int(value.replace(':', ''), 16) == 0:
        raise argparse.ArgumentTypeError('GUID must be nonzero')
    return value.lower()


def configure_plan(args):
    path = require_pf(args.pf)
    link = path / f'virtfn{args.vf_index}'
    if args.vf_index < 0 or not link.exists():
        raise RuntimeError(f'PF {path.name} has no VF index {args.vf_index}')
    vf = link.resolve()
    if driver(vf) != 'mlx5_core':
        raise RuntimeError('Configure IB GUIDs while the VF still has its native mlx5_core driver')
    require_idle(vf)
    directory = path / 'sriov' / str(args.vf_index)
    values = {'node': args.node_guid, 'port': args.port_guid, 'policy': args.policy}
    if not any(value is not None for value in values.values()):
        raise RuntimeError('Specify at least one of --node-guid, --port-guid or --policy')
    entries = []
    for name, value in values.items():
        if value is not None:
            target = directory / name
            if not target.exists():
                raise RuntimeError(f'Missing mlx5 IB VF configuration interface: {target}')
            entries.append({'path': str(target), 'before': read(target), 'after': value})
    return {'pf': path.name, 'vf': vf.name, 'writes': entries}


def vfio_environment():
    limit_path = SYS / 'module/vfio_iommu_type1/parameters/dma_entry_limit'
    soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    def display(value):
        return 'unlimited' if value == resource.RLIM_INFINITY else value
    return {'dma_entry_limit': read(limit_path),
            'dma_entry_limit_path': str(limit_path),
            'memlock_soft_bytes': display(soft), 'memlock_hard_bytes': display(hard)}


def vfio_limit_plan(memory_mib, reserve_entries):
    if memory_mib <= 0 or reserve_entries < 0:
        raise RuntimeError('Memory must be positive and reserve entries must be nonnegative')
    # Coordinated Host retirement uses 4K mappings, including mTHP guests.
    required = memory_mib * 256 + reserve_entries
    if required > 0xffffffff:
        raise RuntimeError('Requested VFIO DMA entry limit exceeds the kernel unsigned integer range')
    path = SYS / 'module/vfio_iommu_type1/parameters/dma_entry_limit'
    current = read(path)
    return {'memory_mib': memory_mib, 'required_entries': required,
            'current_entries': int(current) if current is not None else None,
            'target_entries': max(required, int(current or '0')), 'path': str(path)}


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    sub = command.add_subparsers(dest='command', required=True)
    show = sub.add_parser('inspect', help='Read-only PCI, PF/VF and IOMMU inventory')
    show.add_argument('--pf', type=bdf)
    show.add_argument('--vf', type=bdf, action='append', default=[])
    enable = sub.add_parser('enable-vfs', help='Create VFs without resetting an existing population')
    enable.add_argument('--pf', type=bdf, required=True)
    enable.add_argument('--count', type=int, required=True)
    configure = sub.add_parser('configure-vf', help='Optional mlx5 InfiniBand VF GUID/policy setup')
    configure.add_argument('--pf', type=bdf, required=True)
    configure.add_argument('--vf-index', type=int, required=True)
    configure.add_argument('--node-guid', type=guid)
    configure.add_argument('--port-guid', type=guid)
    configure.add_argument('--policy', choices=['Down', 'Up', 'Follow'])
    limits = sub.add_parser('configure-vfio', help='Raise VFIO DMA entry capacity for 4K Chameleon mappings')
    limits.add_argument('--memory-mib', type=int, required=True)
    limits.add_argument('--reserve-entries', type=int, default=4096)
    bind = sub.add_parser('bind', help='Bind only selected idle RDMA VFs to vfio-pci')
    bind.add_argument('--pf', type=bdf, required=True)
    bind.add_argument('--vf', type=bdf, action='append', required=True)
    restore = sub.add_parser('restore', help='Restore original VF drivers from the saved journal')
    for item in (enable, configure, limits, bind, restore):
        item.add_argument('--apply', action='store_true', help='Perform the displayed operation (requires root)')
    for item in (bind, restore):
        item.add_argument('--state', type=Path, default=DEFAULT_STATE)
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    apply = getattr(args, 'apply', False)
    if apply and os.geteuid() != 0:
        raise RuntimeError('--apply requires root')
    if args.command == 'inspect':
        result = inspect(args.pf, args.vf)
    elif args.command == 'enable-vfs':
        result = enable_plan(args.pf, args.count)
        if apply and result['write']:
            write(Path(result['write']), args.count)
            if int(read(Path(result['write']))) != args.count:
                raise RuntimeError('PF did not accept the requested VF count; inspect current state')
    elif args.command == 'configure-vf':
        result = configure_plan(args)
        if apply:
            completed = []
            try:
                for entry in result['writes']:
                    write(Path(entry['path']), entry['after'])
                    completed.append(entry)
            except BaseException:
                for entry in reversed(completed):
                    write(Path(entry['path']), entry['before'])
                raise
    elif args.command == 'configure-vfio':
        result = vfio_limit_plan(args.memory_mib, args.reserve_entries)
        if apply:
            subprocess.run(['modprobe', 'vfio_iommu_type1'], check=True)
            result = vfio_limit_plan(args.memory_mib, args.reserve_entries)
            if result['current_entries'] is None:
                raise RuntimeError('vfio_iommu_type1 exposes no dma_entry_limit parameter')
            if result['target_entries'] != result['current_entries']:
                write(Path(result['path']), result['target_entries'])
            if int(read(Path(result['path']))) < result['required_entries']:
                raise RuntimeError('VFIO did not accept the DMA entry capacity')
    elif args.command == 'bind':
        result = (apply_bind(args.pf, args.vf, args.state) if apply else bind_plan(args.pf, args.vf))
    else:
        result = restore_plan(json.loads(args.state.read_text()))
        if apply:
            with state_lock(args.state):
                result = json.loads(args.state.read_text())
                restore_devices(result)
                result['status'] = 'restored'
                save_state(args.state, result)
    output = {'operation': args.command, 'applied': apply, 'result': result}
    if args.command in ('inspect', 'configure-vfio'):
        output['environment'] = vfio_environment()
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f'hermit-vfio: {error}', file=sys.stderr)
        sys.exit(1)
