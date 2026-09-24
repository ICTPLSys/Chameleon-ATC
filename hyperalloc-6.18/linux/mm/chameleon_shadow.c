// SPDX-License-Identifier: GPL-2.0-only
/* Chameleon Shadow and saved-data Reclaimed entries. */
#include <linux/mm.h>
#include <linux/chameleon.h>
#include <linux/chameleon_shadow.h>
#include <linux/chameleon_transport.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/huge_mm.h>
#include <linux/ktime.h>
#include <linux/memcontrol.h>
#include <linux/mm.h>
#include <linux/mm_inline.h>
#include <linux/module.h>
#include <linux/mmu_notifier.h>
#include <linux/pagemap.h>
#include <linux/psi.h>
#include <linux/rcupdate.h>
#include <linux/rmap.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/swap.h>
#include <linux/swapops.h>
#include <linux/uaccess.h>
#include <linux/userfaultfd_k.h>
#include <linux/vmstat.h>
#include <linux/workqueue.h>
#include <linux/xarray.h>
#include <asm/tlb.h>
#include <asm/tlbflush.h>
#include "internal.h"

#define SH_PENDING U64_MAX
#define SH_MAX_BATCH 4096

enum shadow_state {
	SH_PREPARING, SH_RESIDENT, SH_SAVING, SH_SAVED, SH_FAILED, SH_RETIRED,
	SH_FINALIZING, SH_RECLAIMED, SH_RELEASED,
};

struct shadow {
	struct mm_struct *mm;       /* mm_count, never long-lived mm_users */
	struct folio *folio;        /* the C2 isolation reference */
	unsigned long address, pfn, table_pfn;
	unsigned int order;
	pid_t pid;
	u64 token, policy_owner;
	refcount_t refs;
	atomic_t live;
	atomic_t state;
	DECLARE_BITMAP(slots, HPAGE_PMD_NR); /* protected by the one PTE-table lock */
	struct delayed_work cleanup;
	struct delayed_work commit_rollback;
	struct work_struct reclaim;
	bool discard_allowed, host_registered, host_ready, finalized, pool_released;
	bool shadow_charged;
	bool host_missing, data_saved, submit_active;
	bool commit_rollback_active; /* backend_lock; owns one work reference */
	u64 saved_offset;
	u32 host_state;
	u64 host_batch;
	struct rcu_head rcu;
	struct mutex backend_lock;
	const struct chameleon_shadow_backend_ops *backend;
	int save_status;
	struct list_head preparing;
	pte_t saved[];
};

static DEFINE_XARRAY(sh_tokens);
static DEFINE_XARRAY(sh_pfns);
static DEFINE_MUTEX(prepare_lock);
static DEFINE_MUTEX(backend_lock);
static const struct chameleon_shadow_backend_ops *backend_ops;
static unsigned int backend_users;
static struct workqueue_struct *shadow_wq;
static atomic64_t next_token = ATOMIC64_INIT(0);
static atomic64_t live_objects, live_slots, metadata_pages, shadow_pages;
static atomic64_t owned_pages;
static atomic64_t prepare_batches, fault_restores, explicit_restores, zapped_slots;
static atomic64_t mm_groups, guest_flushes, prepare_pmd_demotions, fast_gup_syncs;
static atomic64_t pin_rollbacks;
static atomic64_t finalize_mm_groups, finalize_guest_flushes, reclaimed_slots;
static atomic64_t reservation_pages, host_reclaimed_pages;
static atomic64_t range_install_success, range_install_failure, zeroed_pages;
static atomic64_t cleanup_retries, finalize_accepted, finalize_rejected;
static atomic64_t data_save_success, data_save_failure, data_ready_objects;
static atomic64_t data_load_success, data_load_failure, data_restored_pages;
static atomic64_t data_fault_restores;
static atomic64_t demand_faults, demand_fault_successes, demand_fault_failures;
static atomic64_t demand_major_faults, demand_waiters;
static atomic64_t psi_fault_enter, psi_fault_leave, psi_fault_ns;
static atomic64_t load_attempts, load_demand_attempts, load_background_attempts;
static atomic64_t commit_rollback_attempts, commit_rollback_success;
static atomic64_t commit_rollback_failures, commit_rollback_skipped;

static int shadow_install_locked(struct shadow *sh);
static int shadow_host_forget(struct shadow *sh);
static int shadow_discard(u64 token);
static int shadow_range_control(u64 token, u16 op);
static int shadow_restore_token(u64 token);
static int shadow_restore_token_pages(u64 token, u64 *pages);
static int shadow_restore_data(struct shadow *sh, u64 *pages);
static int shadow_commit(u64 token);
static void shadow_reclaim_work(struct work_struct *work);
static void shadow_commit_rollback_work(struct work_struct *work);


static unsigned long pool_limit(void)
{
	return totalram_pages() / 200;
}

unsigned long chameleon_shadow_reserved_pages(void)
{
	/* Includes pending and finalized reservations. Policy subtracts the
	 * retired bytes in its same Host capacity snapshot, avoiding both a
	 * double charge and a race with asynchronous COMMIT_RESULT delivery. */
	return max_t(s64, 0, atomic64_read(&owned_pages));
}

/* backend_lock held, or exclusive final cleanup. Physical ownership and
 * the reservation survive retirement; only the transient Shadow credit is
 * returned so another batch can proceed while cold data stays remote. */
static void shadow_uncharge(struct shadow *sh)
{
	if (sh->shadow_charged) {
		sh->shadow_charged = false;
		atomic64_sub(1UL << sh->order, &shadow_pages);
	}
}

static pmd_t *shadow_pmd(struct mm_struct *mm, unsigned long addr)
{
	pgd_t *pgd = pgd_offset(mm, addr);
	p4d_t *p4d;
	pud_t *pud;
	pmd_t *pmd;

	if (pgd_none(*pgd) || pgd_bad(*pgd))
		return NULL;
	p4d = p4d_offset(pgd, addr);
	if (p4d_none(*p4d) || p4d_bad(*p4d))
		return NULL;
	pud = pud_offset(p4d, addr);
	if (pud_none(*pud) || pud_bad(*pud) || pud_trans_huge(*pud))
		return NULL;
	pmd = pmd_offset(pud, addr);
	return pmd_none(*pmd) ? NULL : pmd;
}

static void metadata_free_rcu(struct rcu_head *rcu)
{
	struct page *page = container_of(rcu, struct page, rcu_head);

	atomic64_dec(&metadata_pages);
	__free_page(page);
}

/* PTL held, or the page table is already detached and being destroyed. */
static void metadata_retire(struct ptdesc *ptdesc)
{
	u64 *slots = ptdesc->pt_shadow;

	if (!slots)
		return;
	WRITE_ONCE(ptdesc->pt_shadow, NULL);
	call_rcu(&virt_to_page(slots)->rcu_head, metadata_free_rcu);
}

void chameleon_shadow_pt_dtor(struct ptdesc *ptdesc)
{
	if (ptdesc->pt_shadow) {
		/* exit_mmap must have notified us of every private entry. */
		WARN_ON_ONCE(page_private(virt_to_page(ptdesc->pt_shadow)));
		metadata_retire(ptdesc);
	}
}

static void shadow_cleanup(struct work_struct *work)
{
	struct shadow *sh = container_of(to_delayed_work(work), struct shadow, cleanup);
	const struct chameleon_shadow_backend_ops *ops;

	/* No mmap, folio, PTL or registry lock may be held across cancel.
	 * cancel drains all backend reads before the isolation ref is released. */
	mutex_lock(&sh->backend_lock);
	ops = sh->backend;
	if (ops) {
		ops->cancel(sh->token);
		mutex_lock(&backend_lock);
		backend_users--;
		mutex_unlock(&backend_lock);
		sh->backend = NULL;
		module_put(ops->owner);
	}
	if (sh->finalized && !sh->pool_released && sh->folio &&
	    shadow_install_locked(sh)) {
		atomic64_inc(&cleanup_retries);
		mutex_unlock(&sh->backend_lock);
		queue_delayed_work(shadow_wq, &sh->cleanup, HZ);
		return;
	}
	if (shadow_host_forget(sh)) {
		atomic64_inc(&cleanup_retries);
		mutex_unlock(&sh->backend_lock);
		queue_delayed_work(shadow_wq, &sh->cleanup, HZ);
		return;
	}
	mutex_unlock(&sh->backend_lock);
	if (!sh->folio)
		goto physical_released;
	folio_lock(sh->folio);
	if (folio_mapped(sh->folio)) {
		folio_set_active(sh->folio);
		folio_unlock(sh->folio);
		folio_putback_lru(sh->folio);
	} else {
		folio_clear_active(sh->folio);
		folio_unlock(sh->folio);
		folio_put(sh->folio);
	}
physical_released:
	shadow_uncharge(sh);
	if (!sh->pool_released)
		atomic64_sub(1UL << sh->order, &owned_pages);
	atomic64_dec(&live_objects);
	mmdrop(sh->mm);
	kfree_rcu(sh, rcu);
}

static void shadow_put(struct shadow *sh)
{
	if (refcount_dec_and_test(&sh->refs))
		queue_delayed_work(shadow_wq, &sh->cleanup, 0);
}

static struct shadow *shadow_lookup(struct xarray *xa, unsigned long key)
{
	struct shadow *sh;

	rcu_read_lock();
	sh = xa_load(xa, key);
	if (sh && !refcount_inc_not_zero(&sh->refs))
		sh = NULL;
	rcu_read_unlock();
	return sh;
}

