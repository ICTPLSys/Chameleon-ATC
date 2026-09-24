# Linux 6.18 in-tree RDMA headers and symbols; no old vendor OFED overlay.
ifneq ($(KERNELRELEASE),)
obj-m += rswap-client.o
rswap-client-y := rswap_client.o
ifneq ($(CONFIG_INFINIBAND),)
rswap-client-y += rswap_rdma.o
ccflags-y += -DRSWAP_HAS_RDMA
endif
ccflags-y += -I$(src)/..

# External-module fixdep paths are relative to M, not the kernel output.
# Explicitly rebuild every object after a kernel configuration change: the
# ib_dma_* inline helpers depend on CONFIG_INFINIBAND_VIRT_DMA for SoftRoCE.
$(addprefix $(obj)/,$(rswap-client-y)): $(objtree)/include/generated/autoconf.h
else
KDIR ?= ../../build/guest
CC ?= clang
.PHONY: all clean
all:
	$(MAKE) -C $(KDIR) CC=$(CC) M=$(CURDIR) modules
clean:
	$(MAKE) -C $(KDIR) CC=$(CC) M=$(CURDIR) clean
endif
