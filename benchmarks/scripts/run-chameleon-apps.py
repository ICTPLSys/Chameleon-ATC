#!/usr/bin/env python3
"""Measure whole-VM free- and cold-page reclamation while existing real applications run.

Uses the running physical mlx5 Guest, preserves its configuration, and keeps
raw failures. No synthetic PFNs/heat; explicit runtime tuning parameters are recorded.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
import uuid
from types import SimpleNamespace
import chameleon_affinity as affinity
import chameleon_barrier

REPO = Path(__file__).resolve().parents[2]
HA = REPO / 'hyperalloc-6.18'
spec = importlib.util.spec_from_file_location('app_physical', HA/'scripts/test-physical-skew.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
guest, live, vm = base.guest, base.live, base.vm
MIB, GIB = 1 << 20, 1 << 30
APPS = ['cassandra', 'xsbench', 'liblinear', 'graph500', 'graphchi', 'spark-kmeans', 'pvc', 'memcached']
SAMPLE = r'''
import json, pathlib, re, os
P=pathlib.Path
result={}
for group,name in GROUPS.items():
 values={}
 for key,value in re.findall(r'^(\w+) (\S+)$',(P('/sys/kernel/debug')/name/'stats').read_text(),re.M):
  try: values[key]=int(value,0)
  except ValueError: values[key]=value
 result[group]=values
result['meminfo']={k:int(v)*1024 for k,v in re.findall(r'^(\w+):\s+(\d+) kB$',P('/proc/meminfo').read_text(),re.M)}
result['applications']=[]
for p in P('/proc').glob('[0-9]*'):
 try:
  comm=(p/'comm').read_text().strip()
  if comm not in ('java','XSBench','train','predict','omp-csr','pagerank','page_view_count','memcached'): continue
  argv=(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
  status=(p/'status').read_text()
  thread_masks=set()
  for t in (p/'task').iterdir():
   try: thread_masks.add(tuple(sorted(os.sched_getaffinity(int(t.name)))))
   except ProcessLookupError: pass
  result['applications'].append({'pid':int(p.name),'comm':comm,
   'allowed_cpus':sorted(os.sched_getaffinity(int(p.name))),
   'thread_cpu_masks':[list(mask) for mask in sorted(thread_masks)],
   'cassandra':'CassandraDaemon' in argv,'spark':'ChameleonSparkKMeans' in argv,
   **{k:int(v)*1024 for k,v in re.findall(r'^(VmRSS|VmHWM|RssAnon|RssFile):\s+(\d+) kB$',status,re.M)}})
 except (OSError,ProcessLookupError): pass
print('APP_SAMPLE='+json.dumps(result))
'''.replace('GROUPS',repr(base.skew.PATHS))


def save(path, data):
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,indent=2)+'\n'); tmp.replace(path)


def validate_affinity_profile(profile, topology, allowed, node, vcpus):
    """Validate a parent's allocation instead of selecting the first node cores."""
    if profile.get('host_numa_node') != node:
        raise ValueError('Affinity profile NUMA node differs from the VM configuration')
    for key in ['vcpu_host_cpus','qemu_service_cpus','guest_application_cpus']:
        values=profile.get(key)
        if not isinstance(values,list) or not values or any(type(v) is not int or v<0 for v in values) or len(values)!=len(set(values)):
            raise ValueError('Invalid affinity CPU list: '+key)
    if len(profile['vcpu_host_cpus'])!=vcpus or len(profile['qemu_service_cpus'])!=2:
        raise ValueError('Affinity profile needs one core per vCPU and two service cores')
    if not set(profile['guest_application_cpus'])<=set(range(vcpus)):
        raise ValueError('Guest application CPU lies outside the VM')
    cpus=profile['vcpu_host_cpus']+profile['qemu_service_cpus']
    rows={row['cpu']:row for row in topology}
    if len(cpus)!=len(set(cpus)) or not set(cpus)<=set(allowed):
        raise ValueError('Affinity CPU allocation overlaps or is outside the QEMU allowed mask')
    if any(cpu not in rows or rows[cpu]['node']!=node for cpu in cpus):
        raise ValueError('Affinity CPU allocation is outside the selected NUMA node')
    if len({(rows[cpu]['socket'],rows[cpu]['core']) for cpu in cpus})!=len(cpus):
        raise ValueError('Affinity allocation shares a physical core through SMT')
    return dict(profile,protocol=affinity.PROTOCOL)


def preflight_affinity(pid, cpus, cfg, profile=None):
    allowed, masks = affinity.qemu_allowed_cpus(pid, cfg['host_cpus'], cpus)
    topology = affinity.topology()
    selected = (validate_affinity_profile(profile, topology, allowed, cfg['host_numa_node'], len(cpus))
                if profile is not None else affinity.select_cores(topology, allowed, cfg['host_numa_node'], len(cpus)))
    evidence = {'qemu_pid': pid, 'allowed_cpus': sorted(allowed),
                'thread_cpu_masks': masks, 'configured_host_cpus': cfg['host_cpus'],
                'host_numa_node': cfg['host_numa_node'], 'vcpus': len(cpus)}
    return selected, evidence


