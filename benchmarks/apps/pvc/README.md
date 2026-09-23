# Metis Page View Count

This directory implements the Page View Count (PVC) benchmark described in
the Mars paper (`papers/PVC.pdf`) on the repository's Metis framework.  A log
record is a fixed-width `<URL, IP, Cookie>` triple.  The implementation keeps
the paper's two executions rather than replacing them with a single count:

1. Stage 1 is `map_reduce`.  It groups the complete 24-byte triple and emits
   one result for every distinct triple.
2. The interstage step replaces the Stage-1 result array with one 64-bit mmap
   offset per distinct record, then releases the Stage-1 Metis results.
3. Stage 2 is `map_group`.  It emits `URL -> IP`; the number of grouped values
   (`keyvals_len_t::len`) is the page-view count.  There is intentionally no
   user reduce function in this stage.

Consequently, exact duplicate triples count once, while two records with the
same URL and IP but different cookies count twice.

Stage-1 results are deterministically ordered by a stable hash before the
offset handoff.  This avoids turning Stage 2 into an accidentally URL-sorted
scan while keeping results reproducible across worker counts.

## Build

From the repository root:

```sh
make -f benchmarks/apps/pvc/metis-pvc.mk
make -f benchmarks/apps/pvc/metis-pvc.mk test
```

This configures/builds the adjacent Metis checkout when necessary and writes
these binaries under `benchmarks/apps/pvc/build/`:

- `page_view_count`
- `pvc_generate`
- `konect_to_pvc`

An alternate Metis source tree can be supplied with
`METIS_ROOT=/absolute/path/to/metis`.  `make ... clean` removes only the PVC
build directory; it refuses unmarked directories and source-tree ancestors.

## Binary format

PVC inputs are little-endian files containing a 64-byte `pvc::FileHeader`
followed by packed, naturally aligned 24-byte `pvc::Record` values:

```text
Record = uint64 URL ID | uint64 IP ID | uint64 cookie ID
```

The header records its magic, version, byte order, structural sizes, record
count, ID-domain sizes, and generation seed.  The benchmark rejects unknown
versions, wrong byte order, integer overflow, truncation, and trailing bytes.
It mmaps a valid input read-only and leaves all Metis keys pointing into that
mapping; it does not copy or parse records in the measured passes.
An ID-domain size of zero denotes an unknown/non-compact domain; the KONECT
converter uses this marker for its opaque 64-bit cookie hashes.

## Run

```sh
benchmarks/apps/pvc/build/page_view_count INPUT \
  -p 4 -m 64 -r 64 -g 64 -q -o counts.tsv
```

`-p` selects Metis workers, `-m` selects map splits for both executions, `-r`
selects Stage-1 reduce tasks, and `-g` selects Stage-2 group tasks.  Zero asks
Metis to use its default or sampling-based choice.  Long aliases are available
via `--help`.  The optional TSV output is sorted numerically by URL ID.  Stdout
always includes phase labels, per-phase milliseconds, and the following
machine-readable correctness fields:

```text
stage1_unique=...
stage2_urls=...
total_views=...
checksum=...
```

The checksum covers sorted `(URL, count)` pairs and is independent of worker
and task counts.

## Workload creation

For a deterministic synthetic log:

```sh
benchmarks/apps/pvc/build/pvc_generate \
  --output pvc-32m.bin --bytes 32MiB --urls 100000 \
  --ips 1000000 --cookies 10000000 --duplicate-rate 0.10 \
  --distribution zipf --zipf-theta 1.10 --seed 1
```

`--bytes` specifies record payload bytes; the 64-byte header is additional.
Use `--records N` instead when an exact record count is desired.  Named output
files are written through a sibling temporary file and atomically installed,
so a normal generation failure does not truncate an existing workload.

`konect_to_pvc` converts an edge list using `URL=destination`, `IP=source`, and
a stable cookie derived from `(seed, source)`.  This preserves graph skew for
experiments, but it is a **KONECT-derived proxy**, not a real page-view log.
Because those hashes are opaque rather than a compact ID range, converted
headers set `cookie_count=0` (unknown/non-compact domain).

For repeatable Guest runs with resource preflight and archived timing logs:

```sh
benchmarks/scripts/run-pvc.sh \
  --input pvc-32m.bin --threads 4 \
  --map-tasks 64 --reduce-tasks 64 --group-tasks 64 \
  --minimum-available-mib 1024
```

The runner checks CPU affinity and basic free-memory headroom, archives both
the benchmark and log-writer exit statuses, and can reuse an existing binary
with `--skip-build` without requiring a Metis source checkout.

## Reproduction boundary

The public Metis repository does not contain the original PageViewCount
application or the paper authors' Wikipedia-to-log conversion.  This directory
is a clean implementation of the two-stage behavior described in `PVC.pdf`.
The KONECT Wikipedia graph can exercise it only through the explicitly labeled
proxy mapping above; it must not be presented as the authors' original PVC
input.  Likewise, the paper's 16-GiB configuration is a VM allocation, not a
direct input-size recipe.  Calibrate a synthetic input with an RSS sweep on the
target Guest before claiming a paper-scale memory experiment.  In particular,
Stage-1 results and the compact offset vector briefly coexist during handoff,
so the runner's basic 1-GiB headroom check is not a paper-scale peak-memory
guarantee.
