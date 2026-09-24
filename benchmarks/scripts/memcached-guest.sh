#!/usr/bin/env bash
set -euo pipefail

source_dir=${MEMCACHED_SOURCE:-"$HOME/chameleon-benchmarks/apps/memcached"}
state_dir=${MEMCACHED_STATE_DIR:-"$HOME/chameleon-memcached"}
memory_mib=${MEMCACHED_MEMORY_MIB:-8192}
minimum_available_mib=${MEMCACHED_MINIMUM_AVAILABLE_MIB:-2048}
threads=${MEMCACHED_THREADS:-4}
port=${MEMCACHED_PORT:-11211}
tcp_port=0
listen_address=${MEMCACHED_LISTEN_ADDRESS:-0.0.0.0}
jobs=${MEMCACHED_BUILD_JOBS:-4}
wait_seconds=${MEMCACHED_WAIT_SECONDS:-30}
command_name=${1:-}

usage() {
    cat <<'EOF'
Usage: memcached-guest.sh <build|start|status|is-running|stop> [options]

Options:
  --source-dir DIR      Memcached source directory
  --state-dir DIR       Install, PID and log directory
  --memory-mib N        Memcached item-cache limit (default: 8192)
  --minimum-available-mib N  Startup available-memory check (default: 2048)
  --threads N           Memcached worker threads (default: 4)
  --port N              UDP port (default: 11211)
  --tcp-port N          Optional Guest TCP warmup port; disabled by default
  --listen ADDRESS      Listen address (default: 0.0.0.0)
  --jobs N              Parallel build jobs (default: 4)
  --wait-seconds N      Startup/shutdown timeout (default: 30)
  -h, --help            Show this help

The server accepts the binary protocol over UDP. It remains in the Guest;
the Host runner adds a temporary QEMU user-network UDP forwarding rule.
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

[[ -n "$command_name" ]] || { usage >&2; exit 2; }
if [[ "$command_name" == -h || "$command_name" == --help ]]; then
    usage
    exit 0
fi
shift
while (( $# > 0 )); do
    case "$1" in
        --source-dir) (( $# >= 2 )) || die "$1 requires a value"; source_dir=$2; shift 2 ;;
        --state-dir) (( $# >= 2 )) || die "$1 requires a value"; state_dir=$2; shift 2 ;;
        --memory-mib) (( $# >= 2 )) || die "$1 requires a value"; memory_mib=$2; shift 2 ;;
        --minimum-available-mib) (( $# >= 2 )) || die "$1 requires a value"; minimum_available_mib=$2; shift 2 ;;
        --threads) (( $# >= 2 )) || die "$1 requires a value"; threads=$2; shift 2 ;;
        --tcp-port) (( $# >= 2 )) || die "$1 requires a value"; tcp_port=$2; shift 2 ;;
        --port) (( $# >= 2 )) || die "$1 requires a value"; port=$2; shift 2 ;;
        --listen) (( $# >= 2 )) || die "$1 requires a value"; listen_address=$2; shift 2 ;;
        --jobs) (( $# >= 2 )) || die "$1 requires a value"; jobs=$2; shift 2 ;;
        --wait-seconds) (( $# >= 2 )) || die "$1 requires a value"; wait_seconds=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ "$tcp_port" =~ ^[0-9]+$ ]] && (( tcp_port <= 65535 )) || die "invalid TCP port"
positive_integer memory-mib "$memory_mib"
positive_integer minimum-available-mib "$minimum_available_mib"
positive_integer threads "$threads"
positive_integer port "$port"
positive_integer jobs "$jobs"
positive_integer wait-seconds "$wait_seconds"
(( port <= 65535 )) || die "port exceeds 65535"
[[ "$listen_address" != *[$'\n\r']* && -n "$listen_address" ]] || die "invalid listen address"

install_dir="$state_dir/install"
binary="$install_dir/bin/memcached"
pid_file="$state_dir/memcached.pid"
server_log="$state_dir/server.log"

pid_is_running() {
    [[ -s "$pid_file" ]] || return 1
    local pid executable
    pid=$(cat "$pid_file")
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    executable=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
    [[ "$executable" == "$(readlink -f "$binary" 2>/dev/null || true)" ]]
}

build_memcached() {
    [[ -d "$source_dir" && -x "$source_dir/autogen.sh" ]] || \
        die "Memcached source tree not found: $source_dir"
    command -v autoreconf >/dev/null || die "autoreconf is required"
    [[ -r /usr/include/event2/event.h ]] || die "libevent development headers are required"
    mkdir -p "$state_dir"
    if [[ ! -x "$source_dir/configure" ]]; then
        (cd "$source_dir" && ./autogen.sh)
    fi
    if [[ ! -f "$source_dir/Makefile" ]]; then
        (cd "$source_dir" && ./configure --prefix="$install_dir")
    fi
    make -C "$source_dir" -j"$jobs"
    make -C "$source_dir" install
    [[ -x "$binary" ]] || die "Memcached binary was not installed: $binary"
    "$binary" -h 2>&1 | sed -n '1p'
}

wait_ready() {
    local deadline=$((SECONDS + wait_seconds))
    while (( SECONDS < deadline )); do
        if pid_is_running && ss -H -lun "sport = :$port" | grep -q .; then
            return 0
        fi
        sleep 1
    done
    tail -n 80 "$server_log" >&2 2>/dev/null || true
    die "Memcached did not become ready within ${wait_seconds}s"
}

case "$command_name" in
    is-running)
        pid_is_running
        ;;
    build)
        build_memcached
        ;;
    start)
        [[ -x "$binary" ]] || build_memcached
        if pid_is_running; then
            printf 'Memcached already running with PID %s\n' "$(cat "$pid_file")"
            wait_ready
            exit 0
        fi
        available_mib=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
        (( available_mib >= minimum_available_mib )) || die "less than $minimum_available_mib MiB is available"
        mkdir -p "$state_dir"
        : >"$server_log"
        nohup "$binary" -p "$tcp_port" -U "$port" -B binary \
            -m "$memory_mib" -t "$threads" -l "$listen_address" \
            >>"$server_log" 2>&1 </dev/null &
        printf '%s\n' "$!" >"$pid_file"
        wait_ready
        printf 'Memcached ready with PID %s on %s:%s/udp\n' \
            "$(cat "$pid_file")" "$listen_address" "$port"
        ;;
    status)
        [[ -x "$binary" ]] || die "Memcached binary not found: $binary"
        pid_is_running || die "Memcached is not running"
        wait_ready
        pid=$(cat "$pid_file")
        "$binary" -h 2>&1 | sed -n '1p'
        ps -o pid,ppid,psr,etimes,rss,vsz,cmd -p "$pid"
        ss -lunp "sport = :$port"
        ;;
    stop)
        if ! pid_is_running; then
            rm -f -- "$pid_file"
            printf 'Memcached is not running\n'
            exit 0
        fi
        pid=$(cat "$pid_file")
        kill -TERM "$pid"
        deadline=$((SECONDS + wait_seconds))
        while pid_is_running && (( SECONDS < deadline )); do
            sleep 1
        done
        pid_is_running && die "Memcached PID $pid did not stop"
        rm -f -- "$pid_file"
        printf 'Memcached stopped\n'
        ;;
    *)
        die "unknown command: $command_name"
        ;;
esac