/* Caller has a matching private PTE under its PTL, hence its mapping ref. */
static struct shadow *shadow_from_pte(struct mm_struct *mm, unsigned long addr,
				     pte_t pte)
{
	struct shadow *sh;
	unsigned long pfn = swp_offset_pfn(pte_to_swp_entry(pte));

	sh = shadow_lookup(&sh_pfns, folio_pfn(page_folio(pfn_to_page(pfn))));
	if (sh && (sh->mm != mm || addr < sh->address ||
		   addr >= sh->address + (PAGE_SIZE << sh->order) ||
		   pfn != sh->pfn + ((addr - sh->address) >> PAGE_SHIFT))) {
		shadow_put(sh);
		return NULL;
	}
	return sh;
}

/* PTL held. Mapping/rmap/RSS references are deliberately unchanged here. */
static void shadow_drop_slot(struct shadow *sh, struct ptdesc *ptdesc,
			     unsigned long addr)
{
	unsigned int index = (addr - sh->address) >> PAGE_SHIFT;
	u64 *slots = ptdesc->pt_shadow;
	struct page *page;

	if (WARN_ON_ONCE(!test_and_clear_bit(index, sh->slots)))
		return;
	if (WARN_ON_ONCE(!slots))
		return;
	page = virt_to_page(slots);
	WARN_ON_ONCE(!slots[pte_index(addr)] || !page_private(page));
	slots[pte_index(addr)] = 0;
	set_page_private(page, page_private(page) - 1);
	if (!page_private(page))
		metadata_retire(ptdesc);
	atomic64_dec(&live_slots);
	if (sh->finalized)
		atomic64_dec(&reclaimed_slots);
	if (atomic_dec_and_test(&sh->live)) {
		atomic_set(&sh->state, SH_RETIRED);
		if (!sh->finalized)
			xa_erase(&sh_pfns, sh->pfn);
		xa_erase(&sh_tokens, sh->token);
		shadow_put(sh); /* registry ownership */
	}
}

/* mmap read/write and folio lock held. An object occupies only one PTE table.
 * Partial zaps can leave holes and different current VMAs inside that table. */
static unsigned int shadow_restore_locked(struct shadow *sh)
{
	struct mm_struct *mm = sh->mm;
	pmd_t *pmd = shadow_pmd(mm, sh->address);
	spinlock_t *ptl;
	pte_t *base;
	unsigned int i, restored = 0;

	if (READ_ONCE(sh->finalized) || !atomic_read(&sh->live) || !pmd || pmd_trans_huge(*pmd))
		return 0;
	base = pte_offset_map_lock(mm, pmd, sh->address, &ptl);
	if (!base)
		return 0;
	for (i = 0; i < (1U << sh->order); i++) {
		unsigned long addr = sh->address + (i << PAGE_SHIFT);
		struct vm_area_struct *vma;
		pte_t old, pte;

		if (!test_bit(i, sh->slots))
			continue;
		old = ptep_get(base + i);
		if (WARN_ON_ONCE(!is_swap_pte(old) ||
		    !is_chameleon_shadow_entry(pte_to_swp_entry(old)) ||
		    swp_offset_pfn(pte_to_swp_entry(old)) != sh->pfn + i))
			continue;
		vma = find_vma(mm, addr);
		if (WARN_ON_ONCE(!vma || vma->vm_start > addr))
			continue;
		pte = mk_pte(folio_page(sh->folio, i), vma->vm_page_prot);
		pte = pte_wrprotect(pte_mkold(pte));
		if (pte_young(sh->saved[i]))
			pte = pte_mkyoung(pte);
		if (pte_dirty(sh->saved[i]))
			pte = pte_mkdirty(pte);
		if (pte_swp_soft_dirty(old))
			pte = pte_mksoft_dirty(pte);
		else
			pte = pte_clear_soft_dirty(pte);
		if (pte_swp_uffd_wp(old))
			pte = pte_mkuffd_wp(pte);
		if (pte_write(sh->saved[i]) && (vma->vm_flags & VM_WRITE) &&
		    can_change_pte_writable(vma, addr, pte))
			pte = pte_mkwrite(pte, vma);
		set_pte_at(mm, addr, base + i, pte);
		update_mmu_cache(vma, addr, base + i);
		shadow_drop_slot(sh, virt_to_ptdesc(base), addr);
		restored++;
	}
	pte_unmap_unlock(base, ptl);
	return restored;
}

static void shadow_start_vmas(struct shadow *sh)
{
	unsigned long addr = sh->address, end = addr + (PAGE_SIZE << sh->order);
	struct vm_area_struct *vma;
	VMA_ITERATOR(vmi, sh->mm, addr);

	for_each_vma_range(vmi, vma, end)
		vma_start_write(vma);
}

vm_fault_t chameleon_shadow_fault(struct vm_fault *vmf)
{
	struct shadow *sh = NULL;
	pmd_t *pmd = shadow_pmd(vmf->vma->vm_mm, vmf->address);
	spinlock_t *ptl;
	pte_t *ptep, pte;
	vm_fault_t ret;

	if (!pmd || pmd_trans_huge(*pmd))
		return 0;
	ptep = pte_offset_map_lock(vmf->vma->vm_mm, pmd, vmf->address, &ptl);
	if (!ptep)
		return 0;
	pte = ptep_get(ptep);
	if (is_swap_pte(pte) && is_chameleon_shadow_entry(pte_to_swp_entry(pte)))
		sh = shadow_from_pte(vmf->vma->vm_mm, vmf->address, pte);
	pte_unmap_unlock(ptep, ptl);
	if (!sh)
		return 0;
	ret = folio_lock_or_retry(sh->folio, vmf);
	if (ret) {
		shadow_put(sh);
		return ret;
	}
	if (shadow_restore_locked(sh))
		atomic64_inc(&fault_restores);
	folio_unlock(sh->folio);
	shadow_put(sh);
	return 0;
}

void chameleon_shadow_zap(struct vm_area_struct *vma, unsigned long addr,
			  pte_t *ptep, pte_t old_pte)
{
	struct shadow *sh = shadow_from_pte(vma->vm_mm, addr, old_pte);

	if (WARN_ON_ONCE(!sh))
		return;
	shadow_drop_slot(sh, virt_to_ptdesc(ptep), addr);
	atomic64_inc(&zapped_slots);
	shadow_put(sh);
}

int chameleon_shadow_restore_range(struct mm_struct *mm, unsigned long start,
				   unsigned long end)
{
	unsigned long index = 0;
	struct shadow *sh;

	mmap_assert_write_locked(mm);
	for (;;) {
		rcu_read_lock();
		sh = xa_find(&sh_tokens, &index, ULONG_MAX, XA_PRESENT);
		if (!sh) {
			rcu_read_unlock();
			break;
		}
		if (!refcount_inc_not_zero(&sh->refs)) {
			rcu_read_unlock();
			if (index++ == ULONG_MAX)
				break;
			continue;
		}
		rcu_read_unlock();
		if (sh->mm == mm && sh->address < end &&
		    sh->address + (PAGE_SIZE << sh->order) > start) {
			if (READ_ONCE(sh->finalized)) {
				int ret;

				shadow_start_vmas(sh);
				ret = shadow_restore_data(sh, NULL);

				shadow_put(sh);
				if (ret)
					return ret == -EOPNOTSUPP ? -EBUSY : ret;
				goto next;
			}
			shadow_start_vmas(sh);
			folio_lock(sh->folio);
			if (shadow_restore_locked(sh))
				atomic64_inc(&explicit_restores);
			folio_unlock(sh->folio);
		}
		shadow_put(sh);
	next:
		if (index++ == ULONG_MAX)
			break;
	}
	return 0;
}

static bool shadow_vma_ok(struct vm_area_struct *vma, unsigned long addr,
			  unsigned int order)
{
	return vma && addr >= vma->vm_start &&
		addr + (PAGE_SIZE << order) <= vma->vm_end &&
		vma_is_anonymous(vma) && !userfaultfd_armed(vma) &&
		!(vma->vm_flags & (VM_SHARED | VM_PFNMAP | VM_MIXEDMAP |
				   VM_HUGETLB | VM_LOCKED | VM_IO));
}

static bool shadow_reserve_pages(unsigned long nr)
{
	s64 old = atomic64_read(&shadow_pages);

	for (;;) {
		if (old + nr > pool_limit())
			return false;
		if (atomic64_try_cmpxchg(&shadow_pages, &old, old + nr)) {
			atomic64_add(nr, &owned_pages);
			return true;
		}
	}
}

/* mmap write held. Successful return keeps the folio locked until the entire
 * mm group has flushed and fast-GUP's pre-existing readers have completed. */
static struct shadow *shadow_prepare_one(struct chameleon_candidate *candidate, u64 owner)
{
	struct mm_struct *mm = candidate->mm;
	struct folio *folio = candidate->folio;
	unsigned long addr = candidate->address;
	unsigned int nr = 1U << candidate->order, i;
	struct vm_area_struct *vma = vma_lookup(mm, addr);
	struct shadow *sh;
	struct ptdesc *ptdesc;
	pmd_t *pmd;
	pte_t *ptes;
	spinlock_t *ptl;
	u64 *metadata;
	int ret;

