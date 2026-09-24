#!/usr/bin/env bash
set -euo pipefail
test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cc=${CC:-cc}
"$cc" -O2 -g -std=gnu11 -Wall -Wextra -Werror -static \
    "$test_dir/guest-msr-guard.c" -o "$test_dir/guest-msr-guard"
