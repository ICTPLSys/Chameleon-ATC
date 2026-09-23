#!/usr/bin/env python3
"""Create an independent Ubuntu cloud-image disk Guest, without libvirt.

The default downloads the official Ubuntu 22.04 amd64 cloud image. --image
uses an existing image instead. Creation never starts QEMU or changes the Host
network. Cloud-init prepares SSH; run guest-setup.sh in the Guest to install
the development and RDMA userspace packages.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE_URL = 'https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img'


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', default='chameleon')
    source = p.add_mutually_exclusive_group()
    source.add_argument('--image', type=Path, help='Existing qcow2 or raw cloud image, left unchanged')
    source.add_argument('--image-url', help='HTTPS cloud image URL; default: official Ubuntu 22.04 amd64')
    p.add_argument('--disk-size', default='40G', help='Final virtual size, e.g. 40G (cannot shrink the source)')
    p.add_argument('--ssh-port', type=int, default=5022, help='Host loopback TCP port forwarded to Guest SSH')
    p.add_argument('--user', default='ubuntu', help='Guest SSH user with passwordless sudo')
    p.add_argument('--memory-mib', type=int, default=8192)
    p.add_argument('--cpus', type=int, default=4)
    p.add_argument('--qemu', type=Path, default=ROOT / 'build/deploy-qemu/qemu-system-x86_64',
                   help='Custom Chameleon QEMU binary; relative paths resolve from the current directory')
    return p


def disk_size(value):
    match = re.fullmatch(r'([1-9][0-9]*)([KMGT])', value.upper())
    if not match:
        raise ValueError('disk-size must be a positive integer followed by K, M, G or T')
    return int(match[1]) * 1024 ** ('KMGT'.index(match[2]) + 1)


def image_info(tool, image, size):
    result = subprocess.run([tool, 'info', '--output=json', str(image)],
                            check=True, capture_output=True, text=True)
    info = json.loads(result.stdout)
    if info.get('format') not in ('qcow2', 'raw'):
        raise ValueError('Source must be a qcow2 or raw disk image')
    if info.get('encrypted'):
        raise ValueError('Encrypted source images are unsupported')
    if type(info.get('virtual-size')) is not int or info['virtual-size'] <= 0:
        raise ValueError('Source image has no valid virtual size')
    if info['virtual-size'] > size:
        raise ValueError(f"disk-size is smaller than source virtual size ({info['virtual-size']} bytes)")
    return info


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def cloud_config(name, user, public_key, setup_script):
    # JSON is a YAML subset and avoids a PyYAML dependency or YAML interpolation.
    return {
        'hostname': name.lower().replace('_', '-'),
        'manage_etc_hosts': True,
        'disable_root': True,
        'ssh_pwauth': False,
        'ssh_deletekeys': True,
        'users': [{
            'name': user, 'groups': ['sudo'], 'shell': '/bin/bash',
            'lock_passwd': True, 'sudo': 'ALL=(ALL) NOPASSWD:ALL',
            'ssh_authorized_keys': [public_key],
        }],
        'package_update': False,
        'package_upgrade': False,
        'write_files': [
            {'path': '/usr/local/sbin/chameleon-guest-setup', 'permissions': '0755',
             'owner': 'root:root', 'content': setup_script},
            {'path': '/etc/default/grub.d/99-chameleon-console.cfg',
             'permissions': '0644', 'owner': 'root:root',
             'content': 'GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8"\n'
                        'GRUB_TERMINAL="serial console"\n'
                        'GRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"\n'
                        'GRUB_TIMEOUT=2\n'},
        ],
        'runcmd': [
            ['update-grub'],
            ['systemctl', 'enable', '--now', 'ssh.service'],
            ['systemctl', 'enable', '--now', 'serial-getty@ttyS0.service'],
        ],
        'final_message': 'Chameleon Guest SSH is ready; run chameleon-guest-setup to install development tools.',
    }


def seed_image(tool, output, directory):
    name = Path(tool).name
    if name == 'cloud-localds':
        command = [tool, '--network-config=' + str(directory / 'network-config'),
                   str(output), str(directory / 'user-data'), str(directory / 'meta-data')]
    else:
        command = [tool]
        if name == 'xorriso':
            command += ['-as', 'mkisofs']
        command += ['-quiet', '-output', str(output), '-volid', 'cidata',
                    '-joliet', '-rock', 'user-data', 'meta-data', 'network-config']
    subprocess.run(command, cwd=directory, check=True)


def create(args):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}', args.name):
        raise ValueError('name must contain 1–40 letters, digits, underscores or hyphens and start with a letter/digit')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', args.user) or args.user == 'root':
        raise ValueError('user must be a non-root Linux login name, at most 31 characters')
    for key, low, high in [('ssh_port', 1, 65535), ('memory_mib', 512, 1048576), ('cpus', 1, 256)]:
        value = getattr(args, key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key} must be an integer in [{low}, {high}]')
    size = disk_size(args.disk_size)
    destination = ROOT / 'build/guests' / args.name
    if destination.exists() or destination.is_symlink():
        raise ValueError(f'Refusing to overwrite existing Guest: {destination}')
    source = args.image.expanduser().resolve() if args.image else None
    if source and not source.is_file():
        raise ValueError(f'Source image does not exist: {source}')
    url = args.image_url or DEFAULT_IMAGE_URL
    parsed = urllib.parse.urlparse(url)
    if not source and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password):
        raise ValueError('image-url must be an HTTPS URL without embedded credentials')
    qemu = args.qemu.expanduser().resolve()
    tools = {}
    for name in ['qemu-img', 'ssh-keygen']:
        tools[name] = shutil.which(name)
        if not tools[name]:
            raise ValueError(f'Missing {name}; install qemu-utils and openssh-client on the Host')
    iso_tool = next((shutil.which(name) for name in ['cloud-localds', 'genisoimage', 'xorriso']
                     if shutil.which(name)), None)
    if not iso_tool:
        raise ValueError('Install cloud-image-utils, genisoimage or xorriso to create the cloud-init seed ISO')
    setup_script = (ROOT / 'scripts/guest-setup.sh').read_text()
    info = image_info(tools['qemu-img'], source, size) if source else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    # mkdir is the ownership boundary: never clean up any pre-existing Guest.
    destination.mkdir(mode=0o700)
    previous_umask = os.umask(0o077)
    try:
        if source is None:
            source = destination / 'downloaded-image'
            print(f'Downloading {url}', file=sys.stderr, flush=True)
            with urllib.request.urlopen(url, timeout=60) as response, source.open('xb') as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            info = image_info(tools['qemu-img'], source, size)
            if info.get('backing-filename'):
                raise ValueError('Downloaded cloud image must not depend on an external backing file')
        disk = destination / 'disk.qcow2'
        subprocess.run([tools['qemu-img'], 'convert', '-f', info['format'], '-O', 'qcow2',
                        str(source), str(disk)], check=True)
        subprocess.run([tools['qemu-img'], 'resize', str(disk), str(size)], check=True)
        if args.image is None:
            source.unlink()
        identity = destination / 'id_ed25519'
        subprocess.run([tools['ssh-keygen'], '-q', '-t', 'ed25519', '-N', '',
                        '-C', f'chameleon-guest-{args.name}', '-f', str(identity)], check=True)
        identity.chmod(0o600)
        public_key = identity.with_suffix('.pub').read_text().strip()
        (destination / 'known_hosts').touch(mode=0o600)
        mac = '52:54:00:' + ':'.join(f'{b:02x}' for b in hashlib.sha256(args.name.encode()).digest()[:3])
        config = cloud_config(args.name, args.user, public_key, setup_script)
        (destination / 'user-data').write_text('#cloud-config\n' + json.dumps(config, indent=2) + '\n')
        write_json(destination / 'meta-data', {'instance-id': 'chameleon-' + args.name,
                                              'local-hostname': args.name.lower().replace('_', '-')})
        write_json(destination / 'network-config', {
            'version': 2,
            # Match the permanent MAC across distro/custom kernel boots.
            # A set-name also makes netplan require that new name in .network;
            # an initramfs rename can then leave the NIC without DHCP.
            'ethernets': {'management': {'match': {'macaddress': mac},
                                        'dhcp4': True, 'dhcp6': False, 'optional': False}},
        })
        seed = destination / 'seed.iso'
        seed_image(iso_tool, seed, destination)
        vm = {'name': args.name, 'qemu': str(qemu), 'kernel': None,
              'disk': str(disk), 'disk_format': 'qcow2', 'seed': str(seed),
              'memory_mib': args.memory_mib, 'cpus': args.cpus, 'network': 'user',
              'ssh_port': args.ssh_port, 'mac': mac, 'vfio': [],
              'pebs': True, 'chameleon': True, 'policy': True, 'reboot': True}
        write_json(destination / 'vm.json', vm)
        write_json(destination / 'bootstrap.json', {**vm, 'pebs': False, 'chameleon': False, 'policy': False})
        access = {'name': args.name, 'host': '127.0.0.1', 'port': args.ssh_port,
                  'user': args.user, 'identity_file': str(identity),
                  'known_hosts': str(destination / 'known_hosts'),
                  'vm_config': str(destination / 'vm.json'),
                  'bootstrap_config': str(destination / 'bootstrap.json')}
        write_json(destination / 'access.json', access)
        return access
    except BaseException:
        shutil.rmtree(destination)
        raise
    finally:
        os.umask(previous_umask)


def main():
    args = parser().parse_args()
    access = create(args)
    print(json.dumps({'status': 'CREATED', 'access': access,
                      'next': 'Start with the bootstrap configuration, wait for cloud-init, then run Guest setup and install the Guest kernel.'},
                     indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'create-deploy-guest: {error}', file=sys.stderr)
        raise SystemExit(1)
