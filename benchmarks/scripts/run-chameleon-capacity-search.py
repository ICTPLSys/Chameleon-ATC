#!/usr/bin/env python3
"""Explore larger VM capacities, then validate a fixed-capacity RDMA curve."""
import argparse
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_OUT = ROOT / 'benchmarks/results/chameleon/tuning-fig78-tracked-v4'
PLAN = ROOT / 'benchmarks/config/chameleon-tracked-search-v4.json'
spec = importlib.util.spec_from_file_location('metrics', HERE / 'chameleon-tuning-metrics.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
spec = importlib.util.spec_from_file_location('feedback', HERE / 'chameleon-search-feedback.py')
feedback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feedback)


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def fixed_baseline_id(case, memory_mib, plan):
    # A repaired preflight needs a fresh result ID while its failed trial is
    # retained. Scope that retry to one application, including normalization.
    app = plan.get('applications', {}).get(case, {})
    return 'b' + str(memory_mib) + app.get('baseline_suffix', plan.get('baseline_suffix', ''))


def parameters(app, template):
    floor = app['measured_peak_mib'] * template['local_peak_fraction'] + app['os_headroom_mib']
    return {**{k: template[k] for k in ['psi_ppm', 'epoch_us', 'cold_folios']},
            'minimum_local_mib': max(2048, math.ceil(floor / 2) * 2)}


def tracking_for(plan, params=None):
    profile = dict(plan['tracking'])
    for key in ('sample_period','cooling_samples','hhh_interval_ms'):
        if params and key in params:
            profile[key] = params[key]
    return profile


def tracking_suffix(plan, profile):
    return ('' if profile == plan['tracking'] else
            '-s%d-c%d-h%d' % tuple(profile[k] for k in ('sample_period','cooling_samples','hhh_interval_ms')))


def coverage_target_for(memory_mib, app, plan, params):
    # Older frozen points remain valid measurements after search targets change.
    # A missing template label must not prevent replay or fabricate coverage.
    return next((target for target, seed in feedback.coverage_seeds(memory_mib, app, plan)
                 if seed['minimum_local_mib'] == params['minimum_local_mib']), None)


def cached_tracking_baseline(entry, baseline_id, tracking):
    baseline=entry['trials'][baseline_id]
    expected={'sampling':tracking['sample_period'],'cooling':tracking['cooling_samples'],
              'hhh_interval_ms':tracking['hhh_interval_ms']}
    for tag,r in entry['trials'].items():
        if (r.get('mode')=='all-local' and r.get('status')=='PASS' and r.get('measurement_eligible',True) and
            r.get('memory_mib')==baseline.get('memory_mib') and r.get('tracking_profile')==expected and
            r.get('checks',{}).get('all_local_tracking_active') and
            all(r.get(k)==baseline.get(k) for k in ('baseline_protocol','workload_identity','workload_configuration','cpu_affinity_profile'))):
            return tag
    return None


def measured_search_round(entry, candidate_ids, reference, plan):
    rows=[entry['trials'][tag] for tag in candidate_ids]
    reports={r.get('baseline_report') for r in rows}
    if len(reports)!=1 or None in reports:return None
    baseline=next((tag for tag,r in entry['trials'].items() if r.get('report') in reports and
                   r.get('mode')=='all-local' and r.get('status')=='PASS' and
                   r.get('checks',{}).get('all_local_tracking_active')),None)
    if baseline is None:return None
    check=metrics.curve_check(rows,reference,check_trend=False,
        reference_tolerance_pp=plan.get('reference_tolerance_pp',0),include_all_local=plan.get('include_all_local_anchor',False),
        reclaim_range=entry.get('reclaim_range'))
    if check['status']!='PASS':return None
    return {'repeat':1,'baseline':baseline,'trial_ids':list(candidate_ids),'check':check,
            'stage':'initial measured search points; subsequent run uses frozen configurations'}


