#!/usr/bin/env python3
"""Persistently disable scheduled APT updates inside an experiment Guest."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

TIMERS = ('apt-daily.timer', 'apt-daily-upgrade.timer')
JOBS = ('apt-daily.service', 'apt-daily-upgrade.service')
MONITOR = 'unattended-upgrades.service'
UNITS = (*TIMERS, *JOBS, MONITOR)
LOCKS = ('/var/lib/dpkg/lock', '/var/lib/dpkg/lock-frontend',
         '/var/cache/apt/archives/lock', '/var/lib/apt/lists/lock')
CONFIG = Path('/etc/apt/apt.conf.d/99zz-chameleon-no-auto-updates')
OPTIONS = ('Enable', 'Update-Package-Lists', 'Download-Upgradeable-Packages',
           'Unattended-Upgrade')


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout


def state(unit):
    return dict(line.split('=', 1) for line in run(
        'systemctl', 'show', unit, '--property=ActiveState,UnitFileState').splitlines())


@contextlib.contextmanager
def package_locks():
    # Match APT/dpkg's POSIX locks. Never remove lock files or kill their owners.
    with contextlib.ExitStack() as stack:
        for name in LOCKS:
            if Path(name).exists():
                stream = stack.enter_context(open(name, 'r+'))
                fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def apply(timeout=300):
    if os.geteuid() != 0:
        raise PermissionError('Run as root inside the experiment Guest')
    # Masking does not stop an existing installation. Only timers stop now.
    run('systemctl', 'mask', *UNITS)
    run('systemctl', 'stop', *TIMERS)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    pending = CONFIG.with_suffix('.tmp')
    pending.write_text(''.join('APT::Periodic::' + key + ' "0";\n' for key in OPTIONS))
    pending.replace(CONFIG)
    deadline = time.monotonic() + timeout
    while True:
        busy = [unit for unit in JOBS if state(unit)['ActiveState'] not in ('inactive', 'failed')]
        if not busy:
            try:
                with package_locks():
                    # The shutdown monitor may hold an inhibitor; stop it only
                    # after package jobs and lock owners have finished.
                    run('systemctl', 'stop', MONITOR)
                    units = {unit: state(unit) for unit in UNITS}
                    if any(s.get('UnitFileState') != 'masked' or
                           s.get('ActiveState') not in ('inactive', 'failed') for s in units.values()):
                        raise RuntimeError('Automatic update units are not disabled: ' + str(units))
                    for key in OPTIONS:
                        if run('apt-config', 'shell', 'value', 'APT::Periodic::' + key).strip() != "value='0'":
                            raise RuntimeError('APT periodic setting is not disabled: ' + key)
                    return {'status': 'PASS', 'units': units, 'config': str(CONFIG),
                            'scope': 'Persistent Guest policy; manual package installation remains available'}
            except BlockingIOError:
                busy = ['APT/dpkg package lock']
        if time.monotonic() >= deadline:
            raise TimeoutError('Existing package update has not finished: ' + ', '.join(busy))
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error('--timeout must be positive')
    print(json.dumps(apply(args.timeout)))


if __name__ == '__main__':
    main()