def client_environment(access, node, client_node, cassandra_port, memcached_port, client_cpus=None, start_barrier=None):
    result=os.environ.copy()
    for prefix in ['CASSANDRA','MEMCACHED']:
        for key,value in {'GUEST_NAME':access['name'],'GUEST_DIR':access['_path'].parent,
                          'GUEST_HOST':access['host'],'GUEST_USER':access['user'],
                          'GUEST_SSH_PORT':access['port'],'GUEST_NUMA_NODE':node,
                          'CLIENT_NUMA_NODE':client_node}.items():
            result[prefix+'_'+key]=str(value)
    result['CASSANDRA_QEMU_PID_FILE']=str(guest.run_dir(access)/'qemu.pid')
    result['MEMCACHED_QEMU_RUN_DIR']=str(guest.run_dir(access))
    result['CASSANDRA_LOCAL_CQL_PORT']=str(cassandra_port)
    result['MEMCACHED_HOST_PORT']=str(memcached_port)
    if client_cpus is not None:
        for prefix in ['CASSANDRA','MEMCACHED']:
            result[prefix+'_CLIENT_CPUS']=client_cpus
    for prefix,script in [('CASSANDRA','cassandra'),('MEMCACHED','memcached')]:
        result[prefix+'_GUEST_CONTROL']='/home/'+access['user']+'/chameleon-benchmarks/scripts/'+script+'-guest.sh'
    for key in ['DIR','MEMBER','TIMEOUT']:
        result.pop('CHAMELEON_BARRIER_'+key,None)
    if start_barrier is not None:
        for key,value in start_barrier.items():
            result['CHAMELEON_BARRIER_'+key]=str(value)
    return result


def integrate(rows, key):
    if len(rows)<2: return None
    span=rows[-1]['seconds']-rows[0]['seconds']
    return sum((b['seconds']-a['seconds'])*(a[key]+b[key])/2
               for a,b in zip(rows,rows[1:]))/span if span else None


def resources_empty(row):
    return (not any(row['shadow'][k] for k in ['live_objects','owned_pages','shadow_pages','reservation_pages','host_reclaimed_pages'])
            and not any(row['hermit'][k] for k in ['live_slots','allocated_pages','inflight'])
            and not row['policy'].get('hard_reclaimed_bytes',0)
            and not row['qmp'].get('policy',{}).get('hard-reclaimed-bytes',0)
            and not any(row['qmp'][k] for k in ['range-records','retired-pages','registered-pages','ready-pages','blocked-pages','error-pages']))


def pre_reclaim_reached(row, target):
    # Both sides must acknowledge the capacity change before application launch.
    return (row['policy']['local_bytes'] <= target + 2*MIB
            and row['qmp']['policy']['local-bytes'] <= target + 2*MIB)


def runtime_policy_command(minimum_local, cold_folios, epoch_us):
    # Debugfs writes preserve the lease; disable/enable would undo preparation.
    return '; '.join(f'echo {value} > /sys/kernel/debug/chameleon_policy/{key}'
                     for key, value in [('epoch_us', epoch_us),
                                        ('minimum_local_bytes', minimum_local),
                                        ('cold_folios', cold_folios)])


def all_local_inactive(before, after):
    if any(row[group]['enabled'] for row in [before,after] for group in ['tracker','manager','policy']):
        return False
    if before['policy']['lease_active'] or after['policy']['lease_active']:
        return False
    fields={'tracker':['hardware_samples','processed_samples'],
            'manager':['epochs','scanned','split_ok','selected'],
            'policy':['epochs','free_reclaimed_bytes','free_returned_bytes','shadow_prepared_pages'],
            'rdma':['write_bytes','read_bytes']}
    return all(after[g][k]==before[g][k] for g,keys in fields.items() for k in keys)


def all_local_tracking_active(before, after):
    for row in [before,after]:
        if not row['tracker']['enabled'] or not row['manager']['enabled']:
            return False
        if row['policy']['enabled'] or row['policy']['lease_active'] or row.get('retired_bytes',0):
            return False
    fields={'policy':['epochs','free_reclaimed_bytes','free_returned_bytes','shadow_prepared_pages'],
            'rdma':['write_bytes','read_bytes'],'manager':['selected']}
    return all(after[g][k]==before[g][k] for g,keys in fields.items() for k in keys)


