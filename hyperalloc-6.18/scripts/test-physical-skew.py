#!/usr/bin/env python3
"""Run the 16 GiB skew experiment on an existing disk Guest and physical RDMA.

The connected Hermit backend and VFIO devices must be prepared beforehand.
This runner never starts, stops, reboots, or destroys the VM, and never changes
network interfaces, PCI bindings, or modules. Only its own workload is started.
"""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


live = module('physical_live', 'test-running-guest.py')
skew = module('physical_skew_common', 'test-chameleon-skew.py')
guest, vm = live.guest, live.control.vm
require, fields, command = skew.require, skew.fields, skew.command
MIB, GIB = 1 << 20, 1 << 30

# This is a read-only inventory. The selected route must use a netdev belonging
# to an actual mlx5 PCI device; an RXE link is not accepted as physical RDMA.
RDMA_INVENTORY = r"""
import json, pathlib, subprocess
P = pathlib.Path
result = {'boot_id': P('/proc/sys/kernel/random/boot_id').read_text().strip(),
          'kernel_version': P('/proc/version').read_text().strip(),
          'transport_stats': P('/sys/kernel/debug/hermit_rdma/stats').read_text(),
          'parameters': {}, 'devices': {}, 'commands': {}}
for key in ('backend', 'pool_mb', 'sip', 'sport'):
    path = P('/sys/module/rswap_client/parameters') / key
    result['parameters'][key] = path.read_text().strip() if path.exists() else None
for path in P('/sys/class/infiniband').glob('*'):
    device = (path / 'device').resolve()
    driver = device / 'driver'
    item = {'device_path': str(device), 'pci_bdf': device.name,
            'driver': driver.resolve().name if driver.exists() else None,
            'netdevs': sorted(p.name for p in (device / 'net').glob('*')),
            'ports': {}}
    for port in (path / 'ports').glob('*'):
        info = {'counters': {}}
        for key in ('state', 'phys_state', 'link_layer', 'rate', 'lid'):
            value = port / key
            if value.exists(): info[key] = value.read_text().strip()
        for directory in ('counters', 'hw_counters'):
            for value in (port / directory).glob('*'):
                try:
                    text = value.read_text().strip()
                    if text.isdigit(): info['counters'][directory + '/' + value.name] = int(text)
                except OSError: pass
        item['ports'][port.name] = info
    result['devices'][path.name] = item
commands = {'links': ['ip', '-j', 'link', 'show'], 'addresses': ['ip', '-j', 'address', 'show'],
            'rdma_links': ['rdma', '-j', 'link', 'show'],
            'rdma_cm': ['rdma', '-j', 'resource', 'show', 'cm_id']}
if result['parameters']['sip']:
    commands['server_route'] = ['ip', '-j', 'route', 'get', result['parameters']['sip']]
for name, argv in commands.items():
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        result['commands'][name] = {'rc': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
    except (OSError, subprocess.TimeoutExpired) as e:
        result['commands'][name] = {'error': str(e)}
print(json.dumps(result))
"""


def inventory(access):
    return json.loads(live.remote_python(access, RDMA_INVENTORY))


def host_identity(access):
    run = guest.run_dir(access)
    pid = int((run / 'qemu.pid').read_text().strip())
    proc = Path('/proc') / str(pid)
    argv = (proc / 'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    require('-name' in argv and argv[argv.index('-name') + 1] == access['name'],
            'QEMU pidfile must identify the requested named VM')
    require(any(str(run / 'qmp.sock') in value for value in argv), 'QEMU PID/QMP paths disagree')
    require('qemu-system' in (proc / 'exe').resolve().name, 'pidfile does not identify QEMU')
    start_time = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    vfio = []
    for i, value in enumerate(argv[:-1]):
        if value != '-device': continue
        device = argv[i + 1]
        if device.startswith('{'):
            obj = json.loads(device)
            if obj.get('driver') == 'vfio-pci': vfio.append(obj['host'])
        elif device.startswith('vfio-pci,'):
            match = re.search(r'(?:^|,)host=([^,]+)', device)
            if match: vfio.append(match[1])
    require(vfio, 'physical test requires an actual VFIO PCI device in the live QEMU command')
    devices = {}
    for bdf in vfio:
        path = Path('/sys/bus/pci/devices') / bdf
        devices[bdf] = {'driver': (path / 'driver').resolve().name,
                        'iommu_group': (path / 'iommu_group').resolve().name,
                        'vendor': (path / 'vendor').read_text().strip(),
                        'device': (path / 'device').read_text().strip()}
        require(devices[bdf]['driver'] == 'vfio-pci', 'assigned physical PCI device must remain VFIO-bound')
    modules = {}
    for name in ('kvm', 'kvm_intel'):
        root = Path('/sys/module') / name
        item = {'loaded': root.exists()}
        for key in ('srcversion', 'version', 'taint'):
            path = root / key
            if path.exists(): item[key] = path.read_text().strip()
        note = root / 'notes/.note.gnu.build-id'
        try: item['loaded_build_id_note_hex'] = note.read_bytes().hex()
        except OSError as error: item['loaded_build_id_note_error'] = str(error)
        modules[name] = item
    return {'pid': pid, 'start_time': start_time, 'argv': argv, 'vfio': devices,
            'loaded_kvm_identity': modules,
            'rss': skew.rss(pid), 'qemu_executable': str((proc / 'exe').resolve())}


