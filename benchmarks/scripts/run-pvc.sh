#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
benchmarks_dir=$(cd -- "$script_dir/.." && pwd -P)

source_overlay=${PVC_SOURCE_OVERLAY:-"$benchmarks_dir/apps/pvc"}
metis_source=${PVC_METIS_SOURCE:-"$benchmarks_dir/apps/metis"}
input=${PVC_INPUT:-}
output_base=${PVC_OUTPUT_BASE:-"$HOME/chameleon-results/pvc"}
threads=${PVC_THREADS:-4}
map_tasks=${PVC_MAP_TASKS:-64}
reduce_tasks=${PVC_REDUCE_TASKS:-64}
group_tasks=${PVC_GROUP_TASKS:-64}
repetitions=${PVC_REPETITIONS:-5}
minimum_available_mib=${PVC_MIN_AVAILABLE_MIB:-1024}
skip_build=0
allow_oversubscribe=0

usage() {
    cat <<'EOF'
Usage: run-pvc.sh --input FILE [options]

Options:
  --input FILE              PVC binary input (required)
  --source-overlay DIR      PVC source/makefile directory
  --metis-source DIR        Metis source directory (only required for builds)
  --output-base DIR         Parent directory for timestamped results
  --output DIR              Alias for --output-base
  --threads N               Metis worker threads (default: 4)
  --map-tasks N             Map tasks in each stage (default: 64)
  --reduce-tasks N          Stage-1 reduce tasks (default: 64)
  --group-tasks N           Stage-2 group tasks (default: 64)
  --repetitions N           Timed repetitions (default: 5)
  --minimum-available-mib N Required free-memory headroom (default: 1024)
  --skip-build              Reuse build/page_view_count; no Metis tree needed
  --allow-oversubscribe     Ignore CPU/cpuset and minimum-memory preflight
  -h, --help                Show this help

The application runs quietly and records aggregate counts and checksums in its
stdout.  It does not write the complete URL/count table, so result-file I/O is
not part of the timed workload.  Each repetition has an independent directory
containing stdout.log, time.txt, command/log/combined exit statuses, and
before/after meminfo.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

positive_integer() {
    local name=$1
    local value=$2
    case "$value" in
        ''|*[!0-9]*) die "$name must be a positive integer: $value" ;;
    esac
    (( 10#$value > 0 )) || die "$name must be greater than zero"
}

cpu_list_count() {
    local list=$1
    local range first last
    local count=0
    local -a ranges

    IFS=',' read -r -a ranges <<<"$list"
    for range in "${ranges[@]}"; do
        if [[ "$range" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            first=${BASH_REMATCH[1]}
            last=${BASH_REMATCH[2]}
            (( last >= first )) || return 1
            count=$((count + last - first + 1))
        elif [[ "$range" =~ ^[0-9]+$ ]]; then
            count=$((count + 1))
        else
            return 1
        fi
    done
    printf '%s\n' "$count"
}

cpu_list_contains() {
    local list=$1
    local needle=$2
    local range first last
    local -a ranges

    IFS=',' read -r -a ranges <<<"$list"
    for range in "${ranges[@]}"; do
        if [[ "$range" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            first=${BASH_REMATCH[1]}
            last=${BASH_REMATCH[2]}
            if (( needle >= first && needle <= last )); then
                return 0
            fi
        elif [[ "$range" =~ ^[0-9]+$ ]] && (( needle == 10#$range )); then
            return 0
        fi
    done
    return 1
}

while (( $# > 0 )); do
    case "$1" in
        --input)
            (( $# >= 2 )) || die "$1 requires a value"
            input=$2
            shift 2
            ;;
        --source-overlay)
            (( $# >= 2 )) || die "$1 requires a value"
            source_overlay=$2
            shift 2
            ;;
        --metis-source)
            (( $# >= 2 )) || die "$1 requires a value"
            metis_source=$2
            shift 2
            ;;
        --output-base|--output)
            (( $# >= 2 )) || die "$1 requires a value"
            output_base=$2
            shift 2
            ;;
        --threads)
            (( $# >= 2 )) || die "$1 requires a value"
            threads=$2
            shift 2
            ;;
        --map-tasks)
            (( $# >= 2 )) || die "$1 requires a value"
            map_tasks=$2
            shift 2
            ;;
        --reduce-tasks)
            (( $# >= 2 )) || die "$1 requires a value"
            reduce_tasks=$2
            shift 2
            ;;
        --group-tasks)
            (( $# >= 2 )) || die "$1 requires a value"
            group_tasks=$2
            shift 2
            ;;
        --repetitions)
            (( $# >= 2 )) || die "$1 requires a value"
            repetitions=$2
            shift 2
            ;;
        --minimum-available-mib)
            (( $# >= 2 )) || die "$1 requires a value"
            minimum_available_mib=$2
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

[[ -n "$input" ]] || die "--input is required"
[[ -f "$input" && -r "$input" ]] || die "input is not a readable regular file: $input"
[[ -d "$source_overlay" ]] || die "source overlay directory not found: $source_overlay"
[[ -x /usr/bin/time ]] || die "/usr/bin/time is required"
command -v tee >/dev/null || die "tee is required"
if (( skip_build == 0 )); then
    [[ -r "$source_overlay/metis-pvc.mk" ]] || \
        die "overlay makefile not found: $source_overlay/metis-pvc.mk"
    [[ -d "$metis_source" && -r "$metis_source/lib/Makefrag" ]] || \
        die "invalid Metis source directory: $metis_source"
    command -v make >/dev/null || die "make is required"
fi

positive_integer threads "$threads"
positive_integer map-tasks "$map_tasks"
positive_integer reduce-tasks "$reduce_tasks"
positive_integer group-tasks "$group_tasks"
positive_integer repetitions "$repetitions"
positive_integer PVC_MIN_AVAILABLE_MIB "$minimum_available_mib"

# Normalize decimal strings before arithmetic so values such as 08 are not
# interpreted as invalid octal constants by Bash.
threads=$((10#$threads))
map_tasks=$((10#$map_tasks))
reduce_tasks=$((10#$reduce_tasks))
group_tasks=$((10#$group_tasks))
repetitions=$((10#$repetitions))
minimum_available_mib=$((10#$minimum_available_mib))

online_cpus=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc --all)
case "$online_cpus" in
    ''|*[!0-9]*) die "could not determine the number of online CPUs" ;;
esac
cpuset_list=$(awk '/^Cpus_allowed_list:/ {print $2; exit}' /proc/self/status)
[[ -n "$cpuset_list" ]] || die "could not read Cpus_allowed_list from /proc/self/status"
cpuset_cpus=$(cpu_list_count "$cpuset_list") || \
    die "could not parse Cpus_allowed_list: $cpuset_list"
effective_cpus=$online_cpus
(( cpuset_cpus < effective_cpus )) && effective_cpus=$cpuset_cpus

available_kib=$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)
case "$available_kib" in
    ''|*[!0-9]*) die "could not read MemAvailable from /proc/meminfo" ;;
esac
available_mib=$((available_kib / 1024))
cgroup_memory_remaining_mib=unlimited
if [[ -r /sys/fs/cgroup/memory.max && -r /sys/fs/cgroup/memory.current ]]; then
    read -r cgroup_memory_max </sys/fs/cgroup/memory.max
    read -r cgroup_memory_current </sys/fs/cgroup/memory.current
    if [[ "$cgroup_memory_max" =~ ^[0-9]+$ && "$cgroup_memory_current" =~ ^[0-9]+$ ]]; then
        if (( cgroup_memory_max > cgroup_memory_current )); then
            cgroup_memory_remaining_mib=$(((cgroup_memory_max - cgroup_memory_current) / 1048576))
        else
            cgroup_memory_remaining_mib=0
        fi
        (( cgroup_memory_remaining_mib < available_mib )) && \
            available_mib=$cgroup_memory_remaining_mib
    fi
fi

if (( allow_oversubscribe == 0 )); then
    (( threads <= effective_cpus )) || \
        die "threads ($threads) exceed the $effective_cpus CPUs allowed by online CPU/cpuset limits; reduce --threads or use --allow-oversubscribe"
    for ((cpu = 0; cpu < threads; cpu++)); do
        cpu_list_contains "$cpuset_list" "$cpu" || \
            die "Metis binds workers to CPUs 0-$((threads - 1)), but CPU $cpu is absent from Cpus_allowed_list ($cpuset_list); adjust the cpuset or use --allow-oversubscribe"
    done
    (( available_mib >= minimum_available_mib )) || \
        die "only ${available_mib} MiB is available; PVC requires at least ${minimum_available_mib} MiB of headroom (or use --allow-oversubscribe)"
fi

input=$(realpath -e -- "$input")
source_overlay=$(realpath -e -- "$source_overlay")
if (( skip_build == 0 )); then
    metis_source=$(realpath -e -- "$metis_source")
else
    metis_source=$(realpath -m -- "$metis_source")
fi
input_bytes=$(stat -Lc %s -- "$input")
input_mtime=$(stat -Lc %y -- "$input")
(( input_bytes > 0 )) || die "input is empty: $input"
input_sha256=$(sha256sum -- "$input" | awk '{print $1}')

binary="$source_overlay/build/page_view_count"
if (( skip_build != 0 )); then
    [[ -x "$binary" ]] || die "PVC binary not found for --skip-build: $binary"
fi

run_id=$(date -u +%Y%m%dT%H%M%SZ)
result_dir="$output_base/$run_id"
if [[ -e "$result_dir" ]]; then
    result_dir="$output_base/${run_id}-$$"
fi
mkdir -p -- "$result_dir"

build_command=(
    make -C "$source_overlay" -f metis-pvc.mk -j"$threads"
    "METIS_ROOT=$metis_source" page_view_count
)
command_template=(
    "$binary" "$input"
    -p "$threads"
    -m "$map_tasks"
    -r "$reduce_tasks"
    -g "$group_tasks"
    -q
)

metis_revision=unknown
if git -C "$metis_source" rev-parse --verify HEAD >/dev/null 2>&1; then
    metis_revision=$(git -C "$metis_source" rev-parse HEAD)
fi

{
    printf 'field\tvalue\n'
    printf 'timestamp_utc\t%s\n' "$run_id"
    printf 'hostname\t%s\n' "$(hostname)"
    printf 'kernel\t%s\n' "$(uname -r)"
    printf 'source_overlay\t%s\n' "$source_overlay"
    printf 'metis_source\t%s\n' "$metis_source"
    printf 'metis_revision\t%s\n' "$metis_revision"
    printf 'input\t%s\n' "$input"
    printf 'input_bytes\t%s\n' "$input_bytes"
    printf 'input_mtime\t%s\n' "$input_mtime"
    printf 'input_sha256\t%s\n' "$input_sha256"
    printf 'threads\t%s\n' "$threads"
    printf 'map_tasks\t%s\n' "$map_tasks"
    printf 'reduce_tasks\t%s\n' "$reduce_tasks"
    printf 'group_tasks\t%s\n' "$group_tasks"
    printf 'repetitions\t%s\n' "$repetitions"
    printf 'skip_build\t%s\n' "$skip_build"
    printf 'allow_oversubscribe\t%s\n' "$allow_oversubscribe"
    printf 'online_cpus\t%s\n' "$online_cpus"
    printf 'cpuset_allowed_list\t%s\n' "$cpuset_list"
    printf 'cpuset_allowed_cpus\t%s\n' "$cpuset_cpus"
    printf 'effective_cpus\t%s\n' "$effective_cpus"
    printf 'mem_available_before_mib\t%s\n' "$available_mib"
    printf 'cgroup_memory_remaining_mib\t%s\n' "$cgroup_memory_remaining_mib"
    printf 'minimum_available_mib\t%s\n' "$minimum_available_mib"
    printf 'full_counts_written\tno\n'
    printf 'build_command\t'
    printf '%q ' "${build_command[@]}"
    printf '\n'
    printf 'command_template\t'
    printf '%q ' "${command_template[@]}"
    printf '\n'
} >"$result_dir/metadata.tsv"

cp /proc/cpuinfo "$result_dir/cpuinfo.txt"
cp /proc/meminfo "$result_dir/meminfo-before.txt"

printf 'PVC result directory: %s\n' "$result_dir"
printf 'Input: %s bytes; threads: %s; tasks: map=%s reduce=%s group=%s; repetitions: %s\n' \
    "$input_bytes" "$threads" "$map_tasks" "$reduce_tasks" "$group_tasks" "$repetitions"

if (( skip_build == 0 )); then
    printf 'Build command:'
    printf ' %q' "${build_command[@]}"
    printf '\n'
    set +e
    "${build_command[@]}" 2>&1 | tee "$result_dir/build.log"
    pipeline_status=("${PIPESTATUS[@]}")
    set -e
    build_status=${pipeline_status[0]}
    build_log_status=${pipeline_status[1]}
    printf '%s\n' "$build_status" >"$result_dir/build-exit-status"
    printf 'build_exit_status\t%s\n' "$build_status" >>"$result_dir/metadata.tsv"
    printf 'build_log_exit_status\t%s\n' "$build_log_status" >>"$result_dir/metadata.tsv"
    if (( build_status != 0 || build_log_status != 0 )); then
        cp /proc/meminfo "$result_dir/meminfo-after.txt"
        if (( build_status != 0 )); then
            failure_status=$build_status
            failure_component=build
        else
            failure_status=$build_log_status
            failure_component=build-log
        fi
        printf 'PVC %s failed with status %s; results kept in %s\n' \
            "$failure_component" "$failure_status" "$result_dir" >&2
        exit "$failure_status"
    fi
else
    printf 'build_exit_status\tskipped\n' >>"$result_dir/metadata.tsv"
    printf 'build_log_exit_status\tskipped\n' >>"$result_dir/metadata.tsv"
fi

[[ -x "$binary" ]] || die "PVC binary not found after build: $binary"
binary_sha256=$(sha256sum "$binary" | awk '{print $1}')
printf 'binary_sha256\t%s\n' "$binary_sha256" >>"$result_dir/metadata.tsv"
printf 'repetition\texit_status\n' >"$result_dir/exit-status.tsv"

for ((repetition = 1; repetition <= repetitions; repetition++)); do
    printf -v repetition_name 'repeat-%03d' "$repetition"
    repetition_dir="$result_dir/$repetition_name"
    mkdir -p -- "$repetition_dir"
    cp /proc/meminfo "$repetition_dir/meminfo-before.txt"

    command=("${command_template[@]}")
    {
        printf 'command\t'
        printf '%q ' "${command[@]}"
        printf '\n'
    } >"$repetition_dir/command.tsv"

    printf 'PVC repetition %s/%s command:' "$repetition" "$repetitions"
    printf ' %q' "${command[@]}"
    printf '\n'

    set +e
    /usr/bin/time -v -o "$repetition_dir/time.txt" \
        "${command[@]}" 2>&1 | tee "$repetition_dir/stdout.log"
    pipeline_status=("${PIPESTATUS[@]}")
    set -e
    command_status=${pipeline_status[0]}
    log_status=${pipeline_status[1]}
    status=$command_status
    (( status == 0 && log_status != 0 )) && status=$log_status

    cp /proc/meminfo "$repetition_dir/meminfo-after.txt"
    printf '%s\n' "$status" >"$repetition_dir/exit-status"
    printf '%s\n' "$command_status" >"$repetition_dir/command-exit-status"
    printf '%s\n' "$log_status" >"$repetition_dir/log-exit-status"
    printf '%s\t%s\n' "$repetition" "$status" >>"$result_dir/exit-status.tsv"
    printf 'repetition_%03d_exit_status\t%s\n' "$repetition" "$status" \
        >>"$result_dir/metadata.tsv"
    printf 'repetition_%03d_command_exit_status\t%s\n' "$repetition" \
        "$command_status" >>"$result_dir/metadata.tsv"
    printf 'repetition_%03d_log_exit_status\t%s\n' "$repetition" \
        "$log_status" >>"$result_dir/metadata.tsv"

    if (( status != 0 )); then
        cp /proc/meminfo "$result_dir/meminfo-after.txt"
        printf 'PVC repetition %s failed with status %s; results kept in %s\n' \
            "$repetition" "$status" "$result_dir" >&2
        exit "$status"
    fi
done

cp /proc/meminfo "$result_dir/meminfo-after.txt"
printf 'PVC completed successfully; results: %s\n' "$result_dir"
