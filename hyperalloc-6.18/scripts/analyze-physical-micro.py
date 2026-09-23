#!/usr/bin/env python3
"""Audit physical microbenchmark mechanisms against QMP and QEMU event logs.

PASS means the event evidence reconciles with the saved counters. It does not
mean that every mechanism occurred, or that the workload met its capacity goal.
No Guest commands are executed by this analyzer.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re


PATTERNS = {
    'begin': re.compile(r'chameleon begin-complete batch=(\d+) transaction=(\d+) ranges=(\d+)'),
    'discard': re.compile(r'chameleon discard transaction=(\d+) token=(\d+) gpa=(0x[0-9a-fA-F]+) pages=(\d+) status=(-?\d+)'),
    'retired': re.compile(r'chameleon retired token=(\d+) pages=(\d+) mincore_resident=(\d+) rc=(-?\d+)'),
    'installed': re.compile(r'chameleon installed token=(\d+) gpa=(0x[0-9a-fA-F]+) pages=(\d+)'),
}
NAMES = {
    'begin': ('batch', 'transaction', 'ranges'),
    'discard': ('transaction', 'token', 'gpa', 'pages', 'status'),
    'retired': ('token', 'pages', 'resident', 'rc'),
    'installed': ('token', 'gpa', 'pages'),
}
COUNTERS = {
    'manager': ('split_ok', 'split_busy', 'collapse_ok', 'collapse_fail', 'selected_mixed', 'selected_native', 'promoted', 'aged'),
    'shadow': ('prepare_pmd_demotions', 'load_demand_attempts', 'load_background_attempts', 'data_fault_restores',
               'demand_fault_successes', 'demand_fault_failures', 'data_save_failure', 'data_load_failure',
               'range_install_failure', 'data_save_success', 'data_load_success', 'range_install_success',
               'psi_fault_enter', 'psi_fault_leave', 'psi_fault_ns', 'commit_rollback_attempts',
               'commit_rollback_success', 'commit_rollback_failures'),
    'policy': ('high_epochs', 'low_epochs', 'shadow_prepared_objects', 'shadow_prepared_pages', 'shadow_restored_pages',
               'free_reclaimed_bytes', 'free_returned_bytes', 'psi_errors', 'action_errors'),
    'hermit': ('store_success', 'load_success', 'bytes_written', 'bytes_read', 'store_failures', 'load_failures',
               'callback_errors', 'canceled'),
    'rdma': ('write_wrs', 'read_wrs', 'write_completions', 'read_completions', 'write_bytes', 'read_bytes',
             'transfer_errors', 'map_failures', 'map_retries'),
    'tracker': ('hardware_samples', 'accepted_samples', 'synthetic_samples', 'cooling_epochs'),
}
QMP_COUNTERS = ('begin-batches', 'begin-flushes', 'discarded-pages', 'installed-pages', 'install-failures',
                'gate-cancellations', 'batch-triggers', 'watermark-triggers')


def histogram(values):
    return {str(k): v for k, v in sorted(Counter(values).items())}


def counter_delta(first, last):
    result = {}
    for group, keys in COUNTERS.items():
        a, b = first.get(group, {}), last.get(group, {})
        result[group] = {key: b[key] - a[key] for key in keys
                         if isinstance(a.get(key), int) and isinstance(b.get(key), int)}
    a, b = (snapshot.get('qmp', {}).get('chameleon', {}) for snapshot in (first, last))
    result['host'] = {key: b[key] - a[key] for key in QMP_COUNTERS
                      if isinstance(a.get(key), int) and isinstance(b.get(key), int)}
    return result


def parse_latest_epoch(transcript):
    """A restart is a new BEGIN transaction 1 after a larger transaction.

    Equal IDs are duplicates, not a restart. A descending ID other than 1 is
    retained and reported as bad evidence. Never combine IDs across boots.
    """
    epochs, events, previous = [], [], None
    for line, text in enumerate(transcript.splitlines(), 1):
        for kind, pattern in PATTERNS.items():
            match = pattern.search(text)
            if not match:
                continue
            event = {'kind': kind, 'line': line}
            event.update({name: int(value, 16 if name == 'gpa' else 10)
                          for name, value in zip(NAMES[kind], match.groups())})
            if kind == 'begin':
                tx = event['transaction']
                if tx == 1 and previous is not None and previous > 1:
                    epochs.append(events)
                    events = []
                previous = tx
            events.append(event)
            break
    epochs.append(events)
    return epochs[-1], len(epochs) - 1


def analyze(report, transcript):
    result = {'schema': 1, 'status': 'FAIL', 'report_status': report.get('status'),
              'checks': {}, 'errors': [], 'semantics': {
                  'status': 'Evidence consistency only; observed flags and original report status are separate.',
                  'flush': 'Dedicated KVM remote TLB flush calls, not per-vCPU INVEPT instruction counts.',
                  'split': 'manager split_ok is physical HHH splitting; prepare_pmd_demotions is page-table demotion.',
                  'psi': 'Background restoration during disable is not evidence of PSI-driven restoration.',
                  'phase': 'Each interval subtracts the preceding saved phase; no counter reset is silently accepted.'}}
    checks, errors = result['checks'], result['errors']

    def check(key, value, reason):
        checks[key] = bool(value)
        if not value:
            errors.append(reason)

    phases = report.get('phases', {})
    check('endpoint_snapshots', all(name in phases for name in ('baseline', 'restored')),
          'Need baseline and restored phase snapshots.')
    if not checks['endpoint_snapshots']:
        return result
    first, last = phases['baseline'], phases['restored']
    qa, qb = (snapshot.get('qmp', {}).get('chameleon', {}) for snapshot in (first, last))
    mandatory = ('begin-batches', 'begin-flushes', 'discarded-pages', 'installed-pages')
    check('endpoint_host_counters', all(isinstance(q.get(k), int) for q in (qa, qb) for k in mandatory),
          'Missing baseline/restored Host counters; event evidence cannot be reconciled.')
    if not checks['endpoint_host_counters']:
        return result
    start, end = qa['begin-batches'], qb['begin-batches']
    check('transaction_interval', end >= start, 'Host BEGIN counters regressed between snapshots.')
    result['transaction_interval_exclusive_inclusive'] = [start, end]
    missing = result['missing_counters'] = {}
    for endpoint, snapshot in [('baseline', first), ('restored', last)]:
        for group, keys in COUNTERS.items():
            absent = [key for key in keys if not isinstance(snapshot.get(group, {}).get(key), int)]
            if absent:
                missing[endpoint + '.' + group] = absent
    check('counter_schema_complete', not missing,
          'Missing cumulative mechanism counters; see missing_counters instead of assuming omitted counters are zero.')
    total_delta = result['counter_delta'] = counter_delta(first, last)
    check('counters_monotonic', all(v >= 0 for group in total_delta.values() for v in group.values()),
          'A cumulative mechanism counter reset/regressed; deltas cannot be interpreted.')
    if 'session' in qa and 'session' in qb:
        check('same_session', qa['session'] == qb['session'], 'Host Chameleon session changed during the test.')

    events, resets = parse_latest_epoch(transcript)
    all_begins = [event for event in events if event['kind'] == 'begin']
    ids = [event['transaction'] for event in all_begins]
    check('latest_epoch_order', all(b > a for a, b in zip(ids, ids[1:])),
          'Latest transaction epoch contains duplicate or descending BEGIN IDs.')
    result['log_epoch'] = {'discarded_older_epochs': resets,
                           'first_begin_line': all_begins[0]['line'] if all_begins else None,
                           'first_transaction': ids[0] if ids else None,
                           'last_transaction': ids[-1] if ids else None}
    begins = [event for event in all_begins if start < event['transaction'] <= end]
    selected_ids = [event['transaction'] for event in begins]
    check('complete_begin_interval', selected_ids == list(range(start + 1, end + 1)),
          'BEGIN evidence is missing, duplicated, or from a different transaction epoch.')
    begin_by_id = {event['transaction']: event for event in begins}
    # Stop at the next transaction after this run so later test records cannot
    # manufacture matching restoration evidence for this run's objects.
    end_line = next((event['line'] for event in all_begins if event['transaction'] > end), float('inf'))
    begin_line = begins[0]['line'] if begins else float('inf')
    selected_events = [event for event in events if begin_line <= event['line'] < end_line]
    discards = [event for event in selected_events if event['kind'] == 'discard'
                and event['transaction'] in begin_by_id]
    pairs = [(event['transaction'], event['token']) for event in discards]
    tokens = [event['token'] for event in discards]
    check('unique_discards', len(set(pairs)) == len(pairs) and len(set(tokens)) == len(tokens),
          'Duplicate discard object evidence within the selected interval.')
    discards_per_tx = Counter(event['transaction'] for event in discards)
    check('every_begin_range_logged', all(discards_per_tx[event['transaction']] == event['ranges'] and event['ranges'] > 0
                                         for event in begins),
          'A BEGIN range has no matching discard result (or has duplicate discard results).')
    check('successful_discards', all(event['status'] == 0 for event in discards),
          'At least one discard failed; do not treat attempted ranges as released memory.')
    check('valid_folio_sizes', all(event['pages'] > 0 and event['pages'] <= 512 and
                                  not event['pages'] & (event['pages'] - 1) and event['pages'] != 2 for event in discards),
          'Discard log has an invalid or unsupported anonymous folio size.')
    successful = [event for event in discards if event['status'] == 0]
    objects = {event['token']: event for event in successful}
    retired = [event for event in selected_events if event['kind'] == 'retired' and event['token'] in objects]
    installed = [event for event in selected_events if event['kind'] == 'installed' and event['token'] in objects]
    check('retired_evidence', len(retired) == len(objects) and len({e['token'] for e in retired}) == len(objects)
          and all(e['pages'] == objects[e['token']]['pages'] and e['line'] > objects[e['token']]['line']
                  and e['resident'] == 0 and e['rc'] == 0 for e in retired),
          'Retired objects need exactly one successful mincore-zero record each.')
    retired_by_token = {e['token']: e for e in retired}
    check('installed_evidence', len(installed) == len(objects) and len({e['token'] for e in installed}) == len(objects)
          and all(e['token'] in retired_by_token and e['line'] > retired_by_token[e['token']]['line']
                  and e['pages'] == objects[e['token']]['pages'] and e['gpa'] == objects[e['token']]['gpa'] for e in installed),
          'Each retired object needs a later matching successful INSTALL record.')
    pages = sum(event['pages'] for event in successful)
    check('discarded_page_total', pages == qb['discarded-pages'] - qa['discarded-pages'],
          'Successful discard page total differs from QMP cumulative discarded-pages delta.')
    check('installed_page_total', sum(event['pages'] for event in installed) == qb['installed-pages'] - qa['installed-pages'],
          'Matching INSTALL page total differs from QMP installed-pages delta; baseline ownership or evidence may be incomplete.')
    flushes = qb['begin-flushes'] - qa['begin-flushes']
    check('dedicated_flush_count', len(begins) == flushes == end - start,
          'Dedicated BEGIN/flush deltas do not equal the number of logged successful transactions.')
    for endpoint, qstate in [('baseline', qa), ('restored', qb)]:
        if 'retired-pages' in qstate:
            check(endpoint + '_retired_empty', qstate['retired-pages'] == 0,
                  endpoint + ' has preexisting or residual retired pages; cumulative restoration totals are not isolated.')
    maximum = max(begins, key=lambda event: event['ranges'], default=None)
    example = dict(maximum) if maximum else None
    if example:
        example['pages'] = sum(e['pages'] for e in discards if e['transaction'] == example['transaction'])
    result['ept'] = {
        'mode': qa.get('ept-mode'), 'begin_count': len(begins), 'dedicated_flush_count': flushes,
        'range_count': sum(e['ranges'] for e in begins),
        'multi_range_begin_count': sum(e['ranges'] > 1 for e in begins),
        'ranges_per_begin_histogram': histogram(e['ranges'] for e in begins),
        'maximum_ranges': maximum['ranges'] if maximum else 0,
        'maximum_example': example,
        'ranges_per_flush': sum(e['ranges'] for e in begins) / flushes if flushes > 0 else None,
    }
    result['retirement'] = {
        'successful_objects': len(successful), 'successful_pages': pages,
        'pages_histogram': histogram(e['pages'] for e in successful),
        'order_histogram': histogram(e['pages'].bit_length() - 1 for e in successful if e['pages'] > 0),
        'status_histogram': histogram(e['status'] for e in discards),
        'sub_2mib_objects': sum(e['pages'] < 512 for e in successful),
        'sub_2mib_examples': [e for e in successful if e['pages'] < 512][:16],
        'retired_mincore_zero_records': len(retired), 'installed_records': len(installed),
    }
    ordered = [(name, snapshot) for name, snapshot in phases.items() if 'seconds' in snapshot]
    ordered.sort(key=lambda item: item[1]['seconds'])
    intervals = result['phase_intervals'] = {}
    for (a_name, a), (b_name, b) in zip(ordered, ordered[1:]):
        ah, bh = (s.get('qmp', {}).get('chameleon', {}) for s in (a, b))
        interval = intervals[a_name + '->' + b_name] = {
            'seconds': b['seconds'] - a['seconds'], 'counter_delta': counter_delta(a, b),
            'retired_pages_before': ah.get('retired-pages'), 'retired_pages_after': bh.get('retired-pages'),
            'batch_pages_before': ah.get('batch-pages'), 'batch_pages_after': bh.get('batch-pages')}
        lo, hi = ah.get('begin-batches'), bh.get('begin-batches')
        if isinstance(lo, int) and isinstance(hi, int):
            phase_begins = [e for e in begins if lo < e['transaction'] <= hi]
            phase_discards = [e for e in successful if lo < e['transaction'] <= hi]
            interval['ept'] = {
                'begin_count': len(phase_begins),
                'dedicated_flush_count': interval['counter_delta']['host'].get('begin-flushes'),
                'range_count': sum(e['ranges'] for e in phase_begins),
                'multi_range_begin_count': sum(e['ranges'] > 1 for e in phase_begins),
                'maximum_ranges': max((e['ranges'] for e in phase_begins), default=0),
                'ranges_per_begin_histogram': histogram(e['ranges'] for e in phase_begins),
                'ranges_per_flush': (sum(e['ranges'] for e in phase_begins) / len(phase_begins)
                                     if phase_begins else None),
                'successful_discarded_pages': sum(e['pages'] for e in phase_discards),
                'discarded_order_histogram': histogram(e['pages'].bit_length() - 1 for e in phase_discards if e['pages'] > 0),
            }
    check('phase_counters_monotonic', all(value >= 0 for interval in intervals.values()
          for group in interval['counter_delta'].values() for value in group.values()),
          'A cumulative mechanism counter regressed between intermediate phases.')
    result['observed'] = {
        'retirement_and_restore': bool(successful) and checks['retired_evidence'] and checks['installed_evidence'],
        'physical_folio_split': total_delta['manager'].get('split_ok', 0) > 0,
        'small_folio_retirement': any(e['pages'] < 512 for e in successful),
        'multi_range_ept_batch': any(e['ranges'] > 1 for e in begins),
        'demand_load': total_delta['shadow'].get('load_demand_attempts', 0) > 0,
        'psi_high_epochs': total_delta['policy'].get('high_epochs', 0) > 0,
        'psi_low_epochs': total_delta['policy'].get('low_epochs', 0) > 0,
    }
    result['status'] = 'PASS' if all(checks.values()) else 'FAIL'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--serial-log', type=Path, help='default: qemu-events.log next to report')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    log = args.serial_log or args.report.parent / 'qemu-events.log'
    result = analyze(json.loads(args.report.read_text()), log.read_text(errors='replace'))
    result['source'] = {'report': str(args.report), 'serial_log': str(log)}
    text = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end='')
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
