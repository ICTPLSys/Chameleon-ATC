# Chameleon debugfs runtime parameter interface

Guest and Host parameters can be read and written with `cat` / `echo`, without
recompiling for each parameter change. The interface follows the per-file style
used by Atlas/Hermit. Writes validate ranges and use the existing control locks
so workers cannot observe partially updated configurations. The existing
`control` commands, `stats`, and QMP interfaces remain available. **Kernel boot
defaults, QEMU defaults, and the benchmark scripts' configured settings are
unchanged.**

These interfaces require Guest kernel, Host KVM, and QEMU versions containing
the implementation. Restart the affected VMs after updating Host KVM and QEMU.
See the [environment guide](../docs/environment.md) for installation.

## Basic usage

Run commands in a root shell on the corresponding machine. File permissions are
`0600`, except for the read-only `Lptw`, which is `0400`. If debugfs is not mounted:

```sh
mount -t debugfs debugfs /sys/kernel/debug
```

Guest example:

```sh
tracker=/sys/kernel/debug/chameleon
manager=/sys/kernel/debug/chameleon_mm
policy=/sys/kernel/debug/chameleon_policy

# Fixed sampling and cooling: set the saved values before switching modes.
echo 8192 > "$tracker/fixed_sample_period"
echo 0 > "$tracker/sampling_adaptive"
echo 131072 > "$tracker/fixed_cooling_samples"
echo 0 > "$tracker/cooling_adaptive"

# PSI some, 1%, 10 ms; enable both free-page and cold-page reclamation.
echo 0 > "$policy/psi_full"
echo 10000 > "$policy/threshold_ppm"
echo 10000 > "$policy/epoch_us"
echo 512 > "$policy/free_pages"
echo 16 > "$policy/cold_folios"

# HHH delta=0.7; wait 5 seconds after maintenance completes.
echo 700 > "$manager/hhh_dominance_permille"
echo 5000 > "$manager/maintenance_interval_ms"
echo 0 > "$manager/split_mode"
echo 0 > "$manager/selector"

# The existing atomic command can still initialize the full cost configuration.
echo 'batch 32 6400 128 4 10' > "$manager/control"
for order in 0 2 3 4 5 6 7 8 9; do
    echo "$((1 << order))" > "$manager/Creclaim_order$order"
done
# Individual parameters can then be adjusted, for example:
echo 6400 > "$manager/Csync"
cat "$manager/Lptw"

cat "$tracker/stats"
cat "$manager/stats"
cat "$policy/stats"
```

These commands set parameters. Controllers still start and stop through the
existing `echo enable/disable > .../control` commands. Parameters are not
persistent: rebooting restores boot defaults. Application runners still write
the configured experiment settings at startup. Make manual changes after runner
initialization, or update the experiment configuration source so the next run
does not overwrite them.

## Guest tracker: `/sys/kernel/debug/chameleon/`

| File | Kernel boot default | Valid range / unit | Update behavior |
|---|---:|---|---|
| `sampling_adaptive` | 1 | 0=fixed, 1=adaptive | Can change while enabled; updates existing PEBS event periods |
| `fixed_sample_period` | 4096 | 512–4294967295 events | Reprograms immediately in fixed mode; only saves the fixed value in adaptive mode |
| `cooling_adaptive` | 1 | 0=fixed, 1=adaptive | Immediately updates cooling state using the selected interval |
| `fixed_cooling_samples` | `totalram_pages()` at boot | 1–2^40 samples | Takes effect in fixed mode; shortening the interval processes cooling periods already crossed |
| `hotset_target_percent` | 30 | 1–100 percent | Immediately recomputes phi from the histogram |

`phi` remains a dynamically computed value read through `stats`; the adjustable
parameter is the target hot-set percentage. The Guest's initial `totalram_pages()`
does not include the full configured VM capacity. Once enabled, the policy updates
the tracker using actual capacity feedback. The tracker's boot-time value must
not replace the experiment's VM-memory denominator.

## Guest manager: `/sys/kernel/debug/chameleon_mm/`

