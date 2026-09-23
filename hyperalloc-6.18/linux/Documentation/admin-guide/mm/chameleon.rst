=================================
Chameleon guest memory management
=================================

``CONFIG_CHAMELEON`` builds the tracker into the x86-64 HyperAlloc guest.
The Ice Lake fixed-MEMINFO KVM extension described in
``Documentation/virt/kvm/x86/hyperalloc-pebs-meminfo.rst`` is required to
enable hardware sampling. Tracking starts disabled. This chapter describes
C1 counters and lifecycle hooks, followed by the C2 anonymous collapse
executor. Capacity control and the Hermit data path are outside its scope.

Storage and sample clock
========================

An early memblock allocation reserves one physically contiguous array of
16-bit counters indexed by guest PFN. Its size is ``2 * max_pfn`` bytes,
including PFN holes. A separate one-bit-per-PFN coverage bitmap records
pages that enter the guest managed allocator, excluding holes and permanently
reserved pages. Memory hotplug is outside the HyperAlloc configuration.

Each accepted sample saturating-increments one 4 KiB counter. The 16-bin
histogram includes zero-valued managed pages: bin 0 contains 0 and 1, and
bin i contains values from ``2**i`` through ``2**(i+1)-1`` for i=1..15.
Histogram snapshots retry across concurrent updates, including page frees.

For byte capacities Mlocal and Mtotal, the PEBS sample period is
``max(512, floor(4096 * Mlocal / Mtotal))``. After ``Mlocal / 4096`` decoded
samples, all counters are halved. Dropped hardware samples still advance
this clock; synthetic test samples advance it separately from hardware
sample statistics. There is no elapsed-time cooling trigger.

The hot threshold phi selects bins from highest to lowest until they cover
``ceil(0.30 * Mlocal / 4096)`` pages, then uses that bin's upper bound.
It is at least 1. When too few pages have nonzero heat, the target hot
population cannot be reached; phi remains 1.

Hardware events and attribution
===============================

Every online CPU receives pinned user-mode raw events for
``MEM_LOAD_RETIRED.L3_MISS`` (0x20d1, precise_ip=2), ``DTLB_LOAD_MISSES.WALK_PENDING``
(0x1008), and ``DTLB_LOAD_MISSES.WALK_COMPLETED`` (0x0e08). CPU hotplug is
disabled while tracking is enabled, then restored when its events are
released. An unavailable counter causes enable to fail.

The two PTW events are enabled, disabled, and read in paired local CPU
callbacks with interrupts disabled. Both exclude kernel execution, so
user code cannot execute between the two operations within a callback.
The reported running/enabled times and read-error fields make unavailable
or multiplexed measurements visible. PTW totals accumulate across repeated
enable/disable cycles until reset. Delta fields describe the most recent
snapshot interval; ``ptw_cycles`` is integer pending-delta/completed-delta,
or zero if that interval completed no walks.

The precise callback uses fast-only GUP to hold a reference to the sampled
virtual address's current page, then enqueues it on a per-CPU bounded ring.
A worker updates its counter before releasing the reference. A full ring
or failed GUP increments explicit drop statistics. No tracker lock is taken
from NMI context. The native LARGE_PEBS task-switch drain is retained with
TID samples. A same-address remap before the callback can change which PFN
the address resolves to: heat reflects callback-time translation, not a
guarantee of the historical physical address at the hardware event.
GUP also sets Linux's referenced state on the folio.

The tracker explicitly opts into a kernel-only per-record PEBS callback.
This delivers all records in a bulk drain, including task-switch drains;
the normal generic overflow handler still runs only for the final PMI
record. Other perf consumers retain their existing bulk-output behavior.

The allocator free-preparation hook clears all base-page counters before
pages become visible to PCP or LLFree. The bootstrap allocator hook also
discovers managed coverage. A page retained by an LRU batch or queued sample
is not yet free; its heat remains until its final reference is released.
Native folio splitting preserves counters because PFNs do not change.

Debugfs interface
=================

All files are under ``/sys/kernel/debug/chameleon/`` and are root-only.
``control`` accepts one command per write::

  enable
  disable
  drain
  reset
  capacity <local_bytes> <total_bytes>

``disable`` stops events, drains queued samples, and releases PMU resources.
``drain`` temporarily stops sampling, drains global LRU batches, processes
the sample queues, drains LRU batches again, and resumes sampling if it was
enabled. The global LRU drain is explicit administrative work, not part of
the normal sample worker. ``reset`` requires disabled sampling and clears
heat and statistics while retaining managed coverage. Capacity values are
decimal bytes with ``4096 <= local <= total <= 2**52``.

