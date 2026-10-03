"""Paths and module loading for the concurrent Figure 9 runner."""
import importlib.util
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
HA = ROOT / 'hyperalloc-6.18'
STATE = ROOT / 'ae/build/fig9-state'
VMROOT = STATE / 'hyperalloc'
SCRIPTS = ROOT / 'benchmarks/scripts'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def within(path, parent=ROOT):
    path = Path(path).resolve()
    if not path.is_relative_to(parent.resolve()):
        raise ValueError('Output must be inside ' + str(parent))
    return path


def configure_guest(guest):
    guest.ROOT = VMROOT
    def run_dir(access):
        own = Path(access['_path']).resolve().is_relative_to(VMROOT)
        return (VMROOT if own else HA) / 'build/running' / access['name']
    guest.run_dir = run_dir
    return guest


def prepare_layout():
    scripts = VMROOT / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    for name, target in {
        'run-deploy-vm.py': HERE / 'launch_vm.py',
        'hermit-guest.py': HA / 'scripts/hermit-guest.py',
        'disable-guest-auto-updates.py': HA / 'scripts/disable-guest-auto-updates.py',
    }.items():
        link = scripts / name
        if link.is_symlink() and link.resolve() == target.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise ValueError('Unexpected state file: ' + str(link))
        link.symlink_to(target)
    (VMROOT / 'build/guests').mkdir(parents=True, exist_ok=True)
    (VMROOT / 'build/running').mkdir(parents=True, exist_ok=True)
