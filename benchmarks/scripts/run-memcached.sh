#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/../.." && pwd -P)
guest_name=${MEMCACHED_GUEST_NAME:-guest-tools-final}
guest_dir=${MEMCACHED_GUEST_DIR:-"$repo_root/hyperalloc-6.18/build/guests/$guest_name"}
qemu_run_dir=${MEMCACHED_QEMU_RUN_DIR:-"$repo_root/hyperalloc-6.18/build/running/$guest_name"}
guest_user=${MEMCACHED_GUEST_USER:-ubuntu}
guest_host=${MEMCACHED_GUEST_HOST:-127.0.0.1}
guest_ssh_port=${MEMCACHED_GUEST_SSH_PORT:-5123}
guest_control=${MEMCACHED_GUEST_CONTROL:-/home/ubuntu/chameleon-benchmarks/scripts/memcached-guest.sh}
generator=${MEMCACHED_GENERATOR:-"$repo_root/benchmarks/memcached-trace-generator/build-release/memcached-trace-generator"}
trace=${MEMCACHED_TRACE:-"$repo_root/benchmarks/dataset/cachelib-kvcache-202206"}
output_base=${MEMCACHED_OUTPUT_BASE:-"$repo_root/benchmarks/results/memcached-guest"}
guest_numa_node=${MEMCACHED_GUEST_NUMA_NODE:-0}
client_numa_node=${MEMCACHED_CLIENT_NUMA_NODE:-1}
client_cpu_list=${MEMCACHED_CLIENT_CPUS:-}
host_port=${MEMCACHED_HOST_PORT:-11211}
guest_port=${MEMCACHED_GUEST_PORT:-11211}
memory_mib=${MEMCACHED_MEMORY_MIB:-8192}
minimum_available_mib=${MEMCACHED_MINIMUM_AVAILABLE_MIB:-2048}
server_threads=${MEMCACHED_SERVER_THREADS:-4}
mpps=${MEMCACHED_MPPS:-0.01}
runtime=${MEMCACHED_RUNTIME:-30}
rampup=${MEMCACHED_RAMPUP:-2}
workers=${MEMCACHED_WORKERS:-8}
rx_threads=${MEMCACHED_RX_THREADS:-2}
producer_shards=${MEMCACHED_PRODUCER_SHARDS:-2}
value_size=${MEMCACHED_VALUE_SIZE:-4096}
warmup_keys=
guest_cpus=

