#!/bin/bash
set -euo pipefail
port_root=$(cd "$(dirname "$0")/.." && pwd)
port_jobs=${PORT_JOBS:-24}
mkdir -p "$port_root/build/host" "$port_root/configs" "$port_root/results"
make -C "$port_root/linux" O="$port_root/build/host" LOCALVERSION= x86_64_defconfig
port_cfg="$port_root/build/host/.config"
"$port_root/linux/scripts/config" --file "$port_cfg" \
  --set-str LOCALVERSION '-hyperalloc-host' --disable LOCALVERSION_AUTO \
  --enable VIRTUALIZATION --enable KVM --enable KVM_INTEL --enable KVM_AMD \
  --enable HYPERVISOR_GUEST --enable KVM_GUEST --enable PARAVIRT \
  --enable VIRTIO --enable VIRTIO_PCI --enable VIRTIO_NET --enable VIRTIO_CONSOLE \
  --enable DEVTMPFS --enable DEVTMPFS_MOUNT --enable BLK_DEV_INITRD --enable RD_GZIP \
  --enable PROC_FS --enable SYSFS --enable TMPFS --enable UNIX --enable INET \
  --enable SERIAL_8250 --enable SERIAL_8250_CONSOLE --enable PROC_PAGE_MONITOR \
  --enable TRANSPARENT_HUGEPAGE --enable PSI --enable DEBUG_FS \
  --enable DEBUG_VM --enable DEBUG_LIST --enable FTRACE --enable GUP_TEST \
  --disable LLFREE --disable VIRTIO_LLFREE_BALLOON \
  --disable DEBUG_INFO --disable DEBUG_INFO_BTF --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
  --enable DEBUG_INFO_NONE --disable MODULE_SIG \
  --set-str SYSTEM_TRUSTED_KEYS '' --set-str SYSTEM_REVOCATION_KEYS ''
"$port_root/linux/scripts/kconfig/merge_config.sh" -m -O "$port_root/build/host" \
  "$port_cfg" "$port_root/configs/hermit-rdma-common.fragment" \
  "$port_root/configs/hermit-rdma-host.fragment"
make -C "$port_root/linux" O="$port_root/build/host" LOCALVERSION= olddefconfig
cp "$port_cfg" "$port_root/configs/host.config"
make -C "$port_root/linux" O="$port_root/build/host" LOCALVERSION= -j"$port_jobs" bzImage
