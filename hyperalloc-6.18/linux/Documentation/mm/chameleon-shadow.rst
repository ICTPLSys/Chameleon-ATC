Chameleon Shadow mappings and saved-data recovery
=================================================

This document first describes resident Shadow ownership, then the Hermit
saved-data backend and the separate, explicitly authorized C4 discard tests.
The current implementation restores ordinary application contents after
actual Host discard when a full save/load backend is registered.

Scope and accounting
--------------------

``CONFIG_CHAMELEON`` provides a private PFN swap-entry type and
``mm/chameleon_shadow.c``. This layer removes CPU-present access while
retaining the original anonymous folio, data, rmap, mapcount, mapping
references and ``MM_ANONPAGES``. It does not create real swap entries or
charge ``MM_SWAPENTS``. This resident phase does not release host backing;
the later finalized phase is described below.

The C2 selector supplies anonymous exclusive folios of orders 0 and 2..9,
with an isolation reference, a temporary ``mm_users`` reference and an
``NR_ISOLATED_ANON`` charge. On publication C3 ends that charge exactly once,
keeps the isolation reference, takes ``mm_count`` with ``mmgrab()``, and drops
C2's ``mm_users`` after releasing mmap/folio locks. An exited process can
therefore reach ``exit_mmap()`` even if it still has Shadow mappings.

The private pool is limited to ``totalram_pages() / 200`` base pages. Its
``shadow_pages`` statistic counts the complete retained folio until successful
saved-data restoration or final cleanup, including pages removed by a partial
zap. ``live_slots`` counts only
remaining Shadow PTEs. These quantities differ after partial unmapping.

Page-table metadata
-------------------

Each participating PTE table has an optional independently allocated page
containing exactly 512 u64 slots, referenced by ``ptdesc->pt_shadow``. Zero
means no Shadow, U64_MAX means pending save, and another value encodes the
saved byte offset plus one. The metadata page's ``struct page`` holds the
occupied-slot count and its RCU callback; metadata is not stored inside the
PTE page's normal entries. Clearing the final occupied slot detaches the
pointer under PTL and frees the metadata after an RCU grace period.

A Shadow object contains saved PTE permissions, a live-slot bitmap, the
original PFN, mm, virtual address and a monotonically increasing token.
Naturally aligned folios up to order 9 fit in one PTE table. Registry and
operation references keep the object and isolation reference alive. Async
completion identifies the object by token, never by a possibly reused PFN.

The generic reverse-map walker deliberately does not consume Shadow entries.
A folio remains isolated from normal LRU reclaim while it has any live slots.
The isolation reference also prevents native split paths from assuming the
folio has only ordinary mapping references. Shadow is nevertheless classified
as a PFN special entry by the PFN decoder and page-table checker.

Preparation and fast GUP
------------------------

``prepare K`` calls the real C2 selector and groups candidates by mm. It holds
one mm's mmap write lock at a time and starts write protection of each VMA.
An order-9 PMD mapping is first demoted by native ``split_huge_pmd_address``;
the real folio remains order 9. Native PMD demotion has an additional TLB
invalidation, recorded as ``prepare_pmd_demotions``.

Preparation rejects shared mappings, extra references, DMA pins, armed UFFD,
unsupported VMAs and mismatched PTEs. After all modified PTEs in a group become
Shadow entries it issues one actual ``flush_tlb_mm()`` for that mm. It then
calls ``tlb_remove_table_sync_one()`` to wait for existing fast-GUP readers,
and rechecks every locked folio against its retained mapping references plus
one isolation reference. Unexpected references cause that folio's original
mapping to be restored. The mmap write lock and folio locks remain held
through this recheck. Backend submission rejects objects still in the
preparing state.

``mm_groups`` and ``guest_flushes`` count modified mm groups. A batch spanning
two mm instances therefore performs two guest TLB flushes. ``fast_gup_syncs``
counts the additional synchronous CPU synchronization separately; it is not a
TLB flush. PMD demotions are also outside that batch-flush count. These extra
operations must be included when measuring preparation cost.

Selection is bounded and best effort: a batch succeeds if at least one
candidate survives revalidation. Ineligible candidates are returned through
C2's normal putback path. If none are eligible, selection returns ENOENT;
other all-failed batches return the last preparation error.

Restore, zap and cleanup
------------------------

A fault initially takes PTL only long enough to verify the private entry and
pin the object. It ends the PTE mapping's RCU read section before waiting for
the folio lock. VMA-lock faults first retry through the mmap-lock path.
``folio_lock_or_retry()`` retains its native retry and mmap-lock semantics.

With mmap read and folio locks, restoration processes all surviving slots in
that object under PTL. It verifies each entry, consults its current VMA and
soft-dirty/UFFD bits, and rebuilds permissions with
``can_change_pte_writable()``. It does not add rmap or mapping references:
those were never removed. Holes produced by partial zap are not recreated.
Concurrent faults count as one actual object restoration.

Zap removes one slot under PTL; the normal MM caller then removes the rmap,
mapping reference and RSS charge. Fork, mprotect, mremap and clear_refs restore
affected objects before native page-table transformations. Smaps treats Shadow
as resident anonymous memory. The separate lifecycle hooks document these
integration points in ``progress/chameleon-c3-lifecycle.md``.

