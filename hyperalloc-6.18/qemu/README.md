# HyperAlloc: QEMU

This repository contains a QEMU 8.2.1 with a new llfree-balloon driver, also called "HyperAlloc".

- See: https://github.com/luhsra/hyperalloc-bench

## Publication

HyperAlloc: Efficient VM Memory De/Inflation via Hypervisor-Shared Page-Frame Allocators Lars Wrenger, Kenny Albes, Marco Wurps, Christian Dietrich, Daniel Lohmann In: Proceedings of the 20th European Conference on Computer Systems (EuroSys 2025); ACM

## Building

To build QEMU with LLFree-Balloon:
```sh
mkdir build
cd build
export CC=clang
../configure --enable-debug --target-list=x86_64-softmmu --enable-slirp --enable-llfree --enable-trace-backends=simple
ninja
```

## LLFree-C Submodule

The submodule with the modified [llfree-c](https://github.com/luhsra/llfree-c) can be found at [subprojects/llfree-c/llc](subprojects/llfree-c/llc).

## Kconfig

Virtio-Balloon and Virtio-LLFree-Balloon are mutually exclusive.
Per default LLfree-Balloon is enabled. To enable Virtio-Balloon instead,
have a look at [hw/virtio/Kconfig](hw/virtio/Kconfig).

## LLFree-Balloon Properties

The following properties can be set in [virtio-llfree-balloon.c](hw/virtio/virtio-llfree-balloon.c):

```c
static Property virtio_llfree_balloon_properties[] = {
    DEFINE_PROP_BIT("shrink-pagecache", VirtIOLLFreeBalloon, host_features,
                    LL_BALLOON_F_SHRINK_PAGECACHE, false),
    DEFINE_PROP_BIT("auto-mode", VirtIOLLFreeBalloon, host_features,
                    LL_BALLOON_F_AUTO_MODE, true),
    DEFINE_PROP_BIT("ioctl", VirtIOLLFreeBalloon, host_features,
                    LL_BALLOON_F_IOCTL, false),
    DEFINE_PROP_LINK("auto-mode-iothread", VirtIOLLFreeBalloon,
                     auto_mode_iothread, TYPE_IOTHREAD, IOThread *),
    DEFINE_PROP_IOTHREAD_VQ_MAPPING_LIST(
        "iothread-vq-mapping", VirtIOLLFreeBalloon, iothread_vq_mapping_list),
    DEFINE_PROP_END_OF_LIST(),
};
```

- **auto-mode**: Enable automatic reclamation, which periodically reclaims unused memory.
- **auto-mode-iothread**: Use the given iothread for the automatic reclamation. This frees up QEMUs main thread.
- **vfio**: We use this to indicate that we have to use VFIO, influencing some decisions in our virtio-llfree-balloon device. This is usually set automatically.
- **demand-shrink-pagecache**: Allow the hypervisor to instruct the guest to drop its page cache if we need more memory.
- **iothread-vq-mapping**: Use the given set of iothreads to handle guest install requests. They are assigned round-robin to the vCPUs.

## Example: LLFree-Balloon QEMU Arguments

```sh
./qemu-system-x86_64  -smp 4 -m 8G -enable-kvm \
    -object iothread,id=auto-mode-iothread \
    -object iothread,id=iothread1 \
    -object iothread,id=iothread2 \
    -object iothread,id=iothread3 \
    -object iothread,id=iothread4 \
    -device '{"driver":"virtio-llfree-balloon","auto-mode-iothread":"auto-mode-iothread","auto-mode":true,"iothread-vq-mapping":[{"iothread":"iothread1"},{"iothread":"iothread2"},{"iothread":"iothread3"},{"iothread":"iothread4"}]}' \
    -device '{"driver":"vfio-pci","host":'\"$PCI_DEVICE\"'}'
    # ...
```

This is an example of configuring LLFree-Balloon on the command line.
In this example, all of LLFree-Balloons optional IOTheads are used.
It is important that, in case of direct device assignment, `virtio-llfree-balloon` is added **before** vfio-pci.
`virtio-llfree-balloon` needs to do some setup, before vfio-pci starts running so that `virtio-llfree-balloon` is controlling the RAM regarding issuing VFIO ioctls.
Furthermore, `virtio-llfree-balloon` and vfio-pci should **always** use the same syntax for their argument.
QEMU allows both a JSON-based syntax, displayed here for `virtio-llfree-balloon` and vfio-pci, but also another syntax, displayed here for every other line.
QEMU internally processes all command lines of the same syntax, before moving on to the next.
Direct device assignment only works if `virtio-llfree-balloon` and vfio-pci use the same syntax and `virtio-llfree-balloon` is placed in the argument list before vfio-pci.


## Using the QEMU Monitor

LLFree-Balloon offers two QEMU Monitor commands.
```sh
info llfree_balloon
```
displays the actual current MiB size of LLFree-Balloon.

```sh
llfree_balloon target_value
```
sets the new target size in MiB for LLFree-Balloon.
These commands trigger the classic ballooning operations.

For QAPI/QMP commands (-> Python) we can use
```sh
llfree-balloon
```
to set the target size (in bytes).

## Provoking a VFIO Issue

In [hw/virtio/virtio-llfree-balloon.c](hw/virtio/virtio-llfree-balloon.c) set the flag:

```c
#define CONFIG_SIMULATE_BROKEN_VFIO 1
```
This disables the changes made to support VFIO.
This has been tested with a passed through Ethernet Controller.
In normal mode ethernet is working in the VM, with the flag set ethernet is broken.
