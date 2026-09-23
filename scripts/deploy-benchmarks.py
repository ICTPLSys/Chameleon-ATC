#!/usr/bin/env python3
"""Populate the running template with apps, build them and prepare frozen inputs.

Inputs are streamed from the packaged datasets. Graph500 is generated once and
cached outside measured runs. The completed template is shut down for overlays.
Default --plan prints commands only; --apply transfers/builds/prepares data.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HA = ROOT / 'hyperalloc-6.18'
APP_DIRS = ('xsbench', 'liblinear-multicore-2.50', 'graphchi', 'graph500-omp',
            'metis', 'pvc', 'memcached', 'apache-cassandra-5.0.1', 'spark-3.3.1-bin-hadoop3')


def execute(cmd, apply, **kwargs):
    print('+ ' + shlex.join([str(x) for x in cmd]), flush=True)
    if apply:
        return subprocess.run([str(x) for x in cmd], check=True, **kwargs)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory', type=Path, default=ROOT / 'ae/config/host.json')
    p.add_argument('--jobs', type=int, default=8)
    p.add_argument('--points', type=Path, default=ROOT / 'ae/config/fig78-points.json',
                   help='Frozen workload definitions used by the Fig7/8 runner')
    p.add_argument('--keep-running', action='store_true', help='Leave template running for manual inspection')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--apply', action='store_true')
    g.add_argument('--plan', action='store_true')
    a = p.parse_args(argv)
    if a.jobs < 1:
        p.error('--jobs must be positive')
    inventory = a.inventory if a.inventory.exists() or a.apply else ROOT / 'ae/config/host.example.json'
    name = json.loads(inventory.read_text())['template_vm']
    access_file = HA / 'build/guests' / name / 'access.json'
    user = json.loads(access_file.read_text())['user'] if access_file.exists() else 'ubuntu'
    remote_root = '/home/' + user + '/chameleon-benchmarks'
    inputs = '/home/' + user + '/chameleon-inputs'
    ctl = ['python3', str(HA / 'scripts/guestctl.py'), '--name', name]
    execute(ctl + ['wait', '--cloud-init'], a.apply)
    execute(ctl + ['exec', '--shell',
            'sudo -n apt-get update && sudo -n apt-get install -y --no-install-recommends '
            'build-essential cmake patch zlib1g-dev libevent-dev autoconf automake libtool '
            'openjdk-11-jdk openjdk-17-jdk && sudo -n update-alternatives --set java '
            '/usr/lib/jvm/java-17-openjdk-amd64/bin/java'], a.apply)
    execute(ctl + ['exec', '--', 'mkdir', '-p', remote_root, inputs, inputs + '/graph500'], a.apply)
    members = ['scripts', 'patches', 'spark-kmeans'] + ['apps/' + x for x in APP_DIRS]
    tar = ['tar', '--exclude=.git', '--exclude=__pycache__', '-C', str(ROOT / 'benchmarks'), '-cf', '-', *members]
    target = 'tar -xf - -C ' + shlex.quote(remote_root)
    print('+ ' + shlex.join(tar) + ' | guestctl exec --shell ' + shlex.quote(target), flush=True)
    if a.apply:
        for member in members:
            if not (ROOT / 'benchmarks' / member).exists():
                raise RuntimeError('Missing packaged application: ' + member)
        spec = importlib.util.spec_from_file_location('ae_guestctl', HA / 'scripts/guestctl.py')
        guest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guest)
        access = guest.load_access(access_file)
        producer = subprocess.Popen(tar, stdout=subprocess.PIPE)
        try:
            result = subprocess.run(guest.ssh_command(access, shell=target), stdin=producer.stdout)
            producer.stdout.close()
            status = producer.wait()
            if status or result.returncode:
                raise RuntimeError(f'Application transfer failed: tar={status}, ssh={result.returncode}')
        finally:
            if producer.poll() is None:
                producer.terminate()
                producer.wait()
    execute(ctl + ['upload', ROOT / 'scripts/build-guest-apps.sh', remote_root + '/build-guest-apps.sh'], a.apply)
    execute(ctl + ['exec', '--', 'bash', remote_root + '/build-guest-apps.sh', remote_root, str(a.jobs)], a.apply)
    frozen = json.loads(a.points.read_text())
    preparation = {'applications': {
        case: app['workload_configuration'] for case, app in frozen['applications'].items()}}
    prepare_config = ROOT / 'ae/build/preparation-workloads.json'
    if a.apply:
        prepare_config.parent.mkdir(parents=True, exist_ok=True)
        prepare_config.write_text(json.dumps(preparation, indent=2) + '\n')
    execute(['python3', ROOT / 'benchmarks/scripts/prepare-table2-inputs.py',
             '--vm', name, '--config', prepare_config], a.apply)
    graph_args = preparation['applications']['graph500']['args']
    def graph_value(flag):
        return graph_args[graph_args.index(flag) + 1].replace('{inputs}', inputs)
    execute(ctl + ['exec', '--', 'bash', remote_root + '/scripts/run-graph500.sh',
                  '--skip-build', '--scale', graph_value('--scale'), '--edgefactor', graph_value('--edgefactor'),
                  '--threads', str(a.jobs), '--bfs-iterations', graph_value('--bfs-iterations'),
                  '--graph-cache', graph_value('--graph-cache'), '--prepare-only'], a.apply)
    execute(ctl + ['exec', '--', 'sync'], a.apply)
    if not a.keep_running:
        execute(ctl + ['stop'], a.apply)
    print('Keep the template offline and unchanged while experiment overlays exist.')


if __name__ == '__main__':
    main()