def reusable_verification_group(entry, candidate_ids):
    """Keep completed unchanged points when replacing one point of a curve.

    Select by configuration and baseline identity, never by best performance.
    A reused observation counts once and retains its original trial/report.
    """
    candidates={tag:entry['trials'][tag] for tag in candidate_ids}
    first=next(iter(candidates.values()))
    best=None
    for validation in reversed(entry.get('validation_sets',[])):
        for round_ in reversed(validation.get('rounds',[])):
            if round_.get('stage','').startswith('initial measured'):continue
            baseline_id=round_.get('baseline');baseline=entry['trials'].get(baseline_id,{})
            if (baseline.get('mode')!='all-local' or baseline.get('status')!='PASS' or not baseline.get('measurement_eligible',True) or
                not baseline.get('checks',{}).get('all_local_tracking_active') or
                baseline.get('memory_mib')!=first.get('memory_mib') or
                baseline.get('tracking_profile')!=first.get('baseline_tracking_profile') or
                any(baseline.get(k)!=first.get(k) for k in ('workload_identity','workload_configuration','baseline_protocol','cpu_affinity_profile')) or
                any(c.get('baseline_report')==baseline.get('report') for c in candidates.values())):
                continue
            reused={}
            for candidate,c in candidates.items():
                for tag in round_['trial_ids']:
                    r=entry['trials'][tag]
                    paired_report=(r.get('per_repeat_normalization') or {}).get('baseline_report',r.get('baseline_report'))
                    if (r.get('status')!='PASS' or r.get('comparison_error') or paired_report!=baseline.get('report')):continue
                    keys=('memory_mib','tracking_profile','baseline_protocol','baseline_tracking_profile',
                          'workload_identity','workload_configuration','performance_reference','pre_reclaim_headroom_mib','pre_reclaim_epoch_us','cpu_affinity_profile','normalization_protocol')
                    if (all(r.get(k)==c.get(k) for k in keys) and
                        (not c.get('normalization_protocol') or r.get('baseline_report')==c.get('baseline_report')) and
                        (r.get('parameters') or r.get('parameters_requested'))==(c.get('parameters') or c.get('parameters_requested'))):
                        reused[candidate]=tag
                        break
            if len(reused)>=2 and (best is None or len(reused)>len(best['trial_ids_by_candidate'])):
                best={'baseline':baseline_id,'trial_ids_by_candidate':reused,
                      'scope':'Continue an existing verification baseline group; reused observations count once, with original trial IDs. Not a new independent repeat.'}
    if best is None and entry.get('prepared_verification_baseline'):
        baseline_id = entry['prepared_verification_baseline']
        baseline = entry['trials'].get(baseline_id, {})
        if (baseline.get('mode') == 'all-local' and baseline.get('status') == 'PASS' and baseline.get('measurement_eligible',True) and
            baseline.get('checks', {}).get('all_local_tracking_active') and
            baseline.get('memory_mib') == first.get('memory_mib') and
            baseline.get('tracking_profile') == first.get('baseline_tracking_profile') and
            all(baseline.get(k) == first.get(k) for k in
                ('workload_identity', 'workload_configuration', 'baseline_protocol', 'cpu_affinity_profile')) and
            all(c.get('baseline_report') != baseline.get('report') for c in candidates.values())):
            best = {'baseline': baseline_id, 'trial_ids_by_candidate': {},
                    'scope': 'Reuse the completed verification baseline after a user-requested high-point retune; all verification points still require actual measurements.'}
    return best


def next_candidate(entry, app, plan):
    if plan.get('strategy') in ('low_range_coverage','tracking_range_coverage'):
        threshold=plan.get('vm_search_after_candidates')
        current=[r for tag,r in entry['trials'].items() if tag.startswith('d') and
                 r.get('memory_mib',entry['memory_mib'])==entry['memory_mib']]
        larger=[v for v in app['memory_steps_mib'] if v>entry['memory_mib']]
        if threshold and len(current)>=threshold and larger and all(r['status']=='PASS' for r in current):
            proposal=feedback.propose_coverage({**entry,'memory_mib':min(larger)},app,plan)
            if proposal:
                return proposal[0],proposal[1],('No accepted curve after %d candidates at %d MiB; test %d MiB with fresh all-local and a separately fixed curve. '%
                    (len(current),entry['memory_mib'],min(larger)))+proposal[2]
        return feedback.propose_coverage(entry, app, plan)
    initial = parameters(app, plan['policy_templates'][0])
    initial['minimum_local_mib'] = app['initial_local_floor_mib']
    return feedback.propose(entry, app, plan, initial)


