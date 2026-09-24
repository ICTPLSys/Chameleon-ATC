# Chameleon-ATC

Chameleon reclaims and restores virtual-machine memory using page-access
sampling, hierarchical hotness tracking, pressure feedback, and batched EPT
updates. This artifact builds on HyperAlloc, runs Linux 6.18 in both Guest and
Host, and uses Hermit over physical RDMA for remote memory. Guests are managed
directly with QEMU.

The artifact evaluates two main claims:

1. **Single VM (Figures 7 and 8):** Chameleon provides a better trade-off between
   memory reclamation and application slowdown than the supplied baselines.
2. **Multiple VMs (Figure 9):** Chameleon achieves lower average application
   slowdown when three applications run concurrently.

Eight applications are included: Liblinear, XSBench, Graph500 BFS, GraphChi
PageRank, Spark-KMeans, Metis PVC, Cassandra, and Memcached. SPEC CPU2017 `gcc`
is omitted because it requires a separate licence. Figure 9 covers Mix1–4.

## Layout

```text
chameleon-ae/
├── README.md, LICENSE
├── run-all.sh                 # Figures 7/8, then Figure 9; parse and draw
├── fig78.sh, fig9.sh           # Independent evaluation entry points
├── scripts/                   # Environment, build, install and Guest setup
├── docs/environment.md        # Full deployment instructions
├── hyperalloc-6.18/
│   ├── linux/                 # Complete Guest + Host kernel source
│   ├── configs/               # Separate Guest/Host build configurations
│   ├── qemu/, llfree-c/       # Modified QEMU and shared allocator
│   ├── hermit/                # Guest module, memory server and protocol
│   ├── scripts/, tests/       # Build, deployment, VM tools and checks
│   └── build/                 # Generated locally; not distributed
├── benchmarks/
│   ├── apps/, vendor/         # Application and library sources/distributions
│   ├── dataset/               # Included input data
│   ├── scripts/, patches/     # Application launch and preparation scripts
│   └── SOURCE-MANIFEST.tsv    # Upstream revisions and input sources
└── ae/
    ├── README.md              # Experiment protocols, baselines and claim assessment
    ├── config/                # Evaluation configurations and site configuration
    ├── scripts/               # Three-run evaluation, parsing and plotting
    ├── results_baselines/     # Author-provided pre-measured baselines
    └── results/              # Reviewer measurements and generated figures
```

Guest and Host share **one Linux source tree**, with different configurations
and build directories. The installed releases are `6.18.0-chameleon-guest` and
`6.18.0-chameleon-host`; deployment outputs are under
`hyperalloc-6.18/build/deploy-{guest,host,qemu}`. QEMU is based on version 8.2.1.

## Evaluation environment

**AE reviewers can contact the authors to request access to our prepared hardware platform and conduct the artifact evaluation.**

The tested compute platform uses a two-socket Intel server with 48 physical cores,
256 GiB RAM, KVM/EPT, PEBS support, and a Mellanox mlx5 InfiniBand adapter with
SR-IOV. A separate memory server provides native RDMA connectivity. Figure 9
needs three compute-side VFs in separate IOMMU groups, one per Guest; the
memory server can share one native RDMA interface across three independent pools. The scripts assign different physical cores to VM vCPUs, QEMU
service threads and Host workload generators.

Use Ubuntu 22.04 userspace, Python 3, NumPy/Matplotlib, a working `/dev/kvm`,
IOMMU/VFIO support, and SSH public-key access to the memory server. Setup needs
sudo; experiments use sudo for the QEMU/numad guard where applicable. Native
kernel RDMA drivers are used. Allow ample space for kernel builds, a 350 GiB
sparse template disk, prepared datasets and VM overlays; these generated
files are substantially larger than the roughly 37 GiB source/data package.

On an already prepared evaluation server, skip installation and begin with
the commands below. Coordinate access so only one evaluation uses the VFs
and server resources at a time. On your own servers, follow
[the environment guide](docs/environment.md). The main setup sequence is:

```bash
bash scripts/setup-host.sh --apply
python3 scripts/build-system.py --jobs 16 --apply
bash scripts/install-host.sh --apply
# Select the new Host kernel at reboot; enable IOMMU as described in the guide.
# Configure the experiment user's KVM/VFIO access and unlimited memlock,
# then log in again; see docs/environment.md.

# Configure the memory-server SSH target, its RDMA IP and the three VF addresses.
python3 scripts/configure-site.py --plan
# Edit ae/config/host.example.json for your hardware, then:
python3 scripts/configure-site.py --write
python3 scripts/prepare-rdma-server.py --install-deps --apply
python3 scripts/create-template.py --apply
python3 scripts/deploy-benchmarks.py --apply

# If the three VFs have not been prepared, provision them while the template is OFF.
# Replace PF_BDF, PF_INTERFACE and REVIEWER_LOGIN with your dedicated IB PF/user.
sudo python3 benchmarks/scripts/prepare-chameleon-fig9.py \
  --pf PF_BDF --interface PF_INTERFACE --user REVIEWER_LOGIN \
  --example ae/config/host.json --inventory ae/config/host.json
sudo python3 benchmarks/scripts/prepare-chameleon-fig9.py \
  --pf PF_BDF --interface PF_INTERFACE --user REVIEWER_LOGIN \
  --example ae/config/host.json --inventory ae/config/host.json --apply
```

`ae/config/host.json` contains site-specific addresses, VF identifiers, Guest
names and ports. It contains no passwords. The guide also covers VF creation,
remote memlock limits, template preparation, and uploading/running commands
with `guestctl.py`. Keep the prepared template stopped and unchanged while
its experiment overlays exist.

## Run the evaluation

From the artifact root, first inspect the plan. This does not start VMs:

```bash
./run-all.sh --plan
```

Run all supported experiments and draw Figures 7, 8 and 9:

```bash
sudo -v
./run-all.sh --results ae/results/evaluation-01
```

`run-all.sh` runs Figure 7/8 before Figure 9. Each application point and each Mix runs
**three times**.

Run a figure or a smaller application selection:

```bash
./fig78.sh --results ae/results/single-01
./fig78.sh --apps xsbench --results ae/results/xsbench-01
./fig9.sh --mixes mix1 --results ae/results/mix1-01
```

See [the evaluation details](ae/README.md) for the single-VM and multi-VM
protocols, provided baselines, and how to assess the two claims.

## Results and plotting

For `--results ae/results/evaluation-01`, the principal outputs are:

```text
ae/results/evaluation-01/
├── fig78/
│   ├── manifest.json, summary.json, all-local.json
│   ├── runs/, raw/            # Per-run parameters, logs and sampled counters
│   ├── fig7.svg, fig7.pdf
│   └── fig8.svg, fig8.pdf
└── fig9/
    ├── config.json, summary.json
    ├── raw/, raw-apps/        # Mix and per-application measurements
    └── fig9.svg, fig9.pdf
```

Reparse and redraw without rerunning applications:

```bash
./run-all.sh --parse-only --results ae/results/evaluation-01
# Or redraw a single already-parsed figure:
python3 ae/scripts/plot_figures.py --figure fig78 \
  --input ae/results/evaluation-01/fig78/summary.json \
  --output-dir ae/results/evaluation-01/fig78
```