``stats`` exposes key/value lines, including metadata physical address and
size, managed coverage, bin counts, phi, sample period, cooling interval
and epochs, accepted/dropped/hardware/synthetic sample counts, GUP/ring
failures, free clears, and PTW values and times. ``worker_ns`` measures
worker elapsed wall time, including scheduling or mutex delay;
``worker_cpu_ns`` measures its scheduled task runtime. These are not full
tracker CPU cost: the NMI callback and allocator hook are not included.

Open ``range`` read/write, write ``<first_pfn> <nr_pages>`` in decimal on
that file descriptor, then read from offset zero. It returns one
``<pfn> <counter>`` line per base page, with a maximum of 4096 pages per
query. Callers requiring allocation identity must retain their own page
ownership; this query snapshots counters but does not pin arbitrary PFNs.

With ``CONFIG_CHAMELEON_TEST``, ``inject`` accepts ``<hex_user_va> <count>``
while hardware sampling is disabled. The VA belongs to the writing task;
the count must be in 1..10000000. The selected page is held through all
updates. Injected samples are always reported as synthetic and never as
hardware PEBS evidence.

C2 anonymous collapse execution
===============================

``chameleon_collapse_anon(mm, address, order)`` is an internal sleeping
interface. The caller holds an ``mm_users`` reference and no mmap, folio,
page-table or LRU lock. Orders 2 through 9 are supported. Order 2 replaces
four complete order-0 folios; each higher order replaces two complete
folios of the preceding order. The address must be naturally aligned.
The manager is responsible for the hot/uniform selection policy; this
helper enforces memory-management eligibility and performs the conversion.

The first implementation requires the entire containing 2 MiB PMD to lie
inside one private anonymous VMA, even for a smaller target. All source
pages must be present, exclusive, fully mapped, on the LRU, and free of
external references. KSM, swapcache, UFFD, mlock, special mappings, shadow
stacks, DMA/GUP pins, holes, zero pages, and source folios crossing the
target boundary are rejected. A transient PEBS/GUP or LRU-batch reference
can make the operation return busy and is a reason to retry later.
Read-only private anonymous VMAs are supported without adding write access.

The destination is allocated and charged before taking the mmap write
lock. The helper revalidates the VMA and source pages, then temporarily
unlinks the containing PMD and performs the native fast-GUP synchronization.
It holds the anon_vma write lock until that PMD is restored or replaced;
this also protects the neighbouring pages that were not isolated.
Each source folio is locked and isolated exactly once.

The scan's ``pte_offset_map_lock()`` mapping is released before copying or
taking the tracker mutex, including its RCU read-side protection. After
copying, ``chameleon_transfer_counters()`` moves every 4 KiB counter to its
new PFN and clears its old value. The worker/cooling mutex and an outer
snapshot generation guard prevent readers from observing an intermediate
duplicated heat distribution. The transfer changes neither sample counts
nor cooling time.

Once copying and counter transfer have succeeded, the remaining commit
operations cannot fail. Sources lose their PTE rmap and mapping references;
the new folio gains native anonymous rmap, LRU, and mapping references.
For orders 2 through 8, the original PTE table is reused and each target
PTE retains its protection, accessed, dirty, and soft-dirty bits. Neighbour
PTEs are unchanged. The x86-64 permanent direct-map accessor is used for
this final table update; ownership of the detached table and the mmap /
anon_vma locks keep it alive without retaining a sleeping RCU section.
Order 9 requires uniform base protection and uses the native huge-PMD
installation and page-table deposit primitives. Its accessed, dirty, and
soft-dirty state is the union of the source PTE state.

On any failure before commit, the original PMD and every source folio are
restored before dropping locks; the unused destination and its charge are
released. The original source data and counters remain unchanged. With
``CONFIG_CHAMELEON_TEST``, the manager can arm a one-shot failure: phase 1
returns ``-ENOMEM`` before destination allocation, and phase 2 copies the
first 4 KiB into the private destination then returns ``-EIO`` through the
full detached-table/isolation rollback path.

Successful replacement preserves ``MM_ANONPAGES`` and uses native rmap
accounting for mapped pages and folio sizes. This guest configuration has
``CONFIG_MEMCG=n``; charge/uncharge helpers are present, but runtime memcg
validation is separate from the initial guest acceptance tests.
