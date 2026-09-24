#!/usr/bin/env python3
"""Run each application in a fresh VM using recorded, approved memory sizes.
Use --reprofile only when new all-local peak+2GiB sizing is requested.

Uses the existing disk/VF, graceful QEMU shutdown, and the existing RDMA server.
Restores the original VM memory/configuration and benchmark services at exit.
"""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('apps',HERE/'run-chameleon-apps.py')
app=importlib.util.module_from_spec(spec); spec.loader.exec_module(app)


def valid_run_name(name,cases,tracking_calibration=False):
    # The leaf runner permits 70 characters; reserve the actual case/mode
    # suffix and the three calibration repeats rather than an arbitrary 35.
    suffix=max(len('-'+case+'-local') for case in cases)+(3 if tracking_calibration else 0)
    return bool(re.fullmatch(r'[A-Za-z0-9_-]+',name)) and len(name)+suffix<=70


def sizing(case, report, directory):
    if report['configuration']['mode']!='all-local':
        raise ValueError('Sizing requires an all-local run')
    record=report['cases'][case]
    active=[r for r in record['samples'] if r['phase']=='running']
    if not active or any(r['retired_bytes'] or r['policy']['enabled'] for r in active):
        raise ValueError('Profiling must have reclamation disabled and no retired backing')
    if record.get('exit_code')!=0 and case!='memcached':
        raise ValueError('Application profiling did not complete successfully')
    if case=='memcached' and record.get('exit_code')!=0:
        import csv
        rows=[r for f in (directory/'application').rglob('generator.csv') for r in csv.DictReader(f.open())]
        if not rows or any(r['schedule_complete']!='1' for r in rows):
            raise ValueError('Incomplete Memcached schedule cannot size the VM')
    sources=[{'kind':'sampled process VmHWM','bytes':max(r.get('application_hwm_bytes',r['application_rss_bytes']) for r in active)}]
    # Batch launchers record GNU time for the actual Guest application, not SSH.
    if case not in ['cassandra','memcached']:
        for f in (directory/'application').rglob('*time.txt'):
            for n in re.findall(r'Maximum resident set size \(kbytes\):\s*(\d+)',f.read_text()):
                sources.append({'kind':'Guest GNU time max RSS','file':str(f),'bytes':int(n)*1024})
    peak=max(s['bytes'] for s in sources)
    if peak<=0: raise ValueError('No application peak memory evidence')
    return {'case':case,'method':'max(all-local process VmHWM, Guest GNU time max RSS) + 2048 MiB; rounded up to 2 MiB',
            'peak_application_bytes':peak,'headroom_mib':2048,
            'memory_mib':math.ceil((peak/app.MIB+2048)/2)*2,
            'baseline_report':str(directory.parent/'report.json'),
            'baseline_status':record['status'],
            'scope':'Observed workload peak, not configured cache/heap limit. Failed load validity makes sizing provisional.',
            'sources':sources}


def within_target(peak_bytes,target_gib,tolerance_percent):
    # Compare byte quantities so an exact +10% boundary is not rejected due
    # to floating-point division (e.g. 22 / 20 - 1 > 0.1).
    target_bytes=target_gib*app.GIB
    return abs(peak_bytes-target_bytes)*100 <= target_bytes*tolerance_percent


