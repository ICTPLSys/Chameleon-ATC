#!/usr/bin/env bash
set -euo pipefail

source_dir=${GRAPHCHI_SOURCE:-"$HOME/chameleon-benchmarks/graphchi"}
output_base=${GRAPHCHI_OUTPUT_BASE:-"$HOME/chameleon-results/graphchi"}
work_base=${GRAPHCHI_WORK_BASE:-"$HOME/chameleon-work/graphchi"}
input=${GRAPHCHI_INPUT:-}
threads=${GRAPHCHI_THREADS:-4}
iterations=${GRAPHCHI_ITERATIONS:-4}
nshards=${GRAPHCHI_NSHARDS:-4}
membudget_mib=${GRAPHCHI_MEMBUDGET_MIB:-4096}
source_revision=${GRAPHCHI_REVISION:-6461c89f217f63482e2468d776bb942067f8288c}
skip_build=0
keep_work=0

usage() {
    cat <<'EOF'
Usage: run-graphchi.sh --input EDGE_LIST [options]

Options:
  --input FILE          Plain text "source destination" edge list (required)
  --source DIR          GraphChi source directory
  --output-base DIR     Parent directory for timestamped result logs
  --work-base DIR       Directory for copied input and generated shards
  --threads N           OpenMP/load/execute threads (default: 4)
  --iterations N        PageRank iterations (default: 4)
  --nshards N           GraphChi shards (default: 4)
  --membudget-mib N     GraphChi memory budget (default: 4096)
  --revision REV        Source revision recorded in metadata
  --skip-build          Reuse existing pagerank binary
  --keep-work           Keep copied input and generated shards after success
  -h, --help            Show this help

The paper uses the Twitter graph at 61.5M vertices/2.4G edges.  A prefix or
other scaled edge list validates the pipeline but is not a paper-scale result.
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
        --input) (( $# >= 2 )) || die "$1 requires a value"; input=$2; shift 2 ;;
        --source) (( $# >= 2 )) || die "$1 requires a value"; source_dir=$2; shift 2 ;;
        --output-base) (( $# >= 2 )) || die "$1 requires a value"; output_base=$2; shift 2 ;;
        --work-base) (( $# >= 2 )) || die "$1 requires a value"; work_base=$2; shift 2 ;;
        --threads) (( $# >= 2 )) || die "$1 requires a value"; threads=$2; shift 2 ;;
        --iterations) (( $# >= 2 )) || die "$1 requires a value"; iterations=$2; shift 2 ;;
        --nshards) (( $# >= 2 )) || die "$1 requires a value"; nshards=$2; shift 2 ;;
        --membudget-mib) (( $# >= 2 )) || die "$1 requires a value"; membudget_mib=$2; shift 2 ;;
        --revision) (( $# >= 2 )) || die "$1 requires a value"; source_revision=$2; shift 2 ;;
        --skip-build) skip_build=1; shift ;;
        --keep-work) keep_work=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -n "$input" ]] || die "--input is required"
[[ -r "$input" ]] || die "input is not readable: $input"
[[ -d "$source_dir" && -f "$source_dir/Makefile" ]] || die "invalid source directory: $source_dir"
positive_integer threads "$threads"
positive_integer iterations "$iterations"
positive_integer nshards "$nshards"
positive_integer membudget_mib "$membudget_mib"

input_bytes=$(stat -c %s "$input")
input_edges=$(wc -l <"$input")
mkdir -p "$work_base"
available_bytes=$(df -PB1 "$work_base" | awk 'NR==2 {print $4}')
# Shards, edge data, vertex data and the private input copy need more than the
# source edge list.  Five times input plus 512 MiB is a conservative preflight.
required_bytes=$((input_bytes * 5 + 512 * 1024 * 1024))
(( available_bytes >= required_bytes )) || \
    die "insufficient work disk: need about $required_bytes bytes, have $available_bytes"

if (( skip_build == 0 )); then
    make -C "$source_dir" example_apps/pagerank
fi
binary="$source_dir/bin/example_apps/pagerank"
[[ -x "$binary" ]] || die "GraphChi pagerank binary not found: $binary"

run_id=$(date -u +%Y%m%dT%H%M%SZ)
result_dir="$output_base/$run_id"
work_dir="$work_base/$run_id"
mkdir -p "$result_dir" "$work_dir"
work_input="$work_dir/graph.edgelist"
cp "$input" "$work_input"
input_sha256=$(sha256sum "$input" | awk '{print $1}')

command=(
    "$binary"
    "--file=$work_input"
    --filetype=edgelist
    "--nshards=$nshards"
    "--niters=$iterations"
    "--execthreads=$threads"
    "--loadthreads=$threads"
    "--membudget_mb=$membudget_mib"
    --top=20
)

{
    printf 'field\tvalue\n'
    printf 'timestamp_utc\t%s\n' "$run_id"
    printf 'hostname\t%s\n' "$(hostname)"
    printf 'kernel\t%s\n' "$(uname -r)"
    printf 'source_revision\t%s\n' "$source_revision"
    printf 'input\t%s\n' "$input"
    printf 'input_bytes\t%s\n' "$input_bytes"
    printf 'input_edges\t%s\n' "$input_edges"
    printf 'input_sha256\t%s\n' "$input_sha256"
    printf 'threads\t%s\n' "$threads"
    printf 'iterations\t%s\n' "$iterations"
    printf 'nshards\t%s\n' "$nshards"
    printf 'membudget_mib\t%s\n' "$membudget_mib"
    printf 'paper_scale\tno\n'
    printf 'command\t'; printf '%q ' "${command[@]}"; printf '\n'
} >"$result_dir/metadata.tsv"
cp /proc/meminfo "$result_dir/meminfo-before.txt"

printf 'GraphChi result directory: %s\n' "$result_dir"
printf 'Input edges: %s; iterations: %s; shards: %s; threads: %s\n' \
    "$input_edges" "$iterations" "$nshards" "$threads"
set +e
GRAPHCHI_ROOT="$source_dir" \
OMP_NUM_THREADS="$threads" OMP_PROC_BIND=close OMP_PLACES=cores \
    /usr/bin/time -v -o "$result_dir/time.txt" \
    stdbuf -oL -eL "${command[@]}" 2>&1 | tee "$result_dir/stdout.log"
status=${PIPESTATUS[0]}
set -e
printf '%s\n' "$status" >"$result_dir/exit-status"
printf 'exit_status\t%s\n' "$status" >>"$result_dir/metadata.tsv"
du -sb "$work_dir" | awk '{print "work_bytes\t" $1}' >>"$result_dir/metadata.tsv"
cp /proc/meminfo "$result_dir/meminfo-after.txt"
(( status == 0 )) || exit "$status"

if (( keep_work == 0 )); then
    rm -rf -- "$work_dir"
    printf 'work_retained\tno\n' >>"$result_dir/metadata.tsv"
else
    printf 'work_retained\tyes\n' >>"$result_dir/metadata.tsv"
fi
printf 'GraphChi completed successfully; results: %s\n' "$result_dir"
