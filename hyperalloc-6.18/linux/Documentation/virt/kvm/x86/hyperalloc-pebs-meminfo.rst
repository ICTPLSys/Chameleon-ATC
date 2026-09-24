HyperAlloc fixed MEMINFO PEBS extension
======================================

This is a private research ABI, not an upstream KVM interface.  It preserves
the ordinary perf_event_open() userspace ABI but requires matching host KVM,
VMM, and guest kernel changes.  It is off by default.  Existing virtual
machines retain their existing PEBS behavior.

Host requirements and opt-in
----------------------------

KVM_CHECK_EXTENSION(KVM_CAP_HYPERALLOC_PEBS_MEMINFO), private capability
number 0x48410001, returns 1 only on Intel Ice Lake server/D (family 6,
model 106/108) with enabled PMU and EPT, an initialized host perf PMU newer
than version 4 with PEBS EPT support, CPU PEBS/PDCM features, and native
IA32_PERF_CAPABILITIES reporting format 4 and architectural baseline PEBS.
On these two CPU models, perf enables PEBS isolation for PMU versions newer
than 4; the older-model microcode isolation quirks do not apply.  Otherwise
the query returns 0.  This check uses the existing host perf capability ABI,
so the host-side change can also be built as matching KVM modules.

Before creating any vCPU, userspace enables the VM capability with
KVM_ENABLE_CAP, flags=0, args[0]=1, and args[1..3]=0.  Repeating that request
before vCPU creation is harmless.  Unsupported hosts return EOPNOTSUPP;
late requests return EBUSY.  Invalid arguments, a disabled VM PMU, or an
existing PMU event filter return EINVAL.  Once enabled, installing any
KVM_SET_PMU_EVENT_FILTER or disabling the VM PMU also returns EINVAL.
Filter installation and extension enablement use the same VM lock.

Userspace must expose native Ice Lake CPU identity, DS, DTES64, PDCM, and
PEBS format 4 to the guest.  QEMU 8.2 requires
``-cpu host,migratable=off,pmu=on`` to retain the format bits, together with
the matching ``-accel kvm,hyperalloc-pebs-meminfo=on`` extension.  This ABI
does not add migration support; do not migrate these VMs to an unverified
host.  An unmodified guest kernel must not be used with this opt-in.

Wire contract
-------------

The read-only vendor MSR 0x4b564d10 returns 1 when this VM opted in and the
guest PMU exposes format 4.  Reads without opt-in fail; all writes fail,
including userspace KVM_SET_MSRS.  Version 1 means that every GP PEBS record
contains exactly 64 bytes: the 32-byte ``struct pebs_basic`` followed by the
32-byte ``struct pebs_meminfo``.  The hardware record's MEMINFO group bit
is set.  The record contains the eventing IP and linear memory address.

The host's existing perf code programs its GP PEBS counters with ADAPTIVE
enabled.  For this private protocol that behavior is intentional: KVM
always switches IA32_PEBS_DATA_CFG to MEMINFO (bit 0) on guest entry and
restores the host's configuration on exit.  Initialization and PMU reset/
refresh preserve that fixed value.  Neither the guest nor KVM_SET_MSRS
can change IA32_PEBS_DATA_CFG through this extension.

Guest RDMSR and WRMSR instructions for IA32_PEBS_DATA_CFG are unsupported
and inject #GP with KVM's default ignore_msrs=0.  Userspace MSR ioctls have
a separate, existing compatibility rule: because DATA_CFG is present in
KVM's advertised save/restore MSR list, KVM_GET_MSRS succeeds with value 0,
and KVM_SET_MSRS with value 0 succeeds as a no-op.  This zero is a synthetic
compatibility value, not the internal fixed MEMINFO configuration (1).
All nonzero KVM_SET_MSRS values are rejected with ignore_msrs=0, including
MEMINFO, GP/XMM/LBR group requests and all-bits requests.  The internal
configuration remains 1 after both the zero no-op and rejected writes.
Tests must distinguish these userspace ioctls from guest instructions.

The compatibility behavior is implemented by kvm_do_msr_access(): a
backend KVM_MSR_RET_UNSUPPORTED result is converted to a successful
zero-valued read or zero-valued write only for host-initiated accesses to
advertised MSRs.  intel_is_valid_msr() rejects DATA_CFG in this private
mode, so this compatibility path does not call its state-changing setter.
The extension preserves this upstream behavior; it does not depend on
ignore_msrs=1, and validation uses ignore_msrs=0.

Architectural PERF_CAP_PEBS_BASELINE stays hidden.  Guest event-select
ADAPTIVE bits, fixed-counter PEBS, and fixed-counter ADAPTIVE bits remain
reserved.  Precise guest events whose constraints require fixed counters are rejected
when perf validates the event, before any unsupported PEBS_ENABLE write.
Ordinary non-PEBS fixed counters keep working.  No GP-register,
XMM-register, counter-snapshot, or LBR record groups are enabled.  Existing
non-PEBS LBR virtualization is unchanged.  In particular, MEMINFO cannot
copy host LBR state into guest memory.  No userspace PMU event filter may
coexist with this protocol because adaptive hardware sampling changes the
set of effective events.

The guest detects KVM, Ice Lake identity, format 4 with baseline disabled,
and this read-only version MSR before changing its buffer record size.
It retains the normal GP PEBS event constraints and reuses the existing
format-4 decoder.  It does not enable PEBS_ALL or adaptive configuration.
PERF_SAMPLE_IP and PERF_SAMPLE_ADDR therefore keep their ordinary perf
ABI.  Physical addresses, when requested and permitted by perf policy,
are guest physical addresses.  Raw 0x20d1 is Ice Lake's
MEM_LOAD_RETIRED.L3_MISS and needs no load-latency threshold MSR.

Validation and deployment boundary
----------------------------------

Build both kernels and run the userspace KVM control tests, including
default-off behavior, malformed requests, both filter ordering cases,
readonly discovery, and rejection of DATA_CFG/ADAPTIVE/fixed PEBS writes.
For DATA_CFG, require the userspace zero-read/zero-write-no-op behavior
described above and rejection of every nonzero write.  Verify guest #GP
behavior separately with actual guest instructions when reporting it as
a runtime result.
Positive hardware acceptance additionally requires observing real
PERF_SAMPLE_IP and PERF_SAMPLE_ADDR records for raw 0x20d1 whose addresses
fall in the probe workload, while the guest still reports baseline=0.
Check the guest boot message for ``PEBS fmt4+-hyperalloc-meminfo-v1``.

An ordinary nested host has baseline disabled and PMU version 2; it cannot
establish these hardware preconditions.  Its expected result is capability
0, and it can validate only rejection paths and default guest behavior.
Compiling the host kernel or passing nested tests does not establish that
the positive physical-host sampling path works.  This patch does not
deploy or reboot the physical host.