usage() {
    cat <<'EOF'
Usage: run-memcached.sh [options]

Options:
  --guest-name NAME       QEMU/guestctl Guest name
  --guest-numa-node N     NUMA node containing every QEMU thread (default: 0)
  --guest-cpus LIST       Explicit Guest application CPU affinity
  --client-numa-node N    NUMA node used by the Host generator (default: 1)
  --client-cpus LIST      Explicit generator CPU subset within that NUMA node
  --generator FILE        Host memcached-trace-generator binary
  --trace PATH            CacheLib trace file or directory
  --output-base DIR       Parent directory for timestamped results
  --host-port N           Temporary Host UDP forwarding port (default: 11211)
  --guest-port N          Guest Memcached UDP port (default: 11211)
  --memory-mib N          Guest Memcached item-cache limit (default: 8192)
  --minimum-available-mib N  Guest startup available-memory check (default: 2048)
  --server-threads N      Guest Memcached worker threads (default: 4)
  --mpps N                Offered load in million ops/s (default: 0.01)
  --runtime N             Steady-state seconds (default: 30)
  --rampup N              Ramp seconds (default: 2)
  --workers N             Generator TX workers (default: 8)
  --rx-threads N          Generator RX threads (default: 2)
  --producer-shards N     Generator producer shards (default: 2)
  --warmup-keys FILE      Guest trace-key file to prefill over Guest-local TCP
  --value-size N          SET value bytes (default: 4096)
  -h, --help              Show this help

The runner refuses same-node placement or a QEMU thread outside the declared
Guest node. It allocates distinct generator logical CPUs from the client node,
binds generator memory there, and creates a temporary QEMU UDP host forward.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

positive_integer() {
    case "$2" in ''|*[!0-9]*) die "$1 must be a positive integer: $2" ;; esac
    (( 10#$2 > 0 )) || die "$1 must be greater than zero"
}

nonnegative_integer() {
    case "$2" in ''|*[!0-9]*) die "$1 must be a non-negative integer: $2" ;; esac
}

while (( $# > 0 )); do
    case "$1" in
        --guest-name) (( $# >= 2 )) || die "$1 requires a value"; guest_name=$2; shift 2 ;;
        --guest-numa-node) (( $# >= 2 )) || die "$1 requires a value"; guest_numa_node=$2; shift 2 ;;
        --guest-cpus) (( $# >= 2 )) || die "$1 requires a value"; guest_cpus=$2; shift 2 ;;
        --client-numa-node) (( $# >= 2 )) || die "$1 requires a value"; client_numa_node=$2; shift 2 ;;
        --client-cpus) (( $# >= 2 )) || die "$1 requires a value"; client_cpu_list=$2; shift 2 ;;
        --generator) (( $# >= 2 )) || die "$1 requires a value"; generator=$2; shift 2 ;;
        --trace) (( $# >= 2 )) || die "$1 requires a value"; trace=$2; shift 2 ;;
        --output-base) (( $# >= 2 )) || die "$1 requires a value"; output_base=$2; shift 2 ;;
        --host-port) (( $# >= 2 )) || die "$1 requires a value"; host_port=$2; shift 2 ;;
        --guest-port) (( $# >= 2 )) || die "$1 requires a value"; guest_port=$2; shift 2 ;;
        --memory-mib) (( $# >= 2 )) || die "$1 requires a value"; memory_mib=$2; shift 2 ;;
        --minimum-available-mib) (( $# >= 2 )) || die "$1 requires a value"; minimum_available_mib=$2; shift 2 ;;
        --server-threads) (( $# >= 2 )) || die "$1 requires a value"; server_threads=$2; shift 2 ;;
        --mpps) (( $# >= 2 )) || die "$1 requires a value"; mpps=$2; shift 2 ;;
        --runtime) (( $# >= 2 )) || die "$1 requires a value"; runtime=$2; shift 2 ;;
        --rampup) (( $# >= 2 )) || die "$1 requires a value"; rampup=$2; shift 2 ;;
        --workers) (( $# >= 2 )) || die "$1 requires a value"; workers=$2; shift 2 ;;
        --rx-threads) (( $# >= 2 )) || die "$1 requires a value"; rx_threads=$2; shift 2 ;;
        --producer-shards) (( $# >= 2 )) || die "$1 requires a value"; producer_shards=$2; shift 2 ;;
        --warmup-keys) (( $# >= 2 )) || die "$1 requires a value"; warmup_keys=$2; shift 2 ;;
        --value-size) (( $# >= 2 )) || die "$1 requires a value"; value_size=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

nonnegative_integer guest-numa-node "$guest_numa_node"
nonnegative_integer client-numa-node "$client_numa_node"
positive_integer host-port "$host_port"
positive_integer guest-port "$guest_port"
positive_integer memory-mib "$memory_mib"
positive_integer minimum-available-mib "$minimum_available_mib"
positive_integer server-threads "$server_threads"
positive_integer runtime "$runtime"
nonnegative_integer rampup "$rampup"
positive_integer workers "$workers"
nonnegative_integer rx-threads "$rx_threads"
positive_integer producer-shards "$producer_shards"
positive_integer value-size "$value_size"
[[ "$mpps" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "mpps must be a positive decimal"
awk -v value="$mpps" 'BEGIN { exit !(value > 0) }' || die "mpps must be greater than zero"
(( host_port <= 65535 && guest_port <= 65535 )) || die "port exceeds 65535"
(( guest_numa_node != client_numa_node )) || die "Guest and generator NUMA nodes must differ"
(( rx_threads <= workers )) || die "rx-threads must not exceed workers"
(( producer_shards <= workers )) || die "producer-shards must not exceed workers"
[[ -x "$generator" ]] || die "generator is not executable: $generator"
[[ -e "$trace" ]] || die "trace path does not exist: $trace"
[[ -r "$guest_dir/id_ed25519" && -r "$guest_dir/known_hosts" ]] || die "Guest SSH files missing: $guest_dir"
[[ -r "$qemu_run_dir/qemu.pid" && -S "$qemu_run_dir/qmp.sock" ]] || die "QEMU runtime files missing: $qemu_run_dir"
[[ -r "/sys/devices/system/node/node${guest_numa_node}/cpulist" ]] || die "Guest NUMA node does not exist"
[[ -r "/sys/devices/system/node/node${client_numa_node}/cpulist" ]] || die "client NUMA node does not exist"
command -v numactl >/dev/null || die "numactl is required"

qemu_pid=$(cat "$qemu_run_dir/qemu.pid")
[[ "$qemu_pid" =~ ^[0-9]+$ && -d "/proc/$qemu_pid/task" ]] || die "QEMU PID is not running: $qemu_pid"
guest_node_cpus=$(cat "/sys/devices/system/node/node${guest_numa_node}/cpulist")
client_node_cpus=$(cat "/sys/devices/system/node/node${client_numa_node}/cpulist")
client_binding=(--cpunodebind="$client_numa_node" --membind="$client_numa_node")
if [[ -n "$client_cpu_list" ]]; then
    client_cpu_list=$(python3 "$script_dir/chameleon_affinity.py" --client-cpus "$client_cpu_list" --node "$client_numa_node") || die "invalid client CPU allocation"
    client_binding=(--physcpubind="$client_cpu_list" --membind="$client_numa_node")
fi
python3 "$script_dir/chameleon_affinity.py" --pid "$qemu_pid" --node "$guest_numa_node" || \
    die "QEMU thread affinity is outside the declared Guest NUMA node"

expand_cpu_list() {
    local list=$1 part first last cpu
    local -a parts
    IFS=',' read -r -a parts <<<"$list"
    for part in "${parts[@]}"; do
        if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            first=${BASH_REMATCH[1]}; last=${BASH_REMATCH[2]}
            for ((cpu=first; cpu<=last; cpu++)); do printf '%s\n' "$cpu"; done
        elif [[ "$part" =~ ^[0-9]+$ ]]; then
            printf '%s\n' "$part"
        else
            return 1
        fi
    done
}

mapfile -t client_cpus < <(expand_cpu_list "${client_cpu_list:-$client_node_cpus}")
needed_cpus=$((workers + rx_threads + producer_shards))
(( needed_cpus <= ${#client_cpus[@]} )) || \
    die "generator roles require $needed_cpus CPUs, client node has ${#client_cpus[@]}"
join_cpus() { local IFS=,; printf '%s' "$*"; }
worker_cpu_list=$(join_cpus "${client_cpus[@]:0:workers}")
rx_cpu_list=
(( rx_threads == 0 )) || rx_cpu_list=$(join_cpus "${client_cpus[@]:workers:rx_threads}")
producer_cpu_list=$(join_cpus "${client_cpus[@]:workers+rx_threads:producer_shards}")

if ss -H -lun | awk '{print $4}' | grep -Eq "(^|:)${host_port}$"; then
    die "Host UDP port is already in use: $host_port"
fi

run_id=$(date -u +%Y%m%dT%H%M%SZ)
result_dir="$output_base/$run_id"
mkdir -p "$result_dir"
ssh_target="$guest_user@$guest_host"
ssh_options=(
    -i "$guest_dir/id_ed25519"
    -p "$guest_ssh_port"
    -o "UserKnownHostsFile=$guest_dir/known_hosts"
    -o StrictHostKeyChecking=yes
    -o BatchMode=yes
    -o ConnectTimeout=10
)
remote() { ssh "${ssh_options[@]}" "$ssh_target" "$@"; }

qmp_hmp() {
    local command=$1
    python3 - "$repo_root/hyperalloc-6.18/scripts/guestctl.py" \
        "$guest_dir/access.json" "$command" <<'PY'
import importlib.util
from pathlib import Path
import sys

module_path = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("chameleon_guestctl", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
access = module.load_access(Path(sys.argv[2]))
print(module.qmp(access, "human-monitor-command", {"command-line": sys.argv[3]}), end="")
PY
}

forward_added=0
cleanup() {
    if (( forward_added != 0 )); then
        qmp_hmp "hostfwd_remove mgmt udp:127.0.0.1:${host_port}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

remote "$guest_control" stop >"$result_dir/server-stop-before.txt"
guest_affinity=()
if [[ -n "$guest_cpus" ]]; then
    [[ "$guest_cpus" =~ ^[0-9]+([,-][0-9]+)*$ ]] || die "invalid Guest CPU list"
    guest_affinity=(taskset -c "$guest_cpus")
fi
tcp_options=()
[[ -z "$warmup_keys" ]] || tcp_options=(--tcp-port 11212)
remote "${guest_affinity[@]}" "$guest_control" start "${tcp_options[@]}" --memory-mib "$memory_mib" --threads "$server_threads" \
    --port "$guest_port" --minimum-available-mib "$minimum_available_mib" >"$result_dir/server-start.txt"
if [[ -n "$warmup_keys" ]]; then
    # No TCP Host forwarding is installed. Only the local Guest warmup uses it.
    remote "${guest_affinity[@]}" python3 /home/"$guest_user"/chameleon-benchmarks/scripts/memcached-warmup.py \
        --keys "$warmup_keys" --value-size "$value_size" >"$result_dir/warmup.jsonl"
fi
remote "$guest_control" status >"$result_dir/server-status-before.txt"
remote 'cat /proc/meminfo' >"$result_dir/guest-meminfo-before.txt"
remote 'cat /proc/net/snmp' >"$result_dir/guest-net-snmp-before.txt"

qmp_hmp "info usernet" >"$result_dir/qmp-usernet-before.txt"
qmp_hmp "hostfwd_add mgmt udp:127.0.0.1:${host_port}-:${guest_port}"
forward_added=1
qmp_hmp "info usernet" >"$result_dir/qmp-usernet-active.txt"
ss -lunmp >"$result_dir/host-udp-sockets-before.txt"
cp /proc/net/snmp "$result_dir/host-net-snmp-before.txt"
cp /proc/net/softnet_stat "$result_dir/host-softnet-before.txt"
numactl --hardware >"$result_dir/numa-hardware.txt"
numastat -p "$qemu_pid" >"$result_dir/qemu-numastat.txt"
for task_status in /proc/"$qemu_pid"/task/*/status; do
    if ! awk -v tid="${task_status%/status}" \
        '/^Name:|^Cpus_allowed_list:|^Mems_allowed_list:/ {print tid " " $0}' \
        "$task_status" 2>/dev/null; then
        [[ ! -e "$task_status" ]] || exit 1
    fi
done >"$result_dir/qemu-thread-affinity.txt"

generator_args=(
    --server "127.0.0.1:$host_port"
    --trace "$trace"
    --start-mpps 0
    --mpps "$mpps"
    --samples 1
    --runtime "$runtime"
    --rampup "$rampup"
    --workers "$workers"
    --rx-threads "$rx_threads"
    --producer-shards "$producer_shards"
    --worker-cpus "$worker_cpu_list"
    --producer-cpus "$producer_cpu_list"
    --amp-factor 1
    --value-size "$value_size"
    --request-timeout-ms 200
    --drain-ms 1000
    --max-send-lag-us 100
    --max-inflight 32768
    --queue-depth 4096
    --rx-queue-depth 1024
    --schedule-ahead-ms 20
    --batch-size 32
    --socket-buffer-mb 16
    --seed 1
)
if (( rx_threads > 0 )); then
    generator_args+=(--rx-cpus "$rx_cpu_list")
fi

{
    printf 'field\tvalue\n'
    printf 'timestamp_utc\t%s\n' "$run_id"
    printf 'hostname\t%s\n' "$(hostname)"
    printf 'host_kernel\t%s\n' "$(uname -r)"
    printf 'guest_name\t%s\n' "$guest_name"
    printf 'guest_kernel\t%s\n' "$(remote uname -r)"
    printf 'network_path\tqemu_user_udp_hostfwd\n'
    printf 'generator_sha256\t%s\n' "$(sha256sum "$generator" | awk '{print $1}')"
    printf 'trace\t%s\n' "$(realpath -e "$trace")"
    printf 'guest_numa_node\t%s\n' "$guest_numa_node"
    printf 'guest_application_cpus\t%s\n' "${guest_cpus:-inherited}"
    printf 'guest_numa_cpus\t%s\n' "$guest_node_cpus"
    printf 'client_numa_node\t%s\n' "$client_numa_node"
    printf 'client_numa_cpus\t%s\n' "$client_node_cpus"
    printf 'client_allocated_cpus\t%s\n' "${client_cpu_list:-$client_node_cpus}"
    printf 'worker_cpus\t%s\n' "$worker_cpu_list"
    printf 'rx_cpus\t%s\n' "$rx_cpu_list"
    printf 'producer_cpus\t%s\n' "$producer_cpu_list"
    printf 'qemu_pid\t%s\n' "$qemu_pid"
    printf 'host_port\t%s\n' "$host_port"
    printf 'guest_port\t%s\n' "$guest_port"
    printf 'minimum_available_mib\t%s\n' "$minimum_available_mib"
    printf 'memory_mib\t%s\n' "$memory_mib"
    printf 'server_threads\t%s\n' "$server_threads"
    printf 'mpps\t%s\n' "$mpps"
    printf 'runtime\t%s\n' "$runtime"
    printf 'rampup\t%s\n' "$rampup"
    printf 'workers\t%s\n' "$workers"
    printf 'rx_threads\t%s\n' "$rx_threads"
    printf 'producer_shards\t%s\n' "$producer_shards"
    printf 'value_size\t%s\n' "$value_size"
    printf 'paper_scale\tno\n'
    printf 'generator_command\t'
    printf '%q ' "$generator" "${generator_args[@]}"
    printf '\n'
} >"$result_dir/metadata.tsv"

printf 'Memcached result directory: %s\n' "$result_dir"
printf 'Guest QEMU: within NUMA %s (node CPUs %s; actual thread masks in qemu-thread-affinity.txt); Host generator: NUMA %s CPUs %s\n' \
    "$guest_numa_node" "$guest_node_cpus" "$client_numa_node" "$client_node_cpus"
printf 'Generator roles: workers=%s rx=%s producers=%s; target=%s Mops/s\n' \
    "$worker_cpu_list" "$rx_cpu_list" "$producer_cpu_list" "$mpps"

if [[ -n "${CHAMELEON_BARRIER_DIR:-}" ]]; then
    python3 "$script_dir/chameleon_barrier.py" --directory "$CHAMELEON_BARRIER_DIR" \
        --member "${CHAMELEON_BARRIER_MEMBER:?barrier member missing}" \
        --timeout "${CHAMELEON_BARRIER_TIMEOUT:-1800}" --case memcached --vm "$guest_name" \
        --phase after_warmup_before_generator >"$result_dir/start-barrier.json"
fi
printf 'timed_phase_start_unix_seconds\t%s\n' "$(date +%s.%N)" >>"$result_dir/metadata.tsv"
client_monitor=()
if [[ -n "$client_cpu_list" ]]; then
    client_monitor=(python3 "$script_dir/chameleon_affinity.py" --client-cpus "$client_cpu_list"
        --node "$client_numa_node" --evidence "$result_dir/generator-affinity.json" --run-command --)
fi
set +e
"${client_monitor[@]}" /usr/bin/time -v -o "$result_dir/generator-time.txt" \
    numactl "${client_binding[@]}" \
    bash -c '
        affinity_file=$1
        shift
        {
            printf "pid=%s\n" "$$"
            awk "/^Cpus_allowed_list:|^Mems_allowed_list:/ {print}" /proc/self/status
            numactl --show
        } >"$affinity_file" 2>&1
        exec "$@"
    ' _ "$result_dir/generator-affinity.txt" "$generator" "${generator_args[@]}" \
    >"$result_dir/generator.csv" 2>"$result_dir/generator.stderr"
generator_status=$?
set -e
printf 'timed_phase_end_unix_seconds\t%s\n' "$(date +%s.%N)" >>"$result_dir/metadata.tsv"
printf '%s\n' "$generator_status" >"$result_dir/generator-exit-status"

cp /proc/net/snmp "$result_dir/host-net-snmp-after.txt"
cp /proc/net/softnet_stat "$result_dir/host-softnet-after.txt"
# Capture the forwarding socket while it still exists. skmem includes the
# receive-buffer limit and per-socket drop counter; SNMP is Host-wide only.
ss -uanmp >"$result_dir/host-udp-sockets-after-active.txt"
remote 'cat /proc/net/snmp' >"$result_dir/guest-net-snmp-after.txt"
remote 'cat /proc/meminfo' >"$result_dir/guest-meminfo-after.txt"
remote "$guest_control" status >"$result_dir/server-status-after.txt"
remote 'tail -n 300 "$HOME/chameleon-memcached/server.log"' >"$result_dir/server.log"

qmp_hmp "hostfwd_remove mgmt udp:127.0.0.1:${host_port}"
forward_added=0
qmp_hmp "info usernet" >"$result_dir/qmp-usernet-after.txt"
ss -lunmp >"$result_dir/host-udp-sockets-after.txt"

printf 'generator_exit_status\t%s\n' "$generator_status" >>"$result_dir/metadata.tsv"
(( generator_status == 0 )) || die "generator failed with status $generator_status"
(( $(wc -l <"$result_dir/generator.csv") >= 2 )) || die "generator produced no result row"
awk -F, '
    NR == 1 {
        for (i = 1; i <= NF; i++) {
            if ($i == "load_valid") valid_column = i
            if ($i == "schedule_complete") schedule_column = i
        }
        next
    }
    {
        rows++
        if (!valid_column || !schedule_column ||
            $valid_column != "1" || $schedule_column != "1") bad = 1
    }
    END { exit !(rows > 0 && !bad) }
' "$result_dir/generator.csv" || die "generator marked the load invalid or incomplete"
printf 'Memcached generator completed successfully; results: %s\n' "$result_dir"
