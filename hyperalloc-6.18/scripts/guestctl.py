#!/usr/bin/env python3
"""Manage a disk Guest directly through QEMU/QMP and SSH, without libvirt.

Use create-deploy-guest.py first, then --name NAME start --bootstrap --wait,
setup, install-kernel, stop, start --wait. Commands execute on the Guest.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
RELEASE = '6.18.0-chameleon-guest'


class QMPGreetingError(ValueError):
    """Peer did not send a QMP greeting; not proof that the VM exited."""


def load_access(path):
    path = Path(path).expanduser().resolve()
    a = json.loads(path.read_text())
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', a['name']):
        raise ValueError('Invalid guest name')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', a['user']):
        raise ValueError('Invalid SSH username')
    if not re.fullmatch(r'[a-zA-Z0-9_.:][a-zA-Z0-9_.:-]*', a['host']):
        raise ValueError('Invalid SSH host')
    if type(a['port']) is not int or not 1 <= a['port'] <= 65535:
        raise ValueError('Invalid SSH port')
    for key in ('identity_file', 'known_hosts', 'vm_config', 'bootstrap_config'):
        p = Path(a[key]).expanduser()
        if not p.is_absolute():
            p = path.parent / p
        a[key] = str(p.resolve())
    if not Path(a['identity_file']).is_file():
        raise ValueError('SSH private key is missing: ' + a['identity_file'])
    a['_path'] = path
    return a


def ssh_options(a):
    known_hosts = a['known_hosts']
    if any(char in known_hosts for char in ('\n', '\r', '\0')):
        raise ValueError('Known-hosts path cannot contain newlines or NUL')
    # -o values use ssh_config syntax even when passed as a single argv item.
    # Without these quotes, a path containing spaces becomes several files.
    known_hosts = '"' + known_hosts.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return ['-F', '/dev/null', '-i', a['identity_file'],
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'UserKnownHostsFile=' + known_hosts,
            '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3']


def ssh_command(a, command=None, shell=None, tty=False):
    if command is not None and shell is not None:
        raise ValueError('Choose an argument list or --shell')
    result = ['ssh', *ssh_options(a), '-p', str(a['port']), '-l', a['user']]
    result += ['-t'] if tty else ['-T']
    result += [a['host']]
    if shell is not None:
        result.append(shell)
    elif command is not None:
        result.append(shlex.join(list(map(str, command))))
    return result


def remote(a, command=None, shell=None, **kwargs):
    return subprocess.run(ssh_command(a, command, shell), check=True, **kwargs)


def sftp_command(a):
    host = '[' + a['host'] + ']' if ':' in a['host'] else a['host']
    return ['sftp', *ssh_options(a), '-P', str(a['port']), '-b', '-',
            a['user'] + '@' + host]


def sftp_path(value):
    value = str(value)
    if any(x in value for x in ('\n', '\r', '\0')):
        raise ValueError('SFTP paths cannot contain newlines or NUL')
    # Quoting suppresses SFTP glob expansion. Escape only the batch parser's
    # quote/backslash characters; an extra wildcard escape becomes literal.
    return '"' + ''.join('\\' + c if c in '\\"' else c for c in value) + '"'


def transfer(a, direction, source, destination, recursive=False):
    if direction not in ('upload', 'download'):
        raise ValueError('Transfer direction must be upload or download')
    if direction == 'upload':
        source = str(Path(source).expanduser().resolve())
        if not Path(source).exists():
            raise ValueError('Missing upload source: ' + source)
        if Path(source).is_dir() and not recursive:
            raise ValueError('Use --recursive to upload a directory')
    else:
        destination = str(Path(destination).expanduser().resolve())
    action = 'put' if direction == 'upload' else 'get'
    batch = f'{action} {"-R " if recursive else ""}-- {sftp_path(source)} {sftp_path(destination)}\n'
    subprocess.run(sftp_command(a), input=batch, text=True, check=True)


def run_dir(a):
    return ROOT / 'build/running' / a['name']


def qmp(a, execute, args=None, timeout=5):
    """One QMP command, ignoring asynchronous events and checking VM identity."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(run_dir(a) / 'qmp.sock'))
        with conn.makefile('rwb') as stream:
            def receive():
                line = stream.readline()
                if not line:
                    raise OSError('QMP connection closed')
                return json.loads(line)

            greeting = receive()
            if 'QMP' not in greeting:
                raise QMPGreetingError('Invalid QMP greeting: ' + repr(greeting))

            def call(name, arguments, ident):
                stream.write((json.dumps(dict(execute=name, arguments=arguments, id=ident)) + '\n').encode())
                stream.flush()
                while True:
                    reply = receive()
                    if reply.get('id') != ident:
                        continue
                    if 'error' in reply:
                        raise ValueError('QMP: ' + str(reply['error']))
                    return reply['return']

            call('qmp_capabilities', {}, 1)
            identity = call('query-name', {}, 2)
            if identity.get('name') != a['name']:
                raise ValueError('QMP socket belongs to a different VM')
            return call(execute, args or {}, 3)


