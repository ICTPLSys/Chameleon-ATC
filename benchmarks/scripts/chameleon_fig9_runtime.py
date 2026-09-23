"""Resource planning and QEMU provisioning for the three-VM Figure 9 runner."""
import contextlib
import copy
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import time
import threading

import chameleon_affinity as affinity

ROOT = Path(__file__).resolve().parents[2]
HA = ROOT / 'hyperalloc-6.18'


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


guest = module('fig9_guest', HA / 'scripts/guestctl.py')
deploy = module('fig9_deploy', HA / 'scripts/run-deploy-vm.py')


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def path(value):
    p = Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (ROOT / p).resolve()


def requested_disk_bytes(slot):
    """An optional minimum virtual disk capacity, in whole GiB."""
    if 'disk_size_gib' not in slot:
        return None
    size = slot['disk_size_gib']
    if type(size) is not int or size <= 0:
        raise ValueError('disk_size_gib must be a positive integer (GiB)')
    return size * 1024 ** 3


def validate_inventory(config, hardware=False):
    slots = config['slots']
    if len(slots) != 3:
        raise ValueError('Figure 9 requires exactly three VM slots')
    ipaddress.ip_address(config['server']['address'])
    server=config['server']
    if server.get('manage_local') and server.get('manage_remote'):
        raise ValueError('Select only one managed RDMA server location')
    if server.get('manage_remote'):
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*',server.get('ssh_host','')):
            raise ValueError('Remote RDMA server requires a valid ssh_host')
        if not Path(server.get('binary','')).is_absolute():
            raise ValueError('Remote RDMA binary must be an absolute path on the SSH host')
        if server.get('command_prefix'):
            raise ValueError('Remote supervisor must run the server as its SSH user; command_prefix is local-only')
    for field in ('name', 'ssh_port', 'server_port', 'rdma_address'):
        values = [s[field] for s in slots]
        if len(set(values)) != 3:
            raise ValueError('Each VM needs a distinct ' + field)
    addresses = [str(ipaddress.ip_interface(s['rdma_address']).ip) for s in slots]
    if len(set(addresses)) != 3 or config['server']['address'] in addresses:
        raise ValueError('Guest RDMA IPs must differ from each other and the server')
    devices = []
    groups = set()
    if 'single_slot' in config:
        requested_disk_bytes(config['single_slot'])
    for slot in slots:
        requested_disk_bytes(slot)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}', slot['name']):
            raise ValueError('Invalid VM slot name')
        if slot['name'] == config['template_vm']:
            raise ValueError('A slot cannot overwrite the template VM')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', slot['rdma_interface']):
            raise ValueError('Invalid Guest RDMA interface')
        for key in ('ssh_port', 'server_port'):
            if type(slot[key]) is not int or not 1 <= slot[key] <= 65535:
                raise ValueError('Invalid ' + key)
        for bdf in slot['vfio']:
            if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', bdf):
                raise ValueError('Invalid VF PCI address')
            devices.append(bdf.lower())
        if hardware:
            if not slot['vfio']:
                raise ValueError(slot['name'] + ': fill vfio with a provisioned independent RDMA VF')
            own_groups = set()
            for bdf in slot['vfio']:
                dev = Path('/sys/bus/pci/devices') / bdf
                if not (dev / 'physfn').exists() or not (dev / 'iommu_group').exists():
                    raise ValueError(bdf + ': requires an SR-IOV VF with an IOMMU group')
                group = (dev / 'iommu_group').resolve()
                if not {p.name for p in (group / 'devices').iterdir()} <= set(slot['vfio']):
                    raise ValueError('Pass every device in IOMMU group ' + group.name)
                own_groups.add(str(group))
            if groups & own_groups:
                raise ValueError('VMs must not share an IOMMU group')
            groups |= own_groups
    if len(devices) != len(set(devices)):
        raise ValueError('VMs must not share a VF')
    return config


