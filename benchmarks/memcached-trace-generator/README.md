# Trace-driven Memcached UDP generator

This is a standalone C++17 load generator for replaying the CacheLib KV
trace against Memcached over UDP. It streams the trace, expands `op_count`
and `ampFactor`, schedules aggregate open-loop Poisson arrivals, and records
completed-request latency in HdrHistogram_c without retaining one record per
request.

## Build and test

```bash
cd /path/to/chameleon-ae/benchmarks/memcached-trace-generator
cmake -S . -B build-release \
  -DHDR_HISTOGRAM_ROOT=../vendor/HdrHistogram_c \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build-release -j
ctest --test-dir build-release --output-on-failure
```

The test suite covers trace expansion and rewind, CLI validation, seeded
Poisson scheduling, binary Memcached framing, multi-datagram response
reassembly (including reorder/duplicate/loss), HdrHistogram merging, a fake
UDP server, and both integrated- and split-RX paths against a real local
Memcached process when `memcached` is installed.

## Run

Memcached disables UDP by default. A local test instance can be started with:

```bash
memcached -p 0 -U 11211 -B binary -m 1024 -t 10 -l 127.0.0.1
```

Run one 60-second point at 0.55 Mops/s:

```bash
./build-release/memcached-trace-generator \
  --server 127.0.0.1:11211 \
  --trace /path/to/chameleon-ae/benchmarks/dataset/cachelib-kvcache-202206 \
  --start-mpps 0 \
  --mpps 0.55 \
  --samples 1 \
  --runtime 60 \
  --rampup 4 \
  --workers 10 \
  --producer-shards 5 \
  --amp-factor 1 \
  --value-size 4096 \
  --request-timeout-ms 200 \
  --drain-ms 1000 \
  --max-send-lag-us 5 \
  --max-inflight 32768 \
  --queue-depth 8192 \
  --schedule-ahead-ms 20 \
  --batch-size 32 \
  --socket-buffer-mb 16 \
  --seed 1
```

Use `--help` for the complete option list.

## Current 3 Mops experiment

This is the exact high-rate configuration used for the current performance
demo. It is hardware-specific; do not copy the CPU lists to a machine with a
different topology without remapping them first.

### Host topology and placement

The tested host has two Intel Xeon Gold 6342 sockets/NUMA nodes, 24 physical
cores per node, and two SMT threads per core. The server is Memcached 1.6.14.
Both processes run on the same machine and communicate through
`127.0.0.1`; this is cross-NUMA process and memory placement over the Linux
loopback path, not a physical-NIC experiment. The loopback MTU was 65,536.
Check the local machine before applying the placement:

```bash
lscpu -e=CPU,NODE,CORE,SOCKET,ONLINE
numactl --hardware
```

On this host, node 0 contains logical CPUs `0-23,48-71`, with SMT sibling
pairs `(0,48)` through `(23,71)`. Node 1 contains `24-47,72-95`. The current
placement is:

| Role | Count | Logical CPUs | NUMA node | Physical-core use |
|---|---:|---|---:|---|
| TX workers | 42 | `3-23,51-71` | 0 | both SMT threads of cores 3-23 |
| Shared RX pollers | 3 | `0-2` | 0 | first SMT thread of cores 0-2 |
| Poisson producers | 3 | `48-50` | 0 | sibling SMT thread of cores 0-2 |
| Memcached | 10 worker threads | `24-39` process mask | 1 | server-only physical cores |

Every generator role owns a distinct logical CPU. The 42 TX workers are not
42 physically exclusive cores: they use both SMT threads of 21 physical
cores. This layout prioritizes maximum offered rate. If the experiment
requires one physical core per TX worker, reserve cores for RX/producer first
and reduce `--workers`; that alternative does not sustain 3 Mops on this host.

Three producer shards are intentional. A tested two-producer layout overloaded
one trace shard at 3 Mops, whereas three producers reported zero producer lag.
Three RX pollers were sufficient for the observed response rate, with a
maximum handoff depth of 106 in the formal run.

The CPU-list lengths must exactly match their thread counts. The generator
rejects duplicate logical CPUs, list-length mismatches, and CPUs outside the
affinity mask inherited from `numactl`.

The complete effective configuration, including explicitly repeated defaults,
is summarized here:

