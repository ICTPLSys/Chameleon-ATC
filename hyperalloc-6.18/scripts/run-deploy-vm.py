#!/usr/bin/env python3
"""Plan, check, or run a disk-backed Chameleon VM with RDMA VFs.

Relative paths in the JSON are relative to hyperalloc-6.18, not the shell cwd.
The default prints the exact command. --check checks this host; --run boots it.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import shlex
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SYS = Path('/sys')
DEFAULTS = dict(name='chameleon', qemu='build/deploy-qemu/qemu-system-x86_64',
                kernel='build/deploy-guest/arch/x86/boot/bzImage', initrd=None,
                disk=None, disk_format='qcow2', root=None, memory_mib=8192,
                cpus=4, network='bridge', bridge='virbr0',
                bridge_helper='/usr/lib/qemu/qemu-bridge-helper', ssh_port=5022,
                mac='52:54:00:ca:00:01', vfio=[], pebs=True, chameleon=True,
                policy=True, auto_mode=False, host_numa_node=None, host_cpus=[],
                append='', snapshot=False, seed=None, reboot=False)


def path(value):
    p = Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


def config(raw):
    unknown = raw.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError(f'Unknown VM settings: {sorted(unknown)}')
    c = {**DEFAULTS, **raw}
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', c['name']):
        raise ValueError('name must contain 1–40 letters, digits, underscores or hyphens')
    for key, lo, hi in [('memory_mib', 512, 1048576), ('cpus', 1, 256), ('ssh_port', 1, 65535)]:
        if type(c[key]) is not int or not lo <= c[key] <= hi:
            raise ValueError(f'{key} must be an integer in [{lo}, {hi}]')
    for key in ['pebs', 'chameleon', 'policy', 'auto_mode', 'snapshot', 'reboot']:
        if type(c[key]) is not bool:
            raise ValueError(f'{key} must be a JSON boolean')
    if not c['disk'] or c['disk_format'] not in ('raw', 'qcow2'):
        raise ValueError('Specify disk and explicit disk_format raw or qcow2')
    if c['kernel'] and (not c['root'] or any(x.isspace() for x in c['root'])):
        raise ValueError('Direct kernel boot requires root, e.g. UUID=... or /dev/vda3')
    if not c['kernel'] and (c['initrd'] or c['append']):
        raise ValueError('Disk GRUB boot cannot accept initrd/append; configure them inside the guest')
    if c['network'] not in ('bridge', 'user', 'none'):
        raise ValueError('network must be bridge, user, or none')
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', c['mac']):
        raise ValueError('Invalid NIC MAC')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', c['bridge']):
        raise ValueError('Invalid bridge name')
    if not isinstance(c['vfio'], list):
        raise ValueError('vfio must be a list of PCI VF addresses')
    for bdf in c['vfio']:
        if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', bdf):
            raise ValueError(f'Use full PCI BDF, e.g. 0000:69:00.2: {bdf}')
    c['vfio'] = [x.lower() for x in c['vfio']]
    if len(c['vfio']) != len(set(c['vfio'])):
        raise ValueError('Duplicate VF')
    if c['policy'] and not c['chameleon']:
        raise ValueError('policy requires chameleon')
    if c['snapshot'] and c['vfio']:
        raise ValueError('Use persistent disk with physical RDMA deployment')
    if c['host_numa_node'] is not None and (type(c['host_numa_node']) is not int or c['host_numa_node'] < 0):
        raise ValueError('host_numa_node must be a nonnegative integer or null')
    if not isinstance(c['host_cpus'], list) or any(type(x) is not int or x < 0 for x in c['host_cpus']):
        raise ValueError('host_cpus must be a list of CPU indices')
    if not isinstance(c['append'], str) or '\n' in c['append']:
        raise ValueError('append must be one kernel command line')
    # Physical VFIO uses HyperAlloc RDM. A virtual IOMMU is a different mapping
    # protocol and cannot be mixed into this deployment contract.
    if c['vfio'] and any(x.startswith(('intel_iommu=', 'amd_iommu=', 'iommu=')) for x in c['append'].split()):
        raise ValueError('Guest IOMMU overrides are unsupported with the RDM VFIO mode')
    return c


def command(c):
    run_dir = ROOT / 'build/running' / c['name']
    coordinated = bool(c['vfio'] and c['chameleon'])
    memory = {'qom-type': 'memory-backend-ram', 'id': 'ram0',
              'size': c['memory_mib'] * 1024 * 1024, 'share': False,
              'merge': False, 'prealloc': False}
    if coordinated:
        memory['thp'] = False
    if c['host_numa_node'] is not None:
        memory.update({'host-nodes': [c['host_numa_node']], 'policy': 'bind'})
    args = [str(path(c['qemu'])), '-name', c['name'], '-L', '/usr/share/qemu',
            '-accel', 'kvm,hyperalloc-pebs-meminfo=on' if c['pebs'] else 'kvm',
            '-cpu', 'host,migratable=off,pmu=on', '-m', str(c['memory_mib']),
            '-smp', str(c['cpus']), '-object', json.dumps(memory),
            '-machine', 'q35,memory-backend=ram0,mem-merge=off', '-nodefaults', '-display', 'none',
            '-serial', 'stdio', '-qmp',
            f'unix:{run_dir}/qmp.sock,server=on,wait=off',
            '-pidfile', str(run_dir / 'qemu.pid')]
    if not c['reboot']:
        args += ['-no-reboot']
    if c['kernel']:
        args += ['-kernel', str(path(c['kernel']))]
        if c['initrd']:
            args += ['-initrd', str(path(c['initrd']))]
        args += ['-append', f"root={c['root']} rw console=ttyS0 net.ifnames=0 biosdevname=0 " + c['append']]
    # -drive uses QemuOpts (not QAPI JSON); double commas quote literal commas.
    drive_file = str(path(c['disk'])).replace(',', ',,')
    args += ['-drive', f"file={drive_file},format={c['disk_format']},if=none,id=os,snapshot={'on' if c['snapshot'] else 'off'}",
             '-device', 'virtio-blk-pci,drive=os,bootindex=1']
    if c['seed']:
        seed_file = str(path(c['seed'])).replace(',', ',,')
        args += ['-drive', f'file={seed_file},format=raw,if=none,id=seed,readonly=on',
                 '-device', 'virtio-blk-pci,drive=seed']
    if c['network'] != 'none':
        if c['network'] == 'bridge':
            net = {'type': 'bridge', 'id': 'mgmt', 'br': c['bridge'], 'helper': c['bridge_helper']}
        else:
            net = {'type': 'user', 'id': 'mgmt', 'hostfwd': [{'str': f"tcp:127.0.0.1:{c['ssh_port']}-:22"}]}
        args += ['-netdev', json.dumps(net), '-device', f"virtio-net-pci,netdev=mgmt,mac={c['mac']}"]
    for thread in ['auto'] + [f'install{i}' for i in range(c['cpus'])]:
        args += ['-object', f'iothread,id={thread}']
    balloon = {'driver': 'virtio-llfree-balloon', 'id': 'ha',
               'auto-mode': c['auto_mode'], 'auto-mode-iothread': 'auto',
               'chameleon': c['chameleon'], 'chameleon-policy': c['policy'],
               'iothread-vq-mapping': [{'iothread': f'install{i}'} for i in range(c['cpus'])]}
    if c['vfio']:
        balloon['vfio'] = True
    if coordinated:
        balloon['chameleon-vfio'] = True
    # Register the discard manager before any VFIO listener maps RAM.
    args += ['-device', json.dumps(balloon)]
    for i, bdf in enumerate(c['vfio']):
        args += ['-device', json.dumps({'driver': 'pcie-root-port', 'id': f'rdma{i}',
                                      'chassis': i + 1, 'slot': i + 1}),
                 '-device', json.dumps({'driver': 'vfio-pci', 'host': bdf, 'bus': f'rdma{i}'})]
    return args


def probe_kvm(c):
    """Exercise required opt-ins on a temporary VM without RAM or vCPUs."""
    capabilities = []
    if c['chameleon']:
        capabilities.append(('Chameleon VFIO' if c['vfio'] else 'Chameleon',
                             0x48410002, int(bool(c['vfio']))))
    if c['pebs']:
        capabilities.append(('PEBS MEMINFO', 0x48410001, 0))
    if not capabilities:
        return []
    kvm = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
    machine = None
    try:
        machine = fcntl.ioctl(kvm, 0xae01, 0)  # KVM_CREATE_VM
        for name, capability, flags in capabilities:
            # struct kvm_enable_cap: cap, flags, args[4], pad[64]. Checking
            # extension presence alone cannot distinguish the old C4 ABI,
            # which rejects args[1]=KVM_CHAMELEON_ENABLE_VFIO.
            request = struct.pack('=II4Q64x', capability, 0, 1, flags, 0, 0)
            try:
                fcntl.ioctl(machine, 0x4068aea3, request)  # KVM_ENABLE_CAP
            except OSError as error:
                raise ValueError(f'Running Host cannot enable {name}: {error}; boot the matching deployment Host kernel') from error
        return [name for name, _, _ in capabilities]
    finally:
        if machine is not None:
            os.close(machine)
        os.close(kvm)


def preflight(c):
    for key in ['qemu', 'disk', 'kernel', 'initrd', 'seed']:
        if c[key] and not path(c[key]).is_file():
            raise ValueError(f'Missing {key}: {path(c[key])}')
    if not os.access('/dev/kvm', os.R_OK | os.W_OK):
        raise ValueError('This user needs read/write access to /dev/kvm')
    capabilities = probe_kvm(c)
    if c['host_cpus'] and not set(c['host_cpus']) <= os.sched_getaffinity(0):
        raise ValueError('host_cpus includes CPUs unavailable to this process')
    if c['host_numa_node'] is not None and not (SYS / f"devices/system/node/node{c['host_numa_node']}").exists():
        raise ValueError('Selected host NUMA node does not exist')
    if c['network'] == 'bridge':
        if not (SYS / 'class/net' / c['bridge'] / 'bridge').exists():
            raise ValueError(f"Bridge does not exist: {c['bridge']}")
        if not os.access(c['bridge_helper'], os.X_OK):
            raise ValueError(f"Missing bridge helper: {c['bridge_helper']}")
    if c['network'] == 'user':
        result = subprocess.run([str(path(c['qemu'])), '-netdev', 'help'], capture_output=True, text=True, check=True)
        if 'user' not in result.stdout.split():
            raise ValueError('Rebuild deploy QEMU with --enable-slirp (requires libslirp development files)')
    selected = set(c['vfio'])
    for bdf in selected:
        device = SYS / 'bus/pci/devices' / bdf
        if not (device / 'physfn').exists():
            raise ValueError(f'{bdf} is not an SR-IOV VF; select a VF from hermit-vfio.py inspect')
        parent_driver = device / 'physfn/driver'
        if not parent_driver.exists() or parent_driver.resolve().name not in ('mlx5_core', 'mlx4_core'):
            raise ValueError(f'{bdf} requires its RDMA PF to retain the native driver')
        if not (device / 'driver').exists() or (device / 'driver').resolve().name != 'vfio-pci':
            raise ValueError(f'{bdf} must be bound to vfio-pci using hermit-vfio.py bind')
        if not (device / 'iommu_group').exists():
            raise ValueError(f'{bdf} has no IOMMU group')
        group = (device / 'iommu_group').resolve()
        members = {p.name for p in (group / 'devices').iterdir()}
        if not members <= selected:
            raise ValueError(f'Pass every function in IOMMU group {group.name}: {sorted(members)}')
        if not os.access('/dev/vfio/' + group.name, os.R_OK | os.W_OK):
            raise ValueError(f'No access to /dev/vfio/{group.name}')
    if selected:
        limit = resource.getrlimit(resource.RLIMIT_MEMLOCK)[0]
        required = c['memory_mib'] * 1024 * 1024 + 64 * 1024 * 1024
        if limit != resource.RLIM_INFINITY and limit < required:
            raise ValueError(f'RLIMIT_MEMLOCK must cover guest RAM plus overhead ({required} bytes); set ulimit -l before launch')
        if c['chameleon']:
            entries = SYS / 'module/vfio_iommu_type1/parameters/dma_entry_limit'
            required_entries = c['memory_mib'] * 256 + 512
            if not entries.is_file() or int(entries.read_text()) < required_entries:
                raise ValueError(f'vfio_iommu_type1.dma_entry_limit must be >= {required_entries}; run hermit-vfio.py configure-vfio --memory-mib {c["memory_mib"]} --apply before starting the VM')
    return {'status': 'PASS', 'kernel_mode': 'direct' if c['kernel'] else 'disk-grub',
            'vfio': c['vfio'], 'kvm_opt_ins': capabilities, 'hardware_rdma_tested': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--run', action='store_true')
    args = p.parse_args()
    c = config(json.loads(args.config.read_text()))
    cmd = command(c)
    if not args.run:
        print(json.dumps(preflight(c), indent=2) if args.check else shlex.join(cmd))
        return
    preflight(c)
    run_dir = ROOT / 'build/running' / c['name']
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / 'launcher.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (run_dir / 'qmp.sock').unlink(missing_ok=True)
        (run_dir / 'config.json').write_text(json.dumps(c, indent=2) + '\n')
        (run_dir / 'command.json').write_text(json.dumps(cmd, indent=2) + '\n')
        if c['host_cpus']:
            os.sched_setaffinity(0, set(c['host_cpus']))
        # Stay in the foreground: serial console, Ctrl-C, and QMP remain usable.
        process = subprocess.Popen(cmd)
        try:
            raise SystemExit(process.wait())
        except KeyboardInterrupt:
            process.terminate()
            process.wait(timeout=30)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'run-deploy-vm: {error}', file=sys.stderr)
        raise SystemExit(1)