def allocate_cpus(applications, client_node, topology=None, allowed=None):
    """Allocate disjoint physical cores; reserve client cores on its NUMA node."""
    topology = topology if topology is not None else affinity.topology()
    allowed = set(allowed if allowed is not None else os.sched_getaffinity(0))
    physical = {}
    for row in sorted(topology, key=lambda r: r['cpu']):
        if row['cpu'] in allowed:
            physical.setdefault((row['socket'], row['core']), row)
    pools = {}
    for row in physical.values():
        pools.setdefault(row['node'], []).append(row['cpu'])
    services = [x for x in applications if x['application'] in ('memcached', 'cassandra')]
    if len(services) != 1 or len({a['application'] for a in applications}) != len(applications):
        raise ValueError('A mix needs distinct applications and exactly one Host-driven service workload')
    service = services[0]
    # Current Memcached high point uses 8 TX + 2 RX + 2 producer CPUs.
    args = service['workload_configuration']['args']
    def arg(name, default):
        return int(args[args.index(name) + 1]) if name in args else default
    client_count = (arg('--workers', 8) + arg('--rx-threads', 2) + arg('--producer-shards', 2)
                    if service['application'] == 'memcached' else arg('--threads', 16))
    if len(pools.get(client_node, [])) < client_count:
        raise ValueError('Insufficient distinct physical cores for Host clients')
    # Allocate client cores from the end so VM placement remains deterministic.
    client = pools[client_node][-client_count:]
    pools[client_node] = pools[client_node][:-client_count]
    assignments = {}
    # Place service first on a different node, then use best-fit for other VMs.
    ordered = [service] + sorted((a for a in applications if a is not service),
                                key=lambda a: -a['workload_configuration']['vcpus'])
    for app in ordered:
        n = app['workload_configuration']['vcpus']
        nodes = [node for node in pools if len(pools[node]) >= n + 2
                 and (app is not service or node != client_node)]
        if not nodes:
            raise ValueError('Insufficient disjoint physical cores for ' + app['application'])
        node = min(nodes, key=lambda x: (len(pools[x]) - (n + 2), x))
        selected, pools[node] = pools[node][:n + 2], pools[node][n + 2:]
        assignments[app['application']] = {
            'protocol': affinity.PROTOCOL, 'host_numa_node': node,
            'vcpu_host_cpus': selected[:n], 'qemu_service_cpus': selected[n:],
            'guest_application_cpus': list(range(n))}
    result = {'applications': assignments, 'client_numa_node': client_node, 'client_cpus': client}
    result['physical_isolation'] = validate_cpu_plan(result, topology)
    return result


def validate_cpu_plan(placement, topology=None):
    """Check the entire co-run, including every VM's service cores and clients."""
    topology = affinity.topology() if topology is None else topology
    rows = {row['cpu']: row for row in topology}
    groups = {'host_client': placement['client_cpus']}
    for case, profile in placement['applications'].items():
        for role in ('vcpu_host_cpus', 'qemu_service_cpus'):
            cpus = profile[role]
            if any(cpu not in rows or rows[cpu]['node'] != profile['host_numa_node'] for cpu in cpus):
                raise ValueError('VM CPU group is outside its NUMA node: ' + case)
            groups[case + '/' + role] = cpus
        if case in ('memcached', 'cassandra') and profile['host_numa_node'] == placement['client_numa_node']:
            raise ValueError('Host-driven Guest and its generator need different NUMA nodes')
    if any(cpu not in rows or rows[cpu]['node'] != placement['client_numa_node'] for cpu in placement['client_cpus']):
        raise ValueError('Host client CPU group is outside its NUMA node')
    return affinity.validate_isolation(groups, topology)


def pin_guest_cpus(access, profile):
    """Pin before leaf startup so leaf cleanup restores these exact masks."""
    pid = int((guest.run_dir(access) / 'qemu.pid').read_text())
    _, evidence = affinity.apply(pid, guest.qmp(access, 'query-cpus-fast'), profile)
    return {'pid': pid, **evidence}