| File | Kernel boot default | Valid range / meaning |
|---|---:|---|
| `hhh_dominance_permille` | 700 | 500–1000 per mille; 700 is exactly equivalent to the original delta=0.7 condition |
| `maintenance_interval_ms` | 5000 | 1–3600000 ms; delay after each work cycle; runtime changes reschedule the next cycle |
| `scan_folios` | 128 | 1–128; maximum eligible candidates processed during maintenance/aging |
| `split_mode` | 0 | 0=HHH, 1=Memtis |
| `selector` | 0 | 0=mixed_cost, 1=linux_lru |
| `memtis_min_bin` | 20 | 1–20; used only in Memtis comparison mode |
| `memtis_budget` | 32 | 1–128 folios; used only in Memtis comparison mode |
| `batch` | 0, unconfigured | 1–128; cost-model K, in folios |
| `Csync` | 0 | 0–U64_MAX; explicit synchronization cost |
| `Etrans` | 0, unconfigured | 1–U64_MAX; number of translation entries |
| `Nactive` | 0 | 0–U64_MAX; number of active contexts |
| `Lptw_fallback` | 0, unconfigured | 1–U64_MAX; explicit PTW latency / fallback value |
| `ptw_auto` | 1 | 0=manual fallback, 1=automatic PMU updates |
| `Lptw` | 0 | **Read-only**; value currently in use |
| `Creclaim_order0`, `Creclaim_order2`…`Creclaim_order9` | 0, unconfigured | 0–U64_MAX; reclamation cost for each order; no order1 file |

Zero cost defaults indicate an unconfigured model. The experiment scripts'
`32/6400/128/4/10` settings have not become kernel defaults. When setting parameters
individually, the cost model becomes valid only after `batch/Etrans/Lptw_fallback`
are all configured; each order also requires explicit configuration.
`Creclaim_orderN=0` assigns zero reclamation cost to that order, preserving
compatibility with the old `cost` command. Arithmetic overflow makes that
selection unavailable, preserving the existing protection.

`scan_folios` does not enlarge the static 128-entry candidate array. With target
process filtering, finding eligible candidates may require scanning more folios,
so it is not a hard limit on raw traversal count in every case. Changing `selector`
while manually isolated candidates are held returns `EBUSY`; first run
`echo putback > control`.

Debugging with a fixed PTW cost:

```sh
echo 0 > /sys/kernel/debug/chameleon_mm/ptw_auto
echo 10 > /sys/kernel/debug/chameleon_mm/Lptw_fallback
cat /sys/kernel/debug/chameleon_mm/Lptw
# Restore automatic PMU measurement:
echo 1 > /sys/kernel/debug/chameleon_mm/ptw_auto
```

Writing `Lptw_fallback` immediately updates the effective Lptw and clears the
measured flag, matching the old `batch` command. In automatic mode, the next valid
PMU update can still overwrite it. Switching back to automatic mode resets the
measurement baseline.

## Guest policy: `/sys/kernel/debug/chameleon_policy/`

| File | Kernel boot default | Valid range / unit |
|---|---:|---|
| `epoch_us` | 1000 | 1000–1000000 microseconds |
| `threshold_ppm` | 10000 | 0–1000000; 10000 = 1% |
| `psi_full` | 0 | 0=memory some, 1=memory full |
| `free_pages` | 512 | 0–2^32, must be a multiple of 512; 4 KiB pages per cycle |
| `cold_folios` | 8 | 0–128 folios per cycle |
| `minimum_local_bytes` | 0 | 0 or [2 MiB,2^52] bytes; 0 resolves to half the VM capacity when enabled |
| `discard_test` | 0 | 0/1; 1 requires CONFIG_CHAMELEON_TEST and is only for the existing tests without a data backend |

Production parameters can be updated while running. Updates take the lock and
wait for the current worker epoch to finish; the next epoch uses the new values.
Changing `epoch_us` does not cancel an already scheduled timer; the next timer
callback reloads the new period. A runtime update to `minimum_local_bytes` cannot
exceed actual total VM memory. Writing 0 while running immediately resolves to
half that total. Raising the floor only limits subsequent reclamation budgets;
it does not immediately restore pages already in remote memory.

While running, `free_pages` and `cold_folios` cannot both be zero. `discard_test`
must be changed with the controller disabled. Parameter changes are rejected
while failure cleanup still holds a lease. The existing `control` command's
`set ...` operation still permits writes only while disabled; runtime updates
are provided by the new per-parameter files.

`free_pages=0` disables proactive free-page reclamation; 512 reclaims at most
2 MiB per cycle. This is neither a percentage nor a Boolean switch set to 1.
The AE application settings in `ae/config/fig78-points.json` specify the
per-run value; these settings are separate from the kernel boot defaults.

