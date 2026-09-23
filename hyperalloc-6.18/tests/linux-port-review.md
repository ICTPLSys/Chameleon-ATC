# Linux port review and PMU preparation

Review scope: allocator ingress/egress, hard-reclaim accounting, virtio installation completion, supported topology/configuration, and PMU exposure. This is a source review in addition to the VM reports; it does not broaden runtime coverage beyond those reports.

## Fixed during validation

1. Installation failure originally returned the allocation with its allocation-only `movable` flag still set. The real failure-injection VM hit LLFree's assertion. The corrected rollback uses `llflags(order)`, releases the exact allocated range, does not decrement guest free-page accounting, and never exposes the page to `prep_new_page()` or the caller. See the preserved initial failure log and the final failure-mode report.
2. QEMU hard reclaim/return updates shared `zone->vm_stat[NR_FREE_PAGES]`, but the artifact does not share or update Linux's aggregate `vm_zone_stat` counter. Commit `6cff96b` makes `global_zone_page_state(NR_FREE_PAGES)` sum atomic per-zone counters only for `CONFIG_LLFREE`; every other statistic keeps its upstream path. Standard per-CPU vmstat batching remains. Zone watermarks already read the changed zone counter. `MemTotal` remains the configured address-space capacity, while free/available memory reflects hard ownership changes.
3. The upstream `drain_all_pages()` optimization skips CPUs with zero PCP count. LLFree intentionally never uses PCP, so that optimization skipped its reservation drain. The same commit forces CPU selection when LLFree is enabled, allowing the adapted drain routine to release reservations.

## Remaining scope boundaries

* Watermarks still enforce the upstream free-page reserve threshold. Contiguous-block availability is checked by LLFree allocation, instead of nonexistent buddy lists. LLFree allocation failure follows the ordinary 6.18 allocator slow path. Generic buddy compaction does not gain an LLFree free-page isolation implementation; this remains an explicit limitation under fragmentation.
* Install OUT/IN enqueue uses `GFP_ATOMIC`, checks the result, and handles used length/status/broken queues. Successful pages are accounted only after host completion. Info and optional cache-pressure notification queues retain artifact-style synchronous registration/notification; this is not a fault-tolerant device-reconnect protocol.
* The recorded configuration excludes CMA, device memory and memory hotplug/hotremove. Driver probe rejects multiple online NUMA nodes. Fixed RAM and fixed contiguous boot CPU IDs are the validated contract. Dynamic removal/suspend/migration and CPU hotplug have not been made safe merely by suppressing userspace unbind.
* Best-effort `alloc_pages_nolock()` cannot use PCP and returns allocation failure; normal `GFP_ATOMIC` allocations still use LLFree. A caller may not treat this best-effort API as guaranteed allocation.

## PEBS and page-table-walk capability probe

`tests/pmu_probe.c` builds as a static binary:

```sh
gcc -O2 -Wall -Wextra -static tests/pmu_probe.c -o tests/pmu_probe
```

The probe prints CPUID architectural PMU fields and these read-only files:

```text
/proc/sys/kernel/perf_event_paranoid
/sys/bus/event_source/devices/cpu/caps/pmu_name
/sys/bus/event_source/devices/cpu/caps/max_precise
/sys/bus/event_source/devices/cpu/events/mem-loads
/sys/bus/event_source/devices/cpu/events/mem-stores
```

It then requests precise-IP level 2 memory-load sampling and checks that returned sample addresses fall within its own 64 MiB mapping. Physical-address reporting requires additional privilege; if denied, the probe retries virtual-address-only sampling and labels that fallback. Event presence or `max_precise` alone is not treated as successful sampling.

For the current Intel family 6 models 106/108, the probe measures the paper's `DTLB_LOAD_MISSES.WALK_PENDING` and `DTLB_LOAD_MISSES.WALK_COMPLETED` simultaneously. Encodings 0x1008 and 0x0e08 come from `linux/tools/perf/pmu-events/arch/x86/icelakex/virtual-memory.json`. Other models are explicitly skipped until their event definitions are checked. The measured ratio is a capability exercise, not a calibrated Chameleon parameter. Here PTW means page-table walk, not Intel Processor Trace/PTWRITE.

The physical-host run in `results/pmu-physical-host.log` observed precise memory addresses and nonzero pagewalk counters. The default `-cpu host` guest run in `results/vm-manual/pmu_probe.log` reported `max_precise=0`, PEBS `ENXIO`, and working pagewalk counters. This is a platform prerequisite for phase 2, independent of the allocator tests.

A bounded follow-up VM has now tested `-cpu host,migratable=off,pmu=on` using the final QEMU, guest kernel and initramfs. The script is `scripts/probe-guest-pmu.py`; exact command, serial output and result are in `results/vm-pmu/`. The VM was shut down through QMP after the probe. It still reported no usable PEBS memory-address samples (`ENXIO`), while pagewalk counters worked and kernel diagnostics stayed clean. This option therefore does **not** resolve the phase-2 prerequisite on the current setup.

The source explains why this flag was worth testing: QEMU's migratable filter excludes unnamed bits in `FEAT_PERF_CAPABILITIES`, and `kvm_msr_entry_add_perf()` intersects that guest mask with KVM capabilities before writing `IA32_PERF_CAPABILITIES`. The follow-up guest boot did expose `PEBS fmt4+`, but that feature announcement did not establish working memory sampling. PDCM was already enabled in both configurations.

Further work should inspect KVM PEBS support and `IA32_PERF_CAPABILITIES`/event-specific MSR exposure, then require actual address samples before integrating the tracker. The `mem-loads` event has an extra latency MSR, and `x86_pmu_extra_regs()` can return `ENXIO` when an event-specific MSR is unavailable; this is an investigation lead, not a demonstrated complete root cause. Upstream Linux 6.18 also deliberately masks adaptive-PEBS baseline support. This port does not override that restriction. No accepted source or default guest configuration was changed for this probe, and no broader PMU investigation was performed.