def verify_vm_cpu_isolation(placement, guests, topology=None):
    """Verify all QEMU vCPU and service threads against disjoint physical cores."""
    physical = validate_cpu_plan(placement, topology)
    if set(guests) != set(placement['applications']):
        raise ValueError('Every planned VM must participate in runtime affinity verification')
    evidence = {}
    for case, access in guests.items():
        pid = int((guest.run_dir(access) / 'qemu.pid').read_text())
        evidence[case] = {'pid': pid, **affinity.verify(pid, guest.qmp(access, 'query-cpus-fast'), placement['applications'][case])}
    return {'status': 'PASS', 'vms': evidence, 'physical_isolation': physical}


def access(name):
    return guest.load_access(HA / 'build/guests' / name / 'access.json')


def slot_config(template, slot, high, cpu):
    value = copy.deepcopy(template)
    directory = HA / 'build/guests' / slot['name']
    value.update(name=slot['name'], disk=str(directory / 'disk.qcow2'), disk_format='qcow2',
                 seed=None, ssh_port=slot['ssh_port'], network='user', vfio=slot['vfio'],
                 memory_mib=high['vm_memory_mib'], cpus=high['workload_configuration']['vcpus'],
                 host_numa_node=cpu['host_numa_node'],
                 host_cpus=cpu['vcpu_host_cpus'] + cpu['qemu_service_cpus'],
                 pebs=True, chameleon=True, policy=True)
    # Retain template MAC: installed netplan matches it. Each user-mode NIC
    # has its own isolated NAT network; RDMA NICs/IPs are independent.
    return value


def grow_slot_overlay(slot, source_disk):
    """Grow only this stopped slot's overlay; never resize its backing image."""
    requested = requested_disk_bytes(slot)
    if requested is None:
        return
    a = access(slot['name'])
    directory = HA / 'build/guests' / slot['name']
    disk = directory / 'disk.qcow2'
    source_disk = Path(source_disk).resolve()
    with guest.control_lock(a):
        if guest.active(a) or not guest.launcher_idle(a):
            raise ValueError('Stop the VM before growing its overlay: ' + slot['name'])
        if disk.is_symlink() or disk.resolve() == source_disk:
            raise ValueError('Disk growth requires an independent owned overlay')
        def info():
            result = subprocess.run(['qemu-img', 'info', '--output=json', str(disk)],
                                    capture_output=True, text=True, check=True)
            value = json.loads(result.stdout)
            backing = value.get('full-backing-filename', value.get('backing-filename'))
            if backing is not None and not Path(backing).is_absolute():
                backing = disk.parent / backing
            if value.get('format') != 'qcow2' or backing is None or Path(backing).resolve() != source_disk:
                raise ValueError('Disk growth requires an overlay of the selected template')
            return value
        before = info()
        grown = before['virtual-size'] < requested
        if grown:
            subprocess.run(['qemu-img', 'resize', str(disk), str(requested)], check=True)
        after = info() if grown else before
        if after['virtual-size'] < max(requested, before['virtual-size']):
            raise RuntimeError('Overlay capacity did not reach the requested size')
        save(directory / 'disk-growth.json', {
            'status': 'PASS', 'vm': slot['name'], 'disk': str(disk),
            'backing_disk': str(source_disk), 'requested_bytes': requested,
            'before_bytes': before['virtual-size'], 'after_bytes': after['virtual-size'],
            'action': 'grown' if grown else 'unchanged', 'unix_seconds': time.time()})


