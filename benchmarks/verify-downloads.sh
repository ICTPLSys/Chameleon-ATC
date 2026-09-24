#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"

sha256sum --check SHA256SUMS
jq -e . dataset/cachelib-kvcache-202206/config_kvcache.json >/dev/null

verify_revision() {
    local path=$1
    local expected=$2
    local actual
    actual=$(git -C "$path" rev-parse HEAD)
    if [[ "$actual" != "$expected" ]]; then
        printf 'revision mismatch: %s expected=%s actual=%s\n' "$path" "$expected" "$actual" >&2
        return 1
    fi
    printf 'revision OK: %s %s\n' "$path" "$actual"
}

verify_revision apps/cachelib fc45cc7de2ef45122675f3b3197256757e2ce903
verify_revision apps/cassandra d48548c7afe91fc654f4dfd518628dd90ab16ccd
verify_revision apps/graph500 f89d643ce4aaae9a823d310c6ab2dd10e3d2982c
verify_revision apps/graph500-omp 6a21c992273f2ba7f742bb64af7bdfe1bc81f101
verify_revision apps/graphchi 6461c89f217f63482e2468d776bb942067f8288c
verify_revision apps/liblinear 1f3c41f2b77bba2723c6454fc2948648283192b6
verify_revision apps/memcached 2d51e364799bc9698bd4b11728ea978cea12da6e
verify_revision apps/metis e5b04e2aa53301de71f0f5193f36e88c82008e6f
verify_revision apps/spark 16c3272d893f10fae8795e23018b60e61d9d654d
verify_revision apps/xsbench ba08e5221af6106252b866e50ea123c69d31a4e2
verify_revision apps/ycsb 66302f301b13f60d4bcb2f29f478586bb1d6f2e0

if [[ ${1:-} == --archives ]]; then
    tar -tzf apps/_archives/apache-cassandra-5.0.1-bin.tar.gz >/dev/null
    tar -tzf apps/_archives/ycsb-0.17.0.tar.gz >/dev/null
    unzip -t apps/_archives/liblinear-multicore-2.50.zip >/dev/null
    unzip -t dataset/wikipedia-france/web-wikipedia_link_fr.zip >/dev/null
    xz -t -T0 dataset/kdd12/kdd12.xz
    if command -v pigz >/dev/null; then
        pigz -t dataset/twitter-2010/twitter-2010.txt.gz
    else
        gzip -t dataset/twitter-2010/twitter-2010.txt.gz
    fi
    bzip2 -t dataset/wikipedia-english/download.tsv.wikipedia_link_en.tar.bz2
    printf 'archive stream checks OK\n'
fi
