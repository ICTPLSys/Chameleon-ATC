#!/usr/bin/env bash
set -euo pipefail
port_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$port_root"
mkdir -p results
tests/llfree-tests.sh
ninja -C build/qemu -j"${JOBS:-16}" \
  tests/unit/test-llfree-states tests/unit/test-iov tests/unit/test-aio \
  tests/unit/test-aio-multithread tests/unit/test-thread-pool \
  > results/qemu-check-build.log 2>&1
build/qemu/pyvenv/bin/meson test -C build/qemu --no-rebuild --print-errorlogs \
  test-llfree-states test-iov test-aio test-aio-multithread test-thread-pool \
  > results/qemu-unit-tests.log 2>&1
make -C tests userspace
tests/upstream/build.sh
${CC:-gcc} -O2 -Wall -Wextra -Werror -static tests/pmu_probe.c -o tests/pmu_probe
python3 scripts/make-guest-initramfs.py
python3 scripts/test-vm.py --mode manual --extended
python3 scripts/test-vm.py --mode auto
python3 scripts/test-vm.py --mode failure
python3 scripts/make-nested-initramfs.py
python3 scripts/test-nested-vm.py
python3 scripts/record-manifest.py
echo 'PASS HyperAlloc unit, guest, and Linux 6.18 nested-host suites'
