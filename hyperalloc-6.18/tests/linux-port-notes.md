# HyperAlloc Linux 6.18 port

## Baselines and build

Source is the unmodified official `linux-6.18.tar.xz` from kernel.org, committed locally and tagged `upstream-v6.18`. The LLFree wrapper and virtio driver originate from HyperAlloc artifact commit `628c12d183414a447e14a30414b1ee6158bf37f6`. `linux/mm/llfree/llc` is a relative symlink to `../../../llfree-c`; Linux and QEMU therefore consume the same shared implementation. This is a real LLFree page allocator, not a buddy-backed adapter.

The build uses `CC=clang` with GNU binutils because this machine has Clang but no `ld.lld`. The recorded configuration is `configs/guest.config`. Build commands, from this directory's parent:

```sh
make -C linux O="$PWD/build/guest" CC=clang LOCALVERSION= -j24 bzImage modules
make -C linux O="$PWD/build/guest" CC=clang LOCALVERSION= M="$PWD/tests" modules
```

The guest kernel image is `build/guest/arch/x86/boot/bzImage`. The test module is `tests/guest_allocator.ko`. Build logs are under `results/guest-*.log`. Runtime results are produced by the top-level VM harness, not inferred from successful compilation.

## 6.18 integration

* Linux 6.18 moved `memblock_free_all()` from x86 `mem_init()` into `mm_core_init()`. All populated zones receive LLFree metadata immediately before that call. Metadata uses memblock reservations; release of ordinary RAM then fills LLFree.
* The 6.18 folio preparation, refcount, split, swap and mTHP code stays in place. `rmqueue()` uses `llfree_get()` and `__free_one_page()` uses `llfree_put()`.
* `pcp_allowed_order()` returns false for LLFree. Thus both the new `free_frozen_pages()` and batched `free_unref_folios()` paths run the normal 6.18 page preparation but return directly to LLFree. Bulk allocation calls the normal single-page allocation entry for every empty array slot.
* LLFree frame offsets consistently use `ALIGN_DOWN(zone_start_pfn, 1UL << 10)` in the wrapper, allocation, free, and matching QEMU code. The configured Linux maximum page allocation order must equal LLFree's order 10 (4 MiB).
* Standard high-order watermarks retain their total-free-page threshold but defer contiguous availability to LLFree. PCP drains release LLFree reservations. Buddy-specific highatomic pageblock reservation is bypassed.
* `/proc/llfree` and `/proc/llfree_frag` expose allocator metadata. `/proc/buddyinfo` uses the artifact's aggregate free-small/free-2MiB representation; it is not a complete buddy-order histogram. Fragmentation estimates retain the artifact's approximate semantics.

## Versioned install completion

The matching virtio device must negotiate feature bit 8, `LL_BALLOON_F_INSTALL_RESULT`. Older QEMU devices are rejected. The original bit 7 name is retained, unused.

Each request has one 20-byte OUT descriptor: native u32 node, native u32 zone, native u64 relative frame, followed by little-endian u32 allocation order. A 4-byte IN descriptor carries little-endian status (zero or a positive errno). The matching QEMU device installs every reclaimed 2 MiB child touched by the allocation and returns used length 4. A successful ACK precedes exposure of the allocated pages to the caller; failure returns the allocation to LLFree and yields allocation failure. This covers order-10 allocations across two children, including the case where only the second child is reclaimed.

The guest sends with IRQs and preemption disabled, using `GFP_ATOMIC`, then busy-waits for completion as in the artifact. The zone-type map has `__MAX_NR_ZONES` entries, fixing the old 3-element Linux-zone-index overflow. Queue discovery uses the Linux 6.18 `struct virtqueue_info` API. Queue-discovery temporary allocations are freed.

`/proc/llfree_protocol` reports the negotiated ABI and successful/failed install counts, including successful order-10 requests. The ready marker is `HyperAlloc install-result-v1 ready, N CPUs`.

## Supported scope and boundaries

The target is fixed RAM, fixed contiguous vCPU IDs, one NUMA node, x86-64, 4 KiB base pages, and orders 0 through 10. Native mTHP fault allocation, folio split, COW/unmap and free remain available. Optional host-requested cache pressure uses normal reclaim and may reclaim anonymous memory when swap is available.

CMA, memory hotplug/hotremove, deferred struct-page initialization and device memory are explicitly excluded by Kconfig: the artifact does not provide their required page-isolation ownership protocol. Driver probe rejects multiple online NUMA nodes. Dynamic device removal, suspend/migration, sparse CPU IDs and CPU hotplug are outside the tested contract.

Generic buddy compaction cannot enumerate LLFree free pages through buddy free lists. This port does not claim to add compaction under fragmentation; mTHP and PMD collapse can allocate contiguous LLFree blocks that already exist. Chameleon's new page manager will need its own explicit integration. The newer best-effort `alloc_pages_nolock()` API returns NULL because its required PCP path is unavailable; ordinary atomic allocations still use LLFree. This respects the API's failure contract but does not implement its optimization.

No Chameleon mechanisms or Hermit paths are introduced by this patch.

## Fault-injection regression discovered during integration

The first installation-failure VM run reached a real guest rollback bug: allocation-only `movable` was mistakenly forwarded to `llfree_put()`, which explicitly rejects that flag. The failure is preserved in `results/vm-failure-initial/serial.log`. The rollback now constructs `llflags(order)` independently, matching the ordinary free path. The top-level failure-injection harness repeats this path against the corrected image; consult its final report rather than treating compilation as validation.

The kernel test module additionally accepts `batch ORDER BLOCKS`, for example `batch 10 384`. It holds 384 separate 4 MiB blocks, fills every word, verifies all blocks while still held, then frees them. This forces fresh trees and can exercise order-10 install after host reclaim/return, instead of repeatedly reusing one installed block. Any error frees every block acquired so far.

The repeatable build entry is `scripts/build-guest.sh`; it rebuilds the kernel, regular modules and test module from `configs/guest.config` with one compiler. It supports `CC` and `JOBS` environment overrides.

The initial port is committed as `930596a`; the accounting/drain correction is `6cff96b` in the Linux repository; `patches/linux-6.18-hyperalloc.patch` applies to the `upstream-v6.18` base. Automatic Git version suffixes are disabled in the recorded guest configuration, and the build explicitly passes `LOCALVERSION=` so kernel/module release stays `6.18.0-hyperalloc` across source commits.

The final read-only correctness review and PEBS/page-table-walk capability preparation are recorded in `tests/linux-port-review.md`.
