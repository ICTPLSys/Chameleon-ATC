# Graph500: cached graph and 32 BFS runs

The Graph500 experiment uses SCALE=25, edgefactor=18, 8 OpenMP threads, and
32 BFS runs. Generate and cache the graph separately before measurement;
measured runs load the cache. All-local and Chameleon use the same edge list,
CSR adjacency structure, and 32 fixed roots, validating every BFS on each run.

The cache retains the original edge list and CSR arrays required for validation.
It loads them into anonymous memory rather than using file mappings as application
memory. Each measured run includes the full cache-loading, BFS, and validation
time. Graph generation, CSR construction, and cache writing happen before
measurement. The experiment workload uses `--require-cache` and fails if the
cache is missing.

Prepare the cache in the Guest first (this example uses 4 threads; measured runs
use 8 vCPUs):

```bash
bash ~/chameleon-benchmarks/scripts/run-graph500.sh \
  --scale 25 --edgefactor 18 --threads 4 --bfs-iterations 32 \
  --graph-cache ~/chameleon-inputs/graph500/csr-s25-e18-n32-seed3737844653-v1.bin \
  --prepare-only
```

Load and run it in an 8-vCPU Guest:

```bash
taskset -c 0-7 bash ~/chameleon-benchmarks/scripts/run-graph500.sh \
  --skip-build --scale 25 --edgefactor 18 --threads 8 --bfs-iterations 32 \
  --graph-cache ~/chameleon-inputs/graph500/csr-s25-e18-n32-seed3737844653-v1.bin \
  --require-cache
```

[`../ae/config/fig78-points.json`](../ae/config/fig78-points.json) stores the
Graph500 experiment configuration. All-local enables PEBS (65536 events) and
HHH (15 seconds), with the reclamation policy disabled.

Graph500's all-local, low, medium, and high points all use 32768 MiB of VM memory.

The build script applies
[`patches/graph500-csr-cache.patch`](patches/graph500-csr-cache.patch) to upstream
commit `6a21c992273f2ba7f742bb64af7bdfe1bc81f101`. The patch saves the complete CSR
and fixed roots, adds `-n/-L/-W/-P` options, and preserves `-fopenmp` when
command-line CFLAGS are supplied. Use a binary built with this patch for both
all-local and Chameleon measurements. The deployment script uploads the patch
and launcher together; `--skip-build` requires this binary to be built first.

Cache files include the version, byte order, SCALE, edgefactor, seed, BFS count,
and layout. Parameter mismatches or incomplete reads cause an error. Writes use
a temporary file that is renamed after completion. Use a new cache path when
changing the graph size, seed, or BFS count. Preparation logs are saved in
`prepare.log` / `prepare-time.txt`; measured-run logs are saved in
`stdout.log` / `time.txt` / `metadata.tsv`.

Validation command:

```bash
python3 benchmarks/scripts/test-graph500-cache.py
```

The tests cover actual OpenMP execution, validation of 32 BFS runs after graph
generation and loading, fixed-root consistency, cache reuse, parameter mismatches,
truncated files, and missing caches. Result summaries check the complete 0–31
validation sequence, actual thread count, and successful CSR loading against the
workload configuration. Older records are still interpreted as 64-BFS runs.

## Host numad and experiment CPU affinity

Keep each QEMU process on its assigned CPUs and NUMA node throughout the
experiment. If the Host runs `numad`, exclude the experiment VM from automatic
placement with the guard below. Set `--run-dir` to that VM's runtime directory:

```bash
sudo python3 benchmarks/scripts/guard-qemu-numad.py \
  --run-dir hyperalloc-6.18/build/running/guest-tools-final \
  --stop-file /tmp/chameleon-stop-numad-guard --duration 14400
```

The stop file must not exist when the guard starts. At the end of the experiment,
run `touch /tmp/chameleon-stop-numad-guard`. The guard verifies QEMU's user,
executable, VM name, and QMP path before calling `numad -x PID`. It updates the
exclusion when the VM restarts and removes it when the guard stops. It does not
stop numad or change CPU masks; measured runs still validate Host and Guest CPU
affinity. For VFIO EBUSY errors in the current startup log, the launcher retries
within the original timeout; other errors still fail. Diagnostic logs are saved
in the current run's result directory.
