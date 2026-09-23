#!/usr/bin/env bash
set -euo pipefail

# Run the OpenMP XSBench workload used by Chameleon Table 2.  The paper says
# "H-M large, custom grid; event mode; unionized grid" but does not publish the
# custom gridpoint count.  This script therefore defaults to the public H-M
# large grid (11,303 points/nuclide) and always records the selected value.

source_dir=${XSBENCH_SOURCE:-"$HOME/chameleon-benchmarks/xsbench/openmp-threading"}
output_base=${XSBENCH_OUTPUT_BASE:-"$HOME/chameleon-results/xsbench"}
threads=${XSBENCH_THREADS:-4}
gridpoints=${XSBENCH_GRIDPOINTS:-11303}
lookups=${XSBENCH_LOOKUPS:-17000000}
source_revision=${XSBENCH_REVISION:-unknown}
expected_checksum=
skip_build=0
allow_oversubscribe=0

usage() {
    cat <<'EOF'
Usage: run-xsbench.sh [options]

Options:
  --source DIR             XSBench openmp-threading source directory
  --output-base DIR        Parent directory for timestamped results
  --threads N              OpenMP threads (default: 4)
  --expected-checksum N    Explicit custom-grid reference checksum (keeps raw upstream status)
  --gridpoints N           Gridpoints per nuclide (default: 11303)
  --lookups N              Event-mode lookups (default: 17000000)
  --revision REV           Source revision recorded in metadata
  --skip-build             Reuse an existing XSBench binary
  --allow-oversubscribe    Run even if MemAvailable is below the estimate
  -h, --help               Show this help

The fixed workload choices are:
  simulation method: event
  H-M benchmark size: large
  grid search: unionized
  kernel: baseline (0)

The Chameleon paper does not publish its custom -g value.  Supply it with
--gridpoints when it becomes available; do not label the default run as an
exact paper reproduction.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

require_positive_integer() {
    local name=$1
    local value=$2
    case "$value" in
        ''|*[!0-9]*) die "$name must be a positive integer: $value" ;;
    esac
    (( value > 0 )) || die "$name must be greater than zero"
}

