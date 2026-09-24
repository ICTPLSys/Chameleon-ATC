#!/usr/bin/env python3
"""Targeted ordinary-memory mechanisms on the existing physical RDMA Guest.

Scenarios exercise HHH split/mixed retirement, EPT batching, hotspot migration,
and a low-pressure / uniform-demand / low-pressure PSI wave. No injected heat,
PFNs or PSI fixture is used. The live VM and backend are kept running.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('physical_micro_base', ROOT / 'scripts/test-physical-skew.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
live, skew, guest, vm = base.live, base.skew, base.guest, base.vm
require, fields, command = base.require, base.fields, base.command
MIB, GIB = base.MIB, base.GIB
inventory, host_identity = base.inventory, base.host_identity
select_physical_rdma, observe_capacity, preflight = base.select_physical_rdma, base.observe_capacity, base.preflight

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', default='guest-tools-final', help='existing guestctl VM identity')
    parser.add_argument('--access', type=Path)
    parser.add_argument('--name', '--run-id', dest='name', default='physical-micro-' + time.strftime('%Y%m%d-%H%M%S'),
                        help='unique experiment/result name; unrelated to the existing VM identity')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--remote-dir', help='new dedicated Guest directory; retained as evidence')
    parser.add_argument('--binary', type=Path, default=ROOT / 'tests/chameleon_skew')
    parser.add_argument('--rdma-device', help='active Guest mlx5 device; auto-detect only when unique')
    parser.add_argument('--netdev', help='required physical interface for the server IP route')
    for name, default in [('gib',16), ('guest-mib',24576), ('pool-mib',8192), ('threads',4),
                          ('hot-access-ppm',None), ('sampling-period',8192), ('cold-folios',16),
                          ('warmup-seconds',30), ('run-seconds',180), ('reclaim-mib',4096),
                          ('reclaim-timeout',360), ('verify-timeout',1800), ('sample-seconds',5),
                          ('psi-ppm',1000000)]:
        parser.add_argument('--' + name, type=int, default=default)
    parser.add_argument('--scenario', choices=('split','batch','psi','hotspot'), required=True)
    parser.add_argument('--pattern', choices=('contiguous','striped','uniform','sequential'))
    parser.add_argument('--stripe-hot-pages', type=int, default=64)
    parser.add_argument('--hot-percent', type=float, default=1,
                        help='contiguous hot-region size as a percentage of each worker partition')
    parser.add_argument('--cooling-samples', type=int, default=16777216)
    parser.add_argument('--split-warm-seconds', type=int, default=60)
    parser.add_argument('--min-splits', type=int, default=16)
    parser.add_argument('--ept-mode', choices=('deferred','immediate'), default='deferred')
    parser.add_argument('--ept-batch-pages', type=int, default=512)
    parser.add_argument('--serial-log', type=Path)
    args = parser.parse_args()
    args.pattern = args.pattern or ('striped' if args.scenario == 'split' else 'contiguous')
    if args.hot_access_ppm is None:
        args.hot_access_ppm = 1000000 if args.scenario in ('psi','hotspot') else 999990
    require(1 <= args.stripe_hot_pages <= 512 and args.cooling_samples > 0 and
            args.ept_batch_pages > 0 and args.min_splits > 0 and args.split_warm_seconds > 0,
            'invalid mechanism parameters')
    require(0 < args.hot_percent <= 100,'hot region must be in (0,100] percent')
    require(args.scenario != 'psi' or 0 < args.psi_ppm < 1000000,
            'PSI wave requires a fixed nonzero threshold below 100 percent')
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
    serial_path = args.serial_log or ROOT / 'build/guests' / args.vm / 'serial.log'
    serial_offset = serial_path.stat().st_size
    serial_inode = serial_path.stat().st_ino
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
        require(not check_workload or not re.search(r'SKEW_ERROR|SKEW_COMMAND_ERROR|SKEW_FAIL|SKEW_CORRUPTION|SKEW_LEDGER_MISMATCH|status=FAIL|"(?:verify_)?errors":\s*[1-9]|SKEW_EXIT=[1-9]',last_log),
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
        qmp.execute('chameleon-configure',{'config':{'ept-mode':args.ept_mode,'batch-pages':args.ept_batch_pages,'watermark-bytes':0,
            'begin-fail-count':0,'install-fail-count':0,'dma-map-fail-count':0,'discard-fail-index':-1}})
        console.command('echo never > /sys/kernel/mm/transparent_hugepage/defrag && '
            'for f in /sys/kernel/mm/transparent_hugepage/hugepages-*kB/enabled; do echo never > "$f" || exit; done && '
            'echo always > /sys/kernel/mm/transparent_hugepage/hugepages-2048kB/enabled && '
            'echo 600000 > /sys/kernel/mm/transparent_hugepage/khugepaged/scan_sleep_millisecs')
        console.command('mkfifo '+q(fifo)+' || exit; ('+q(remote_binary)+' --gib '+str(args.gib)+' --threads '+str(args.threads)+
            ' --hot-access-ppm '+str(args.hot_access_ppm)+' --pattern '+args.pattern+
            ' --stripe-hot-pages '+str(args.stripe_hot_pages)+' --hot-percent '+str(args.hot_percent)+
            ' < '+q(fifo)+' >> '+q(remote_log)+
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
        require(int(actual['hot_access_ppm'])==args.hot_access_ppm and float(actual['hot_percent'])==args.hot_percent and
                int(actual['write_percent'])==10 and int(actual['seed'])==1,'actual workload distribution differs')
        require(actual.get('pattern') == args.pattern, 'actual workload pattern differs')
        report['workload']={'pattern':args.pattern,'pid':pid,'address':address,'bytes':size,'threads':threads,
                            'hot_pages_percent':args.stripe_hot_pages/512*100 if args.pattern=='striped' else args.hot_percent,
                            'hot_access_ppm':args.hot_access_ppm,'write_percent':10,'actual_ready':actual}
        ram=args.guest_mib*MIB; target=args.reclaim_mib*MIB
        # A persistent Guest may have tracking enabled before the test.
        command(console,'tracker','disable')
        for value in ['reset',f'capacity {ram} {ram}',f'sampling fixed {args.sampling_period}',f'cooling fixed {args.cooling_samples}','enable']:
            command(console,'tracker',value)
        for value in [f'target {pid} {address} {size}','split_mode hhh','selector mixed_cost','batch 32 6400 128 4 10']:
            command(console,'manager',value)
        for order in [0]+list(range(2,10)): command(console,'manager',f'cost {order} {1<<order}')
        phase='warmup'
        work(f'warmup {args.warmup_seconds}',r'^SKEW_DONE phase=warmup status=PASS\b',args.warmup_seconds+120)
        report['phases']['baseline']=baseline=sample('baseline',True)
        require(baseline['tracker']['hardware_samples']>0 and baseline['tracker']['accepted_samples']>0 and
                not baseline['tracker']['synthetic_samples'],'hotness must use actual hardware records')
        require(baseline['compute_memory']['Rss']>0.9*size,'compute backing must initially cover the complete dataset')
        command(console,'manager','enable')
        if args.scenario == 'split':
            phase='split_warm'
            work(f'run {args.split_warm_seconds}',r'^SKEW_DONE phase=run status=PASS\b',args.split_warm_seconds+300)
            report['phases']['split_warm']=sample('split_warm_done',True)
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
        report['phases']['active_reclaim_end']=current
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
        qmp.execute('chameleon-configure',{'config':{'batch-pages':args.ept_batch_pages}})
        phase='shifted_hotspot'
        if args.scenario == 'psi':
            console.command("echo 'pattern uniform' >&3")
        else:
            console.command("echo 'shift 50' >&3")
        work(f'run {args.run_seconds}',r'^SKEW_DONE phase=run status=PASS\b',args.run_seconds+300)
        report['phases']['shifted']=shifted=sample('shifted',True)
        report['checks']['demand_readback']=(shifted['shadow']['load_demand_attempts']>retired['shadow']['load_demand_attempts'] and
            shifted['shadow']['data_fault_restores']>retired['shadow']['data_fault_restores'] and
            shifted['hermit']['bytes_read']>retired['hermit']['bytes_read'])
        report['checks']['sampling_shifted']=shifted['tracker']['hardware_samples']>retired['tracker']['hardware_samples']
        if args.scenario == 'psi':
            # The application stops between commands. At a 10 ms policy
            # period, reclamation can finish before the next 5 s poll. Keep
            # the last actual uniform-progress observation as the left
            # bracket of that transition, rather than requiring additional
            # reclamation after the capacity floor has already been reached.
            pressure_progress=[s for s in report['samples'] if s['phase']=='shifted_hotspot'
                and s.get('workload_event',{}).get('event')=='progress'
                and s['workload_event'].get('pattern')=='uniform']
            require(pressure_progress,'pressure phase needs an actual running uniform-access sample')
            pressure_end=pressure_progress[-1]
            report['phases']['pressure_active_end']=pressure_end
            report['pressure_transition']={
                'last_active_seconds':pressure_end['seconds'],
                'first_quiescent_seconds':shifted['seconds'],
                'scope':'Observed bracket includes the end of uniform access and the command-switch pause.'}
            phase='cooled_hotspot'
            console.command("echo 'pattern contiguous' >&3; echo 'shift 0' >&3")
            work(f'run {args.run_seconds}',r'^SKEW_DONE phase=run status=PASS\b',args.run_seconds+300)
            report['phases']['cooled']=cooled=sample('cooled',True)
            report['checks']['psi_high_response']=(
                shifted['policy']['high_epochs'] > retired['policy']['high_epochs'] and
                shifted['shadow']['load_background_attempts'] > retired['shadow']['load_background_attempts'] and
                shifted['policy']['shadow_restored_pages'] > retired['policy']['shadow_restored_pages'] and
                shifted['shadow']['psi_fault_ns'] > retired['shadow']['psi_fault_ns'])
            report['checks']['psi_low_resume']=(
                cooled['policy']['low_epochs'] > shifted['policy']['low_epochs'] and
                cooled['policy']['shadow_prepared_pages'] > pressure_end['policy']['shadow_prepared_pages'] and
                cooled['shadow']['host_reclaimed_pages'] > pressure_end['shadow']['host_reclaimed_pages'] and
                any(s['phase']=='cooled_hotspot' and s.get('workload_event',{}).get('event')=='progress' and
                    s['shadow']['host_reclaimed_pages'] > 0 for s in report['samples']))
            report['checks']['psi_threshold_fixed']=all(
                s['policy']['threshold_ppm']==args.psi_ppm for s in report['samples']
                if s['policy']['enabled'])
            report['checks']['psi_actual_patterns']=all(any(
                s['phase']==label and s.get('workload_event',{}).get('event')=='progress' and
                s['workload_event'].get('pattern')==pattern for s in report['samples'])
                for label,pattern in [('skew_reclaim','contiguous'),('shifted_hotspot','uniform'),
                                      ('cooled_hotspot','contiguous')])
        elif args.scenario == 'hotspot':
            phase='returned_hotspot'
            console.command("echo 'shift 0' >&3")
            work(f'run {args.run_seconds}',r'^SKEW_DONE phase=run status=PASS\b',args.run_seconds+300)
            report['phases']['returned']=returned=sample('returned',True)
            report['checks']['repeat_hotspot_demand']=(returned['shadow']['load_demand_attempts'] > shifted['shadow']['load_demand_attempts'])
        last_active=report['phases'].get('cooled',report['phases'].get('returned',shifted))
        phase='policy_restore'; command(console,'manager','disable')
        command(console,'policy','disable',timeout=args.verify_timeout)
        report['phases']['policy_restored']=policy_restored=sample('policy_restored',True)
        report['restoration']={'demand_phase_load_attempts':shifted['shadow']['load_demand_attempts']-retired['shadow']['load_demand_attempts'],
            'shift_phase_background_load_attempts':shifted['shadow']['load_background_attempts']-retired['shadow']['load_background_attempts'],
            'shift_phase_total_bytes_read':shifted['hermit']['bytes_read']-retired['hermit']['bytes_read'],
            'disable_background_load_attempts':policy_restored['shadow']['load_background_attempts']-last_active['shadow']['load_background_attempts'],
            'disable_bytes_read':policy_restored['hermit']['bytes_read']-last_active['hermit']['bytes_read']}
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
        report['checks']['no_data_or_policy_failures']=all(
            restored[group][key]==baseline[group][key]
            for group,keys in [('shadow',('data_save_failure','data_load_failure','demand_fault_failures')),
                               ('policy',('psi_errors','action_errors')),
                               ('rdma',('map_failures','transfer_errors'))]
            for key in keys)
        capacity=report['capacity_observations']
        report['checks']['capacity_floor']=(capacity['lease_active_samples']>0 and not capacity['floor_violations'])
        report['summary']['minimum_local_bytes']=capacity['minimum_local_bytes']
        report['summary']['minimum_leased_local_bytes']=capacity['minimum_leased_local_bytes']
        if args.scenario == 'split':
            report['checks']['hhh_physical_splits']=(
                restored['manager']['split_ok']-baseline['manager']['split_ok'] >= args.min_splits and
                restored['manager']['memtis_splits']==baseline['manager']['memtis_splits'])
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
        evidence_errors=[]
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
        try:
            require(serial_path.stat().st_ino == serial_inode and
                    serial_path.stat().st_size >= serial_offset,
                    'QEMU serial log was rotated or truncated during the experiment')
            with serial_path.open('rb') as stream:
                stream.seek(serial_offset)
                transcript = stream.read().decode(errors='replace')
            (output/'qemu-events.log').write_text(transcript)
            if 'restored' in report['phases']:
                analyzer = base.module('physical_micro_analysis','analyze-physical-micro.py')
                evidence = analyzer.analyze(report, transcript)
                (output/'mechanism-evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
                report['mechanism_evidence'] = 'mechanism-evidence.json'
                report['checks']['transaction_evidence_complete']=evidence['status']=='PASS'
                if args.scenario == 'split':
                    report['checks']['split_small_objects_retired']=evidence['retirement']['sub_2mib_objects']>0
                if args.scenario == 'batch':
                    ept=evidence['ept']
                    report['checks']['ept_mode_behavior']=(ept['begin_count']>0 and
                        (ept['maximum_ranges']==1 if args.ept_mode=='immediate'
                         else ept['multi_range_begin_count']>0))
                if not evidence['status']=='PASS':
                    evidence_errors.extend(evidence.get('errors') or ['transaction evidence incomplete'])
                if not all(report['checks'].values()):
                    report['status']='FAIL'
        except Exception as error:
            evidence_errors.append('transaction mechanism evidence: '+repr(error))
        if qmp:
            qmp.file.close(); qmp.sock.close()
        if lock: lock.__exit__(None,None,None)
        if errors: report.update(status='FAIL',cleanup_errors=errors)
        if evidence_errors: report.update(status='FAIL',evidence_errors=evidence_errors)
        save()
        print(json.dumps({k:report[k] for k in ('status','summary','failed_phase','error','cleanup_errors') if k in report},indent=2),flush=True)
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
