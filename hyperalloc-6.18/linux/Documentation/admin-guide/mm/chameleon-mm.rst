====================================
Chameleon mixed-order manager (C2)
====================================

The manager builds on the C1 PFN counters and the Linux 6.18 anonymous folio
implementation. It supports orders 0 and 2 through 9 (4 KiB, 16 KiB through
2 MiB). Anonymous order 1 is not representable in this kernel: a logical
HHH order-1 segment becomes two real order-0 folios with the same role.
This is an explicit difference from the paper's complete order-0..9 model.

Execution and scope
===================

``mm/chameleon_mm.c`` owns the manager. ``enable`` schedules an unbound
workqueue every five seconds. Each epoch visits every supported order,
rotating the first order, and examines at most 128 eligible active folios
per order and NUMA node. Promote/Age also examines inactive folios.
A debugfs target filters the scan to one mm/address range; foreign folios
are traversed but do not consume its 128-folio processing quota. Without
that observation scope, traversal itself is bounded to 128 per queue.
The target holds only ``mm_count`` to preserve mm identity. It does not
keep mappings alive after process exit. Each actual operation separately
uses ``mmget_not_zero()`` before accessing the address space; selected
candidates retain the documented ``mm_users`` reference until consumed.
Replacing or clearing a target releases its ``mm_count`` reference.
The manager starts disabled. ``disable`` waits for in-flight maintenance
without holding the manager mutex needed by that work.

Only traditional root LRUs are supported. MEMCG and MGLRU configurations
are rejected; this implementation does not maintain a per-order MGLRU.
Private anonymous, resident, fully mapped folios are considered. Shared
or COW-shared pages, KSM, swapcache, mlock, UFFD, DMA pins, and special
mappings are rejected. Temporary sampling/GUP references can cause a
native split or collapse to return busy; the next epoch may retry.

HHH uses ``10 * max(left, right) > 7 * total`` and descends only into the
dominant half. A folio must first satisfy ``total >= nr_pages * phi``.
Zero, uniform, and exactly-delta-boundary inputs remain intact. The
planner invokes native ``folio_split()`` with the dominant page and final
order; it does not substitute metadata labels for physical folios.
The caller's folio reference/lock remains on the original head. Other
children are reacquired through their actual mappings before use.
Dominant segments enter their active queue head; siblings enter the tail.
PFNs and individual 4 KiB heat counters do not change on a split.

Automatic collapse requires hot previous-order sources and an aggregate
HHH plan with one segment. Order 2 consumes four order-0 sources; larger
orders consume two sources of the previous order. The real executor is
``chameleon_collapse_anon()`` in ``mm/khugepaged.c``. Its mapping, permission,
copy, heat-transfer, and rollback contract is documented in
``chameleon.rst``. Explicit test collapse bypasses the heat-policy check
but uses this same executor and all its ownership checks.

Queues and cost model
=====================

Each online root lruvec has active/inactive lists indexed by supported
order. PFN sidecars have their own list links, do not hold references,
and use the native lruvec lock. Hooks cover native add/delete/requeue,
vmscan isolation and skipped-page splice, and actual split/head-order
changes. A split-only slow path preserves the native list's per-order
subsequence; normal hooks are constant time. Sidecar storage is allocated
through boot-time ``max_pfn``; later PFNs beyond that extent are untracked.
The allocation size is exposed as ``index_bytes``.

Promote/Age reads C1 snapshots outside LRU/PTL spinlocks and moves actual
folios between Linux active/inactive lists. Selection considers cold
queues in increasing score, then their LRU tail::

  score[o] = (Creclaim[o] * K + Csync + Etrans * Nactive * Lptw)
             / (K * 2^o)

Scores use a 1024 fixed-point scale with checked multiplication/addition.
Unavailable orders, missing calibration, and overflowing scores are not
silently treated as zero cost. Reclaim and synchronization costs and TLB
parameters are explicit inputs; no Hermit transport cost is claimed to
have been measured. The debugfs ``batch`` command labels these as explicit
parameters. ``ptw`` uses the difference between paired C1 pending/completed
snapshots, requires a valid event window, and establishes a new baseline
after C1 counters reset. Its first observation or a zero/invalid window
returns ``ENODATA`` and restores the explicit ``batch`` Lptw parameter.
``ptw_measured`` identifies the current source of Lptw; ``Lptw_explicit``
retains the fallback input.

Candidate ownership
===================

``chameleon_select_batch(maximum, batch)`` appends up to ``maximum``
(maximum 128) descriptors to a caller-initialized list. A descriptor owns
one mm_users reference and exactly one native LRU isolation reference,
with its base-page count charged to ``NR_ISOLATED_ANON``.
Its mm, naturally aligned VA, PFN, order, and unique token describe the
mapping verified during selection. No MM, folio, rmap, or LRU/PTL lock is
retained at API return. Debugfs PID identifies the explicitly targeted
process; without that scope, PID is zero and mm is the authoritative
owner reference.

A consumer must revalidate the current mapping under appropriate locks
before committing any unmap or transfer: references prevent physical
reuse, but do not prevent userspace unmap, mprotect, or exit.
``chameleon_candidate_putback()`` consumes the descriptor, removes its
batch link, discharges ``NR_ISOLATED_ANON``, calls ``folio_putback_lru()``,
and drops the mm reference. A successful C3 transfer must explicitly
discharge or transfer that isolation accounting exactly once.
Callers must not separately delete the link, drop the isolation reference,
or use the descriptor afterwards. C3/C4 transfer/shadow behavior is not
implemented by this module.

Debugfs interface
=================

The root-only directory ``/sys/kernel/debug/chameleon_mm`` provides
``control`` (0600), ``stats``, ``last``, and ``candidates`` (0400).
Numbers accept decimal or a 0x-prefixed hexadecimal value. Commands::

  target PID ADDRESS BYTES
  clear_target
  enable
  disable
  epoch
  age
  cost ORDER CYCLES
  batch K CSYNC ETRANS NACTIVE LPTW
  ptw
  select MAXIMUM
  putback

Target ranges are page-aligned and cannot be changed while debugfs owns
candidates. ``select`` actually isolates Linux folios; ``putback`` releases
all descriptors and drains LRU batches so restored membership is visible
on return. Held debugfs candidates require an explicit ``putback`` even
if their process exits. ``epoch`` runs one maintenance round immediately;
normal runtime conversions use the asynchronous workqueue.

The following deterministic commands require CONFIG_CHAMELEON_TEST::

  split PID ADDRESS
  collapse PID ADDRESS ORDER
  fail_collapse 0|1|2

Manual split/collapse return their executor error as write errno. A
balanced/cold split returns success with ``split_changed 0``. Failure
injection is consumed once by the collapse executor: 1 is preallocation
ENOMEM; 2 is EIO after copying at least one 4 KiB page, using full real
mapping/isolation rollback; 0 clears a pending injection. C1 ``inject``
can seed heat while its hardware tracker is disabled.

``last`` reports operation, errno, real ``split_changed``, original PFN,
and physical segments as ``segment offset=N order=O dominant=B``. Segment
order need not be address order; the partition covers the source exactly.
``candidates`` emits one line per held descriptor in selection order::

  candidate token=T pid=P address=0xA pfn=F order=O

``stats`` reports conversion, promotion, aging, selection and putback
counts; explicit model values and source; index bytes; and per-order
``active``, ``inactive``, ``score`` and ``reclaim`` values. Queue counts
are global root-LRU counts, not the optional target's private accounting.
