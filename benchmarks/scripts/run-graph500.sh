#!/usr/bin/env bash
set -euo pipefail

source_dir=${GRAPH500_SOURCE:-"$HOME/chameleon-benchmarks/graph500-omp"}
output_base=${GRAPH500_OUTPUT_BASE:-"$HOME/chameleon-results/graph500"}
threads=${GRAPH500_THREADS:-4}
scale=${GRAPH500_SCALE:-22}
edgefactor=${GRAPH500_EDGEFACTOR:-15}
bfs_iterations=${GRAPH500_BFS_ITERATIONS:-32}
graph_cache=${GRAPH500_CACHE:-}
prepare_only=0
require_cache=0
source_revision=${GRAPH500_REVISION:-6a21c992273f2ba7f742bb64af7bdfe1bc81f101}
skip_build=0
allow_oversubscribe=0

usage() {
    cat <<'EOF'
Usage: run-graph500.sh [options]

Options:
  --source DIR             Graph500 OpenMP source directory
  --output-base DIR        Parent directory for timestamped results
  --threads N              OpenMP threads (default: 4)
  --scale N                Kronecker SCALE (default: 22)
  --edgefactor N           Edges per generated vertex (default: 15)
  --bfs-iterations N       BFS roots to run and verify (default: 32)
  --graph-cache FILE       Persistent edges, CSR and fixed BFS roots
  --prepare-only           Build/save cache without running BFS
  --require-cache          Fail if cache is absent; use for timed experiments
  --revision REV           Source revision recorded in metadata
  --skip-build             Reuse existing omp-csr binary
  --allow-oversubscribe    Ignore approximate memory preflight
  -h, --help               Show this help

The paper path is SCALE=27, edgefactor=15, verbose validation, and 8 vCPUs.
The default SCALE=22 is a validation run for the current 4-vCPU Guest.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

positive_integer() {
    case "$2" in ''|*[!0-9]*) die "$1 must be a positive integer: $2" ;; esac
    (( $2 > 0 )) || die "$1 must be greater than zero"
}

