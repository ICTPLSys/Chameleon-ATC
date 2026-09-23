# Hermit data path for Chameleon on Linux 6.18

This port keeps Hermit's organization: a separately built `rswap-client.ko`,
small kernel-side integration, and an RDMA memory server. Reference sources are
`../../hermit/remoteswap` at Hermit commit
`10dfa5c918310559376069160bec6609a9a41fd4`. The reference tree stays unchanged.
The original module identifies Chenxi Wang and Yifan Qiao as its authors and
uses `Dual BSD/GPL`; this integration is distributed under the GPL alternative.
Hermit credits Fastswap and Canvas for the remoteswap transport ancestry.

The module supports two backends through the same token/folio lifecycle:

* `backend=rdma` connects to the separate memory server, negotiates a registered
  memory pool using Hermit's existing protocol, and performs actual one-sided
  RDMA writes and reads. Saved data resides on the server.
* `backend=dram` keeps Hermit's local DRAM debug backend. It copies real data to
  a separate Guest `vzalloc` pool. This exercises save/load and restoration but
  does not demonstrate net Guest memory savings or an RDMA network transfer.

Linux 6.18 removed the old `frontswap` interface. The module uses
`linux/include/linux/chameleon_shadow.h` instead of reinstating the old swap
implementation. The integration in `linux/mm/chameleon_shadow.c` connects
successful data saves to Chameleon's existing Host range protocol and faults
back the original application contents. Hermit's old frontswap prefetcher,
per-CPU queue scheduler, and workload tuning are not enabled by this port.

## Build

For disk-backed guests and physical RDMA passthrough, use the separate
[deployment workflow](../../progress/physical-rdma-deployment.md).
`scripts/kernel-deploy.py build/package --role guest` builds and packages the
client against `6.18.0-chameleon-guest` without overwriting the acceptance module.
`scripts/build-hermit.sh --server-only` builds the server without rebuilding
the original test client. Run `scripts/hermit-guest.py` inside the installed
Guest to check mlx5 routing and start the client. Host/VFIO coordination is
required; attaching a PCI device to the older uncoordinated path is insufficient.

From `hyperalloc-6.18`, after building the Guest kernel and its modules:

```sh
scripts/build-hermit.sh
```

This builds `hermit/client/rswap-client.ko`, prepares the fixed upstream
rdma-core v58.0 package, and builds `hermit/server/rswap-server` against those
matching headers and libraries. The matching package includes the RXE provider
and its configuration files; the physical Host's vendor OFED installation may
contain only the hardware provider. Test images stage this package under
`/opt/hermit-rdma` and set `LD_LIBRARY_PATH=/opt/hermit-rdma/lib`.

The lower-level build commands remain available:

```sh
make -C hermit/client KDIR="$PWD/build/guest" CC=clang
make -C hermit/server
```

The client automatically includes RDMA support when the target kernel has
`CONFIG_INFINIBAND` enabled. It uses that kernel's in-tree RDMA headers and
`Module.symvers`, with no vendor OFED header overlay. The standalone server
Makefile uses the system's standard `libibverbs` and `librdmacm` development
files; use `scripts/build-hermit.sh` for the tested matching SoftRoCE package.

The client Makefile explicitly makes every constituent object depend on the
target kernel's `include/generated/autoconf.h`. This fixes a real configuration
transition failure: an object compiled before `CONFIG_INFINIBAND_VIRT_DMA=y`
was reused by an external-module incremental build, so its inline DMA helpers
incorrectly followed the hardware DMA path on SoftRoCE's null `dma_device`.
A clean rebuild and the explicit dependency fixed the issue; the complete
RDMA acceptance tests below passed with the rebuilt module.

## Run

On the memory server (or the isolated outer test VM):

```sh
./hermit/server/rswap-server 192.0.2.1 9400 128
```

The last argument is the pool size in **MiB**, between 1 and 65536. The server
accepts IPv4 and IPv6 and prints `READY Hermit RDMA` once listening. One client
owns the pool at a time. On disconnect, the server releases the connection and
continues listening for the next client.

In a Guest with the target Chameleon kernel:

```sh
insmod /tests/rswap-client.ko backend=rdma sip=192.0.2.1 sport=9400 pool_mb=128
cat /sys/kernel/debug/hermit/stats
```

The module registers with Chameleon during `insmod`. For the original debug
mode use `backend=dram pool_mb=128`. In RDMA mode, `pool_mb` bounds the amount of
the advertised remote pool that this client may allocate.

## Kernel contract and ownership

A `submit(token, folio)` that returns zero produces exactly one save completion.
The module retains a source folio reference while copying or posting DMA. All
source byte accesses finish, and that reference is dropped, **before** the
successful completion. The kernel can then authorize Host discard safely.

The slot allocator reserves an aligned, contiguous number of 4 KiB slots equal
to the folio size. The current Chameleon path supports orders 0 and 2 through 9,
including its native mTHP orders.
The RDMA transport splits the transfer into genuine 4 KiB READ/WRITE work
requests, so folio sizes do not depend on an RNIC's scatter-gather limit.
The initial correctness implementation uses one serialized RC QP.

`load(token, offset, destination)` validates the identity and exact folio size,
then fills the entire destination. It returns zero only after all bytes have
arrived. A failed load retains its slot for retry. The core publishes present
PTEs only after a successful load, and performs the existing Host INSTALL
transaction before touching physically retired Guest backing.

