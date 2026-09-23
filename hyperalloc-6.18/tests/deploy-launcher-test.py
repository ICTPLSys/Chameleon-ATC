#!/usr/bin/env python3
"""Deployment contracts: boot args, RDMA routing, VFIO mapping prerequisites."""
import importlib.util
from pathlib import Path
import json
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


vm = module('deploy_vm', 'run-deploy-vm.py')
guest = module('hermit_guest', 'hermit-guest.py')


class Deployment(unittest.TestCase):
    def cfg(self, **kwargs):
        return vm.config(dict(disk='/tmp/os.qcow2', root='/dev/vda3') | kwargs)

    def test_vfio_order_and_backing(self):
        args = vm.command(self.cfg(vfio=['0000:69:02.0', '0000:69:02.1']))
        devices = [json.loads(args[i+1]) for i, a in enumerate(args) if a == '-device' and args[i+1].startswith('{')]
        self.assertEqual(devices[0]['driver'], 'virtio-llfree-balloon')
        self.assertTrue(devices[0]['chameleon-vfio'])
        self.assertTrue(devices[0]['vfio'])
        self.assertEqual(sum(d['driver'] == 'vfio-pci' for d in devices), 2)
        memory = json.loads(args[args.index('-object') + 1])
        self.assertFalse(memory['thp'])
        self.assertFalse(memory['share'])
        self.assertFalse(memory['prealloc'])
        self.assertFalse(any('intel-iommu' in a for a in args))
        self.assertIn('mem-merge=off', args[args.index('-machine')+1])

    def test_direct_boot_root_and_initrd(self):
        args = vm.command(self.cfg(root='UUID=abcd', initrd='/tmp/initrd', disk='/tmp/os,a.qcow2'))
        self.assertEqual(args[args.index('-initrd')+1], '/tmp/initrd')
        self.assertIn('root=UUID=abcd', args[args.index('-append')+1])
        self.assertIn('file=/tmp/os,,a.qcow2,format=qcow2,', args[args.index('-drive')+1])

    def test_user_network_qapi(self):
        args = vm.command(self.cfg(network='user'))
        self.assertEqual(json.loads(args[args.index('-netdev')+1])['hostfwd'],
                         [{'str': 'tcp:127.0.0.1:5022-:22'}])

    def test_disk_boot(self):
        args = vm.command(self.cfg(kernel=None))
        self.assertNotIn('-kernel', args)
        self.assertNotIn('-append', args)

    def test_regression_backing_unchanged(self):
        args = vm.command(self.cfg())
        self.assertNotIn('thp', json.loads(args[args.index('-object')+1]))
        self.assertNotIn('chameleon-vfio', ' '.join(args))

    def test_exact_vfio_capability_probe(self):
        requests = []
        def ioctl(fd, operation, value):
            if operation == 0xae01:
                return 101
            requests.append(struct.unpack('=II4Q64x', value))
            return 0
        with patch.object(vm.os, 'open', return_value=100), patch.object(vm.os, 'close') as close:
            with patch.object(vm.fcntl, 'ioctl', side_effect=ioctl):
                self.assertEqual(vm.probe_kvm(self.cfg(vfio=['0000:69:02.0'])), ['Chameleon VFIO', 'PEBS MEMINFO'])
            self.assertEqual(requests[0], (0x48410002, 0, 1, 1, 0, 0))
            self.assertEqual([x.args[0] for x in close.call_args_list], [101, 100])

    def test_old_host_rejects_vfio_opt_in(self):
        with patch.object(vm.os, 'open', return_value=100), patch.object(vm.os, 'close') as close:
            with patch.object(vm.fcntl, 'ioctl', side_effect=[101, OSError(22, 'Invalid argument')]):
                with self.assertRaisesRegex(ValueError, 'Host cannot enable Chameleon VFIO'):
                    vm.probe_kvm(self.cfg(vfio=['0000:69:02.0']))
            self.assertEqual(close.call_count, 2)

    def test_bad_config(self):
        for bad in [dict(root=None), dict(policy=True, chameleon=False), dict(cpus=0),
                    dict(vfio=['69:00.0']), dict(vfio=['0000:69:00.1']*2),
                    dict(kernel=None, initrd='/tmp/initrd'), dict(name='../x'),
                    dict(chameleon='off'), dict(host_cpus=[True]),
                    dict(vfio=['0000:69:00.1'], append='iommu=pt'),
                    dict(vfio=['0000:69:00.1'], snapshot=True)]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.cfg(**bad)

    def test_parameters(self):
        args = SimpleNamespace(server='2001:db8::1', port=9400, pool_mib=128)
        self.assertEqual(guest.parameters(args), ['backend=rdma', 'sip=2001:db8::1', 'sport=9400', 'pool_mb=128'])
        for address in ['127.0.0.1', '::', 'not-an-ip', '224.0.0.1']:
            args.server = address
            with self.subTest(address=address), self.assertRaises(ValueError):
                guest.parameters(args)

    def test_reject_management_route(self):
        with patch.object(guest.os, 'uname', return_value=SimpleNamespace(release='6.18.0-chameleon-guest')):
            with patch.object(guest, 'run', side_effect=['6.18.0-chameleon-guest SMP', '[{"dev":"eth0"}]']):
                with self.assertRaisesRegex(ValueError, 'Route uses eth0'):
                    guest.check(SimpleNamespace(server='192.0.2.1', port=9400, pool_mib=128, interface='ib0'))

    def test_ipoib_partition_and_vlan_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, index, parent in [('ib0', 2, 2), ('ib0.8001', 3, 2), ('vlan', 4, 3)]:
                dev = root / 'class/net' / name
                dev.mkdir(parents=True)
                (dev / 'ifindex').write_text(str(index))
                (dev / 'iflink').write_text(str(parent))
            (root / 'class/net/ib0/device/infiniband/mlx5_0').mkdir(parents=True)
            with patch.object(guest, 'SYS', root):
                self.assertEqual(guest.rdma_devices('ib0.8001'), ['mlx5_0'])
                self.assertEqual(guest.rdma_devices('vlan'), ['mlx5_0'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
