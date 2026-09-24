# Application sources and inputs

The AE entry points are [`../fig78.sh`](../fig78.sh),
[`../fig9.sh`](../fig9.sh) and [`../run-all.sh`](../run-all.sh).
Follow the root [README](../README.md) and
[environment guide](../docs/environment.md) to prepare the Guest template.

`apps/` contains application source and required Spark/Cassandra/YCSB runtime
distributions. `dataset/` contains raw inputs and the Spark input used by the
saved configuration. `vendor/HdrHistogram_c/` is the local dependency of the
Memcached trace generator.

The evaluation configuration is
[`../ae/config/fig78-points.json`](../ae/config/fig78-points.json).

Graph500's CSR graph is generated once in the template and loaded for each
32-BFS execution. See [README-graph500-cache.md](README-graph500-cache.md).
PVC is the included independent Metis implementation. SPEC CPU2017 gcc is
not included.

