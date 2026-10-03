"""Physical-core isolation with explicit NUMA sets for host clients."""
import argparse
import os
from pathlib import Path
import re
import signal
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chameleon_mix.common import SCRIPTS, load

base = load('concurrent_affinity_base', SCRIPTS / 'chameleon_affinity.py')
for name in dir(base):
    if not name.startswith('_'):
        globals()[name] = getattr(base, name)


def node_set(value):
    text = str(value)
    if not re.fullmatch(r'\d+(?:,\d+)*', text):
        raise ValueError('Expected comma-separated NUMA node IDs')
    nodes = [int(v) for v in text.split(',')]
    if len(set(nodes)) != len(nodes):
        raise ValueError('Duplicate NUMA nodes')
    return nodes


def validate_client_cpus(value, node, rows=None, allowed=None):
    rows = topology() if rows is None else rows
    nodes = node_set(node)
    if not set(nodes) <= {r['node'] for r in rows}:
        raise ValueError('Client NUMA node does not exist')
    selected = [dict(row, node=0) for row in rows if row['node'] in nodes]
    return base.validate_client_cpus(value, 0, selected, allowed)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--pid', type=int)
    group.add_argument('--client-cpus')
    p.add_argument('--node', required=True)
    p.add_argument('--run-command', action='store_true')
    p.add_argument('--evidence', type=Path)
    p.add_argument('--also-pid', type=int, action='append', default=[])
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args()
    if a.pid is not None:
        nodes = node_set(a.node)
        if len(nodes) != 1:
            p.error('VM CPU affinity requires one node')
        print(json.dumps(verify_node(a.pid, nodes[0])))
        return 0
    cpus = validate_client_cpus(a.client_cpus, a.node)
    if not a.run_command:
        print(cpu_list(cpus)); return 0
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not a.evidence or not command:
        p.error('Need --evidence and a command after --')
    def stop(signum, frame):
        raise KeyboardInterrupt('signal ' + str(signum))
    signal.signal(signal.SIGTERM, stop)
    return run_confined(command, cpus, a.evidence, also_pids=a.also_pid)


if __name__ == '__main__':
    sys.exit(main())
