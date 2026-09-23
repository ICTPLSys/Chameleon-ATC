#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$root/build/qemu" "$root/results"
cd "$root/build/qemu"
"$root/qemu/configure" \
  --target-list=x86_64-softmmu --cc=clang --cxx=clang++ \
  --without-default-features --enable-kvm --enable-tcg \
  --enable-pixman --enable-llfree --enable-debug \
  --disable-slirp --disable-docs --disable-gtk --disable-sdl \
  --disable-opengl --disable-vnc --disable-guest-agent \
  --disable-rdma --disable-pvrdma \
  --enable-trace-backends=simple "$@" 2>&1 | tee "$root/results/qemu-configure.log"
ninja -j "${JOBS:-16}" qemu-system-x86_64 2>&1 | tee "$root/results/qemu-build.log"
