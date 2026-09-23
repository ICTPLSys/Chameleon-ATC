#!/usr/bin/env bash
# Run inside the disk Guest. Safe to repeat after cloud-init has completed.
# This installs userspace dependencies; kernel/Hermit installation is separate.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo 'guest-setup: run as root inside the Guest (sudo bash guest-setup.sh)' >&2
    exit 1
fi
if ! command -v apt-get >/dev/null; then
    echo 'guest-setup: requires an apt-based Guest; Ubuntu 22.04 amd64 is the default image' >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
# Package installation never upgrades the distribution or reboots the Guest.
apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300 update
apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=300 install -y --no-install-recommends \
    ca-certificates curl wget git rsync openssh-server \
    python3 python3-pip python3-venv \
    build-essential gcc-11 g++-11 make cmake ninja-build pkg-config \
    bc flex bison libssl-dev libelf-dev dwarves \
    cpio kmod initramfs-tools grub2-common \
    pciutils iproute2 ethtool numactl stress-ng time jq \
    rdma-core ibverbs-providers ibverbs-utils rdmacm-utils \
    libibverbs-dev librdmacm-dev perftest

install -d -m 0755 /opt/chameleon /etc/security/limits.d
cat > /etc/security/limits.d/90-chameleon.conf <<'EOF'
# RDMA applications need enough locked memory for their registered buffers.
* soft memlock unlimited
* hard memlock unlimited
root soft memlock unlimited
root hard memlock unlimited
EOF

systemctl enable --now ssh.service serial-getty@ttyS0.service
if ! mountpoint -q /sys/kernel/debug; then
    mount -t debugfs debugfs /sys/kernel/debug
fi

printf '%s\n' 'Guest userspace setup complete. Reconnect SSH to pick up memlock limits.' \
    'Install the Chameleon Guest kernel package next; RDMA network and Hermit are configured separately.'
