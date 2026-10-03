import argparse
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from chameleon_mix.common import HERE, ROOT, HA, STATE, load, prepare_layout
from chameleon_mix.runtime import rt, engine
from chameleon_mix.concurrent_run import run_mix
from chameleon_mix import placement

engine.run_mix = run_mix
data = engine.data


def configurations(path):
    high = json.loads(path.read_text())
    if high.get('schema') != 'chameleon-ae-frozen-high-v1':
        raise ValueError('Expected frozen high configuration')
    high.setdefault('normalization', 'bundled-isolated-tracked-all-local')
    return high


def isolated_inventory(source):
    inventory = copy.deepcopy(source)
    inventory.pop('single_slot', None)
    inventory['fig9_execution'] = {mix: 'three-concurrent' for mix in data.MIXES}
    for slot in inventory['slots']:
        slot['name'] = 'co-' + slot['name']
    rt.validate_inventory(inventory)
    return inventory


def build_plan(high, source, mixes, snapshot=None, topology=None, allowed=None):
    inventory = isolated_inventory(source)
    snapshot = rt.numa_memory_snapshot() if snapshot is None else snapshot
    result = {'protocol': 'fig9-three-concurrent-high-v1', 'mixes': {}, 'inventory': inventory,
              'normalization': high['normalization'],
              'metric': 'Arithmetic mean of the three application slowdown percentages relative to isolated all-local',
              'reference': data.reference_data(ROOT / 'ae/results_baselines/fig9.json')}
    for mix in mixes:
        apps = [copy.deepcopy(high['applications'][case]) for case in data.MIXES[mix]]
        for app in apps:
            app['configuration'].update(high.get('mix_configurations', {}).get(mix, {}).get(app['application'], {}))
            if app['remote_pool_mib'] != 24576:
                raise ValueError('Each VM requires a 24576-MiB RDMA pool')
            data._finite(app['all_local']['performance']['cost'], 'all-local cost', positive=True)
        resources = placement.plan(apps, inventory, snapshot, rt.memory_reserves(inventory), topology, allowed)
        phase = {'applications': apps, 'slot_indices': [0, 1, 2], **resources}
        result['mixes'][mix] = {'applications': apps, 'phases': [phase], **resources,
                              'execution_mode': 'three-concurrent', 'simultaneous_vms': 3,
                              'total_vm_memory_mib': sum(a['vm_memory_mib'] for a in apps)}
    return result


def summarize(out, repeats):
    ae = load('fig9_summary', ROOT / 'ae/scripts/ae_fig9.py')
    ae.finish(out, repeats)
    return json.loads((out / 'summary.json').read_text())


def prepare_generator():
    build = ROOT / 'ae/build/fig9-generator'
    subprocess.run(['cmake', '-S', str(HERE / 'generator'), '-B', str(build),
                    '-DCMAKE_BUILD_TYPE=Release'], check=True)
    subprocess.run(['cmake', '--build', str(build), '--parallel', '4'], check=True)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--name', default=time.strftime('ae9-%Y%m%d-%H%M%S'))
    p.add_argument('--inventory', type=Path, default=ROOT / 'ae/config/host.json')
    p.add_argument('--qualified', '--config', type=Path, default=ROOT / 'ae/config/fig9-highs.json')
    p.add_argument('--reference-plot', type=Path, default=ROOT / 'ae/results_baselines/fig9.json')
    p.add_argument('--mixes', nargs='+', choices=list(data.MIXES), default=list(data.MIXES))
    p.add_argument('--repeats', type=int, choices=(1, 2, 3), default=3)
    p.add_argument('--timeout', type=int, default=14400)
    p.add_argument('--output-dir', type=Path)
    p.add_argument('--results-root', type=Path)
    p.add_argument('--performance-mode', action='store_true')
    p.add_argument('--no-plot', action='store_true')
    p.add_argument('--baseline-level', choices=('50', '75'), default='75')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--run', action='store_true')
    a = p.parse_args(argv)
    if a.timeout < 60 or len(a.mixes) != len(set(a.mixes)):
        p.error('Need timeout >= 60 seconds and distinct Mixes')
    high = configurations(a.qualified)
    source_inventory = json.loads(a.inventory.read_text())
    plan = build_plan(high, source_inventory, a.mixes)
    plan['reference'] = data.reference_data(a.reference_plot)
    plan['repeats'] = a.repeats
    if not (a.run or a.check or a.prepare):
        print(json.dumps(plan, indent=2))
        return 0
    template = rt.guest.load_access(HA / 'build/guests' / source_inventory['template_vm'] / 'access.json')
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'experiment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (template['_path'].parent / 'control.lock').open('r') as template_lock:
            fcntl.flock(template_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for slot in source_inventory['slots'] + ([source_inventory['single_slot']] if 'single_slot' in source_inventory else []):
                old = HA / 'build/guests' / slot['name'] / 'access.json'
                if old.exists():
                    access = rt.guest.load_access(old)
                    if rt.guest.active(access) or not rt.guest.launcher_idle(access):
                        raise ValueError('Existing AE VM is active: ' + slot['name'])
            source = engine.hardware_check(plan['inventory'], plan, template)
            if a.check:
                print(json.dumps({'status': 'PASS', 'mixes': a.mixes, 'plan': plan}, indent=2))
                return 0
            prepare_generator()
            prepare_layout()
            first = plan['mixes'][a.mixes[0]]
            configs = [rt.slot_config(source, slot, app, first['cpus']['applications'][app['application']])
                       for slot, app in zip(plan['inventory']['slots'], first['applications'])]
            rt.prepare_slots(plan['inventory'], template, configs)
            if a.prepare:
                return 0
            out = a.output_dir.resolve() if a.output_dir else ROOT / 'ae/results' / a.name
            out.mkdir(parents=True, exist_ok=False)
            rt.save(out / 'plan.json', plan)
            rt.save(out / 'manifest.json', {'repeats': a.repeats, 'uid': os.getuid(), 'started_unix_seconds': time.time()})
            def stop(signum, frame):
                raise KeyboardInterrupt('signal ' + str(signum))
            previous = signal.signal(signal.SIGTERM, stop)
            try:
                engine.run_campaign(plan, plan['inventory'], source, out, a.name, a.repeats, a.timeout,
                                    a.results_root or out / 'apps', True, {'execution_mode': 'three-concurrent'})
            finally:
                signal.signal(signal.SIGTERM, previous)
            if not a.no_plot:
                subprocess.run([sys.executable, str(ROOT / 'benchmarks/scripts/plot-chameleon-fig9.py'),
                                '--directory', str(out), '--baseline-level', a.baseline_level,
                                '--errorbars', 'none'], check=True)
            print('RESULT ' + str(out / 'report.json'))
    return 0