	if (!shadow_vma_ok(vma, addr, candidate->order) ||
	    !IS_ALIGNED(addr, PAGE_SIZE << candidate->order))
		return ERR_PTR(-EINVAL);
	vma_start_write(vma);
	pmd = shadow_pmd(mm, addr);
	if (!pmd || pmd_trans_huge(*pmd) || pmd_bad(*pmd))
		return ERR_PTR(-EAGAIN);
	if (!folio_trylock(folio))
		return ERR_PTR(-EBUSY);
	ret = -EBUSY;
	if (folio_order(folio) != candidate->order ||
	    !folio_test_anon(folio) || folio_test_ksm(folio) ||
	    folio_test_swapcache(folio) || folio_test_writeback(folio) ||
	    folio_test_private(folio) || folio_test_unevictable(folio) ||
	    folio_test_mlocked(folio) || folio_test_lru(folio) ||
	    folio_maybe_dma_pinned(folio) || folio_maybe_mapped_shared(folio) ||
	    folio_mapcount(folio) != nr ||
	    folio_ref_count(folio) != folio_expected_ref_count(folio) + 1)
		goto unlock;
	ret = -ENOSPC;
	if (!shadow_reserve_pages(nr))
		goto unlock;
	ret = -ENOMEM;
	sh = kzalloc(struct_size(sh, saved, nr), GFP_KERNEL);
	metadata = (u64 *)get_zeroed_page(GFP_KERNEL);
	if (!sh || !metadata)
		goto free_alloc;
	sh->mm = mm;
	sh->folio = folio;
	sh->address = addr;
	sh->pfn = folio_pfn(folio);
	sh->order = candidate->order;
	sh->pid = candidate->pid;
	sh->token = atomic64_inc_return(&next_token);
	sh->policy_owner = owner;
	sh->shadow_charged = true;
	refcount_set(&sh->refs, 2); /* registry + this preparer */
	atomic_set(&sh->live, nr);
	atomic_set(&sh->state, SH_PREPARING);
	bitmap_set(sh->slots, 0, nr);
	INIT_DELAYED_WORK(&sh->cleanup, shadow_cleanup);
	INIT_DELAYED_WORK(&sh->commit_rollback, shadow_commit_rollback_work);
	INIT_WORK(&sh->reclaim, shadow_reclaim_work);
	INIT_LIST_HEAD(&sh->preparing);
	mutex_init(&sh->backend_lock);
	/* Reserve nodes before PTL, so publication cannot sleep. */
	ret = xa_reserve(&sh_tokens, sh->token, GFP_KERNEL);
	if (ret)
		goto free_alloc;
	ret = xa_reserve(&sh_pfns, sh->pfn, GFP_KERNEL);
	if (ret)
		goto unreserve_token;
	ptes = pte_offset_map_lock(mm, pmd, addr, &ptl);
	ret = -EAGAIN;
	if (!ptes)
		goto unreserve_pfn;
	ptdesc = virt_to_ptdesc(ptes);
	for (i = 0; i < nr; i++) {
		pte_t pte = ptep_get(ptes + i);

		if (!pte_present(pte) || pte_protnone(pte) || pte_uffd_wp(pte) ||
		    vm_normal_page(vma, addr + i * PAGE_SIZE, pte) != folio_page(folio, i) ||
		    !PageAnonExclusive(folio_page(folio, i)) ||
		    (ptdesc->pt_shadow && ptdesc->pt_shadow[pte_index(addr) + i]))
			goto unmap;
	}
	if (!ptdesc->pt_shadow) {
		ptdesc->pt_shadow = metadata;
		set_page_private(virt_to_page(metadata), 0);
		atomic64_inc(&metadata_pages);
		metadata = NULL;
	}
	sh->table_pfn = page_to_pfn(virt_to_page(ptes));
	mmgrab(mm);
	atomic64_inc(&live_objects);
	atomic64_add(nr, &live_slots);
	WARN_ON_ONCE(xa_is_err(xa_store(&sh_pfns, sh->pfn, sh, GFP_NOWAIT)));
	WARN_ON_ONCE(xa_is_err(xa_store(&sh_tokens, sh->token, sh, GFP_NOWAIT)));
	for (i = 0; i < nr; i++) {
		unsigned long a = addr + i * PAGE_SIZE;
		struct page *meta_page = virt_to_page(ptdesc->pt_shadow);
		pte_t old = ptep_get_and_clear(mm, a, ptes + i);
		pte_t entry = swp_entry_to_pte(make_chameleon_shadow_entry(sh->pfn + i));

		sh->saved[i] = old;
		if (pte_dirty(old))
			folio_mark_dirty(folio);
		if (pte_soft_dirty(old))
			entry = pte_swp_mksoft_dirty(entry);
		ptdesc->pt_shadow[pte_index(a)] = SH_PENDING;
		set_page_private(meta_page, page_private(meta_page) + 1);
		set_pte_at(mm, a, ptes + i, entry);
	}
	/* Explicitly transfer C2's isolation charge to the private pool. The
	 * same folio reference remains owned until the last slot is retired. */
	node_stat_sub_folio(folio, NR_ISOLATED_ANON);
	pte_unmap_unlock(ptes, ptl);
	if (metadata)
		free_page((unsigned long)metadata);
	return sh;
unmap:
	pte_unmap_unlock(ptes, ptl);
unreserve_pfn:
	xa_release(&sh_pfns, sh->pfn);
unreserve_token:
	xa_release(&sh_tokens, sh->token);
free_alloc:
	if (metadata)
		free_page((unsigned long)metadata);
	kfree(sh);
	atomic64_sub(nr, &shadow_pages);
	atomic64_sub(nr, &owned_pages);
unlock:
	folio_unlock(folio);
	return ERR_PTR(ret);
}

static int shadow_prepare_owned(unsigned int maximum, u64 owner,
		struct mm_struct *target, unsigned long target_start,
		unsigned long target_end, unsigned long max_pages, u64 *tokens,
		struct chameleon_shadow_policy_result *result)
{
	LIST_HEAD(batch);
	LIST_HEAD(group);
	LIST_HEAD(transferred);
	LIST_HEAD(prepared);
	struct chameleon_candidate *c, *next;
	struct shadow *sh, *shnext;
	unsigned int successful = 0;
	unsigned long selected_pages = 0;
	int ret, error = -ENOENT;

	if (!maximum || maximum > SH_MAX_BATCH)
		return -EINVAL;
	mutex_lock(&prepare_lock);
	ret = target ? chameleon_select_batch_mm(maximum, target, target_start,
						target_end, &batch) :
		chameleon_select_batch(maximum, &batch);
	if (ret <= 0) {
		error = ret ? ret : -ENOENT;
		goto out;
	}
	list_for_each_entry_safe(c, next, &batch, batch) {
		unsigned long nr = 1UL << c->order;

		if (nr > max_pages - selected_pages)
			chameleon_candidate_putback(c);
		else
			selected_pages += nr;
	}
	while (!list_empty(&batch)) {
		struct mm_struct *mm = list_first_entry(&batch,
				struct chameleon_candidate, batch)->mm;
		unsigned long start = ULONG_MAX, end = 0;
		struct mmu_notifier_range range;

		list_for_each_entry_safe(c, next, &batch, batch) {
			if (c->mm != mm)
				continue;
			start = min(start, c->address);
			end = max(end, c->address + (PAGE_SIZE << c->order));
			list_move_tail(&c->batch, &group);
		}
		mmap_write_lock(mm);
		/* PMD demotion has its own native flush; it is never included in
		 * the one Shadow invalidation counted below for this mm group. */
		list_for_each_entry(c, &group, batch) {
			struct vm_area_struct *vma = vma_lookup(mm, c->address);
			pmd_t *pmd = shadow_pmd(mm, c->address);

			if (c->order != HPAGE_PMD_ORDER || !pmd ||
			    !pmd_trans_huge(*pmd) ||
			    !shadow_vma_ok(vma, c->address, c->order))
				continue;
			vma_start_write(vma);
			split_huge_pmd_address(vma, c->address, false);
			if (!pmd_trans_huge(*pmd))
				atomic64_inc(&prepare_pmd_demotions);
		}
		mmu_notifier_range_init(&range, MMU_NOTIFY_CLEAR, 0, mm, start, end);
		mmu_notifier_invalidate_range_start(&range);
		list_for_each_entry_safe(c, next, &group, batch) {
			sh = shadow_prepare_one(c, owner);
			if (IS_ERR(sh)) {
				error = PTR_ERR(sh);
				continue;
			}
			list_add_tail(&sh->preparing, &prepared);
			list_move_tail(&c->batch, &transferred);
		}
		if (!list_empty(&prepared)) {
			flush_tlb_mm(mm);
			atomic64_inc(&guest_flushes);
			atomic64_inc(&mm_groups);
			/* fast-GUP disables interrupts while reading PTEs. The sync
			 * waits out any reader that raced with replacing those PTEs. */
			tlb_remove_table_sync_one();
			atomic64_inc(&fast_gup_syncs);
			list_for_each_entry(sh, &prepared, preparing) {
				if (folio_maybe_dma_pinned(sh->folio) ||
				    folio_ref_count(sh->folio) !=
					folio_expected_ref_count(sh->folio) + 1) {
					shadow_restore_locked(sh);
					atomic64_inc(&pin_rollbacks);
					error = -EBUSY;
				} else {
					atomic_set(&sh->state, SH_RESIDENT);
					if (tokens)
						tokens[successful] = sh->token;
					if (result) {
						result->prepared_objects++;
						result->prepared_pages += 1UL << sh->order;
					}
					successful++;
				}
			}
		}
		mmu_notifier_invalidate_range_end(&range);
		list_for_each_entry_safe(sh, shnext, &prepared, preparing) {
			list_del_init(&sh->preparing);
			folio_unlock(sh->folio);
			shadow_put(sh);
		}
		mmap_write_unlock(mm);
		/* Last mm_users may run exit_mmap: never drop it under mmap lock. */
		list_for_each_entry_safe(c, next, &transferred, batch) {
			mmput(c->mm);
			list_del(&c->batch);
			kfree(c);
		}
		list_for_each_entry_safe(c, next, &group, batch)
			chameleon_candidate_putback(c);
	}
	if (successful)
		atomic64_inc(&prepare_batches);
out:
	mutex_unlock(&prepare_lock);
	return successful ? 0 : error;
}

static int shadow_prepare(unsigned int maximum)
{
	return shadow_prepare_owned(maximum, 0, NULL, 0, 0, ULONG_MAX, NULL, NULL);
}

