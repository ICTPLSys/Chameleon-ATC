#!/usr/bin/env python3
"""Create and prepare the independent disk Guest used by all AE overlays.

Requires the built Guest package and custom QEMU; --apply creates/boots the VM.
Source applications and inputs are installed separately by deploy-benchmarks.py.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HA = ROOT / 'hyperalloc-6.18'


def commands(a, inventory):
    name = inventory['template_vm']
    create = ['python3', str(HA / 'scripts/create-deploy-guest.py'), '--name', name,
              '--disk-size', a.disk_size, '--memory-mib', str(a.memory_mib),
              '--cpus', str(a.cpus), '--ssh-port', str(a.ssh_port), '--user', a.user]
    if a.image:
        create += ['--image', str(a.image.resolve())]
    ctl = ['python3', str(HA / 'scripts/guestctl.py'), '--name', name]
    return [create, ctl + ['start', '--bootstrap', '--wait'], ctl + ['setup'],
            ctl + ['install-kernel'], ctl + ['stop'], ctl + ['start', '--wait'],
            ctl + ['exec', '--', 'uname', '-r']]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory', type=Path, default=ROOT / 'ae/config/host.json')
    p.add_argument('--image', type=Path, help='Existing Ubuntu 22.04 cloud image; otherwise download official image')
    p.add_argument('--disk-size', default='350G')
    p.add_argument('--memory-mib', type=int, default=65536)
    p.add_argument('--cpus', type=int, default=12)
    p.add_argument('--ssh-port', type=int, default=5220)
    p.add_argument('--user', default='ubuntu')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--apply', action='store_true')
    g.add_argument('--plan', action='store_true')
    a = p.parse_args(argv)
    path = a.inventory
    if not a.apply and not path.exists():
        path = ROOT / 'ae/config/host.example.json'
    inv = json.loads(path.read_text())
    for cmd in commands(a, inv):
        print('+ ' + shlex.join(cmd), flush=True)
        if a.apply:
            subprocess.run(cmd, cwd=ROOT, check=True)
    print('Next: scripts/deploy-benchmarks.py --apply; it prepares inputs and shuts down the template.')


if __name__ == '__main__':
    main()