| Setting | Current value | Purpose |
|---|---:|---|
| `--mpps` / `--start-mpps` / `--samples` | `3` / `0` / `1` | one 3 Mops point |
| `--runtime` / `--rampup` / `--drain-ms` | `60` / `4` / `1000` | timing boundaries |
| `--workers` / `--rx-threads` / `--producer-shards` | `42` / `3` / `3` | thread layout |
| `--max-send-lag-us` | `100` | hard arrival-admission limit |
| `--queue-depth` / `--rx-queue-depth` | `4096` / `128` | bounded SPSC queues |
| `--max-inflight` / `--request-timeout-ms` | `32768` / `200` | per-worker request state |
| `--value-size` / `--amp-factor` | `4096` / `1` | trace replay payload/key space |
| `--schedule-ahead-ms` / `--batch-size` | `20` / `32` | scheduling and syscall batches |
| `--socket-buffer-mb` / `--seed` | `16` / `1` | requested client buffers/RNG seed |

### Exact server and generator commands

The following block creates a new result directory, starts the test Memcached
instance on node 1, runs the generator on node 0, and records the important
host-side UDP counters. Run it from the generator directory after building
`build-release`:

```bash
set -euo pipefail
cd /path/to/chameleon-ae/benchmarks/memcached-trace-generator

experiment_dir="../results/memcached/$(date -u +%Y%m%dT%H%M%SZ)-split-rx-42w"
mkdir -p "$experiment_dir"

lscpu >"$experiment_dir/lscpu.txt"
numactl --hardware >"$experiment_dir/numa-hardware.txt"
uname -a >"$experiment_dir/uname.txt"
ip -details link show lo >"$experiment_dir/loopback.txt"
/usr/bin/memcached -h >"$experiment_dir/memcached-help.txt"
pgrep -a numad >"$experiment_dir/numad.txt" 2>&1 || true
sysctl net.core.rmem_max net.core.rmem_default \
  net.core.wmem_max net.core.wmem_default \
  >"$experiment_dir/socket-sysctls.txt"

numactl --physcpubind=24-39 --membind=1 \
  /usr/bin/memcached \
  -p 0 -U 11212 -B binary -m 256 -t 10 -l 127.0.0.1 \
  >"$experiment_dir/server.log" 2>&1 &
experiment_server_pid=$!
cleanup_experiment() {
  kill "$experiment_server_pid" 2>/dev/null || true
  wait "$experiment_server_pid" 2>/dev/null || true
}
trap cleanup_experiment EXIT

sleep 1
kill -0 "$experiment_server_pid"
taskset -apc "$experiment_server_pid" \
  >"$experiment_dir/server-affinity-before.txt" 2>&1
numastat -p "$experiment_server_pid" \
  >"$experiment_dir/server-numastat-before.txt" 2>&1 || true
ss -lunmp 'sport = :11212' \
  >"$experiment_dir/server-socket-before.txt" 2>&1 || true
cp /proc/net/snmp "$experiment_dir/net-snmp-before.txt"
cp /proc/net/softnet_stat "$experiment_dir/softnet-before.txt"

/usr/bin/time -v -o "$experiment_dir/3m-time.txt" \
  numactl --physcpubind=0-23,48-71 --membind=0 \
  ./build-release/memcached-trace-generator \
  --server 127.0.0.1:11212 \
  --trace /path/to/chameleon-ae/benchmarks/dataset/cachelib-kvcache-202206 \
  --start-mpps 0 --mpps 3 --samples 1 \
  --runtime 60 --rampup 4 \
  --workers 42 --rx-threads 3 --producer-shards 3 \
  --worker-cpus 3-23,51-71 \
  --rx-cpus 0-2 \
  --producer-cpus 48-50 \
  --amp-factor 1 --value-size 4096 \
  --request-timeout-ms 200 --drain-ms 1000 \
  --max-send-lag-us 100 --max-inflight 32768 \
  --queue-depth 4096 --rx-queue-depth 128 \
  --schedule-ahead-ms 20 --batch-size 32 \
  --socket-buffer-mb 16 --seed 1 \
  >"$experiment_dir/3m.csv" \
  2>"$experiment_dir/3m.stderr"

cp /proc/net/snmp "$experiment_dir/net-snmp-after.txt"
cp /proc/net/softnet_stat "$experiment_dir/softnet-after.txt"
taskset -apc "$experiment_server_pid" \
  >"$experiment_dir/server-affinity-after.txt" 2>&1
numastat -p "$experiment_server_pid" \
  >"$experiment_dir/server-numastat-after.txt" 2>&1 || true
ss -lunmp 'sport = :11212' \
  >"$experiment_dir/server-socket-after.txt" 2>&1 || true
```

