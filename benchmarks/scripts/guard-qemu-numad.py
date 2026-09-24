#!/usr/bin/env python3
"""Exclude one named experiment VM from numad while its QEMU PID changes.

Run as root alongside a campaign. Does not stop numad or change CPU masks.
Exclusions are removed when QEMU exits or the guard stops.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def identity(run_dir, proc_root=Path('/proc')):
    try:
        pid = int((run_dir/'qemu.pid').read_text())
        proc = proc_root/str(pid)
        argv = (proc/'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
        if (proc.stat().st_uid != run_dir.stat().st_uid or
                'qemu-system' not in (proc/'exe').resolve().name or
                '-name' not in argv or argv[argv.index('-name')+1] != run_dir.name or
                not any(str(run_dir/'qmp.sock') in arg for arg in argv)):
            return None
        start = (proc/'stat').read_text().rsplit(')', 1)[1].split()[19]
        return pid, start
    except (OSError, ValueError, IndexError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--stop-file', type=Path, required=True)
    parser.add_argument('--duration', type=int, default=14400)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('numad exclusion requires root')
    daemon = int(Path('/run/numad.pid').read_text())
    if (Path('/proc')/str(daemon)/'comm').read_text().strip() != 'numad':
        parser.error('existing numad daemon required; guard never starts it')
    args.run_dir = args.run_dir.resolve()
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def command(flag, pid):
        subprocess.run(['/usr/bin/numad', flag, str(pid)], check=True,
                       capture_output=True, text=True, timeout=10)
        print(json.dumps({'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                          'action':flag,'pid':pid}), flush=True)
    previous = None
    deadline = time.monotonic() + args.duration
    print(json.dumps({'status':'READY','pid':os.getpid(),'run_dir':str(args.run_dir),
                      'numad_pid':daemon}), flush=True)
    try:
        while not stopping and not args.stop_file.exists() and time.monotonic() < deadline:
            current = identity(args.run_dir)
            if current != previous:
                if previous is not None:
                    command('-r', previous[0])
                    previous = None
                if current is not None:
                    command('-x', current[0])
                    previous = current
            time.sleep(0.1)
    finally:
        if previous is not None:
            command('-r', previous[0])
        print(json.dumps({'status':'STOPPED'}), flush=True)


if __name__ == '__main__':
    main()
