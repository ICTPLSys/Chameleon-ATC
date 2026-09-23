#!/usr/bin/env bash
# Use native host providers, not the software-RDMA test staging libraries.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
mkdir -p "$root/hyperalloc-6.18/build/fig9"
cc -O2 -g -std=gnu11 -Wall -Wextra -Werror \
  "$root/hyperalloc-6.18/hermit/server/rswap_server.c" \
  -o "$root/hyperalloc-6.18/build/fig9/rswap-server" -lrdmacm -libverbs
printf 'Built %s\n' "$root/hyperalloc-6.18/build/fig9/rswap-server"
