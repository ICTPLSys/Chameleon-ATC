#!/usr/bin/env python3
"""Exercise production Hermit RDMA segmentation and DMA lifetime with ASan/UBSan.

Only the RDMA/DMA/device boundary is mocked. The tested wait/transfer functions
are extracted from the current rswap_rdma.c on every invocation. No VM or RNIC
is required; actual RDMA integration remains a separate Hermit regression.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hermit/client/rswap_rdma.c"
FUNCTIONS = ("wait_request", "transfer_segment", "transfer")
SCENARIOS = 18


def production_helpers(source):
    parts = []
    for name in FUNCTIONS:
        pattern = rf"^static int {name}\([^)]*\)\n\{{.*?^\}}"
        match = re.search(pattern, source, re.MULTILINE | re.DOTALL)
        if match is None:
            raise RuntimeError("Production helper layout changed: " + name)
        line = source.count("\n", 0, match.start()) + 1
        parts.append(f'#line {line} "hermit/client/rswap_rdma.c"\n' + match.group(0))
    return "\n\n".join(parts)


# Test devices expose deterministic DMA addresses, segment limits and CQ
# failures. A successful CQ performs the requested copy; unmap asserts that
# the operation has completed or the production timeout path drained its QP.
PREAMBLE = r"""

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef long long atomic64_t;
#define PAGE_SIZE 4096UL
#define PAGE_SHIFT 12
#define RSWAP_TIMEOUT_MS 10000
#define IB_SEND_SIGNALED 1
#define IB_WR_RDMA_WRITE 1
#define IB_WR_RDMA_READ 2
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define max_t(t, a, b) ((t)(a) > (t)(b) ? (t)(a) : (t)(b))
#define round_down(a, b) ((a) & ~((__typeof__(a))(b)-1))
#define check_add_overflow(a, b, c) __builtin_add_overflow(a, b, c)
#define atomic64_inc(p) (++*(p))
#define atomic64_add(v, p) (*(p) += (v))
#define mutex_lock(p) ((void)(p))
#define mutex_unlock(p) ((void)(p))
#define msecs_to_jiffies(n) (n)
enum dma_data_direction { DMA_TO_DEVICE, DMA_FROM_DEVICE };
struct ib_device {
    int dummy;
};
struct page {
    unsigned char *data;
};
struct folio {
    unsigned long nr;
    struct page pages[512];
};
struct completion {
    int dummy;
};
struct ib_cqe {
    int dummy;
};
struct rswap_completion {
    struct ib_cqe cqe;
    struct completion complete;
    int error;
    u32 bytes;
};
struct ib_sge {
    u64 addr;
    u32 length, lkey;
};
struct ib_send_wr {
    struct ib_sge *sg_list;
    int num_sge, send_flags, opcode;
    struct ib_cqe *wr_cqe;
};
struct ib_rdma_wr {
    struct ib_send_wr wr;
    u64 remote_addr;
    u32 rkey;
};
struct rdma_id {
    struct ib_device *device;
    void *qp;
};
struct pd {
    u32 local_dma_lkey;
};
static struct {
    struct rdma_id *id;
    struct pd *pd;
    bool broken;
    unsigned long pages;
    size_t max_transfer;
    u64 segment_boundary;
    struct {
        int mapped_chunk;
        u64 mapped_size[16], buf[16];
        u32 rkey[16];
    } regions;
} remote;
static int io_lock;
static atomic64_t write_wrs, read_wrs, write_completions, read_completions, write_bytes,
    read_bytes, map_failures, map_retries, transfer_errors, region_splits, limit_splits;
