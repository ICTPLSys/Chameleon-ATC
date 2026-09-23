#!/usr/bin/env python3
"""Build/install Hermit's native RDMA memory-server binary over SSH.

The experiment runners own the individual pool processes (one for a single VM,
three for Fig9). This preparation step never starts a pool or changes IPs.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def remote_script(binary, install_deps=False):
    if not binary.startswith('/') or '\n' in binary or '\r' in binary:
        raise ValueError('server.binary must be an absolute remote path')
    deps = ('sudo -n apt-get update\nsudo -n apt-get install -y --no-install-recommends '
            'build-essential rdma-core ibverbs-providers libibverbs-dev librdmacm-dev\n') if install_deps else ''
    return ('set -eu\n' + deps +
            'stage=$(mktemp -d)\ntrap \'rm -rf -- "$stage"\' EXIT\n'
            'tar -xf - -C "$stage"\n'
            'make -C "$stage/server"\n'
            'sudo -n install -d -m 0755 ' + shlex.quote(str(Path(binary).parent)) + '\n'
            'sudo -n install -m 0755 "$stage/server/rswap-server" ' + shlex.quote(binary) + '\n'
            'printf \'%s\\n\' ' + shlex.quote('Installed ' + binary) + '\n')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory', type=Path, default=ROOT / 'ae/config/host.json')
    p.add_argument('--install-deps', action='store_true', help='Run apt on the memory server first')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--apply', action='store_true')
    g.add_argument('--plan', action='store_true')
    a = p.parse_args(argv)
    path = a.inventory if a.inventory.exists() or a.apply else ROOT / 'ae/config/host.example.json'
    server = json.loads(path.read_text())['server']
    host = server['ssh_host']
    if host.startswith('-') or any(c.isspace() for c in host):
        p.error('invalid server SSH destination')
    script = remote_script(server['binary'], a.install_deps)
    cmd = ['ssh', '-o', 'BatchMode=yes', host, 'bash -c ' + shlex.quote(script)]
    tar = ['tar', '-C', str(ROOT / 'hyperalloc-6.18/hermit'), '-cf', '-',
           'wire.h', 'server/rswap_server.c', 'server/Makefile']
    print('+ ' + shlex.join(tar) + ' | ' + shlex.join(cmd), flush=True)
    if a.apply:
        stream = subprocess.Popen(tar, stdout=subprocess.PIPE)
        try:
            result = subprocess.run(cmd, stdin=stream.stdout)
            stream.stdout.close()
            status = stream.wait()
            if result.returncode or status:
                raise RuntimeError(f'Server build failed: tar={status}, ssh={result.returncode}')
        finally:
            if stream.poll() is None:
                stream.terminate()
                stream.wait()


if __name__ == '__main__':
    main()
