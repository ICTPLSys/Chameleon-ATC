#!/usr/bin/env python3
"""Bounded local workload checks; no VM, kernel interfaces, or root required.

Compiles the current source in a temporary directory. Checks address selection
at wraparound boundaries, the legacy contiguous sequence, ordinary-memory data
integrity, deterministic fixed-operation replay, and idle command transitions.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).with_name('chameleon_skew.c')
PATTERNS = ('contiguous', 'striped', 'uniform', 'sequential')


class WorkloadPatterns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='chameleon-skew-test-')
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        cls.binary = directory / 'chameleon_skew'
        compiler = shlex.split(os.environ.get('CC', 'cc'))
        flags = ['-O2', '-Wall', '-Wextra', '-Werror', '-pthread']
        subprocess.run([*compiler, *flags, str(SOURCE), '-lm', '-o', str(cls.binary)], check=True)
        fixture = directory / 'selection.c'
        fixture.write_text('#define main workload_main\n#include ' + json.dumps(str(SOURCE)) + r'''
#undef main
#include <assert.h>

int main(void)
{
    struct worker w = {.pages = 1024, .hot_pages = 16, .hot_start = 508, .random = 1};
    bool seen[1024] = {0};
    bench.pattern = PAT_STRIPED;
    bench.stripe_hot_pages = 8;
    bench.hot_access_ppm = 1000000;
    for (unsigned i = 0; i < 100000; i++) {
        uint64_t page = select_page(&w);
        assert(page < w.pages);
        assert(page % 512 >= 508 || page % 512 < 4);
        seen[page] = true;
    }
    for (unsigned p = 0; p < 1024; p++)
        assert(seen[p] == (p % 512 >= 508 || p % 512 < 4));

    bench.pattern = PAT_UNIFORM;
    memset(seen, 0, sizeof(seen));
    for (unsigned i = 0; i < 100000; i++) {
        uint64_t page = select_page(&w);
        assert(page < w.pages);
        seen[page] = true;
    }
    for (unsigned p = 0; p < 1024; p++) assert(seen[p]);

    bench.pattern = PAT_SEQUENTIAL;
    w.sequential_page = 1022;
    for (unsigned i = 0; i < 3072; i++)
        assert(select_page(&w) == (1022 + i) % 1024);

    /* Keep the original default contiguous page/RNG sequence unchanged. */
    bench.pattern = PAT_CONTIGUOUS;
    bench.hot_access_ppm = 990000;
    w.hot_pages = 10;
    w.hot_start = 1020;
    struct worker legacy = w;
    for (unsigned i = 0; i < 100000; i++) {
        uint64_t expected;
        if (random_next(&legacy) % 1000000 < bench.hot_access_ppm) {
            expected = legacy.hot_start + random_next(&legacy) % legacy.hot_pages;
            if (expected >= legacy.pages) expected -= legacy.pages;
        } else {
            expected = random_next(&legacy) % legacy.pages;
        }
        assert(select_page(&w) == expected);
        assert(w.random == legacy.random);
    }
    return 0;
}
''')
        cls.selector = directory / 'selection'
        subprocess.run([*compiler, *flags, str(fixture), '-lm', '-o', str(cls.selector)], check=True)

    def workload(self, *extra, commands='run\nshift 99.99\nrun\nverify\nquit\n', expected=0):
        result = subprocess.run([
            str(self.binary), '--mib', '8', '--threads', '4',
            '--ops-per-thread', '10000', '--write-percent', '50', *extra,
        ], input=commands, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    @staticmethod
    def records(output):
        return [json.loads(line) for line in output.splitlines() if line.startswith('{')]

    @staticmethod
    def field(line, name):
        return dict(item.split('=', 1) for item in line.split()[1:] if '=' in item)[name]

    def test_page_selection_and_legacy_sequence(self):
        subprocess.run([str(self.selector)], check=True, timeout=10)

    def test_all_patterns_full_data_integrity_and_fixed_operation_replay(self):
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern):
                first = self.workload('--pattern', pattern)
                second = self.workload('--pattern', pattern)
                records = self.records(first.stdout)
                runs = [record for record in records if record['phase'] == 'run']
                self.assertEqual(len(runs), 2)
                for record in runs:
                    self.assertEqual(record['pattern'], pattern)
                    self.assertEqual(record['touches'], 40000)
                    self.assertEqual(record['reads'] + record['writes'], 40000)
                    self.assertEqual(record['verify_errors'], 0)
                verification = next(record for record in records if record['phase'] == 'verify')
                self.assertEqual(verification['verify_bytes'], 8 << 20)
                self.assertEqual(verification['verify_errors'], 0)
                def ledgers(output):
                    return [(self.field(line, 'thread'), self.field(line, 'ledger_a'),
                             self.field(line, 'ledger_b')) for line in output.splitlines()
                            if line.startswith('SKEW_THREAD phase=verify ')]
                self.assertEqual(ledgers(first.stdout), ledgers(second.stdout))
                self.assertIn('SKEW_TOTAL status=PASS', first.stdout)

    def test_default_matches_explicit_contiguous(self):
        implicit, explicit = self.workload(), self.workload('--pattern', 'contiguous')
        for field in ('reads', 'writes', 'ledger_a', 'ledger_b'):
            def values(output):
                return [self.field(line, field) for line in output.splitlines()
                        if line.startswith('SKEW_THREAD ')]
            self.assertEqual(values(implicit.stdout), values(explicit.stdout))

    def test_pattern_switch_shift_and_hot_probability_commands(self):
        commands = ('pattern striped\nhot-access-ppm 1000000\nrun\nshift 50\nrun\n'
                    'pattern uniform\nhot-access-ppm 0\nrun\n'
                    'pattern sequential\nshift 99.99\nrun\n'
                    'pattern contiguous\nrun\nverify\nquit\n')
        result = self.workload(commands=commands)
        runs = [record for record in self.records(result.stdout) if record['phase'] == 'run']
        self.assertEqual([r['pattern'] for r in runs],
                         ['striped', 'striped', 'uniform', 'sequential', 'contiguous'])
        self.assertEqual([r['hot_access_ppm'] for r in runs], [1000000, 1000000, 0, 0, 0])
        self.assertIn('SKEW_SHIFT percent=50.000000 pattern=striped stripe_start_page=256', result.stdout)
        self.assertIn('SKEW_VERIFIED status=PASS bytes=8388608', result.stdout)

    def test_stripe_full_and_single_page_hot_windows(self):
        for pages in ('1', '512'):
            with self.subTest(pages=pages):
                result = self.workload('--pattern', 'striped', '--stripe-hot-pages', pages,
                                       '--hot-access-ppm', '1000000')
                self.assertIn('SKEW_TOTAL status=PASS', result.stdout)

    def test_invalid_options_fail_before_initialization(self):
        for options in (('--pattern', 'invalid'), ('--stripe-hot-pages', '0'),
                        ('--stripe-hot-pages', '513'), ('--stripe-hot-pages', '-1'),
                        ('--hot-access-ppm', '1000001'),
                        ('--pattern', 'striped', '--mib', '4')):
            with self.subTest(options=options):
                result = self.workload(*options, expected=2)
                self.assertNotIn('SKEW_START', result.stdout)

    def test_invalid_commands_preserve_verification(self):
        commands = ('pattern invalid\npattern\npattern striped extra\n'
                    'hot-access-ppm -1\nhot-access-ppm 1000001\nhot-access-ppm\n'
                    'shift 100\nquit\n')
        result = self.workload(commands=commands, expected=1)
        self.assertEqual(result.stderr.count('SKEW_COMMAND_ERROR'), 7)
        self.assertIn('SKEW_VERIFIED status=PASS bytes=8388608', result.stdout)
        self.assertIn('command_errors=7', result.stdout)

    def test_command_rejects_unaligned_striped_partition(self):
        result = self.workload('--mib', '4', commands='pattern striped\nrun\nquit\n', expected=1)
        self.assertIn('SKEW_COMMAND_ERROR command=pattern', result.stderr)
        runs = [record for record in self.records(result.stdout) if record['phase'] == 'run']
        self.assertEqual(runs[0]['pattern'], 'contiguous')
        self.assertIn('SKEW_VERIFIED status=PASS bytes=4194304', result.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
