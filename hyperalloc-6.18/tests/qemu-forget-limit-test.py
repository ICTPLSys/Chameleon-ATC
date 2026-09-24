#!/usr/bin/env python3
"""ASan/UBSan tests of production QEMU range, READY and FORGET indexes."""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "qemu/hw/virtio/virtio-llfree-chameleon.c.inc"


def extract(text, pattern):
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise RuntimeError("Production helper layout changed: " + pattern)
    return match.group(0)


def production_helpers():
    text = SOURCE.read_text()
    parts = [
        extract(text, r"^typedef struct ChRange \{.*?^\} ChRange;"),
        """typedef struct {
            GHashTable *ranges;
            IntervalTreeRoot live, forgotten;
            uint64_t record_limit, forgotten_intervals, forgotten_tokens, ready_pages;
            QTAILQ_HEAD(, ChRange) ready;
        } LLChameleon;""",
    ]
    parts += [
        extract(text, rf"^static {result} {name}\([^)]*\)\n\{{.*?^\}}")
        for result, name in (
            ("bool", "ch_live"), ("void", "ch_set_state"),
            ("bool", "ch_intersects"), ("bool", "ch_overlap_locked"),
            ("bool", "ch_was_forgotten"), ("int", "ch_remember_forgotten"),
            ("void", "ch_clear_forgotten"), ("uint64_t", "ch_ready_pages"),
            ("bool", "ch_record_available"),
        )
    ]
    return "\n\n".join(parts)


PREAMBLE = r"""
#include "qemu/osdep.h"
#include "qemu/interval-tree.h"
#include "qemu/queue.h"
#include "standard-headers/linux/virtio_llfree_balloon.h"
#define LL_PAGE_SIZE 4096
typedef uint64_t hwaddr;
"""