After the final slot and operation reference disappear, a workqueue cancels
any backend operation, then puts a still-mapped folio onto the active LRU or
drops the final isolation reference for an unmapped folio. It then releases
the mm structure reference and RCU-retires the object. Neither zap nor fault
waits for the global preparation mutex. PTL never spans a sleeping cleanup.

Backend boundary
----------------

The separate Hermit ``rswap-client.ko`` implements the operations in
``include/linux/chameleon_shadow.h``. It supports a local DRAM debug pool
and actual RDMA transfers to a separate memory server. The old C3 local-copy
test backend has no load operation and retains its resident-only contract.

A registered backend keeps its operations table alive until unregister
succeeds. A save/load backend sets ``owner = THIS_MODULE``; each accepted
object pins that module until cancellation completes. Unregister returns
EBUSY while objects still own backend submissions or saved data.

``submit(token, folio)`` returns zero if accepted; a failure means it has left
no asynchronous accesses behind. The callback runs without the object's
backend mutex or MM locks and produces one sleepable
``chameleon_shadow_save_complete(token, status, offset)``. Before successful
completion, every source-byte access must have finished and any extra source
folio reference must have been dropped. The core's isolation reference remains
held. The core can then finalize and release Host backing immediately.

``cancel(token)`` synchronously drains source and destination I/O and releases
that token's remote/debug slots. Cleanup calls it without mmap, folio, PTL or
registry locks. Completion does not acquire the object's backend mutex;
retired tokens return ESTALE and cannot affect a new owner of a reused PFN.
Failed saves may be resubmitted after cancellation has drained the old job.

A successful completion records offset metadata. Only a backend with ``load``
and negotiated feature 11 ``LL_BALLOON_F_CHAMELEON_DATA`` queues automatic
REGISTER/READY with ``LL_CH_RANGE_DATA_SAVED = 2``. The worker revalidates the
whole live object and its successful save before authorizing finalization.
A save-only C3 test backend still receives EOPNOTSUPP for ``commit TOKEN``;
without any backend, ``submit TOKEN`` returns EOPNOTSUPP and preserves data.
Manual commit is available for a still-saved object, but normal Hermit saves
already queue it automatically.

``load(token, offset, destination)`` verifies identity and fills the complete
folio before returning zero. It runs with mmap, object backend and folio locks,
without PTL. It must not recursively lock the folio or call back into MM.
The core first completes Host INSTALL before accessing retired backing, then
loads every byte and verifies all live token PTEs under PTL. Only after a
successful full load does it add native rmap/mapping refs/RSS and publish the
original PFNs with current VMA permissions. Partial-unmap holes stay absent.

An INSTALL or load error retains the token, saved data and Guest reservation.
A failed demand load returns SIGBUS without publishing any target PTE; explicit
restore or a subsequent fault can retry. A delayed COMMIT_RESULT cannot undo
a previous successful Host INSTALL. Test-only FORGET rejects a saved-data
object until restoration completes so a retryable Host identity is retained.

Fork, mprotect, mremap, mlock and clear_refs use the existing pre-restore gate
for real saved-data objects before native MM transformations. After exit or
the last zap, no application data need be reloaded: cleanup cancels I/O,
installs Host backing, zeros the full reservation and only then frees it.
Failed installation retains ownership and schedules another cleanup attempt.

Debugfs
-------

``/sys/kernel/debug/chameleon_shadow/control`` accepts::

  prepare K
  restore TOKEN
  cancel TOKEN
  submit TOKEN
  commit TOKEN
  drain

``restore`` and ``cancel`` restore surviving mappings, using original resident
bytes for Shadow and complete backend loads for saved-data Reclaimed objects. Unknown or retired
tokens return ESTALE. ``drain`` waits for queued object cleanup and metadata
RCU callbacks; it does not force live objects to be restored.

``entries`` has one line per currently registered object::

  entry token=N pid=N address=0xHEX pfn=N order=N live_slots=N table_pfn=N slot=N

``pid`` is an observation hint; untargeted C2 ownership discovery can leave it
zero. The actual owner is the held mm, not a later PID lookup. ``table_pfn`` is
the physical PTE-table page number and ``slot`` the starting PTE index.

``stats`` reports pool/object/slot/metadata occupancy, preparation and restore
counts, zapped slots, group/flush/demotion/GUP-synchronization counts, rejected
pin races, backend presence and pool limit. Occupancy can temporarily include
queued cleanup; use ``drain`` for quiescent leak checks. Hardware and CPU cost
measurements must not infer one invalidation from one debugfs command.

``offsets`` provides a diagnostic snapshot of each live metadata slot::

  slot token=N address=0xHEX value=N

``value`` is the raw encoding: U64_MAX is pending and 1 represents saved byte
offset zero. Each slot is sampled under PTL; values from different objects need
not share one sampling instant. The temporary copy buffer is heap allocated,
and no metadata pointer or kernel virtual address escapes to userspace.