static u32 largest_write_wr, largest_read_wr;
static unsigned char *local, *peer;
static struct ib_device dev;
static struct rdma_id id = {&dev, (void *)1};
static struct pd pd = {1};
static size_t map_limit, mapped_len, bias;
static u64 mapped_addr;
static bool mapped, outstanding;
static int posts, post_fail, cq_fail, timeout_at, drains;
static struct ib_rdma_wr pending;
static struct rswap_completion *request;
static unsigned long folio_nr_pages(struct folio *f) { return f->nr; }
static struct page *folio_page(struct folio *f, unsigned long n) {
    assert(n < f->nr);
    return &f->pages[n];
}
static u64 ib_dma_map_page(struct ib_device *d, struct page *p, unsigned long off, size_t n,
                           enum dma_data_direction dir) {
    (void)d;
    (void)dir;
    assert(!mapped && !outstanding);
    assert(!off);
    assert(n && !(n % PAGE_SIZE));
    if (n > map_limit)
        return UINT64_MAX;
    mapped = true;
    mapped_len = n;
    mapped_addr = (uintptr_t)p->data + bias;
    return mapped_addr;
}
static int ib_dma_mapping_error(struct ib_device *d, u64 addr) {
    (void)d;
    return addr == UINT64_MAX;
}
static void ib_dma_unmap_page(struct ib_device *d, u64 addr, size_t n,
                              enum dma_data_direction dir) {
    (void)d;
    (void)dir;
    assert(mapped && !outstanding && addr == mapped_addr && n == mapped_len);
    mapped = false;
}
static void init_request(struct rswap_completion *r) { memset(r, 0, sizeof(*r)); }
static int ib_post_send(void *qp, struct ib_send_wr *wr, const struct ib_send_wr **bad) {
    (void)qp;
    (void)bad;
    assert(mapped && !outstanding);
    assert(wr->num_sge == 1);
    assert(wr->sg_list->length == mapped_len);
    assert((mapped_addr & ~remote.segment_boundary) ==
           ((mapped_addr + mapped_len - 1) & ~remote.segment_boundary));
    pending = *(struct ib_rdma_wr *)wr;
    bool found = false;
    for (int i = 0; i < remote.regions.mapped_chunk; i++)
        if (pending.rkey == remote.regions.rkey[i]) {
            assert(pending.remote_addr >= remote.regions.buf[i]);
            assert(pending.remote_addr + mapped_len <=
                   remote.regions.buf[i] + remote.regions.mapped_size[i]);
            found = true;
        }
    assert(found);
    posts++;
    if (posts == post_fail)
        return -EIO;
    request = (struct rswap_completion *)wr->wr_cqe;
    outstanding = true;
    return 0;
}
static unsigned long wait_for_completion_timeout(struct completion *c, unsigned long timeout) {
    (void)c;
    (void)timeout;
    assert(mapped && outstanding);
    if (posts == timeout_at)
        return 0;
    size_t n = posts == cq_fail ? mapped_len / 2 : mapped_len;
    if (pending.wr.opcode == IB_WR_RDMA_WRITE)
        memcpy((void *)(uintptr_t)pending.remote_addr, (void *)(uintptr_t)(mapped_addr - bias),
               n);
    else
        memcpy((void *)(uintptr_t)(mapped_addr - bias), (void *)(uintptr_t)pending.remote_addr,
               n);
    request->error = posts == cq_fail ? -EIO : 0;
    outstanding = false;
    return 1;
}
static void ib_drain_qp(void *qp) {
    (void)qp;
    assert(mapped && outstanding);
    outstanding = false;
    drains++;
}
"""

TEST = r"""
static struct folio f;
static void setup(unsigned long nr) {
    assert(!mapped && !outstanding);
    memset(&remote, 0, sizeof(remote));
    remote.id = &id;
    remote.pd = &pd;
    remote.pages = nr + 16;
    remote.max_transfer = 2UL << 20;
    remote.segment_boundary = UINT64_MAX;
    remote.regions.mapped_chunk = 1;
    remote.regions.buf[0] = (uintptr_t)peer;
    remote.regions.mapped_size[0] = (nr + 16) * PAGE_SIZE;
    remote.regions.rkey[0] = 17;
    map_limit = SIZE_MAX;
    bias = 0;
    posts = post_fail = cq_fail = timeout_at = drains = 0;
    write_wrs = read_wrs = write_completions = read_completions = write_bytes = read_bytes =
        map_failures = map_retries = transfer_errors = region_splits = limit_splits = 0;
    largest_write_wr = largest_read_wr = 0;
    f.nr = nr;
    for (unsigned long i = 0; i < nr; i++)
        f.pages[i].data = local + i * PAGE_SIZE;
    for (unsigned long i = 0; i < nr * PAGE_SIZE; i++)
        local[i] = (unsigned char)((i * 197 + (i >> 12) * 31) ^ 0x96);
    memset(peer, 0, (nr + 16) * PAGE_SIZE);
}
static void roundtrip(unsigned long slot) {
    size_t n = f.nr * PAGE_SIZE;
    unsigned char *expected = malloc(n);
    assert(expected);
    memcpy(expected, local, n);
    assert(!transfer(slot, &f, true));
    assert(!memcmp(expected, peer + slot * PAGE_SIZE, n));
    memset(local, 0xa5, n);
    assert(!transfer(slot, &f, false));
    assert(!memcmp(expected, local, n));
    assert(!mapped && !outstanding);
    free(expected);
}
int main(void) {
    assert(!posix_memalign((void **)&local, 2UL << 20, 4UL << 20));
    assert(!posix_memalign((void **)&peer, 2UL << 20, 4UL << 20));
    unsigned orders[] = {0, 2, 3, 4, 5, 6, 7, 8, 9};
    for (unsigned i = 0; i < sizeof(orders) / sizeof(*orders); i++) {
        setup(1UL << orders[i]);
        roundtrip(0);
        assert(write_wrs == 1 && read_wrs == 1 && largest_write_wr == f.nr * PAGE_SIZE);
    }
    setup(512);
    remote.max_transfer = 65536;
    roundtrip(0);
    assert(write_wrs == 32 && read_wrs == 32 && largest_write_wr == 65536);
    setup(16);
    remote.regions.mapped_chunk = 2;
    remote.regions.mapped_size[0] = 12 * PAGE_SIZE;
    remote.regions.buf[1] = (uintptr_t)(peer + 12 * PAGE_SIZE);
    remote.regions.mapped_size[1] = (remote.pages - 12) * PAGE_SIZE;
    remote.regions.rkey[1] = 19;
    roundtrip(8);
    assert(write_wrs == 2 && read_wrs == 2);
    setup(512);
    map_limit = 65536;
    roundtrip(0);
    assert(map_failures > 0 && map_retries > 0 && largest_write_wr <= 65536);
    setup(512);
    remote.segment_boundary = 65535;
    remote.max_transfer = 65536;
    bias = 4096;
    roundtrip(0);
    assert(map_retries > 0 && write_wrs == 33 && read_wrs == 33);
    setup(16);
    map_limit = 0;
    assert(transfer(0, &f, true) == -EIO && posts == 0 && !mapped && !outstanding);
    setup(16);
    post_fail = 1;
    assert(transfer(0, &f, true) == -EIO && write_wrs == 0 && !mapped && !outstanding);
    setup(16);
    remote.max_transfer = PAGE_SIZE;
    cq_fail = 2;
    assert(transfer(0, &f, false) == -EIO && read_wrs == 2 && read_completions == 1 &&
           remote.broken && !mapped && !outstanding);
    setup(16);
    timeout_at = 1;
    assert(transfer(0, &f, false) == -ETIMEDOUT && drains == 1 && remote.broken && !mapped &&
           !outstanding);
    setup(16);
    assert(transfer(remote.pages - 15, &f, true) == -ERANGE && !posts);
    free(local);
    free(peer);
    puts("PASS extracted production RDMA transfer: orders 0,2-9 full-byte roundtrip; 64 KiB "
         "limit; cross-MR offset/rkey; map failure halving; actual DMA boundary; unmapped "
         "post/CQ/timeout failure; capacity guard");
    return 0;
}
"""

COVERAGE = [
    "orders 0,2-9 full-byte roundtrip",
    "64 KiB device limit",
    "remote MR crossing with nonzero pool slot and distinct rkey",
    "DMA map failure and halving fallback",
    "mapped DMA address segment boundary",
    "map failure at minimum PAGE_SIZE",
    "post failure before ownership transfer",
    "CQ error after a completed partial segment",
    "timeout drains QP before unmap",
    "remote capacity guard"
]


def relative(path):
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=os.environ.get("CC", "clang"))
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build")
    parser.add_argument("--output", type=Path, default=ROOT / "results/rdma-transfer-unit.json")
    args = parser.parse_args()
    build = args.build_dir.resolve()
    output = args.output.resolve()
    build.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    harness = build / "rdma-transfer-unit.c"
    binary = build / "rdma-transfer-unit"
    source = SOURCE.read_text()
    # The kernel build disables sign-compare too: the wire protocol uses an
    # int region count, validated positive before the production loop runs.
    command = shlex.split(args.cc) + [
        "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wno-sign-compare",
        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
        relative(harness), "-o", relative(binary),
    ]
    report = {
        "status": "FAIL",
        "scope": "extracted production wait_request, transfer_segment and transfer; "
                 "mocked RDMA/DMA operations, actual full-byte memory copies",
        "source": relative(SOURCE),
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "harness": relative(harness),
        "compile_command": command,
        "scenarios": 0,
        "sanitizers": ["address", "undefined"],
        "coverage": COVERAGE,
        "stdout": "",
    }
    try:
        harness.write_text(PREAMBLE + production_helpers(source) +
                           '\n#line 1 "rdma_transfer_tests.c"\n' + TEST)
        subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
        result = subprocess.run([str(binary)], cwd=ROOT, text=True, capture_output=True, check=True)
        report.update(status="PASS", scenarios=SCENARIOS, stdout=result.stdout)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        report["error"] = repr(error)
        if isinstance(error, subprocess.CalledProcessError):
            report["stdout"] = error.stdout or ""
            report["stderr"] = error.stderr or ""
    output.write_text(json.dumps(report, indent=2) + "\n")
    if report["status"] != "PASS":
        print(json.dumps(report, indent=2))
        raise SystemExit(1)
    print(report["stdout"], end="")
    print("Report: " + str(output))


if __name__ == "__main__":
    main()
