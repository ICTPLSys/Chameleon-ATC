"""Plan disjoint physical cores and NUMA memory for three resident VMs."""
import itertools
import math
import os
from pathlib import Path

from chameleon_mix import chameleon_affinity as affinity


def client_cores(applications):
    services = [a for a in applications if a['application'] in ('memcached', 'cassandra')]
    if len(services) > 1:
        raise ValueError('A Mix may contain at most one host-driven service')
    if not services:
        return 0
    service = services[0]
    args = service['workload_configuration']['args']
    def arg(name, default):
        return int(args[args.index(name) + 1]) if name in args else default
    return (arg('--workers', 8) + arg('--rx-threads', 2) + arg('--producer-shards', 2)
            if service['application'] == 'memcached' else arg('--threads', 16))


def validate_cpu_plan(placement, topology=None):
    rows = affinity.topology() if topology is None else topology
    by_cpu = {r['cpu']: r for r in rows}
    groups = {}
    for case, profile in placement['applications'].items():
        for role in ('vcpu_host_cpus', 'qemu_service_cpus'):
            cpus = profile[role]
            if any(cpu not in by_cpu or by_cpu[cpu]['node'] != profile['host_numa_node'] for cpu in cpus):
                raise ValueError('VM CPUs outside assigned node: ' + case)
            groups[case + '/' + role] = cpus
    if placement['client_cpus']:
        groups['host_client'] = affinity.validate_client_cpus(
            affinity.cpu_list(placement['client_cpus']), placement['client_numa_node'],
            rows, set(by_cpu))
    return affinity.validate_isolation(groups, rows)


def memory_charges(applications, placement, reserves, nodes):
    charges = {n: {'vm_memory_mib': 0, 'vm_overhead_mib': 0, 'client_reserve_mib': 0,
                   'host_reserve_mib': reserves['host_reserve_mib']} for n in nodes}
    for app in applications:
        profile = placement['applications'][app['application']]
        memory_nodes = profile['host_memory_nodes']
        for node in memory_nodes:
            charges[node]['vm_memory_mib'] += math.ceil(app['vm_memory_mib'] / len(memory_nodes))
        charges[profile['host_numa_node']]['vm_overhead_mib'] += reserves['per_vm_overhead_mib']
    if placement['client_cpus']:
        client_nodes = affinity.node_set(placement['client_numa_node'])
        for node in client_nodes:
            charges[node]['client_reserve_mib'] += math.ceil(reserves['client_reserve_mib'] / len(client_nodes))
    for row in charges.values():
        row['required_mib'] = sum(row.values())
    return charges


