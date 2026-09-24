#!/usr/bin/env bash
# Build the loadable client and a server with matching upstream RDMA providers.
set -euo pipefail
port_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
port_cc=${CC:-clang}
server_only=false
if [[ ${1:-} == --server-only && $# == 1 ]]; then
    server_only=true
elif [[ $# != 0 ]]; then
    echo 'Usage: build-hermit.sh [--server-only]' >&2
    exit 2
fi
if ! $server_only; then
[[ -f "$port_root/build/guest/Module.symvers" ]] || {
    echo 'Build the Guest kernel and modules with scripts/build-guest.sh first.' >&2
    exit 1
}
mkdir -p "$port_root/results"
make -C "$port_root/hermit/client" KDIR="$port_root/build/guest" CC="$port_cc" \
    > "$port_root/results/hermit-module-build.log" 2>&1
fi
"$port_root/scripts/build-hermit-rdma-tools.sh"
rdma_build="$port_root/build/hermit-rdma-core"
# A clean server compile also picks up changed compiler/library flags.
make -B -C "$port_root/hermit/server" CC="${HOSTCC:-gcc}" \
    CFLAGS="-O2 -g -std=gnu11 -Wall -Wextra -Werror -I$rdma_build/include" \
    LDLIBS="-L$rdma_build/lib -Wl,-rpath,$rdma_build/lib -lrdmacm -libverbs" \
    > "$port_root/results/hermit-server-build.log" 2>&1
if ! $server_only; then
    printf 'Client: %s\n' "$port_root/hermit/client/rswap-client.ko"
fi
printf 'Server: %s\n' "$port_root/hermit/server/rswap-server"
