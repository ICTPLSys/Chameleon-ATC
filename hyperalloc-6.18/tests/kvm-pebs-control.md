# Restricted PEBS MEMINFO control-interface tests

Build: `./tests/kvm-pebs-control-build.sh`.

The static `tests/kvm-pebs-control` binary exercises the private per-VM capability `0x48410001` and read-only ABI MSR `0x4b564d10`. It uses `/dev/kvm`, supported CPUID, the system `KVM_GET_MSRS` feature query, and guest MSR save/restore ioctls. It creates only disposable VM/vCPU objects and does **not** invoke `KVM_RUN`, reload modules, or modify host settings.

## Commands and meaning

```sh
# Requires a host that advertises the private capability.
./tests/kvm-pebs-control

# Explicit negative test on an unpatched or hardware-gated host.
./tests/kvm-pebs-control --expect-unavailable
```

Default execution returns **4 / SKIP** when the capability is absent. This is not a successful supported-host test. `--expect-unavailable` requires `KVM_CHECK_EXTENSION=0` and a rejected enable request: `EINVAL` on an unknown-capability kernel, or `EOPNOTSUPP` when the patched extension's hardware gate is false.

Before loading the candidate, the physical host passed the explicit negative test (3 checks, unknown capability / `EINVAL`), and the default invocation returned 4. Logs are `kvm-pebs-control-current-host.log` and `kvm-pebs-control-default-current-host.log` in this directory.

After the user loaded the candidate modules, the supported-host execution passed **67 checks**; see `../results/kvm-pebs-control-active-final.log`. The first run stopped at check 46 because the test incorrectly required userspace DATA_CFG reads to fail; its unchanged log is `../results/kvm-pebs-control-active-initial.log`. The correction below preserves the upstream KVM save/restore compatibility behavior and does not broaden the guest ABI.

## Supported-host assertions

- Default VM: private MSR absent; no implicit opt-in.
- Correct enable request: `args[0]=1`, zero flags/remaining args, before vCPU creation; repeated early enable is idempotent.
- Illegal arguments return `EINVAL`; enable after vCPU creation returns `EBUSY`.
- Opted-in VM with valid PDCM/PMU/PEBS-format-4 state: private MSR reads 1 and rejects writes of 0, 1, and all bits, including userspace restore.
- PMU filters and the private mode reject coexistence in both operation orders. Disabling the PMU and enabling the mode are likewise mutually exclusive.
- `PEBS_BASELINE` remains masked in the system capability MSR and the guest; userspace cannot restore that bit.
- Userspace `KVM_GET_MSRS(DATA_CFG)` returns exactly zero. `KVM_SET_MSRS(DATA_CFG, 0)` succeeds as a compatibility no-op; another read still returns zero and the private ABI MSR remains 1. Nonzero restore attempts for MEMINFO, GP registers, XMM, LBR, LBR count, and all bits are rejected.
- GP-counter PEBS and the paper's raw event encoding remain usable. Fixed-counter PEBS enable, adaptive GP event-select, and adaptive fixed-counter control are rejected; rejected writes preserve prior state.

`kvm_do_msr_access()` permits userspace to save an advertised but unsupported MSR as zero and restore zero as a no-op. This fallback is `host_initiated`; it does not permit guest RDMSR/WRMSR. The ioctl test cannot directly read the forced internal MEMINFO bit or prove guest instruction faults. The internal layout needs source review and actual guest PEBS IP/address/physical-address validation with `pmu_probe --require-pebs`. Guest instruction access is checked separately by `guest-msr-guard` below. A nested host whose L0 masks the required hardware capability cannot execute the supported-host branch and must not count it as passed.

## Guest instruction fault test

Build `./tests/guest-msr-guard-build.sh`, then copy the static `tests/guest-msr-guard` binary into the disposable opted-in guest and run it there with no arguments. The guest needs `CONFIG_X86_MSR=y`, `/dev/cpu/<cpu>/msr`, and sufficient permissions to access that device. The tool requires the hypervisor CPUID bit, private ABI MSR value 1, PEBS format 4, and masked PEBS_BASELINE before attempting any write. It must not be run as a physical-host test.

The Linux MSR driver executes `rdmsr_safe_on_cpu()` / `wrmsr_safe_on_cpu()` for `pread()` / `pwrite()`. In this guest these are actual ring-0 guest RDMSR/WRMSR instructions, not host KVM_GET_MSRS/KVM_SET_MSRS ioctls. The guest exception-table handler turns a general-protection fault into `-EIO`. Only this precise error counts as a successfully blocked operation; permission errors or short transfers fail.

The 21 checks require guest DATA_CFG reads and all tested writes (including zero) to fault; the private ABI MSR rejects writes; fixed-counter PEBS, adaptive GP event-select, and adaptive fixed-counter control writes fault and preserve prior state. Any forbidden write that succeeds terminates the process immediately without issuing further writes. The success marker is `PASS GUEST_MSR_GUARD checks=21 actual_guest_instructions=1 unexpected_write_success=0`.

Actual execution passed all 21 checks in `../results/vm-pmu-meminfo-final/guest-msr-guard.log`. The same guest then produced 222 validated IP/VA/guest-PA samples, observed PTW counters, and passed the full HyperAlloc core regression; see that directory's `report.json` and `pmu_probe.log`.

The native MSR driver normally prints a warning for an unrecognized write and marks the guest kernel with `TAINT_CPU_OUT_OF_SPEC` even when the attempted WRMSR faults. The isolated VM harness may temporarily set its own `/sys/module/msr/parameters/allow_writes` to `on` and restore the prior value to suppress that expected warning; the taint still occurs. The tool does not modify this parameter. These guest-only effects do not change physical-host MSRs or host module settings.
