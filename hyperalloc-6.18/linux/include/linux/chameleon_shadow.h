/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef _LINUX_CHAMELEON_SHADOW_H
#define _LINUX_CHAMELEON_SHADOW_H
#include <linux/types.h>
#include <linux/mm_types.h>
struct mm_struct;
struct vm_area_struct;
struct vm_fault;
struct ptdesc;
struct folio;
struct module;
struct ll_chameleon_range;
struct chameleon_shadow_policy_result {
	u64 prepared_objects, prepared_pages, ready_objects;
	u64 restored_pages, installed_pages, pending_objects;
};

/* submit borrows the isolated source until cancel has drained all I/O.
 * A successful save_complete publishes a durable offset; load must fill the
 * entire destination folio before returning zero. cancel drains asynchronous
 * work and releases that token's saved data. The core pins owner per token.
 * Backends without load retain the resident-only C3 contract. */
struct chameleon_shadow_backend_ops {
	struct module *owner;
	int (*submit)(u64 token, struct folio *folio);
	void (*cancel)(u64 token);
	int (*load)(u64 token, u64 offset, struct folio *destination);
};

#ifdef CONFIG_CHAMELEON
vm_fault_t chameleon_shadow_fault(struct vm_fault *vmf);
vm_fault_t chameleon_reclaimed_fault(struct vm_fault *vmf);
/* Sleepable event work; driver retains ranges for the duration of this call. */
void chameleon_shadow_host_event(u16 op, u64 batch,
		const struct ll_chameleon_range *ranges, unsigned int nr);
void chameleon_reclaimed_zap(struct vm_area_struct *vma, unsigned long address,
		pte_t *ptep, pte_t old_pte);
/* PTL held; notify only. Caller does native clear/rmap/ref/RSS accounting. */
void chameleon_shadow_zap(struct vm_area_struct *vma, unsigned long address,
			  pte_t *ptep, pte_t old_pte);
/* Caller holds mmap write, no PTL; restores all live slots of overlaps. */
int chameleon_shadow_restore_range(struct mm_struct *mm, unsigned long start,
				   unsigned long end);
void chameleon_shadow_pt_dtor(struct ptdesc *ptdesc);
int chameleon_shadow_backend_register(const struct chameleon_shadow_backend_ops *ops);
int chameleon_shadow_backend_unregister(const struct chameleon_shadow_backend_ops *ops);
int chameleon_shadow_save_submit(u64 token);
int chameleon_shadow_save_complete(u64 token, int status, u64 offset);
/* A policy owner is a unique nonzero enable generation, not a task PID.
 * Optional target mm has a caller-held mm_users ref. No MM locks on entry. */
int chameleon_shadow_policy_prepare(u64 owner, struct mm_struct *mm,
		unsigned long start, unsigned long end, unsigned int maximum,
		unsigned long max_pages, bool discard,
		struct chameleon_shadow_policy_result *result);
int chameleon_shadow_policy_restore(u64 owner, unsigned int maximum,
		struct chameleon_shadow_policy_result *result);
/* All owned source folios, including finalized reservations. Subtract only
 * the retired bytes already included in the matching Host capacity view. */
unsigned long chameleon_shadow_reserved_pages(void);
#else
static inline void chameleon_shadow_host_event(u16 op, u64 batch,
	const struct ll_chameleon_range *ranges, unsigned int nr) { }
static inline void chameleon_reclaimed_zap(struct vm_area_struct *vma,
	unsigned long address, pte_t *ptep, pte_t old_pte) { }
static inline vm_fault_t chameleon_shadow_fault(struct vm_fault *vmf) { return 0; }
static inline vm_fault_t chameleon_reclaimed_fault(struct vm_fault *vmf) { return VM_FAULT_SIGBUS; }
static inline void chameleon_shadow_zap(struct vm_area_struct *vma,
	unsigned long address, pte_t *ptep, pte_t old_pte) { }
static inline void chameleon_shadow_pt_dtor(struct ptdesc *ptdesc) { }
static inline int chameleon_shadow_restore_range(struct mm_struct *mm,
				unsigned long start, unsigned long end) { return 0; }
#endif
#endif