def select_physical_rdma(state, requested=None, netdev=None):
    candidates = {name: value for name, value in state['devices'].items()
                  if value['driver'] == 'mlx5_core' and not name.startswith('rxe') and
                  any(port.get('state', '').startswith('4:') for port in value['ports'].values())}
    require(requested in candidates if requested else len(candidates) == 1,
            'select one active physical mlx5 RDMA device with --rdma-device')
    name = requested or next(iter(candidates))
    device = candidates[name]
    route = state['commands'].get('server_route', {})
    require(route.get('rc') == 0, 'Hermit server IP must have a resolved IP route')
    routes = json.loads(route['stdout'])
    require(len(routes) == 1 and routes[0].get('dev') in device['netdevs'],
            'Hermit server route must use a netdev of the selected physical mlx5 device')
    require(not netdev or routes[0]['dev'] == netdev, 'server route differs from requested --netdev')
    cm = physical_hermit_cm(state, name)
    return {'name': name, 'pci_bdf': device['pci_bdf'], 'driver': device['driver'],
            'netdev': routes[0]['dev'], 'route': routes[0],
            'route_identity': {k:routes[0].get(k) for k in ('dst','dev','gateway','prefsrc','src','table')},
            'parameters': state['parameters'], 'ports': device['ports'], 'hermit_cm': cm}


def physical_hermit_cm(state, device):
    """Match the actual kernel client's CM ID, not just the current IP route."""
    command = state['commands'].get('rdma_cm', {})
    require(command.get('rc') == 0, 'RDMA CM inventory unavailable; route alone cannot identify the Hermit connection')
    entries = json.loads(command['stdout'])
    require(isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries),
            'unsupported RDMA CM JSON schema; preserve raw inventory for inspection')
    clients = [entry for entry in entries if entry.get('comm') == 'rswap_client']
    require(len(clients) == 1, 'expected the single rswap_client kernel CM ID in the RDMA inventory')
    cm = clients[0]
    required = {'ifname', 'port', 'cm-idn', 'lqpn', 'qp-type', 'state', 'ps', 'src-addr', 'dst-addr'}
    require(required <= cm.keys(), 'RDMA CM fields insufficient to prove client device/endpoint identity')
    host, separator, port = cm['dst-addr'].rpartition(':')
    require(separator and port.isdigit(), 'RDMA CM destination must expose its IP and port')
    require(ipaddress.ip_address(host.strip('[]')) == ipaddress.ip_address(state['parameters']['sip']) and
            int(port) == int(state['parameters']['sport']), 'kernel Hermit CM destination differs from its configured server')
    require(cm['ifname'] == device and str(cm['port']) in state['devices'][device]['ports'] and
            state['devices'][device]['ports'][str(cm['port'])]['state'].startswith('4:'),
            'kernel Hermit CM must use the selected active physical mlx5 port')
    require(cm['state'] == 'CONNECT' and cm['qp-type'] == 'RC' and cm['ps'] == 'TCP' and cm['lqpn'] > 0,
            'kernel Hermit CM must retain its RC/TCP connection and local QP')
    require(not fields(state['transport_stats'])['broken'], 'kernel Hermit RDMA connection must remain healthy')
    # Linux CMA's CONNECT state spans connection establishment and use; it is
    # not independently an ESTABLISHED indication. Backend registration below
    # follows wait_cm(CONNECTED) and successful remote-region exchange, and the
    # before/after completion counters additionally prove actual RDMA I/O.
    return {'entry': cm, 'evidence': 'kernel rswap_client CM device/port/endpoint identity; '
            'CONNECT plus registered backend and healthy transport, completed RDMA I/O checked separately'}


def observe_capacity(report, qstate, floor):
    """Record actual QMP capacity, including the sample that violates the floor."""
    observed = report.setdefault('capacity_observations', {
        'floor_bytes': floor, 'minimum_local_bytes': qstate['actual'],
        'minimum_leased_local_bytes': None, 'query_samples': 0,
        'lease_active_samples': 0, 'floor_violations': 0})
    observed['query_samples'] += 1
    observed['minimum_local_bytes'] = min(observed['minimum_local_bytes'], qstate['actual'])
    if qstate['chameleon']['policy']['lease']:
        observed['lease_active_samples'] += 1
        previous = observed['minimum_leased_local_bytes']
        observed['minimum_leased_local_bytes'] = (qstate['actual'] if previous is None
                                                 else min(previous, qstate['actual']))
        if qstate['actual'] < floor:
            observed['floor_violations'] += 1
            return False
    return True


