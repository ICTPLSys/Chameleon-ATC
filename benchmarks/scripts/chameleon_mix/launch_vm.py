#!/usr/bin/env python3
"""Launch a pinned VM with explicit single-node or interleaved host memory."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chameleon_mix.common import HA, VMROOT, load

deploy = load('concurrent_deploy', HA / 'scripts/run-deploy-vm.py')
deploy.ROOT = VMROOT
deploy.path = lambda value: Path(value).expanduser() if Path(value).expanduser().is_absolute() else HA / value
deploy.DEFAULTS['host_memory_nodes'] = []
original_config = deploy.config
original_command = deploy.command
original_preflight = deploy.preflight


def config(raw):
    value = original_config(raw)
    nodes = value['host_memory_nodes']
    if (not isinstance(nodes, list) or not nodes or
            any(type(n) is not int or n < 0 for n in nodes) or len(set(nodes)) != len(nodes)):
        raise ValueError('host_memory_nodes must contain distinct NUMA node IDs')
    if value['memory_mib'] % (2 * len(nodes)):
        raise ValueError('Interleaved memory must have a 2-MiB-aligned share on every node')
    return value


def command(value):
    argv = original_command(value)
    index = argv.index('-object') + 1
    memory = json.loads(argv[index])
    nodes = value['host_memory_nodes']
    memory.update({'host-nodes': nodes, 'policy': 'bind' if len(nodes) == 1 else 'interleave'})
    argv[index] = json.dumps(memory)
    return argv


def preflight(value):
    for node in value['host_memory_nodes']:
        if not (deploy.SYS / f'devices/system/node/node{node}/meminfo').exists():
            raise ValueError('Missing memory node: ' + str(node))
    return original_preflight(value)


deploy.config = config
deploy.command = command
deploy.preflight = preflight
if __name__ == '__main__':
    deploy.main()
