Chameleon native PSI policy
===========================

``CONFIG_CHAMELEON_POLICY`` builds the C5 policy, disabled at boot. Enabling
requires feature 10, an exclusive QEMU policy lease and native PSI enabled.
The lease suspends competing HyperAlloc automatic/resize control. With a
registered Hermit save/load backend and data feature 11, ordinary cold
candidates are saved and can proceed from Shadow to actual Host reclaim.
Without that backend, they retain the resident Shadow behavior.

Pressure source and timing
--------------------------

``psi_memory_snapshot()`` reads the native per-CPU scheduler PSI counters
under their sequence counters, including currently active memory stall
intervals. It maintains independent cursors and cumulative some/full totals.
It never reads a proc file and never consumes the native AVGS/POLL cursors.
Each CPU is weighted by its nonidle nanoseconds; full-width multiply/divide
avoids overflow when aggregating many CPUs. Per-term rounding is less than
one nanosecond per contributing CPU/window.

This independent nanosecond aggregation is necessary for 1 ms controls:
native AVGS uses jiffy-scaled weights, so repeatedly consuming its buckets
at sub-jiffy intervals would lose pressure and alter proc statistics. The
policy uses the same scheduler accounting source, not the proc total's
sampling schedule or avg10 value. Gaps of at least U32_MAX nanoseconds can
contain multiple native bucket wraps; the helper explicitly rebaselines and
returns EAGAIN. An invalid window closes the reclaim gate and is not low PSI.

An hrtimer defaults to 1000 us and only queues an ordered work item. The
worker uses actual elapsed monotonic time and stall deltas to calculate
parts per million. Work coalesces when an earlier action takes longer than
the configured period; the configured period is not a latency guarantee.
``timer_ticks``, ``coalesced_ticks`` and ``delta_ns`` expose that distinction.
``worker_ns`` is elapsed worker wall time, including waiting, not CPU time.

Actions and ownership
---------------------

Below the threshold, the worker opens the reclaim gate, requests at most one
configured free-capacity batch, and prepares at most one configured bounded
cold-folio batch. Free actions use HyperAlloc's 2 MiB unit and completed-byte
feedback. The minimum local-capacity floor bounds requests. Existing Shadow
reservations are conservatively deducted from the available budget so a
queued asynchronous cold completion cannot later breach that floor. All
cold prepares use this same budget, including ordinary saved-data objects and
a backend registered while policy is running; it is not a discard-test-only
restriction.

At or above the threshold, the worker closes the host gate, actively returns
free capacity and restores its resident Shadow or saved-data Reclaimed objects.
The latter first INSTALL backing, then load and publish actual contents through
the same kernel path used by demand faults. An enabled cold-only
policy still actively returns available hard-reclaimed capacity, in batches
of at least 2 MiB. This is a functional threshold, not a measured slowdown
calibration.

Each enable generation has a unique owner cookie. Target selection takes a
temporary live mm reference, and the configured target itself holds only
mm_count. The page manager selects under its own mutex with the explicit
range, then restores any separate debug target. High-pressure restoration
and disable process only objects tagged with this policy owner; manual C3
objects retain their ownership.

Ordinary candidates automatically submit to a registered save/load backend.
Successful completion queues DATA_SAVED REGISTER/READY only with feature 11;
failed saves and save-only backends cannot authorize Host discard. These
objects are restored with their real data during high pressure or disable.

Separately, ``discard_test=1`` with an explicit PID/VA target and
CONFIG_CHAMELEON_TEST authorizes disposable-test REGISTER/READY. After those objects become
Reclaimed, high pressure requests INSTALL, waits for confirmation, zeros the
source and releases the reservation. Their user addresses still SIGBUS:
installation is not remote-data restoration. Policy-owned terminal objects
are forgotten at cleanup; a host batch still using their record causes a
delayed retry. Manual C4 objects keep their existing diagnostic history.

Disable first stops the timer and synchronously drains policy work. It
closes the gate and restores/installs owned live reservations. If any such
operation fails or remains pending, it retains the lease and owner and
returns an error; another disable retries them. Only then does it release
the lease, which restores newly hard-reclaimed capacity to the acquisition
baseline. Last-slot exit cleanup can independently finish an INSTALL using
the C4 lifetime rules, without holding a process alive.

Capacity feedback
-----------------

Every valid action epoch samples QEMU's explicit capacity accounting and
feeds changed local/total values to the tracker. ``local_bytes`` means
nominal RAM minus hard-reclaimed capacity and confirmed retired ranges. It
is distinct from QEMU RSS: soft-discarded free backing is reported separately
as ``soft_reclaimed_bytes``. Returning HyperAlloc capacity preserves its
normal lazy installation semantics rather than claiming all bytes have
already been physically populated. Uncertain feedback closes reclaim.

Debugfs
-------

``/sys/kernel/debug/chameleon_policy/control`` accepts::

  set epoch_us N
  set threshold_ppm N
  set psi_full 0|1
  set free_pages N
  set cold_folios N
  set minimum_local_bytes N
  set discard_test 0|1
  target PID HEX_ADDRESS BYTES
  clear_target
  enable
  disable

Configuration requires disabled state and no outstanding lease. Setters
allow intermediate incomplete configurations; enable rejects both batches
being zero, or discard authorization without a target. free_pages is zero
or a multiple of 512; cold_folios is 0..128; epoch_us is 1000..1000000;
threshold_ppm is 0..1000000. Defaults are some PSI, a 10000 ppm threshold,
512 free pages, eight cold folios, and half of total RAM as the floor.

``stats`` exposes cumulative epochs, native-source totals/ppm, action counts,
completed free bytes, actual Shadow restore/install pages, feedback capacity,
owner/lease/target and error fields. The ``discard_ready_objects/discard_installed_pages`` fields
retain the explicit-test meaning; asynchronous data READY/load counts are in
``chameleon_shadow/stats`` as ``data_ready_objects``, ``data_load_success/failure``
and ``data_restored_pages``. Policy-triggered real data restoration contributes
to ``shadow_restored_pages``. Counters are not reset by enable. Errors
retain their last diagnostic value after later successful work.

``chameleon_shadow/control`` additionally accepts test-only ``forget TOKEN``
to exercise the wire boundary without changing local object ownership.
Unfinished host records are rejected; a saved-data object also returns EBUSY
after Host INSTALL if its load/publication has not completed. Repeated
successful forgetting returns ESTALE. This interface is not used by normal manual C4 tests.
