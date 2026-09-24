#!/usr/bin/env bash
# Build matching software-RDMA providers and tools without installing on L0.
set -euo pipefail
port_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
rdma_source="$port_root/build/hermit-rdma-core-src"
rdma_build="$port_root/build/hermit-rdma-core"
rdma_stage="$port_root/build/hermit-rdma-root"
rdma_commit=6697a72f6cfa2a5895cdf796d45744bbb778723c
if [[ ! -d "$rdma_source/.git" ]]; then
    git clone --depth 1 --branch v58.0 https://github.com/linux-rdma/rdma-core.git "$rdma_source"
fi
[[ $(git -C "$rdma_source" rev-parse HEAD) == "$rdma_commit" ]]
mkdir -p "$port_root/results"
cmake -S "$rdma_source" -B "$rdma_build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/hermit-rdma \
    -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_INSTALL_SYSCONFDIR=etc \
    -DCMAKE_INSTALL_RPATH=/opt/hermit-rdma/lib \
    -DNO_MAN_PAGES=1 -DNO_PYVERBS=1 -DENABLE_STATIC=0 \
    > "$port_root/results/hermit-rdma-configure.log" 2>&1
cmake --build "$rdma_build" -j "${JOBS:-8}" \
    > "$port_root/results/hermit-rdma-build.log" 2>&1
DESTDIR="$rdma_stage" cmake --install "$rdma_build" \
    > "$port_root/results/hermit-rdma-install.log" 2>&1
printf 'RDMA source: %s\nRuntime staging: %s\nBuild libraries: %s/lib\nBuild headers: %s/include\n' \
    "$rdma_commit" "$rdma_stage" "$rdma_build" "$rdma_build"