def fixed_plan(config, case):
    plan=dict(config['applications'][case])
    if plan['case']!=case or not isinstance(plan['memory_mib'],int) or plan['memory_mib']<=2048 or plan['memory_mib']%2:
        raise ValueError('Invalid fixed memory plan for '+case)
    return dict(plan,allocation_mode='fixed-user-approved')


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name',required=True)
    p.add_argument('--vm',default='guest-tools-final')
    p.add_argument('--cases',nargs='+',choices=app.APPS,default=app.APPS)
    p.add_argument('--profile',choices=['table2','small'],default='table2',help='Workload and VM defaults (default: table2); small selects legacy inputs/sizes')
    p.add_argument('--profile-memory-mib',type=int,help='Calibration VM MiB (table2: 65536; small: 24576)')
    p.add_argument('--workload-config',type=Path,help='Override the selected profile workload JSON')
    p.add_argument('--profile-only',action='store_true',help='Measure all-local peaks only; write plans without Chameleon runs')
    p.add_argument('--timeout',type=int,default=14400)
    p.add_argument('--sample-seconds',type=float,default=2)
    p.add_argument('--memory-config',type=Path,help='Override the selected profile fixed VM sizes')
    p.add_argument('--reprofile',action='store_true',help='Explicitly measure new all-local peak sizes instead of the fixed table')
    p.add_argument('--free-pages',type=int,default=512)
    p.add_argument('--run-mode',choices=['chameleon','all-local','tracking-only'],default='chameleon')
    p.add_argument('--vm-memory-mib',type=int,help='Explicit tuning VM size; keeps measured footprint as provenance')
    p.add_argument('--psi-ppm',type=int,default=10000)
    p.add_argument('--epoch-us',type=int,default=10000)
    p.add_argument('--cold-folios',type=int,default=16)
    p.add_argument('--minimum-local-mib',type=int)
    p.add_argument('--pre-reclaim-headroom-mib',type=int)
    p.add_argument('--pre-reclaim-epoch-us',type=int)
    p.add_argument('--all-local-tracking',action=argparse.BooleanOptionalAction,default=True)
    p.add_argument('--cpu-pinning',action=argparse.BooleanOptionalAction,default=True)
    p.add_argument('--tracking-components',choices=['both','pebs','hhh'],default='both')
    p.add_argument('--tracking-calibration',action='store_true',help='Three alternating off/on pairs, fresh matching VM per execution; no reclamation')
    p.add_argument('--sample-period',type=int,default=65536)
    p.add_argument('--cooling-samples',type=int,default=131072)
    p.add_argument('--hhh-interval-ms',type=int,default=15000)
    a=p.parse_args(argv)
    if a.workload_config is None and a.profile=='table2':
        a.workload_config=app.REPO/'benchmarks/config/chameleon-table2-workloads.json'
    if a.memory_config is None:
        a.memory_config=app.REPO/'benchmarks/config'/('chameleon-table2-memory.json' if a.profile=='table2' else 'chameleon-app-memory.json')
    if a.profile_memory_mib is None: a.profile_memory_mib=65536 if a.profile=='table2' else 24576
    return p,a


