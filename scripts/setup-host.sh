#!/usr/bin/env bash
# Ubuntu 22.04 compute-host userspace. Does not install a kernel or reboot.
set -euo pipefail
case ${1:---plan} in
  --help|-h) echo 'Usage: scripts/setup-host.sh [--plan|--apply]'; exit 0 ;;
  --plan) apply=0 ;;
  --apply) apply=1 ;;
  *) echo 'Use --plan or --apply' >&2; exit 2 ;;
esac
(( $# <= 1 )) || exit 2
packages=(build-essential gcc g++ clang llvm make cmake ninja-build meson
  pkg-config bc flex bison libssl-dev libelf-dev dwarves cpio kmod
  libglib2.0-dev libpixman-1-dev libslirp-dev libnuma-dev zlib1g-dev
  python3 python3-pip python3-venv python3-setuptools python3-numpy python3-matplotlib
  qemu-utils cloud-image-utils genisoimage openssh-client rsync curl wget git
  pciutils iproute2 ethtool numactl time jq acl patch xz-utils unzip
  rdma-core ibverbs-providers ibverbs-utils rdmacm-utils libibverbs-dev librdmacm-dev
  libevent-dev autoconf automake libtool openjdk-8-jre-headless openjdk-11-jdk
  initramfs-tools grub2-common)
if (( apply )); then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends "${packages[@]}"
else
  printf 'sudo apt-get update\nsudo apt-get install -y --no-install-recommends'
  printf ' %q' "${packages[@]}"
  printf '\n'
fi
