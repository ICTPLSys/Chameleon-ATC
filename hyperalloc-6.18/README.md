# Chameleon system source

`linux/` is the complete Linux 6.18 source shared by Guest and Host builds.
Guest Chameleon code resides primarily in `mm/chameleon*.c` and
`drivers/virtio/virtio_llfree.c`; Host KVM changes reside under
`arch/x86/kvm/`. The `configs/` directory supplies separate configurations.
`qemu/` contains HyperAlloc-based QEMU 8.2.1 with the Chameleon/VFIO protocol;
`llfree-c/` is shared through relative source links. `hermit/` contains the
Guest remote-swap module, native RDMA server and common protocol.

Use [`../scripts/build-system.py`](../scripts/build-system.py) to build the
deployment kernels and QEMU. Output goes to
`build/deploy-host`, `build/deploy-guest`, and `build/deploy-qemu`.
The deployment Guest package also contains its matching Hermit module.
See [`../docs/environment.md`](../docs/environment.md) for installation.

`scripts/guestctl.py` creates no libvirt dependency and provides Guest
start/stop/SSH/upload/download operations. `scripts/hermit-guest.py` verifies
the negotiated RDMA backend and retries only fresh InfiniBand stale-connection
rejections. Test source is in `tests/`; no prior build directory is required.

The runtime debugfs parameters are documented in
[README-debugfs.md](README-debugfs.md). The AE runners apply the configured
application parameters automatically; their source is
[`../ae/config/fig78-points.json`](../ae/config/fig78-points.json).
