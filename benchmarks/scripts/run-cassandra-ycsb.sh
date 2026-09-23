#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
guest_name=${CASSANDRA_GUEST_NAME:-guest-tools-final}
guest_dir=${CASSANDRA_GUEST_DIR:-"$repo_root/hyperalloc-6.18/build/guests/$guest_name"}
qemu_pid_file=${CASSANDRA_QEMU_PID_FILE:-"$repo_root/hyperalloc-6.18/build/running/$guest_name/qemu.pid"}
guest_user=${CASSANDRA_GUEST_USER:-ubuntu}
guest_host=${CASSANDRA_GUEST_HOST:-127.0.0.1}
guest_ssh_port=${CASSANDRA_GUEST_SSH_PORT:-5123}
guest_control=${CASSANDRA_GUEST_CONTROL:-/home/ubuntu/chameleon-benchmarks/scripts/cassandra-guest.sh}
ycsb_home=${YCSB_HOME:-"$repo_root/benchmarks/apps/ycsb-0.17.0"}
java_home=${JAVA_HOME:-/usr/lib/jvm/java-8-openjdk-amd64}
output_base=${CASSANDRA_OUTPUT_BASE:-"$repo_root/benchmarks/results/cassandra"}
guest_numa_node=${CASSANDRA_GUEST_NUMA_NODE:-0}
client_numa_node=${CASSANDRA_CLIENT_NUMA_NODE:-1}
client_cpu_list=${CASSANDRA_CLIENT_CPUS:-}
local_cql_port=${CASSANDRA_LOCAL_CQL_PORT:-19042}
records=${CASSANDRA_RECORDS:-100000}
operations=${CASSANDRA_OPERATIONS:-100000}
threads=${CASSANDRA_YCSB_THREADS:-16}
connections=${CASSANDRA_YCSB_CONNECTIONS:-8}
workload=${CASSANDRA_YCSB_WORKLOAD:-workloada}
read_proportion=${CASSANDRA_YCSB_READ_PROPORTION:-1.0}
update_proportion=${CASSANDRA_YCSB_UPDATE_PROPORTION:-0}
request_distribution=${CASSANDRA_YCSB_REQUEST_DISTRIBUTION:-zipfian}
load_target=0
load_retry_limit=${CASSANDRA_LOAD_RETRY_LIMIT:-10}
load_retry_interval=${CASSANDRA_LOAD_RETRY_INTERVAL:-1}
keep_schema=1

usage() {
    cat <<'EOF'
Usage: run-cassandra-ycsb.sh [options]

Options:
  --guest-name NAME          guestctl/QEMU guest name
  --guest-numa-node N        NUMA node containing all QEMU threads (default: 0)
  --client-numa-node N       NUMA node for SSH tunnel and YCSB (default: 1)
  --client-cpus LIST         Explicit client CPU subset within that NUMA node
  --local-cql-port PORT      Host port forwarded to Guest CQL (default: 19042)
  --ycsb-home DIR            Extracted YCSB distribution on Host
  --java-home DIR            Host Java 8 directory
  --output-base DIR          Parent directory for timestamped Host results
  --records N                Records loaded (default: 100000)
  --operations N             Timed operations (default: 100000)
  --load-target N            Loading ops/s (0 = unlimited; default: 0)
  --load-retry-limit N       Retry each failed INSERT (default: 10; 0 disables)
  --load-retry-interval N    Seconds between INSERT retries (default: 1)
  --threads N                YCSB threads (default: 16)
  --connections N            Cassandra driver core/max connections (default: 8)
  --workload NAME            YCSB workload filename (default: workloada)
  --read-proportion N        Read fraction (default: 1.0, matching cass_run)
  --update-proportion N      Update fraction (default: 0, matching cass_run)
  --request-distribution D   YCSB request distribution (default: zipfian)
  --drop-schema-after        Drop the test keyspace after a successful run
  -h, --help                 Show this help

The runner refuses to start unless every QEMU thread is confined to the Guest
NUMA node and the YCSB node is different. Both the SSH tunnel and YCSB JVM are
launched with --cpunodebind and --membind on the client NUMA node.
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

nonnegative_integer() {
    case "$2" in ''|*[!0-9]*) die "$1 must be a non-negative integer: $2" ;; esac
}