Memcached's `-p 0` disables TCP, `-U 11212` enables UDP on the experiment
port, and `-B binary` matches the generator protocol. Restart this test
instance before every independent rate point when a clean cache and identical
trace prefix are required.

`--runtime 60` means 60 seconds at the target rate. The first 10% of this
interval is warmup that is sent but excluded from counters and histograms, so
the CSV row reports `measured_seconds=54`. Including the 4-second ramp,
100-millisecond start delay, and 1-second drain, expected wall time is about
65 seconds.

The current Memcached cache is deliberately 256 MiB. A previous 1 GiB run
grew large enough for the root-owned `numad` service on this host to rewrite
both client and server affinity. Generator threads verify their masks every
100 ms and report repairs, but Memcached cannot do so. For a controlled run,
stop or exclude the benchmark processes from automatic placement services
when possible. Otherwise verify the server masks before and after the run and
discard any row with nonzero `affinity_repairs_total_sample`. Changing
Memcached's `-m` value also changes cache hit ratio, so record it with every
result.

For the archived run, `rmem_max` and `wmem_max` were 4,194,304 bytes and both
defaults were 212,992 bytes. The client requested 16 MiB and Linux reported
8,388,608-byte send and receive buffers (the kernel reports its doubled
internal accounting value). Memcached's listening socket retained an
`rb212992` receive buffer. Raising the sysctls or restarting Memcached after
doing so creates a different experiment configuration and must be recorded.

The RX ring depth of 128 is the measured configuration, not a universal
default. Check `maximum_rx_queue_depth_total_sample` and
`rx_handoff_drops_total_sample`; increase `--rx-queue-depth` if the high-water
mark approaches the capacity. Larger rings increase generator RSS and can
make automatic NUMA placement more likely.

### Current reference result

The 2026-09-21 run on the topology above produced:

| Metric | Result |
|---|---:|
| Target / actual TX | 3.000000 / 2.939870 Mops |
| Admission rate | 97.9822% |
| Completed goodput | 0.300315 Mops |
| Response rate | 10.2152% |
| Worker-late requests | 3,269,240 (2.0178%) |
| Producer late / queue full / no slot / send error | 0 / 0 / 0 / 0 |
| Client socket drops / RX handoff drops | 0 / 0 |
| Affinity repairs | 0 |
| Completed p99 / p99.9 | 698.879 / 919.551 us |
| Completed p99.99 / max | 5,595.135 / 9,969.663 us |

The row is `load_valid=0`: TX is 2.00% below target and therefore outside the
generator's 1% minimum tolerance, admission is below 99%, and response and
end-to-end rates are below 99%. Memcached's 212,992-byte UDP receive queue
dropped 163,043,816 datagrams. Across the complete ramp plus measurement run,
that drop count exactly equals `sent - completed`; client socket drops,
handoff drops, and softnet drops were zero. Thus the remaining completed-rate
bottleneck is the Memcached UDP receive queue, while the final approximately
2% TX shortfall is saturation of the generator/Linux loopback path under the
100-us admission bound.

Completed latency percentiles cover successful responses only. Offered-load
tail percentiles are `NA` for this run because most scheduled requests did not
complete. Do not report the completed p99 as the overall latency of this
overloaded point.

The archived CSV, stderr diagnostics, topology snapshots, and detailed
interpretation are in
[`../results/memcached/20260921T094555Z-split-rx-42w-final/`](../results/memcached/20260921T094555Z-split-rx-42w-final/).

## Replay semantics

The accepted CSV header is exactly:

```text
key,op,size,op_count,key_size
```

- `--trace` may name one file or a directory. A directory discovers only
  `kvcache_traces_*.csv`, in natural numeric filename order.
- The reader is streaming and has bounded memory. At the end of the last
  file it automatically rewinds to the first file until the configured time
  expires.
- `GET`, `SET`, and `DELETE` are supported. Each row is emitted exactly
  `op_count` times per amplified key, for `op_count * ampFactor` logical
  operations in total.
- A key is padded on the right with ASCII `0` until `key_size`. With
  `amp-factor > 1`, the CacheLib convention is used: remove up to the last
  four bytes while retaining at least 16 bytes, then append a four-digit
  suffix. Expansion order is suffix outermost, then `op_count` repeats.
- The trace `size` field is validated as an unsigned integer but otherwise
  ignored. Every SET sends exactly `--value-size` bytes (4096 by default),
  including when the trace size is smaller, zero, or larger.
- Final keys longer than Memcached's 250-byte limit and malformed trace rows
  fail immediately with the source filename and line number.