`cancel(token)` synchronously drains that token's store and load work before
freeing its remote slots. It runs from the kernel cleanup path without MM
locks. It does not free any source or DMA buffer while I/O may still use it.
Each active token pins the module. Explicit unregister and `rmmod` therefore
fail while a token still owns backend data.

RDMA completions are checked, and a timed-out request breaks and drains the QP
before any DMA mapping or request object is released. The caller receives an
error; the module never turns a transport error into a successful save/load.
A broken connection requires ending its active objects and reloading the
module. Automatic reconnect and remote data replication are outside the
current functional implementation.

## Control and statistics

The root-only debugfs interface is `/sys/kernel/debug/hermit`:

```sh
echo unregister > /sys/kernel/debug/hermit/control
echo register > /sys/kernel/debug/hermit/control
```

`unregister` returns `EBUSY` while the core has active backend users. Once all
objects have been restored or unmapped, unregister succeeds and the module can
be unloaded. No module unload is needed to switch registration off and on.

With `CONFIG_CHAMELEON_TEST`, the same control file accepts:

```sh
echo 'delay 500' > /sys/kernel/debug/hermit/control
echo 'fail_store 1' > /sys/kernel/debug/hermit/control
echo 'fail_load 1' > /sys/kernel/debug/hermit/control
```

`delay` is milliseconds applied before a store. The failure counters cause the
next specified number of actual whole-folio calls to return `-EIO`. They do not
inject successful completions or saved contents. A following call uses the
real backend again. The default delay and counters are zero.

`stats` reports:

| Field | Meaning |
|---|---|
| `backend` | `dram` or `rdma` |
| `registered` | Whether this module is the current kernel backend |
| `capacity_pages` | Available 4 KiB remote/debug slots |
| `live_slots` | Number of token objects owning slots |
| `allocated_pages`, `peak_pages` | Current and peak number of allocated 4 KiB slots |
| `inflight` | Stores or loads that have not finished byte transfer |
| `store_success`, `store_failures` | Completed whole-folio store outcomes |
| `load_success`, `load_failures` | Whole-folio load outcomes |
| `bytes_written`, `bytes_read` | Bytes in successful complete folio transfers |
| `canceled` | Token objects drained and released, including normal cleanup |
| `completions` | Save completion calls made to the core |
| `callback_errors` | Completions rejected because core ownership/state changed |

A nonzero `callback_errors` count can be expected when fault/unmap cancels a
pending save. It is not evidence that discarded data were lost; the core must
have retained or restored resident data in that path. Final quiescent tests
check `live_slots=allocated_pages=inflight=0`.

## Functional acceptance

The current acceptance reports are:

| Backend | Report | Guest checks | Real Host transactions / pages |
|---|---|---:|---:|
| DRAM | [vm-hermit-acceptance-dram/report.json](../results/vm-hermit-acceptance-dram/report.json) | 343,304 | 38 / 3,293 |
| RDMA | [vm-hermit-acceptance-rdma/report.json](../results/vm-hermit-acceptance-rdma/report.json) | 343,661 | 38 / 3,293 |

Both passed. These tests save real application bytes, verify actual Host
backing removal, then fault back and compare the complete contents, PFNs, and
RSS for folio orders 0 and 2–9. They cover store, Host INSTALL, and load
failures; actual demand-fault `SIGBUS` followed by retry; concurrent faults;
partial unmap and partial-PMD `mremap`; permissions and fork/COW; process exit;
4 MiB pool exhaustion and slot reuse; native PSI restoration and the capacity
floor; and repeated reclamation. Final module unregister, unload, and reload
passed, including reconnecting the RDMA client to the same server. Guest and
Host kernel diagnostics stayed clean, and the HyperAlloc regression passed.

Reproduce the DRAM run from `hyperalloc-6.18` with the built Guest, Host, and
QEMU images:

```sh
scripts/build-hermit.sh
make -C tests userspace
python3 scripts/make-guest-initramfs.py
python3 scripts/make-nested-initramfs.py --chameleon --chameleon-policy \
    --output build/hermit-dram-host.cpio.gz
python3 scripts/test-chameleon-hermit.py --backend dram --regression \
    --name hermit-acceptance-dram
```

Build the RDMA test image and run the same acceptance through the isolated
L1 TAP `192.0.2.1` to L2 virtio-net `192.0.2.2` path:

```sh
python3 scripts/make-hermit-network-initramfs.py \
    --guest-initramfs build/guest-initramfs.cpio.gz \
    --server hermit/server/rswap-server
python3 scripts/test-chameleon-hermit.py --backend rdma --regression \
    --name hermit-acceptance-rdma
```

The RDMA run uses real RDMA CM, registered server memory, and one-sided
READ/WRITE through SoftRoCE RXE. It does not modify the physical Host's
networking or modules. Hardware RNIC throughput and latency, parameter tuning,
and paper performance experiments have not been measured by these tests.

## Protocol compatibility

`wire.h` preserves Hermit's existing 328-byte native-endian x86 control message
layout and message identifiers: AVAILABLE, QUERY/FREE_SIZE, and
REQUEST_CHUNKS/GOT_CHUNKS. The server advertises registered virtual addresses,
rkeys and lengths; all subsequent data traffic is one-sided RDMA. This port
fixes the old server's IPv4 parsing, unchecked connection counts, shared send
buffer reuse, memory registration error handling, and disconnected-session
cleanup. The server's command-line pool unit is intentionally MiB to support
small VM functional tests; the old server used GiB and a CPU-count argument.
