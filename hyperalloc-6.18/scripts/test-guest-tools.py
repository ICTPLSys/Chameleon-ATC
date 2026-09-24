#!/usr/bin/env python3
"""Regression checks for disk Guest provisioning and management boundaries.

Runs without root, a running VM, downloads, or changes to a user's SSH files.
"""
import importlib.util
import fcntl
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = load('guest_launcher_test', 'run-deploy-vm.py')
creator = load('guest_creator_test', 'create-deploy-guest.py')


class DiskBootTests(unittest.TestCase):
    def test_cloud_guest_can_reboot_without_qemu_exiting(self):
        settings = launcher.config(dict(disk='disk.qcow2', kernel=None,
                                        reboot=True, seed='seed.iso'))
        command = launcher.command(settings)
        self.assertNotIn('-no-reboot', command)
        self.assertNotIn('-kernel', command)
        self.assertNotIn('-append', command)

    def test_existing_acceptance_launch_keeps_no_reboot_behavior(self):
        settings = launcher.config(dict(disk='disk.qcow2', kernel=None))
        self.assertIn('-no-reboot', launcher.command(settings))

    def test_cloud_seed_readonly_and_comma_paths_preserved(self):
        settings = launcher.config(dict(disk='/tmp/guest, disk.qcow2',
                                        seed='/tmp/seed, config.iso', kernel=None))
        command = launcher.command(settings)
        drives = [command[i + 1] for i, value in enumerate(command)
                  if value == '-drive']
        self.assertIn('file=/tmp/guest,, disk.qcow2,', drives[0])
        self.assertIn('file=/tmp/seed,, config.iso,', drives[1])
        self.assertIn('readonly=on', drives[1])
        self.assertNotIn('snapshot=on', drives[0])


class CreatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='guest-tools-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / 'build/guests/example'
        self.source = self.root / 'source image.qcow2'
        self.source.write_bytes(b'leave this original image unchanged')
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/guest-setup.sh').write_text('#!/bin/sh\ntrue\n')
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(creator, 'ROOT', self.root).start()
        mock.patch.object(creator.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name).start()

    def args(self, *extra):
        return creator.parser().parse_args([
            '--name', 'example', '--image', str(self.source), '--disk-size', '8M', *extra])

    @staticmethod
    def external_command(command, **kwargs):
        """Model external image/key tools while retaining real local file effects."""
        tool = Path(command[0]).name
        if tool == 'qemu-img' and command[1] == 'info':
            return subprocess.CompletedProcess(command, 0,
                                               json.dumps({'format': 'qcow2', 'virtual-size': 1048576}))
        if tool == 'qemu-img' and command[1] == 'convert':
            Path(command[-1]).write_bytes(b'new independent disk')
        elif tool == 'ssh-keygen':
            identity = Path(command[command.index('-f') + 1])
            identity.write_text('test private key\n')
            identity.with_suffix('.pub').write_text('ssh-ed25519 TEST fixture\n')
        return subprocess.CompletedProcess(command, 0, '')

    def test_existing_guest_is_not_modified_even_on_failure(self):
        self.destination.mkdir(parents=True)
        disk = self.destination / 'disk.qcow2'
        disk.write_bytes(b'existing user data')
        with mock.patch.object(creator.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                creator.create(self.args())
            run.assert_not_called()
        self.assertEqual(disk.read_bytes(), b'existing user data')

    def test_existing_symlink_is_not_followed_or_removed(self):
        unrelated = self.root / 'unrelated'
        unrelated.mkdir()
        marker = unrelated / 'keep'
        marker.write_text('keep')
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(unrelated, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            creator.create(self.args())
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(marker.read_text(), 'keep')

    def test_seed_failure_removes_only_new_guest_and_restores_umask(self):
        sibling = self.destination.parent / 'other-guest'
        sibling.mkdir(parents=True)
        marker = sibling / 'disk.qcow2'
        marker.write_bytes(b'other user data')
        before = self.source.read_bytes()
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        with mock.patch.object(creator.subprocess, 'run', side_effect=self.external_command):
            with mock.patch.object(creator, 'seed_image', side_effect=RuntimeError('ISO tool failed')):
                with self.assertRaisesRegex(RuntimeError, 'ISO tool failed'):
                    creator.create(self.args())
        observed = os.umask(0o022)
        self.assertEqual(observed, 0o022)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(marker.read_bytes(), b'other user data')

    def test_invalid_image_size_fails_before_guest_is_created(self):
        info = subprocess.CompletedProcess([], 0, json.dumps({'format': 'qcow2', 'virtual-size': 16 * 1048576}))
        with mock.patch.object(creator.subprocess, 'run', return_value=info):
            with self.assertRaisesRegex(ValueError, 'smaller'):
                creator.create(self.args())
        self.assertFalse(self.destination.exists())

    def test_generated_guest_keeps_provisioning_and_deployment_separate(self):
        with mock.patch.object(creator.subprocess, 'run', side_effect=self.external_command):
            with mock.patch.object(creator, 'seed_image', side_effect=lambda tool, output, directory: output.write_bytes(b'iso')):
                access = creator.create(self.args())
        bootstrap = json.loads(Path(access['bootstrap_config']).read_text())
        deployment = json.loads(Path(access['vm_config']).read_text())
        self.assertEqual(bootstrap['disk'], deployment['disk'])
        self.assertIsNone(bootstrap['kernel'])
        self.assertEqual(bootstrap['vfio'], [])
        self.assertFalse(any(bootstrap[name] for name in ['pebs', 'chameleon', 'policy']))
        self.assertTrue(all(deployment[name] for name in ['pebs', 'chameleon', 'policy', 'reboot']))
        self.assertEqual(access['host'], '127.0.0.1')
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(access['identity_file']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(access['known_hosts']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.source.read_bytes(), b'leave this original image unchanged')
        network = json.loads((self.destination / 'network-config').read_text())
        management = network['ethernets']['management']
        self.assertEqual(management['match'], {'macaddress': deployment['mac']})
        self.assertNotIn('set-name', management)
        self.assertFalse(management.get('optional', False))

    def test_management_dhcp_does_not_depend_on_initramfs_interface_name(self):
        # shutil.which is mocked for the creator's external build prerequisites.
        netplan = '/usr/sbin/netplan' if Path('/usr/sbin/netplan').is_file() else None
        if not netplan:
            self.skipTest('netplan unavailable for renderer regression test')
        with mock.patch.object(creator.subprocess, 'run', side_effect=self.external_command):
            with mock.patch.object(creator, 'seed_image', side_effect=lambda tool, output, directory: output.write_bytes(b'iso')):
                creator.create(self.args())
        network = json.loads((self.destination / 'network-config').read_text())
        netplan_root = self.root / 'netplan-root'
        configuration = netplan_root / 'etc/netplan/50-cloud-init.yaml'
        configuration.parent.mkdir(parents=True)
        configuration.write_text(json.dumps({'network': network}))
        configuration.chmod(0o600)
        subprocess.run([netplan, 'generate', '--root-dir', str(netplan_root)],
                       check=True, capture_output=True, text=True)
        rendered = (netplan_root / 'run/systemd/network/10-netplan-management.network').read_text()
        match = rendered.split('[Match]', 1)[1].split('[', 1)[0]
        self.assertIn('MACAddress=', match)
        self.assertNotIn('Name=', match)
        self.assertIn('DHCP=ipv4', rendered)
        # Ignoring the only managed NIC leaves wait-online with no qualifying
        # online link even after DHCP succeeds, delaying SSH for 120 seconds.
        self.assertNotIn('RequiredForOnline=no', rendered)


class GuestControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load('guest_control_test', 'guestctl.py')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='guest-control-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = self.root / 'private key'
        self.identity.write_text('fixture key')
        self.identity.chmod(0o600)
        self.known_hosts = self.root / 'known hosts'
        self.known_hosts.touch()
        self.access = {
            'name': 'example', 'host': '127.0.0.1', 'port': 15022,
            'user': 'ubuntu', 'identity_file': str(self.identity),
            'known_hosts': str(self.known_hosts),
            'vm_config': str(self.root / 'vm.json'),
            'bootstrap_config': str(self.root / 'bootstrap.json'),
            '_path': self.root / 'access.json',
        }
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.control, 'ROOT', self.root).start()

    def test_exec_arguments_arrive_literally_at_remote_shell_boundary(self):
        marker = self.root / 'must not be created'
        values = ['two words', "single'quote", 'double"quote',
                  '$(touch ' + str(marker) + ')', '; exit 37', '*.txt', 'back\\slash']
        remote = [sys.executable, '-c', 'import json,sys; print(json.dumps(sys.argv[1:]))', *values]
        command = self.control.ssh_command(self.access, command=remote)
        result = subprocess.run(['/bin/sh', '-c', command[-1]], check=True,
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), values)
        self.assertFalse(marker.exists())
        self.assertIn('/dev/null', command)
        self.assertIn('StrictHostKeyChecking=accept-new', command)
        self.assertIn('UserKnownHostsFile="' + str(self.known_hosts) + '"', command)

    def test_explicit_shell_mode_retains_shell_operations(self):
        command = self.control.ssh_command(self.access, shell='printf alpha; printf beta')
        result = subprocess.run(['/bin/sh', '-c', command[-1]], check=True,
                                capture_output=True, text=True)
        self.assertEqual(result.stdout, 'alphabeta')

    def test_file_upload_download_preserves_spaces_quotes_and_wildcards(self):
        server = next((path for path in ['/usr/lib/openssh/sftp-server', '/usr/lib/ssh/sftp-server']
                       if Path(path).is_file()), None)
        if not server or not shutil.which('sftp'):
            self.skipTest('local OpenSSH sftp-server is unavailable')
        source = self.root / 'input space \' " \\ *.txt'
        source.write_bytes(b'file contents\x00\n')
        distractor = self.root / 'input space \' " \\ other.txt'
        distractor.write_bytes(b'wrong wildcard selection')
        remote = self.root / 'remote space \' " \\ *.txt'
        downloaded = self.root / 'download space \' " \\ *.txt'
        with mock.patch.object(self.control, 'sftp_command',
                               return_value=['sftp', '-D', server, '-b', '-']):
            self.control.transfer(self.access, 'upload', str(source), str(remote))
            self.control.transfer(self.access, 'download', str(remote), str(downloaded))
        self.assertEqual(remote.read_bytes(), source.read_bytes())
        self.assertEqual(downloaded.read_bytes(), source.read_bytes())
        self.assertEqual(distractor.read_bytes(), b'wrong wildcard selection')

    def test_transfer_newline_cannot_inject_another_batch_command(self):
        with mock.patch.object(self.control.subprocess, 'run') as run:
            with self.assertRaises((ValueError, RuntimeError)):
                self.control.transfer(self.access, 'download', '/remote/file\n!touch /tmp/injected', str(self.root / 'out'))
            run.assert_not_called()

    def test_transfer_failure_is_propagated(self):
        source = self.root / 'input'
        source.write_text('data')
        failure = subprocess.CalledProcessError(17, ['sftp'])
        with mock.patch.object(self.control.subprocess, 'run', side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError) as result:
                self.control.transfer(self.access, 'upload', str(source), '/remote/output')
        self.assertEqual(result.exception.returncode, 17)

    def test_wait_ssh_retries_a_connection_timeout_during_boot(self):
        results = [subprocess.TimeoutExpired(['ssh'], 1),
                   subprocess.CompletedProcess(['ssh'], 0, '', '')]
        with mock.patch.object(self.control.subprocess, 'run', side_effect=results) as run:
            with mock.patch.object(self.control.time, 'sleep'):
                self.control.wait_ssh(self.access, timeout=5)
        self.assertEqual(run.call_count, 2)

    def test_start_does_not_spawn_when_standalone_launcher_holds_lock(self):
        Path(self.access['vm_config']).write_text(json.dumps({'name': 'example'}))
        run_dir = self.root / 'build/running/example'
        run_dir.mkdir(parents=True)
        with (run_dir / 'launcher.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(self.control.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ValueError, 'already running or starting'):
                    self.control.start(self.access)
                spawn.assert_not_called()

    @contextlib.contextmanager
    def starting_guest(self, readiness):
        """Model slow QMP startup without launching QEMU or waiting in real time."""
        Path(self.access['vm_config']).write_text(json.dumps({'name': 'example'}))
        clock = [0.0]
        probes = []

        def query(access, execute, timeout):
            probes.append(timeout)
            if len(probes) == 1:
                raise FileNotFoundError('no previous QEMU')
            return readiness(clock, timeout)

        with contextlib.ExitStack() as stack:
            spawn = stack.enter_context(mock.patch.object(self.control.subprocess, 'Popen'))
            spawn.return_value.poll.return_value = None
            stack.enter_context(mock.patch.object(self.control, 'qmp', side_effect=query))
            stack.enter_context(mock.patch.object(self.control.time, 'monotonic', side_effect=lambda: clock[0]))
            stack.enter_context(mock.patch.object(self.control.time, 'sleep',
                side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            yield clock, probes, spawn

    def test_start_waits_for_slow_vfio_greeting_beyond_thirty_seconds(self):
        def readiness(clock, timeout):
            if clock[0] < 40:
                clock[0] += timeout
                raise TimeoutError('QMP greeting is not ready')
            return {'status': 'running'}

        with self.starting_guest(readiness) as (clock, probes, spawn):
            self.control.start(self.access, timeout=600)
            self.assertEqual(clock[0], 40)
            self.assertEqual(probes, [5] * 10)  # preflight, eight waits, ready
            spawn.assert_called_once()

    def test_start_qmp_timeout_exhausts_only_the_requested_budget(self):
        def readiness(clock, timeout):
            clock[0] += timeout
            raise TimeoutError('QMP greeting is not ready')

        with self.starting_guest(readiness) as (clock, probes, spawn):
            with self.assertRaisesRegex(TimeoutError, 'did not become ready in 12s'):
                self.control.start(self.access, timeout=12)
            self.assertEqual(clock[0], 12)
            self.assertEqual(probes, [5, 5, 5, 2])
            # A timeout preserves the starting VM for status/inspection.
            spawn.return_value.terminate.assert_not_called()
            spawn.return_value.kill.assert_not_called()

    def test_start_reports_launcher_exit_after_a_greeting_timeout(self):
        def readiness(clock, timeout):
            clock[0] += timeout
            raise TimeoutError('QMP greeting is not ready')

        with self.starting_guest(readiness) as (_, probes, spawn):
            spawn.return_value.poll.side_effect = [None, 17]
            spawn.return_value.returncode = 17
            with self.assertRaisesRegex(ValueError, 'launcher exited 17'):
                self.control.start(self.access, timeout=5)
            self.assertEqual(probes, [5, 5])

    def test_start_does_not_retry_a_wrong_qmp_identity(self):
        def readiness(clock, timeout):
            raise ValueError('QMP socket belongs to a different VM')

        with self.starting_guest(readiness) as (_, probes, _):
            with self.assertRaisesRegex(ValueError, 'different VM'):
                self.control.start(self.access, timeout=600)
            self.assertEqual(probes, [5, 5])

    def test_start_retries_current_vfio_busy_then_starts(self):
        busy = b'qemu-system-x86_64: vfio 0000:69:00.3: failed to open /dev/vfio/262: Device or resource busy\n'
        with self.starting_guest(lambda clock, timeout: {'status':'running'}) as (clock, _, spawn):
            failed = mock.Mock(returncode=1)
            failed.poll.return_value = 1
            running = mock.Mock()
            running.poll.return_value = None
            def launch(*args, **kwargs):
                if spawn.call_count == 1:
                    kwargs['stdout'].write(busy)
                    return failed
                return running
            spawn.side_effect = launch
            with mock.patch.object(self.control, 'active', side_effect=[None, None, {'status':'running'}]):
                self.control.start(self.access, timeout=10)
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(clock[0], 2)

    def test_start_vfio_retry_is_bounded_and_ignores_old_errors(self):
        busy = b'qemu-system-x86_64: vfio 0000:69:00.3: failed to open /dev/vfio/262: Device or resource busy\n'
        for stale in (False, True):
            with self.subTest(stale=stale), self.starting_guest(lambda clock, timeout: None) as (clock, _, spawn):
                if stale:
                    (self.root/'serial.log').write_bytes(busy)
                failed = mock.Mock(returncode=1)
                failed.poll.return_value = 1
                def launch(*args, **kwargs):
                    kwargs['stdout'].write(b'current unrelated failure\n' if stale else busy)
                    return failed
                spawn.side_effect = launch
                with mock.patch.object(self.control, 'active', return_value=None):
                    with self.assertRaises(ValueError if stale else TimeoutError):
                        self.control.start(self.access, timeout=3)
                self.assertEqual(spawn.call_count, 1 if stale else 2)
                self.assertEqual(clock[0], 0 if stale else 3)

    def test_preexisting_qmp_timeout_does_not_spawn_another_vm(self):
        Path(self.access['vm_config']).write_text(json.dumps({'name': 'example'}))
        with mock.patch.object(self.control, 'qmp', side_effect=TimeoutError('existing QMP stalled')):
            with mock.patch.object(self.control.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(TimeoutError, 'existing QMP stalled'):
                    self.control.start(self.access)
                spawn.assert_not_called()

    def test_active_and_stop_propagate_qmp_timeouts(self):
        with mock.patch.object(self.control, 'qmp', side_effect=TimeoutError('QMP stalled')) as qmp:
            for action in (self.control.active, self.control.stop):
                with self.subTest(action=action.__name__):
                    with self.assertRaisesRegex(TimeoutError, 'QMP stalled'):
                        action(self.access)
            self.assertTrue(all(call.args[1] == 'query-status' for call in qmp.call_args_list))

    def test_stop_retries_connection_reset_during_graceful_shutdown(self):
        with mock.patch.object(self.control, 'active', side_effect=[{'status':'running'}, ConnectionResetError(), None]), \
             mock.patch.object(self.control, 'qmp') as qmp, \
             mock.patch.object(self.control, 'launcher_idle', return_value=True), \
             mock.patch.object(self.control.time, 'sleep'):
            self.control.stop(self.access)
        qmp.assert_called_once_with(self.access, 'system_powerdown')

    def test_stop_retries_bad_greeting_only_after_shutdown_request(self):
        with mock.patch.object(self.control, 'active', side_effect=[{'status':'running'}, self.control.QMPGreetingError('shutdown event'), None]), \
             mock.patch.object(self.control, 'qmp') as qmp, \
             mock.patch.object(self.control, 'launcher_idle', return_value=True) as idle, \
             mock.patch.object(self.control.time, 'sleep'):
            self.control.stop(self.access)
        qmp.assert_called_once_with(self.access, 'system_powerdown')
        idle.assert_called_once()

    def test_bad_initial_greeting_cannot_authorize_shutdown(self):
        with mock.patch.object(self.control, 'active', side_effect=self.control.QMPGreetingError('bad initial greeting')), \
             mock.patch.object(self.control, 'qmp') as qmp:
            with self.assertRaises(self.control.QMPGreetingError):
                self.control.stop(self.access)
        qmp.assert_not_called()

    def test_stop_does_not_ignore_wrong_vm_identity_after_shutdown(self):
        with mock.patch.object(self.control, 'active', side_effect=[{'status':'running'}, ValueError('different VM')]), \
             mock.patch.object(self.control, 'qmp'):
            with self.assertRaisesRegex(ValueError, 'different VM'):
                self.control.stop(self.access)

    def test_start_cli_passes_full_timeout_to_qmp_and_guest_readiness(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, 'argv',
                ['guestctl.py', 'start', '--wait', '--timeout', '600']))
            stack.enter_context(mock.patch.object(self.control, 'load_access', return_value=self.access))
            start = stack.enter_context(mock.patch.object(self.control, 'start'))
            wait = stack.enter_context(mock.patch.object(self.control, 'wait_cloud'))
            self.control.main()
            start.assert_called_once_with(self.access, False, 600)
            wait.assert_called_once_with(self.access, 600)

    def test_controller_lock_serializes_mutating_commands(self):
        with self.control.control_lock(self.access):
            with self.assertRaisesRegex(ValueError, 'Another Guest'):
                with self.control.control_lock(self.access):
                    self.fail('Second controller acquired an active Guest lock')

    def qmp_server(self, name='example'):
        run_dir = self.root / 'build/running/example'
        run_dir.mkdir(parents=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(run_dir / 'qmp.sock'))
        listener.listen(1)
        listener.settimeout(3)
        commands = []
        failures = []

        def serve():
            try:
                connection, _ = listener.accept()
                with connection, connection.makefile('rwb') as stream:
                    stream.write(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
                    stream.flush()
                    while True:
                        line = stream.readline()
                        if not line:
                            return
                        request = json.loads(line)
                        execute = request['execute']
                        commands.append(execute)
                        result = {'name': name} if execute == 'query-name' else (
                            {'status': 'running'} if execute == 'query-status' else {})
                        response = {'return': result}
                        if 'id' in request:
                            response['id'] = request['id']
                        stream.write(b'{"event":"STOP","data":{}}\r\n')
                        stream.write(json.dumps(response).encode() + b'\r\n')
                        stream.flush()
            except Exception as error:
                failures.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 4)
        self.addCleanup(listener.close)
        return commands, failures

    def test_qmp_skips_events_and_checks_machine_identity(self):
        commands, failures = self.qmp_server()
        result = self.control.qmp(self.access, 'query-status')
        self.assertEqual(result, {'status': 'running'})
        self.assertEqual(commands, ['qmp_capabilities', 'query-name', 'query-status'])
        self.assertEqual(failures, [])

    def test_qmp_wrong_guest_does_not_receive_mutation(self):
        commands, failures = self.qmp_server(name='different-guest')
        with self.assertRaises((ValueError, RuntimeError)):
            self.control.qmp(self.access, 'quit')
        self.assertNotIn('quit', commands)
        self.assertEqual(failures, [])


class GuestCLITests(unittest.TestCase):
    """Exercise the real CLI and SSH/SFTP against a private loopback sshd."""
    @classmethod
    def setUpClass(cls):
        sshd = shutil.which('sshd')
        if not sshd or not shutil.which('ssh-keygen'):
            raise unittest.SkipTest('sshd/ssh-keygen unavailable for loopback integration tests')
        temporary = tempfile.TemporaryDirectory(prefix='guest-cli-test-')
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        for key in ['host', 'identity']:
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '',
                            '-f', str(cls.root / key)], check=True)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        user = pwd.getpwuid(os.getuid()).pw_name
        server = cls.root / 'sshd_config'
        server.write_text(f'Port {port}\nListenAddress 127.0.0.1\n'
                          f'HostKey {cls.root}/host\nPidFile {cls.root}/sshd.pid\n'
                          f'AuthorizedKeysFile {cls.root}/identity.pub\n'
                          'StrictModes no\nUsePAM no\nPasswordAuthentication no\n'
                          'KbdInteractiveAuthentication no\nAuthenticationMethods publickey\n'
                          f'AllowUsers {user}\nSubsystem sftp internal-sftp\n')
        cls.log = (cls.root / 'sshd.log').open('w+')
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([sshd, '-D', '-e', '-f', str(server)],
                                       stdout=cls.log, stderr=cls.log)

        def close_server():
            if cls.process.poll() is None:
                cls.process.terminate()
            cls.process.wait(timeout=10)
        cls.addClassCleanup(close_server)
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                if cls.process.poll() is not None or time.monotonic() > deadline:
                    cls.log.flush()
                    cls.log.seek(0)
                    raise unittest.SkipTest('private sshd could not start: ' + cls.log.read())
                time.sleep(0.02)
        access = {'name': 'guest-cli-test-' + str(os.getpid()), 'host': '127.0.0.1',
                  'port': port, 'user': user, 'identity_file': str(cls.root / 'identity'),
                  'known_hosts': str(cls.root / 'known hosts'),
                  'vm_config': str(cls.root / 'vm.json'),
                  'bootstrap_config': str(cls.root / 'bootstrap.json')}
        cls.access_path = cls.root / 'access.json'
        cls.access_path.write_text(json.dumps(access))

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPTS / 'guestctl.py'),
                               '--access', str(self.access_path), *map(str, arguments)],
                              capture_output=True, text=True, timeout=15)

    def test_cli_exec_returns_remote_failure_status(self):
        result = self.cli('exec', '--', sys.executable, '-c', 'import sys; sys.exit(37)')
        self.assertEqual(result.returncode, 37, result.stderr)

    def test_cli_shell_returns_output_and_remote_failure_status(self):
        result = self.cli('exec', '--shell', 'printf from-guest; exit 19')
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertEqual(result.stdout, 'from-guest')

    def test_cli_keeps_host_keys_in_requested_path_with_spaces(self):
        result = self.cli('exec', '--', 'true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'known hosts').is_file())
        self.assertFalse((self.root / 'known').exists())

    def test_cli_upload_download_roundtrip_over_ssh(self):
        source = self.root / 'input with "quote" and spaces'
        source.write_bytes(b'contents\x00\xff\n')
        remote = self.root / 'remote with "quote" and spaces'
        destination = self.root / 'output with "quote" and spaces'
        upload = self.cli('upload', source, remote)
        self.assertEqual(upload.returncode, 0, upload.stderr)
        download = self.cli('download', remote, destination)
        self.assertEqual(download.returncode, 0, download.stderr)
        self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_cli_status_reports_stopped_when_no_qemu_exists(self):
        result = self.cli('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['qemu']['status'], 'stopped')


class KernelSelectionTests(unittest.TestCase):
    """Run the exact remote selector against a temporary Ubuntu GRUB tree."""
    @classmethod
    def setUpClass(cls):
        cls.control = load('guest_kernel_selection_test', 'guestctl.py')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='guest-grub-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        grub = self.root / 'boot/grub/grub.cfg'
        grub.parent.mkdir(parents=True)
        grub.write_text('''menuentry 'Ubuntu' --class ubuntu $menuentry_id_option 'gnulinux-simple-testuuid' {
}
submenu 'Advanced options for Ubuntu' $menuentry_id_option 'gnulinux-advanced-testuuid' {
    menuentry 'Ubuntu, with Linux 6.18.0-chameleon-guest-old' $menuentry_id_option 'wrong-suffix' {
    }
    menuentry 'Ubuntu, with Linux 6.18.0-chameleon-guest (recovery mode)' $menuentry_id_option 'custom-recovery' {
    }
    menuentry 'Ubuntu, with Linux 6.18.0-chameleon-guest' $menuentry_id_option 'custom-normal' {
    }
    menuentry "Ubuntu, with Linux 5.15.0-191-generic (recovery mode)" $menuentry_id_option "distro-recovery" {
    }
    menuentry "Ubuntu, with Linux 5.15.0-191-generic" $menuentry_id_option "distro-normal" {
    }
}
''')
        self.selected = self.root / 'etc/default/grub.d/99-chameleon-kernel.cfg'
        self.selected.parent.mkdir(parents=True)
        self.selected.write_text("GRUB_DEFAULT='previous-selection'\n")

    def select(self, release):
        def private_path(path):
            if not str(path).startswith('/'):
                raise AssertionError('Unexpected selector path: ' + str(path))
            return self.root / str(path).lstrip('/')
        with mock.patch('pathlib.Path', side_effect=private_path):
            with mock.patch.object(sys, 'argv', ['-', release]):
                with mock.patch.object(subprocess, 'run') as update:
                    with contextlib.redirect_stdout(io.StringIO()):
                        exec(self.control.SELECT_KERNEL, {'__name__': '__main__'})
                    update.assert_called_once_with(['update-grub'], check=True)

    def test_custom_kernel_selects_exact_non_recovery_entry(self):
        self.select('6.18.0-chameleon-guest')
        config = self.selected.read_text()
        self.assertIn("GRUB_DEFAULT='gnulinux-advanced-testuuid>custom-normal'", config)
        self.assertNotIn('recovery', config)
        self.assertNotIn('wrong-suffix', config)

    def test_distro_kernel_can_be_selected_before_reinstalling_custom_modules(self):
        self.select('5.15.0-191-generic')
        config = self.selected.read_text()
        self.assertIn("GRUB_DEFAULT='gnulinux-advanced-testuuid>distro-normal'", config)
        self.assertNotIn('recovery', config)

    def test_missing_kernel_preserves_existing_boot_selection(self):
        before = self.selected.read_bytes()
        with self.assertRaisesRegex(SystemExit, 'Cannot find exact kernel GRUB entry'):
            self.select('6.18.0-chameleon-missing')
        self.assertEqual(self.selected.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