The local-memory floor limits the combined free-page and cold-page reclamation
budget. As a calculation example, a 7700 MiB VM with a 2048 MiB local-memory floor
has a maximum reclamation budget of 5652 MiB. The command below illustrates that
floor; it does not specify an AE workload's VM size or high-point setting:

```sh
# min(6144,7700-2048)=5652 MiB, so the local-memory floor is 2048 MiB.
echo 2147483648 > /sys/kernel/debug/chameleon_policy/minimum_local_bytes
```

## Host EPT: a separate directory for each VM

The path is `/sys/kernel/debug/kvm/<QEMU-PID>-<VM-FD>/chameleon/`. Find the directory
for the current QEMU PID before writing; do not write to another VM's directory:

```sh
ls -d /sys/kernel/debug/kvm/*/chameleon
# Replace this example with the target QEMU directory from the output above.
host_knobs=/sys/kernel/debug/kvm/12345-10/chameleon
cat "$host_knobs/ept_mode"
echo deferred > "$host_knobs/ept_mode"
echo 512 > "$host_knobs/batch_pages"
echo 0 > "$host_knobs/watermark_bytes"
```

| File | QEMU default | Valid range / semantics |
|---|---:|---|
| `ept_mode` | deferred | `deferred`/`immediate`, also accepts 0/1; reads return the mode name |
| `batch_pages` | 512 | 1–U64_MAX, in 4 KiB pages; threshold for batching READY ranges |
| `watermark_bytes` | 0 | 0–U64_MAX bytes; 0 disables the Host available-memory low-watermark trigger |

EPT batch_pages, Guest cost-model K, and Guest free_pages per cycle are three
different parameters. QEMU startup options can still override these defaults.

KVM stores a unified requested configuration per VM, and QEMU synchronizes it
through the private `KVM_CHAMELEON_TUNING` interface. **QMP and debugfs modify the
same configuration.** QEMU synchronizes at each batch decision, QMP query, and
existing 100 ms timer tick; actual latency also includes thread scheduling.
FINALIZE batches already in flight retain the EPT mode selected when they were
created; the next batch uses the new value. QMP retains its existing rejection
of mode changes for batches in flight.

When an old QEMU has not registered the interface, file reads and writes return
`ENOTCONN`. New QEMU can still use the existing QMP approach on old KVM and prints
a compatibility fallback message, but Host debugfs parameter access is unavailable.
Errors reading Host configuration pause new batches and are logged. QMP may still
show the last cached value, which does not establish that a new parameter has
taken effect. Ordinary invalid values are rejected before modification. A copyout
fault in the private ioctl can occur after configuration has been committed;
issue GET again to verify the state in that case.

## Build and retest

Run from `hyperalloc-6.18/`. These commands check interface behavior in isolated
test VMs:

```sh
# Build incrementally with existing configurations; see the main README for a first build.
make -C linux O="$PWD/build/guest" CC=clang LOCALVERSION= -j24 bzImage modules
make -C linux O="$PWD/build/host" LOCALVERSION= -j24 bzImage
ninja -C build/qemu qemu-system-x86_64
make -C tests module userspace kvm-chameleon-tuning kvm-chameleon-control
python3 scripts/make-guest-initramfs.py

python3 scripts/test-chameleon.py --stage tuning --name debugfs-guest-rerun
python3 scripts/make-nested-initramfs.py --chameleon-control-only \
  --chameleon-tuning-tests --output build/debugfs-host-control.cpio.gz
python3 scripts/test-chameleon-host.py --tuning \
  --initramfs build/debugfs-host-control.cpio.gz --name debugfs-host-rerun
python3 scripts/make-nested-initramfs.py --chameleon --chameleon-policy \
  --host-shell --output build/debugfs-policy-host.cpio.gz
python3 scripts/test-chameleon-policy.py --tuning --regression \
  --initramfs build/debugfs-policy-host.cpio.gz --name debugfs-policy-rerun
```

`--host-shell` is an isolated test-image option that provides a serial shell for
the runner to write debugfs in L1; normal deployment does not need it. Production
disk packages use the existing `kernel-deploy.py --role guest/host build/package`
and `build-deploy-qemu.sh`; see the [environment guide](../docs/environment.md)
for installation. Updating only the Guest kernel provides the Guest files. Host
interfaces require updating both KVM and QEMU and restarting the affected VMs.
