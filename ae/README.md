# Evaluation details

See the [main README](../README.md) for setup, run commands and output files.
Paths shown in code spans are relative to the repository root.

## Figures 7 and 8: single VM

[`ae/config/fig78-points.json`](config/fig78-points.json) provides the
configurations for each application.
For each application the VM memory allocation is fixed across all points;
PSI threshold/control period,
PEBS sampling period and other parameters vary according to the saved point.
These parameters affect the memory reclamation ratio and application performance,
so we tune them to examine how reclamation and performance change.
Each execution boots a fresh Guest and connects its RDMA pool before running
the application. Graph500 loads its prepared graph and performs 32 BFS runs.

For every application, successful new all-local measurements determine a
fixed denominator. Batch slowdown uses runtime; Memcached uses inverse
completed-request throughput; Cassandra follows the saved runtime metric.
Figure 8 uses **P95 latency**, and shares the exact Memcached/Cassandra runs
and parameter configurations with Figure 7. The all-local point is `(0%, 0%)`.
Average reclamation is integrated over the recorded application measurement
window using the Guest's fixed configured capacity, then averaged over the
successful executions out of three scheduled attempts.

## Figure 9: multiple VMs

| Mix | VM 1 | VM 2 | VM 3 |
|---|---|---|---|
| Mix1 | Memcached | GraphChi PageRank | XSBench |
| Mix2 | Memcached | GraphChi PageRank | Spark-KMeans |
| Mix3 | Memcached | Graph500 BFS | Liblinear |
| Mix4 | Cassandra | Graph500 BFS | XSBench |

The runner creates independent QEMU disk overlays, passes through one VF per
VM, and starts three independent remote-swap services. The Host client and VM
use disjoint physical cores. `bash fig9.sh --plan` shows the resource assignments.

Each repetition's value is the mean of its three application slowdowns; the
plotted bar averages the successful repetition values from three attempts. VM capacities, high-point settings
and isolated all-local denominators are preserved. VMs are stopped when the
repetition finishes; owned pool services are stopped and restarted for each attempt.

## Failed attempts

Each point or Mix has three scheduled attempts. If an application crashes,
times out, or fails validation, the runner records that attempt as `FAIL`,
cleans up its processes, VMs and RDMA pools, and proceeds to the next attempt.
The next attempt boots fresh VMs. A failed attempt is not retried or replaced.
If cleanup remains incomplete, the next attempt tries cleanup again before
starting a VM; it is recorded as failed if resources still cannot be released.

Means use only successful attempts: two successes are averaged, and one success
is used directly. A point or Mix with no successes is omitted from the figure.
Reports retain failed attempts, their errors, and the successful observation
count. A completed campaign with any failed attempts is marked `PARTIAL` and
still produces the figures for its valid measurements.

An application with no successful all-local baseline has no normalized curve.
Figure 9 skips Mixes requiring that missing baseline and continues the others;
it does not substitute archived baselines. If a figure has no valid measurements,
the runner saves its diagnostics without creating an SVG or PDF. User interrupts
stop the campaign. Existing result directories are not overwritten by a new run.

## Provided baselines

To simplify evaluation, we provide a prepared Chameleon environment for
reviewers to run the experiments. A fair comparison with baseline systems
requires the same hardware configuration. Due to limited hardware resources,
we currently do not have enough servers with that configuration to also set
up the HyperAlloc and HyperAlloc+Memtis.

We therefore provide baseline results measured in advance.
The plotting scripts combine these pre-run results with the reviewer's newly
measured Chameleon results to generate Figures 7, 8 and 9. Reviewers interested
in running the baseline systems can contact the authors. We will set up their
environments when additional servers with the matching hardware configuration
become available.

These baseline results were measured and supplied by the authors. The arrays
are preserved in `ae/results_baselines/`. Figure 8 uses **P95 latency slowdown**
for both the supplied baselines and reviewer-run Chameleon measurements.

## Relating the results to the two claims

- **Claim 1 — single-VM reclamation/slowdown trade-off (Figures 7 and 8):**
  compare Chameleon's curves with the supplied baseline curves over their
  overlapping reclamation range. The expected result is lower slowdown at a
  comparable reclamation ratio, or greater reclamation at a comparable
  slowdown. Figure 7 evaluates runtime/throughput slowdown across the eight
  supported applications; Figure 8 evaluates P95 latency slowdown for
  Memcached and Cassandra using the same runs. Use both figures to assess
  this claim, rather than reclamation alone.
- **Claim 2 — concurrent-VM application performance (Figure 9):** use
  `three-concurrent` execution to evaluate this claim and compare Chameleon's
  bar with the baseline bars for each of Mix1–4. The expected
  result is lower mean application slowdown when three applications run
  concurrently in separate VMs. Each bar averages successful repetitions from three attempts, with
  each repetition averaging the three application slowdowns against their
  isolated all-local denominators.
