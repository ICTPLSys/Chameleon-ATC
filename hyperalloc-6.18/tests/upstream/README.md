# Additional Linux 6.18 / HyperAlloc workload checks

Build with `./tests/upstream/build.sh`. All four outputs in `tests/upstream/bin/` are static x86-64 binaries. The build exports Linux 6.18 UAPI headers into the local `headers/` directory; it does not modify kernel source or require guest development tools. Logs: `results/upstream-headers-build.log` and `results/upstream-build.log`.

Run the following **inside the disposable root test guest**, after mounting `/proc` and `/sys`. `khugepaged-anon-subset` changes and restores global THP settings. The commands below use the guest harness installation directory `/tests`:

```sh
/tests/madv_populate
/tests/mremap_dontunmap
/tests/khugepaged-anon-subset 0
/tests/khugepaged-anon-subset 4
mkdir -p /tmp/stream-check
cd /tmp/stream-check
/tests/stream-check
```

## Coverage and required results

| Binary | Source / coverage | Required result |
|---|---|---|
| `madv_populate` | Unmodified `linux/tools/testing/selftests/mm/madv_populate.c`: anonymous prefault, read/write permissions, VMA holes, presence checks; optionally soft-dirty | Exit 0; TAP plan 16 and 16 `ok` with the current guest config. With `CONFIG_MEM_SOFT_DIRTY`, plan becomes 21. No `not ok` or `# SKIP`. |
| `mremap_dontunmap` | Unmodified `linux/tools/testing/selftests/mm/mremap_dontunmap.c`: remap anonymous/shmem mappings, fixed/partial moves and overwrite; byte-level data checks | Exit 0; TAP plan 5 and all 5 `ok`; no skips. In particular, a kernel missing MREMAP_DONTUNMAP may exit without all tests, which is **not** a pass. |
| `khugepaged-anon-subset 0` | Thin selection entry point directly includes upstream `khugepaged.c`; anonymous base-page faults followed by MADV_COLLAPSE, COW, compound PTEs and re-collapse | Exit 0 and `PASS upstream-collapse-subset order=0 cases=10 plus_alloc_at_fault`; no `Fail`, `Timeout`, or `Skip`. |
| `khugepaged-anon-subset 4` | Same selected upstream cases, with order-4 (64 KiB) anonymous fault policy; collapse output is PMD sized | Exit 0 and the corresponding `order=4 cases=10` marker; no failure/skip. Actual source mTHP folio-size proof is provided separately by `memory_test mthp`, since upstream khugepaged checks PMD mappings via smaps. |
| `stream-check` | Unmodified HyperAlloc `hyperalloc-stream/stream.c` included by a checking entry point, all Copy/Scale/Add/Triad operations enabled | Exit 0, original `Solution Validates`, and `PASS STREAM all-elements arrays=4194304 iterations=10 kernels=4`. Also reject CSV output errors. |

Peak mapped memory of the selected collapse cases is small (the largest full-collapse case uses four PMD regions). STREAM uses three 32 MiB arrays, 96 MiB total, single threaded, for ten iterations. It writes `Copy.csv`, `Scale.csv`, `Add.csv`, and `Triad.csv` in its working directory. This is a correctness workload; its timing numbers are not a reproduction of paper performance.

## Explicit selection boundaries

The collapse binary is a **subset**, not the entire upstream khugepaged suite. It reuses the unmodified test functions and settings helpers and selects:

1. `collapse_full`
2. `collapse_empty`
3. `collapse_single_pte_entry`
4. `collapse_max_ptes_none`
5. `collapse_single_pte_entry_compound`
6. `collapse_full_of_compound`
7. `collapse_fork`
8. `collapse_fork_compound`
9. `collapse_max_ptes_shared`
10. `madvise_collapse_existing_thps`

It also runs upstream `alloc_at_fault`. No selected anonymous case contains an applicable filesystem skip. The original `khugepaged -s 4 madvise:anon` CLI cannot filter individual cases and still invokes swap tests; without configured swap, these fail. The subset therefore explicitly excludes swap cases, file/shmem collapse, the approximately 1 GiB extreme compound construction, and asynchronous khugepaged scheduling tests. Excluded cases are not counted as passed.

`hugepage-mremap` was not selected: it uses hugetlb reservations and userfaultfd, and the current guest has `CONFIG_USERFAULTFD=n`. `mremap_dontunmap` provides direct remap/data coverage without those dependencies.

## HyperAlloc provenance and inflate

STREAM source is vendored without modification in `vendor/stream.c`, from [`luhsra/hyperalloc-stream`](https://github.com/luhsra/hyperalloc-stream) commit `54ab9af3610da6011ce9c9f597fed8dd96367165`. Its original license is retained in the source header and `vendor/STREAM-LICENSE.txt`; see `vendor/README.md` for provenance and publication requirements. Building this workload requires no separate HyperAlloc reference checkout. All four operation macros are enabled because the original validator assumes all four operations; compiling only COPY produces incompatible validation semantics. The original validator also returns through `main` with exit 0 on data error. `stream-check.c` adds an independent check of **every element**, including NaN/Inf rejection, and a nonzero failure status. The reference repository is unchanged.

`hyperalloc-bench/inflate/bench.py` is a Python/SSH/QMP orchestration workload, not a standalone C program suitable for static linking. Its touch phase uses `linux-alloc-bench` through `/proc/alloc/run`, plus a separate `write` program. A static STREAM executable does not reproduce that orchestration. The port's VM harness must validate equivalent shrink/grow/reinstall behavior explicitly and must not label it a successful run of the original inflate suite.

All selected binaries passed in the final Linux 6.18 HyperAlloc guest: see `results/vm-manual/report.json` and the per-program logs in that directory. The runner requires every declared TAP case and marker and rejects skips. STREAM also passed a host-only, process-local preflight (`results/upstream-stream-host/stream-host.log`). No MM selftest changing physical-host THP settings was run.
