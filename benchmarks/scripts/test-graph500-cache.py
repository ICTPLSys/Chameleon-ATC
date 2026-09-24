#!/usr/bin/env python3
"""Exercise real CSR save/load, fixed roots, OpenMP and runner cache reuse."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'benchmarks/apps/graph500-omp'
RUNNER = ROOT / 'benchmarks/scripts/run-graph500.sh'


class CacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (SOURCE / 'make.inc').exists():
            (SOURCE / 'make.inc').symlink_to('make-incs/make.inc-gcc')
        subprocess.run(['make', '-C', str(SOURCE), 'BUILD_OPENMP=Yes', 'CC=gcc',
                        'CFLAGS=-g -std=gnu99 -Wall -O3', 'CFLAGS_OPENMP=-fopenmp',
                        'LDLIBS=-lm -lrt', 'omp-csr/omp-csr'], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        cls.temp = tempfile.TemporaryDirectory(prefix='graph500-cache-test-')
        cls.directory = Path(cls.temp.name)
        cls.cache = cls.directory / 'graph.bin'
        cls.binary = SOURCE / 'omp-csr/omp-csr'
        cls.env = dict(os.environ, OMP_NUM_THREADS='2', OMP_PROC_BIND='close', OMP_PLACES='cores')
        cls.base = [str(cls.binary), '-s', '10', '-e', '8', '-n', '32', '-V']
        cls.generated = cls.run_binary(cls.base + ['-W', str(cls.cache)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def run_binary(cls, command):
        return subprocess.run(command, env=cls.env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)

    def test_load_preserves_roots_and_validates_all_32_bfs(self):
        loaded = self.run_binary(self.base + ['-L', str(self.cache)])
        for result in (self.generated, loaded):
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(re.findall(r'Verifying bfs (\d+)\.\.\.done', result.stdout),
                             [str(i) for i in range(32)])
            self.assertIn('OpenMP team size: 2', result.stdout)
        self.assertEqual(re.findall(r'^bfs_roots:.*$', self.generated.stdout, re.M),
                         re.findall(r'^bfs_roots:.*$', loaded.stdout, re.M))
        self.assertNotIn('Generating edge list', loaded.stdout)
        self.assertNotIn('Creating graph', loaded.stdout)

    def test_wrong_workload_and_truncated_cache_fail(self):
        for scale, count in [('11', '32'), ('10', '16')]:
            result = self.run_binary([str(self.binary), '-s', scale, '-e', '8',
                                      '-n', count, '-L', str(self.cache)])
            self.assertNotEqual(result.returncode, 0)
        truncated = self.directory / 'truncated.bin'
        truncated.write_bytes(self.cache.read_bytes()[:-128])
        self.assertNotEqual(self.run_binary(self.base + ['-L', str(truncated)]).returncode, 0)

    def test_runner_prepare_then_reuses_cache(self):
        cache = self.directory / 'runner.bin'
        base = ['bash', str(RUNNER), '--source', str(SOURCE), '--skip-build',
                '--scale', '10', '--edgefactor', '8', '--threads', '2',
                '--bfs-iterations', '32', '--graph-cache', str(cache)]
        prepared = self.run_binary(base + ['--prepare-only', '--output-base', str(self.directory / 'prepare')])
        self.assertEqual(prepared.returncode, 0, prepared.stdout)
        before = (cache.stat().st_mtime_ns, cache.stat().st_size)
        loaded = self.run_binary(base + ['--require-cache', '--output-base', str(self.directory / 'load')])
        self.assertEqual(loaded.returncode, 0, loaded.stdout)
        self.assertEqual(before, (cache.stat().st_mtime_ns, cache.stat().st_size))
        self.assertEqual(len(re.findall(r'Verifying bfs \d+\.\.\.done', loaded.stdout)), 32)
        self.assertIn('Loading CSR checkpoint... done.', loaded.stdout)
        self.assertEqual(len(list((self.directory / 'load').rglob('*time.txt'))), 1)

    def test_required_missing_cache_is_not_generated(self):
        cache = self.directory / 'missing.bin'
        result = self.run_binary(['bash', str(RUNNER), '--source', str(SOURCE), '--skip-build',
                                 '--require-cache', '--graph-cache', str(cache)])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(cache.exists())


if __name__ == '__main__':
    unittest.main()
