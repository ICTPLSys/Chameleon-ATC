/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef X86_KVM_CHAMELEON_H
#define X86_KVM_CHAMELEON_H

#include <linux/kvm_host.h>

struct kvm_page_fault;

bool kvm_chameleon_supported(void);
int kvm_chameleon_enable(struct kvm *kvm, struct kvm_enable_cap *cap);
long kvm_chameleon_ioctl(struct kvm *kvm, unsigned int ioctl,
			 unsigned long arg);
void kvm_chameleon_destroy(struct kvm *kvm);
int kvm_chameleon_fault(struct kvm_vcpu *vcpu, struct kvm_page_fault *fault);
struct rw_semaphore *kvm_chameleon_fault_gate(struct kvm *kvm);
bool kvm_chameleon_fault_blocked(struct kvm *kvm,
		const struct kvm_memory_slot *slot, gfn_t gfn);
int kvm_chameleon_max_level(struct kvm *kvm,
		const struct kvm_memory_slot *slot, gfn_t gfn, int level);

#endif
