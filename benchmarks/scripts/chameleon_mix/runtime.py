"""Reuse AE lifecycle operations with private Figure 9 VM state."""
import sys
from pathlib import Path
from chameleon_mix.common import HERE, ROOT, HA, VMROOT, SCRIPTS, load, configure_guest
from chameleon_mix import chameleon_affinity as affinity
from chameleon_mix import placement
from chameleon_mix import launch_vm

if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
rt = load('concurrent_runtime_base', SCRIPTS / 'chameleon_fig9_runtime.py')
rt.HA = VMROOT
rt.REMOTE_POOL_MIB = 24576
configure_guest(rt.guest)
rt.deploy = launch_vm.deploy
rt.affinity = affinity
rt.validate_cpu_plan = placement.validate_cpu_plan
original_slot_config = rt.slot_config


def slot_config(template, slot, high, cpu):
    value = original_slot_config(template, slot, high, cpu)
    value['host_memory_nodes'] = cpu['host_memory_nodes']
    for key in ('qemu', 'kernel', 'initrd'):
        if value.get(key) and not Path(value[key]).is_absolute():
            value[key] = str(HA / value[key])
    return value


def check_memory_available(applications, cpus, inventory, snapshot=None,
                           host_available_mib=None, local_pools_pending=False):
    return placement.check_available(applications, cpus, inventory,
        rt.numa_memory_snapshot() if snapshot is None else snapshot, rt.memory_reserves(inventory),
        host_available_mib, local_pools_pending)


def verify_vm_cpu_isolation(placement, guests, topology=None):
    physical = rt.validate_cpu_plan(placement, topology)
    if set(guests) != set(placement['applications']):
        raise ValueError('Every planned VM must participate in runtime affinity verification')
    evidence = {}
    for case, access in guests.items():
        pid = int((rt.guest.run_dir(access) / 'qemu.pid').read_text())
        evidence[case] = {'pid': pid, **rt.affinity.verify(
            pid, rt.guest.qmp(access, 'query-cpus-fast', timeout=15), placement['applications'][case])}
    return {'status': 'PASS', 'vms': evidence, 'physical_isolation': physical}


rt.verify_vm_cpu_isolation = verify_vm_cpu_isolation
rt.slot_config = slot_config
rt.check_memory_available = check_memory_available
engine = load('concurrent_engine_base', SCRIPTS / 'run-chameleon-fig9.py')
engine.rt = rt
def leaf_command(name, slot, high, affinity_file, workload_file, memory_file,
                 cpus, timeout, results_root):
    argv = [sys.executable, '-B', str(HERE / 'leaf.py'), '--name', name,
            '--vm', slot['name'], '--mode', 'chameleon', '--cases', high['application'],
            '--workload-config', str(workload_file), '--memory-plan', str(memory_file),
            '--cpu-affinity-profile', str(affinity_file), '--cpu-pinning',
            '--timeout', str(timeout), '--sample-seconds', '1', '--all-local-tracking',
            '--client-numa-node', str(cpus['client_numa_node']),
            '--rdma-interface', slot['rdma_interface'], '--results-root', str(results_root),
            '--performance-mode']
    if cpus['client_cpus']:
        argv += ['--client-cpus', ','.join(map(str, cpus['client_cpus']))]
    for key, value in high['configuration'].items():
        if key == 'vm_memory_mib':
            continue
        if int(value) != value:
            raise ValueError('Nonintegral parameter: ' + key)
        argv += ['--' + key.replace('_', '-'), str(int(value))]
    return argv