def prepare_slots(inventory, template_access, initial_configs):
    """Create qcow2 overlays over an offline installed template, once."""
    source = json.loads(Path(template_access['vm_config']).read_text())
    source_disk = path(source['disk'])
    if guest.active(template_access) or not guest.launcher_idle(template_access):
        raise ValueError('Stop the template VM before preparing/running its overlays')
    for slot, config in zip(inventory['slots'], initial_configs):
        requested_disk_bytes(slot)
        directory = HA / 'build/guests' / slot['name']
        marker = directory / 'fig9-template.json'
        if directory.exists():
            if not marker.is_file() or json.loads(marker.read_text())['disk'] != str(source_disk):
                raise ValueError('Existing slot is not a Figure 9 overlay of this template: ' + slot['name'])
            existing = access(slot['name'])
            if existing['name'] != slot['name'] or existing['port'] != slot['ssh_port']:
                raise ValueError('Existing slot SSH identity/port differs from inventory: ' + slot['name'])
            grow_slot_overlay(slot, source_disk)
            continue
        directory.mkdir(mode=0o700, parents=True)
        try:
            subprocess.run(['qemu-img', 'create', '-f', 'qcow2', '-F', source['disk_format'],
                            '-b', str(source_disk), str(directory / 'disk.qcow2')], check=True)
            shutil.copyfile(template_access['identity_file'], directory / 'id_ed25519')
            (directory / 'id_ed25519').chmod(0o600)
            (directory / 'known_hosts').touch(mode=0o600)
            save(directory / 'vm.json', config)
            save(directory / 'bootstrap.json', config)
            save(directory / 'access.json', {'name': slot['name'], 'host': '127.0.0.1',
                 'port': slot['ssh_port'], 'user': template_access['user'],
                 'identity_file': str(directory / 'id_ed25519'),
                 'known_hosts': str(directory / 'known_hosts'),
                 'vm_config': str(directory / 'vm.json'), 'bootstrap_config': str(directory / 'bootstrap.json')})
            save(marker, {'template_vm': template_access['name'], 'disk': str(source_disk),
                          'scope': 'Template must remain offline and unchanged while overlays exist'})
            grow_slot_overlay(slot, source_disk)
        except BaseException:
            shutil.rmtree(directory)
            raise


class GuestCommandError(subprocess.CalledProcessError):
    """Keep the command's useful diagnosis in the persisted exception too."""
    def __str__(self):
        details = '\n'.join(text[-6000:] for text in (self.stdout, self.stderr) if text)
        return super().__str__() + ('\n' + details if details else '')


