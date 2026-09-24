#!/usr/bin/env python3
"""Write an AE inventory using the reviewer's existing VF and RDMA addresses.

This only writes JSON; it never creates VFs, changes networking or boots VMs.
Without --write it prints the inventory for review.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, default=ROOT / 'ae/config/host.example.json')
    p.add_argument('--output', type=Path, default=ROOT / 'ae/config/host.json')
    p.add_argument('--server-ssh', help='SSH alias or user@host; public-key login must work')
    p.add_argument('--server-address', help='Reachable native RDMA IPv4 address')
    p.add_argument('--server-binary', help='Absolute path on the memory server')
    p.add_argument('--template', help='Offline guestctl template name')
    p.add_argument('--vf', action='append', help='One PCI BDF per VM; specify exactly three times')
    p.add_argument('--guest-address', action='append', help='One IPv4 CIDR per VM; specify three times')
    p.add_argument('--single-address', help='IPv4 CIDR for the single-VM experiment')
    p.add_argument('--single-server-port', type=int, help='Dedicated single-VM RDMA pool port; default 9404')
    p.add_argument('--guest-interface', help='RDMA interface name as seen INSIDE each Guest')
    p.add_argument('--client-numa-node', type=int)
    p.add_argument('--write', action='store_true')
    p.add_argument('--plan', action='store_true', help='Print only (the default)')
    a = p.parse_args(argv)
    if a.write and a.plan:
        p.error('--write and --plan are mutually exclusive')
    data = json.loads(a.input.read_text())
    if a.server_ssh:
        if a.server_ssh.startswith('-') or any(c.isspace() for c in a.server_ssh):
            p.error('server-ssh must be a single SSH destination')
        data['server']['ssh_host'] = a.server_ssh
    if a.server_address:
        data['server']['address'] = str(ipaddress.IPv4Address(a.server_address))
    if a.server_binary:
        if not a.server_binary.startswith('/'):
            p.error('server-binary must be an absolute remote path')
        data['server']['binary'] = a.server_binary
    if a.template:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}', a.template):
            p.error('invalid template name')
        data['template_vm'] = a.template
    if a.client_numa_node is not None:
        if a.client_numa_node < 0:
            p.error('client-numa-node must be nonnegative')
        data['client_numa_node'] = a.client_numa_node
    if a.vf:
        if len(a.vf) != 3 or len(set(a.vf)) != 3 or any(
                not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', v) for v in a.vf):
            p.error('specify three different PCI BDFs, e.g. 0000:69:00.3')
        for slot, vf in zip(data['slots'], a.vf):
            slot['vfio'] = [vf.lower()]
        data['single_slot']['vfio'] = [a.vf[0].lower()]
    if a.guest_address:
        if len(a.guest_address) != 3:
            p.error('--guest-address must occur exactly three times')
        for slot, address in zip(data['slots'], a.guest_address):
            slot['rdma_address'] = str(ipaddress.IPv4Interface(address))
    if a.single_address:
        data['single_slot']['rdma_address'] = str(ipaddress.IPv4Interface(a.single_address))
    if a.single_server_port is not None:
        if not 1 <= a.single_server_port <= 65535:
            p.error('--single-server-port must be in 1..65535')
        if a.single_server_port in {slot['server_port'] for slot in data['slots']}:
            p.error('--single-server-port must differ from the Fig9 pool ports')
        data['single_slot']['server_port'] = a.single_server_port
    if a.guest_interface:
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', a.guest_interface):
            p.error('invalid Linux interface name')
        for slot in [*data['slots'], data['single_slot']]:
            slot['rdma_interface'] = a.guest_interface
    text = json.dumps(data, indent=2) + '\n'
    if a.write:
        if a.output.exists():
            p.error(f'{a.output} exists; use a new --output or edit it directly')
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
