#!/usr/bin/env python3
"""Offline checks for relocatable setup commands; never builds/boots/installs."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class SetupTests(unittest.TestCase):
    def run_script(self, name, *args):
        return subprocess.run(['python3', str(ROOT / 'scripts' / name), *args],
                              cwd='/tmp', text=True, capture_output=True)

    def test_plans_without_existing_vm_or_root(self):
        for name in ('create-template.py', 'deploy-benchmarks.py', 'build-system.py', 'prepare-rdma-server.py'):
            with self.subTest(name=name):
                result = self.run_script(name, '--plan')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('/path/to/chameleon-ae', result.stdout)
                self.assertIn(str(ROOT), result.stdout)

    def test_inventory_vf_disjoint_and_single_reuse(self):
        result = self.run_script('configure-site.py', '--vf', '0000:69:00.3',
                                 '--vf', '0000:69:00.4', '--vf', '0000:69:00.5')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(len({x['vfio'][0] for x in data['slots']}), 3)
        self.assertEqual(data['single_slot']['vfio'], data['slots'][0]['vfio'])

    def test_reject_duplicate_vf(self):
        result = self.run_script('configure-site.py', '--vf', '0000:69:00.3',
                                 '--vf', '0000:69:00.3', '--vf', '0000:69:00.5')
        self.assertNotEqual(result.returncode, 0)

    def test_pool_ports_are_distinct_and_override_checked(self):
        default = json.loads(self.run_script('configure-site.py').stdout)
        self.assertEqual(default['single_slot']['server_port'], 9404)
        self.assertNotIn(default['single_slot']['server_port'],
                         [slot['server_port'] for slot in default['slots']])
        self.assertNotEqual(self.run_script('configure-site.py', '--single-server-port', '9401').returncode, 0)
        self.assertNotEqual(self.run_script('configure-site.py', '--single-server-port', '65536').returncode, 0)
        changed = self.run_script('configure-site.py', '--single-server-port', '9410')
        self.assertEqual(json.loads(changed.stdout)['single_slot']['server_port'], 9410)

    def test_json_writer_does_not_replace_existing_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host.json'
            result = self.run_script('configure-site.py', '--output', str(path), '--write')
            self.assertEqual(result.returncode, 0, result.stderr)
            initial = path.read_bytes()
            result = self.run_script('configure-site.py', '--output', str(path), '--write')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), initial)

    def test_server_path_is_shell_quoted(self):
        script = module('prepare-rdma-server').remote_script('/opt/space dir/server/rswap-server')
        self.assertIn("'/opt/space dir/server/rswap-server'", script)
        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_relative_path_is_rejected(self):
        with self.assertRaises(ValueError):
            module('prepare-rdma-server').remote_script('~/server')

    def test_plan_stages_use_frozen_graph500_and_stop_template(self):
        result = self.run_script('deploy-benchmarks.py', '--plan', '--inventory',
                                 str(ROOT / 'ae/config/host.example.json'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--bfs-iterations 32', result.stdout)
        self.assertIn('--scale 25 --edgefactor 18', result.stdout)
        self.assertIn('--prepare-only', result.stdout)
        self.assertIn('--name chameleon-template stop', result.stdout)


if __name__ == '__main__':
    unittest.main()
