#!/usr/bin/env bash
set -euo pipefail
port_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
port_cc=${CC:-clang}
port_jobs=${JOBS:-24}
mkdir -p "$port_root/build/guest" "$port_root/results"
cp "$port_root/configs/guest.config" "$port_root/build/guest/.config"
"$port_root/linux/scripts/kconfig/merge_config.sh" -m -O "$port_root/build/guest" \
    "$port_root/build/guest/.config" "$port_root/configs/hermit-rdma-common.fragment" \
    > "$port_root/results/guest-rdma-configure.log" 2>&1
make -C "$port_root/linux" O="$port_root/build/guest" CC="$port_cc" LOCALVERSION= olddefconfig \
    > "$port_root/results/guest-configure.log" 2>&1
make -C "$port_root/linux" O="$port_root/build/guest" CC="$port_cc" LOCALVERSION= -j"$port_jobs" bzImage \
    > "$port_root/results/guest-build.log" 2>&1
make -C "$port_root/linux" O="$port_root/build/guest" CC="$port_cc" LOCALVERSION= -j"$port_jobs" modules \
    > "$port_root/results/guest-modules-build.log" 2>&1
make -C "$port_root/linux" O="$port_root/build/guest" CC="$port_cc" LOCALVERSION= \
    M="$port_root/tests" modules > "$port_root/results/guest-test-module-build.log" 2>&1
printf 'Guest image: %s\nTest module: %s\n' \
    "$port_root/build/guest/arch/x86/boot/bzImage" "$port_root/tests/guest_allocator.ko"
