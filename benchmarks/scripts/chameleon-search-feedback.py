#!/usr/bin/env python3
"""Read existing experiment samples and propose evidence-based, one-knob trials."""
import json
import math
import re
from pathlib import Path

MIB = 2 ** 20
GIB = 2 ** 30


def application_health(application_directory, case):
    """Read YCSB failures while it runs, including workers that exited early.

    A running load is never marked PASS. A failed interval remains visible even
    if surviving workers subsequently report intervals with zero errors.
    """
    if case != 'cassandra':
        return None
    evidence = []
    for path in sorted(Path(application_directory).glob('*/*.err.log')):
        if path.name not in ('load.err.log', 'run.err.log'):
            continue
        body = path.read_text(errors='replace')
        failed = sum(int(n) for n in re.findall(r'\[(?:INSERT|READ|UPDATE)-FAILED: Count=(\d+)', body))
        aborted = body.count('Error inserting, not retrying any more.')
        if failed or aborted:
            evidence.append({'path': str(path), 'phase': 'load' if path.name=='load.err.log' else 'run', 'reported_failed_operations': failed,
                             'insert_worker_aborts': aborted})
    return {'status': ('FAIL' if any(v['phase']=='run' for v in evidence) else 'WARN_INSERT_ERRORS') if evidence else 'NO_FAILURE_OBSERVED',
            'evidence': evidence,
            'scope': 'Read-only live YCSB log check; absence of errors is not completed workload validation'}


def preparation_headroom(app, plan):
    return app.get('pre_reclaim_headroom_mib', plan['pre_reclaim_headroom_mib'])


def coverage_seeds(memory_mib, app, plan):
    return [(target, {**template, 'minimum_local_mib':
                     int(math.ceil(memory_mib * (1 - target / 100) / 2)) * 2})
            for target, template in zip(app['coverage_targets_percent'],
                                        app.get('coverage_templates', plan['coverage_templates']))]


def coverage_contains(reclaim, target, targets, tolerance_pp=0):
    """Search bands allow small edge variation; curve spacing is checked separately."""
    if reclaim is None:
        return False
    i = targets.index(target)
    lower = (targets[i-1] + target)/2 if i else max(0, target-(targets[1]-target)/2)
    upper = (target + targets[i+1])/2 if i+1 < len(targets) else target+(target-targets[i-1])/2
    return max(0, lower-tolerance_pp) <= reclaim <= upper+tolerance_pp


def reference_probe_eligible(trial, tolerance_pp=0):
    """A bounded noisy point may be repeated; its strict comparison stays intact."""
    if trial.get('status')!='PASS' or trial.get('comparison_error'):return False
    if trial.get('reference_status')=='PASS':return True
    margins=[v for fig in trial.get('reference_margin_pp',{}).values() for v in fig.values()]
    return tolerance_pp>0 and bool(margins) and all(v>=-tolerance_pp for v in margins)


