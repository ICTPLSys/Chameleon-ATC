#!/usr/bin/env python3
"""Boot the deployment Guest from a throwaway ext4 virtio disk and test it.

Uses the actual deployment launcher command, without physical VFIO or C4 host
transactions. Installs the packaged modules into the fixture before boot.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full', action='store_true', help='Also run allocator, C1/C2/C3; needs Host PEBS MEMINFO and kernel-deploy.py test-modules')
    args = parser.parse_args()
    vm = load('test_vm', 'test-vm.py')
    launcher = load('launcher', 'run-deploy-vm.py')
    output = ROOT / 'results/deployment/guest-disk'
    output.mkdir(parents=True, exist_ok=True)
    build = ROOT / 'build/deploy-guest/disk-test'
    stage = build / 'root'
    for name in ['bin', 'sbin', 'etc', 'dev', 'proc', 'sys', 'tmp']:
        (stage / name).mkdir(parents=True, exist_ok=True)
    shutil.copyfile('/bin/busybox', stage / 'bin/busybox')
    os.chmod(stage / 'bin/busybox', 0o755)
    for name in subprocess.check_output(['/bin/busybox', '--list'], text=True).split():
        if name != 'busybox':
            link = stage / 'bin' / name
            if not link.is_symlink():
                link.symlink_to('busybox')
    shutil.copytree(ROOT / 'build/deploy-guest/package/lib', stage / 'lib', dirs_exist_ok=True)
    shutil.copy2(ROOT / 'tests/memory_test', stage / 'memory_test')
    if args.full:
        (stage / 'tests').mkdir(exist_ok=True)
        for name in ['guest_allocator.ko', 'chameleon_shadow_backend.ko']:
            target = stage / name if name == 'guest_allocator.ko' else stage / 'tests' / name
            shutil.copy2(ROOT / 'build/deploy-guest/test-modules' / name, target)
        for name in ['chameleon_tracker', 'chameleon_manager', 'chameleon_shadow']:
            shutil.copy2(ROOT / 'tests' / name, stage / 'tests' / name)
    init = stage / 'init'
    init.write_text('''#!/bin/sh
export PATH=/bin:/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mount -t tmpfs tmpfs /tmp
mount -t debugfs debugfs /sys/kernel/debug
echo DEPLOY_DISK_READY
exec sh
''')
    init.chmod(0o755)
    disk = build / 'root.raw'
    with disk.open('wb') as stream:
        stream.truncate(768 * 1024 * 1024)
    subprocess.run(['mkfs.ext4', '-q', '-F', '-d', str(stage), str(disk)], check=True)
    config = launcher.config(dict(name='deploy-disk-test', disk=str(disk), disk_format='raw',
                                  root='/dev/vda', memory_mib=2048, network='none', pebs=args.full,
                                  chameleon=False, policy=False, append='init=/init panic=1'))
    config_file = build / 'vm.json'
    config_file.write_text(json.dumps(config, indent=2) + '\n')
    # Validate all regular launcher prerequisites. No VF is bound by this test.
    report = dict(preflight=launcher.preflight(config), status='RUNNING', physical_rdma='NOT_RUN')
    cmd = launcher.command(config)
    run_dir = ROOT / 'build/running/deploy-disk-test'
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'qmp.sock').unlink(missing_ok=True)
    (output / 'command.json').write_text(json.dumps(cmd, indent=2) + '\n')
    with (output / 'qemu.log').open('w') as log:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        console = vm.Console(process.stdout.fileno(), process.stdin.fileno(), output / 'serial.log')
        try:
            qmp = vm.QMP(str(run_dir / 'qmp.sock'))
            console.wait(r'^DEPLOY_DISK_READY$', timeout=120)
            text = console.command('uname -r; mount; cat /proc/cmdline; cat /proc/llfree_protocol')
            assert '6.18.0-chameleon-guest' in text and ' on / type ext4' in text and 'root=/dev/vda ' in text, text
            report['guest_kernel'] = '6.18.0-chameleon-guest'
            report['root_device'] = '/dev/vda ext4'
            text = console.command('modprobe mlx5_ib && modprobe ib_ipoib && modprobe rdma_cm && modprobe ib_uverbs && lsmod')
            assert 'mlx5_ib' in text and 'mlx5_core' in text and 'ib_ipoib' in text, text
            report['mlx5_module_dependencies'] = 'PASS (no physical NIC attached)'
            console.command('modprobe rswap_client backend=dram pool_mb=4 && cat /sys/kernel/debug/hermit/stats && rmmod rswap_client')
            report['matching_hermit_module_load_unload'] = 'PASS (DRAM backend)'
            text = console.command('/memory_test mthp', timeout=180)
            assert text.count('PASS mthp size_kb=') == 8, text
            assert text.count('PASS split source_kb=') == 9, text
            report['guest_mthp_kib'] = [16, 32, 64, 128, 256, 512, 1024, 2048]
            report['mthp_native_split'] = 'PASS'
            report['initial_balloon'] = qmp.execute('query-llfree-balloon')
            report['shrink'] = qmp.resize(1024)
            console.command('/memory_test touch 384', timeout=120)
            report['return'] = qmp.resize(2048)
            console.command('/memory_test touch 768', timeout=120)
            report['hyperalloc_resize_data_integrity'] = 'PASS'
            if args.full:
                console.command('insmod /guest_allocator.ko && echo HYPERALLOC_READY')
                report['allocator_hyperalloc_regression'] = {}
                vm.run_tests(console, qmp, 'manual', 2048, report['allocator_hyperalloc_regression'])
                milestones = load('milestones', 'test-chameleon.py')
                for name in ['c1', 'c2', 'c3']:
                    report[name] = milestones.run_milestone(console, output, name)
            dmesg = console.command('dmesg')
            assert not any(marker in dmesg for marker in ['BUG:', 'WARNING:', 'Oops:', 'Kernel panic']), dmesg
            report['status'] = 'PASS'
        except Exception as error:
            report.update(status='FAIL', error=str(error))
            raise
        finally:
            process.terminate()
            process.wait(timeout=30)
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
