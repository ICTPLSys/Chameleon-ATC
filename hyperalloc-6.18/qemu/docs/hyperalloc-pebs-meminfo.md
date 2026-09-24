# HyperAlloc restricted PEBS MEMINFO

Enable the private host/guest ABI explicitly:

```sh
qemu-system-x86_64 \
  -accel kvm,hyperalloc-pebs-meminfo=on \
  -cpu host,migratable=off,pmu=on \
  ...
```

The accelerator property defaults to `off`. Before creating any vCPU, QEMU
checks `KVM_CAP_HYPERALLOC_PEBS_MEMINFO` (`0x48410001`) and enables it with
`KVM_ENABLE_CAP`, `args[0] = 1`, and other arguments zero. A host without
the matching capability or a rejected enable request causes startup to fail.
There is no fallback that silently ignores this explicit request.

The matching host kernel exposes the guest read-only ABI identification MSR
`0x4b564d10`, value `1`. That ABI is restricted to fixed PEBS basic + MEMINFO
records and eligible general-purpose counters; it is not general adaptive
PEBS. QEMU does not set the architectural PEBS BASELINE bit or advertise
additional architectural PMU capabilities. The host enforces the allowed
record fields, counter types, event policy and per-VM authorization. The
matching guest kernel recognizes and consumes the private record ABI.

Use a single KVM accelerator for this mode. Explicitly configuring a separate
accelerator fallback follows QEMU's normal fallback semantics and does not
enable this KVM-specific ABI in another accelerator.

On the unmodified physical host, the three startup cases were checked: the
default and explicit `off` both start with KVM, while explicit `on` exits
with the missing-capability diagnostic. These checks do not establish that
MEMINFO address samples work; that requires the patched host kernel and
guest runtime sampling validation on capable physical hardware.
