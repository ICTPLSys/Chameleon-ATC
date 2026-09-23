#!/usr/bin/env bash
set -euo pipefail

port_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
core="$port_root/llfree-c"
results="$port_root/results"
jobs=${JOBS:-8}
mkdir -p "$results"

make -C "$core" -j"$jobs" test >"$results/llfree-patched.log" 2>&1
make -C "$core" -j"$jobs" BUILDDIR=build/optimized DEBUG=0 test \
    >"$results/llfree-optimized.log" 2>&1
make -C "$core" -j"$jobs" BUILDDIR=build/prefer-installed \
    CC='clang -DLLFREE_PREFER_INSTALLED=true' test \
    >"$results/llfree-prefer-installed.log" 2>&1
make -C "$core" -j"$jobs" BUILDDIR=build/sanitize \
    CC='clang -fsanitize=address,undefined -fno-omit-frame-pointer' \
    LDFLAGS='-fsanitize=address,undefined' test A=hyperalloc \
    >"$results/llfree-sanitize.log" 2>&1

printf 'LLFree: default, optimized, prefer-installed, and ASan/UBSan passed.\n'
printf 'Logs: %s/llfree-*.log\n' "$results"
