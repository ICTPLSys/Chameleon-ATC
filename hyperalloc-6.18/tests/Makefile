obj-m += guest_allocator.o
obj-m += chameleon_shadow_backend.o
obj-m += chameleon_psi_load.o
obj-m += chameleon_control_cleanup.o

PORT_ROOT := $(abspath ..)
UAPI_EXPORT := $(PORT_ROOT)/build/uapi-export
GUEST_BUILD ?= $(PORT_ROOT)/build/guest
KERNEL_CC ?= clang

.PHONY: all module userspace clean kvm-chameleon-control
all: module userspace
module:
	$(MAKE) -C $(PORT_ROOT)/linux O=$(GUEST_BUILD) CC=$(KERNEL_CC) LOCALVERSION= M=$(CURDIR) modules
userspace: chameleon_tuning memory_test chameleon_tracker chameleon_manager chameleon_shadow chameleon_control chameleon_policy chameleon_modes chameleon_hermit chameleon_scale chameleon_fault_accounting chameleon_swap_tracking chameleon_commit_rollback chameleon_skew pmu_probe
memory_test: memory_test.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_tuning: chameleon_tuning.c chameleon_manager.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_tracker: chameleon_tracker.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static $< -o $@
chameleon_manager: chameleon_manager.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_shadow: chameleon_shadow.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_control: chameleon_control.c chameleon_shadow.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_policy: chameleon_policy.c chameleon_control.c chameleon_shadow.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_modes: chameleon_modes.c chameleon_tracker.c chameleon_manager.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_hermit: chameleon_hermit.c chameleon_control.c chameleon_shadow.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_scale chameleon_fault_accounting chameleon_swap_tracking chameleon_commit_rollback: %: %.c chameleon_hermit.c chameleon_control.c chameleon_shadow.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
chameleon_skew: chameleon_skew.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@ -lm
pmu_probe: pmu_probe.c
	$(CC) -O2 -g -Wall -Wextra -Werror -static $< -o $@
kvm-chameleon-tuning: kvm-chameleon-tuning.c
	$(MAKE) -C $(PORT_ROOT)/linux O=$(PORT_ROOT)/build/uapi INSTALL_HDR_PATH=$(UAPI_EXPORT) headers_install
	$(CC) -I$(UAPI_EXPORT)/include -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
kvm-chameleon-control: kvm-chameleon-control.c
	$(MAKE) -C $(PORT_ROOT)/linux O=$(PORT_ROOT)/build/uapi INSTALL_HDR_PATH=$(UAPI_EXPORT) headers_install
	$(CC) -I$(UAPI_EXPORT)/include -O2 -g -Wall -Wextra -Werror -static -pthread $< -o $@
clean:
	$(MAKE) -C $(PORT_ROOT)/linux O=$(GUEST_BUILD) M=$(CURDIR) clean
	$(RM) chameleon_tuning memory_test chameleon_tracker chameleon_manager chameleon_shadow chameleon_control chameleon_policy chameleon_modes chameleon_hermit chameleon_scale chameleon_fault_accounting chameleon_swap_tracking chameleon_commit_rollback chameleon_skew pmu_probe kvm-chameleon-control kvm-chameleon-tuning