def main():
    p,a=parse_args()
    if a.profile_only and not a.reprofile: p.error('--profile-only requires --reprofile')
    if a.vm_memory_mib is not None and (a.reprofile or a.vm_memory_mib<=2048 or a.vm_memory_mib%2): p.error('explicit VM size must be >2048, 2-MiB aligned, and cannot accompany reprofile')
    workload=json.loads(a.workload_config.read_text()) if a.workload_config else None
    if a.free_pages<0 or a.free_pages>2**32 or a.free_pages%512: p.error('invalid free-page batch')
    fixed=json.loads(a.memory_config.read_text()) if not a.reprofile else None
    if fixed:
        for case in a.cases:
            plan=fixed_plan(fixed,case)
            if workload and (plan.get('workload_args')!=workload['applications'][case]['args'] or plan.get('workload_service_args',[])!=workload['applications'][case].get('service_args',[])):
                p.error('Memory evidence does not match workload parameters for '+case)
    if not valid_run_name(a.name,a.cases,a.tracking_calibration): p.error('invalid name or generated leaf name exceeds 70 characters')
    out=app.REPO/'benchmarks/results/chameleon'/a.name;out.mkdir(parents=True,exist_ok=False)
    access=app.guest.load_access(app.HA/'build/guests'/a.vm/'access.json')
    cfgpath=Path(access['vm_config']);original=cfgpath.read_text();cfg=json.loads(original)
    report={'status':'RUNNING','cases':{},'original_memory_mib':cfg['memory_mib'],
            'metric':'Paper section 6: 1 - arithmetic_mean(QEMU RSS)/configured VM memory',
            'configuration':{k:str(v) if isinstance(v,Path) else v for k,v in vars(a).items()},'restarts':[]}
    app.save(out/'report.json',report);(out/'original-vm.json').write_text(original)
    def remote(args,timeout=300):
        r=subprocess.run(app.guest.ssh_command(access,args),capture_output=True,text=True,timeout=timeout)
        if r.returncode: raise RuntimeError('Guest command failed: '+r.stderr[-1500:]+r.stdout[-1500:])
        return r.stdout
    service='/home/'+access['user']+'/chameleon-benchmarks/scripts/'
    helper='/home/'+access['user']+'/chameleon-tools/hermit-guest.py'
    services={};changed=False
    def restart(memory,restore=False,case=None):
        nonlocal changed
        # Leaf measurements acquire this same lifecycle lock independently.
        with app.guest.control_lock(access):
            changed=True
            if app.guest.active(access):
                for n in ['cassandra','memcached']: remote([service+n+'-guest.sh','stop'])
                remote(['sudo','-n','python3',helper,'stop'])
                app.guest.stop(access,timeout=180)
            changed=True
            new=dict(cfg,memory_mib=memory)
            if workload and case: new['cpus']=workload['applications'][case]['vcpus']
            cfgpath.write_text(original if restore else json.dumps(new,indent=2)+'\n')
            app.guest.start(access,timeout=300);app.guest.wait_ssh(access,300)
            app.guest.transfer(access,'upload',app.HA/'scripts/hermit-guest.py',helper)
            remote(['sudo','-n','sh','-c',"echo 'batch 32 6400 128 4 10' > /sys/kernel/debug/chameleon_mm/control; for o in 0 2 3 4 5 6 7 8 9; do echo \"cost $o $((1 << o))\" > /sys/kernel/debug/chameleon_mm/control; done"])
            connection=json.loads(remote(['sudo','-n','python3',helper,'start','--server','192.0.2.1','--interface','ibp1s0','--port','9400','--pool-mib','8192']))
            # Keep Guest startup checks compatible with the measured VM sizes.
            app.guest.transfer(access,'upload',HERE.parent/'patches/graph500-csr-cache.patch',service+'graph500-csr-cache.patch')
            for launcher in ['run-xsbench.sh','run-liblinear.sh','run-graph500.sh','run-graphchi.sh','run-spark-kmeans.sh','run-pvc.sh','memcached-guest.sh','memcached-warmup.py','cassandra-guest.sh']:
                app.guest.transfer(access,'upload',HERE/launcher,service+launcher)
                remote(['chmod','+x',service+launcher])
            state={'memory_mib':memory,'boot_id':remote(['cat','/proc/sys/kernel/random/boot_id']).strip(),
                   'qemu_pid':app.base.host_identity(access)['pid'],'restoring_original':restore,
                   'hermit_connection':connection}
            report['restarts'].append(state);app.save(out/'report.json',report)
            print('FRESH_VM '+json.dumps(state),flush=True)
    def run(case,mode,plan=None,tag=''):
        name=a.name+'-'+case+('-local' if mode=='all-local' else '-on')+tag
        argv=[sys.executable,str(HERE/'run-chameleon-apps.py'),'--name',name,'--vm',a.vm,'--cases',case,'--mode',mode,'--free-pages',str(a.free_pages)]
        argv+=['--timeout',str(a.timeout),'--sample-seconds',str(a.sample_seconds)]
        argv+=['--cpu-pinning' if a.cpu_pinning else '--no-cpu-pinning']
        if a.tracking_calibration: argv+=['--drop-caches','--no-all-local-tracking']
        elif not a.all_local_tracking: argv+=['--no-all-local-tracking']
        argv+=['--profile',a.profile,'--psi-ppm',str(a.psi_ppm),'--epoch-us',str(a.epoch_us),'--cold-folios',str(a.cold_folios)]
        argv+=['--tracking-components',a.tracking_components]
        argv+=['--sample-period',str(a.sample_period),'--cooling-samples',str(a.cooling_samples),'--hhh-interval-ms',str(a.hhh_interval_ms)]
        if a.minimum_local_mib is not None: argv+=['--minimum-local-mib',str(a.minimum_local_mib)]
        if a.pre_reclaim_headroom_mib is not None: argv+=['--pre-reclaim-headroom-mib',str(a.pre_reclaim_headroom_mib)]
        if a.pre_reclaim_epoch_us is not None: argv+=['--pre-reclaim-epoch-us',str(a.pre_reclaim_epoch_us)]
        if a.workload_config: argv+=['--workload-config',str(a.workload_config)]
        if plan:
            memory=json.loads(plan.read_text())['memory_mib']
            argv+=['--memory-plan',str(plan)]
            if a.minimum_local_mib is None:
                argv+=['--max-reclaim-mib',str(min(6144,memory-2048))]
        with (out/(case+'-'+mode+tag+'.log')).open('w') as log:
            rc=subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT).returncode
        d=app.REPO/'benchmarks/results/chameleon'/name;r=json.loads((d/'report.json').read_text())
        if r.get('cleanup_errors') or not r.get('after') or not r['cases'].get(case,{}).get('checks',{}).get('resources_empty'):
            raise RuntimeError('Run infrastructure/cleanup failure: '+str(d/'report.json'))
        print('FINISHED '+case+' '+mode+' '+r['status'],flush=True)
        return d,r
    try:
        # Exercise the configured binary's NUMA backend before stopping the VM.
        probe=[cfg['qemu'],'-machine','none','-display','none','-nodefaults','-S',
               '-object',json.dumps({'qom-type':'memory-backend-ram','id':'numa-probe','size':2*app.MIB,'host-nodes':[cfg['host_numa_node']],'policy':'bind'}),'-qmp','stdio']
        checked=subprocess.run(probe,input='{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n',text=True,capture_output=True,timeout=15)
        if checked.returncode: raise RuntimeError('QEMU NUMA preflight: '+checked.stderr)
        before=app.live.snapshot(access)
        rdma=app.base.inventory(access);q=app.guest.qmp(access,'query-llfree-balloon')
        app.base.preflight(before,q,rdma,app.SimpleNamespace(pool_mib=8192,guest_mib=cfg['memory_mib']))
        # Update lifecycle helpers before trusting PID files or stopping services.
        for name in ['cassandra','memcached']:
            launcher=name+'-guest.sh'
            app.guest.transfer(access,'upload',HERE/launcher,service+launcher)
            remote(['chmod','+x',service+launcher])
        # Save the original service liveness without starting/stopping anything.
        for n in ['cassandra','memcached']:
            text=remote(['sh','-c',app.shlex.quote(service+n+'-guest.sh')+' is-running && echo RUNNING || true'])
            services[n]='RUNNING' in text
        report['original_services']=services
        for case in a.cases:
            entry=report['cases'][case]={'status':'RUNNING'};app.save(out/'report.json',report)
            if a.reprofile:
                restart(a.profile_memory_mib,case=case)
                directory,baseline=run(case,'all-local')
                entry['baseline_report']=str(directory/'report.json')
                app.save(out/'report.json',report)
                plan=sizing(case,baseline,directory/case)
                if workload:
                    measured=baseline['cases'][case]['workload_configuration']
                    plan['workload_args']=measured['args']
                    plan['workload_service_args']=measured.get('service_args',[])
            else:
                plan=fixed_plan(fixed,case)
            if a.vm_memory_mib is not None:
                plan=dict(plan,measured_peak_headroom_memory_mib=plan['memory_mib'],memory_mib=a.vm_memory_mib,allocation_mode='explicit-tuning-capacity')
            planpath=out/(case+'-memory.json');app.save(planpath,plan)
            entry['memory_plan']=plan
            print('MEMORY_PLAN '+json.dumps(plan),flush=True)
            if a.profile_only:
                entry.update(status=baseline['status'],baseline_report=str(directory/'report.json'))
                if workload:
                    target=workload['applications'][case]['target_peak_gib']*app.GIB
                    entry['target_error_percent']=100*(plan['peak_application_bytes']/target-1)
                    entry['within_target']=within_target(plan['peak_application_bytes'],workload['applications'][case]['target_peak_gib'],workload.get('tolerance_percent',10))
                    if not entry['within_target']: entry['status']='ADJUST_REQUIRED'
                app.save(out/'report.json',report)
                continue
            restart(plan['memory_mib'],case=case)
            if a.tracking_calibration:
                def module(filename,name):
                    spec=importlib.util.spec_from_file_location(name,HERE/filename)
                    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
                metrics=module('chameleon-tuning-metrics.py','tracking_metrics')
                overhead=module('chameleon-tracking-overhead.py','tracking_overhead')
                entry['cache_protocol']='fresh matching VM for every off/on execution; sync + Guest drop_caches=3 outside timing'
                entry['fresh_vm_per_execution']=True
                entry['pairs']=[]
                for repeat in range(3):
                    pair={'repeat':repeat+1,'order':['all-local','tracking-only'] if repeat%2==0 else ['tracking-only','all-local']}
                    entry['pairs'].append(pair)
                    for mode in pair['order']:
                        if repeat or mode!=pair['order'][0]:restart(plan['memory_mib'],case=case)
                        directory,result=run(case,mode,planpath,'-r'+str(repeat+1))
                        trial=metrics.extract(directory,case)
                        raw=result['cases'][case];b,e=raw['baseline'],raw.get('end',raw['baseline'])
                        trial['tracking_counters']={g:{k:e[g][k]-b[g][k] for k in keys} for g,keys in {
                            'tracker':['hardware_samples','processed_samples','worker_cpu_ns','cooling_epochs','ring_drops'],
                            'manager':['epochs','scanned','split_ok','split_cpu_ns','selected','aged','promoted']}.items()}
                        trial['tracking_settings']=result['settings']
                        pair['off' if mode=='all-local' else 'on']=trial
                        app.save(out/'report.json',report)
                        if trial['status']!='PASS':raise RuntimeError('Invalid overhead trial: '+str(directory))
                    entry['overhead']=overhead.evaluate(entry['pairs'])
                    app.save(out/'report.json',report)
                    print('OVERHEAD '+case+' '+json.dumps(entry['overhead']),flush=True)
                entry['status']=entry['overhead']['status']
                app.save(out/'report.json',report)
                continue
            directory,result=run(case,a.run_mode,planpath)
            entry.update(status=result['status'],summary=result['cases'][case].get('summary'))
            entry['baseline_report' if a.run_mode=='all-local' else 'chameleon_report']=str(directory/'report.json')
            app.save(out/'report.json',report)
        report['status']='PASS' if all(r['status']=='PASS' for r in report['cases'].values()) else 'INCOMPLETE_OR_FAILED'
    except BaseException as e:
        report.update(status='FAIL',error=repr(e));print('FAILED '+repr(e),flush=True)
        for entry in report['cases'].values():
            if entry['status']=='RUNNING':
                entry.update(status='FAIL',error=repr(e))
    finally:
        if changed:
            try:
                restart(cfg['memory_mib'],restore=True)
                for n,was_running in services.items():
                    if was_running: remote([service+n+'-guest.sh','start'])
                report['restoration']='Original VM configuration, connected RDMA backend and service liveness restored; counters reset by reboot'
            except Exception as e: report.update(status='FAIL',restoration_error=repr(e))
        app.save(out/'report.json',report)
        print('RESULT '+str(out/'report.json'),flush=True)
    return 0 if report['status']=='PASS' else 1

if __name__=='__main__':sys.exit(main())
