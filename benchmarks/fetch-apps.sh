#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
apps_dir="$script_dir/apps"
mkdir -p "$apps_dir" "$apps_dir/_archives"

checkout_repo() {
    local name=$1
    local url=$2
    local revision=$3
    local target="$apps_dir/$name"

    if [[ ! -d "$target/.git" ]]; then
        git clone --filter=blob:none --no-checkout "$url" "$target"
    fi
    git -C "$target" fetch --depth 1 origin "$revision"
    git -C "$target" checkout --detach FETCH_HEAD
}

checkout_repo cachelib https://github.com/facebook/CacheLib.git fc45cc7de2ef45122675f3b3197256757e2ce903
checkout_repo cassandra https://github.com/apache/cassandra.git d48548c7afe91fc654f4dfd518628dd90ab16ccd
checkout_repo graph500 https://github.com/graph500/graph500.git f89d643ce4aaae9a823d310c6ab2dd10e3d2982c
checkout_repo graph500-omp https://github.com/graph500/graph500.git 6a21c992273f2ba7f742bb64af7bdfe1bc81f101
checkout_repo graphchi https://github.com/GraphChi/graphchi-cpp.git 6461c89f217f63482e2468d776bb942067f8288c
checkout_repo liblinear https://github.com/cjlin1/liblinear.git 1f3c41f2b77bba2723c6454fc2948648283192b6
checkout_repo memcached https://github.com/memcached/memcached.git 2d51e364799bc9698bd4b11728ea978cea12da6e
checkout_repo metis https://github.com/ydmao/Metis.git e5b04e2aa53301de71f0f5193f36e88c82008e6f
checkout_repo spark https://github.com/apache/spark.git 16c3272d893f10fae8795e23018b60e61d9d654d
checkout_repo xsbench https://github.com/ANL-CESAR/XSBench.git ba08e5221af6106252b866e50ea123c69d31a4e2
checkout_repo ycsb https://github.com/brianfrankcooper/YCSB.git 66302f301b13f60d4bcb2f29f478586bb1d6f2e0

archive="$apps_dir/_archives/liblinear-multicore-2.50.zip"
if [[ ! -f "$archive" ]]; then
    wget -c -O "$archive" \
        https://www.csie.ntu.edu.tw/~cjlin/libsvmtools/multicore-liblinear/liblinear-multicore-2.50.zip
fi
if [[ ! -d "$apps_dir/liblinear-multicore-2.50" ]]; then
    unzip -q "$archive" -d "$apps_dir"
fi

cassandra_archive="$apps_dir/_archives/apache-cassandra-5.0.1-bin.tar.gz"
if [[ ! -f "$cassandra_archive" ]]; then
    wget -c -O "$cassandra_archive" \
        https://archive.apache.org/dist/cassandra/5.0.1/apache-cassandra-5.0.1-bin.tar.gz
fi
if [[ ! -d "$apps_dir/apache-cassandra-5.0.1" ]]; then
    tar -xzf "$cassandra_archive" -C "$apps_dir"
fi

ycsb_archive="$apps_dir/_archives/ycsb-0.17.0.tar.gz"
if [[ ! -f "$ycsb_archive" ]]; then
    wget -c -O "$ycsb_archive" \
        https://github.com/brianfrankcooper/YCSB/releases/download/0.17.0/ycsb-0.17.0.tar.gz
fi
if [[ ! -d "$apps_dir/ycsb-0.17.0" ]]; then
    tar -xzf "$ycsb_archive" -C "$apps_dir"
fi
