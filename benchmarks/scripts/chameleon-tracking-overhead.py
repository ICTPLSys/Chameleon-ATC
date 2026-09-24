"""Paired all-local PEBS + HHH overhead acceptance, with mechanism evidence."""
import statistics


def evaluate(pairs, mean_limit=2.0, single_limit=5.0):
    values = []
    for pair in pairs:
        off, on = pair['off'], pair['on']
        if off.get('status') != 'PASS' or on.get('status') != 'PASS':
            return {'status': 'INVALID', 'reason': 'Failed workload, mechanism or cleanup check'}
        if off['memory_mib'] != on['memory_mib'] or off['workload_configuration'] != on['workload_configuration'] or off.get('workload_identity') != on.get('workload_identity'):
            return {'status': 'INVALID', 'reason': 'VM/workload mismatch'}
        item = {'slowdown_percent': 100 * (on['performance']['cost'] / off['performance']['cost'] - 1)}
        if 'p95_us' in on['performance']:
            item['p95_slowdown_percent'] = 100 * (on['performance']['p95_us'] / off['performance']['p95_us'] - 1)
        values.append(item)
    if len(values) != 3:
        return {'status': 'INCOMPLETE', 'pairs': values, 'reason': 'Require three pairs'}
    metrics = {key: {'mean_percent': statistics.mean(v[key] for v in values),
                     'maximum_percent': max(v[key] for v in values),
                     'minimum_percent': min(v[key] for v in values)} for key in values[0]}
    # Retain negative observations; never clip slowdowns or manufacture zero.
    passed = all(v['mean_percent'] <= mean_limit and v['maximum_percent'] <= single_limit for v in metrics.values())
    return {'status': 'PASS' if passed else 'FAIL', 'pairs': values, 'metrics': metrics,
            'mean_limit_percent': mean_limit, 'single_limit_percent': single_limit,
            'scope': 'Overhead only. Sampling quality and reclaim/slowdown curves require subsequent validation.'}
