#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
data_dir="$script_dir/dataset"

download() {
    local relative_path=$1
    local url=$2
    mkdir -p "$(dirname -- "$data_dir/$relative_path")"
    wget -c -O "$data_dir/$relative_path" "$url"
}

download kdd12/kdd12.xz \
    https://www.csie.ntu.edu.tw/~cjlin/libsvmtools/datasets/binary/kdd12.xz
download twitter-2010/twitter-2010.txt.gz \
    https://snap.stanford.edu/data/twitter-2010.txt.gz
download wikipedia-france/web-wikipedia_link_fr.zip \
    https://nrvis.com/download/data/web/web-wikipedia_link_fr.zip
download wikipedia-english/download.tsv.wikipedia_link_en.tar.bz2 \
    http://konect.cc/files/download.tsv.wikipedia_link_en.tar.bz2

download cachelib-kvcache-202206/config_kvcache.json \
    https://cachelib-workload-sharing.s3.amazonaws.com/pub/kvcache/202206/config_kvcache.json
for trace_id in 1 2 3 4 5; do
    download "cachelib-kvcache-202206/kvcache_traces_${trace_id}.csv" \
        "https://cachelib-workload-sharing.s3.amazonaws.com/pub/kvcache/202206/kvcache_traces_${trace_id}.csv"
done
