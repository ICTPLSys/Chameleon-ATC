#!/usr/bin/env bash
set -euo pipefail
tool_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
port_root=$(cd -- "$tool_dir/../.." && pwd)
mm_src="$port_root/linux/tools/testing/selftests/mm"
cc=${CC:-gcc}
mkdir -p "$tool_dir/bin" "$port_root/results"

make -C "$port_root/linux" O="$port_root/build/selftest-headers" ARCH=x86 \
    headers_install INSTALL_HDR_PATH="$tool_dir/headers" \
    >"$port_root/results/upstream-headers-build.log" 2>&1

common=(-O2 -g -Wall -static -I "$tool_dir/headers/include" -I "$mm_src")
support=("$mm_src/vm_util.c" "$mm_src/thp_settings.c")
{
    "$cc" "${common[@]}" "$mm_src/madv_populate.c" "${support[@]}" \
        -pthread -lrt -lm -o "$tool_dir/bin/madv_populate"
    "$cc" "${common[@]}" "$mm_src/mremap_dontunmap.c" \
        -pthread -lrt -lm -o "$tool_dir/bin/mremap_dontunmap"
    "$cc" "${common[@]}" "$tool_dir/khugepaged-anon-subset.c" "${support[@]}" \
        -pthread -lrt -lm -o "$tool_dir/bin/khugepaged-anon-subset"
    "$cc" -O2 -g -Wall -static -Wno-unknown-pragmas \
        -DCOPY -DSCALE -DADD -DTRIAD -DSTREAM_ARRAY_SIZE=4194304 -DNTIMES=10 \
        "$tool_dir/stream-check.c" -lm -o "$tool_dir/bin/stream-check"
    file "$tool_dir"/bin/*
} >"$port_root/results/upstream-build.log" 2>&1
printf 'Static test binaries: %s/bin\n' "$tool_dir"