def plan(applications, inventory, snapshot, reserves, topology=None, allowed=None):
    rows = affinity.topology() if topology is None else topology
    allowed = set(os.sched_getaffinity(0) if allowed is None else allowed)
    physical = {}
    for row in sorted(rows, key=lambda r: r['cpu']):
        if row['cpu'] in allowed and row['node'] in snapshot:
            physical.setdefault((row['socket'], row['core']), row)
    pools = {n: [] for n in sorted(snapshot)}
    for row in physical.values():
        pools[row['node']].append(row['cpu'])
    if len(applications) != 3 or len({a['application'] for a in applications}) != 3:
        raise ValueError('Concurrent Mix requires exactly three distinct applications')
    required_cores = sum(a['workload_configuration']['vcpus'] + 2 for a in applications) + client_cores(applications)
    if required_cores > len(physical):
        raise ValueError(f'Mix requires {required_cores} disjoint physical cores; only {len(physical)} are allowed')
    nodes = sorted(pools)
    memory_options = [(n,) for n in nodes] + list(itertools.combinations(nodes, 2))
    if len(nodes) > 2:
        memory_options.append(tuple(nodes))
    winner = None
    for cpu_nodes in itertools.product(nodes, repeat=3):
        remaining = {n: list(cpus) for n, cpus in pools.items()}
        profiles = {}
        for app, node in zip(applications, cpu_nodes):
            count = app['workload_configuration']['vcpus']
            if len(remaining[node]) < count + 2:
                break
            chosen, remaining[node] = remaining[node][:count + 2], remaining[node][count + 2:]
            profiles[app['application']] = {
                'protocol': affinity.PROTOCOL, 'host_numa_node': node,
                'vcpu_host_cpus': chosen[:count], 'qemu_service_cpus': chosen[count:],
                'guest_application_cpus': list(range(count))}
        if len(profiles) != 3:
            continue
        count = client_cores(applications)
        for first in nodes:
            order = [first] + [n for n in nodes if n != first]
            client = [cpu for node in order for cpu in remaining[node]][:count]
            if len(client) != count:
                continue
            client_nodes = sorted({r['node'] for r in rows if r['cpu'] in client})
            for memory_nodes in itertools.product(memory_options, repeat=3):
                if any(app['vm_memory_mib'] % (2 * len(mem)) for app, mem in zip(applications, memory_nodes)):
                    continue
                cpus = {'applications': {a['application']: dict(profiles[a['application']], host_memory_nodes=list(mem))
                                          for a, mem in zip(applications, memory_nodes)},
                        'client_cpus': client, 'client_numa_node': ','.join(map(str, client_nodes or [first]))}
                charges = memory_charges(applications, cpus, reserves, nodes)
                limits = {n: min(snapshot[n]['total_mib'], snapshot[n]['available_estimate_mib']) for n in nodes}
                if any(charges[n]['required_mib'] > limits[n] for n in nodes):
                    continue
                service_nodes = [cpu_nodes[i] for i, a in enumerate(applications)
                                 if a['application'] in ('memcached', 'cassandra')]
                remote_mib = sum(a['vm_memory_mib'] * (1 - (1 / len(mem) if cpu in mem else 0))
                                 for a, cpu, mem in zip(applications, cpu_nodes, memory_nodes))
                score = (max(0, len(client_nodes) - 1),
                         sum(n in client_nodes for n in service_nodes),
                         remote_mib, sum(len(mem) > 1 for mem in memory_nodes),
                         max(charges[n]['required_mib'] / max(1, limits[n]) for n in nodes),
                         first != inventory['client_numa_node'], cpu_nodes, memory_nodes, first)
                if winner is None or score < winner[0]:
                    winner = (score, cpus, charges)
    if winner is None:
        raise ValueError('No feasible CPU/NUMA memory placement including full VM RAM, QEMU overhead and host/client reserves')
    _, cpus, charges = winner
    cpus['physical_isolation'] = validate_cpu_plan(cpus, rows)
    for node, charge in charges.items():
        charge.update(snapshot[node])
        charge['spare_mib'] = min(charge['total_mib'], charge['available_estimate_mib']) - charge['required_mib']
    return {'cpus': cpus, 'numa_memory': {'reserves': reserves, 'nodes': charges},
            'physical_core_count': required_cores}


def check_available(applications, placement, inventory, snapshot, reserves,
                    host_available_mib=None, local_pools_pending=False):
    charges = memory_charges(applications, placement, reserves, snapshot)
    for node, row in charges.items():
        available = min(snapshot[node]['total_mib'], snapshot[node]['available_estimate_mib'])
        row['available_estimate_mib'] = available
        if row['required_mib'] > available:
            raise ValueError(f'NUMA node {node}: need {row["required_mib"]} MiB, available estimate {available} MiB')
    if host_available_mib is None:
        host_available_mib = int(next(s.split()[1] for s in Path('/proc/meminfo').read_text().splitlines()
                                      if s.startswith('MemAvailable:'))) // 1024
    required = sum(row['required_mib'] for row in charges.values())
    if local_pools_pending and inventory['server'].get('manage_local'):
        required += 3 * 24576
    if required > host_available_mib:
        raise ValueError(f'Host needs {required} MiB available; has {host_available_mib} MiB')
    return {'nodes': charges, 'host_required_mib': required, 'host_available_mib': host_available_mib}