def preflight(before, qstate, rdma, args):
    stats = before['stats']
    for name in ('chameleon', 'chameleon_mm', 'chameleon_shadow', 'chameleon_policy', 'hermit'):
        require(name in stats, 'missing Guest debugfs ' + name)
    t, m, s, p, h = (fields(stats[n]) for n in
                    ('chameleon', 'chameleon_mm', 'chameleon_shadow', 'chameleon_policy', 'hermit'))
    require(not p['enabled'] and not p['lease_active'] and not p['target_pid'],
            'existing policy must be disabled without a target or lease')
    require(not m['enabled'] and not m['target_pid'] and not before['manager_candidates'].strip(),
            'existing manager must be disabled without a target or held candidates')
    require(m['cost_valid'] == 1 and m['batch'] > 0 and m['Etrans'] > 0 and m['Lptw_explicit'] > 0,
            'configure valid manager batch/cost parameters before testing; the current ABI cannot restore an unconfigured cost model')
    require(not s['live_objects'] and not s['reservation_pages'] and not s['owned_pages'],
            'existing Shadow ownership must be empty')
    if 'chameleon_psi_load' in stats:
        require(not fields(stats['chameleon_psi_load'])['workers'], 'existing PSI fixture must be stopped')
    require(h['backend'] == 'rdma' and h['registered'] == 1 and
            h['capacity_pages'] * 4096 == args.pool_mib * MIB and
            not h['live_slots'] and not h['allocated_pages'] and not h['inflight'],
            'Hermit must already have an idle connected physical RDMA pool of the requested size')
    require(rdma['parameters']['backend'] == 'rdma' and
            int(rdma['parameters']['pool_mb']) == args.pool_mib,
            'loaded Hermit module parameters must describe the requested RDMA backend')
    transport = fields(rdma['transport_stats'])
    require(not transport['broken'] and transport['max_transfer_bytes'] >= 4096,
            'physical RDMA transport must already be connected and usable')
    ch = qstate['chameleon']
    require(ch['vfio-coordinated'], 'physical test requires Chameleon VFIO coordination')
    require(not any(ch[k] for k in ('range-records', 'registered-pages', 'retired-pages', 'blocked-pages', 'error-pages'))
            and not ch['policy']['lease'], 'Host Chameleon must have no previous live ranges or lease')
    require(qstate['actual'] == ch['policy']['total-bytes'] == args.guest_mib * MIB,
            'live Guest must have the requested RAM with all legacy balloon capacity returned')
    require(t.get('pebs_reload_mode') == 'guarded_auto', 'Guest must contain the validated guarded PEBS implementation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', default='guest-tools-final', help='existing guestctl VM identity')
    parser.add_argument('--access', type=Path)
    parser.add_argument('--name', '--run-id', dest='name', default='physical-skew-' + time.strftime('%Y%m%d-%H%M%S'),
                        help='unique experiment/result name; unrelated to the existing VM identity')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--remote-dir', help='new dedicated Guest directory; retained as evidence')
    parser.add_argument('--binary', type=Path, default=ROOT / 'tests/chameleon_skew')
    parser.add_argument('--rdma-device', help='active Guest mlx5 device; auto-detect only when unique')
    parser.add_argument('--netdev', help='required physical interface for the server IP route')
    for name, default in [('gib',16), ('guest-mib',24576), ('pool-mib',8192), ('threads',4),
                          ('hot-access-ppm',999990), ('sampling-period',8192), ('cold-folios',16),
                          ('warmup-seconds',30), ('run-seconds',180), ('reclaim-mib',4096),
                          ('reclaim-timeout',360), ('verify-timeout',1800), ('sample-seconds',5),
                          ('psi-ppm',1000000)]:
        parser.add_argument('--' + name, type=int, default=default)
    args = parser.parse_args()
    require(re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', args.name), 'experiment name must be a simple unique name')
    require(args.binary.is_file(), 'build the chameleon_skew workload binary before running')
    require(all(getattr(args, k) > 0 for k in ('gib','guest_mib','pool_mib','threads','sampling_period',
            'cold_folios','warmup_seconds','run_seconds','reclaim_mib','reclaim_timeout','verify_timeout','sample_seconds')),
            'sizes, counts and timeouts must be positive')
    require(0 <= args.hot_access_ppm <= 1000000 and 0 <= args.psi_ppm <= 1000000 and
            args.guest_mib * MIB > args.gib * GIB and args.pool_mib >= args.reclaim_mib,
            'invalid experiment dimensions or policy parameters')
    access = guest.load_access(args.access or ROOT / 'build/guests' / args.vm / 'access.json')
    require(access['name'] == args.vm, 'access and requested VM identities differ')
    output = (args.output or ROOT / 'results' / ('vm-' + args.name)).resolve()
    require(not output.exists(), 'choose a new output directory; failure evidence must not be overwritten')
    output.mkdir(parents=True)
    remote_dir = args.remote_dir or '/home/' + access['user'] + '/chameleon-tests/' + args.name
    require(remote_dir.startswith('/') and not any(c in remote_dir for c in '\n\r\0'),
            'remote directory must be an absolute single-line path')
    remote_binary, remote_log, fifo = (remote_dir + '/' + name for name in ('chameleon_skew','workload.log','commands'))
    q = shlex.quote
    report = {'status':'RUNNING', 'configuration':{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
              'physical_host':subprocess.check_output(['uname','-r'],text=True).strip(),
              'vm':args.vm,
              'scope':'existing disk Guest with physical mlx5 VFIO RDMA and preconnected external Hermit storage',
              'rss_interpretation':'VFIO may pin all Guest RAM before the workload. RSS includes that pre-pinned backing; '
                  'initial RSS is not an application allocation measurement. Acceptance still requires an observed RSS reduction '
                  'and mincore absence for retired ranges, followed by restoration.',
              'counter_units':{'port_xmit_data':'4-byte units','port_rcv_data':'4-byte units'},
              'samples':[], 'phases':{}, 'remote_directory':remote_dir}
    report['input_sha256'] = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                             (args.binary.resolve(),Path(__file__).resolve(),ROOT/'tests/chameleon_skew.c')}
    started = time.monotonic()
    qmp = console = process = before = qbefore = lock = host_before = rdma_before = None
    mutated = workload_started = workload_exited = data_verified = False
    phase, last_log = 'preflight', ''

    def save():
        report['duration_seconds'] = round(time.monotonic()-started,3)
        temp = output / 'report.json.tmp'
        temp.write_text(json.dumps(report,indent=2)+'\n')
        temp.replace(output / 'report.json')

    def sample(label, query=False, check_workload=True):
        nonlocal last_log
        text = console.command('; '.join('echo SKEW_STATS_'+key+'; cat /sys/kernel/debug/'+value+'/stats'
            for key,value in skew.PATHS.items())+'; echo SKEW_WORKLOAD_BEGIN; tail -n 16 '+q(remote_log)+
            '; echo SKEW_WORKLOAD_END',timeout=120)
        groups = re.split(r'^SKEW_STATS_(\w+)\s*$',text,flags=re.M)
        row = {'seconds':round(time.monotonic()-started,3),'phase':label,
               'compute_memory':skew.rss(host_before['pid'])}
        for i in range(1,len(groups),2): row[groups[i]]=fields(groups[i+1].split('SKEW_WORKLOAD_BEGIN')[0])
        match = re.search(r'^SKEW_WORKLOAD_BEGIN\n(.*?)^SKEW_WORKLOAD_END',text,re.M|re.S)
        require(match,'workload log must be available')
        last_log = match[1]
        row['workload_tail'] = last_log
        events = [json.loads(l) for l in last_log.splitlines() if l.startswith('{"event":')]
        if events: row['workload_event']=events[-1]
        report['samples'].append(row)
        save()  # Preserve the failing sample before any assertion raises.
        require(not check_workload or not re.search(r'SKEW_ERROR|SKEW_FAIL|SKEW_CORRUPTION|SKEW_LEDGER_MISMATCH|status=FAIL|"(?:verify_)?errors":\s*[1-9]|SKEW_EXIT=[1-9]',last_log),
                'workload must remain healthy and data-correct')
        require(not row['rdma']['broken'],'physical RDMA transport must remain connected')
        if query:
            row['pmu']=console.command('cat /sys/kernel/debug/chameleon/pmu')
            full=qmp.execute('query-llfree-balloon'); ch=full['chameleon']
            (output/('qmp-'+label+'-'+str(len(report['samples']))+'.json')).write_text(json.dumps(full,indent=2)+'\n')
            row['qmp']={k:v for k,v in full.items() if k!='chameleon'}
            row['qmp']['chameleon']={k:v for k,v in ch.items() if k!='ranges'}
            row['qmp']['range_orders']={str(order):sum(r['order']==order for r in ch['ranges']) for order in range(10)}
            row['qmp']['retired_resident_pages']=sum(r['resident-pages'] for r in ch['ranges'] if r['state']==5)
            capacity_ok=observe_capacity(report,full,(args.guest_mib-args.reclaim_mib)*MIB)
            save()
            require(capacity_ok,'policy lease must preserve the configured local-memory capacity floor')
            require(not ch['error-pages'],'Host transactions must retain correct ownership')
            require(all(r['residency-status']==0 for r in ch['ranges']),'Host backing must be queryable')
            require(full['actual']==args.guest_mib*MIB-ch['retired-pages']*4096 and ch['blocked-pages']==ch['retired-pages'],
                    'Host capacity must match actual retirement without legacy ballooning')
            require(all(r['flags']==2 for r in ch['ranges'] if r['state']==5),'retired data must be saved')
            require(all(r['dma-unmapped'] and not r['host-installed'] for r in ch['ranges'] if r['state']==5),
                    'retired VFIO ranges must release DMA mappings and Host backing')
        save()
        print(json.dumps({'phase':label,'seconds':row['seconds'],'rss_gib':round(row['compute_memory']['Rss']/GIB,3),
            'remote_gib':round(row['shadow']['host_reclaimed_pages']*4096/GIB,3),
            'read_gib':round(row['hermit']['bytes_read']/GIB,3),'hardware_samples':row['tracker']['hardware_samples']}),flush=True)
        return row

    def work(value, marker, timeout):
        boundary='SKEW_COMMAND_BOUNDARY_'+str(time.monotonic_ns())
        console.command('echo '+boundary+' >> '+q(remote_log)+'; echo '+q(value)+' >&3')
        deadline=time.monotonic()+timeout
        while True:
            row=sample(phase)
            if re.search(marker,last_log.rsplit(boundary,1)[-1],re.M): return row
            require(time.monotonic()<deadline,'workload phase timed out: '+value)
            time.sleep(args.sample_seconds)

    def resources_empty(row):
        ch,s,h=row['qmp']['chameleon'],row['shadow'],row['hermit']
        return (row['qmp']['actual']==args.guest_mib*MIB and
                not any(ch[k] for k in ('range-records','registered-pages','ready-pages','retired-pages','blocked-pages','error-pages')) and
                not any(s[k] for k in ('live_objects','live_slots','metadata_pages','reclaimed_slots',
                                      'host_reclaimed_pages','reservation_pages','owned_pages','shadow_pages')) and
                not any(h[k] for k in ('live_slots','allocated_pages','inflight')))

    try:
        candidate=guest.control_lock(access); candidate.__enter__(); lock=candidate
        guest.wait_ssh(access,30)
        qmp=vm.QMP(str(guest.run_dir(access)/'qmp.sock'))
        require(qmp.execute('query-name').get('name')==args.vm,'QMP identity mismatch')
        require(qmp.execute('query-status')['running'] and qmp.execute('query-kvm')['enabled'],'VM must already run with KVM')
        require(len(qmp.execute('query-cpus-fast'))==args.threads,'live VM vCPU count must match requested workers')
        host_before=host_identity(access); before=live.snapshot(access); rdma_before=inventory(access)
        physical=select_physical_rdma(rdma_before,args.rdma_device,args.netdev)
        qbefore=qmp.execute('query-llfree-balloon')
        report['before']={'host':host_before,'guest':before,'rdma':rdma_before,'qmp':qbefore}
        report['physical_rdma']=physical
        preflight(before,qbefore,rdma_before,args)
        guest.remote(access,['mkdir','-p',str(Path(remote_dir).parent)])
        guest.remote(access,['mkdir','-m','0700',remote_dir])
        guest.transfer(access,'upload',args.binary,remote_binary)
        guest.remote(access,['chmod','0755',remote_binary])
        process=subprocess.Popen(guest.ssh_command(access,['sudo','-n','sh']),stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        console=vm.Console(process.stdout.fileno(),process.stdin.fileno(),output/'ssh-console.log')
        console.command('test -r /sys/kernel/debug/hermit_rdma/stats && test -r /sys/kernel/debug/chameleon/pmu')
        mutated=True
        qmp.execute('chameleon-configure',{'config':{'ept-mode':'deferred','batch-pages':512,'watermark-bytes':0,
            'begin-fail-count':0,'install-fail-count':0,'dma-map-fail-count':0,'discard-fail-index':-1}})
        console.command('echo never > /sys/kernel/mm/transparent_hugepage/defrag && '
            'for f in /sys/kernel/mm/transparent_hugepage/hugepages-*kB/enabled; do echo never > "$f" || exit; done && '
            'echo always > /sys/kernel/mm/transparent_hugepage/hugepages-2048kB/enabled && '
            'echo 600000 > /sys/kernel/mm/transparent_hugepage/khugepaged/scan_sleep_millisecs')
        console.command('mkfifo '+q(fifo)+' || exit; ('+q(remote_binary)+' --gib '+str(args.gib)+' --threads '+str(args.threads)+
            ' --hot-access-ppm '+str(args.hot_access_ppm)+' < '+q(fifo)+' >> '+q(remote_log)+
            ' 2>&1; echo SKEW_EXIT=$? >> '+q(remote_log)+') & skew_pid=$!; exec 3>'+q(fifo),timeout=30)
        workload_started=True
        phase='initialize'; deadline=time.monotonic()+300
        while True:
            initial=sample(phase)
            ready=re.search(r'^SKEW_READY pid=(\d+) address=(0x[0-9a-f]+) bytes=(\d+) threads=(\d+)',last_log,re.M)
            if ready: break
            require(time.monotonic()<deadline,'full working-set initialization timed out')
            time.sleep(args.sample_seconds)
        pid,address,size,threads=int(ready[1]),ready[2],int(ready[3]),int(ready[4])
        require(size==args.gib*GIB and threads==args.threads,'actual workload dimensions differ')
        actual=dict(re.findall(r'(\w+)=([^ ]+)',re.search(r'^SKEW_READY .*$',last_log,re.M)[0]))
        require(int(actual['hot_access_ppm'])==args.hot_access_ppm and float(actual['hot_percent'])==1 and
                int(actual['write_percent'])==10 and int(actual['seed'])==1,'actual workload distribution differs')
        report['workload']={'pid':pid,'address':address,'bytes':size,'threads':threads,'hot_pages_percent':1,
                            'hot_access_ppm':args.hot_access_ppm,'write_percent':10,'actual_ready':actual}
        ram=args.guest_mib*MIB; target=args.reclaim_mib*MIB
        # A persistent Guest may have tracking enabled before the test.
        command(console,'tracker','disable')
        for value in ['reset',f'capacity {ram} {ram}',f'sampling fixed {args.sampling_period}','cooling fixed 131072','enable']:
            command(console,'tracker',value)
        for value in [f'target {pid} {address} {size}','split_mode hhh','selector mixed_cost','batch 32 6400 128 4 10']:
            command(console,'manager',value)
        for order in [0]+list(range(2,10)): command(console,'manager',f'cost {order} {1<<order}')
        command(console,'manager','enable')
        phase='warmup'
        work(f'warmup {args.warmup_seconds}',r'^SKEW_DONE phase=warmup status=PASS\b',args.warmup_seconds+120)
        report['phases']['baseline']=baseline=sample('baseline',True)
        require(baseline['tracker']['hardware_samples']>0 and baseline['tracker']['accepted_samples']>0 and
                not baseline['tracker']['synthetic_samples'],'hotness must use actual hardware records')
        require(baseline['compute_memory']['Rss']>0.9*size,'compute backing must initially cover the complete dataset')
        for value in ['set epoch_us 10000',f'set threshold_ppm {args.psi_ppm}','set psi_full 0','set free_pages 0',
                      f'set cold_folios {args.cold_folios}',f'set minimum_local_bytes {ram-target}',
                      'set discard_test 0',f'target {pid} {address} {size}','enable']:
            command(console,'policy',value)
        phase='skew_reclaim'; deadline=time.monotonic()+args.reclaim_timeout
        while True:
            work(f'run {args.run_seconds}',r'^SKEW_DONE phase=run status=PASS\b',args.run_seconds+300)
            current=sample('reclaim_checkpoint',True)
            active_target = any(s['phase']=='skew_reclaim' and
                s.get('workload_event',{}).get('event')=='progress' and
                s['shadow']['host_reclaimed_pages']*4096>=target-64*MIB for s in report['samples'])
            if (active_target and current['qmp']['chameleon']['retired-pages']*4096>=target-64*MIB) or time.monotonic()>=deadline:
                break
        qmp.execute('chameleon-configure',{'config':{'batch-pages':1}})
        report['tail_drain_batch_pages']=1
        deadline=time.monotonic()+300
        while True:
            row=sample('drain_pending')
            if not row['shadow']['shadow_pages'] and not row['hermit']['inflight']: break
            require(time.monotonic()<deadline,'pending save pipeline must drain')
            time.sleep(args.sample_seconds)
        report['phases']['retired']=retired=sample('retired',True)
        reclaimed=retired['qmp']['chameleon']['retired-pages']*4096
        reduction=baseline['compute_memory']['Rss']-retired['compute_memory']['Rss']
        report['checks']={'retirement_target':reclaimed>=target-64*MIB,
            'retirement_during_random_access':any(s['phase']=='skew_reclaim' and
                s.get('workload_event',{}).get('event')=='progress' and s['shadow']['host_reclaimed_pages']*4096>=target-64*MIB
                for s in report['samples']), 'rss_reduction':reduction>=reclaimed*0.7,
            'sampling_reclaim':retired['tracker']['hardware_samples']>baseline['tracker']['hardware_samples']}
        require(not retired['qmp']['retired_resident_pages'],'retired backing must actually be absent')
        require(retired['policy']['hard_reclaimed_bytes']==baseline['policy']['hard_reclaimed_bytes'],
                'free-page ballooning must not contribute to memory reduction')
        phase='shifted_hotspot'; console.command("echo 'shift 50' >&3")
        work(f'run {args.run_seconds}',r'^SKEW_DONE phase=run status=PASS\b',args.run_seconds+300)
        report['phases']['shifted']=shifted=sample('shifted',True)
        report['checks']['demand_readback']=(shifted['shadow']['load_demand_attempts']>retired['shadow']['load_demand_attempts'] and
            shifted['shadow']['data_fault_restores']>retired['shadow']['data_fault_restores'] and
            shifted['hermit']['bytes_read']>retired['hermit']['bytes_read'])
        report['checks']['sampling_shifted']=shifted['tracker']['hardware_samples']>retired['tracker']['hardware_samples']
        phase='policy_restore'; command(console,'manager','disable')
        command(console,'policy','disable',timeout=args.verify_timeout)
        report['phases']['policy_restored']=policy_restored=sample('policy_restored',True)
        report['restoration']={'demand_phase_load_attempts':shifted['shadow']['load_demand_attempts']-retired['shadow']['load_demand_attempts'],
            'shift_phase_background_load_attempts':shifted['shadow']['load_background_attempts']-retired['shadow']['load_background_attempts'],
            'shift_phase_total_bytes_read':shifted['hermit']['bytes_read']-retired['hermit']['bytes_read'],
            'disable_background_load_attempts':policy_restored['shadow']['load_background_attempts']-shifted['shadow']['load_background_attempts'],
            'disable_bytes_read':policy_restored['hermit']['bytes_read']-shifted['hermit']['bytes_read']}
        phase='verify_all_data'
        work('verify',r'^SKEW_VERIFIED status=PASS bytes='+str(size)+r' errors=0 cumulative_errors=0\b',args.verify_timeout)
        data_verified=True
        command(console,'tracker','disable'); command(console,'tracker','drain')
        deadline=time.monotonic()+120
        while True:
            restored=sample('restored',True)
            if resources_empty(restored): break
            require(time.monotonic()<deadline,'all restored Host/Guest/backend ownership must drain')
            time.sleep(args.sample_seconds)
        report['phases']['restored']=restored
        require(restored['compute_memory']['Rss']>=baseline['compute_memory']['Rss']*0.9,
                'full verification must repopulate compute backing')
        report['checks']['readback_after_retirement']=(restored['shadow']['demand_fault_successes']>retired['shadow']['demand_fault_successes'] and
            restored['hermit']['bytes_read']>retired['hermit']['bytes_read'])
        console.command("echo quit >&3; exec 3>&-")
        full=console.command('wait "$skew_pid"; cat '+q(remote_log),timeout=120)
        (output/'workload.log').write_text(full)
        require(re.search(r'^SKEW_EXIT=0$',full,re.M),'workload must exit successfully')
        workload_exited=True
        report['summary']={'workload_bytes':size,'threads':threads,'retired_bytes':reclaimed,
            'compute_rss_reduction_bytes':reduction,'baseline_rss':baseline['compute_memory']['Rss'],
            'retired_rss':retired['compute_memory']['Rss'],'restored_rss':restored['compute_memory']['Rss'],
            'data_verified':True,'hardware_samples':restored['tracker']['hardware_samples'],
            'demand_faults':restored['shadow']['demand_faults']-baseline['shadow']['demand_faults'],
            'bytes_written':restored['hermit']['bytes_written']-baseline['hermit']['bytes_written'],
            'bytes_read':restored['hermit']['bytes_read']-baseline['hermit']['bytes_read']}
        windows=[]
        for name in ('warmup','skew_reclaim','shifted_hotspot'):
            anchor=None
            for point in report['samples']:
                if point['phase']!=name: continue
                if anchor is None: anchor=point
                elif point['seconds']-anchor['seconds']>=30:
                    windows.append({'phase':name,'start':anchor['seconds'],'end':point['seconds'],
                        'samples':point['tracker']['hardware_samples']-anchor['tracker']['hardware_samples']})
                    anchor=point
        report['sampling_windows']=windows
        report['checks']['sampling_no_stall']=(all(w['samples']>0 for w in windows) and
            all(any(w['phase']==name for w in windows) for name in ('skew_reclaim','shifted_hotspot')))
        report['checks']['rdma_transport']=(not restored['rdma']['broken'] and
            restored['rdma']['transfer_errors']==baseline['rdma']['transfer_errors'] and
            restored['rdma']['write_completions']>baseline['rdma']['write_completions'] and
            restored['rdma']['read_completions']>baseline['rdma']['read_completions'])
        capacity=report['capacity_observations']
        report['checks']['capacity_floor']=(capacity['lease_active_samples']>0 and not capacity['floor_violations'])
        report['summary']['minimum_local_bytes']=capacity['minimum_local_bytes']
        report['summary']['minimum_leased_local_bytes']=capacity['minimum_leased_local_bytes']
        phase='acceptance'
        require(all(report['checks'].values()),'experiment acceptance checks: '+str(report['checks']))
        report['status']='PASS'
    except (Exception,KeyboardInterrupt) as error:
        report.update(status='FAIL',error=repr(error),failed_phase=phase)
        if console:
            try: (output/'failure-diagnostics.log').write_text(console.command('cat '+q(remote_log)+'; dmesg',timeout=20))
            except Exception as diagnostic_error: report['diagnostic_error']=repr(diagnostic_error)
    finally:
        errors=[]
        if console and mutated:
            try:
                command(console,'manager','disable')
                command(console,'policy','disable',timeout=args.verify_timeout)
                if workload_started and not workload_exited:
                    # Finish the current bounded phase, then verify every byte
                    # before asking this owned process to exit. No PID killing.
                    marker='SKEW_RECOVERY_BOUNDARY_'+str(time.monotonic_ns())
                    text=console.command('echo '+marker+' >> '+q(remote_log)+
                        "; echo verify >&3; echo quit >&3; exec 3>&-; wait \"$skew_pid\"; cat "+q(remote_log),
                        timeout=args.verify_timeout+args.run_seconds+300)
                    (output/'workload-recovery.log').write_text(text)
                    section=text.rsplit(marker,1)[-1]
                    report['failure_recovery_verified']=bool(re.search(r'^SKEW_VERIFIED status=PASS bytes='+str(args.gib*GIB)+
                        r' errors=0 cumulative_errors=0\b',section,re.M))
                    workload_exited=bool(re.search(r'^SKEW_EXIT=0$',section,re.M))
                    require(workload_exited,'owned workload must exit cleanly during recovery')
                command(console,'tracker','disable'); command(console,'shadow','drain')
                deadline=time.monotonic()+120
                while True:
                    report['cleanup_resources']=row=sample('cleanup_resources',True,check_workload=False)
                    if resources_empty(row): break
                    require(time.monotonic()<deadline,'cleanup must release all Host/Guest/Hermit resources')
                    time.sleep(args.sample_seconds)
            except Exception as error: errors.append('restore test-owned data/process: '+repr(error))
        if process:
            try:
                process.stdin.close(); process.wait(timeout=30)
            except Exception as error: errors.append('close owned SSH shell: '+repr(error))
        if mutated and before and (not process or process.poll() is not None):
            try: report['restore_guest']=live.remote_python(access,live.RESTORE,json.dumps(before))
            except Exception as error: errors.append('restore Guest settings: '+repr(error))
        if qmp and mutated and qbefore:
            try:
                saved=qbefore['chameleon']
                qmp.execute('chameleon-configure',{'config':{**{k:saved[k] for k in ('ept-mode','batch-pages','watermark-bytes')},
                    'begin-fail-count':0,'install-fail-count':0,'dma-map-fail-count':0,'discard-fail-index':-1}})
            except Exception as error: errors.append('restore QMP configuration: '+repr(error))
        if before and rdma_before and report.get('physical_rdma'):
            try:
                after=live.snapshot(access); rdma_after=inventory(access); host_after=host_identity(access)
                report['after']={'host':host_after,'guest':after,'rdma':rdma_after,'qmp':qmp.execute('query-llfree-balloon')}
                selected=select_physical_rdma(rdma_after,report['physical_rdma']['name'],report['physical_rdma']['netdev'])
                require(host_after['pid']==host_before['pid'] and host_after['start_time']==host_before['start_time'] and
                        host_after['vfio']==host_before['vfio'] and
                        host_after['loaded_kvm_identity']==host_before['loaded_kvm_identity'] and rdma_after['boot_id']==rdma_before['boot_id'],
                        'VM process/boot/physical PCI identity must remain unchanged')
                require(all(selected[k]==report['physical_rdma'][k] for k in
                            ('name','pci_bdf','driver','netdev','parameters','route_identity','hermit_cm')),
                        'physical RDMA device, backend, route and kernel CM connection must remain unchanged')
                require(fields(after['stats']['hermit'])['registered'] == 1,
                        'physical Hermit backend must remain registered after restoring settings')
                name=selected['name']; deltas={}
                for port,info in rdma_after['devices'][name]['ports'].items():
                    old=rdma_before['devices'][name]['ports'][port]['counters']
                    deltas[port]={k:v-old[k] for k,v in info['counters'].items() if k in old}
                report['physical_rdma_counter_delta']=deltas
                if data_verified:
                    require(any(d.get('counters/port_xmit_data',0)>0 and d.get('counters/port_rcv_data',0)>0
                                for d in deltas.values()),'physical mlx5 transmit and receive data counters must increase')
                old=before['dmesg'].splitlines(); new=after['dmesg'].splitlines()
                diagnostic='\n'.join(new[len(old):]) if new[:len(old)]==old else after['dmesg']
                (output/'guest-dmesg-new.log').write_text(diagnostic+'\n')
                require(not re.search(skew.BAD,diagnostic),'new Guest kernel diagnostics contain errors')
                require(qmp.execute('query-status')['running'],'existing Guest must remain running')
            except Exception as error: errors.append('final identity/counter/health checks: '+repr(error))
        if qmp:
            qmp.file.close(); qmp.sock.close()
        if lock: lock.__exit__(None,None,None)
        if errors: report.update(status='FAIL',cleanup_errors=errors)
        save()
        print(json.dumps({k:report[k] for k in ('status','summary','failed_phase','error','cleanup_errors') if k in report},indent=2),flush=True)
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