Submission first atomically claims a resident object. A success or failure
completion must win the single ``SAVING`` transition; subsequent completion
attempts return EALREADY and cannot rewrite metadata. Success changes metadata
under the same PTL used by restoration and zap. Tokens already retired return
ESTALE. These checks also apply to the separate local-copy test backend; that
module validates the save interface but does not implement Hermit or host
reclamation.

C4 discard-only host handoff
---------------------------

The C4 control path is available only when the LLFree virtio device negotiates
``LL_BALLOON_F_CHAMELEON_RANGE`` (feature 9). In the save-only C3 test mode,
``commit TOKEN`` returns EOPNOTSUPP. Neither save-only local-copy completion
nor C2 selection alone authorizes discarding application data. ``CONFIG_CHAMELEON_TEST``
adds the following explicit commands for dedicated disposable test mappings::

  discard TOKEN
  ready TOKEN
  query TOKEN
  install TOKEN

``discard`` authorizes one existing token and sends REGISTER. ``ready`` makes
it eligible for the host's bounded batch policy. Save-backend submission and
this authorization are mutually exclusive under the object's backend mutex.
A resident object may still win a normal C3 fault or explicit restore before
finalization; a host request for such a retired token is rejected.

The host sends FINALIZE_REQUEST with immutable token/GPA/order/range identity.
The guest revalidates each object under mmap write, folio and PTE locks. Only
an entire exclusive, unpinned, still-Shadow folio can be accepted. Successful
objects change to private Reclaimed entries encoding the object token, not a
PFN. This transition removes their rmap, mapping references and resident RSS,
while retaining the isolation reference as a GPA reservation. Each affected
mm receives one additional real ``flush_tlb_mm()``. Only after all MM locks
are released does the driver send FINALIZE_ACK containing accepted and
rejected per-range results. ``finalize_mm_groups`` and
``finalize_guest_flushes`` count this second guest invalidation separately
from C3 preparation. Host EPT invalidation and backing removal are subsequent
host responsibilities; the guest ACK alone does not establish their success.

COMMIT_RESULT reports each range independently. An uncertain or failed
transaction never authorizes freeing the reservation. ``install`` requires a
successful host INSTALL response with state INSTALLED, then explicitly zeros
every base page before dropping the final folio reference into the allocator.
Even ranges whose original backing survived a partial host failure follow
this INSTALL acknowledgement and zeroing path. The private Reclaimed PTEs
remain until zap and continue to fault with SIGBUS after installation; this
test mode does not restore an application mapping or emulate remote data.

Reclaimed entries are not PFN swap entries, do not count as real swap, and
have no rmap or RSS charge to remove a second time. ``mincore`` reports them
nonresident. Zap removes only token/metadata ownership. If the final slot is
removed before explicit installation, asynchronous cleanup installs and zeros
the reservation before freeing it. Failed installation retains the bounded
reservation and retries after one second. ``drain`` does not force delayed
retry timers or convert failures into success; poll occupancy for such tests.
The mm has only an mm_count reference, so this cleanup also works after exit.

For this discard test interface, fork, mprotect, mremap, mlock and clear_refs
on a range containing live Reclaimed entries return EBUSY through the common
pre-restore gate. Munmap, MADV_DONTNEED and exit can remove entries normally.
This restriction does not apply to resident C3 Shadow mappings. The PFN
registry association is removed at finalization, before any successful
installation can return that PFN to the allocator. Later retirement of its
old token cannot erase a new Shadow owner of the reused PFN.

``states`` adds a per-object diagnostic line without changing ``entries``::

  state token=N kind=shadow|reclaimed phase=N authorized=N registered=N ready=N host_state=N reserved=N batch=N data_saved=N saved_offset=N

``reservation_pages`` counts allocated finalized folios awaiting successful
INSTALL plus data load/publication, or INSTALL and cleanup zeroing. ``host_reclaimed_pages`` counts the subset confirmed
RETIRED by the host, ``reclaimed_slots`` counts remaining token PTEs, and
``shadow_pages`` continues to count all retained physical reservations until
they are safely released. An installed object's token/metadata can therefore
remain live with zero reservation pages. ``range_install_success``,
``range_install_failure``, ``zeroed_pages`` and ``cleanup_retries`` report
reinstallation progress. Saved-data statistics additionally include
``data_save_success/failure``, ``data_ready_objects``, ``data_load_success/failure``,
``data_restored_pages`` and ``data_fault_restores``. Application content checks
and the backend byte counters distinguish data round trips from discard tests.

The driver uses a sleepable control queue and a separate event receive queue.
It validates exact message lengths, session/request/batch identities and
immutable range fields. A control timeout permanently stops further requests
on that transport instance while retaining DMA buffers and uncertain GPA
reservations. Control requests never hold folio/PTL locks. Saved-data INSTALL
may hold mmap: its control-queue callback completes directly without waiting
for the Guest MM event worker. An INSTALL arriving before FINALIZE_ACK returns
EBUSY and is retried. Other MM control requests, including FINALIZE_ACK, are
issued after dropping mmap. The device queues event messages when its single
receive buffer is unavailable, rather than waiting for the Guest worker.