while (( $# > 0 )); do
    case "$1" in
        --source) (( $# >= 2 )) || die "$1 requires a value"; source_dir=$2; shift 2 ;;
        --output-base) (( $# >= 2 )) || die "$1 requires a value"; output_base=$2; shift 2 ;;
        --threads) (( $# >= 2 )) || die "$1 requires a value"; threads=$2; shift 2 ;;
        --scale) (( $# >= 2 )) || die "$1 requires a value"; scale=$2; shift 2 ;;
        --edgefactor) (( $# >= 2 )) || die "$1 requires a value"; edgefactor=$2; shift 2 ;;
        --bfs-iterations) (( $# >= 2 )) || die "$1 requires a value"; bfs_iterations=$2; shift 2 ;;
        --graph-cache) (( $# >= 2 )) || die "$1 requires a value"; graph_cache=$2; shift 2 ;;
        --prepare-only) prepare_only=1; shift ;;
        --require-cache) require_cache=1; shift ;;
        --revision) (( $# >= 2 )) || die "$1 requires a value"; source_revision=$2; shift 2 ;;
        --skip-build) skip_build=1; shift ;;
        --allow-oversubscribe) allow_oversubscribe=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

positive_integer threads "$threads"
positive_integer scale "$scale"
positive_integer edgefactor "$edgefactor"
positive_integer bfs_iterations "$bfs_iterations"
(( bfs_iterations >= 2 && bfs_iterations <= 64 )) || die "bfs-iterations must be 2..64"
[[ -n "$graph_cache" ]] || graph_cache="$HOME/chameleon-inputs/graph500/csr-s${scale}-e${edgefactor}-n${bfs_iterations}-seed${SEED:-3737844653}-v1.bin"
if (( require_cache )) && [[ ! -f "$graph_cache" ]]; then
    die "graph cache is absent; run --prepare-only first: $graph_cache"
fi
(( scale <= 40 )) || die "scale above 40 is not supported by this runner"
[[ -d "$source_dir" && -f "$source_dir/Makefile" ]] || die "invalid source directory: $source_dir"

vertices=$((1 << scale))
edges=$((vertices * edgefactor))
# Conservative scaling from the measured peak of this exact OpenMP
# implementation: SCALE=22/edgefactor=15 used about 2.12 GiB RSS.  The paper's
# 20 GiB VM allocation is not a safe allocation estimate for an unmodified
# standalone run, so do not use it as the preflight baseline.
estimated_mib=$(((2304 * edgefactor * vertices + 15 * (1 << 22) - 1) / (15 * (1 << 22))))
available_mib=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
if (( allow_oversubscribe == 0 && available_mib < estimated_mib + 1024 )); then
    die "estimated requirement is ${estimated_mib} MiB plus headroom, but MemAvailable is ${available_mib} MiB"
fi

if (( skip_build == 0 )); then
    if ! grep -q GRAPH500_CSR_CACHE "$source_dir/Makefile"; then
        script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
        patch_file="$script_dir/graph500-csr-cache.patch"
        [[ -f "$patch_file" ]] || patch_file="$script_dir/../patches/graph500-csr-cache.patch"
        [[ -f "$patch_file" ]] || die "missing Graph500 CSR cache source patch"
        patch --batch --forward --dry-run -d "$source_dir" -p1 <"$patch_file"
        patch --batch --forward -d "$source_dir" -p1 <"$patch_file"
    fi
    ln -sfn make-incs/make.inc-gcc "$source_dir/make.inc"
    make -C "$source_dir" BUILD_OPENMP=Yes CC=gcc \
        CFLAGS='-g -std=gnu99 -Wall -O3 -march=native' \
        CFLAGS_OPENMP=-fopenmp LDLIBS='-lm -lrt' clean
    make -C "$source_dir" -j"$threads" BUILD_OPENMP=Yes CC=gcc \
        CFLAGS='-g -std=gnu99 -Wall -O3 -march=native' \
        CFLAGS_OPENMP=-fopenmp LDLIBS='-lm -lrt' omp-csr/omp-csr
fi

binary="$source_dir/omp-csr/omp-csr"
[[ -x "$binary" ]] || die "Graph500 binary not found: $binary"
binary_help=$("$binary" -h)
[[ "$binary_help" == *"Load an omp-csr checkpoint"* ]] || die "rebuild Graph500: this binary lacks CSR cache support"
run_id=$(date -u +%Y%m%dT%H%M%SZ)
result_dir="$output_base/$run_id"
mkdir -p "$result_dir"
command=("$binary" -s "$scale" -e "$edgefactor" -n "$bfs_iterations" -V -L "$graph_cache")
paper_scale=no
[[ "$scale" == 27 && "$edgefactor" == 15 ]] && paper_scale=yes

{
    printf 'field\tvalue\n'
    printf 'timestamp_utc\t%s\n' "$run_id"
    printf 'hostname\t%s\n' "$(hostname)"
    printf 'kernel\t%s\n' "$(uname -r)"
    printf 'source_revision\t%s\n' "$source_revision"
    printf 'threads\t%s\n' "$threads"
    printf 'scale\t%s\n' "$scale"
    printf 'edgefactor\t%s\n' "$edgefactor"
    printf 'bfs_iterations\t%s\n' "$bfs_iterations"
    printf 'graph_cache\t%s\n' "$graph_cache"
    printf 'graph_cache_format\t%s\n' 'csr-v1'
    printf 'seed\t%s\n' "${SEED:-3737844653}"
    printf 'prepare_only\t%s\n' "$prepare_only"
    printf 'generated_vertices\t%s\n' "$vertices"
    printf 'generated_edges\t%s\n' "$edges"
    printf 'estimated_memory_mib\t%s\n' "$estimated_mib"
    printf 'paper_graph_scale\t%s\n' "$paper_scale"
    printf 'command\t'; printf '%q ' "${command[@]}"; printf '\n'
} >"$result_dir/metadata.tsv"
cp /proc/meminfo "$result_dir/meminfo-before.txt"

printf 'Graph500 result directory: %s\n' "$result_dir"
printf 'SCALE=%s edgefactor=%s threads=%s (~%s MiB preflight)\n' \
    "$scale" "$edgefactor" "$threads" "$estimated_mib"
if [[ ! -f "$graph_cache" ]]; then
    mkdir -p "$(dirname "$graph_cache")"
    OMP_NUM_THREADS="$threads" OMP_PROC_BIND=close OMP_PLACES=cores \
        /usr/bin/time -v -o "$result_dir/prepare-time.txt" \
        "$binary" -s "$scale" -e "$edgefactor" -n "$bfs_iterations" -V \
        -W "$graph_cache" -P >"$result_dir/prepare.log" 2>&1
    printf 'cache_prepared\t1\n' >>"$result_dir/metadata.tsv"
else
    printf 'cache_prepared\t0\n' >>"$result_dir/metadata.tsv"
fi
if (( prepare_only )); then
    printf 'Graph500 cache ready: %s\n' "$graph_cache"
    exit 0
fi
printf 'Loading cached CSR graph; BFS iterations=%s; cache=%s\n' "$bfs_iterations" "$graph_cache"
set +e
OMP_NUM_THREADS="$threads" OMP_PROC_BIND=close OMP_PLACES=cores \
    /usr/bin/time -v -o "$result_dir/time.txt" \
    stdbuf -oL -eL "${command[@]}" 2>&1 | tee "$result_dir/stdout.log"
status=${PIPESTATUS[0]}
set -e
printf '%s\n' "$status" >"$result_dir/exit-status"
printf 'exit_status\t%s\n' "$status" >>"$result_dir/metadata.tsv"
cp /proc/meminfo "$result_dir/meminfo-after.txt"
(( status == 0 )) || exit "$status"
printf 'Graph500 completed successfully; results: %s\n' "$result_dir"
