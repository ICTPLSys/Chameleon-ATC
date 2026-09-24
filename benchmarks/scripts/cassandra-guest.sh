#!/usr/bin/env bash
set -euo pipefail

cassandra_home=${CASSANDRA_HOME:-"$HOME/chameleon-benchmarks/apache-cassandra-5.0.1"}
state_dir=${CASSANDRA_STATE_DIR:-"$HOME/chameleon-cassandra"}
heap_mib=${CASSANDRA_HEAP_MIB:-8192}
wait_seconds=${CASSANDRA_WAIT_SECONDS:-180}
command_name=${1:-}

usage() {
    cat <<'EOF'
Usage: cassandra-guest.sh <prepare|start|status|is-running|reset-schema|drop-schema|stop> [options]

Options:
  --cassandra-home DIR   Extracted Cassandra directory
  --state-dir DIR        Configuration, PID and log directory
  --heap-mib N           Cassandra JVM heap in MiB (default: 8192)
  --wait-seconds N       Startup/shutdown timeout (default: 180)
  -h, --help             Show this help

This script runs inside the Guest. Cassandra listens only on Guest localhost;
the Host benchmark reaches CQL through an SSH local-forward.
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

[[ -n "$command_name" ]] || { usage >&2; exit 2; }
shift
while (( $# > 0 )); do
    case "$1" in
        --cassandra-home) (( $# >= 2 )) || die "$1 requires a value"; cassandra_home=$2; shift 2 ;;
        --state-dir) (( $# >= 2 )) || die "$1 requires a value"; state_dir=$2; shift 2 ;;
        --heap-mib) (( $# >= 2 )) || die "$1 requires a value"; heap_mib=$2; shift 2 ;;
        --wait-seconds) (( $# >= 2 )) || die "$1 requires a value"; wait_seconds=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

positive_integer heap-mib "$heap_mib"
positive_integer wait-seconds "$wait_seconds"
[[ -x "$cassandra_home/bin/cassandra" ]] || die "Cassandra executable not found: $cassandra_home/bin/cassandra"
[[ -x "$cassandra_home/bin/cqlsh" ]] || die "cqlsh not found: $cassandra_home/bin/cqlsh"

conf_dir="$state_dir/conf"
log_dir="$state_dir/logs"
pid_file="$state_dir/cassandra.pid"
console_log="$log_dir/console.log"

prepare() {
    mkdir -p "$state_dir" "$log_dir"
    if [[ ! -f "$conf_dir/cassandra.yaml" ]]; then
        mkdir -p "$conf_dir"
        cp -a "$cassandra_home/conf/." "$conf_dir/"
        sed -i \
            -e "s/^cluster_name:.*/cluster_name: 'Chameleon YCSB'/" \
            -e 's/^listen_address:.*/listen_address: 127.0.0.1/' \
            -e 's/^rpc_address:.*/rpc_address: 127.0.0.1/' \
            -e 's/^endpoint_snitch:.*/endpoint_snitch: SimpleSnitch/' \
            "$conf_dir/cassandra.yaml"
    fi
}

pid_is_running() {
    [[ -s "$pid_file" ]] || return 1
    local pid
    pid=$(cat "$pid_file")
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    [[ -r "/proc/$pid/cmdline" ]] || return 1
    # A PID file can survive reboot and point to an unrelated process.
    tr '\0' '\n' <"/proc/$pid/cmdline" | grep -Fxq 'org.apache.cassandra.service.CassandraDaemon'
}

cql() {
    CASSANDRA_CONF="$conf_dir" CQLSH_PYTHON=python3 \
        "$cassandra_home/bin/cqlsh" --request-timeout=60 127.0.0.1 9042 "$@"
}

wait_ready() {
    local deadline=$((SECONDS + wait_seconds))
    while (( SECONDS < deadline )); do
        if pid_is_running && cql -e 'SELECT release_version FROM system.local;' >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    tail -n 80 "$console_log" >&2 || true
    die "Cassandra did not become ready within ${wait_seconds}s"
}

case "$command_name" in
    is-running)
        pid_is_running
        ;;
    prepare)
        prepare
        printf 'Cassandra Guest state prepared at %s\n' "$state_dir"
        ;;
    start)
        prepare
        if pid_is_running; then
            printf 'Cassandra already running with PID %s\n' "$(cat "$pid_file")"
            wait_ready
            exit 0
        fi
        available_mib=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
        (( available_mib >= heap_mib + 2048 )) || \
            die "heap plus 2048 MiB headroom exceeds MemAvailable (${available_mib} MiB)"
        : >"$console_log"
        nohup env \
            CASSANDRA_HOME="$cassandra_home" \
            CASSANDRA_CONF="$conf_dir" \
            CASSANDRA_LOG_DIR="$log_dir" \
            MAX_HEAP_SIZE="${heap_mib}M" \
            HEAP_NEWSIZE="$((heap_mib / 4))M" \
            "$cassandra_home/bin/cassandra" -f \
            >>"$console_log" 2>&1 </dev/null &
        printf '%s\n' "$!" >"$pid_file"
        wait_ready
        printf 'Cassandra ready with PID %s on 127.0.0.1:9042\n' "$(cat "$pid_file")"
        ;;
    status)
        prepare
        pid_is_running || die "Cassandra is not running"
        wait_ready
        pid=$(cat "$pid_file")
        printf 'pid=%s\n' "$pid"
        ps -o pid,ppid,psr,etimes,rss,vsz,cmd -p "$pid"
        CASSANDRA_CONF="$conf_dir" "$cassandra_home/bin/nodetool" status
        cql -e 'SELECT cluster_name, release_version, rpc_address FROM system.local;'
        ;;
    reset-schema)
        prepare
        wait_ready
        cql -e "DROP KEYSPACE IF EXISTS ycsb; CREATE KEYSPACE ycsb WITH replication = {'class':'SimpleStrategy','replication_factor':1}; CREATE TABLE ycsb.usertable (y_id varchar PRIMARY KEY, field0 varchar, field1 varchar, field2 varchar, field3 varchar, field4 varchar, field5 varchar, field6 varchar, field7 varchar, field8 varchar, field9 varchar);"
        cql -e "DESCRIBE KEYSPACE ycsb;"
        ;;
    drop-schema)
        prepare
        wait_ready
        cql -e 'DROP KEYSPACE IF EXISTS ycsb;'
        ;;
    stop)
        if ! pid_is_running; then
            rm -f -- "$pid_file"
            printf 'Cassandra is not running\n'
            exit 0
        fi
        pid=$(cat "$pid_file")
        kill -TERM "$pid"
        deadline=$((SECONDS + wait_seconds))
        while pid_is_running && (( SECONDS < deadline )); do
            sleep 2
        done
        pid_is_running && die "Cassandra PID $pid did not stop"
        rm -f -- "$pid_file"
        printf 'Cassandra stopped\n'
        ;;
    *)
        die "unknown command: $command_name"
        ;;
esac