while (( $# > 0 )); do
    case "$1" in
        --source)
            (( $# >= 2 )) || die "$1 requires a value"
            source_dir=$2
            shift 2
            ;;
        --output-base)
            (( $# >= 2 )) || die "$1 requires a value"
            output_base=$2
            shift 2
            ;;
        --threads)
            (( $# >= 2 )) || die "$1 requires a value"
            threads=$2
            shift 2
            ;;
        --expected-checksum)
            (( $# >= 2 )) || die "$1 requires a value"
            expected_checksum=$2; shift 2 ;;
        --gridpoints)
            (( $# >= 2 )) || die "$1 requires a value"
            gridpoints=$2
            shift 2
            ;;
        --lookups)
            (( $# >= 2 )) || die "$1 requires a value"
            lookups=$2
            shift 2
            ;;
        --revision)
            (( $# >= 2 )) || die "$1 requires a value"
            source_revision=$2
            shift 2
            ;;
        --skip-build)
            skip_build=1
            shift
            ;;
        --allow-oversubscribe)
            allow_oversubscribe=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

if [[ -n "$expected_checksum" ]]; then
    [[ "$expected_checksum" =~ ^[0-9]+$ ]] || die "invalid expected checksum"
fi
require_positive_integer threads "$threads"
require_positive_integer gridpoints "$gridpoints"
require_positive_integer lookups "$lookups"
[[ -d "$source_dir" ]] || die "source directory not found: $source_dir"
[[ -f "$source_dir/Makefile" ]] || die "Makefile not found in: $source_dir"

# For H-M large with a unionized grid:
#   355 * 48 bytes                 nuclide grid
# + 355 * 8 bytes                  unionized energies
# + 355 * 355 * 4 bytes            unionized index grid
# per gridpoint.  Materials and allocator overhead are small by comparison.
bytes_per_gridpoint=$((355 * 48 + 355 * 8 + 355 * 355 * 4))
estimated_bytes=$((gridpoints * bytes_per_gridpoint))
estimated_mib=$(((estimated_bytes + 1048575) / 1048576))
available_kib=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
available_mib=$((available_kib / 1024))
headroom_mib=1024

if (( allow_oversubscribe == 0 && available_mib < estimated_mib + headroom_mib )); then
    die "estimated XSBench data is ${estimated_mib} MiB, but only ${available_mib} MiB is available; reduce --gridpoints or use a larger Guest"
fi

if (( skip_build == 0 )); then
    make -C "$source_dir" clean
    make -C "$source_dir" -j"$threads" CC="${CC:-gcc}"
fi

binary="$source_dir/XSBench"
[[ -x "$binary" ]] || die "XSBench binary not found after build: $binary"

run_id=$(date -u +%Y%m%dT%H%M%SZ)
result_dir="$output_base/$run_id"
mkdir -p "$result_dir"

command=(
    "$binary"
    -t "$threads"
    -m event
    -s large
    -g "$gridpoints"
    -G unionized
    -l "$lookups"
    -k 0
)

{
    printf 'field\tvalue\n'
    printf 'timestamp_utc\t%s\n' "$run_id"
    printf 'hostname\t%s\n' "$(hostname)"
    printf 'kernel\t%s\n' "$(uname -r)"
    printf 'source_revision\t%s\n' "$source_revision"
    printf 'source_dir\t%s\n' "$source_dir"
    printf 'threads\t%s\n' "$threads"
    printf 'simulation_method\tevent\n'
    printf 'hm_size\tlarge\n'
    printf 'grid_type\tunionized\n'
    printf 'gridpoints_per_nuclide\t%s\n' "$gridpoints"
    printf 'lookups\t%s\n' "$lookups"
    printf 'estimated_data_mib\t%s\n' "$estimated_mib"
    printf 'mem_available_before_mib\t%s\n' "$available_mib"
    printf 'paper_custom_grid_exact\t%s\n' "$([[ "$gridpoints" == 11303 ]] && printf no || printf unknown)"
    printf 'command\t'
    printf '%q ' "${command[@]}"
    printf '\n'
} >"$result_dir/metadata.tsv"

cp /proc/cpuinfo "$result_dir/cpuinfo.txt"
cp /proc/meminfo "$result_dir/meminfo-before.txt"

printf 'XSBench result directory: %s\n' "$result_dir"
printf 'Estimated data allocation: %s MiB; MemAvailable: %s MiB\n' \
    "$estimated_mib" "$available_mib"
printf 'Command:'
printf ' %q' "${command[@]}"
printf '\n'

set +e
OMP_NUM_THREADS="$threads" OMP_PROC_BIND=close OMP_PLACES=cores \
    /usr/bin/time -v -o "$result_dir/time.txt" \
    stdbuf -oL -eL "${command[@]}" 2>&1 | tee "$result_dir/stdout.log"
status=${PIPESTATUS[0]}
set -e

cp /proc/meminfo "$result_dir/meminfo-after.txt"
printf '%s\n' "$status" >"$result_dir/exit-status"
printf 'exit_status\t%s\n' "$status" >>"$result_dir/metadata.tsv"

if [[ -n "$expected_checksum" ]]; then
    observed=$(sed -n 's/^Verification checksum: \([0-9]*\).*/\1/p' "$result_dir/stdout.log")
    printf 'expected_checksum\t%s\nobserved_checksum\t%s\nupstream_exit_status\t%s\n' "$expected_checksum" "$observed" "$status" >>"$result_dir/metadata.tsv"
    if (( status == 0 || status == 1 )) && [[ "$observed" == "$expected_checksum" ]]; then
        printf 'Custom-grid reference checksum matched: %s\n' "$observed"
        status=0
    else
        printf 'Custom-grid reference mismatch or execution failure\n' >&2
        status=1
    fi
fi
printf '%s\n' "$status" >"$result_dir/validated-exit-status"
if (( status != 0 )); then
    printf 'XSBench failed with status %s; results kept in %s\n' "$status" "$result_dir" >&2
    exit "$status"
fi

printf 'XSBench completed successfully; results: %s\n' "$result_dir"
