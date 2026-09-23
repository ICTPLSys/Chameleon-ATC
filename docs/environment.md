# Environment and installation

## Machines

The tested compute host has two Intel Xeon Gold 6342 sockets, 24 physical cores
per socket, 256 GiB RAM, and a BlueField-3 / ConnectX-7 native InfiniBand port.
Use an Intel machine with KVM/EPT, working PEBS virtualization, two NUMA nodes,
and an IOMMU. Enable Intel VT-x, VT-d and SR-IOV in firmware. The supplied Fig9
CPU allocator uses different physical cores for each vCPU, QEMU service thread
group and request generator, excluding SMT siblings. The largest mix needs
48 physical cores and 200 GiB of VM RAM, plus Host headroom.

The memory server needs a native RDMA interface reachable from the Guests and
at least 24 GiB of free RAM for three independent 8 GiB pools. It does not need
three VFs: three server processes share its native RDMA interface. The compute
host passes one distinct VF to each concurrent Guest. A single-VM experiment
reuses one VF after the multi-VM experiment has stopped.

Use Ubuntu 22.04 amd64 userspace on the compute host and Guests. Both deployment
kernels are Linux **6.18.0**, built from `hyperalloc-6.18/linux/` with separate
configurations and output directories. Their release strings are
`6.18.0-chameleon-host` and `6.18.0-chameleon-guest`. This source includes mTHP;
it uses the in-tree mlx5/RDMA drivers, without a separately installed OFED
kernel stack. QEMU includes the HyperAlloc/Chameleon changes. VMs use disk
images and QEMU user networking for SSH and application requests; no libvirt
is required. Far-memory traffic uses the passed-through physical RDMA VF.

Allow roughly 40 GiB for the unpacked sources/data, additional space for kernel
builds, and a **350 GiB virtual template disk**. The disk is sparse, but input
preparation and GraphChi work files can consume substantial real disk space.
Fig9 creates three writable qcow2 overlays; do not modify their backing template.
Use fast local SSD/NVMe storage, and avoid concurrent copies during measurements.

## Build and boot the Host

```bash
bash scripts/setup-host.sh --apply
python3 scripts/build-system.py --jobs 24 --apply
bash scripts/install-host.sh --apply
```

`build-system.py` merges the currently running Host's `/boot/config-*` with the
deployment fragment to retain storage/network drivers. Use `--host-config FILE`
to choose another configuration. `--component host|guest|qemu|tools` builds an
individual component. The Guest build also builds its matching Hermit module;
the tools build compiles the Host Memcached generator with the bundled
HdrHistogram source. No Guest binary is reused from the author machine.

Add `intel_iommu=on iommu=pt` to the Host GRUB command line, update GRUB, then
reboot and select `6.18.0-chameleon-host`. The installer does not select a Host
kernel or reboot automatically. Verify:

```bash
uname -r
test -r /dev/kvm && test -w /dev/kvm
lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE
numactl --hardware
```

The experiment user needs `/dev/kvm` and the selected VFIO group permissions,
passwordless/noninteractive `sudo` for the runner's Host setup/numad guard,
and enough locked memory. Set memlock to unlimited for that user using
`/etc/security/limits.d/90-chameleon.conf`, then log in again. Check `ulimit -l` in both actual login sessions. Establish SSH key access to the
memory server; generated Guest SSH keys stay in the local `build/guests/` tree.

## Site configuration

Copy `ae/config/host.example.json` to `ae/config/host.json` and edit the memory
server SSH alias, absolute server binary path, RDMA address, and Guest IPs.
`192.0.2.0/24` is only an example isolated IB subnet. Use addresses configured
for the review machine's fabric. `rdma_interface` is the name inside a Guest,
not the Host PF interface. `client_numa_node` reserves the generator placement.
The managed pool ports default to 9401/9402/9403 for Fig9 and **9404** for
single-VM runs. Port 9400 is left available for an existing legacy pool. Choose
unused ports in this inventory; the AE runner owns the services it starts and
does not attach to a pre-existing process. `configure-site.py
--single-server-port PORT` changes the single-VM port when writing a new file.

When reusing a smaller installed template, add `"disk_size_gib": 350` to each
entry in `slots` and to `single_slot`. The runner grows only its stopped qcow2
overlays, then explicitly grows the Guest's `/dev/vda1` ext4 root before starting
applications. It never shrinks a disk or changes the backing template. This is
disk capacity, separate from each application's configured VM RAM allocation.
Overlays have no cloud-init seed, so automatic filesystem expansion must not
be assumed. Growth results are saved as `disk-growth.json`; ensure the Host also
has enough real storage for prepared inputs and GraphChi work files.

For existing, correctly isolated VFIO devices, the JSON can also be generated:

```bash
python3 scripts/configure-site.py --server-ssh reviewer@memory-server \
  --server-address 192.0.2.1 \
  --server-binary /opt/chameleon-hermit/server/rswap-server \
  --vf 0000:69:00.3 --vf 0000:69:00.4 --vf 0000:69:00.5 --write
```

