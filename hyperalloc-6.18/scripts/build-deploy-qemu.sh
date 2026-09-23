#!/usr/bin/env bash
# Disk-backed VMs; slirp provides SSH without a bridge or libvirt.
# Requires libslirp-dev and libnuma-dev. RDMA runs in the guest, not QEMU.
set -euo pipefail
port_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$port_root/build/deploy-qemu" "$port_root/results/deployment"
cd "$port_root/build/deploy-qemu"
"$port_root/qemu/configure" \
  --target-list=x86_64-softmmu --cc=clang --cxx=clang++ \
  --without-default-features --enable-kvm --enable-tcg \
  --enable-pixman --enable-llfree --enable-debug \
  --enable-slirp --enable-numa --disable-docs --disable-gtk --disable-sdl \
  --disable-opengl --disable-vnc --disable-guest-agent \
  --disable-rdma --disable-pvrdma \
  --enable-trace-backends=simple "$@" \
  2>&1 | tee "$port_root/results/deployment/qemu-configure.log"
ninja -j "${JOBS:-16}" qemu-system-x86_64 \
  2>&1 | tee "$port_root/results/deployment/qemu-build.log"
