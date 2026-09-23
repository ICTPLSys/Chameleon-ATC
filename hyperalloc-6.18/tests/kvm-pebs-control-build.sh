#!/usr/bin/env bash
set -euo pipefail
test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cc=${CC:-cc}
"$cc" -O2 -g -std=gnu11 -Wall -Wextra -Werror -static \
    -I "$test_dir/upstream/headers/include" \
    "$test_dir/kvm-pebs-control.c" -o "$test_dir/kvm-pebs-control"
