#!/usr/bin/env python3
"""Build the two deployment kernels, matching Guest Hermit module and QEMU.

Prints commands by default; --apply performs the build. No installation/reboot.
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HA = ROOT / 'hyperalloc-6.18'


def commands(args):
    deploy = HA / 'scripts/kernel-deploy.py'
    result = []
    for role in ('host', 'guest'):
        if args.component not in ('all', role):
            continue
        if role == 'host':
            result.append(['python3', deploy, 'configure', '--role', role,
                           '--base-config', args.host_config, '--cc', args.cc])
        result += [['python3', deploy, 'build', '--role', role, '--jobs', str(args.jobs), '--cc', args.cc],
                   ['python3', deploy, 'package', '--role', role, '--cc', args.cc]]
    if args.component in ('all', 'qemu'):
        result.append(['bash', HA / 'scripts/build-deploy-qemu.sh'])
    if args.component in ('all', 'tools'):
        gen = ROOT / 'benchmarks/memcached-trace-generator'
        result += [['cmake', '-S', gen, '-B', gen / 'build-release', '-DCMAKE_BUILD_TYPE=Release',
                    '-DHDR_HISTOGRAM_ROOT=' + str(ROOT / 'benchmarks/vendor/HdrHistogram_c')],
                   ['cmake', '--build', gen / 'build-release', '-j', str(args.jobs)],
                   ['bash', ROOT / 'benchmarks/scripts/build-fig9-rdma-server.sh']]
    return [[str(x) for x in cmd] for cmd in result]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--component', choices=('all', 'host', 'guest', 'qemu', 'tools'), default='all')
    p.add_argument('--jobs', type=int, default=16)
    p.add_argument('--cc', default='gcc')
    p.add_argument('--host-config', type=Path, default=Path('/boot/config-' + os.uname().release))
    g = p.add_mutually_exclusive_group()
    g.add_argument('--apply', action='store_true')
    g.add_argument('--plan', action='store_true')
    a = p.parse_args(argv)
    if a.jobs < 1:
        p.error('--jobs must be positive')
    env = dict(os.environ, JOBS=str(a.jobs))
    for cmd in commands(a):
        print('+ ' + shlex.join(cmd), flush=True)
        if a.apply:
            subprocess.run(cmd, cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