Replace those BDFs with the actual VF addresses reported by `lspci` and
`hermit-vfio.py inspect`. This command only writes configuration; it does not
bind hardware. An empty `vfio` array is allowed while preparing the template,
but each experiment VM must have its assigned VF before real runs.

## Create the template and prepare applications

```bash
python3 scripts/create-template.py --apply
python3 scripts/deploy-benchmarks.py --apply
```

The first command creates a fresh Ubuntu 22.04 cloud Guest, installs development
and RDMA tools, uploads/installs the Guest kernel package, selects it in Guest
GRUB and boots it. Supply `--image /path/to/jammy-cloud.img` to use an existing
cloud image. Its defaults are 64 GiB RAM, 12 vCPUs and 350 GiB of virtual disk;
these are preparation settings, not the per-application measurement settings.

The second command uploads the bundled application trees, builds native Guest
binaries, prepares the KDD12/Twitter/Spark/PVC/CacheLib benchmark inputs, and builds
the saved Graph500 CSR graph with 32 BFS roots. It installs Java 17 for
Cassandra 5 and the Spark driver. YCSB runs on the Host with Java 8. The data
preparation can take considerable time, especially the 1.05-billion-edge
Twitter prefix. Reusing an already prepared template avoids repeating it.
The command shuts the template down when complete. Input details remain in
`ae/config/fig78-points.json` (the `workload_configuration` entries).

For manual access and file transfer:

```bash
python3 hyperalloc-6.18/scripts/guestctl.py --name chameleon-template status
python3 hyperalloc-6.18/scripts/guestctl.py --name chameleon-template start --wait
python3 hyperalloc-6.18/scripts/guestctl.py --name chameleon-template exec -- uname -r
python3 hyperalloc-6.18/scripts/guestctl.py --name chameleon-template upload LOCAL_FILE /tmp/FILE
python3 hyperalloc-6.18/scripts/guestctl.py --name chameleon-template download /tmp/FILE LOCAL_FILE
python3 hyperalloc-6.18/scripts/guestctl.py --name chameleon-template stop
```

Keep the template stopped during experiments. Run Fig7/8 and Fig9 sequentially:
the single-VM slot and Fig9 slot 1 intentionally share one VF.

## RDMA server and three compute-side VFs

Set up the physical IB fabric/subnet manager and the memory server's IP address
using the site's network configuration. The server's RDMA port must be ACTIVE.
Then compile the supplied server against its own native userspace RDMA stack:

```bash
python3 scripts/prepare-rdma-server.py --install-deps --apply
```

This uploads only the Hermit server source/header, builds it remotely and
installs the configured binary; it starts no persistent pool. It uses
`sudo -n` on that server for packages and installation. Subsequent experiment
runners start and clean up the pool processes they own.

If VFs have not yet been prepared, use the existing IB provisioning tool after
the template is offline. Replace `PF_BDF`, `PF_INTERFACE` and `REVIEWER_LOGIN`
with actual values. First inspect the printed plan; `--apply` creates/rebinds
the three selected VFs and records a restoration journal:

```bash
sudo python3 benchmarks/scripts/prepare-chameleon-fig9.py \
  --pf PF_BDF --interface PF_INTERFACE --user REVIEWER_LOGIN \
  --example ae/config/host.json --inventory ae/config/host.json
sudo python3 benchmarks/scripts/prepare-chameleon-fig9.py \
  --pf PF_BDF --interface PF_INTERFACE --user REVIEWER_LOGIN \
  --example ae/config/host.json --inventory ae/config/host.json --apply
```

Use a dedicated IB PF. The tool checks idle ownership, distinct IOMMU groups,
unique VF GUIDs and VFIO permissions, and increases the DMA-entry allowance for
the largest configured VM. It does not move the Host's management interface.
The selected PF's network interface must be administratively DOWN while changing
its VF population, and all existing VFs on that PF must be idle. This is distinct
from the physical IB link, which must remain ACTIVE for RDMA. Configure these
conditions for the dedicated experiment PF before running the provisioning tool.
For other fabrics or already prepared hardware, use the lower-level
`hyperalloc-6.18/scripts/hermit-vfio.py` and populate the inventory explicitly.

An optional live functional check uses small VMs with the actual Mix1 CPU
layout, reconnects each pool twice, and exercises a real Memcached generator:

```bash
make -C hyperalloc-6.18/tests chameleon_swap_tracking
sudo -v
python3 benchmarks/scripts/smoke-chameleon-fig9-rdma.py \
  --inventory ae/config/host.json --run --read-write required \
  --cpu-mix mix1 --reconnects 2 --client-probe \
  --directory ae/results/rdma-smoke
```


After all experiments and VMs have stopped, the provisioning journal can restore
the previous VF population/drivers with
`sudo python3 benchmarks/scripts/prepare-chameleon-fig9.py --restore`.