while (( $# > 0 )); do
    case "$1" in
        --guest-name) (( $# >= 2 )) || die "$1 requires a value"; guest_name=$2; shift 2 ;;
        --guest-numa-node) (( $# >= 2 )) || die "$1 requires a value"; guest_numa_node=$2; shift 2 ;;
        --client-numa-node) (( $# >= 2 )) || die "$1 requires a value"; client_numa_node=$2; shift 2 ;;
        --client-cpus) (( $# >= 2 )) || die "$1 requires a value"; client_cpu_list=$2; shift 2 ;;
        --local-cql-port) (( $# >= 2 )) || die "$1 requires a value"; local_cql_port=$2; shift 2 ;;
        --ycsb-home) (( $# >= 2 )) || die "$1 requires a value"; ycsb_home=$2; shift 2 ;;
        --java-home) (( $# >= 2 )) || die "$1 requires a value"; java_home=$2; shift 2 ;;
        --output-base) (( $# >= 2 )) || die "$1 requires a value"; output_base=$2; shift 2 ;;
        --records) (( $# >= 2 )) || die "$1 requires a value"; records=$2; shift 2 ;;
        --operations) (( $# >= 2 )) || die "$1 requires a value"; operations=$2; shift 2 ;;
        --load-target) (( $# >= 2 )) || die "$1 requires a value"; load_target=$2; shift 2 ;;
        --load-retry-limit) (( $# >= 2 )) || die "$1 requires a value"; load_retry_limit=$2; shift 2 ;;
        --load-retry-interval) (( $# >= 2 )) || die "$1 requires a value"; load_retry_interval=$2; shift 2 ;;
        --threads) (( $# >= 2 )) || die "$1 requires a value"; threads=$2; shift 2 ;;
        --connections) (( $# >= 2 )) || die "$1 requires a value"; connections=$2; shift 2 ;;
        --workload) (( $# >= 2 )) || die "$1 requires a value"; workload=$2; shift 2 ;;
        --read-proportion) (( $# >= 2 )) || die "$1 requires a value"; read_proportion=$2; shift 2 ;;
        --update-proportion) (( $# >= 2 )) || die "$1 requires a value"; update_proportion=$2; shift 2 ;;
        --request-distribution) (( $# >= 2 )) || die "$1 requires a value"; request_distribution=$2; shift 2 ;;
        --drop-schema-after) keep_schema=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

nonnegative_integer guest-numa-node "$guest_numa_node"
nonnegative_integer client-numa-node "$client_numa_node"
positive_integer local-cql-port "$local_cql_port"
positive_integer records "$records"
positive_integer operations "$operations"
nonnegative_integer load-target "$load_target"
nonnegative_integer load-retry-limit "$load_retry_limit"
nonnegative_integer load-retry-interval "$load_retry_interval"
positive_integer threads "$threads"
positive_integer connections "$connections"
awk -v read="$read_proportion" -v update="$update_proportion" 'BEGIN {
    valid = read ~ /^[0-9]+([.][0-9]+)?$/ && update ~ /^[0-9]+([.][0-9]+)?$/
    total = read + update
    exit !(valid && read >= 0 && read <= 1 && update >= 0 && update <= 1 &&
           total > 0.999999 && total < 1.000001)
}' || die "read-proportion and update-proportion must be fractions whose sum is 1"
[[ "$request_distribution" =~ ^[A-Za-z0-9_-]+$ ]] || die "invalid request distribution: $request_distribution"
(( local_cql_port <= 65535 )) || die "local CQL port exceeds 65535"
(( guest_numa_node != client_numa_node )) || die "Guest and YCSB NUMA nodes must differ"
[[ -x "$ycsb_home/bin/ycsb.sh" ]] || die "YCSB launcher not found: $ycsb_home/bin/ycsb.sh"
[[ -r "$ycsb_home/workloads/$workload" ]] || die "YCSB workload not found: $workload"
[[ -x "$java_home/bin/java" ]] || die "Java executable not found under: $java_home"
[[ -r "$guest_dir/id_ed25519" && -r "$guest_dir/known_hosts" ]] || die "Guest SSH access files missing: $guest_dir"
[[ -r "$qemu_pid_file" ]] || die "QEMU PID file missing: $qemu_pid_file"
[[ -r "/sys/devices/system/node/node${guest_numa_node}/cpulist" ]] || die "Guest NUMA node does not exist"
[[ -r "/sys/devices/system/node/node${client_numa_node}/cpulist" ]] || die "client NUMA node does not exist"
command -v numactl >/dev/null || die "numactl is required"

qemu_pid=$(cat "$qemu_pid_file")
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

if ss -lnt | awk '{print $4}' | grep -Eq "(^|:)${local_cql_port}$"; then
    die "local CQL port is already in use: $local_cql_port"
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

remote() {
    ssh "${ssh_options[@]}" "$ssh_target" "$@"
}

tunnel_pid=
cleanup() {
    if [[ -n "$tunnel_pid" ]] && kill -0 "$tunnel_pid" 2>/dev/null; then
        kill -TERM "$tunnel_pid" 2>/dev/null || true
        wait "$tunnel_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

remote "$guest_control" status >"$result_dir/cassandra-status-before.txt"
remote "$guest_control" reset-schema >"$result_dir/schema.txt"

numactl "${client_binding[@]}" \
    ssh "${ssh_options[@]}" -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
        -N -L "127.0.0.1:${local_cql_port}:127.0.0.1:9042" "$ssh_target" &
tunnel_pid=$!
deadline=$((SECONDS + 30))
while (( SECONDS < deadline )); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$local_cql_port") 2>/dev/null; then
        exec 3>&-
        break
    fi
    kill -0 "$tunnel_pid" 2>/dev/null || die "SSH CQL tunnel exited during startup"
    sleep 1
done
if ! (exec 3<>"/dev/tcp/127.0.0.1/$local_cql_port") 2>/dev/null; then
    die "SSH CQL tunnel did not become ready"
fi
exec 3>&-

{
    printf 'tunnel_pid=%s\n' "$tunnel_pid"
    awk '/^Cpus_allowed_list:|^Mems_allowed_list:/ {print}' "/proc/$tunnel_pid/status"
} >"$result_dir/tunnel-affinity.txt"
sed -n '1,12p' "/proc/$tunnel_pid/numa_maps" >"$result_dir/tunnel-numa-maps.txt"
numactl --hardware >"$result_dir/numa-hardware.txt"
numastat -p "$qemu_pid" >"$result_dir/qemu-numastat.txt"
for task_status in /proc/"$qemu_pid"/task/*/status; do
    if ! awk -v tid="${task_status%/status}" \
        '/^Name:|^Cpus_allowed_list:|^Mems_allowed_list:/ {print tid " " $0}' \
        "$task_status" 2>/dev/null; then
        [[ ! -e "$task_status" ]] || exit 1
    fi
done >"$result_dir/qemu-thread-affinity.txt"
remote 'cat /proc/meminfo' >"$result_dir/guest-meminfo-before.txt"

ycsb_common=(
    -P "$ycsb_home/workloads/$workload"
    -p "hosts=127.0.0.1"
    -p "port=$local_cql_port"
    -p "recordcount=$records"
    -p "operationcount=$operations"
    -p "cassandra.maxconnections=$connections"
    -p "cassandra.coreconnections=$connections"
    -p "readproportion=$read_proportion"
    -p "updateproportion=$update_proportion"
    -p "requestdistribution=$request_distribution"
    -threads "$threads"
    -s
)

run_phase() {
    local phase=$1
    local time_file="$result_dir/${phase}-time.txt"
    local affinity_file="$result_dir/${phase}-affinity.txt"
    local stdout_file="$result_dir/${phase}.log"
    local stderr_file="$result_dir/${phase}.err.log"
    local mode=$phase
    [[ "$phase" == load ]] || mode=run

    local rate_options=()
    if [[ "$phase" == load && "$load_target" != 0 ]]; then rate_options=(-target "$load_target"); fi
    if [[ "$phase" == load ]]; then
        rate_options+=(-p "core_workload_insertion_retry_limit=$load_retry_limit"
                       -p "core_workload_insertion_retry_interval=$load_retry_interval")
    fi
    local client_monitor=()
    if [[ -n "$client_cpu_list" ]]; then
        client_monitor=(python3 "$script_dir/chameleon_affinity.py" --client-cpus "$client_cpu_list"
            --node "$client_numa_node" --also-pid "$tunnel_pid"
            --evidence "$result_dir/${phase}-affinity.json" --run-command --)
    fi
    set +e
    JAVA_HOME="$java_home" "${client_monitor[@]}" /usr/bin/time -v -o "$time_file" \
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
        ' _ "$affinity_file" "$ycsb_home/bin/ycsb.sh" "$mode" cassandra-cql \
            "${ycsb_common[@]}" "${rate_options[@]}" >"$stdout_file" 2>"$stderr_file"
    local status=$?
    set -e
    printf '%s\n' "$status" >"$result_dir/${phase}-exit-status"
    (( status == 0 )) || die "YCSB $phase failed with status $status"
    grep -q '^\[OVERALL\], Throughput(ops/sec),' "$stdout_file" || \
        die "YCSB $phase output lacks throughput"
    local expected=$operations
    [[ "$phase" != load ]] || expected=$records
    python3 "$script_dir/check-ycsb-result.py" "$stdout_file" --phase "$phase" \
        --expected "$expected" >"$result_dir/${phase}-validation.json" || \
        die "YCSB $phase contains failed or incomplete operations; see ${phase}-validation.json"
}

{
    printf 'field\tvalue\n'
    printf 'timestamp_utc\t%s\n' "$run_id"
    printf 'hostname\t%s\n' "$(hostname)"
    printf 'host_kernel\t%s\n' "$(uname -r)"
    printf 'guest_name\t%s\n' "$guest_name"
    printf 'guest_kernel\t%s\n' "$(remote uname -r)"
    printf 'cassandra_version\t5.0.1\n'
    printf 'ycsb_version\t0.17.0\n'
    printf 'workload\t%s\n' "$workload"
    printf 'workload_sha256\t%s\n' "$(sha256sum "$ycsb_home/workloads/$workload" | awk '{print $1}')"
    printf 'read_proportion\t%s\n' "$read_proportion"
    printf 'update_proportion\t%s\n' "$update_proportion"
    printf 'request_distribution\t%s\n' "$request_distribution"
    printf 'records\t%s\n' "$records"
    printf 'operations\t%s\n' "$operations"
    printf 'threads\t%s\n' "$threads"
    printf 'load_target\t%s\n' "$load_target"
    printf 'load_retry_limit\t%s\n' "$load_retry_limit"
    printf 'load_retry_interval_seconds\t%s\n' "$load_retry_interval"
    printf 'connections\t%s\n' "$connections"
    printf 'guest_numa_node\t%s\n' "$guest_numa_node"
    printf 'guest_numa_cpus\t%s\n' "$guest_node_cpus"
    printf 'client_numa_node\t%s\n' "$client_numa_node"
    printf 'client_numa_cpus\t%s\n' "$client_node_cpus"
    printf 'client_allocated_cpus\t%s\n' "${client_cpu_list:-$client_node_cpus}"
    printf 'qemu_pid\t%s\n' "$qemu_pid"
    printf 'local_cql_port\t%s\n' "$local_cql_port"
    printf 'paper_scale\tno\n'
} >"$result_dir/metadata.tsv"

printf 'Cassandra/YCSB result directory: %s\n' "$result_dir"
printf 'Guest QEMU: within NUMA %s (node CPUs %s; actual thread masks in qemu-thread-affinity.txt); Host YCSB: NUMA %s CPUs %s\n' \
    "$guest_numa_node" "$guest_node_cpus" "$client_numa_node" "$client_node_cpus"
printf 'Loading %s records with %s threads...\n' "$records" "$threads"
run_phase load
if [[ -n "${CHAMELEON_BARRIER_DIR:-}" ]]; then
    python3 "$script_dir/chameleon_barrier.py" --directory "$CHAMELEON_BARRIER_DIR" \
        --member "${CHAMELEON_BARRIER_MEMBER:?barrier member missing}" \
        --timeout "${CHAMELEON_BARRIER_TIMEOUT:-1800}" --case cassandra --vm "$guest_name" \
        --phase after_load_before_read >"$result_dir/start-barrier.json"
fi
printf 'Running %s operations (%s; read=%s, update=%s)...\n' \
    "$operations" "$request_distribution" "$read_proportion" "$update_proportion"
printf 'timed_phase_start_unix_seconds\t%s\n' "$(date +%s.%N)" >>"$result_dir/metadata.tsv"
run_phase run
printf 'timed_phase_end_unix_seconds\t%s\n' "$(date +%s.%N)" >>"$result_dir/metadata.tsv"

remote "$guest_control" status >"$result_dir/cassandra-status-after.txt"
remote 'cat /proc/meminfo' >"$result_dir/guest-meminfo-after.txt"
remote 'tail -n 300 "$HOME/chameleon-cassandra/logs/system.log" 2>/dev/null || tail -n 300 "$HOME/chameleon-cassandra/logs/console.log"' \
    >"$result_dir/cassandra-log-tail.txt"

if (( keep_schema == 0 )); then
    remote "$guest_control" drop-schema
    printf 'schema_retained\tno\n' >>"$result_dir/metadata.tsv"
else
    printf 'schema_retained\tyes\n' >>"$result_dir/metadata.tsv"
fi
printf 'load_exit_status\t%s\n' "$(cat "$result_dir/load-exit-status")" >>"$result_dir/metadata.tsv"
printf 'run_exit_status\t%s\n' "$(cat "$result_dir/run-exit-status")" >>"$result_dir/metadata.tsv"
printf 'Cassandra/YCSB completed successfully; results: %s\n' "$result_dir"