def remote(a, argv, timeout=300):
    result = subprocess.run(guest.ssh_command(a, argv), capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise GuestCommandError(result.returncode, result.args,
                                output=result.stdout, stderr=result.stderr)
    return result.stdout


GROW_GUEST_ROOT = r'''
import json,os,pathlib,subprocess,sys,time
requested=int(sys.argv[1])
record={'status':'RUNNING','requested_bytes':requested,'unix_seconds':time.time()}
def command(argv):
    return subprocess.check_output(argv,text=True).strip()
def snapshot():
    fs=os.statvfs('/')
    return {'disk_bytes':int(command(['blockdev','--getsize64','/dev/vda'])),
            'partition_bytes':int(command(['blockdev','--getsize64','/dev/vda1'])),
            'partition_start_bytes':int(pathlib.Path('/sys/class/block/vda1/start').read_text())*512,
            'filesystem_bytes':fs.f_blocks*fs.f_frsize,'available_bytes':fs.f_bavail*fs.f_frsize}
try:
    root=json.loads(command(['findmnt','--json','--output','SOURCE,FSTYPE','--target','/']))['filesystems'][0]
    if os.path.realpath(root['source'])!='/dev/vda1' or root['fstype']!='ext4':
        raise RuntimeError('Explicit disk growth supports only the installed /dev/vda1 ext4 root')
    record['before']=snapshot()
    if record['before']['disk_bytes']<requested:
        raise RuntimeError('Guest disk is smaller than disk_size_gib; grow the stopped overlay first')
    grow=subprocess.run(['growpart','/dev/vda','1'],capture_output=True,text=True)
    record['growpart']={'returncode':grow.returncode,'stdout':grow.stdout,'stderr':grow.stderr}
    if grow.returncode and not (grow.returncode==1 and grow.stdout.lstrip().startswith('NOCHANGE:')):
        raise RuntimeError('growpart failed')
    resize=subprocess.run(['resize2fs','/dev/vda1'],capture_output=True,text=True)
    record['resize2fs']={'returncode':resize.returncode,'stdout':resize.stdout,'stderr':resize.stderr}
    if resize.returncode:
        raise RuntimeError('resize2fs failed')
    record['after']=snapshot()
    after=record['after']
    if after['disk_bytes']-after['partition_start_bytes']-after['partition_bytes']>1024**2:
        raise RuntimeError('Root partition did not grow to the end of the disk')
    if after['filesystem_bytes']<record['before']['filesystem_bytes']:
        raise RuntimeError('Root filesystem unexpectedly became smaller')
    record['status']='PASS'
except Exception as error:
    record.update(status='FAIL',error=str(error))
print(json.dumps(record))
sys.exit(0 if record['status']=='PASS' else 1)
'''


def grow_guest_root(a, slot):
    """Cloud-init is disabled in overlays without a seed; grow explicitly."""
    requested = requested_disk_bytes(slot)
    if requested is None:
        return None
    result = subprocess.run(guest.ssh_command(a, ['sudo', '-n', 'python3', '-c',
        GROW_GUEST_ROOT, str(requested)]), capture_output=True, text=True, timeout=300)
    evidence = {'vm': a['name'], 'returncode': result.returncode,
                'stdout': result.stdout, 'stderr': result.stderr}
    try:
        evidence['growth'] = json.loads(result.stdout)
    except ValueError:
        pass
    save(guest.run_dir(a) / 'disk-growth.json', evidence)
    if result.returncode:
        raise GuestCommandError(result.returncode, result.args,
                                output=result.stdout, stderr=result.stderr)
    if evidence.get('growth', {}).get('status') != 'PASS':
        raise RuntimeError('Guest disk growth did not return a valid PASS record')
    return evidence['growth']


def setup_guest(a, slot, server):
    guest.wait_ssh(a, 300)
    disk_growth = grow_guest_root(a, slot)
    remote(a, ['sudo', '-n', 'sh', '-c',
               'echo "batch 32 6400 128 4 10" > /sys/kernel/debug/chameleon_mm/control; '
               'for o in 0 2 3 4 5 6 7 8 9; do '
               'echo "cost $o $((1 << o))" > /sys/kernel/debug/chameleon_mm/control; done'])
    scripts = '/home/' + a['user'] + '/chameleon-benchmarks/scripts/'
    for service in ('cassandra', 'memcached'):
        remote(a, [scripts + service + '-guest.sh', 'stop'])
    helper = '/home/' + a['user'] + '/chameleon-tools/hermit-guest.py'
    # Installed overlays can contain an older connector. Deploy the version
    # used by this runner before attempting any new remote-pool connection.
    guest.transfer(a, 'upload', HA / 'scripts/hermit-guest.py', helper)
    remote(a, ['sudo', '-n', 'sh', '-c',
               'if test -d /sys/module/rswap_client; then modprobe -r rswap_client; fi'])
    for name in ('mlx5_core', 'mlx5_ib', 'ib_ipoib', 'rdma_cm'):
        remote(a, ['sudo', '-n', 'modprobe', name])
    interface = slot['rdma_interface']
    remote(a, ['sudo', '-n', 'ip', 'link', 'set', interface, 'up'])
    # The clone initially inherits the template RDMA IP. Replace only global
    # addresses on this explicitly selected experimental interface.
    remote(a, ['sudo', '-n', 'ip', 'address', 'flush', 'dev', interface, 'scope', 'global'])
    remote(a, ['sudo', '-n', 'ip', 'address', 'add', slot['rdma_address'], 'dev', interface])
    for file in (ROOT / 'benchmarks/scripts').glob('*.sh'):
        guest.transfer(a, 'upload', file, scripts + file.name)
        remote(a, ['chmod', '+x', scripts + file.name])
    guest.transfer(a, 'upload', ROOT / 'benchmarks/patches/graph500-csr-cache.patch', scripts + 'graph500-csr-cache.patch')
    guest.transfer(a, 'upload', ROOT / 'benchmarks/scripts/memcached-warmup.py', scripts + 'memcached-warmup.py')
    result = subprocess.run(guest.ssh_command(a, ['sudo', '-n', 'python3', helper,
        'start', '--server', server['address'], '--port', str(slot['server_port']),
        '--pool-mib', '8192', '--interface', interface]),
        capture_output=True, text=True, timeout=300)
    # Save both successful retries and failures independently of caller output.
    diagnosis = {'vm': a['name'], 'server': server['address'], 'port': slot['server_port'],
                 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    save(guest.run_dir(a) / 'hermit-connection.json', diagnosis)
    if result.returncode:
        raise GuestCommandError(result.returncode, result.args,
                                output=result.stdout, stderr=result.stderr)
    connection = json.loads(result.stdout)
    if connection.get('status') != 'PASS' or not connection.get('connection_tested'):
        raise RuntimeError('Hermit did not verify a ready pool: ' + str(connection))
    if disk_growth is not None:
        connection['disk_growth'] = disk_growth
    return connection


def free_tcp_port(port):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))


