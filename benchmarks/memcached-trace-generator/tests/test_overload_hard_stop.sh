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

port=$((45000 + ($$ % 10000)))
user_args=()
if [[ $(id -u) -eq 0 ]]; then
  user_args=(-u root)
fi
"${memcached_bin}" -p 0 -U "${port}" -B binary -m 32 -t 1 \
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
# 50 Mops/s is intentionally far beyond one producer shard. The process must
# stop at the one-second phase wall instead of replaying overdue arrivals.
timeout 5s "${generator}" \
  --server "127.0.0.1:${port}" \
  --trace "${trace}" \
  --mpps 50 \
  --samples 1 \
  --runtime 1 \
  --rampup 0 \
  --workers 1 \
  --producer-shards 1 \
  --amp-factor 1 \
  --value-size 64 \
  --request-timeout-ms 20 \
  --drain-ms 50 \
  --max-send-lag-us 5 \
  --max-inflight 64 \
  --queue-depth 64 \
  --schedule-ahead-ms 5 \
  --batch-size 16 \
  --socket-buffer-mb 1 \
  --seed 19 >"${output}"

awk -F, '
  NR == 1 {
    for (i = 1; i <= NF; ++i) column[$i] = i
    next
  }
  NR == 2 {
    if ($(column["load_valid"]) != 0) exit 10
    if ($(column["schedule_complete"]) != 0) exit 11
    if ($(column["producer_wall_seconds"]) > 1.25) exit 12
    if ($(column["sample_wall_seconds"]) > 1.35) exit 13
    if (index($(column["invalid_reason"]), "schedule_incomplete") == 0) exit 14
    found = 1
  }
  END { if (!found) exit 15 }
' "${output}"