- `--producer-shards 1` is the file-order mode: it reads all files through one
  cursor in natural order, exactly as a single streaming reader would.
  The default `--producer-shards 0` selects `min(trace files, workers)` for the
  performance demo. Files are assigned round-robin to independent cursors,
  and each cursor loops over its own files. Its rate share is proportional to
  the number of workers assigned to that shard (shares are equal when workers
  divide evenly across shards), not to its logical row count. This parallel
  mode intentionally changes global inter-file ordering; the selected shard
  count is recorded in every CSV row.
- Trace cursors and Memcached state continue across internal rate-sweep
  samples. For experiments that require the same trace prefix and a clean
  server at every point, invoke the generator once per point with
  `--samples 1` and reset Memcached between invocations.

## Rate semantics

`--mpps` is the maximum aggregate rate in millions of logical operations per
second, after both `op_count` and `ampFactor` expansion. The rate points are:

```text
rate[j] = start_mpps + (mpps - start_mpps) / samples * j
j = 1 .. samples
```

For each point, the generator ramps from low load to the target in 100 ms
steps for exactly `--rampup` seconds, then runs at the target for
`--runtime` seconds. Arrivals are generated from an exponential interarrival
distribution and assigned absolute deadlines. As in the original netbench
generator, each UDP worker has an independent seeded Poisson stream at
`target / workers`; their superposition is an aggregate Poisson process at the
requested target rate. A producer shard merges the streams belonging to its
workers in deadline order while consuming its trace partition. The exact
key-to-worker assignment and seeded aggregate sequence therefore differ
between worker/shard counts.
Requests are assigned to the worker whose Poisson deadline is next, not by key
hash. This matches the balanced independent-worker structure of the original
performance generator, but does not guarantee strict same-key SET/GET order
across UDP sockets. Even with one producer shard, use `--workers 1` if strict
send order is more important than load-generator capacity.
The first 10% of steady-state time is sent but excluded from reported
statistics. This is the streaming, time-based equivalent of the original
generator's first-10%-of-packets discard; under stationary Poisson arrivals
both select the same expected fraction without retaining every request. Thus
the throughput denominator is `0.9 * runtime`; ramp and drain time are
excluded.

The scheduler only looks ahead by `--schedule-ahead-ms`. A request more than
`--max-send-lag-us` late, one whose worker queue is full, or one for which no
request-id slot is available is dropped at that arrival; it is never sent
later as catch-up traffic. Both producer and workers obey a hard wall-clock
phase/drain deadline, so an overloaded run cannot extend itself by replaying
an overdue schedule. Such a row is emitted with `load_valid=0` and an explicit
semicolon-separated `invalid_reason`. `--runtime` is per rate point, so a
sweep's total wall time includes the ramp, runtime, and drain for every
sample.

For deadline accuracy, a worker sleeps while its next event is far away and
busy-spins during the final 200 microseconds. Reserve (and, on noisy systems,
pin) at least one CPU per worker when interpreting high-rate results. `late`
is intentionally visible rather than hidden by catch-up bursts.

## UDP and latency behavior

Each worker owns one connected nonblocking UDP socket and one private
HdrHistogram. By default it handles both TX and RX. With `--rx-threads N`, N
shared epoll/`recvmmsg` pollers copy timestamped datagrams into bounded
per-socket SPSC rings; the TX worker remains the sole owner of request IDs,
reassembly, timeout state, and latency recording. This separates receive work
from deadline-sensitive sending without adding locks to request state.
Requests use the Memcached 8-byte UDP frame plus the binary
protocol. GET responses containing a 4096-byte value normally span multiple
Memcached UDP datagrams; the generator accepts out-of-order fragments,
deduplicates them, and records latency only after the complete response is
present. It does not copy or retain the returned value.

Request IDs are unique while active and quarantined before reuse because
nonzero response fragments do not contain the binary opaque value. UDP has no
retransmission: a missing request or response fragment becomes a timeout.
`--max-inflight` limits active requests per worker; the worker uses the full
16-bit wire-ID space so quarantined completed IDs do not consume that active
limit.

Latency is actual successful send to final response fragment, stored in
nanoseconds in per-worker HdrHistograms (1 ns to 60 s, three significant
figures) and merged after the worker threads stop. No coordinated-omission
correction is applied because arrivals are open-loop Poisson.

