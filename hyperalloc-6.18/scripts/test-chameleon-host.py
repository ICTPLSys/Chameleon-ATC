#!/usr/bin/env python3
"""Exercise the modified KVM through real vCPUs inside an isolated L1 Host."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='chameleon-c4-host')
    parser.add_argument('--initramfs', type=Path, default=ROOT / 'build/chameleon-host-control.cpio.gz')
    parser.add_argument('--kernel', type=Path, default=ROOT / 'build/host/arch/x86/boot/bzImage')
    parser.add_argument('--expected-release', default='6.18.0-hyperalloc-host',
                        help='Required Host release prefix (deployment: 6.18.0-chameleon-host)')
    parser.add_argument('--tuning', action='store_true', help='Require new Host debugfs tuning checks too')
    args = parser.parse_args()
    output = ROOT / 'results' / ('vm-' + args.name)
    output.mkdir(parents=True, exist_ok=True)
    kernel = args.kernel.resolve()
    command = ['/usr/bin/qemu-system-x86_64', '-accel', 'kvm', '-cpu', 'host',
               '-m', '4096', '-smp', '8', '-nodefaults', '-display', 'none',
               '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
               '-kernel', str(kernel), '-initrd', str(args.initramfs),
               '-append', 'console=ttyS0 rdinit=/init panic=1 nokaslr']
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    host_config = kernel.parents[3] / '.config'
    inputs = [kernel, args.initramfs, ROOT / 'tests/kvm-chameleon-control',
              ROOT / 'tests/kvm-chameleon-control.c', host_config,
              ROOT / 'linux/include/uapi/linux/kvm.h']
    if args.tuning:
        inputs += [ROOT / 'tests/kvm-chameleon-tuning', ROOT / 'tests/kvm-chameleon-tuning.c']
    report = {'status': 'RUNNING', 'scope': 'real L1 KVM control and L2 test vCPUs',
              'physical_host': subprocess.check_output(['uname', '-r'], text=True).strip(),
              'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT.parent, text=True).strip(),
              'input_sha256': {str(p.relative_to(ROOT) if ROOT in p.parents else p): digest(p) for p in inputs}}
    started = time.monotonic()
    process = None
    try:
        with (output / 'serial.log').open('w') as serial, (output / 'qemu.log').open('w') as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=serial, stderr=log)
            if process.wait(timeout=240):
                raise RuntimeError('Outer QEMU exited unsuccessfully')
        text = (output / 'serial.log').read_text(errors='replace')
        kernel_match = re.search(r'^NESTED_HOST_KERNEL (\S+)$', text, re.MULTILINE)
        if not kernel_match or not kernel_match.group(1).startswith(args.expected_release):
            raise RuntimeError('Modified L1 Host version evidence missing')
        report['host_kernel'] = kernel_match.group(1)
        if 'NESTED_HOST_MODULES' in text:
            for module in ('kvm', 'vfio_pci', 'mlx5_ib', 'ib_ipoib'):
                if not re.search(r'^' + module + r' \d+ ', text, re.MULTILINE):
                    raise RuntimeError(f'Deployment module failed to load: {module}')
            report['deployment_modules'] = 'KVM, VFIO PCI, mlx5 RDMA, IPoIB loaded'
        passed = re.search(r'^PASS KVM_CHAMELEON checks=(\d+)$', text, re.MULTILINE)
        if not passed or 'NESTED_CHAMELEON_CONTROL_EXIT status=0' not in text:
            raise RuntimeError('Actual KVM control test did not pass')
        for phase in ('controls', 'actual_batch', 'uncertain_failure', 'partial_results', 'scalable_retirement'):
            if not re.search(r'^PASS KVM_CHAMELEON ' + phase + r'\b', text, re.MULTILINE):
                raise RuntimeError(f'Missing actual {phase} test evidence')
        if 'CHAMELEON_TRACE_READY value=1' not in text:
            raise RuntimeError('Actual KVM Chameleon tracepoints were not enabled')
        match = re.search(r'CHAMELEON_TRACE_BEGIN\n(.*?)CHAMELEON_TRACE_END', text, re.DOTALL)
        if not match or not re.search(r'\bkvm_chameleon\w*:', match.group(1)):
            raise RuntimeError('No actual transaction trace events captured')
        (output / 'host-trace.log').write_text(match.group(1))
        dmesg = re.search(r'CHAMELEON_HOST_DMESG_BEGIN\n(.*?)CHAMELEON_HOST_DMESG_END', text, re.DOTALL)
        if not dmesg:
            raise RuntimeError('Complete Host diagnostics missing')
        (output / 'dmesg.log').write_text(dmesg.group(1))
        if re.search(r'BUG:|WARNING:|kernel BUG|Kernel panic|Oops:|Bad page state|general protection fault', text):
            raise RuntimeError('Host kernel diagnostics failed')
        if args.tuning:
            tuning = re.search(r'^PASS KVM_CHAMELEON_TUNING checks=(\d+)\b', text, re.MULTILINE)
            if not tuning or 'NESTED_TUNING_EXIT=0' not in text:
                raise RuntimeError('Host tuning test did not pass')
            report['tuning_checks'] = int(tuning.group(1))
        report.update(status='PASS', checks=int(passed.group(1)), kernel_diagnostics='clean',
                      batch_evidence=re.findall(r'^EPT_BATCH .*$', text, re.MULTILINE))
    except Exception as error:
        report.update(status='FAIL', error=repr(error))
        raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
