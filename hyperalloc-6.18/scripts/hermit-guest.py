#!/usr/bin/env python3
"""Check or connect Hermit on the disk guest with a passed-through mlx5 VF.

Run inside the Guest after installing its deployment kernel package and setting
the RDMA/IPoIB interface IP. `check` and `status` are read-only; `start` loads the
module. The remote rswap-server must already be listening. No swapfile is used.
"""
import argparse
import contextlib
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SYS = Path('/sys')
CONTROL_LOCK = Path('/run/chameleon-hermit-guest.lock')


class ConnectionFailure(RuntimeError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def parameters(args):
    address = ipaddress.ip_address(args.server)
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError('server must be the reachable unicast RDMA address of the memory server')
    if not 1 <= args.port <= 65535 or not 1 <= args.pool_mib <= 65536:
        raise ValueError('port must be 1..65535 and pool-mib 1..65536')
    return ['backend=rdma', f'sip={address}', f'sport={args.port}', f'pool_mb={args.pool_mib}']


def rdma_devices(interface):
    """Follow VLAN/IPoIB partition parents before consulting GID netdevs."""
    seen = set()
    current = interface
    while current and current not in seen:
        seen.add(current)
        net = SYS / 'class/net' / current
        names = sorted(p.name for p in (net / 'device/infiniband').glob('*'))
        if names:
            return names
        current = None
        try:
            parent_index = (net / 'iflink').read_text().strip()
            own_index = (net / 'ifindex').read_text().strip()
        except OSError:
            break
        if parent_index != own_index:
            for candidate in (SYS / 'class/net').iterdir():
                try:
                    if (candidate / 'ifindex').read_text().strip() == parent_index:
                        current = candidate.name
                        break
                except OSError:
                    continue
    names = set()
    for entry in (SYS / 'class/infiniband').glob('*/ports/*/gid_attrs/ndevs/*'):
        try:
            if entry.read_text().strip() == interface:
                names.add(entry.parents[4].name)
        except OSError:
            # Native IB GIDs without an associated Ethernet netdev return EINVAL.
            continue
    return sorted(names)


def check(args):
    params = parameters(args)
    release = os.uname().release
    if not release.endswith('-chameleon-guest'):
        raise ValueError(f'Boot the deployment Guest kernel first; running {release}')
    vermagic = run('modinfo', '-F', 'vermagic', 'rswap_client').split()[0]
    if vermagic != release:
        raise ValueError(f'Hermit module vermagic {vermagic} differs from {release}')
    routes = json.loads(run('ip', '-j', 'route', 'get', args.server))
    if not routes or 'dev' not in routes[0]:
        raise ValueError('No IP route to the RDMA server')
    interface = routes[0]['dev']
    if args.interface and interface != args.interface:
        raise ValueError(f'Route uses {interface}, expected {args.interface}; configure the RDMA route in the guest')
    rdma = rdma_devices(interface)
    if not rdma:
        raise ValueError(f'Route to server uses {interface}, which is not an RDMA interface')
    native = []
    for name in rdma:
        driver = SYS / 'class/infiniband' / name / 'device/driver'
        if driver.exists() and driver.resolve().name == 'mlx5_core':
            native.append(name)
    if not native:
        raise ValueError(f'{interface} does not use the passed-through mlx5 driver')
    return {'status': 'PASS', 'guest_kernel': release, 'interface': interface,
            'rdma_devices': native, 'server': args.server, 'module_parameters': params,
            'connection_tested': False}


def status():
    loaded = (SYS / 'module/rswap_client').exists()
    result = {'loaded': loaded, 'guest_kernel': os.uname().release}
    stats = SYS / 'kernel/debug/hermit/stats'
    if stats.is_file():
        result['stats'] = stats.read_text()
    return result


@contextlib.contextmanager
def control_lock():
    with CONTROL_LOCK.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another Hermit helper is changing the backend') from error
        yield


def stats_fields(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            result[fields[0]] = int(fields[1], 0)
        except ValueError:
            result[fields[0]] = fields[1]
    return result


def dmesg_snapshot():
    try:
        result = subprocess.run(['dmesg', '--color=never'], capture_output=True, text=True)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    except OSError as error:
        return {'returncode': -1, 'stdout': '', 'stderr': repr(error)}


def fresh_dmesg(before, after):
    """Do not classify an old rejection, or ambiguous ring-buffer contents."""
    result = {'available': False, 'text': '', 'read_errors':
              [x['stderr'] for x in (before, after) if x['returncode']]}
    if before['returncode'] or after['returncode']:
        return result
    old, new = before['stdout'], after['stdout']
    if new.startswith(old):
        return dict(result, available=True, text=new[len(old):])
    old_lines, new_lines = old.splitlines(), new.splitlines()
    # A retained timestamped final line anchors the old/new boundary even if
    # the ring discarded its beginning. No overlap means no safe retry proof.
    if old_lines and old_lines[-1] in new_lines:
        boundary = len(new_lines) - 1 - new_lines[::-1].index(old_lines[-1])
        return dict(result, available=True, text='\n'.join(new_lines[boundary + 1:]))
    return dict(result, reason='dmesg history changed without an identifiable boundary')


def stale_connection(evidence):
    # rswap_rdma.c cm_event() negates event.status. IB CM propagates its
    # rejection reason through cma.c; ib_cm.h defines STALE_CONN=10. ROUTE=2
    # means address/path resolution completed before the rejected connect.
    errors = re.findall(r'hermit: RDMA connect failed error=(-?\d+) state=(\d+)',
                        evidence.get('text', ''))
    return evidence.get('available') and errors == [('-10', '2')]


def module_identity():
    entry = (SYS / 'module/rswap_client').stat()
    return (entry.st_ino, entry.st_ctime_ns)


def verify_connected(args):
    directory = SYS / 'module/rswap_client/parameters'
    actual = {key: (directory / key).read_text().strip()
              for key in ('backend', 'sip', 'sport', 'pool_mb')}
    expected = {'backend': 'rdma', 'sip': str(ipaddress.ip_address(args.server)),
                'sport': str(args.port), 'pool_mb': str(args.pool_mib)}
    if actual != expected:
        raise ValueError('Loaded Hermit parameters differ from this connection request: ' + str(actual))
    backend = stats_fields((SYS / 'kernel/debug/hermit/stats').read_text())
    transport = stats_fields((SYS / 'kernel/debug/hermit_rdma/stats').read_text())
    if backend.get('backend') != 'rdma' or backend.get('registered') != 1:
        raise ValueError('Hermit RDMA backend is not registered')
    if backend.get('capacity_pages', 0) * 4096 != args.pool_mib * 1024 * 1024:
        raise ValueError('Remote pool capacity differs from the requested capacity')
    if transport.get('broken') != 0 or transport.get('max_transfer_bytes', 0) < 4096:
        raise ValueError('Hermit RDMA transport is not healthy')
    return {'status': 'PASS', 'module_parameters': actual,
            'backend_stats': backend, 'transport_stats': transport}


def cleanup_owned(identity):
    module = SYS / 'module/rswap_client'
    if not module.exists():
        return {'status': 'ALREADY_ABSENT'}
    if module_identity() != identity:
        return {'status': 'PRESERVED', 'reason': 'module identity changed after our load'}
    stats = SYS / 'kernel/debug/hermit/stats'
    if stats.exists():
        current = stats_fields(stats.read_text())
        if any(current.get(key, -1) != 0 for key in ('live_slots', 'allocated_pages', 'inflight')):
            return {'status': 'PRESERVED', 'reason': 'backend resources are active or their counters are unknown'}
    elif (module / 'refcnt').read_text().strip() != '0':
        return {'status': 'PRESERVED', 'reason': 'backend counters unavailable and module references remain'}
    result = subprocess.run(['modprobe', '-r', '--first-time', 'rswap_client'],
                            capture_output=True, text=True)
    return {'status': 'UNLOADED' if result.returncode == 0 and not module.exists() else 'FAILED',
            'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


def start(args):
    parameters(args)
    attempts = getattr(args, 'connect_attempts', 3)
    delay = getattr(args, 'retry_delay_seconds', 1.0)
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise ValueError('connect-attempts must be 1..5')
    if not math.isfinite(delay) or not 0 <= delay <= 10:
        raise ValueError('retry-delay-seconds must be finite and in [0,10]')
    report = {'status': 'FAIL', 'ready': False, 'connection_tested': False,
              'attempts': [], 'maximum_attempts': attempts,
              'retry_policy': 'fresh-IB_CM_REJ_STALE_CONN-only-v1'}
    owned = None
    with control_lock():
        if (SYS / 'module/rswap_client').exists():
            raise ConnectionFailure('Hermit is already loaded; inspect status or stop it before changing parameters', report)
        try:
            for module in ('mlx5_core', 'mlx5_ib', 'ib_ipoib', 'rdma_cm'):
                subprocess.run(['modprobe', module], check=True)
            report.update(check(args))
            report['status'] = 'FAIL'
            for number in range(1, attempts + 1):
                if (SYS / 'module/rswap_client').exists():
                    raise RuntimeError('Hermit appeared before this attempt; another backend was not unloaded')
                before = dmesg_snapshot()
                started = time.monotonic()
                attempt = {'attempt': number, 'started_unix_seconds': time.time()}
                report['attempts'].append(attempt)
                result = subprocess.run(['modprobe', '--first-time', 'rswap_client', *report['module_parameters']],
                                        capture_output=True, text=True)
                attempt.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
                if result.returncode == 0:
                    # --first-time cannot silently succeed for an existing
                    # module, unlike an ordinary modprobe invocation.
                    owned = module_identity()
                evidence = fresh_dmesg(before, dmesg_snapshot())
                attempt.update(duration_seconds=time.monotonic() - started, dmesg=evidence)
                if result.returncode == 0:
                    attempt['classification'] = 'CONNECTED_VERIFICATION_FAILED'
                    report['verification'] = verify_connected(args)
                    attempt['classification'] = 'CONNECTED_AND_VERIFIED'
                    report.update(status='PASS', ready=True, connection_tested=True, hermit=status(),
                                  connected_attempt=number)
                    return report
                attempt['classification'] = 'IB_CM_REJ_STALE_CONN' if stale_connection(evidence) else 'NONRETRYABLE_OR_UNPROVEN'
                if (SYS / 'module/rswap_client').exists():
                    raise RuntimeError('A module is present after failed --first-time load; it was not unloaded or retried')
                if attempt['classification'] != 'IB_CM_REJ_STALE_CONN' or number == attempts:
                    raise RuntimeError('Hermit connection failed: ' + attempt['classification'] +
                                       (' (attempt limit reached)' if number == attempts else ''))
                attempt['retry_delay_seconds'] = delay * number
                time.sleep(attempt['retry_delay_seconds'])
        except BaseException as error:
            if owned is not None:
                try:
                    report['cleanup'] = cleanup_owned(owned)
                except Exception as cleanup_error:
                    report['cleanup'] = {'status': 'FAILED', 'error': repr(cleanup_error)}
            report.update(status='FAIL', ready=False, connection_tested=False, error=repr(error))
            raise ConnectionFailure(str(error), report) from error


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['check', 'start', 'stop', 'status'])
    p.add_argument('--server', help='Memory server RDMA IP, required for check/start')
    p.add_argument('--port', type=int, default=9400)
    p.add_argument('--pool-mib', type=int, default=128)
    p.add_argument('--interface', help='Require the server route to use this Guest RDMA interface')
    p.add_argument('--connect-attempts', type=int, default=3,
                   help='Bounded start attempts; retries only a fresh IB CM stale-connection rejection (default: 3)')
    p.add_argument('--retry-delay-seconds', type=float, default=1,
                   help='Initial stale-connection retry delay, multiplied by failed attempt number (default: 1)')
    args = p.parse_args()
    if args.action in ('check', 'start') and not args.server:
        p.error('--server is required for check/start')
    if args.action == 'check':
        result = check(args)
    elif args.action == 'status':
        result = status()
    else:
        if os.geteuid() != 0:
            raise ValueError('start/stop must run as root inside the Guest')
        if args.action == 'start':
            result = start(args)
        else:
            with control_lock():
                subprocess.run(['modprobe', '-r', 'rswap_client'], check=True)
                result = status()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except ConnectionFailure as error:
        print(json.dumps(error.report, indent=2))
        print(f'hermit-guest: {error}', file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'hermit-guest: {error}', file=sys.stderr)
        raise SystemExit(1)