def high_slowdown_check(rows, entry):
    """An optional application-specific target applies to each real observation."""
    minimum = entry.get('high_slowdown_min_exclusive_percent')
    if minimum is None:
        return {'status': 'PASS', 'scope': 'No additional high-point target'}
    high = max(rows, key=lambda r: r.get('reclaim_percent', -1))
    value = high.get('slowdown_percent')
    return {'status': 'PASS' if value is not None and math.isfinite(value) and value > minimum else 'FAIL',
            'trial_id': high.get('trial_id'), 'slowdown_percent': value,
            'minimum_exclusive_percent': minimum,
            'scope': 'User-requested high-point target, checked on every observation; fixed all-local denominator'}


def pending_curve(entry, reference):
    rows = [r for tag, r in entry['trials'].items()
            if tag.startswith('d') and r['memory_mib'] == entry['memory_mib'] and
            (not entry.get('execution_protocol') or r.get('execution_protocol')==entry['execution_protocol'])]
    fixed = entry.get('frozen_low_middle_trial_ids')
    if fixed:
        # Freeze the existing lower points when only the high point is retuned.
        high_floor = entry['high_probe_anchor_trial_id']
        params = entry['trials'][high_floor]['parameters_requested']
        rows = [r for r in rows if r['trial_id'] in fixed or
                ((r.get('parameters_requested') or {}).get('minimum_local_mib', float('inf')) <= params['minimum_local_mib'] and
                 (entry.get('high_probe_allow_floor_reduction') or
                  (r.get('parameters_requested') or {}).get('minimum_local_mib') == params['minimum_local_mib']) and
                 high_slowdown_check([r], entry)['status'] == 'PASS')]
    required_targets=entry.get('coverage_targets_percent') if entry.get('require_coverage_bands',True) else None
    if required_targets:
        targets = entry['coverage_targets_percent']
        rows = [r for r in rows if r.get('coverage_target_percent') in targets and
                feedback.coverage_contains(r.get('reclaim_percent'),r['coverage_target_percent'],targets,
                                           entry.get('coverage_tolerance_pp',0))]
    return metrics.find_curve(rows, reference, [v['candidate_ids'] for v in entry['validation_sets']],
                              required_targets,
                              check_trend=entry.get('require_overall_trend',not bool(entry.get('coverage_targets_percent'))),
                              include_all_local=entry.get('include_all_local_anchor',False),reclaim_range=entry.get('reclaim_range'),
                              trend_from_reclaimed_only=entry.get('trend_from_reclaimed_only',False),
                              reference_tolerance_pp=entry.get('reference_tolerance_pp',0),
                              flat_throughput_tolerance_pp=entry.get('flat_throughput_tolerance_pp',0))


def coverage_verified(rows, targets, tolerance_pp=0):
    return (not targets or
            ({r.get('coverage_target_percent') for r in rows} == set(targets) and
             all(feedback.coverage_contains(r.get('reclaim_percent'),r['coverage_target_percent'],targets,tolerance_pp)
                 for r in rows)))