def stop_guest(a, timeout=180):
    """Disconnect the owned Guest's pool before tearing down its VF/QEMU."""
    if not guest.active(a):
        return guest.stop(a, timeout=timeout)
    error = None
    try:
        helper = '/home/' + a['user'] + '/chameleon-tools/hermit-guest.py'
        loaded = remote(a, ['sh', '-c', 'test ! -d /sys/module/rswap_client || echo loaded']).strip()
        if loaded:
            remote(a, ['sudo', '-n', 'python3', helper, 'stop'])
    except (OSError, subprocess.SubprocessError, ValueError) as failure:
        error = failure
    # Even on failed initialization/SSH, stop only this invocation's VM.
    guest.stop(a, timeout=timeout)
    if error:
        raise RuntimeError('Guest stopped, but Hermit disconnect was not confirmed: ' + str(error)) from error


def stop_process(child, timeout=20):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)


REMOTE_CHECK = r'''
import json,os,pathlib,resource,subprocess,sys
binary,address,required_mib=sys.argv[1:]
if not os.path.isfile(binary) or not os.access(binary,os.X_OK):
    raise RuntimeError('Remote RDMA binary is not executable: '+binary)
devices=json.loads(subprocess.check_output(['ip','-j','address','show'],text=True))
matches=[d['ifname'] for d in devices if any(i.get('local')==address for i in d.get('addr_info',[]))]
if not matches: raise RuntimeError('Remote RDMA IP is not configured: '+address)
available=int(next(x.split()[1] for x in pathlib.Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))//1024
if available<int(required_mib): raise RuntimeError('Remote memory insufficient: available=%s required=%s MiB'%(available,required_mib))
memlock=resource.getrlimit(resource.RLIMIT_MEMLOCK)[0]
if memlock!=resource.RLIM_INFINITY and memlock<8192*1024*1024:
    raise RuntimeError('Remote memlock must cover one 8192MiB registered pool')
print(json.dumps({'status':'PASS','hostname':os.uname().nodename,'binary':binary,'address':address,
                  'interfaces':matches,'available_mib':available,'required_mib':int(required_mib),
                  'memlock_soft_bytes':memlock}))
'''


# The SSH channel carries heartbeats. Its disappearance (including a killed
# local orchestrator) revokes the lease; only this supervisor's child is killed.
# No PID files or process-name matching can accidentally select the old pool.
REMOTE_SUPERVISOR = r'''
import json,os,select,signal,subprocess,sys,time
command=json.loads(sys.argv[1]);lease=float(sys.argv[2]);stopping=False
def stop(signum,frame):
    global stopping
    stopping=True
for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,stop)
child=None;reason='supervisor_error';status=1
try:
    child=subprocess.Popen(command,stdin=subprocess.DEVNULL,start_new_session=True)
    print('STARTED Fig9 remote '+json.dumps({'pid':child.pid,'command':command}),flush=True)
    deadline=time.monotonic()+lease
    while not stopping:
        code=child.poll()
        if code is not None:
            reason='server_exit';status=code or 1;break
        if time.monotonic()>=deadline:
            reason='heartbeat_expired';break
        readable,_,_=select.select([sys.stdin.fileno()],[],[],min(.2,max(0,deadline-time.monotonic())))
        if readable:
            message=os.read(sys.stdin.fileno(),4096)
            if not message: reason='ssh_stdin_closed';status=0;break
            if b'STOP\n' in message: reason='requested_stop';status=0;break
            if b'PING\n' in message: deadline=time.monotonic()+lease
    else:
        reason='signal';status=0
finally:
    if child is not None:
        if child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)
        print('STOPPED Fig9 remote '+json.dumps({'pid':child.pid,'returncode':child.returncode,'reason':reason}),flush=True)
sys.exit(status)
'''