def summarize(rows, total):
    active=[r for r in rows if r['phase']=='running']
    mean=integrate(active,'retired_bytes')
    free_rows=[dict(r,free_bytes=r.get('policy',{}).get('hard_reclaimed_bytes',0)) for r in active]
    rss=[r['compute_memory']['Rss'] for r in active]
    mean_rss=sum(rss)/len(rss) if rss else None
    return {'window':'samples bracketing launcher execution (last sample before launch through first after exit), includes input checks/initialization; excludes explicit restore',
      'duration_seconds':active[-1]['seconds']-active[0]['seconds'] if len(active)>1 else 0,
      'samples':len(active),'mean_retired_bytes':mean,
      'metric_version':'paper-rss-v1',
      'window_complete':False, # caller sets true only after the post-exit sample succeeds
      'configured_memory_bytes':total,
      'mean_qemu_rss_bytes':mean_rss,
      'mean_reclaim_percent':100*(1-mean_rss/total) if mean_rss is not None else None,
      'mean_backing_reclaim_percent':100*mean/total if mean is not None else None,
      'mean_free_reclaimed_bytes':integrate(free_rows,'free_bytes'),
      'peak_free_reclaimed_bytes':max((r['free_bytes'] for r in free_rows),default=0),
      'peak_retired_bytes':max((r['retired_bytes'] for r in active),default=0),
      'peak_reclaim_percent':100*(1-min(rss)/total) if rss else None,
      'peak_backing_reclaim_percent':100*max((r['retired_bytes'] for r in active),default=0)/total,
      'peak_application_rss_bytes':max((r['application_rss_bytes'] for r in active),default=0),
      'peak_application_hwm_bytes':max((r.get('application_hwm_bytes',r['application_rss_bytes']) for r in active),default=0),
      'application_observed_samples':sum(r['application_rss_bytes']>0 for r in active),
      'minimum_qemu_rss_bytes':min((r['compute_memory']['Rss'] for r in active),default=0),
      'maximum_sampling_gap_seconds':max((b['seconds']-a['seconds'] for a,b in zip(active,active[1:])),default=0)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name',default='physical-apps-'+time.strftime('%Y%m%d-%H%M%S'))
    p.add_argument('--results-root',type=Path,default=REPO/'benchmarks/results/chameleon',help='Parent directory for this run, including raw samples and application logs')
    p.add_argument('--performance-mode',action='store_true',help='Record diagnostic policy/data counters without turning counter increases into performance-run failures')
    p.add_argument('--vm',default='guest-tools-final')
    p.add_argument('--mode',choices=['chameleon','all-local','tracking-only'],default='chameleon')
    p.add_argument('--profile',choices=['table2','small'],default='table2',help='Workload defaults (default: table2); small selects legacy inputs')
    p.add_argument('--workload-config',type=Path,help='Override the selected profile workload JSON')
    p.add_argument('--cpu-pinning',action=argparse.BooleanOptionalAction,default=True,
                   help='Pin each vCPU to a physical Host core and explicitly bind Guest applications')
    p.add_argument('--cpu-affinity-profile',type=Path,help='Parent-allocated affinity profile JSON; validated against VM topology and allowed CPUs')
    p.add_argument('--start-barrier-dir',type=Path,help='Prepared co-run barrier directory; parent writes RELEASE or ABORT')
    p.add_argument('--barrier-member',help='Unique member name within the start barrier')
    p.add_argument('--barrier-timeout',type=float,default=1800)
    p.add_argument('--client-numa-node',type=int,default=1)
    p.add_argument('--client-cpus',help='Explicit Host client CPU subset within the client NUMA node')
    p.add_argument('--rdma-interface',default='ibp1s0')
    p.add_argument('--cassandra-local-cql-port',type=int,default=19042)
    p.add_argument('--memcached-host-port',type=int,default=11211)
    p.add_argument('--memory-plan',type=Path,help='Verified all-local peak+2GiB sizing evidence for this one-app run')
    p.add_argument('--cases',nargs='+',choices=['idle']+APPS)
    p.add_argument('--sample-seconds',type=float,default=0.5)
    p.add_argument('--timeout',type=int,default=3600)
    p.add_argument('--graph500-scale',type=int,default=22,
                   help='existing validated 4-vCPU input; larger scales can take much longer')
    p.add_argument('--psi-ppm',type=int,default=10000)
    p.add_argument('--epoch-us',type=int,default=10000)
    p.add_argument('--cold-folios',type=int,default=16)
    p.add_argument('--minimum-local-mib',type=int,help='Explicit local capacity floor; overrides max-reclaim-mib')
    p.add_argument('--pre-reclaim-headroom-mib',type=int,help='Before launching workload, reclaim free pages to local floor plus this margin; 0 targets the floor')
    p.add_argument('--max-reclaim-mib',type=int,default=6144)
    p.add_argument('--free-pages',type=int,default=512,help='Free-page batch per policy epoch, multiple of 512 base pages; 0 for cold-only control')
    p.add_argument('--all-local-tracking',action=argparse.BooleanOptionalAction,default=True,help='All-local keeps PEBS+HHH enabled; disable only for overhead diagnostics')
    p.add_argument('--tracking-components',choices=['both','pebs','hhh'],default='both',help='Isolate components in tracking-only mode')
    p.add_argument('--drop-caches',action='store_true',help='Equalize Guest filesystem cache before timing each application')
    p.add_argument('--sample-period',type=int,default=65536)
    p.add_argument('--cooling-samples',type=int,default=131072)
    p.add_argument('--hhh-interval-ms',type=int,default=15000)
    p.add_argument('--pre-reclaim-epoch-us',type=int,help='Optional free-only preparation period; runtime epoch is restored before application launch')
    a=p.parse_args()
    if not 512<=a.sample_period<=2**32-1 or not 1<=a.cooling_samples<=2**40 or not 1<=a.hhh_interval_ms<=3600000: p.error('invalid tracking parameters')
    if a.cases is None: a.cases=APPS if a.profile=='table2' else ['idle']+APPS
    if a.cpu_affinity_profile and not a.cpu_pinning: p.error('affinity profile requires CPU pinning')
    if bool(a.start_barrier_dir)!=bool(a.barrier_member): p.error('start barrier requires both directory and member')
    if a.start_barrier_dir and (len(a.cases)!=1 or not 0<a.barrier_timeout<86400): p.error('barrier requires exactly one application and timeout below one day')
    if a.client_numa_node<0 or any(not 1<=port<=65535 for port in [a.cassandra_local_cql_port,a.memcached_host_port]): p.error('invalid client node or port')
    if a.client_cpus is not None:
        try: a.client_cpus=affinity.cpu_list(affinity.validate_client_cpus(a.client_cpus,a.client_numa_node))
        except ValueError as error: p.error(str(error))
    if a.workload_config is None and a.profile=='table2':
        a.workload_config=REPO/'benchmarks/config/chameleon-table2-workloads.json'
    if a.free_pages<0 or a.free_pages>2**32 or a.free_pages%512: p.error('free-pages must be a multiple of 512 in [0, 2^32]')
    profile=json.loads(a.workload_config.read_text()) if a.workload_config else None
    if profile:
        for case in a.cases:
            if case!='idle' and case not in profile['applications']: p.error('Missing workload configuration: '+case)
    free_pages=a.free_pages if a.mode=='chameleon' else 0
    tracking_pebs=a.mode=='chameleon' or (a.mode=='all-local' and a.all_local_tracking) or (a.mode=='tracking-only' and a.tracking_components in ['both','pebs'])
    tracking_hhh=a.mode=='chameleon' or (a.mode=='all-local' and a.all_local_tracking) or (a.mode=='tracking-only' and a.tracking_components in ['both','hhh'])
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,70}',a.name): p.error('invalid unique name')
    if a.sample_seconds<=0 or a.timeout<=0 or not 0<a.psi_ppm<=1000000 or not 0<a.max_reclaim_mib<8192: p.error('invalid settings')
    if not 1000<=a.epoch_us<=1000000 or not 0<=a.cold_folios<=128: p.error('invalid policy epoch/batch')
    if a.pre_reclaim_epoch_us is not None and not 1000<=a.pre_reclaim_epoch_us<=1000000: p.error('invalid pre-reclaim epoch')
    if a.minimum_local_mib is not None and (a.minimum_local_mib<2 or a.minimum_local_mib%2): p.error('local floor must be positive and 2-MiB aligned')
    if a.pre_reclaim_headroom_mib is not None and (a.pre_reclaim_headroom_mib<0 or a.pre_reclaim_headroom_mib%2): p.error('pre-reclaim headroom must be nonnegative and 2-MiB aligned')
    out=a.results_root.resolve()/a.name; out.mkdir(parents=True,exist_ok=False)
    access=guest.load_access(HA/'build/guests'/a.vm/'access.json')
    # Distinct output roots may reuse a run name on the same overlay. Keep
    # Guest logs unique as well, so later parsing cannot include old samples.
    remote='/home/'+access['user']+'/chameleon-results/'+a.name+'-'+uuid.uuid4().hex[:12]
    scripts='/home/'+access['user']+'/chameleon-benchmarks/scripts/'
    inputs='/home/'+access['user']+'/chameleon-inputs/'
    report={'status':'RUNNING','configuration':{k:str(v) if isinstance(v,Path) else v for k,v in vars(a).items()},'cases':{},'scope':'Whole-VM free-page and anonymous cold-page reclaim on real mlx5 RDMA. Application clients use their existing management-network paths.',
      'metric':'Paper section 6: 100*(1-arithmetic_mean(QEMU RSS)/initial configured VM RAM). Peak is supplemental 100*(1-min(RSS)/RAM). Backing counts are separate diagnostics; negative RSS ratios are retained.',
      'settings':{'sampling':a.sample_period,'cooling':a.cooling_samples,'hhh_interval_ms':a.hhh_interval_ms,'epoch_us':a.epoch_us,'psi_ppm':a.psi_ppm,'cold_folios':a.cold_folios,'free_pages':free_pages,'ept_mode':'deferred','batch_pages':512,'max_reclaim_mib':a.max_reclaim_mib,'target':'system-wide; other benchmark servers stopped between workloads'},
      'skipped':{'spec_cpu2017':'No licensed runnable installation/result supplied in benchmarks'}}
    report['baseline_protocol']='all-local-pebs-hhh-on-v1' if a.all_local_tracking else 'all-local-controls-off-legacy'
    report['guest_results_directory']=remote
    if a.cpu_pinning: report['baseline_protocol']+='-cpu-pinned-v1'
    report['enabled_components']={'tracker':tracking_pebs,'manager':tracking_hhh,'policy':a.mode=='chameleon'}
    if profile: report['workload_profile']=profile
    q=lambda name,args=None:guest.qmp(access,name,args,timeout=120)
    def remote_run(args,timeout=180):
        r=subprocess.run(guest.ssh_command(access,args),capture_output=True,text=True,timeout=timeout)
        if r.returncode: raise RuntimeError('Guest command failed: '+r.stdout[-1000:]+r.stderr[-1000:])
        return r.stdout
    before=console=conn=lock=host=None; initial_services={}; changed=False; original_hhh_interval=None
    affinity_before=None; cpu_profile=None; guest_cpu_list=None
    original_sigterm=None
    if a.start_barrier_dir:
        def cancel(signum,frame):
            raise KeyboardInterrupt('Co-run cancelled by parent')
        original_sigterm=signal.signal(signal.SIGTERM,cancel)
    def control(group,value): base.command(console,group,value,timeout=300)
    def service(name,action,options=()):
        prefix=['taskset','-c',guest_cpu_list] if guest_cpu_list and action=='start' else []
        text=remote_run(prefix+[scripts+name+'-guest.sh',action]+list(options),timeout=300)
        with (out/'services.log').open('a') as stream: stream.write(name+' '+action+'\n'+text+'\n')
    try:
        lock=guest.control_lock(access); lock.__enter__()
        before=live.snapshot(access); rdma=base.inventory(access); host=base.host_identity(access)
        if a.cpu_pinning:
            cfg=json.loads(Path(access['vm_config']).read_text())
            cpus=q('query-cpus-fast')
            cpu_profile,report['cpu_affinity_preflight']=preflight_affinity(
                host['pid'],cpus,cfg,
                json.loads(a.cpu_affinity_profile.read_text()) if a.cpu_affinity_profile else None)
            affinity_before,evidence=affinity.apply(host['pid'],cpus,cpu_profile)
            guest_cpu_list=affinity.cpu_list(cpu_profile['guest_application_cpus'])
            report['cpu_affinity']={'profile':cpu_profile,'host_before':affinity_before,'host_pinned':evidence}
        qbefore=q('query-llfree-balloon'); total=qbefore['chameleon']['policy']['total-bytes']
        minimum_local=a.minimum_local_mib*MIB if a.minimum_local_mib is not None else max(0,total-a.max_reclaim_mib*MIB)
        if a.mode=='chameleon':
            base.require(0<minimum_local<=total,'local floor must fit configured VM RAM')
            if a.pre_reclaim_headroom_mib is not None:
                base.require(free_pages>0,'pre-reclaim requires free-page reclamation')
                base.require(minimum_local+a.pre_reclaim_headroom_mib*MIB<=total,'pre-reclaim target must fit VM RAM')
        report['effective_policy']={'epoch_us':a.epoch_us,'psi_ppm':a.psi_ppm,'cold_folios':a.cold_folios,'minimum_local_bytes':minimum_local,'free_pages':free_pages,
            'total_reclaim_limit_bytes':total-minimum_local,'capacity_limit_source':'explicit_local_floor' if a.minimum_local_mib is not None else 'legacy_max_reclaim_mib'}
        if a.minimum_local_mib is not None: report['settings']['max_reclaim_mib']=None
        if a.memory_plan:
            plan=json.loads(a.memory_plan.read_text())
            base.require(len(a.cases)==1 and a.cases[0]==plan['case'] and total==plan['memory_mib']*MIB,'VM does not match sizing plan')
            if profile:
                selected=profile['applications'][a.cases[0]]
                base.require(plan.get('workload_args')==selected['args'] and plan.get('workload_service_args',[])==selected.get('service_args',[]),'Memory evidence does not match workload parameters')
            report['memory_plan']=plan
        base.preflight(before,qbefore,rdma,SimpleNamespace(pool_mib=8192,guest_mib=total//MIB))
        report.update(before={'guest':before,'rdma':rdma,'host':host,'qmp':qbefore},
                      physical_rdma=base.select_physical_rdma(rdma,None,a.rdma_interface))
        remote_run(['mkdir','-p',remote])
        for name in ['cassandra','memcached']:
            launcher=name+'-guest.sh'
            guest.transfer(access,'upload',REPO/'benchmarks/scripts'/launcher,scripts+launcher)
            remote_run(['chmod','+x',scripts+launcher])
        initial_services={}
        for name in ['cassandra','memcached']:
            value=remote_run(['sh','-c',shlex.quote(scripts+name+'-guest.sh')+' is-running && echo RUNNING || true'])
            initial_services[name]='RUNNING' in value
        report['initial_services']=initial_services
        conn=subprocess.Popen(guest.ssh_command(access,['sudo','-n','sh']),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        console=vm.Console(conn.stdout.fileno(),conn.stdin.fileno(),out/'ssh-console.log')
        changed=True
        knob='/sys/kernel/debug/chameleon_mm/maintenance_interval_ms'
        original_hhh_interval=remote_run(['sudo','-n','cat',knob]).strip()
        console.command('echo '+str(a.hhh_interval_ms)+' > '+knob)
        report['tracking_readback']={'hhh_interval_ms':int(remote_run(['sudo','-n','cat',knob]))}
        q('chameleon-configure',{'config':{'ept-mode':'deferred','batch-pages':512,'watermark-bytes':0,'begin-fail-count':0,'install-fail-count':0,'dma-map-fail-count':0,'discard-fail-index':-1}})
        console.command('echo never > /sys/kernel/mm/transparent_hugepage/defrag; echo always > /sys/kernel/mm/transparent_hugepage/hugepages-2048kB/enabled')
        for name in ['cassandra','memcached']: service(name,'stop')
        for case in a.cases:
            directory=out/case; directory.mkdir()
            record=report['cases'][case]={'status':'RUNNING','samples':[],'checks':{}}
            save(out/'report.json',report)
            print('START '+case,flush=True)
            if case=='spark-kmeans':
                guest.transfer(access,'upload',REPO/'benchmarks/spark-kmeans/ChameleonSparkKMeans.java','/home/'+access['user']+'/chameleon-benchmarks/spark-kmeans/ChameleonSparkKMeans.java')
            start=time.monotonic(); serial=HA/'build/guests'/a.vm/'serial.log'; offset=serial.stat().st_size
            workload=None; transcript=None; query_serial=0
            def sample(phase,query=False):
                nonlocal query_serial
                text=console.command('python3 -c '+shlex.quote(SAMPLE),timeout=120)
                row=json.loads(next(line[len('APP_SAMPLE='):] for line in text.splitlines() if line.startswith('APP_SAMPLE=')))
                row.update(seconds=time.monotonic()-start,phase=phase,compute_memory=base.skew.rss(host['pid']))
                row['retired_bytes']=row['shadow']['host_reclaimed_pages']*4096
                names={'xsbench':'XSBench','liblinear':'train','graph500':'omp-csr','graphchi':'pagerank','pvc':'page_view_count','memcached':'memcached'}
                row['application_rss_bytes']=sum(v.get('VmRSS',0) for v in row['applications'] if
                    (v['comm']==names.get(case) or (case=='cassandra' and v['cassandra']) or (case=='spark-kmeans' and v['spark'])))
                row['application_hwm_bytes']=sum(v.get('VmHWM',0) for v in row['applications'] if
                    (v['comm']==names.get(case) or (case=='liblinear' and v['comm']=='predict') or (case=='cassandra' and v['cassandra']) or (case=='spark-kmeans' and v['spark'])))
                if query:
                    state=q('query-llfree-balloon'); ch=state['chameleon']
                    row['qmp']={k:v for k,v in ch.items() if k!='ranges'}
                    if a.drop_caches:  # Host-side diagnostics for controlled overhead runs.
                        try:
                            row['kvm_stats']={}
                            for cpu in q('query-stats',{'target':'vcpu'}):
                                for stat in cpu['stats']:
                                    if type(stat['value']) is int:
                                        key=stat['name'];row['kvm_stats'][key]=row['kvm_stats'].get(key,0)+stat['value']
                        except Exception as error:
                            row['kvm_stats_error']=str(error)
                    row['retired_resident_pages']=sum(r['resident-pages'] for r in ch['ranges'] if r['state']==5)
                    row['retired_range_count']=sum(r['state']==5 for r in ch['ranges'])
                    save(directory/('qmp-'+str(query_serial)+'.json'),state)
                    query_serial+=1
                    base.require(not ch['error-pages'],'Host range errors')
                    base.require(all(r['residency-status']==0 and r['resident-pages']==0 and r['dma-unmapped'] for r in ch['ranges'] if r['state']==5),'retired backing must be absent and DMA-unmapped')
                record['samples'].append(row)
                with (directory/'samples.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
                base.require(not row['rdma']['broken'],'RDMA connection broken')
                return row
            try:
                control('tracker','disable'); control('tracker','reset')
                if a.drop_caches:
                    control('manager','disable')
                    console.command('sync; echo 3 > /proc/sys/vm/drop_caches',timeout=120)
                    record['cache_protocol']='Guest sync + drop_caches=3 before controls enable/application launch; excluded from application timer'
                pre_reclaim=a.mode=='chameleon' and a.pre_reclaim_headroom_mib is not None
                for v in [f'capacity {total} {total}',f'sampling fixed {a.sample_period}',f'cooling fixed {a.cooling_samples}']+(['enable'] if tracking_pebs and not pre_reclaim else []): control('tracker',v)
                for v in ['clear_target','split_mode hhh','selector mixed_cost','batch 32 6400 128 4 10']: control('manager',v)
                for order in [0]+list(range(2,10)): control('manager',f'cost {order} {1<<order}')
                control('manager','enable' if tracking_hhh and not pre_reclaim else 'disable')
                for v in ['clear_target',f'set epoch_us {a.epoch_us}',f'set threshold_ppm {a.psi_ppm}','set psi_full 0',f'set free_pages {free_pages}',f'set cold_folios {a.cold_folios}',f'set minimum_local_bytes {minimum_local}','set discard_test 0']: control('policy',v)
                if pre_reclaim:
                    target=minimum_local+a.pre_reclaim_headroom_mib*MIB
                    warm_epoch=a.pre_reclaim_epoch_us or a.epoch_us
                    control('policy',f'set epoch_us {warm_epoch}')
                    control('policy',f'set minimum_local_bytes {target}')
                    control('policy','set cold_folios 0')
                    initial=sample('pre_reclaim',True)
                    record['baseline']=initial  # Cleanup also needs a baseline if preparation fails.
                    warm_start=time.monotonic()
                    ideal_seconds=max(0,total-target)/(free_pages*4096)*(warm_epoch/1e6)
                    warm_timeout=max(120,3*ideal_seconds+60)
                    record['pre_reclaim']={'status':'RUNNING','target_local_bytes':target,'headroom_mib':a.pre_reclaim_headroom_mib,'epoch_us':warm_epoch,'runtime_epoch_us':a.epoch_us,'timeout_seconds':warm_timeout,'initial':initial,
                        'scope':'Free-only preparation; tracker and manager disabled. Application launch and measured running window begin after target acknowledgement.'}
                    control('policy','enable')
                    while True:
                        row=sample('pre_reclaim',True)
                        guest_local=row['policy']['local_bytes']
                        host_local=row['qmp']['policy']['local-bytes']
                        if pre_reclaim_reached(row,target):
                            break
                        if time.monotonic()-warm_start>warm_timeout:
                            record['pre_reclaim'].update(status='FAIL',last=row)
                            raise RuntimeError('Pre-reclaim did not reach target; application was not launched')
                        if int(time.monotonic()-warm_start)%15<max(1,a.sample_seconds):
                            print(f'PRE_RECLAIM {case} local={host_local/MIB:.1f} MiB target={target/MIB:.1f} MiB',flush=True)
                        time.sleep(a.sample_seconds)
                    record['pre_reclaim'].update(status='PASS',duration_seconds=time.monotonic()-warm_start,end=row)
                    # Keep the active lease: disabling policy would return all pre-reclaimed memory.
                    console.command(runtime_policy_command(minimum_local,a.cold_folios,a.epoch_us))
                    control('tracker','enable'); control('manager','enable')
                    if case=='cassandra': service('cassandra','start',profile['applications'][case].get('service_args',[]) if profile else [])
                if case=='cassandra' and not pre_reclaim:
                    service('cassandra','start',profile['applications'][case].get('service_args',[]) if profile else [])
                if a.start_barrier_dir and case not in ['cassandra','memcached']:
                    record['baseline']=sample('prepared',True)
                    record['start_barrier']=chameleon_barrier.wait(a.start_barrier_dir,a.barrier_member,a.barrier_timeout,
                        {'case':case,'vm':a.vm,'qemu_pid':host['pid'],'configuration':report['configuration']})
                elif a.start_barrier_dir:
                    record['start_barrier']={'location':'inside_host_client_after_preparation',
                        'directory':str(a.start_barrier_dir),'member':a.barrier_member,
                        'phase':'after_load_before_read' if case=='cassandra' else 'after_warmup_before_generator'}
                record['baseline']=sample('baseline',True)
                if pre_reclaim:
                    base.require(all(record['baseline']['policy'][key]==value for key,value in
                        [('epoch_us',a.epoch_us),('cold_folios',a.cold_folios),('minimum_local_bytes',minimum_local)]),
                        'runtime policy parameters were not restored after preparation')
                if a.mode=='chameleon' and not pre_reclaim: control('policy','enable')
                common=['--output-base',remote+'/'+case]
                commands={
                 'idle':['sleep','20'],
                 # The upstream checksum oracle is tied to 17M lookups.
                 'xsbench':[scripts+'run-xsbench.sh','--skip-build','--lookups','17000000']+common,
                 'liblinear':[scripts+'run-liblinear.sh','--skip-build','--dataset',inputs+'kdd12-prefix-200k']+common,
                 'graph500':[scripts+'run-graph500.sh','--skip-build','--scale',str(a.graph500_scale),'--threads','4']+common,
                 'graphchi':[scripts+'run-graphchi.sh','--skip-build','--input',inputs+'twitter-prefix-1m.edgelist','--iterations','20']+common,
                 'spark-kmeans':[scripts+'run-spark-kmeans.sh','--input',inputs+'km-prefix-5m.dat','--threads','4','--partitions','8','--driver-memory-mib','8192']+(['--minimum-available-mib','1024'] if a.memory_plan else [])+common,
                 'pvc':[scripts+'run-pvc.sh','--skip-build','--input',inputs+'pvc-1g.bin','--repetitions','1','--minimum-available-mib','1024']+common,
                 'cassandra':[str(REPO/'benchmarks/scripts/run-cassandra-ycsb.sh'),'--records','100000','--operations','1000000','--threads','16','--connections','8','--output-base',str(directory/'application')],
                 'memcached':[str(REPO/'benchmarks/scripts/run-memcached.sh'),'--runtime','120','--rampup','2','--mpps','0.01','--output-base',str(directory/'application')]+(['--minimum-available-mib','1024'] if a.memory_plan else []),
                }
                argv=commands[case]
                if profile and case!='idle':
                    spec=profile['applications'][case]
                    argv=[argv[0]]+[v.replace('{inputs}',inputs.rstrip('/')) for v in spec['args']]
                    argv+=['--output-base',str(directory/'application') if case in ['cassandra','memcached'] else remote+'/'+case]
                    record['workload_configuration']=spec
                    if a.memory_plan:
                        # The measured peak+headroom plan replaces approximate
                        # allocation estimates, which may reject a correctly sized VM.
                        if case in ['xsbench','graph500']: argv+=['--allow-oversubscribe']
                        if case=='spark-kmeans': argv+=['--minimum-available-mib','1024']
                if guest_cpu_list and case not in ['cassandra','memcached','idle']:
                    argv=['taskset','-c',guest_cpu_list]+argv
                if guest_cpu_list and case=='memcached':
                    argv+=['--guest-cpus',guest_cpu_list]
                record['command']=argv
                actual=argv if case in ['cassandra','memcached','idle'] else guest.ssh_command(access,argv)
                client_env=None
                if case in ['cassandra','memcached']:
                    vm_config=json.loads(Path(access['vm_config']).read_text())
                    barrier_env={'DIR':a.start_barrier_dir.resolve(),'MEMBER':a.barrier_member,'TIMEOUT':a.barrier_timeout} if a.start_barrier_dir else None
                    client_env=client_environment(access,vm_config['host_numa_node'],a.client_numa_node,a.cassandra_local_cql_port,a.memcached_host_port,a.client_cpus,barrier_env)
                transcript=(directory/'launcher.log').open('w')
                sample('running',True)
                record['workload_start_unix_seconds']=time.time()
                workload=subprocess.Popen(actual,stdout=transcript,stderr=subprocess.STDOUT,start_new_session=True,cwd=REPO,env=client_env)
                deadline=time.monotonic()+a.timeout; nextq=time.monotonic()+5; nextprint=time.monotonic()+15
                while workload.poll() is None:
                    now=time.monotonic(); row=sample('running',now>=nextq)
                    if now>=nextq: nextq=time.monotonic()+5
                    if now>=nextprint:
                        print(f"{case} t={row['seconds']:.1f} retired={row['retired_bytes']/MIB:.1f} MiB rss={row['application_rss_bytes']/MIB:.1f} MiB",flush=True); nextprint=now+15
                    base.require(now<deadline,'application timeout')
                    time.sleep(a.sample_seconds)
                record['workload_end_unix_seconds']=time.time()
                record['exit_code']=workload.returncode; record['end']=sample('running',True)
                record['summary']=summarize(record['samples'],total)
                record['summary']['window_complete']=True
                record['checks']['application_exit_zero']=workload.returncode==0
                if cpu_profile:
                    record['cpu_affinity']={'profile':cpu_profile,
                        'host_end':affinity.verify(host['pid'],q('query-cpus-fast'),cpu_profile)}
                    observed=[p for row in record['samples'] if row['phase']=='running' for p in row['applications']]
                    record['checks']['guest_application_affinity']=case=='idle' or bool(observed) and all(
                        set(mask)<=set(cpu_profile['guest_application_cpus'])
                        for p in observed for mask in p['thread_cpu_masks'])
                    base.require(record['checks']['guest_application_affinity'],'Guest application CPU affinity missing or outside requested CPUs')
                record['status']='PASS' if workload.returncode==0 else 'FAIL'
            except Exception as e:
                record.update(status='FAIL',error=repr(e))
            finally:
                if workload and workload.poll() is None:
                    os.killpg(workload.pid,signal.SIGTERM)
                    try: workload.wait(timeout=30)
                    except subprocess.TimeoutExpired: record['workload_stop_error']='launcher still alive'
                if workload and 'workload_end_unix_seconds' not in record and workload.poll() is not None:
                    record['workload_end_unix_seconds']=time.time()
                if transcript: transcript.close()
                try:
                    control('manager','disable'); control('policy','disable'); control('tracker','disable')
                    control('manager','putback'); control('manager','clear_target'); control('policy','clear_target'); control('shadow','drain')
                    # Process exit can leave queued zap/FORGET work after
                    # policy disable. Observe completion rather than treating
                    # the first asynchronous cleanup snapshot as a leak.
                    drain_deadline=time.monotonic()+120
                    while True:
                        record['restored']=sample('restored',True)
                        if resources_empty(record['restored']) or time.monotonic()>=drain_deadline: break
                        time.sleep(a.sample_seconds)
                    r=record['restored']; b=record['baseline']
                    if a.mode=='all-local' and 'end' in record:
                        if a.all_local_tracking:
                            record['checks']['all_local_tracking_active']=all_local_tracking_active(b,record['end'])
                        else:
                            record['checks']['all_local_controls_inactive']=all_local_inactive(b,record['end'])
                    if (a.mode=='tracking-only' or (a.mode=='all-local' and a.all_local_tracking)) and 'end' in record:
                        e=record['end']
                        record['checks']['tracking_enabled']=all(v[g]['enabled']==expected for v in record['samples'] if v['phase'] in ['baseline','running'] for g,expected in [('tracker',tracking_pebs),('manager',tracking_hhh)])
                        record['checks']['tracking_parameters_match']=all(v['tracker']['sample_period']==a.sample_period and v['tracker']['cooling_samples']==a.cooling_samples and v['manager']['maintenance_interval_ms']==a.hhh_interval_ms and v['tracker']['period_mismatches']==0 for v in record['samples'] if v['phase'] in ['baseline','running'])
                        record['checks']['sampling_observed']=(e['tracker']['hardware_samples']>b['tracker']['hardware_samples'] and e['tracker']['processed_samples']>b['tracker']['processed_samples']) if tracking_pebs else all(e['tracker'][k]==b['tracker'][k] for k in ['hardware_samples','processed_samples'])
                        record['checks']['maintenance_observed']=(e['manager']['epochs']>b['manager']['epochs'] and any(e['manager'][k]>b['manager'][k] for k in ['scanned','aged','promoted'])) if tracking_hhh else e['manager']['epochs']==b['manager']['epochs']
                        if a.mode=='all-local':
                            record['sampling_observed']=record['checks'].pop('sampling_observed')
                            record['maintenance_observed']=record['checks'].pop('maintenance_observed')
                        record['hhh_conversion_scan_observed']=e['manager']['scanned']>b['manager']['scanned']
                        record['checks']['no_reclamation']=all(v['policy']['enabled']==0 and v['policy']['lease_active']==0 and v['retired_bytes']==0 for v in record['samples']) and all(e['rdma'][k]==b['rdma'][k] for k in ['write_bytes','read_bytes'])
                    record['checks']['resources_empty']=resources_empty(r)
                    if not a.performance_mode:
                        record['checks']['no_transport_or_data_error']=all(r[g][k]==b[g][k] for g,keys in {'shadow':['data_save_failure','data_load_failure','demand_fault_failures'],'rdma':['map_failures','transfer_errors'],'policy':['psi_errors','action_errors']}.items() for k in keys)
                    record['counter_delta']={g:{k:r[g][k]-v for k,v in b[g].items() if isinstance(v,int) and isinstance(r[g].get(k),int)} for g in base.skew.PATHS}
                    record['reclamation_observed']=record.get('summary',{}).get('peak_retired_bytes',0)>0 or record.get('summary',{}).get('peak_free_reclaimed_bytes',0)>0
                    record['free_reclamation_observed']=record.get('summary',{}).get('peak_free_reclaimed_bytes',0)>0
                    record['physical_rdma_io_observed']=record['counter_delta']['rdma']['write_completions']>0
                    if not all(record['checks'].values()): record['status']='FAIL'
                    base.require(record['checks']['resources_empty'],'resource cleanup incomplete')
                except Exception as e:
                    record.update(status='FAIL',cleanup_error=repr(e))
                with serial.open('rb') as f: f.seek(offset); (directory/'qemu-events.log').write_bytes(f.read())
                save(directory/'report.json',record); save(out/'report.json',report)
            print(case+' '+record['status']+' '+json.dumps(record.get('summary',{})),flush=True)
            if record.get('cleanup_error'): raise RuntimeError('Stop suite after cleanup failure')
            if case=='cassandra': service('cassandra','stop')
            if case=='memcached': service('memcached','stop')
            if case not in ['idle','cassandra','memcached']:
                guest.transfer(access,'download',remote+'/'+case,directory/'application',recursive=True)
        report['status']='PASS' if all(r['status']=='PASS' for r in report['cases'].values()) else 'FAIL'
    except (Exception,KeyboardInterrupt) as e:
        report.update(status='FAIL',error=repr(e))
    finally:
        errors=[]
        if changed and before:
            if original_hhh_interval is not None:
                try: remote_run(['sudo','-n','sh','-c','echo '+str(int(original_hhh_interval))+' > /sys/kernel/debug/chameleon_mm/maintenance_interval_ms'])
                except Exception as e: errors.append('HHH interval: '+repr(e))
            try: report['restoration']=live.remote_python(access,live.RESTORE,json.dumps(before))
            except Exception as e: errors.append('settings: '+repr(e))
            try:
                s=qbefore['chameleon']; q('chameleon-configure',{'config':{k:s[k] for k in ['ept-mode','batch-pages','watermark-bytes']}})
            except Exception as e: errors.append('QMP: '+repr(e))
            for name,was_running in initial_services.items():
                if was_running:
                    try: service(name,'start')
                    except Exception as e: errors.append(name+': '+repr(e))
            try:
                after=live.snapshot(access); report['after']={'guest':after,'rdma':base.inventory(access),'host':base.host_identity(access),'qmp':q('query-llfree-balloon')}
                old=before['dmesg'].splitlines(); new=after['dmesg'].splitlines()
                log='\n'.join(new[len(old):]) if new[:len(old)]==old else after['dmesg']
                (out/'guest-dmesg-new.log').write_text(log+'\n')
                if re.search(base.skew.BAD,log): errors.append('new Guest kernel diagnostic')
                if report['after']['rdma']['boot_id']!=rdma['boot_id'] or report['after']['host']['pid']!=host['pid']: errors.append('VM identity changed')
            except Exception as e: errors.append('final evidence: '+repr(e))
        if conn:
            try: conn.stdin.close(); conn.wait(timeout=20)
            except Exception as e: errors.append('SSH close: '+repr(e))
        if affinity_before is not None:
            try:
                affinity.restore(host['pid'],affinity_before)
                report['cpu_affinity']['host_restored']=affinity.snapshot(host['pid'])
            except Exception as e: errors.append('Host CPU affinity restoration: '+repr(e))
        if lock: lock.__exit__(None,None,None)
        if original_sigterm is not None: signal.signal(signal.SIGTERM,original_sigterm)
        if errors: report.update(status='FAIL',cleanup_errors=errors)
        save(out/'report.json',report)
        print('RESULT '+str(out/'report.json'),flush=True)
    return 0 if report['status']=='PASS' else 1

if __name__=='__main__': raise SystemExit(main())
