#!/usr/bin/env python3
"""Provision three idle IB VFs for Fig9; default is a read-only plan.

Stop the template VM first. --apply explicitly permits an idle VF population
resize (through zero), journals original drivers/GUIDs, and emits inventory.
--restore returns that PF's VF population to the journaled state after VMs stop.
The PF driver and both machines' IP configuration are left intact.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time

import chameleon_fig9_runtime as rt

vf = rt.module('fig9_vfio', rt.HA / 'scripts/hermit-vfio.py')


def command(argv):
    return subprocess.run(argv, check=True, text=True, capture_output=True).stdout


def link(interface):
    return json.loads(command(['ip', '-j', '-d', 'link', 'show', 'dev', interface]))[0]


def children(parent):
    return [p.resolve() for p in sorted(parent.glob('virtfn*'), key=lambda p: int(p.name[6:]))]


def snapshot(parent, interface):
    info = link(interface)
    records = []
    for i, dev in enumerate(children(parent)):
        guid = next(x for x in info['vfinfo_list'] if x['vf'] == i)
        records.append({'index': i, **vf.describe(dev),
                        'node_guid': guid['node guid'], 'port_guid': guid['port guid'],
                        'link_state': guid['link_state'],
                        'vfio_acl': command(['getfacl', '-p', str(vf.DEV / 'vfio' / vf.group_members(dev)[0])])
                        if (vf.DEV / 'vfio' / vf.group_members(dev)[0]).exists() else None})
    return {'version': 1, 'boot_id': vf.read(vf.PROC / 'sys/kernel/random/boot_id'),
            'pf': parent.name, 'identity': vf.identity(parent), 'interface': interface,
            'pf_port_address': info['address'],
            'autoprobe': vf.read(parent / 'sriov_drivers_autoprobe'),
            'count': len(records), 'devices': records, 'status': 'prepared'}


def require_idle(parent):
    vf.require_idle(parent)
    for dev in children(parent):
        vf.require_idle(dev)
        if vf.driver(dev) not in (None, 'mlx5_core', 'vfio-pci'):
            raise ValueError('Unexpected VF driver: ' + str(vf.driver(dev)))
        if vf.group_members(dev)[1] != [dev.name]:
            raise ValueError('Each Fig9 VF must have a separate IOMMU group')


def population(parent, count):
    if int(vf.read(parent / 'sriov_numvfs')) == count:
        return
    require_idle(parent)
    # Releasing vfio first prevents removal of a bound VFIO device; PF remains native.
    for dev in children(parent):
        vf.rebind(dev, 'mlx5_core', '')
    vf.write(parent / 'sriov_numvfs', 0)
    if count:
        vf.write(parent / 'sriov_numvfs', count)
    if int(vf.read(parent / 'sriov_numvfs')) != count:
        raise RuntimeError('PF did not accept VF population')
    for _ in range(100):
        if len(children(parent)) == count:
            return
        time.sleep(.05)
    raise RuntimeError('VF enumeration incomplete')


def configure(interface, index, node, port, state='auto'):
    # Linux 6.18 uses standard rtnetlink; legacy OFED sriov/INDEX/node is absent.
    for name, value in [('node_guid', node), ('port_guid', port), ('state', state)]:
        command(['ip', 'link', 'set', 'dev', interface, 'vf', str(index), name, value])
    actual = next(x for x in link(interface)['vfinfo_list'] if x['vf'] == index)
    if (actual['node guid'], actual['port guid'], actual['link_state']) != (node, port, state):
        raise RuntimeError('VF GUID/state readback differs from request')


def guid_plan(before):
    """Preserve existing GUIDs; derive unique local-admin GUIDs for added VFs."""
    used = {x[k] for x in before['devices'] for k in ('node_guid', 'port_guid')}
    result = []
    for i in range(3):
        if i < before['count']:
            result.append({k: before['devices'][i][k] for k in ('node_guid', 'port_guid', 'link_state')})
        else:
            # Include physical port identity so equal PCI BDFs on different
            # machines do not generate the same fabric GUIDs.
            seed = before.get('pf_port_address', before['pf']) + '/' + str(i)
            raw = '02' + hashlib.sha256(seed.encode()).hexdigest()[:14]
            guid = ':'.join(raw[p:p + 2] for p in range(0, 16, 2))
            if guid in used:
                raise ValueError('Generated GUID collides with existing VF')
            used.add(guid)
            result.append({'node_guid': guid, 'port_guid': guid, 'link_state': 'auto'})
    return result


def restore(saved):
    if saved['boot_id'] != vf.read(vf.PROC / 'sys/kernel/random/boot_id'):
        raise ValueError('Journal is from another boot; inspect hardware before recovery')
    parent = vf.require_pf(saved['pf'])
    if vf.identity(parent) != saved['identity']:
        raise ValueError('PF PCI identity differs from journal')
    require_idle(parent)
    population(parent, saved['count'])
    for entry, dev in zip(saved['devices'], children(parent)):
        vf.rebind(dev, 'mlx5_core', '')
        configure(saved['interface'], entry['index'], entry['node_guid'], entry['port_guid'], entry['link_state'])
        vf.rebind(dev, entry['driver'], entry['driver_override'])
        if entry.get('vfio_acl') and entry['driver'] == 'vfio-pci':
            node = vf.DEV / 'vfio' / vf.group_members(dev)[0]
            # Group numbers can change after recreation; rewrite only the ACL header.
            acl = '\n'.join('# file: ' + str(node) if line.startswith('# file: ') else line
                            for line in entry['vfio_acl'].splitlines()) + '\n'
            subprocess.run(['setfacl', '--restore=-'], input=acl, text=True, check=True)
    saved['status'] = 'restored'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pf', type=vf.bdf, default='0000:69:00.0')
    parser.add_argument('--interface', default='ibs2f0')
    parser.add_argument('--user', default=os.environ.get('SUDO_USER', 'a'))
    parser.add_argument('--example', type=Path, default=rt.ROOT / 'benchmarks/config/chameleon-fig9-host.example.json')
    parser.add_argument('--inventory', type=Path, default=rt.ROOT / 'benchmarks/config/chameleon-fig9-host.json')
    parser.add_argument('--state', type=Path, default=rt.HA / 'build/hardware/fig9-vfs.json')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true')
    group.add_argument('--restore', action='store_true')
    args = parser.parse_args(argv)
    if (args.apply or args.restore) and os.geteuid() != 0:
        parser.error('--apply/--restore require root')
    if args.restore:
        with vf.state_lock(args.state):
            saved = json.loads(args.state.read_text())
            restore(saved)
            vf.save_state(args.state, saved)
        print('RESTORED ' + str(args.state))
        return
    parent = vf.require_pf(args.pf)
    if args.interface not in {p.name for p in (parent / 'net').iterdir()}:
        raise ValueError('Interface is not on the selected PF')
    info = link(args.interface)
    if info.get('link_type') != 'infiniband':
        raise ValueError('This helper provisions native InfiniBand VFs')
    count = int(vf.read(parent / 'sriov_numvfs'))
    if count > 3 or int(vf.read(parent / 'sriov_totalvfs')) < 3:
        raise ValueError('Expected at most three existing VFs on a PF supporting three')
    if vf.read(parent / 'sriov_drivers_autoprobe') != '1':
        raise ValueError('Enable native VF autoprobe before provisioning')
    inventory = json.loads(args.example.read_text())
    rt.validate_inventory(inventory)
    original = snapshot(parent, args.interface)
    guids = guid_plan(original)
    plan = {'pf': args.pf, 'old_count': count, 'new_count': 3,
            'guids': guids, 'server': inventory['server'], 'inventory': str(args.inventory),
            'state': str(args.state), 'requires_offline_template': inventory['template_vm']}
    print(json.dumps(plan, indent=2), flush=True)
    if not args.apply:
        return
    user = pwd.getpwnam(args.user)
    template = rt.access(inventory['template_vm'])
    with rt.guest.control_lock(template), vf.state_lock(args.state):
        if rt.guest.active(template) or not rt.guest.launcher_idle(template):
            raise ValueError('Stop template VM before resizing its VF population')
        if args.state.exists() and json.loads(args.state.read_text()).get('status') not in ('restored', 'rolled-back'):
            raise ValueError('Existing active journal: use its inventory, or --restore first')
        require_idle(parent)
        original = snapshot(parent, args.interface)
        if original['count'] > 3:
            raise ValueError('VF population changed since planning; inspect again')
        guids = guid_plan(original)
        vf.save_state(args.state, original)
        try:
            population(parent, 3)
            devices = children(parent)
            for i, (dev, values) in enumerate(zip(devices, guids)):
                vf.rebind(dev, 'mlx5_core', '')
                configure(args.interface, i, values['node_guid'], values['port_guid'], values['link_state'])
            # Validate whole isolated groups and idle device fds before binding.
            binding = vf.bind_plan(args.pf, [d.name for d in devices])
            original['binding'] = binding
            original['permissions'] = []
            vf.save_state(args.state, original)
            command(['modprobe', 'vfio-pci'])
            command(['modprobe', 'vfio_iommu_type1'])
            limit = vf.vfio_limit_plan(90112, 4096)
            vf.write(Path(limit['path']), limit['target_entries'])
            for slot, dev in zip(inventory['slots'], devices):
                vf.rebind(dev, 'vfio-pci', 'vfio-pci')
                node = vf.DEV / 'vfio' / vf.group_members(dev)[0]
                acl = command(['getfacl', '-p', str(node)])
                original['permissions'].append({'path': str(node), 'acl': acl})
                vf.save_state(args.state, original)
                command(['setfacl', '-m', f'u:{user.pw_uid}:rw', str(node)])
                slot['vfio'] = [dev.name]
            if 'single_slot' in inventory:
                inventory['single_slot']['vfio'] = list(inventory['slots'][0]['vfio'])
            rt.validate_inventory(inventory, hardware=True)
            rt.save(args.inventory, inventory)
            os.chown(args.inventory, user.pw_uid, user.pw_gid)
            original.update(status='active', inventory=str(args.inventory), after=vf.inspect(args.pf))
            vf.save_state(args.state, original)
        except BaseException as error:
            try:
                restore(original)
                original['status'] = 'rolled-back'
                vf.save_state(args.state, original)
            except BaseException as recovery:
                raise RuntimeError(f'{error}; recovery failed: {recovery}; journal: {args.state}') from error
            raise
    print('READY ' + str(args.inventory))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('fig9-prepare: ' + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