def propose_coverage(entry, app, plan):
    """Fill low/middle reclaim bands, retaining failed measurements and fixed VM."""
    rows = [r for tag, r in entry['trials'].items() if tag.startswith('d')]
    if len(rows) >= app['remaining_candidates']:
        return None
    # An intentionally interrupted run has no complete performance result.
    # Keep it in the attempt budget, but do not treat it as a failed policy
    # configuration that blocks the user's next high-point experiment.
    rows = [r for r in rows if r.get('status') != 'CANCELLED_BY_USER']
    memory = entry['memory_mib']
    rows = [r for r in rows if r.get('memory_mib',memory)==memory]
    if plan.get('execution_protocol'):
        rows = [r for r in rows if r.get('execution_protocol')==plan['execution_protocol']]
    # A diagnosed setup failure before the timed application is not evidence
    # about its requested parameters. A wrapper report may still exist; that
    # case requires explicit evidence that the application did not start.
    # Keep the failure in the attempt budget and ledger after setup is fixed.
    rows = [r for r in rows if not (r.get('setup_failure_resolved') and
                                   r.get('status')=='FAIL' and
                                   (not r.get('report') or
                                    (isinstance(r['setup_failure_resolved'], dict) and
                                     r['setup_failure_resolved'].get('application_started') is False)))]
    coverage_tolerance = plan.get('coverage_tolerance_pp', 0)
    # A diagnosed preparation change is a distinct experiment. Retain old
    # measurements/budget usage, but permit a controlled repeat of the same
    # runtime knobs under the new, explicitly recorded preparation protocol.
    preparation = preparation_headroom(app, plan)
    current = [r for r in rows if (r.get('pre_reclaim') or {}).get('headroom_mib',
               r.get('pre_reclaim_headroom_mib',preparation)) == preparation and
               (r.get('pre_reclaim') or {}).get('epoch_us',r.get('pre_reclaim_epoch_us',r['parameters_requested']['epoch_us'])) ==
               app.get('pre_reclaim_epoch_us',r['parameters_requested']['epoch_us'])]
    seen = {tuple(sorted(r['parameters_requested'].items())) for r in current}
    if app.get('high_probe_overrides'):
        # A user-scoped high-point retune must not change already frozen lower
        # points or VM size. Every proposal still consumes the normal budget.
        anchor = entry['trials'][app['high_probe_anchor_trial_id']]
        if anchor.get('memory_mib', memory) != memory:
            return None
        floor = anchor['parameters_requested']['minimum_local_mib']
        if any(r['status'] != 'PASS' for r in current
               if r['parameters_requested']['minimum_local_mib'] == floor):
            return None
        for override in app['high_probe_overrides']:
            params = {**anchor['parameters_requested'], **override}
            if params['minimum_local_mib'] != floor and not (
                    app.get('high_probe_allow_floor_reduction') and
                    2048 <= params['minimum_local_mib'] < floor):
                raise ValueError('High-point-only tracking retune must preserve its local floor')
            if tuple(sorted(params.items())) not in seen:
                return memory, params, ('User-requested high-point capacity/tracking probe; preserve VM, lower points and fixed all-local; all changed knobs are recorded in parameters_requested'
                                        if params['minimum_local_mib'] != floor else
                                        'User-requested high-point slowdown retune; preserve VM, lower points, local floor and fixed all-local; measure real tracking/policy response')
        return None
    rejected = set()
    coverage_rejected = set()
    targets_by_floor = {p['minimum_local_mib']: target
                        for target, p in coverage_seeds(memory, app, plan)}
    for validation in entry.get('validation_sets', []):
        if validation.get('status') != 'FAIL':
            continue
        for repeat in validation.get('rounds', []):
            for candidate, tag in zip(validation['candidate_ids'], repeat['trial_ids']):
                if entry['trials'][candidate].get('memory_mib',memory)!=memory:
                    continue
                result = entry['trials'][tag]
                if result['status'] != 'PASS':
                    return None  # A failed workload/transport still needs diagnosis.
                if result.get('reference_status') != 'PASS':
                    rejected.add(candidate)
                target = targets_by_floor.get(entry['trials'][candidate]['parameters_requested']['minimum_local_mib'])
                if target is not None and not coverage_contains(result.get('reclaim_percent'), target,
                                                                app['coverage_targets_percent'], coverage_tolerance):
                    rejected.add(candidate)
                    coverage_rejected.add(candidate)
    for target, seed in coverage_seeds(memory, app, plan):
        band = [r for r in current if r['parameters_requested']['minimum_local_mib'] == seed['minimum_local_mib']]
        if any(reference_probe_eligible(r,plan.get('reference_tolerance_pp',0)) and r.get('trial_id') not in rejected and
               coverage_contains(r.get('reclaim_percent'),target,app['coverage_targets_percent'],coverage_tolerance) for r in band):
            continue
        if len(band) >= plan.get('max_candidates_per_coverage_band', 5):
            continue
        if not band:
            return memory, seed, 'Coverage probe near %.2f%% reclaim: fixed VM, distinct policy frequency and local floor; measured RSS determines actual reclaim' % target
        if any(r['status'] != 'PASS' for r in band):
            return None  # Investigate functional failure before tuning further.
        proposals = []
        for anchor in reversed(band):
            p = anchor['parameters_requested']
            # Zero cold/RDMA activity at the local floor is a valid free-only
            # operating point; do not force cold traffic to qualify a point.
            proposals.append(({**p, 'epoch_us': min(1000000, p['epoch_us'] * 2)},
                              'Test lower policy frequency to reduce low-reclaim overhead'))
            proposals.append(({**p, 'psi_ppm': max(1, p['psi_ppm'] // 2)},
                              'Test lower PSI tolerance at the same capacity and cycle'))
        for params, reason in proposals:
            if tuple(sorted(params.items())) not in seen:
                repeat_note = ('; frozen repeat missed its measured reclamation band, retain failed validation and test a new configuration'
                               if any(r.get('trial_id') in coverage_rejected for r in band) else
                               '; frozen repeat missed a reference, retain failed validation and test a new configuration'
                               if any(r.get('trial_id') in rejected for r in band) else '')
                return memory, params, 'Coverage %.2f%%: %s%s; actual results are not forced to target' % (target, reason, repeat_note)
    if plan.get('tracking_search'):
        # If repeated measurements show a local reversal, probe the point on
        # its right first. Increasing only the highest point cannot resolve a
        # low-to-middle reversal. Otherwise start at the high end as before.
        # Every candidate measures total overhead and a matched tracking-only
        # baseline; a changed knob does not establish a performance trend.
        reversal_floors = set()
        for validation in reversed(entry.get('validation_sets', [])):
            aggregate = validation.get('aggregate_check', {})
            if validation.get('status') != 'FAIL' or aggregate.get('status') != 'FAIL':
                continue
            ids = validation['candidate_ids']
            for reversal in aggregate.get('adjacent_mean_reversals', []):
                right = reversal['left_index'] + 1
                if right >= len(ids):
                    continue
                trial = entry['trials'][ids[right]]
                if trial.get('memory_mib', memory) == memory:
                    reversal_floors.add(trial['parameters_requested']['minimum_local_mib'])
            if reversal_floors:
                break
        anchors = sorted(current, key=lambda r: (
            r['parameters_requested']['minimum_local_mib'] not in reversal_floors,
            -r.get('reclaim_percent', -1)))
        for anchor in anchors:
            if not reference_probe_eligible(anchor,plan.get('reference_tolerance_pp',0)):continue
            p=anchor['parameters_requested']
            if sum(r['parameters_requested']['minimum_local_mib']==p['minimum_local_mib'] for r in current)>=plan.get('max_candidates_per_coverage_band',5):continue
            options=[]
            period=p.get('sample_period',plan['tracking']['sample_period'])
            for value in app.get('sample_period_ladder',plan.get('sample_period_ladder',[65536,32768,16384,8192])):
                if value<period:options.append(({**p,'sample_period':value},'denser PEBS sampling'))
            interval=p.get('hhh_interval_ms',plan['tracking']['hhh_interval_ms'])
            for value in app.get('hhh_interval_ladder_ms',plan.get('hhh_interval_ladder_ms',[15000,10000,5000])):
                if value<interval:options.append(({**p,'hhh_interval_ms':value},'more frequent HHH'))
            for params,reason in options:
                if tuple(sorted(params.items())) not in seen:
                    context = ('Repeated-curve reversal tracking probe' if p['minimum_local_mib'] in reversal_floors
                               else 'High-reclaim tracking probe')
                    return memory,params,context+': '+reason+'; measure total, matched-baseline overhead, and cold/RDMA response'
    return None


def analyze(samples, memory_mib, params):
    rows = [r for r in samples if r.get('phase') == 'running']
    if len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    elapsed = last['seconds'] - first['seconds']
    if elapsed <= 0:
        return None
    def delta(group, key):
        return last[group].get(key, 0) - first[group].get(key, 0)
    total = memory_mib * MIB
    floor = params['minimum_local_mib'] * MIB
    epochs = delta('policy', 'epochs')
    low, high = delta('policy', 'low_epochs'), delta('policy', 'high_epochs')
    gross = delta('policy', 'free_reclaimed_bytes')
    returned = delta('policy', 'free_returned_bytes')
    free_net = last['policy'].get('hard_reclaimed_bytes', 0)
    free_rate = (gross - returned) / elapsed / MIB
    local = last['policy'].get('local_bytes', total)
    min_local = min(r['policy'].get('local_bytes', total) for r in rows)
    cold_peak = max(r['retired_bytes'] for r in rows)
    cold_mean = sum((b['seconds'] - a['seconds']) * (a['retired_bytes'] + b['retired_bytes']) / 2 for a, b in zip(rows, rows[1:])) / elapsed
    written, read = delta('rdma', 'write_bytes'), delta('rdma', 'read_bytes')
    ticks = delta('policy', 'timer_ticks')
    available = min(r['meminfo']['MemAvailable'] for r in rows)
    flags = []
    policy_active = any(r['policy'].get('enabled',1) for r in rows)
    headroom = max(0, local - floor)
    utilization = (total - min_local) / max(total - floor, 1)
    high_fraction = high / max(low + high, 1)
    busy = delta('policy', 'worker_ns') / elapsed / 1e9
    coalesced = delta('policy', 'coalesced_ticks') / max(ticks, 1)
    if available < 512 * MIB:
        flags.append('low_guest_memory')
    if delta('rdma', 'transfer_errors') or delta('rdma', 'map_failures'):
        flags.append('transport_error')
    if delta('policy', 'action_errors') or delta('policy', 'psi_errors'):
        flags.append('policy_error')
    if any(delta('shadow', k) for k in ('data_save_failure','data_load_failure','demand_fault_failures')):
        flags.append('data_path_error')
    if policy_active and elapsed >= 5 and headroom > 2 * GIB and utilization < .8:
        flags.append('free_reclaim_not_converged')
    if high_fraction >= .10 and returned > .1 * max(gross, 1):
        flags.append('psi_returning_capacity')
    if read > GIB and read > cold_peak and delta('shadow', 'demand_fault_successes') > 100:
        flags.append('cold_readback_pressure')
    if busy > .85 or coalesced > .5:
        flags.append('policy_worker_busy')
    if policy_active and min_local - floor <= 64 * MIB:
        flags.append('local_floor_reached')
    elif policy_active and min_local - floor < total * .02:
        flags.append('local_floor_near')
    if policy_active and (cold_peak <= 0 or written <= 0 or read <= 0):
        flags.append('cold_reclaim_not_observed')
    result = {
        'elapsed_seconds': elapsed, 'samples': len(rows), 'flags': flags,
        'policy_active':bool(policy_active),
        'prefix_mean_reclaim_percent': 100 * (1 - sum(r['compute_memory']['Rss'] for r in rows) / len(rows) / total),
        'last_reclaim_percent': 100 * (1 - last['compute_memory']['Rss'] / total),
        'free_net_gib': free_net / GIB, 'free_gross_gib': gross / GIB,
        'free_returned_gib': returned / GIB, 'net_free_mib_per_second': free_rate,
        'free_mib_per_low_epoch': gross / max(low, 1) / MIB,
        'low_epochs': low, 'high_epochs': high, 'high_epoch_fraction': high_fraction,
        'effective_epochs_per_second': epochs / elapsed,
        'policy_worker_wall_fraction': busy, 'coalesced_tick_fraction': coalesced,
        'floor_headroom_gib': headroom / GIB, 'capacity_limit_utilization': utilization,
        'minimum_available_gib': available / GIB, 'cold_peak_gib': cold_peak / GIB,
        'cold_mean_gib': cold_mean / GIB, 'rdma_write_gib': written / GIB,
        'rdma_read_gib': read / GIB, 'demand_faults': delta('shadow', 'demand_fault_successes'),
        'psi_some_seconds': delta('policy', 'psi_some_ns') / 1e9,
        'policy_action_errors': delta('policy', 'action_errors'),
        'policy_psi_errors': delta('policy', 'psi_errors'),
        'policy_last_error': last['policy'].get('last_error', 0),
        'split_ok': delta('manager', 'split_ok'),
        'application_peak_gib': max(r['application_rss_bytes'] for r in rows) / GIB,
        'scope': 'Read-only monitoring of existing running samples; prefix metrics are not completed performance results',
    }
    return result


class LiveSamples:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.pending = ''
        self.rows = []

    def read(self):
        if not self.path.exists():
            return self.rows
        with self.path.open() as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset = stream.tell()
        parts = (self.pending + chunk).split('\n')
        self.pending = parts.pop()
        self.rows.extend(json.loads(line) for line in parts if line.strip())
        return self.rows


def completed_diagnosis(trial):
    if not trial.get('report') or not trial.get('parameters_requested'):
        return None
    raw = json.loads(Path(trial['report']).read_text())['cases'][trial['case']]
    result = analyze(raw.get('samples', []), trial['memory_mib'], trial['parameters_requested'])
    if result:
        result['complete_application_window'] = bool(raw.get('summary', {}).get('window_complete'))
        result['application_error'] = raw.get('error')
        result['application_exit_code'] = raw.get('exit_code')
        result['inference_limit'] = 'Counters identify bottleneck candidates; they do not prove that the same page was repeatedly reclaimed or assign all slowdown to a single cause'
    return result


def propose(entry, app, plan, initial_parameters, reference=None):
    rows = [r for tag, r in entry['trials'].items() if tag.startswith('d')]
    if len(rows) >= app.get('remaining_candidates', plan['max_candidates']):
        return None
    memory = entry['memory_mib']
    if not rows:
        return memory, initial_parameters, 'Initial probe; subsequent parameters depend on measured diagnostics'
    same = [r for r in rows if r['memory_mib'] == memory and r.get('diagnosis')]
    if not same:
        return None

    case = next((r.get('case') for r in same if r.get('case')), None)
    if reference is None and case:
        reference = json.loads((Path(__file__).resolve().parents[1] / 'config/chameleon-tuning-reference.json').read_text())
    series = [points for fig in ('fig7', 'fig8')
              for points in (reference or {}).get(fig, {}).get(case, {}).values()]
    bounds = (max(points[0][0] for points in series), min(points[-1][0] for points in series)) if series else None

    def outside(r):
        return (r.get('comparison_error') == 'Outside reference interpolation domain' or
                r['diagnosis'].get('reference_comparison_error') == 'Outside reference interpolation domain')

    def score(r):
        margins = [v for fig in r.get('reference_margin_pp', {}).values() for v in fig.values()]
        return min(margins) if margins and r['status'] == 'PASS' else -float('inf')

    # A large numeric margin alone can come from negligible cold reclamation.
    anchors = sorted(same, key=lambda r: (r.get('reference_status') == 'PASS', not outside(r), score(r),
                     -abs(r.get('reclaim_percent', 0) - bounds[1]) if bounds else 0), reverse=True)
    seen = {(r['memory_mib'], tuple(sorted((r.get('parameters_requested') or {}).items()))) for r in rows}
    proposals = []

    def add(anchor, key, value, reason, new_memory=None):
        p = dict(anchor['parameters_requested'])
        p[key] = value
        mem = new_memory or memory
        if (mem, tuple(sorted(p.items()))) in seen:
            return
        if not (1000 <= p['epoch_us'] <= 1000000 and 0 < p['cold_folios'] <= 128 and 0 < p['psi_ppm'] <= 1000000 and 2048 <= p['minimum_local_mib'] < mem):
            return
        proposals.append((mem, p, anchor['trial_id'] + ': ' + reason + '; change only ' + key + (', VM capacity' if new_memory else '')))

    def healthy(r):
        return (r.get('reference_status') == 'PASS' and
                not {'transport_error', 'low_guest_memory', 'policy_worker_busy'}.intersection(r['diagnosis']['flags']))

    # Matched cold-batch probes can reveal a capacity plateau. Preserve the
    # effective cold batch and test capacity, rather than repeatedly increasing
    # traffic at a floor that already caps total reclamation.
    for high in sorted(same, key=lambda r: r.get('reclaim_percent', -math.inf), reverse=True):
        if not healthy(high) or 'local_floor_reached' not in high['diagnosis']['flags']:
            continue
        hp = high['parameters_requested']
        for low in same:
            lp = low['parameters_requested']
            if low['status'] != 'PASS' or lp['cold_folios'] >= hp['cold_folios']:
                continue
            if any(lp[k] != hp[k] for k in hp if k != 'cold_folios'):
                continue
            if not all(k in r for r in (low, high) for k in ('reclaim_percent', 'slowdown_percent')):
                continue
            gain = high['reclaim_percent'] - low['reclaim_percent']
            cost = high['slowdown_percent'] - low['slowdown_percent']
            if abs(gain) < 1 and cost >= .25:
                add(high, 'minimum_local_mib', max(2048, hp['minimum_local_mib'] - 1024),
                    'Matched cold-batch probe %s->%s gained %.3f pp reclaim but added %.3f pp slowdown at the local floor: retain effective cold selection and test 1 GiB less local capacity' %
                    (low['trial_id'], high['trial_id'], gain, cost))

    # Once two measured points have the requested direction, explore from the
    # higher-reclamation point. Always returning to the largest-margin point
    # tends to spend the budget revisiting the low end of the curve.
    curve_metrics = ['reclaim_percent', 'slowdown_percent']
    for high in sorted(same, key=lambda r: r.get('reclaim_percent', -math.inf), reverse=True):
        if not healthy(high):
            continue
        for low in same:
            metrics = curve_metrics + (['p95_slowdown_percent'] if 'p95_slowdown_percent' in high else [])
            if low.get('reference_status') != 'PASS' or not all(k in r for r in (low, high) for k in metrics):
                continue
            if (high['reclaim_percent'] - low['reclaim_percent'] >= 1 and
                    high['slowdown_percent'] - low['slowdown_percent'] >= .25 and
                    ('p95_slowdown_percent' not in high or high['p95_slowdown_percent'] - low['p95_slowdown_percent'] >= .25)):
                cold = high['parameters_requested']['cold_folios']
                add(high, 'cold_folios', min(128, cold + max(1, cold // 2)),
                    'Extend measured increasing pair %s->%s with a smaller cold-batch step (+50%%, at least 1 folio); full three-point and three-repeat checks still required' %
                    (low['trial_id'], high['trial_id']))
                break

    for anchor in anchors:
        p = anchor['parameters_requested']
        d = anchor['diagnosis']
        flags = d['flags']
        if 'transport_error' in flags:
            continue
        if 'low_guest_memory' in flags or d.get('application_exit_code') == 137:
            add(anchor, 'minimum_local_mib', min(memory - 2048, p['minimum_local_mib'] + 2048), 'Guest memory shortage: raise local floor before further reclamation')
            continue
        if anchor.get('status') != 'PASS':
            continue
        if outside(anchor):
            if bounds and 'reclaim_percent' in anchor:
                observed = anchor['reclaim_percent']
                margin = min(1.0, (bounds[1] - bounds[0]) / 4)
                target = bounds[1] - margin if observed > bounds[1] else bounds[0] + margin
                floor = int(math.ceil((p['minimum_local_mib'] + memory * (observed - target) / 100) / 2)) * 2
                floor = max(2048, min(memory - 2048, floor))
                add(anchor, 'minimum_local_mib', floor,
                    'Measured reclaim %.3f%% is outside shared reference [%.3f, %.3f]%%: estimate a floor for %.3f%%, then remeasure; no extrapolation or VM expansion' %
                    (observed, bounds[0], bounds[1], target))
            else:
                add(anchor, 'minimum_local_mib', min(memory-2048,p['minimum_local_mib']+1024), 'Reclaim outside supplied target range: raise floor to measure within the comparison domain')
            # Do not fall through to lower floors, larger VMs or larger cold
            # batches when the correction was already tested.
            continue
        if 'policy_worker_busy' in flags and 'cold_readback_pressure' in flags and p['cold_folios'] > 1:
            add(anchor, 'cold_folios', max(1, p['cold_folios'] // 2), 'Policy worker busy with cold readback: reduce cold batch to test its cost')
        if 'free_reclaim_not_converged' in flags:
            if p['epoch_us'] > 1000 and d['coalesced_tick_fraction'] < .5:
                add(anchor, 'epoch_us', max(1000, p['epoch_us'] // 2), 'Unused local-floor headroom %.2f GiB; net free speed %.1f MiB/s: shorten cycle before enlarging VM' % (d['floor_headroom_gib'], d['net_free_mib_per_second']))
            if 'psi_returning_capacity' in flags:
                add(anchor, 'psi_ppm', min(100000, p['psi_ppm'] * 2), 'High-PSI epochs %.1f%% returned %.2f GiB: test a higher threshold' % (100*d['high_epoch_fraction'], d['free_returned_gib']))
        if 'cold_reclaim_not_observed' in flags or anchor.get('reference_status') == 'PASS':
            add(anchor, 'cold_folios', min(128, p['cold_folios'] * 2), 'Test more cold selection at unchanged VM, PSI, cycle and floor')
        if 'cold_readback_pressure' in flags and p['cold_folios'] > 1:
            add(anchor, 'cold_folios', max(1, p['cold_folios'] // 2), 'RDMA readback %.2f GiB vs peak retired %.2f GiB: test smaller cold batches' % (d['rdma_read_gib'], d['cold_peak_gib']))
        if 'local_floor_reached' in flags:
            add(anchor, 'minimum_local_mib', max(2048, p['minimum_local_mib'] - 1024), 'Measured local capacity reached floor: test a lower floor')
            larger = [m for m in app['memory_steps_mib'] if m > memory]
            if larger and not any(r.get('reference_status') == 'PASS' for r in same):
                add(anchor, 'minimum_local_mib', p['minimum_local_mib'], 'Reclamation reached local floor: larger VM can now provide reclaimable headroom', min(larger))
        # Controlled sensitivity probes, used only after evidence-directed alternatives.
        add(anchor, 'cold_folios', min(128, p['cold_folios'] * 2), 'Controlled cold-batch sensitivity probe; do not assume larger is better')
        add(anchor, 'psi_ppm', min(100000, p['psi_ppm'] * 2), 'Controlled PSI tolerance probe')
        add(anchor, 'epoch_us', min(1000000, p['epoch_us'] * 2), 'Controlled cycle-overhead probe')
    return proposals[0] if proposals else None
