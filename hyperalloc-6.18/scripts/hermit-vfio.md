# Hermit RDMA VF passthrough

`hermit-vfio.py` prepares explicitly selected SR-IOV VFs for the disk-backed
Chameleon VM. It follows the PF → VF → `vfio-pci` → Guest native RDMA driver
workflow used by `the original VFIO tooling`, with PCI addresses supplied on
the command line and a journal for restoring the original VF drivers.

The PF keeps its native driver throughout. The tool does not rebind a PF, stop a
VM, change interface addresses, or grant global access to `/dev/vfio`. Commands
are read-only unless `--apply` is present; `inspect` is always read-only.

## Select the hardware

Run this after booting the deployment Host kernel with IOMMU enabled. For an
Intel host the boot command line includes `intel_iommu=on iommu=pt`; the required
VFIO, IOMMU and native RDMA options are in the deployment kernel configuration.

```bash
cd /path/to/chameleon-ae/hyperalloc-6.18
python3 scripts/hermit-vfio.py inspect
```

The JSON lists PF/VF PCI addresses, driver and override, network interfaces,
RDMA devices, IOMMU group members and current VF counts. `environment` reports
`vfio_iommu_type1.dma_entry_limit` and the invoking process's memlock limits.

The read-only inventory on this development host found `mlx5_0` at
`0000:69:00.0` and `mlx5_1` at `0000:69:00.1`; each supports 16 VFs and currently
has zero enabled. These are inventory observations, not default device choices.
Select a PF appropriate for the intended RDMA fabric, then create VFs:

```bash
HERMIT_PF_BDF=0000:69:00.0  # Replace with the chosen PF from inspect.
sudo python3 scripts/hermit-vfio.py enable-vfs --pf "$HERMIT_PF_BDF" --count 2
sudo python3 scripts/hermit-vfio.py enable-vfs --pf "$HERMIT_PF_BDF" --count 2 --apply
python3 scripts/hermit-vfio.py inspect --pf "$HERMIT_PF_BDF"
```

The NIC firmware must already enable SR-IOV. The tool writes the standard PCI
`sriov_numvfs` attribute. An existing matching VF count is a no-op; changing an
existing nonzero count is refused because recreating VFs invalidates assignments.
It never cycles the count through zero automatically. Creating the initial VF
population also requires the PF network interface to be down and its RDMA
device unused, because a PF driver may reset resources when enabling SR-IOV.

For an InfiniBand mlx5 VF, configure unique fabric-appropriate node/port GUIDs
and policy if the NIC requires them. Do this before binding the VF to VFIO:

```bash
# Choose unique GUIDs for this fabric. These values are examples only.
sudo python3 scripts/hermit-vfio.py configure-vf \
  --pf "$HERMIT_PF_BDF" --vf-index 0 \
  --node-guid 11:22:33:44:77:20:01:90 \
  --port-guid 11:22:33:44:77:20:01:91 --policy Follow --apply
```

This uses the original Hermit deployment's mlx5 `sriov/INDEX/node`, `port` and
`policy` interfaces. Missing attributes produce a clear error. RoCE address,
VLAN and routing configuration is supplied by the deployment's network setup.
The Guest's native VF driver probes after VFIO assignment; no PF driver reload
is performed by this tool.

## Bind the selected VF and provision mapping capacity

Set the VF address from `inspect`'s `virtfnN` mapping; do not infer it from the PF
address. Repeat `--vf BDF` if a single IOMMU group contains multiple intended VFs.
Every group member must be an explicitly selected VF of the stated PF.

```bash
read -r -p 'Selected VF BDF from inspect: ' HERMIT_VF_BDF
sudo python3 scripts/hermit-vfio.py bind --pf "$HERMIT_PF_BDF" --vf "$HERMIT_VF_BDF"
sudo python3 scripts/hermit-vfio.py bind \
  --pf "$HERMIT_PF_BDF" --vf "$HERMIT_VF_BDF" --apply
```

The bind/restore dry runs use sudo so open device descriptors can be checked
across all processes. The VF must be idle: no administratively UP network interface and no process
holding its uverbs or VFIO group/device file. An unrelated `driver_override` or
unexpected VF driver is refused. The current supported native VF drivers are
`mlx5_core` and `mlx4_core`; an unbound VF is also supported. The journal defaults
to `build/hardware/vfio-state.json`; use `--state PATH` for independent selections.
A failure after changing some VFs rolls them back. If rollback itself fails,
the journal remains available for explicit recovery.

The Chameleon launcher's coordinated VFIO path uses 4 KiB Host backing so it can
retire and reinstall memory while maintaining the device mappings. Provision at
least one DMA entry per 4 KiB of Guest RAM plus a reserve **before starting QEMU**:

```bash
python3 scripts/hermit-vfio.py configure-vfio --memory-mib 8192
sudo python3 scripts/hermit-vfio.py configure-vfio --memory-mib 8192 --apply
```

The default reserve is 4,096 entries; 8 GiB therefore needs 2,101,248 entries.
`--reserve-entries N` overrides the reserve. This operation only increases the
running module parameter and never lowers an existing larger value. For
multiple VMs, the kernel enforces the entry limit per VFIO container: provision
for the largest required container (including any extra DMA mappings).

The current development host reports `dma_entry_limit=65535`, so this setting
must be increased before an 8 GiB deployment launch. Its current memlock soft
and hard limits are both unlimited. Other launch sessions must have enough
`RLIMIT_MEMLOCK` to pin Guest RAM plus device overhead; configure the launching
shell/service accordingly (for example, a privileged shell with `ulimit -l
unlimited`). The helper cannot raise the limit of its parent QEMU launcher.
Neither the DMA parameter nor VF driver binding persists automatically across a
Host reboot; rerun the preparation commands after boot as needed.

Use the selected BDF in the Chameleon VM launcher's VFIO argument. The Guest uses
its own native mlx5 driver and normal RDMA verbs; VFIO and the IOMMU remain on
the Host. Hermit then uses `backend=rdma` with the memory server address. The
Guest's RDMA path must use this assigned VF, with the appropriate IPoIB or RoCE
address configured inside the Guest.

## Restore the Host VF driver

Stop the VM and any process using the VF before restoring it:

```bash
sudo python3 scripts/hermit-vfio.py restore
sudo python3 scripts/hermit-vfio.py restore --apply
python3 scripts/hermit-vfio.py inspect --pf "$HERMIT_PF_BDF"
```

Restore uses the saved driver and override for each VF, including a previously
unbound VF. It validates the boot ID, PCI identity, PF and IOMMU group before
modifying anything. It never disables the PF's VF population. A completed
journal records `restored`; a subsequent binding can replace that completed
journal. With a custom journal, pass the same `--state PATH` to bind and restore.

## Verification performed without changing physical hardware

```bash
python3 tests/hermit-vfio-test.py
python3 scripts/hermit-vfio.py inspect
```

The 23 fake-sysfs tests cover exact VF/group selection, PF protection, active
network/RDMA/VM refusal, interrupted binding, partial rollback recovery,
identity checks, original unbound states, VF count preservation, GUID rollback,
and DMA mapping-capacity planning. All pass. The hardware inventory and an
8 GiB DMA-capacity plan were also read successfully. No physical VF creation,
VF driver rebinding, DMA-limit change, or VFIO VM run is claimed by these tests.