int chameleon_shadow_policy_prepare(u64 owner, struct mm_struct *mm,
		unsigned long start, unsigned long end, unsigned int maximum,
		unsigned long max_pages, bool discard,
		struct chameleon_shadow_policy_result *result)
{
	u64 *tokens;
	unsigned int i;
	int ret, error = 0;

	if (!owner || !maximum || maximum > LL_CHAMELEON_MAX_RANGES ||
	    !max_pages || (discard && (!mm || !IS_ENABLED(CONFIG_CHAMELEON_TEST))))
		return -EINVAL;
	memset(result, 0, sizeof(*result));
	tokens = kcalloc(maximum, sizeof(*tokens), GFP_KERNEL);
	if (!tokens)
		return -ENOMEM;
	ret = shadow_prepare_owned(maximum, owner, mm, start, end,
				   max_pages, tokens, result);
	/* All mmap/folio/PTL and preparation locks are gone before transport.
	 * A concurrent fast restore can retire a token; never authorize its PFN
	 * by looking it up again as a different object. */
	if (!ret && discard) {
		for (i = 0; i < result->prepared_objects; i++) {
			int rc = shadow_discard(tokens[i]);

			if (!rc)
				rc = shadow_range_control(tokens[i], LL_CH_OP_READY);
			if (!rc)
				result->ready_objects++;
			else if (rc != -ESTALE)
				error = rc;
		}
	} else if (!ret && chameleon_data_available()) {
		bool capable;

		mutex_lock(&backend_lock);
		capable = backend_ops && backend_ops->load;
		mutex_unlock(&backend_lock);
		if (capable) {
			for (i = 0; i < result->prepared_objects; i++) {
				int rc = chameleon_shadow_save_submit(tokens[i]);

				if (rc && rc != -ESTALE)
					error = rc;
			}
		}
	}
	kfree(tokens);
	return ret ?: error;
}

int chameleon_shadow_policy_restore(u64 owner, unsigned int maximum,
		struct chameleon_shadow_policy_result *result)
{
	unsigned long index = 0;
	unsigned int processed = 0;
	int error = 0;

	memset(result, 0, sizeof(*result));
	for (;;) {
		struct shadow *sh;
		int ret;
		u64 nr;

		rcu_read_lock();
		sh = xa_find(&sh_tokens, &index, ULONG_MAX, XA_PRESENT);
		if (!sh) { rcu_read_unlock(); break; }
		if (!refcount_inc_not_zero(&sh->refs)) {
			rcu_read_unlock();
			goto next;
		}
		rcu_read_unlock();
		if (sh->policy_owner != owner || !READ_ONCE(sh->folio))
			goto put;
		if (processed++ >= maximum) {
			result->pending_objects++;
			goto put;
		}
		nr = 1UL << sh->order;
		if (READ_ONCE(sh->finalized) && !READ_ONCE(sh->data_saved)) {
			bool allocated;

			mutex_lock(&sh->backend_lock);
			allocated = sh->folio != NULL;
			ret = sh->discard_allowed && sh->host_registered ?
				shadow_install_locked(sh) : -EPERM;
			if (!ret && allocated)
				result->installed_pages += nr;
			mutex_unlock(&sh->backend_lock);
		} else {
			u64 restored = 0;

			ret = shadow_restore_token_pages(sh->token, &restored);
			if (!ret)
				result->restored_pages += restored;
		}
		if (!ret && READ_ONCE(sh->folio) && atomic_read(&sh->live))
			ret = -EAGAIN;
		if (ret && ret != -ESTALE) {
			error = ret;
			result->pending_objects++;
		}
put:
		shadow_put(sh);
next:
		if (index++ == ULONG_MAX)
			break;
	}
	return error;
}