def server_ssh_command(config, program, arguments):
    command=['python3','-u','-c',program,*map(str,arguments)]
    return ['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=10',
            '-o','ServerAliveInterval=10','-o','ServerAliveCountMax=3',
            config['ssh_host'],shlex.join(command)]


def check_remote_server(inventory):
    config=inventory['server']
    if not config.get('manage_remote'):
        return None
    result=subprocess.run(server_ssh_command(config,REMOTE_CHECK,
        [config['binary'],config['address'],len(inventory['slots'])*8192+1024]),
        check=True,capture_output=True,text=True,timeout=30)
    return json.loads(result.stdout)


def assert_servers(children):
    failed=[{'pid':p.pid,'returncode':p.poll()} for p in children if p.poll() is not None]
    if failed:
        raise RuntimeError('Owned RDMA server/session exited: '+str(failed))


def heartbeat(child, done):
    while not done.is_set() and child.poll() is None:
        try:
            os.write(child.stdin.fileno(),b'PING\n')
        except (OSError,ValueError):
            return
        done.wait(2)


def stop_remote_server(child, done, thread, logfile):
    done.set();thread.join(timeout=3)
    was_running=child.poll() is None
    try:
        if was_running: os.write(child.stdin.fileno(),b'STOP\n')
    except (OSError,ValueError):
        pass
    if child.stdin and not child.stdin.closed: child.stdin.close()
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        stop_process(child)
    if was_running and 'STOPPED Fig9 remote' not in logfile.read_text():
        raise RuntimeError('Remote cleanup acknowledgement missing; owned server lease expires within 30s: '+str(logfile))


@contextlib.contextmanager
def servers(inventory, output):
    children, streams = [], []
    remote_sessions=[]
    config = inventory['server']
    try:
        if config.get('manage_local') or config.get('manage_remote'):
            remote_mode=bool(config.get('manage_remote'))
            binary=config['binary'] if remote_mode else path(config['binary'])
            if not remote_mode and not binary.is_file():
                raise ValueError('Build rswap-server first: ' + str(binary))
            for slot in inventory['slots']:
                logfile = output / (slot['name'] + '-rdma-server.log')
                stream = logfile.open('w'); streams.append(stream)
                command=[*config.get('command_prefix', []),str(binary),config['address'],str(slot['server_port']),'8192']
                if remote_mode:
                    child=subprocess.Popen(server_ssh_command(config,REMOTE_SUPERVISOR,[json.dumps(command),30]),
                                           stdin=subprocess.PIPE,stdout=stream,stderr=subprocess.STDOUT)
                    children.append(child)
                    os.set_blocking(child.stdin.fileno(),False)
                    done=threading.Event()
                    thread=threading.Thread(target=heartbeat,args=(child,done),daemon=True)
                    remote_sessions.append((child,done,thread,logfile))
                    thread.start()
                else:
                    child = subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT)
                    children.append(child)
                deadline = time.monotonic() + 90
                while 'READY Hermit RDMA' not in logfile.read_text():
                    if child.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('RDMA server failed; see ' + str(logfile))
                    time.sleep(.2)
        assert_servers(children)
        yield children
    finally:
        errors=[]
        remote_children={p for p,_,_,_ in remote_sessions}
        for session in remote_sessions:
            try: stop_remote_server(*session)
            except Exception as error: errors.append(str(error))
        for child in children:
            if child not in remote_children: stop_process(child)
        for stream in streams:
            stream.close()
        if errors: raise RuntimeError('; '.join(errors))