def interrupt_workload(wrapper_pid):
    """Interrupt the leaf once; its finally block and wrapper restore the VM."""
    processes = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            stat = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            processes[int(directory.name)] = (int(stat[1]), (directory / 'cmdline').read_bytes())
        except (OSError, ValueError):
            pass
    descendants = {wrapper_pid}
    while True:
        expanded = descendants | {pid for pid, (parent, _) in processes.items() if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    leaves = [pid for pid in descendants if b'/run-chameleon-apps.py\0' in processes.get(pid, (0, b''))[1]]
    for pid in leaves or [wrapper_pid]:
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass


class StopRequested(Exception):
    pass


def stop_requested(stopping, stop_file, drain_file, interrupt_child=False):
    return stopping or stop_file.exists() or (not interrupt_child and drain_file.exists())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--stop', action='store_true', help='Ask the active controller to interrupt its trial and restore the VM')
    parser.add_argument('--drain', action='store_true', help='Finish the current trial and restoration, then stop before launching another')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--plan', type=Path, default=PLAN)
    args = parser.parse_args()
    out = args.directory.resolve()
    out.mkdir(parents=True, exist_ok=True)
    stop_file = out / 'STOP'
    drain_file = out / 'DRAIN'
    if args.stop or args.drain:
        (stop_file if args.stop else drain_file).write_text(utc() + '\n')
        print('Stop requested; controller waits for Guest cleanup and restoration.' if args.stop else
              'Drain requested; current trial continues to completion before the controller exits.')
        return
    lock = (out.parent / 'capacity-search.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path = out / 'search.json'
    if path.exists() and not args.resume:
        raise RuntimeError('Existing search: use --resume; evidence is never overwritten')
    plan = json.loads(args.plan.read_text())
    state = json.loads(path.read_text()) if path.exists() else {
        'status': 'RUNNING', 'protocol': 'tracked-all-local-pre-reclaim-v4', 'plan': plan,
        'mechanism_criterion': metrics.MECHANISM_CRITERION,
        'trend_criterion': metrics.TREND_CRITERION,
        'run_prefix': plan.get('run_prefix', 'f78v4'),
        'max_distinct_candidates_per_app': plan['max_candidates'], 'validation_repeats': plan.get('validation_repeats',3),
        'cases': {}, 'unavailable': {'spec602-gcc': 'No licensed runnable installation'},
        'previous_search': 'benchmarks/results/chameleon/tuning-fig78-feedback-v3/search.json'}
    plan = state['plan']
    if state['protocol'] != 'tracked-all-local-pre-reclaim-v4':
        raise RuntimeError('This controller runs the new pre-reclaim protocol; older evidence remains archived')
    cases = args.cases or list(plan['applications'])
    if any(c not in plan['applications'] for c in cases):
        parser.error('Unknown application')
    if args.resume:
        stop_file.unlink(missing_ok=True)
        drain_file.unlink(missing_ok=True)
    if state.get('error'):
        state.setdefault('interruptions', []).append(state.pop('error'))
    reference = json.loads((ROOT / 'benchmarks/config/chameleon-tuning-reference.json').read_text())
    stopping = False

    def signal_stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, signal_stop)
    signal.signal(signal.SIGTERM, signal_stop)

    def requested():
        return stop_requested(stopping,stop_file,drain_file)

    def publish():
        save(path, state)
        subprocess.run([sys.executable, str(HERE / 'summarize-chameleon-tuning.py'), '--directory', str(out)],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)

    def execute(case, tag, mode, params=None, baseline_id=None, tracking_override=None, matched_baseline_id=None):
        entry = state['cases'][case]
        preparation = feedback.preparation_headroom(plan['applications'][case], plan)
        preparation_epoch = plan['applications'][case].get('pre_reclaim_epoch_us')
        existing = entry['trials'].get(tag)
        if existing and existing['status'] != 'RUNNING':
            return existing
        if requested():
            raise StopRequested()
        name = f'{state.get("run_prefix", "f78v4")}-{case}-{tag}'
        directory = out.parent / name
        timeout = 14400 if mode == 'all-local' else max(300, int(entry['trials'][baseline_id]['window_duration_seconds'] * 3 + 120))
        command = [sys.executable, str(HERE / 'run-sized-chameleon-apps.py'), '--name', name,
                   '--cases', case, '--vm-memory-mib', str(entry['memory_mib']), '--run-mode', mode,
                   '--sample-seconds', '1', '--timeout', str(timeout)]
        command += ['--cpu-pinning' if plan.get('execution_protocol') else '--no-cpu-pinning']
        tracking=tracking_override or tracking_for(plan,params)
        command += ['--all-local-tracking','--sample-period',str(tracking['sample_period']),'--cooling-samples',str(tracking['cooling_samples']),'--hhh-interval-ms',str(tracking['hhh_interval_ms'])]
        if mode == 'chameleon':
            command += ['--pre-reclaim-headroom-mib', str(preparation)]
            if preparation_epoch is not None:
                command += ['--pre-reclaim-epoch-us', str(preparation_epoch)]
        if params:
            for option, key in [('psi-ppm', 'psi_ppm'), ('epoch-us', 'epoch_us'),
                                ('cold-folios', 'cold_folios'), ('minimum-local-mib', 'minimum_local_mib')]:
                command += ['--' + option, str(params[key])]
        trial = existing or {'trial_id': tag, 'name': name, 'case': case, 'mode': mode,
                             'memory_mib': entry['memory_mib'], 'parameters_requested': params,
                             'command': command, 'status': 'RUNNING', 'started_utc': utc()}
        trial['tracking_requested'] = tracking
        trial['execution_protocol'] = plan.get('execution_protocol')
        if params:
            app=plan['applications'][case];peak=app.get('measured_peak_mib')
            trial['capacity_context']={'calibrated_application_peak_mib':peak,'peak_source':app.get('peak_source'),
                'local_floor_mib':params['minimum_local_mib'],
                'floor_minus_peak_mib':params['minimum_local_mib']-peak if peak is not None else None,
                'os_headroom_mib':app.get('os_headroom_mib'),
                'scope':'Capacity comparison only; actual page-out/page-in is established by RDMA, shadow and fault counters'}
        if matched_baseline_id:
            trial['matched_tracking_baseline_id']=matched_baseline_id
            canonical=baseline_id+tracking_suffix(plan,tracking)
            expected=plan.get('tracking_baseline_retries',{}).get(canonical,canonical)
            trial['matched_tracking_baseline_reused']=matched_baseline_id!=expected
            if expected!=canonical and matched_baseline_id==expected:
                trial['matched_tracking_baseline_retry_of']=canonical
            trial['matched_tracking_baseline_scope']=('Earlier completed diagnostic; main denominator remains the current fixed all-local'
                if trial['matched_tracking_baseline_reused'] else 'Current baseline group')
        if params and plan.get('strategy') in ('low_range_coverage','tracking_range_coverage'):
            trial['pre_reclaim_headroom_mib'] = preparation
            trial['pre_reclaim_epoch_us'] = preparation_epoch or params['epoch_us']
            trial['coverage_target_percent'] = coverage_target_for(
                entry['memory_mib'], plan['applications'][case], plan, params)
        entry['trials'][tag] = trial
        publish()
        print('START', case, tag, entry['memory_mib'], params, flush=True)
        cancelled = False
        if not directory.exists():
            leaf_directory = out.parent / (name + '-' + case + ('-local' if mode == 'all-local' else '-on'))
            reader = feedback.LiveSamples(leaf_directory / case / 'samples.jsonl')
            next_monitor = 0
            with (out / (name + '.log')).open('w') as log:
                child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                while child.poll() is None:
                    if time.monotonic() >= next_monitor:
                        samples = reader.read()
                        monitor_params = params or {'minimum_local_mib': entry['memory_mib'], 'epoch_us': 10000, 'psi_ppm': 10000, 'cold_folios': 0}
                        diagnosis = feedback.analyze(samples, entry['memory_mib'], monitor_params)
                        latest = samples[-1] if samples else {}
                        snapshot = {'timestamp_utc': utc(), 'case': case, 'trial_id': tag, 'mode': mode,
                                    'phase': latest.get('phase', 'boot_or_setup'), 'diagnosis': diagnosis,
                                    'local_mib': latest.get('policy', {}).get('local_bytes', 0) / 2**20}
                        snapshot['target_local_mib'] = params['minimum_local_mib'] + preparation if params else None
                        health = feedback.application_health(leaf_directory / case / 'application', case)
                        if health is not None:
                            snapshot['application_health'] = health
                        progress_log = leaf_directory / case / 'launcher.log'
                        if progress_log.exists():
                            with progress_log.open('rb') as stream:
                                stream.seek(max(0, progress_log.stat().st_size - 2048))
                                snapshot['application_progress'] = stream.read().decode(errors='replace').splitlines()[-3:]
                        save(out / 'live-monitor.json', snapshot)
                        with (out / 'monitor.jsonl').open('a') as stream:
                            stream.write(json.dumps(snapshot) + '\n')
                        if diagnosis:
                            print('MONITOR', case, tag, latest.get('phase'), 'seconds', round(diagnosis['elapsed_seconds'], 1), 'reclaim', round(diagnosis['prefix_mean_reclaim_percent'], 2), 'flags', diagnosis['flags'], flush=True)
                        elif latest:
                            print('MONITOR', case, tag, latest.get('phase'), 'local_mib', round(snapshot['local_mib'], 1), 'target', snapshot['target_local_mib'], flush=True)
                        next_monitor = time.monotonic() + 10
                    if stop_requested(stopping,stop_file,drain_file,interrupt_child=True) and not cancelled:
                        cancelled = True
                        interrupt_workload(child.pid)
                    time.sleep(1)
                trial['launcher_exit_code'] = child.returncode
        wrapper = json.loads((directory / 'report.json').read_text())
        if wrapper['status'] == 'RUNNING':
            raise RuntimeError('Existing unfinished wrapper: ' + name)
        record = wrapper['cases'].get(case, {})
        leaf = record.get('baseline_report' if mode == 'all-local' else 'chameleon_report')
        if leaf:
            trial.update(metrics.extract(Path(leaf).parent, case))
        else:
            trial.update(status='FAIL', error=wrapper.get('error', 'No leaf report'))
        trial.update(wrapper_report=str(directory / 'report.json'), wrapper_status=wrapper['status'], finished_utc=utc())
        if mode == 'chameleon':
            trial['diagnosis'] = feedback.completed_diagnosis(trial)
            if leaf:
                trial['pre_reclaim'] = json.loads(Path(leaf).read_text())['cases'][case].get('pre_reclaim')
        if not wrapper.get('restoration') or wrapper.get('restoration_error'):
            trial['status'] = 'FAIL'
            publish()
            raise RuntimeError('VM restoration incomplete: ' + name)
        if cancelled:
            trial.update(status='CANCELLED_BY_USER', error='User requested stop; VM restored')
        elif mode == 'chameleon':
            if plan.get('normalization_protocol') == 'fixed-first-capacity-all-local-v1':
                fixed_id=fixed_baseline_id(case,entry['memory_mib'],plan)
                trial.update(metrics.compare_shared_baseline(trial,entry['trials'][fixed_id],
                             entry['trials'][baseline_id],entry['trials'][matched_baseline_id or baseline_id],reference))
                trial['fixed_normalization_baseline_id']=fixed_id
                trial['per_repeat_baseline_id']=baseline_id
            elif plan.get('performance_reference') == 'fixed-tracking-with-matched-diagnostic-v1':
                trial.update(metrics.compare_fixed_tracking(trial, entry['trials'][baseline_id],
                             entry['trials'][matched_baseline_id or baseline_id], reference))
            else:
                trial.update(metrics.compare(trial, entry['trials'][baseline_id], reference))
            if trial.get('diagnosis'):
                trial['diagnosis']['slowdown_percent'] = trial.get('slowdown_percent')
                trial['diagnosis']['reference_margin_pp'] = trial.get('reference_margin_pp')
                trial['diagnosis']['reference_comparison_error'] = trial.get('comparison_error')
        publish()
        print('END', case, tag, trial['status'], trial.get('reclaim_percent'), trial.get('slowdown_percent'), trial.get('reference_status'), flush=True)
        if requested():
            raise StopRequested()
        return trial

    def measure_candidate(case, tag, params, baseline_id):
        matched_id=baseline_id
        tracking=tracking_for(plan,params)
        if plan.get('performance_reference') == 'fixed-tracking-with-matched-diagnostic-v1':
            matched_id=baseline_id+tracking_suffix(plan,tracking)
            if matched_id!=baseline_id and plan.get('reuse_matched_tracking_diagnostics'):
                matched_id=cached_tracking_baseline(state['cases'][case],baseline_id,tracking) or matched_id
            matched_id=plan.get('tracking_baseline_retries',{}).get(matched_id,matched_id)
            baseline=execute(case,matched_id,'all-local',tracking_override=tracking)
            if baseline['status']!='PASS':
                raise RuntimeError('Matched tracking baseline failed: '+case+'/'+matched_id)
        return execute(case,tag,'chameleon',params,baseline_id,matched_baseline_id=matched_id)

    def validate(case, validation, index):
        entry = state['cases'][case]
        ids = validation['candidate_ids']
        repeats=plan.get('validation_repeats',3)
        validation['required_repeats']=repeats
        validation['trend_criterion']=metrics.trend_criterion(repeats)
        validation['includes_all_local_origin']=plan.get('include_all_local_anchor',False)
        if not validation['rounds'] and plan.get('count_initial_measurement'):
            initial=measured_search_round(entry,ids,reference,plan)
            if initial:
                validation['rounds'].append(initial)
                validation['measurement_basis']='Initial measured configuration plus frozen verification; not two additional post-search repeats'
                publish()
        for rep in range(1, repeats+1):
            if any(r['repeat'] == rep for r in validation['rounds']):
                continue
            tag = f'v{index}r{rep}'
            baseline_id = tag + '-b'
            reuse=None
            if repeats==2 and rep==2 and plan.get('count_initial_measurement'):
                reuse=validation.get('verification_reuse') or reusable_verification_group(entry,ids)
                if reuse:
                    validation['verification_reuse']=reuse
                    baseline_id=reuse['baseline']
            execute(case, baseline_id, 'all-local')
            rows = [entry['trials'][reuse['trial_ids_by_candidate'][candidate]]
                    if reuse and candidate in reuse['trial_ids_by_candidate'] else
                    measure_candidate(case, tag + '-' + candidate, entry['trials'][candidate]['parameters_requested'], baseline_id) for candidate in ids]
            check = metrics.curve_check(rows, reference, check_trend=False,
                                        reference_tolerance_pp=plan.get('reference_tolerance_pp',0),
                                        include_all_local=plan.get('include_all_local_anchor',False),reclaim_range=entry.get('reclaim_range'))
            target_check = high_slowdown_check(rows, entry)
            check['high_point_target'] = target_check
            if target_check['status'] != 'PASS':
                check.update(status='FAIL', reason='High point did not exceed the requested slowdown threshold')
            if plan.get('require_coverage_bands',True) and not coverage_verified(rows,entry.get('coverage_targets_percent'),entry.get('coverage_tolerance_pp',0)):
                check = {'status': 'FAIL', 'reason': 'Measured repeat left its required reclamation band'}
            if check['status'] == 'PASS' and sorted(range(len(rows)),key=lambda i:rows[i]['reclaim_percent']) != list(range(len(ids))):
                check = {'status': 'FAIL', 'reason': 'Candidate ordering changed between repeats'}
            validation['rounds'].append({'repeat': rep, 'baseline': baseline_id, 'trial_ids': [r['trial_id'] for r in rows], 'check': check,
                                         'verification_reuse':reuse})
            publish()
        validation['aggregate_check'] = metrics.repeated_curve_check(
            [[entry['trials'][tag] for tag in r['trial_ids']] for r in validation['rounds']], reference,
            check_trend=plan.get('strategy') != 'low_range_coverage',
            reference_tolerance_pp=plan.get('reference_tolerance_pp',0),required_repeats=repeats,
            include_all_local=plan.get('include_all_local_anchor',False),reclaim_range=entry.get('reclaim_range'),
            max_mean_reversal_pp=plan.get('max_mean_reversal_pp'),trend_from_reclaimed_only=plan.get('trend_from_reclaimed_only',False),
            flat_throughput_tolerance_pp=entry.get('flat_throughput_tolerance_pp',0),
            mean_reference_tolerance_pp=plan.get('mean_reference_tolerance_pp',0))
        validation['reference_criterion'] = plan.get('reference_criterion','strict-each-repeat')
        validation['high_point_target_checks'] = [high_slowdown_check(
            [entry['trials'][tag] for tag in r['trial_ids']], entry) for r in validation['rounds']]
        validation['scope'] = ('low_range_coverage_only; full low-to-high trend needs joint validation'
                               if plan.get('strategy') == 'low_range_coverage' else 'full_candidate_curve')
        validation['trend_criterion'] = metrics.trend_criterion(repeats)
        validation['status'] = 'PASS' if all(r['check']['status'] == 'PASS' for r in validation['rounds']) and validation['aggregate_check']['status'] == 'PASS' and all(c['status']=='PASS' for c in validation['high_point_target_checks']) else 'FAIL'
        if validation['status'] == 'PASS':
            entry['status'] = 'ACCEPTED'
            entry['accepted'] = {'memory_mib': entry['memory_mib'], 'points': [entry['trials'][tag]['parameters_requested'] for tag in ids], 'validation': validation}
        publish()

    save(out / 'controller.json', {'pid': os.getpid(), 'started_utc': utc(), 'command': sys.argv})
    state.update(status='RUNNING', requested_cases=cases,validation_repeats=plan.get('validation_repeats',3),
                 trend_criterion=metrics.trend_criterion(plan.get('validation_repeats',3)))
    publish()
    try:
        for case in cases:
            app = plan['applications'][case]
            entry = state['cases'].setdefault(case, {'status': 'RUNNING', 'memory_mib': app['memory_steps_mib'][0], 'trials': {}, 'validation_sets': [], 'decisions': []})
            if entry['status']!='ACCEPTED': entry['execution_protocol']=plan.get('execution_protocol')
            for key in ('frozen_low_middle_trial_ids', 'high_probe_anchor_trial_id', 'high_slowdown_min_exclusive_percent', 'high_probe_allow_floor_reduction'):
                if key in app:
                    entry[key] = app[key]
            if app.get('coverage_targets_percent'):
                entry['coverage_targets_percent'] = app['coverage_targets_percent']
                entry['require_overall_trend'] = plan.get('strategy') == 'tracking_range_coverage'
                entry['coverage_tolerance_pp'] = plan.get('coverage_tolerance_pp',0)
                entry['require_coverage_bands'] = plan.get('require_coverage_bands',True)
                entry['include_all_local_anchor'] = plan.get('include_all_local_anchor',False)
                entry['reclaim_range'] = app.get('reclaim_range')
                entry['trend_from_reclaimed_only'] = plan.get('trend_from_reclaimed_only',False)
                entry['reference_tolerance_pp'] = plan.get('reference_tolerance_pp',0)
                entry['flat_throughput_tolerance_pp'] = app.get('flat_throughput_tolerance_pp',0)
            if entry['status'] == 'ACCEPTED':
                continue
            if entry['status'] == 'SKIPPED_AFTER_16' and not pending_curve(entry, reference):
                continue
            if entry['status'] == 'SKIPPED_AFTER_16':
                entry['reason'] = 'Existing measurements form a candidate curve under the updated acceptance criterion; validate without new search candidates'
            entry['status'] = 'RUNNING'
            for index, validation in enumerate(entry['validation_sets'], 1):
                if validation['status'] == 'RUNNING':
                    validate(case, validation, index)
            while entry['status'] != 'ACCEPTED':
                if requested():
                    raise StopRequested()
                # Reclassified existing measurements can form a curve even
                # when the exploration budget was already exhausted.
                curve = pending_curve(entry, reference)
                if curve:
                    validation = {'candidate_ids': [r['trial_id'] for r in curve], 'status': 'RUNNING', 'rounds': [],
                                  'mechanism_criterion': metrics.MECHANISM_CRITERION, 'trend_criterion': metrics.TREND_CRITERION}
                    entry['validation_sets'].append(validation)
                    publish()
                    validate(case, validation, len(entry['validation_sets']))
                    continue
                pending = [(tag, r) for tag, r in entry['trials'].items() if tag.startswith('d') and r['status'] == 'RUNNING']
                if pending:
                    tag, r = pending[0]
                    memory, params, reason = r['memory_mib'], r['parameters_requested'], 'resume interrupted trial'
                else:
                    candidate = next_candidate(entry, app, plan)
                    if candidate is None:
                        completed = sum(t.startswith('d') for t in entry['trials'])
                        entry.update(status='SKIPPED_AFTER_16' if completed>=app['remaining_candidates'] else 'NEEDS_DIAGNOSIS', reason='Remaining attempt allowance exhausted' if completed>=app['remaining_candidates'] else 'No untested evidence-based proposal; investigate before continuing')
                        break
                    memory, params, reason = candidate
                    tag = 'd%02d' % (1 + sum(t.startswith('d') for t in entry['trials']))
                assert 2048 <= params['minimum_local_mib'] < memory
                entry['decisions'].append({'before_candidate': tag, 'memory_mib': memory, 'reason': reason, 'parameters': params})
                entry['memory_mib'] = memory
                baseline_id = fixed_baseline_id(case,memory,plan)
                baseline = execute(case, baseline_id, 'all-local')
                if baseline['status'] != 'PASS':
                    entry.update(status='BASELINE_FAILED', reason='Complete same-capacity all-local run required')
                    break
                measure_candidate(case, tag, params, baseline_id)
            publish()
            print('CASE', case, entry['status'], flush=True)
            subprocess.run([sys.executable, str(HERE / 'plot-chameleon-tuning.py'), '--directory', str(out)], cwd=ROOT, check=True)
        state['status'] = 'COMPLETE'
    except StopRequested:
        state.update(status='STOPPED_BY_USER', stopped_utc=utc())
        for entry in state['cases'].values():
            if entry['status'] == 'RUNNING':
                entry['status'] = 'STOPPED_BY_USER'
    except BaseException as error:
        state.update(status='INTERRUPTED', error=repr(error))
        raise
    finally:
        publish()
    print('RESULT', path, flush=True)


if __name__ == '__main__':
    main()