int chameleon_shadow_backend_register(const struct chameleon_shadow_backend_ops *ops)
{
	int ret = 0;

	if (!ops || !ops->submit || !ops->cancel || (ops->load && !ops->owner))
		return -EINVAL;
	mutex_lock(&backend_lock);
	if (backend_ops)
		ret = -EBUSY;
	else
		WRITE_ONCE(backend_ops, ops);
	mutex_unlock(&backend_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(chameleon_shadow_backend_register);

int chameleon_shadow_backend_unregister(const struct chameleon_shadow_backend_ops *ops)
{
	int ret = 0;

	mutex_lock(&backend_lock);
	if (backend_ops != ops)
		ret = -EINVAL;
	else if (backend_users)
		ret = -EBUSY;
	else
		WRITE_ONCE(backend_ops, NULL);
	mutex_unlock(&backend_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(chameleon_shadow_backend_unregister);

int chameleon_shadow_save_submit(u64 token)
{
	struct shadow *sh = shadow_lookup(&sh_tokens, token);
	const struct chameleon_shadow_backend_ops *ops;
	int ret;

	if (!sh)
		return -ESTALE;
	mutex_lock(&sh->backend_lock);
	if (!atomic_read(&sh->live)) { ret = -ESTALE; goto unlock; }
	if (sh->host_registered || sh->finalized) { ret = -EBUSY; goto unlock; }
	if (sh->submit_active) { ret = -EBUSY; goto unlock; }
	if (atomic_read(&sh->state) == SH_FAILED) {
		ops = sh->backend;
		if (ops) {
			ops->cancel(token);
			sh->backend = NULL;
			mutex_lock(&backend_lock);
			backend_users--;
			mutex_unlock(&backend_lock);
			module_put(ops->owner);
		}
		atomic_cmpxchg(&sh->state, SH_FAILED, SH_RESIDENT);
	}
	if (sh->backend) { ret = -EALREADY; goto unlock; }
	if (atomic_read(&sh->state) != SH_RESIDENT) { ret = -EBUSY; goto unlock; }
	mutex_lock(&backend_lock);
	ops = backend_ops;
	if (!ops || !try_module_get(ops->owner)) {
		ret = -EOPNOTSUPP;
	} else {
		int old = atomic_cmpxchg(&sh->state, SH_RESIDENT, SH_SAVING);

		if (old != SH_RESIDENT) {
			ret = old == SH_RETIRED ? -ESTALE : -EBUSY;
			module_put(ops->owner);
		} else {
			backend_users++;
			WRITE_ONCE(sh->backend, ops);
			ret = 0;
		}
	}
	mutex_unlock(&backend_lock);
	if (ret)
		goto unlock;
	/* The lookup reference holds the source and backend lifetime. Do not
	 * hold backend_lock across submit: synchronous completion takes mmap
	 * read, while fault/explicit restore must take mmap before backend. */
	sh->submit_active = true;
	mutex_unlock(&sh->backend_lock);
	ret = ops->submit(token, sh->folio);
	mutex_lock(&sh->backend_lock);
	sh->submit_active = false;
	if (ret) {
		WRITE_ONCE(sh->backend, NULL);
		atomic_cmpxchg(&sh->state, SH_SAVING, SH_FAILED);
		mutex_lock(&backend_lock);
		backend_users--;
		mutex_unlock(&backend_lock);
		module_put(ops->owner);
	}
unlock:
	mutex_unlock(&sh->backend_lock);
	shadow_put(sh);
	return ret;
}
EXPORT_SYMBOL_GPL(chameleon_shadow_save_submit);

/* Sleepable completion. Only a backend with the full load contract queues
 * later authorization; an offset alone never replaces a resident mapping. */
int chameleon_shadow_save_complete(u64 token, int status, u64 offset)
{
	struct shadow *sh = shadow_lookup(&sh_tokens, token);
	pmd_t *pmd;
	pte_t *base;
	spinlock_t *ptl;
	unsigned int i;
	int ret = 0;

	if (!sh)
		return -ESTALE;
	if (!READ_ONCE(sh->backend)) { ret = -EOPNOTSUPP; goto put; }
	if (status) {
		int old;

		if (status > 0) { ret = -EINVAL; goto put; }
		old = atomic_cmpxchg(&sh->state, SH_SAVING, SH_FAILED);
		if (old != SH_SAVING)
			ret = old == SH_RETIRED ? -ESTALE : -EALREADY;
		else {
			WRITE_ONCE(sh->save_status, status);
			atomic64_inc(&data_save_failure);
		}
		goto put;
	}
	if (offset >= U64_MAX - (PAGE_SIZE << sh->order)) {
		ret = -EOVERFLOW;
		goto put;
	}
	if (!mmget_not_zero(sh->mm)) { ret = -ESTALE; goto put; }
	mmap_read_lock(sh->mm);
	pmd = shadow_pmd(sh->mm, sh->address);
	if (!pmd || pmd_trans_huge(*pmd)) { ret = -ESTALE; goto unlock; }
	base = pte_offset_map_lock(sh->mm, pmd, sh->address, &ptl);
	if (!base) { ret = -ESTALE; goto unlock; }
	if (!atomic_read(&sh->live)) {
		ret = -ESTALE;
	} else {
		int old = atomic_cmpxchg(&sh->state, SH_SAVING, SH_SAVED);
		u64 *slots = virt_to_ptdesc(base)->pt_shadow;

		if (old != SH_SAVING) {
			ret = old == SH_RETIRED ? -ESTALE : -EALREADY;
		} else {
			sh->saved_offset = offset;
			for (i = 0; i < (1U << sh->order); i++) {
				if (test_bit(i, sh->slots))
					slots[pte_index(sh->address) + i] = offset + i * PAGE_SIZE + 1;
			}
			WRITE_ONCE(sh->save_status, 1);
			atomic64_inc(&data_save_success);
		}
	}
	pte_unmap_unlock(base, ptl);
unlock:
	mmap_read_unlock(sh->mm);
	mmput(sh->mm);
	if (!ret && READ_ONCE(sh->backend) && READ_ONCE(sh->backend)->load) {
		/* A queued reference prevents cancel/free before authorization.
		 * The worker does no MM I/O until save_complete has dropped locks. */
		refcount_inc(&sh->refs);
		if (!queue_work(shadow_wq, &sh->reclaim))
			shadow_put(sh);
	}
put:
	shadow_put(sh);
	return ret;
}
EXPORT_SYMBOL_GPL(chameleon_shadow_save_complete);


static void shadow_wire(struct shadow *sh, struct ll_chameleon_range *range)
{
	*range = (struct ll_chameleon_range) {
		.token = cpu_to_le64(sh->token),
		.gpa = cpu_to_le64((u64)sh->pfn << PAGE_SHIFT),
		.nr_pages = cpu_to_le32(1U << sh->order),
		.order = cpu_to_le16(sh->order),
		.flags = cpu_to_le16(sh->data_saved ? LL_CH_RANGE_DATA_SAVED :
				    LL_CH_RANGE_DISCARD_TEST),
	};
}

static bool shadow_wire_matches(struct shadow *sh,
				const struct ll_chameleon_range *range)
{
	return le64_to_cpu(range->token) == sh->token &&
		le64_to_cpu(range->gpa) == (u64)sh->pfn << PAGE_SHIFT &&
		le32_to_cpu(range->nr_pages) == 1U << sh->order &&
		le16_to_cpu(range->order) == sh->order &&
		le16_to_cpu(range->flags) == (sh->data_saved ?
			LL_CH_RANGE_DATA_SAVED : LL_CH_RANGE_DISCARD_TEST);
}

/* Per-object backend_lock is also the C4 sleepable I/O mutex. It is never
 * acquired by PTL holders, and finalization does not hold it across MM locks. */
static void shadow_host_state(struct shadow *sh, u32 state)
{
	/* COMMIT_RESULT can sit behind an already completed INSTALL on the
	 * event queue, including an old ERROR result for a partial transaction.
	 * Once zeroed backing has been released, INSTALLED is terminal. */
	if ((sh->pool_released || sh->host_state == LL_CH_STATE_INSTALLED) &&
	    state != LL_CH_STATE_INSTALLED)
		return;
	WRITE_ONCE(sh->host_state, state);
	if (state == LL_CH_STATE_RETIRED && sh->folio && !sh->host_missing) {
		sh->host_missing = true;
		shadow_uncharge(sh);
		atomic64_add(1UL << sh->order, &host_reclaimed_pages);
		if (atomic_read(&sh->state) != SH_RETIRED)
			atomic_set(&sh->state, SH_RECLAIMED);
	}
}

static int shadow_host_request(struct shadow *sh, u16 op)
{
	struct ll_chameleon_range range;
	int ret;

	shadow_wire(sh, &range);
	ret = chameleon_transport_request(op, 0, &range, 1);
	if (ret)
		return ret;
	shadow_host_state(sh, le32_to_cpu(range.state));
	return -(int)le32_to_cpu(range.status);
}

static int shadow_host_forget(struct shadow *sh)
{
	if (sh->host_registered) {
		/* A range that was never ACKed cannot be released by the host.
		 * A finalized range reaches here only after INSTALL succeeded. */
		shadow_host_request(sh, LL_CH_OP_CANCEL);
		if (sh->policy_owner || sh->data_saved) {
			int ret = shadow_host_request(sh, LL_CH_OP_FORGET);

			if (ret && ret != -ESTALE)
				return ret;
		}
		sh->host_registered = false;
	}
	return 0;
}

static int shadow_install_locked(struct shadow *sh)
{
	struct folio *folio = sh->folio;
	unsigned int i, nr = 1U << sh->order;
	int ret;

	lockdep_assert_held(&sh->backend_lock);
	if (!sh->finalized)
		return -EINVAL;
	if (!folio)
		return 0;
	ret = shadow_host_request(sh, LL_CH_OP_INSTALL);
	if (ret || sh->host_state != LL_CH_STATE_INSTALLED) {
		atomic64_inc(&range_install_failure);
		return ret ? ret : -EIO;
	}
	/* The host ACK is the lifetime barrier for direct-map access. The
	 * reservation remains allocated until every byte has been zeroed. */
	folio_lock(folio);
	VM_BUG_ON_FOLIO(folio_mapped(folio) || folio_test_lru(folio), folio);
	for (i = 0; i < nr; i++) {
		clear_highpage(folio_page(folio, i));
		cond_resched();
	}
	folio_clear_active(folio);
	folio_unlock(folio);
	sh->folio = NULL;
	folio_put(folio);
	sh->pool_released = true;
	shadow_uncharge(sh);
	atomic64_sub(nr, &owned_pages);
	atomic64_sub(nr, &reservation_pages);
	if (sh->host_missing) {
		sh->host_missing = false;
		atomic64_sub(nr, &host_reclaimed_pages);
	}
	atomic64_add(nr, &zeroed_pages);
	atomic64_inc(&range_install_success);
	if (atomic_read(&sh->state) != SH_RETIRED)
		atomic_set(&sh->state, SH_RELEASED);
	return 0;
}

static int shadow_discard(u64 token)
{
	struct shadow *sh;
	int ret;

	if (!IS_ENABLED(CONFIG_CHAMELEON_TEST))
		return -EOPNOTSUPP;
	sh = shadow_lookup(&sh_tokens, token);
	if (!sh)
		return -ESTALE;
	mutex_lock(&sh->backend_lock);
	if (!atomic_read(&sh->live)) { ret = -ESTALE; goto unlock; }
	if (sh->backend || sh->finalized || sh->host_registered ||
	    atomic_read(&sh->state) != SH_RESIDENT) { ret = -EBUSY; goto unlock; }
	if (!chameleon_transport_available()) { ret = -EOPNOTSUPP; goto unlock; }
	sh->discard_allowed = true;
	/* Keep cancellation responsibility even if the reply is lost. */
	sh->host_registered = true;
	ret = shadow_host_request(sh, LL_CH_OP_REGISTER);
unlock:
	mutex_unlock(&sh->backend_lock);
	shadow_put(sh);
	return ret;
}

static int shadow_range_control(u64 token, u16 op)
{
	struct shadow *sh = shadow_lookup(&sh_tokens, token);
	int ret;

	if (!sh)
		return -ESTALE;
	if (op == LL_CH_OP_INSTALL && READ_ONCE(sh->data_saved)) {
		shadow_put(sh);
		return shadow_restore_token(token);
	}
	mutex_lock(&sh->backend_lock);
	if ((!sh->discard_allowed && !sh->data_saved) || !sh->host_registered) {
		ret = -EPERM;
	} else if (op == LL_CH_OP_INSTALL) {
		ret = shadow_install_locked(sh);
	} else if (op == LL_CH_OP_READY) {
		if (sh->finalized || atomic_read(&sh->state) !=
		    (sh->data_saved ? SH_SAVED : SH_RESIDENT))
			ret = -EBUSY;
		else {
			/* Publish eligibility before READY: its host event may run
			 * before the synchronous control reply is consumed. */
			WRITE_ONCE(sh->host_ready, true);
			ret = shadow_host_request(sh, op);
			if (ret)
				WRITE_ONCE(sh->host_ready, false);
		}
	} else {
		ret = shadow_host_request(sh, op);
	}
	mutex_unlock(&sh->backend_lock);
	shadow_put(sh);
	return ret;
}

/* PTL held. The token entry has no folio mapping reference and no RSS charge. */
void chameleon_reclaimed_zap(struct vm_area_struct *vma, unsigned long addr,
			    pte_t *ptep, pte_t old_pte)
{
	struct shadow *sh = shadow_lookup(&sh_tokens,
					 swp_offset(pte_to_swp_entry(old_pte)));

	if (WARN_ON_ONCE(!sh))
		return;
	if (WARN_ON_ONCE(!sh->finalized || sh->mm != vma->vm_mm ||
			addr < sh->address || addr >= sh->address + (PAGE_SIZE << sh->order))) {
		shadow_put(sh);
		return;
	}
	shadow_drop_slot(sh, virt_to_ptdesc(ptep), addr);
	atomic64_inc(&zapped_slots);
	shadow_put(sh);
}

/* mmap write and folio lock held. All source mapping references remain until
 * every PTE has been revalidated; no transport request is made in this scope. */
static int shadow_finalize_locked(struct shadow *sh)
{
	unsigned int i, nr = 1U << sh->order;
	struct vm_area_struct *vma = vma_lookup(sh->mm, sh->address);
	pmd_t *pmd = shadow_pmd(sh->mm, sh->address);
	spinlock_t *ptl;
	pte_t *base;
	int ret = -EBUSY;
	int expected = READ_ONCE(sh->data_saved) ? SH_SAVED : SH_RESIDENT;

	if (sh->finalized ||
	    (!READ_ONCE(sh->discard_allowed) && !READ_ONCE(sh->data_saved)) ||
	    !READ_ONCE(sh->host_registered) || !READ_ONCE(sh->host_ready) ||
	    atomic_read(&sh->state) != expected ||
	    atomic_read(&sh->live) != nr ||
	    !shadow_vma_ok(vma, sh->address, sh->order) || !pmd ||
	    pmd_trans_huge(*pmd) || folio_maybe_dma_pinned(sh->folio) ||
	    folio_maybe_mapped_shared(sh->folio) ||
	    folio_mapcount(sh->folio) != nr ||
	    folio_ref_count(sh->folio) != nr + 1)
		return ret;
	vma_start_write(vma);
	chameleon_lifecycle_begin();
	base = pte_offset_map_lock(sh->mm, pmd, sh->address, &ptl);
	if (!base) {
		chameleon_lifecycle_end();
		return -EAGAIN;
	}
	for (i = 0; i < nr; i++) {
		pte_t old = ptep_get(base + i);

		if (!test_bit(i, sh->slots) || !is_swap_pte(old) ||
		    !is_chameleon_shadow_entry(pte_to_swp_entry(old)) ||
		    swp_offset_pfn(pte_to_swp_entry(old)) != sh->pfn + i)
			goto unlock;
	}
	if (atomic_cmpxchg(&sh->state, expected, SH_FINALIZING) != expected)
		goto unlock;
	/* Fast restores and failed saves never reach this transition. The
	 * exact folio refs exclude queued PEBS pins; the lifecycle guard keeps
	 * cooling from crossing the vector update before its mappings vanish. */
	chameleon_swapout(sh->pfn, nr);
	WRITE_ONCE(sh->finalized, true);
	/* Reclaimed entries are token identities. Remove the PFN association
	 * now, before an installed/freed PFN can acquire a new Shadow owner. */
	xa_erase(&sh_pfns, sh->pfn);
	atomic64_add(nr, &reclaimed_slots);
	atomic64_add(nr, &reservation_pages);
	for (i = 0; i < nr; i++) {
		unsigned long addr = sh->address + i * PAGE_SIZE;
		pte_t old = ptep_get(base + i);
		pte_t entry = swp_entry_to_pte(make_chameleon_reclaimed_entry(sh->token));

		if (pte_swp_soft_dirty(old))
			entry = pte_swp_mksoft_dirty(entry);
		set_pte_at(sh->mm, addr, base + i, entry);
		folio_remove_rmap_pte(sh->folio, folio_page(sh->folio, i), vma);
	}
	add_mm_counter(sh->mm, MM_ANONPAGES, -(long)nr);
	folio_put_refs(sh->folio, nr);
	ret = 0;
unlock:
	pte_unmap_unlock(base, ptl);
	chameleon_lifecycle_end();
	return ret;
}

void chameleon_shadow_host_event(u16 op, u64 batch,
		const struct ll_chameleon_range *ranges, unsigned int nr)
{
	struct shadow **objects;
	struct ll_chameleon_range *ack;
	unsigned int i, j;

	if (!nr || nr > LL_CHAMELEON_MAX_RANGES)
		return;
	if (op == LL_CH_OP_COMMIT_RESULT) {
		for (i = 0; i < nr; i++) {
			struct shadow *sh = shadow_lookup(&sh_tokens, le64_to_cpu(ranges[i].token));

			if (!sh)
				continue;
			mutex_lock(&sh->backend_lock);
			if (sh->finalized && sh->host_batch == batch && shadow_wire_matches(sh, ranges + i)) {
				shadow_host_state(sh, le32_to_cpu(ranges[i].state));
				/* BEGIN can refuse a finalized object without discarding
				 * its Host backing. It still has Reclaimed PTEs and owns
				 * saved data and a pending Shadow credit. Recover the
				 * complete object, rather than leaking that credit until
				 * an application fault. INSTALL/load must not block this
				 * event worker: it also delivers the ACKs they need. */
				if (le32_to_cpu(ranges[i].state) == LL_CH_STATE_INSTALLED &&
				    le32_to_cpu(ranges[i].status) && sh->data_saved &&
				    sh->backend && sh->backend->load && sh->folio &&
				    atomic_read(&sh->live) && !sh->pool_released &&
				    !sh->commit_rollback_active) {
					sh->commit_rollback_active = true;
					refcount_inc(&sh->refs);
					queue_delayed_work(shadow_wq, &sh->commit_rollback, 0);
				}
			}
			mutex_unlock(&sh->backend_lock);
			shadow_put(sh);
		}
		return;
	}
	if (op != LL_CH_OP_FINALIZE_REQUEST)
		return;
	objects = kcalloc(nr, sizeof(*objects), GFP_KERNEL);
	ack = kmemdup(ranges, nr * sizeof(*ranges), GFP_KERNEL);
	if (!objects || !ack)
		goto free;
	mutex_lock(&prepare_lock);
	for (i = 0; i < nr; i++) {
		struct shadow *sh = shadow_lookup(&sh_tokens, le64_to_cpu(ranges[i].token));

		ack[i].status = cpu_to_le32(ESTALE);
		if (!sh)
			continue;
		if (!shadow_wire_matches(sh, ranges + i) ||
		    !READ_ONCE(sh->host_ready) || !mmget_not_zero(sh->mm)) {
			shadow_put(sh);
			continue;
		}
		objects[i] = sh;
	}
	for (i = 0; i < nr; i++) {
		struct mm_struct *mm;
		struct mmu_notifier_range range;
		bool changed = false;

		if (!objects[i])
			continue;
		mm = objects[i]->mm;
		/* Earlier groups have set their processed status away from ESTALE. */
		if (le32_to_cpu(ack[i].status) != ESTALE)
			continue;
		mmap_write_lock(mm);
		mmu_notifier_range_init(&range, MMU_NOTIFY_CLEAR, 0, mm, 0, TASK_SIZE_MAX);
		mmu_notifier_invalidate_range_start(&range);
		for (j = i; j < nr; j++) {
			struct shadow *sh = objects[j];
			int ret = -EBUSY;

			if (!sh || sh->mm != mm || le32_to_cpu(ack[j].status) != ESTALE)
				continue;
			if (!READ_ONCE(sh->finalized) && sh->folio && folio_trylock(sh->folio)) {
				ret = shadow_finalize_locked(sh);
				if (!ret) {
					WRITE_ONCE(sh->host_batch, batch);
					changed = true;
				}
				folio_unlock(sh->folio);
			}
			ack[j].status = cpu_to_le32(-ret);
			if (!ret)
				atomic64_inc(&finalize_accepted);
			else
				atomic64_inc(&finalize_rejected);
		}
		if (changed) {
			flush_tlb_mm(mm);
			atomic64_inc(&finalize_mm_groups);
			atomic64_inc(&finalize_guest_flushes);
		}
		mmu_notifier_invalidate_range_end(&range);
		mmap_write_unlock(mm);
	}
	/* The host may only BEGIN after this ACK. Every affected mm has
	 * completed its second real guest flush, and all MM locks are gone. */
	chameleon_transport_request(LL_CH_OP_FINALIZE_ACK, batch, ack, nr);
	for (i = 0; i < nr; i++) {
		if (objects[i]) {
			mmput(objects[i]->mm);
			shadow_put(objects[i]);
		}
	}
	mutex_unlock(&prepare_lock);
free:
	kfree(ack);
	kfree(objects);
}
EXPORT_SYMBOL_GPL(chameleon_shadow_host_event);

/* mmap read/write held, never PTL. The reserved folio has retained its anon
 * identity and original physical pages, but none may be accessed until the
 * host has completed INSTALL. A failed load leaves every token PTE intact. */
static int shadow_restore_data_accounted(struct shadow *sh, u64 *pages,
		struct vm_fault *vmf, bool *major)
{
	struct folio *folio;
	pmd_t *pmd;
	pte_t *base;
	spinlock_t *ptl;
	unsigned int i, attempt, nr = 1U << sh->order, restored = 0;
	unsigned long psi_flags = 0;
	u64 psi_started = 0;
	bool stalled = false, tracking_locked = false;
	int ret;

	mmap_assert_locked(sh->mm);
	if (!READ_ONCE(sh->data_saved))
		return -EOPNOTSUPP;
	/* A competing demand fault waits for the same unavailable data, even
	 * when another fault performs its single read. Account that task's
	 * memory stall too; explicit/policy restores never enter demand PSI. */
	if (vmf) {
		if (!mutex_trylock(&sh->backend_lock)) {
			atomic64_inc(&demand_waiters);
			psi_started = ktime_get_ns();
			psi_memstall_enter(&psi_flags);
			atomic64_inc(&psi_fault_enter);
			stalled = true;
			mutex_lock(&sh->backend_lock);
		}
	} else {
		mutex_lock(&sh->backend_lock);
	}
	if (sh->pool_released) { ret = 0; goto unlock; }
	if (!sh->backend || !sh->backend->load || !sh->finalized ||
	    !sh->folio || !atomic_read(&sh->live)) {
		ret = -ESTALE;
		goto unlock;
	}
	if (vmf && !stalled) {
		psi_started = ktime_get_ns();
		psi_memstall_enter(&psi_flags);
		atomic64_inc(&psi_fault_enter);
		stalled = true;
	}
	folio = sh->folio;
	/* A fault may reach the token just after the second Guest flush but
	 * before the event worker's FINALIZE_ACK has reached the host. That
	 * transient overlap is not a failed data read; let ACK make progress. */
	for (attempt = 0; ; attempt++) {
		ret = shadow_host_request(sh, LL_CH_OP_INSTALL);
		if ((ret != -EBUSY && ret != -EAGAIN) || attempt == 999)
			break;
		usleep_range(1000, 2000);
	}
	if (ret || sh->host_state != LL_CH_STATE_INSTALLED) {
		atomic64_inc(&range_install_failure);
		ret = ret ?: -EIO;
		goto unlock;
	}
	if (sh->host_missing) {
		sh->host_missing = false;
		atomic64_sub(nr, &host_reclaimed_pages);
	}
	atomic64_inc(&range_install_success);
	folio_lock(folio);
	VM_BUG_ON_FOLIO(folio_mapped(folio) || folio_test_lru(folio), folio);
	atomic64_inc(&load_attempts);
	if (vmf) {
		/* Like do_swap_page(), count the initiating demand read before
		 * knowing whether I/O succeeds. A concurrent waiter does not
		 * submit another read or increment PGMAJFAULT a second time.
		 * Error returns remain SIGBUS, so task maj_flt and PGMAJFAULT
		 * intentionally differ for failed reads, as for native swap. */
		*major = true;
		atomic64_inc(&demand_major_faults);
		atomic64_inc(&load_demand_attempts);
		count_vm_event(PGMAJFAULT);
		count_memcg_event_mm(vmf->vma->vm_mm, PGMAJFAULT);
	} else {
		atomic64_inc(&load_background_attempts);
	}
	ret = sh->backend->load(sh->token, sh->saved_offset, folio);
	if (ret) {
		atomic64_inc(&data_load_failure);
		goto unlock_folio;
	}
	atomic64_inc(&data_load_success);
	pmd = shadow_pmd(sh->mm, sh->address);
	if (!pmd || pmd_trans_huge(*pmd)) { ret = -ESTALE; goto unlock_folio; }
	chameleon_lifecycle_begin();
	tracking_locked = true;
	base = pte_offset_map_lock(sh->mm, pmd, sh->address, &ptl);
	if (!base) { ret = -EAGAIN; goto unlock_folio; }
	/* Check the entire live subset before publishing even its first page. */
	for (i = 0; i < nr; i++) {
		unsigned long addr = sh->address + i * PAGE_SIZE;
		struct vm_area_struct *vma;
		pte_t old;

		if (!test_bit(i, sh->slots))
			continue;
		old = ptep_get(base + i);
		vma = vma_lookup(sh->mm, addr);
		if (!vma || !vma_is_anonymous(vma) || !vma->anon_vma ||
		    !is_swap_pte(old) ||
		    !is_chameleon_reclaimed_entry(pte_to_swp_entry(old)) ||
		    swp_offset(pte_to_swp_entry(old)) != sh->token) {
			ret = -ESTALE;
			goto unmap;
		}
	}
	chameleon_swapin(sh->pfn, nr, sh->slots);
	folio_mark_uptodate(folio);
	for (i = 0; i < nr; i++) {
		unsigned long addr = sh->address + i * PAGE_SIZE;
		struct vm_area_struct *vma;
		struct page *page;
		pte_t old, pte;

		if (!test_bit(i, sh->slots))
			continue;
		vma = vma_lookup(sh->mm, addr);
		old = ptep_get(base + i);
		page = folio_page(folio, i);
		pte = pte_wrprotect(pte_mkold(mk_pte(page, vma->vm_page_prot)));
		if (pte_young(sh->saved[i]))
			pte = pte_mkyoung(pte);
		if (pte_dirty(sh->saved[i]))
			pte = pte_mkdirty(pte);
		if (pte_swp_soft_dirty(old))
			pte = pte_mksoft_dirty(pte);
		else
			pte = pte_clear_soft_dirty(pte);
		if (pte_swp_uffd_wp(old))
			pte = pte_mkuffd_wp(pte);
		folio_get(folio);
		folio_add_anon_rmap_pte(folio, page, vma, addr, RMAP_EXCLUSIVE);
		if (pte_write(sh->saved[i]) && (vma->vm_flags & VM_WRITE) &&
		    can_change_pte_writable(vma, addr, pte))
			pte = pte_mkwrite(pte, vma);
		add_mm_counter(sh->mm, MM_ANONPAGES, 1);
		flush_icache_page(vma, page);
		set_pte_at(sh->mm, addr, base + i, pte);
		update_mmu_cache(vma, addr, base + i);
		shadow_drop_slot(sh, virt_to_ptdesc(base), addr);
		restored++;
	}
	sh->pool_released = true;
	shadow_uncharge(sh);
	atomic64_sub(nr, &owned_pages);
	atomic64_sub(nr, &reservation_pages);
	atomic64_add(restored, &data_restored_pages);
	if (pages)
		*pages = restored;
	ret = 0;
unmap:
	pte_unmap_unlock(base, ptl);
unlock_folio:
	if (tracking_locked)
		chameleon_lifecycle_end();
	folio_unlock(folio);
unlock:
	mutex_unlock(&sh->backend_lock);
	if (stalled) {
		psi_memstall_leave(&psi_flags);
		atomic64_inc(&psi_fault_leave);
		atomic64_add(ktime_get_ns() - psi_started, &psi_fault_ns);
	}
	return ret;
}

static int shadow_restore_data(struct shadow *sh, u64 *pages)
{
	return shadow_restore_data_accounted(sh, pages, NULL, NULL);
}

vm_fault_t chameleon_reclaimed_fault(struct vm_fault *vmf)
{
	u64 token = swp_offset(pte_to_swp_entry(vmf->orig_pte));
	struct shadow *sh = shadow_lookup(&sh_tokens, token);
	u64 restored = 0;
	bool major = false;
	int ret;

	atomic64_inc(&demand_faults);
	if (!sh) {
		atomic64_inc(&demand_fault_successes);
		return 0; /* Another fault has already restored this token. */
	}
	if (sh->mm != vmf->vma->vm_mm || vmf->address < sh->address ||
	    vmf->address >= sh->address + (PAGE_SIZE << sh->order)) {
		shadow_put(sh);
		atomic64_inc(&demand_fault_failures);
		return VM_FAULT_SIGBUS;
	}
	ret = shadow_restore_data_accounted(sh, &restored, vmf, &major);
	if (!ret && restored)
		atomic64_inc(&data_fault_restores);
	shadow_put(sh);
	atomic64_inc(ret ? &demand_fault_failures : &demand_fault_successes);
	/* Failed reads never make any PTE present. Users may retry the retained
	 * token explicitly after resolving a transport/backend failure. */
	return ret ? VM_FAULT_SIGBUS : major ? VM_FAULT_MAJOR : 0;
}

static int shadow_restore_token_pages(u64 token, u64 *pages)
{
	struct shadow *sh = shadow_lookup(&sh_tokens, token);
	unsigned int restored;
	int ret = 0;

	if (!sh)
		return -ESTALE;
	if (!mmget_not_zero(sh->mm)) { ret = -ESTALE; goto put; }
	mmap_write_lock(sh->mm);
	/* FINALIZE may have won while this caller waited for mmap_lock. The
	 * subsequent INSTALL is allowed to release sh->folio, so the check must
	 * precede even taking the folio lock, under the same exclusion as FINALIZE.
	 */
	if (READ_ONCE(sh->finalized)) {
		shadow_start_vmas(sh);
		ret = shadow_restore_data(sh, pages);
		goto unlock;
	}
	shadow_start_vmas(sh);
	folio_lock(sh->folio);
	restored = shadow_restore_locked(sh);
	if (restored)
		atomic64_inc(&explicit_restores);
	if (pages)
		*pages = restored;
	folio_unlock(sh->folio);
unlock:
	mmap_write_unlock(sh->mm);
	mmput(sh->mm);
put:
	shadow_put(sh);
	return ret;
}

static int shadow_restore_token(u64 token)
{
	return shadow_restore_token_pages(token, NULL);
}

static void shadow_commit_rollback_work(struct work_struct *work)
{
	struct shadow *sh = container_of(to_delayed_work(work), struct shadow,
					commit_rollback);
	u64 pages = 0;
	int ret;

	mutex_lock(&sh->backend_lock);
	if (sh->pool_released || !sh->folio || !atomic_read(&sh->live)) {
		atomic64_inc(&commit_rollback_skipped);
		goto done;
	}
	mutex_unlock(&sh->backend_lock);
	/* Reuse the normal mmap -> backend -> folio -> PTL ordering. A fault
	 * or policy restore may win the race; backend_lock prevents two loads.
	 * The work reference survives registry removal, unmap and mm exit. */
	atomic64_inc(&commit_rollback_attempts);
	ret = shadow_restore_token_pages(sh->token, &pages);
	mutex_lock(&sh->backend_lock);
	if (!ret && pages)
		atomic64_inc(&commit_rollback_success);
	else if (ret)
		atomic64_inc(&commit_rollback_failures);
	else
		atomic64_inc(&commit_rollback_skipped);
	if (ret && !sh->pool_released && sh->folio && atomic_read(&sh->live)) {
		/* Keep the token, saved object and work reference after a failed
		 * INSTALL/read. Never publish partial data or silently uncharge
		 * still-owned backing. A later retry, fault or unmap resolves it. */
		queue_delayed_work(shadow_wq, &sh->commit_rollback, HZ);
		mutex_unlock(&sh->backend_lock);
		return;
	}
done:
	sh->commit_rollback_active = false;
	mutex_unlock(&sh->backend_lock);
	shadow_put(sh);
}

static int shadow_commit(u64 token)
{
	struct shadow *sh = shadow_lookup(&sh_tokens, token);
	int ret;

	if (!sh)
		return -ESTALE;
	mutex_lock(&sh->backend_lock);
	if (!sh->backend || !sh->backend->load || !chameleon_data_available()) {
		ret = -EOPNOTSUPP;
		goto unlock;
	}
	if (sh->discard_allowed || sh->finalized ||
	    atomic_read(&sh->state) != SH_SAVED || sh->save_status != 1 ||
	    atomic_read(&sh->live) != 1U << sh->order) {
		ret = -EBUSY;
		goto unlock;
	}
	sh->data_saved = true;
	sh->host_registered = true; /* Also owns cancellation on a lost reply. */
	ret = shadow_host_request(sh, LL_CH_OP_REGISTER);
	if (ret)
		goto unlock;
	WRITE_ONCE(sh->host_ready, true);
	ret = shadow_host_request(sh, LL_CH_OP_READY);
	if (ret)
		WRITE_ONCE(sh->host_ready, false);
	else
		atomic64_inc(&data_ready_objects);
unlock:
	mutex_unlock(&sh->backend_lock);
	shadow_put(sh);
	return ret;
}

static void shadow_reclaim_work(struct work_struct *work)
{
	struct shadow *sh = container_of(work, struct shadow, reclaim);

	shadow_commit(sh->token);
	shadow_put(sh);
}

static int shadow_forget_test(u64 token)
{
	struct ll_chameleon_range range;
	struct shadow *sh;
	int ret;

	if (!IS_ENABLED(CONFIG_CHAMELEON_TEST))
		return -EOPNOTSUPP;
	sh = shadow_lookup(&sh_tokens, token);
	if (!sh)
		return -ESTALE;
	mutex_lock(&sh->backend_lock);
	/* INSTALL can have succeeded while LOAD is still retryable. Keep the
	 * host identity until valid bytes have been published or the object is
	 * abandoned by normal cleanup; this debug probe must not strand it. */
	if (sh->data_saved && !sh->pool_released) {
		ret = -EBUSY;
		goto unlock;
	}
	shadow_wire(sh, &range);
	ret = chameleon_transport_request(LL_CH_OP_FORGET, 0, &range, 1);
	if (!ret)
		ret = -(int)le32_to_cpu(range.status);
	/* This explicit wire-boundary test does not change local ownership or
	 * fabricate a terminal state for a record that the host rejected. */
unlock:
	mutex_unlock(&sh->backend_lock);
	shadow_put(sh);
	return ret;
}

static ssize_t shadow_control_write(struct file *file, const char __user *buffer,
				    size_t count, loff_t *position)
{
	char command[96], op[24], extra;
	unsigned long long value;
	int fields, ret;

	if (!count || count >= sizeof(command))
		return -EINVAL;
	if (copy_from_user(command, buffer, count))
		return -EFAULT;
	command[count] = 0;
	fields = sscanf(command, "%23s %llu %c", op, &value, &extra);
	if (fields == 1 && !strcmp(op, "drain")) {
		flush_workqueue(shadow_wq);
		rcu_barrier();
		return count;
	}
	if (fields != 2)
		return -EINVAL;
	if (!strcmp(op, "prepare"))
		ret = value > SH_MAX_BATCH ? -EINVAL : shadow_prepare(value);
	else if (!strcmp(op, "restore") || !strcmp(op, "cancel"))
		ret = shadow_restore_token(value);
	else if (!strcmp(op, "discard"))
		ret = shadow_discard(value);
	else if (!strcmp(op, "ready"))
		ret = shadow_range_control(value, LL_CH_OP_READY);
	else if (!strcmp(op, "install"))
		ret = shadow_range_control(value, LL_CH_OP_INSTALL);
	else if (!strcmp(op, "query"))
		ret = shadow_range_control(value, LL_CH_OP_QUERY);
	else if (!strcmp(op, "forget"))
		ret = shadow_forget_test(value);
	else if (!strcmp(op, "submit"))
		ret = chameleon_shadow_save_submit(value);
	else if (!strcmp(op, "commit"))
		ret = shadow_commit(value);
	else
		ret = -EINVAL;
	return ret ? ret : count;
}

static const struct file_operations shadow_control_fops = {
	.owner = THIS_MODULE,
	.write = shadow_control_write,
	.llseek = noop_llseek,
};

static int shadow_stats_show(struct seq_file *seq, void *unused)
{
#define SH_STAT(name) seq_printf(seq, #name " %lld\n", atomic64_read(&name))
	SH_STAT(live_objects);
	SH_STAT(live_slots);
	SH_STAT(metadata_pages);
	SH_STAT(shadow_pages);
	SH_STAT(owned_pages);
	SH_STAT(prepare_batches);
	SH_STAT(fault_restores);
	SH_STAT(explicit_restores);
	SH_STAT(zapped_slots);
	SH_STAT(mm_groups);
	SH_STAT(guest_flushes);
	SH_STAT(prepare_pmd_demotions);
	SH_STAT(fast_gup_syncs);
	SH_STAT(pin_rollbacks);
	SH_STAT(finalize_mm_groups);
	SH_STAT(finalize_guest_flushes);
	SH_STAT(finalize_accepted);
	SH_STAT(finalize_rejected);
	SH_STAT(reclaimed_slots);
	SH_STAT(reservation_pages);
	SH_STAT(host_reclaimed_pages);
	SH_STAT(range_install_success);
	SH_STAT(range_install_failure);
	SH_STAT(zeroed_pages);
	SH_STAT(cleanup_retries);
	SH_STAT(data_save_success);
	SH_STAT(data_save_failure);
	SH_STAT(data_ready_objects);
	SH_STAT(data_load_success);
	SH_STAT(data_load_failure);
	SH_STAT(data_restored_pages);
	SH_STAT(data_fault_restores);
	SH_STAT(demand_faults);
	SH_STAT(demand_fault_successes);
	SH_STAT(demand_fault_failures);
	SH_STAT(demand_major_faults);
	SH_STAT(demand_waiters);
	SH_STAT(psi_fault_enter);
	SH_STAT(psi_fault_leave);
	SH_STAT(psi_fault_ns);
	SH_STAT(load_attempts);
	SH_STAT(load_demand_attempts);
	SH_STAT(load_background_attempts);
	SH_STAT(commit_rollback_attempts);
	SH_STAT(commit_rollback_success);
	SH_STAT(commit_rollback_failures);
	SH_STAT(commit_rollback_skipped);
#undef SH_STAT
	seq_printf(seq, "transport_available %u\n", chameleon_transport_available());
	seq_printf(seq, "data_transport_available %u\n", chameleon_data_available());
	seq_printf(seq, "backend_present %u\npool_limit_pages %lu\n",
		   READ_ONCE(backend_ops) != NULL, pool_limit());
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(shadow_stats);

static int shadow_entries_show(struct seq_file *seq, void *unused)
{
	struct shadow *sh;
	unsigned long index;

	rcu_read_lock();
	xa_for_each(&sh_tokens, index, sh) {
		seq_printf(seq, "entry token=%llu pid=%d address=0x%lx pfn=%lu order=%u live_slots=%d table_pfn=%lu slot=%lu\n",
			   sh->token, sh->pid, sh->address, sh->pfn, sh->order,
			   atomic_read(&sh->live), sh->table_pfn,
			   pte_index(sh->address));
	}
	rcu_read_unlock();
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(shadow_entries);

/* Diagnostic offset view. Each record is sampled under its PTL; no virtual
 * kernel address is exposed and no metadata pointer escapes its RCU lifetime. */
static int shadow_offsets_show(struct seq_file *seq, void *unused)
{
	unsigned long index = 0;
	struct shadow *sh;
	u64 *values = kmalloc_array(HPAGE_PMD_NR, sizeof(*values), GFP_KERNEL);

	if (!values)
		return -ENOMEM;
	for (;;) {
		unsigned int i, nr = 0;
		pmd_t *pmd;
		pte_t *base;
		spinlock_t *ptl;

		rcu_read_lock();
		sh = xa_find(&sh_tokens, &index, ULONG_MAX, XA_PRESENT);
		if (!sh) {
			rcu_read_unlock();
			break;
		}
		if (!refcount_inc_not_zero(&sh->refs)) {
			rcu_read_unlock();
			if (index++ == ULONG_MAX)
				break;
			continue;
		}
		rcu_read_unlock();
		if (!mmget_not_zero(sh->mm))
			goto put_offset;
		mmap_read_lock(sh->mm);
		pmd = shadow_pmd(sh->mm, sh->address);
		if (!pmd || pmd_trans_huge(*pmd))
			goto unlock_offset;
		base = pte_offset_map_lock(sh->mm, pmd, sh->address, &ptl);
		if (!base)
			goto unlock_offset;
		if (atomic_read(&sh->live)) {
			u64 *slots = virt_to_ptdesc(base)->pt_shadow;

			nr = 1U << sh->order;
			for (i = 0; i < nr; i++)
				values[i] = slots[pte_index(sh->address) + i];
		}
		pte_unmap_unlock(base, ptl);
	unlock_offset:
		mmap_read_unlock(sh->mm);
		mmput(sh->mm);
		for (i = 0; i < nr; i++) {
			if (values[i])
				seq_printf(seq, "slot token=%llu address=0x%lx value=%llu\n",
					   sh->token, sh->address + i * PAGE_SIZE, values[i]);
		}
	put_offset:
		shadow_put(sh);
		if (index++ == ULONG_MAX)
			break;
	}
	kfree(values);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(shadow_offsets);

static int shadow_states_show(struct seq_file *seq, void *unused)
{
	struct shadow *sh;
	unsigned long index;

	rcu_read_lock();
	xa_for_each(&sh_tokens, index, sh) {
		seq_printf(seq, "state token=%llu kind=%s phase=%d authorized=%u registered=%u ready=%u host_state=%u reserved=%u batch=%llu data_saved=%u saved_offset=%llu\n",
			sh->token, READ_ONCE(sh->finalized) ? "reclaimed" : "shadow",
			atomic_read(&sh->state), READ_ONCE(sh->discard_allowed) || READ_ONCE(sh->data_saved),
			READ_ONCE(sh->host_registered), READ_ONCE(sh->host_ready),
			READ_ONCE(sh->host_state),
			READ_ONCE(sh->finalized) && READ_ONCE(sh->folio) != NULL,
			READ_ONCE(sh->host_batch), READ_ONCE(sh->data_saved),
			READ_ONCE(sh->saved_offset));
	}
	rcu_read_unlock();
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(shadow_states);

static int __init chameleon_shadow_init(void)
{
	struct dentry *dir;

	BUILD_BUG_ON(PTRS_PER_PTE * sizeof(u64) != PAGE_SIZE);
	shadow_wq = alloc_workqueue("chameleon_shadow", WQ_UNBOUND | WQ_MEM_RECLAIM, 0);
	if (!shadow_wq)
		return -ENOMEM;
	dir = debugfs_create_dir("chameleon_shadow", NULL);
	debugfs_create_file("control", 0600, dir, NULL, &shadow_control_fops);
	debugfs_create_file("stats", 0400, dir, NULL, &shadow_stats_fops);
	debugfs_create_file("entries", 0400, dir, NULL, &shadow_entries_fops);
	debugfs_create_file("offsets", 0400, dir, NULL, &shadow_offsets_fops);
	debugfs_create_file("states", 0400, dir, NULL, &shadow_states_fops);
	return 0;
}
late_initcall(chameleon_shadow_init);