TEST = r"""
int main(void)
{
    enum { N = 70000, LIVE = 8192 };
    LLChameleon ch = { .record_limit = N + 8,
                      .ranges = g_hash_table_new(g_direct_hash, g_direct_equal) };
    QTAILQ_INIT(&ch.ready);
    /* A permutation tests both insertion orders and more than 65536 sparse
     * exact intervals. The same production interval-tree implementation is
     * compiled below, with ASan and UBSan instrumentation. */
    for (unsigned i = 0; i < N; i++)
        assert(ch_remember_forgotten(&ch, 4ULL * ((i * 4093ULL) % N) + 4) == 0);
    assert(ch.forgotten_intervals == N && ch.forgotten_tokens == N);
    for (unsigned i = 0; i < N; i++) {
        assert(ch_was_forgotten(&ch, 4ULL * i + 4));
        assert(!ch_was_forgotten(&ch, 4ULL * i + 5));
    }
    uint64_t before = ch.forgotten_tokens;
    assert(ch_remember_forgotten(&ch, 4) == -EALREADY);
    assert(ch.forgotten_intervals == N && ch.forgotten_tokens == before);
    for (unsigned i = 1; i <= 8; i++)
        g_hash_table_insert(ch.ranges, GUINT_TO_POINTER(i), GUINT_TO_POINTER(i));
    assert(!ch_record_available(&ch, 0) && !ch_record_available(&ch, 1));
    assert(ch_remember_forgotten(&ch, 3) == 0); /* Extend next. */
    assert(ch_remember_forgotten(&ch, 5) == 0); /* Extend previous. */
    assert(ch_remember_forgotten(&ch, 6) == 0);
    assert(ch_remember_forgotten(&ch, 7) == 0); /* Bridge at capacity. */
    assert(ch.forgotten_intervals == N - 1 && ch_record_available(&ch, 1));
    assert(!ch_record_available(&ch, 2));
    for (uint64_t token = 3; token <= 8; token++)
        assert(ch_was_forgotten(&ch, token));
    uint64_t next = 4ULL * N + 4;
    assert(ch_remember_forgotten(&ch, next) == 0);
    assert(!ch_record_available(&ch, 1));
    /* Real FORGET consumes a terminal record and replaces it with at most
     * one sparse interval, keeping the shared metadata budget unchanged. */
    assert(ch_remember_forgotten(&ch, next + 4) == 0);
    assert(g_hash_table_remove(ch.ranges, GUINT_TO_POINTER(8)));
    assert(ch.forgotten_intervals + g_hash_table_size(ch.ranges) == ch.record_limit);
    assert(!ch_record_available(&ch, 1));
    ch_clear_forgotten(&ch);
    assert(ch.forgotten_intervals == 0 && interval_tree_is_empty(&ch.forgotten));
    assert(ch_record_available(&ch, N));
    ch.forgotten_tokens = 0;
    assert(ch_remember_forgotten(&ch, UINT64_MAX) == 0);
    assert(ch_remember_forgotten(&ch, UINT64_MAX - 1) == 0);
    assert(ch_remember_forgotten(&ch, 0) == 0);
    assert(ch.forgotten_intervals == 2 && ch.forgotten_tokens == 3);
    assert(ch_was_forgotten(&ch, 0) && ch_was_forgotten(&ch, UINT64_MAX));
    assert(!ch_was_forgotten(&ch, 1));
    ch_clear_forgotten(&ch);

    ChRange *ranges = g_new0(ChRange, LIVE);
    for (unsigned i = 0; i < LIVE; i++) {
        ChRange *r = &ranges[i];
        r->gpa = (uint64_t)(i + 1) * 2 * LL_PAGE_SIZE;
        r->pages = 1;
        r->state = LL_CH_STATE_REGISTERED;
        r->live.start = r->gpa;
        r->live.last = r->gpa + LL_PAGE_SIZE - 1;
        interval_tree_insert(&r->live, &ch.live);
        ch_set_state(&ch, r, LL_CH_STATE_READY);
    }
    assert(ch_ready_pages(&ch) == LIVE);
    for (unsigned i = 0; i < LIVE; i++) {
        ChRange *r = &ranges[i];
        assert(ch_overlap_locked(&ch, r->gpa, LL_PAGE_SIZE, false));
        assert(!ch_overlap_locked(&ch, r->gpa + LL_PAGE_SIZE, LL_PAGE_SIZE, false));
        assert(!ch_overlap_locked(&ch, r->gpa, LL_PAGE_SIZE, true));
        assert(QTAILQ_FIRST(&ch.ready) == r);
        ch_set_state(&ch, r, LL_CH_STATE_FINALIZING);
        ch_set_state(&ch, r, LL_CH_STATE_BLOCKED);
        assert(ch_overlap_locked(&ch, r->gpa, LL_PAGE_SIZE, true));
        ch_set_state(&ch, r, LL_CH_STATE_RETIRED);
    }
    assert(!ch_ready_pages(&ch) && QTAILQ_EMPTY(&ch.ready));
    for (unsigned i = LIVE; i--;) {
        ChRange *r = &ranges[i];
        ch_set_state(&ch, r, LL_CH_STATE_INSTALLED);
        assert(!ch_overlap_locked(&ch, r->gpa, LL_PAGE_SIZE, false));
    }
    assert(interval_tree_is_empty(&ch.live));
    /* Cancellation must remove both READY accounting and its GPA node. */
    ranges[0].state = LL_CH_STATE_REGISTERED;
    interval_tree_insert(&ranges[0].live, &ch.live);
    ch_set_state(&ch, &ranges[0], LL_CH_STATE_READY);
    ch_set_state(&ch, &ranges[0], LL_CH_STATE_CANCELED);
    assert(!ch_ready_pages(&ch) && QTAILQ_EMPTY(&ch.ready));
    assert(interval_tree_is_empty(&ch.live));
    g_free(ranges);
    g_hash_table_destroy(ch.ranges);
    puts("PASS production indexes: 70000 exact sparse FORGET intervals, shared metadata "
         "budget, replay rejection, coalescing, UINT64 boundaries; 8192 live GPA ranges, "
         "READY queue, exact overlap, reverse installation and cancellation");
    return 0;
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    parser.add_argument("--qemu-build", type=Path, default=ROOT / "build/qemu")
    args = parser.parse_args()
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "glib-2.0"], text=True))
    source = PREAMBLE + production_helpers() + TEST
    with tempfile.TemporaryDirectory(prefix="chameleon-qemu-index-") as directory:
        path = Path(directory)
        c_file, binary = path / "test.c", path / "test"
        c_file.write_text(source)
        subprocess.run(shlex.split(args.cc) + [
            "-std=gnu11", "-D_GNU_SOURCE", "-g", "-Wall", "-Wextra", "-Werror",
            "-I", str(args.qemu_build), "-I", str(ROOT / "qemu/include"),
            "-I", str(ROOT / "qemu"),
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-fno-omit-frame-pointer", str(c_file),
            str(ROOT / "qemu/util/interval-tree.c"), *flags, "-o", str(binary),
        ], check=True)
        print("Production source: " + str(SOURCE.relative_to(ROOT)), flush=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
