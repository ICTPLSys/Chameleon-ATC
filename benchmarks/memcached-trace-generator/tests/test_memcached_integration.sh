#!/usr/bin/env bash
set -euo pipefail

generator=$1
memcached_bin=$2
trace=$3

tmp_dir=$(mktemp -d)
server_pid=
cleanup() {
  if [[ -n "${server_pid}" ]]; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

# Derive a high unprivileged port, then let the readiness probe catch the
# unlikely collision. CI executes this test serially by default.
port=$((30000 + ($$ % 20000)))
user_args=()
if [[ $(id -u) -eq 0 ]]; then
  user_args=(-u root)
fi
"${memcached_bin}" -p 0 -U "${port}" -B binary -m 32 -t 2 \
  -l 127.0.0.1 "${user_args[@]}" >"${tmp_dir}/memcached.log" 2>&1 &
server_pid=$!

ready=0
for _ in $(seq 1 50); do
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    cat "${tmp_dir}/memcached.log" >&2
    exit 1
  fi
  if ! command -v ss >/dev/null 2>&1; then
    sleep 0.1
    ready=1
    break
  fi
  if ss -H -lun "sport = :${port}" | grep -q ":${port}"; then
    ready=1
    break
  fi
  sleep 0.02
done
if [[ ${ready} -ne 1 ]]; then
  echo "memcached UDP port ${port} did not become ready" >&2
  cat "${tmp_dir}/memcached.log" >&2
  exit 1
fi

output="${tmp_dir}/result.csv"
"${generator}" \
  --server "127.0.0.1:${port}" \
  --trace "${trace}" \
  --mpps 0.001 \
  --samples 1 \
  --runtime 1 \
  --rampup 0 \
  --workers 2 \
  --rx-threads 1 \
  --rx-queue-depth 128 \
  --amp-factor 1 \
  --value-size 4096 \
  --request-timeout-ms 100 \
  --drain-ms 300 \
  --max-send-lag-us 500 \
  --max-inflight 256 \
  --queue-depth 256 \
  --schedule-ahead-ms 10 \
  --batch-size 16 \
  --socket-buffer-mb 1 \
  --seed 7 >"${output}"

awk -F, '
  NR == 1 {
    for (i = 1; i <= NF; ++i) column[$i] = i
    next
  }
  NR == 2 {
    if ($(column["sent"]) <= 0) exit 10
    if ($(column["completed"]) <= 0) exit 11
    if ($(column["set_success"]) <= 0) exit 12
    if ($(column["get_hit"]) <= 0) exit 13
    if ($(column["get_miss"]) <= 0) exit 14
    if ($(column["multi_fragment_response"]) <= 0) exit 15
    if ($(column["latency_samples"]) != $(column["completed"])) exit 16
    if ($(column["rx_handoff_drops_total_sample"]) != 0) exit 18
    if ($(column["maximum_rx_queue_depth_total_sample"]) <= 0) exit 19
    found = 1
  }
  END { if (!found) exit 17 }
' "${output}"
