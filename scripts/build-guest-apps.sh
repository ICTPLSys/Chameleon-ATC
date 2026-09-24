#!/usr/bin/env bash
# Run as the benchmark user inside the template Guest.
set -euo pipefail
if [[ ${1:-} == --help ]]; then
  echo 'Usage: build-guest-apps.sh [BENCHMARK_ROOT] [JOBS]'; exit 0
fi
bench_root=${1:-"$HOME/chameleon-benchmarks"}
jobs=${2:-4}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo 'JOBS must be positive' >&2; exit 2; }
for name in xsbench liblinear-multicore-2.50 graphchi graph500-omp apache-cassandra-5.0.1 spark-3.3.1-bin-hadoop3; do
  [[ -d "$bench_root/apps/$name" ]] || { echo "Missing $bench_root/apps/$name" >&2; exit 1; }
  ln -sfn "apps/$name" "$bench_root/$name"
done
make -C "$bench_root/xsbench/openmp-threading" clean
make -C "$bench_root/xsbench/openmp-threading" -j"$jobs" CC=gcc
make -C "$bench_root/liblinear-multicore-2.50" clean
make -C "$bench_root/liblinear-multicore-2.50" -j"$jobs"
make -C "$bench_root/graphchi" -B -j"$jobs" example_apps/pagerank
(cd "$bench_root/apps/metis" && ./configure)
make -C "$bench_root/apps/metis" clean
make -C "$bench_root/apps/pvc" -f metis-pvc.mk -B -j"$jobs" \
  "METIS_ROOT=$bench_root/apps/metis" all
bash "$bench_root/scripts/memcached-guest.sh" build --jobs "$jobs"
graph="$bench_root/graph500-omp"
if ! grep -q GRAPH500_CSR_CACHE "$graph/Makefile"; then
  patch --batch --forward -d "$graph" -p1 <"$bench_root/patches/graph500-csr-cache.patch"
fi
ln -sfn make-incs/make.inc-gcc "$graph/make.inc"
make -C "$graph" BUILD_OPENMP=Yes CC=gcc clean
make -C "$graph" -j"$jobs" BUILD_OPENMP=Yes CC=gcc \
  CFLAGS='-g -std=gnu99 -Wall -O3 -march=native' \
  CFLAGS_OPENMP=-fopenmp LDLIBS='-lm -lrt' omp-csr/omp-csr
printf '%s\n' 'Guest application binaries are ready.'