def active(a, timeout=5):
    try:
        return qmp(a, 'query-status', timeout=timeout)
    except (FileNotFoundError, ConnectionRefusedError):
        return None


@contextlib.contextmanager
def control_lock(a):
    with (a['_path'].parent / 'control.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another Guest lifecycle or provisioning command is running') from error
        yield


def launcher_idle(a):
    lock = run_dir(a) / 'launcher.lock'
    if not lock.exists():
        return True
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def start(a, bootstrap=False, timeout=30):
    cfg = Path(a['bootstrap_config'] if bootstrap else a['vm_config'])
    data = json.loads(cfg.read_text())
    if data['name'] != a['name']:
        raise ValueError('VM config name and access.json name differ')
    if active(a) is not None or not launcher_idle(a):
        raise ValueError('Guest already running or starting; use status/stop first')
    log = a['_path'].parent / 'serial.log'
    def launch():
        with log.open('ab', buffering=0) as output:
            offset = output.tell()
            output.write(('\n--- start ' + time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + cfg.name + ' ---\n').encode())
            process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/run-deploy-vm.py'),
                                        '--config', str(cfg), '--run'],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       start_new_session=True)
        return process, offset
    process, offset = launch()
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            with log.open('rb') as stream:
                stream.seek(offset)
                attempt = stream.read().decode(errors='replace')
            tail = attempt[-4000:]
            busy = re.search(r'vfio [^\n]*failed to open /dev/vfio/\d+: Device or resource busy', attempt)
            # A just-exited VM can leave VFIO teardown pending briefly. Retry
            # only our exited launch, within the original deadline; never use
            # historical serial output or take over another live launcher.
            if busy and active(a) is None and launcher_idle(a):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                print('VFIO device still busy after shutdown; retrying startup', flush=True)
                time.sleep(min(2, remaining))
                if time.monotonic() >= deadline:
                    break
                process, offset = launch()
                continue
            raise ValueError(f'QEMU launcher exited {process.returncode}; {log}\n{tail}')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            state = active(a, timeout=min(5, remaining))
        except TimeoutError:
            # VFIO can publish the socket before RAM pinning finishes and QMP
            # can send its greeting. Only our own live launch may retry this;
            # status/stop and the pre-launch check must propagate timeouts.
            continue
        if state is not None:
            print(f'Started {a["name"]} ({cfg.name}); console: {log}', flush=True)
            return
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise TimeoutError(f'QEMU did not become ready in {timeout}s; inspect {log} and status')


def stop(a, force=False, timeout=120):
    if active(a) is None:
        if not launcher_idle(a):
            raise ValueError('Launcher is starting; retry after QMP is ready')
        print('Guest is stopped')
        return
    qmp(a, 'quit' if force else 'system_powerdown')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = active(a)
        except (ConnectionResetError, QMPGreetingError):
            # After our verified shutdown request, QMP may reset or no longer
            # produce a greeting. Neither proves exit: require an absent QMP
            # endpoint AND an idle launcher before returning success.
            time.sleep(0.5)
            continue
        if state is None and launcher_idle(a):
            print('Guest stopped')
            return
        time.sleep(0.5)
    raise TimeoutError('Guest did not shut down; inspect serial.log or use stop --force (unclean power-off)')