The CSV output contains target rate, actual transmitted Mops/s, completed
goodput, admission/response/end-to-end rates, operation outcomes, and every
drop/error category. `completed_p*` is the conventional latency distribution
over completed responses only. `offered_p*` uses all scheduled arrivals:
late, dropped, timed-out, outstanding, and out-of-histogram requests are
treated as infinite latency, so a percentile is `NA` when the finite-response
fraction is too small. Always report these columns together with `timeout`,
`socket_receive_drops_total_sample`,
`rx_handoff_drops_total_sample`,
`affinity_repairs_total_sample`,
`host_udp_rcvbuf_errors_delta_total_sample`, and the admission/response rates.

The main rate fields are computed as:

```text
actual_tx_mops  = sent / measured_seconds / 1,000,000
goodput_mops    = completed / measured_seconds / 1,000,000
admission_rate  = sent / scheduled
response_rate   = completed / sent
end_to_end_rate = completed / scheduled
```

A row is load-valid only when scheduling completes, at least one arrival is
present, producer-late fraction is at most 0.1%, admission, response, and
end-to-end rates are each at least 99%, RX handoff drops and affinity repairs
are zero, and actual TX is within
`max(1%, 5 / sqrt(scheduled))` of the target. Failure is reported through
`load_valid=0` and `invalid_reason`; it is a benchmark result, not a process
failure. The program still exits 0 after emitting such a row. Exit status 2
means a CLI/configuration error and exit status 1 means a runtime error.

Request counters and latency columns cover the measured 90% of steady state.
Columns ending in `_total_sample`, plus the queue-depth and producer-lag
high-water columns, cover ramp + steady state + drain. Configuration and
per-worker diagnostics go to stderr, including offered/sent/late counts,
queue high-water, RX progress, kernel `SO_RXQ_OVFL` drops, requested/actual
`SO_SNDBUF` and `SO_RCVBUF`, and all socket-option errno values. Stdout can be
redirected directly to a results file.

Because measured request counters exclude the first 10% of steady state while
host errors and `_total_sample` columns cover the whole sample, compare totals
only with totals. In the archived run, the equality between complete-sample
`sent - completed` and the server/host drop delta was established from the
per-worker stderr totals, not from the measured CSV `sent` column.

`host_udp_rcvbuf_errors_delta_total_sample` is read from Linux
`/proc/net/snmp`. It is host-wide rather than process-specific, but on a
dedicated same-host run it also exposes drops at Memcached's listening UDP
socket, which cannot be observed through the generator's client sockets. It
is `NA` when the counter is unavailable.

Linux caps `--socket-buffer-mb` at the host's `net.core.rmem_max` and
`net.core.wmem_max`; the generator reports the effective values and does not
mutate host-wide sysctls. On a dedicated test host, an administrator can
raise them before the run, for example:

```bash
sudo sysctl -w net.core.rmem_max=33554432
sudo sysctl -w net.core.wmem_max=33554432
sudo sysctl -w net.core.rmem_default=33554432
sudo sysctl -w net.core.wmem_default=33554432
sudo sysctl -w net.core.netdev_max_backlog=250000
```

Record both the sysctl values and reported effective socket buffers with the
result, and restart Memcached after changing the defaults so its listening
socket inherits the larger receive buffer. `SO_RXQ_OVFL` is read from received
datagrams; if drops occur after the last delivered datagram, Linux may not
expose the final cumulative value to the application.

## Practical UDP limit

A 4096-byte SET is one Memcached UDP request datagram and will normally be IP
fragmented on an MTU-1500 network. Losing any IP fragment loses the whole SET.
By contrast, a large GET response is split into application-level UDP
datagrams by Memcached and reassembled by this generator. Use jumbo frames
for the main performance demo when available, and consider a smaller
`--value-size` comparison to quantify fragmentation effects.

The current 3 Mops experiment uses `127.0.0.1` over an MTU-65,536 loopback
interface, so its 4096-byte SET requests are not IP-fragmented. The paragraph
above applies when the generator and server communicate over an MTU-1500
network. Memcached can still split large GET responses into its own UDP
application fragments on loopback, which the generator must reassemble.

The worker disables path-MTU discovery on its UDP sockets so Linux is allowed
to create those IP fragments instead of rejecting the 4096-byte send with
`EMSGSIZE`. This makes the default functional on MTU-1500 paths, but it does
not remove the loss and CPU costs of fragmentation.

The encoded request must fit the UDP payload limit of 65,507 bytes. The CLI
therefore conservatively limits `--value-size` to 65,217 bytes so even a
250-byte key fits. `--max-inflight` is per worker and must be below 65,536.