def wait_ssh(a, timeout=300):
    deadline = time.monotonic() + timeout
    last = ''
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(ssh_command(a, ['true']), capture_output=True, text=True,
                                    timeout=min(10, max(1, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            last = 'SSH connection timed out'
            continue
        if result.returncode == 0:
            return
        last = result.stderr.strip()
        if 'REMOTE HOST IDENTIFICATION HAS CHANGED' in last:
            raise ValueError(last)
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise TimeoutError(f'SSH did not become ready in {timeout}s: {last}')


def wait_cloud(a, timeout=300):
    wait_ssh(a, timeout)
    remote(a, ['sudo', '-n', 'timeout', str(timeout), 'cloud-init', 'status', '--wait'])


def setup(a):
    wait_cloud(a)
    directory = '/home/' + a['user'] + '/chameleon-tools'
    remote(a, ['mkdir', '-p', directory])
    transfer(a, 'upload', ROOT / 'scripts/guest-setup.sh', directory + '/guest-setup.sh')
    remote(a, ['sudo', '-n', 'install', '-m', '0755', directory + '/guest-setup.sh',
               '/usr/local/sbin/chameleon-guest-setup'])
    remote(a, ['sudo', '-n', '/usr/local/sbin/chameleon-guest-setup'])
    transfer(a, 'upload', ROOT / 'scripts/hermit-guest.py', directory + '/hermit-guest.py')


SELECT_KERNEL = r'''
import pathlib, re, subprocess, sys
release = sys.argv[1]
text = pathlib.Path('/boot/grub/grub.cfg').read_text()
parent = None
selected = None
for line in text.splitlines():
    if line.startswith('submenu '):
        match = re.search(r"\$menuentry_id_option ['\"]([^'\"]+)['\"]", line)
        parent = match.group(1) if match else None
    if line.lstrip().startswith('menuentry ') and re.search('with Linux ' + re.escape(release) + "['\"]", line):
        match = re.search(r"\$menuentry_id_option ['\"]([^'\"]+)['\"]", line)
        if match:
            selected = (parent + '>' if line[0].isspace() and parent else '') + match.group(1)
            break
if not selected:
    raise SystemExit('Cannot find exact kernel GRUB entry for ' + release + '; inspect /boot/grub/grub.cfg')
directory = pathlib.Path('/etc/default/grub.d')
directory.mkdir(exist_ok=True)
(directory / '99-chameleon-kernel.cfg').write_text("GRUB_DEFAULT='" + selected + "'\nGRUB_TIMEOUT_STYLE=menu\nGRUB_TIMEOUT=2\n")
subprocess.run(['update-grub'], check=True)
print('Selected GRUB entry: ' + selected)
'''


def select_kernel(a, release):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._+-]*', release):
        raise ValueError('Invalid kernel release')
    remote(a, ['sudo', '-n', 'python3', '-', release], input=SELECT_KERNEL, text=True)


def install_kernel(a, archive):
    archive = Path(archive).expanduser().resolve()
    with tarfile.open(archive) as stream:
        manifest = json.load(stream.extractfile('manifest.json'))
        if manifest.get('role') != 'guest' or manifest.get('kernelrelease') != RELEASE:
            raise ValueError('Expected the matching Guest kernel package')
        for member in stream.getmembers():
            if Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                raise ValueError('Invalid kernel archive path')
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError('Kernel deployment archive must contain ordinary files/directories')
    wait_cloud(a)
    current = remote(a, ['uname', '-r'], capture_output=True, text=True).stdout.strip()
    if current == RELEASE:
        raise ValueError('Boot the distro kernel before replacing this kernel and its modules')
    directory = remote(a, ['mktemp', '-d', '/tmp/chameleon-kernel.XXXXXXXX'],
                       capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'/tmp/chameleon-kernel\.[a-zA-Z0-9]+', directory):
        raise ValueError('Unexpected remote temporary directory')
    try:
        transfer(a, 'upload', archive, directory + '/kernel.tar.gz')
        remote(a, ['tar', '-xzf', directory + '/kernel.tar.gz', '-C', directory])
        remote(a, ['sudo', '-n', 'python3', directory + '/install-kernel.py', 'install',
                   '--role', 'guest', '--package', directory, '--system', '--update-grub'])
        select_kernel(a, RELEASE)
    finally:
        remote(a, ['rm', '-rf', '--', directory])
    print('Installed and selected ' + RELEASE + '; use stop then start for Chameleon mode, or reboot in the current mode.')


def reboot(a, wait=False, timeout=300):
    current = json.loads((run_dir(a) / 'config.json').read_text())
    if not current.get('reboot'):
        raise ValueError('This VM exits on reboot; use stop then start, or set reboot=true')
    before = remote(a, ['cat', '/proc/sys/kernel/random/boot_id'], capture_output=True, text=True).stdout.strip()
    # Return SSH successfully before systemd performs the reboot.
    remote(a, ['sudo', '-n', 'systemd-run', '--on-active=2s', '/sbin/reboot'])
    if wait:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(ssh_command(a, ['cat', '/proc/sys/kernel/random/boot_id']),
                                        capture_output=True, text=True,
                                        timeout=min(10, max(1, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0 and result.stdout.strip() != before:
                wait_cloud(a, max(1, int(deadline - time.monotonic())))
                remote(a, ['uname', '-r'])
                return
            time.sleep(2)
        raise TimeoutError('Guest did not complete a new boot')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', default='chameleon')
    p.add_argument('--access', type=Path, help='access.json for a created or existing Guest')
    sub = p.add_subparsers(dest='action', required=True)
    boot = sub.add_parser('start', help='start QEMU in background; serial.log in Guest directory')
    boot.add_argument('--bootstrap', action='store_true', help='disable Chameleon/PEBS for distro installation')
    boot.add_argument('--wait', action='store_true', help='wait for SSH and cloud-init')
    boot.add_argument('--timeout', type=int, default=300)
    sub.add_parser('status')
    shutdown = sub.add_parser('stop', help='graceful ACPI shutdown')
    shutdown.add_argument('--force', action='store_true', help='QMP quit; equivalent to unclean power-off')
    shutdown.add_argument('--timeout', type=int, default=120)
    restart = sub.add_parser('reboot', help='reboot Guest in its current QEMU configuration')
    restart.add_argument('--wait', action='store_true')
    restart.add_argument('--timeout', type=int, default=300)
    ready = sub.add_parser('wait')
    ready.add_argument('--cloud-init', action='store_true')
    ready.add_argument('--timeout', type=int, default=300)
    sub.add_parser('ssh', help='interactive SSH terminal')
    execute = sub.add_parser('exec', help='literal argv after --, or explicit remote --shell')
    execute.add_argument('--shell', help='remote shell command; quote it for the local shell')
    execute.add_argument('command', nargs=argparse.REMAINDER)
    for name in ('upload', 'download'):
        copy = sub.add_parser(name)
        copy.add_argument('--recursive', '-r', action='store_true')
        copy.add_argument('source')
        copy.add_argument('destination')
    sub.add_parser('setup', help='install Guest development/RDMA tools and helper script')
    kernel = sub.add_parser('install-kernel', help='upload/install/select exact Guest kernel; does not reboot')
    kernel.add_argument('--package', type=Path, default=ROOT / f'build/deploy-guest/{RELEASE}.tar.gz')
    select = sub.add_parser('select-kernel', help='set an installed GRUB kernel as default; does not reboot')
    select.add_argument('--release', required=True, help='exact release, e.g. the retained distro kernel')
    args = p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', args.name):
        p.error('Invalid Guest name')
    if hasattr(args, 'timeout') and args.timeout < 1:
        p.error('--timeout must be positive')
    a = load_access(args.access or ROOT / 'build/guests' / args.name / 'access.json')
    if args.action in ('start', 'stop', 'reboot', 'setup', 'install-kernel', 'select-kernel'):
        with control_lock(a):
            if args.action == 'start':
                start(a, args.bootstrap, args.timeout)
                if args.wait:
                    wait_cloud(a, args.timeout)
            elif args.action == 'stop':
                stop(a, args.force, args.timeout)
            elif args.action == 'reboot':
                reboot(a, args.wait, args.timeout)
            elif args.action == 'setup':
                setup(a)
            elif args.action == 'select-kernel':
                select_kernel(a, args.release)
            else:
                install_kernel(a, args.package)
    elif args.action == 'status':
        state = active(a)
        print(json.dumps(dict(name=a['name'], qemu=state or {'status': 'stopped'},
                              ssh=f'{a["user"]}@{a["host"]}:{a["port"]}',
                              serial_log=str(a['_path'].parent / 'serial.log')), indent=2))
    elif args.action == 'wait':
        (wait_cloud if args.cloud_init else wait_ssh)(a, args.timeout)
        print('Guest is ready')
    elif args.action == 'ssh':
        raise SystemExit(subprocess.call(ssh_command(a, tty=True)))
    elif args.action == 'exec':
        command = args.command
        if command and command[0] == '--':
            command = command[1:]
        if bool(command) == (args.shell is not None):
            p.error('exec requires either -- argv... or --shell COMMAND')
        raise SystemExit(subprocess.call(ssh_command(a, command or None, args.shell)))
    else:
        transfer(a, args.action, args.source, args.destination, args.recursive)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(f'guestctl: command failed (exit {error.returncode})', file=sys.stderr)
        raise SystemExit(error.returncode if error.returncode > 0 else 1)
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.TimeoutExpired) as error:
        print(f'guestctl: {error}', file=sys.stderr)
        raise SystemExit(1)
