// SPDX-License-Identifier: GPL-2.0-only
/* Chameleon C2: real folio granularity, per-order LRU and cost selection. */
#include <linux/chameleon.h>
#include <linux/debugfs.h>
#include <linux/jiffies.h>
#include <linux/math64.h>
#include <linux/memcontrol.h>
#include <linux/mm.h>
#include <linux/huge_mm.h>
#include <linux/mm_inline.h>
#include <linux/mutex.h>
#include <linux/overflow.h>
#include <linux/pagewalk.h>
#include <linux/pid.h>
#include <linux/rmap.h>
#include <linux/sched/mm.h>
#include <linux/sched/cputime.h>
#include <linux/sort.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/swap.h>
#include <linux/uaccess.h>
#include <linux/userfaultfd_k.h>
#include <linux/vmalloc.h>
#include <linux/workqueue.h>
#include "internal.h"

#define CH_MAX_SEGMENTS 12
#define CH_SCAN 128
#define CH_SCORE_SCALE 1024ULL
#define CH_SUPPORTED ((1U << CHAMELEON_ORDERS) - 1 - BIT(1))

/* No folio reference is owned by an index entry. All links use lru_lock. */
struct ch_lru_entry {
	struct list_head link;
	struct lruvec *owner;
	u8 order, active;
};
static struct ch_lru_entry *index_entries;
static unsigned long index_pfns;
static DEFINE_MUTEX(manager_lock);
/* Serialize disable/cancel against a concurrent debugfs enable. */
static DEFINE_MUTEX(command_lock);
static bool manager_enabled;
static bool memtis_split, linux_lru;
static unsigned int hhh_dominance_permille = 700;
static unsigned int maintenance_interval_ms = 5000;
/* Independent of the fixed CH_SCAN candidate/allocation safety bound. */
static unsigned int scan_folios = CH_SCAN;
static bool ptw_auto = true;
static unsigned int memtis_min_bin = 20, memtis_budget = 32;
static u64 mode_changes, selected_mixed, selected_native, native_scanned;
static u64 memtis_considered, memtis_qualified, memtis_splits, memtis_pages;
static u64 split_cpu_ns, select_cpu_ns;
static unsigned int scan_first_order;
static struct mm_struct *target_mm;
static unsigned long target_start, target_end;
static pid_t target_pid;
static LIST_HEAD(held_candidates);
static u64 next_token;
static u64 epochs, scanned, split_ok, split_busy, collapse_ok, collapse_fail;
static u64 promoted, aged, selected;
static atomic64_t returned = ATOMIC64_INIT(0);

struct ch_segment { unsigned int offset, order; bool dominant; };
struct ch_plan {
	struct ch_segment segment[CH_MAX_SEGMENTS];
	unsigned int count, original_order, dominant_offset, dominant_order;
};
static struct {
	char operation[16];
	int error;
	bool split_changed, uniform_base;
	unsigned int memtis_bin, hot_subpages;
	unsigned long pfn;
	struct ch_plan plan;
} last;
static struct {
	u64 reclaim[CHAMELEON_ORDERS];
	u64 sync, entries, active, ptw, explicit_ptw;
	unsigned int batch, mask;
	bool valid, measured_ptw;
} cost;

static bool supported_order(unsigned int order)
{
	return order < CHAMELEON_ORDERS && (CH_SUPPORTED & BIT(order));
}

void chameleon_lru_del(struct lruvec *lruvec, struct folio *folio)
{
	struct ch_lru_entry *entry;
	unsigned long pfn = folio_pfn(folio);

	if (!smp_load_acquire(&index_entries) || pfn >= index_pfns)
		return;
	lockdep_assert_held(&lruvec->lru_lock);
	entry = &index_entries[pfn];
	if (!entry->owner)
		return;
	if (WARN_ON_ONCE(entry->owner != lruvec))
		return;
	if (lruvec->chameleon_cursor[entry->active][entry->order] == &entry->link)
		lruvec->chameleon_cursor[entry->active][entry->order] = entry->link.next;
	list_del_init(&entry->link);
	lruvec->chameleon_count[entry->active][entry->order]--;
	entry->owner = NULL;
}

void chameleon_lru_add(struct lruvec *lruvec, struct folio *folio, bool tail)
{
	struct ch_lru_entry *entry;
	unsigned long pfn = folio_pfn(folio);
	unsigned int order = folio_order(folio), active = folio_test_active(folio);

	if (!smp_load_acquire(&index_entries) || pfn >= index_pfns)
		return;
	lockdep_assert_held(&lruvec->lru_lock);
	chameleon_lru_del(lruvec, folio);
	if (!supported_order(order) || !folio_test_anon(folio) ||
	    !folio_test_swapbacked(folio) || folio_test_unevictable(folio))
		return;
	entry = &index_entries[pfn];
	entry->owner = lruvec;
	entry->order = order;
	entry->active = active;
	if (tail)
		list_add_tail(&entry->link, &lruvec->chameleon_lists[active][order]);
	else
		list_add(&entry->link, &lruvec->chameleon_lists[active][order]);
	lruvec->chameleon_count[active][order]++;
}

/* Native split inserts children at the old folio's position, not at either
 * end. Preserve the per-order subsequence there. This slow path is used only
 * for split; usual add/del/requeue hooks remain constant time. */
void chameleon_lru_add_split(struct lruvec *lruvec, struct folio *folio)
{
	struct ch_lru_entry *entry, *previous;
	struct list_head *head, *pos, *where;
	unsigned long pfn = folio_pfn(folio);

	if (!smp_load_acquire(&index_entries) || pfn >= index_pfns)
		return;
	chameleon_lru_add(lruvec, folio, false);
	entry = &index_entries[pfn];
	if (!entry->owner)
		return;
	head = &lruvec->lists[folio_lru_list(folio)];
	where = &lruvec->chameleon_lists[entry->active][entry->order];
	for (pos = folio->lru.prev; pos != head; pos = pos->prev) {
		struct folio *neighbor = list_entry(pos, struct folio, lru);
		unsigned long neighbor_pfn = folio_pfn(neighbor);

		if (neighbor_pfn >= index_pfns)
			continue;
		previous = &index_entries[neighbor_pfn];
		if (previous->owner == lruvec && previous->order == entry->order &&
		    previous->active == entry->active) {
			where = &previous->link;
			break;
		}
	}
	list_move(&entry->link, where);
}

static void move_lru(struct folio *folio, bool active, bool tail)
{
	struct lruvec *lruvec;
	unsigned long flags;

	lruvec = folio_lruvec_lock_irqsave(folio, &flags);
	if (folio_test_lru(folio) && !folio_test_unevictable(folio)) {
		lruvec_del_folio(lruvec, folio);
		if (active)
			folio_set_active(folio);
		else
			folio_clear_active(folio);
		if (tail)
			lruvec_add_folio_tail(lruvec, folio);
		else
			lruvec_add_folio(lruvec, folio);
	}
	unlock_page_lruvec_irqrestore(lruvec, flags);
}

static bool eligible_folio(struct folio *folio)
{
	return folio_test_anon(folio) && folio_test_swapbacked(folio) &&
		!folio_test_ksm(folio) && !folio_test_swapcache(folio) &&
		!folio_test_unevictable(folio) && !folio_test_mlocked(folio) &&
		!folio_maybe_dma_pinned(folio) && !folio_maybe_mapped_shared(folio) &&
		supported_order(folio_order(folio));
}

static bool eligible_vma(struct vm_area_struct *vma)
{
	return vma_is_anonymous(vma) && vma->anon_vma &&
		!(vma->vm_flags & (VM_SHARED | VM_LOCKED | VM_PFNMAP | VM_MIXEDMAP | VM_IO)) &&
		!userfaultfd_armed(vma) && !is_vm_hugetlb_page(vma);
}

/* Caller holds mmap lock and a reference to folio. No retained VMA pointer. */
static int validate_mapping(struct mm_struct *mm, unsigned long address,
			    struct folio *folio)
{
	struct vm_area_struct *vma = vma_lookup(mm, address);
	unsigned long bytes = folio_size(folio), i;

	if (!vma || !eligible_vma(vma) || !IS_ALIGNED(address, bytes) ||
	    bytes > vma->vm_end - address || !eligible_folio(folio))
		return -EINVAL;
	for (i = 0; i < folio_nr_pages(folio); i++) {
		struct folio_walk walk;
		struct folio *found = folio_walk_start(&walk, vma, address + i * PAGE_SIZE, 0);
		bool valid;

		if (!found)
			return -EAGAIN;
		valid = found == folio && page_to_pfn(walk.page) == folio_pfn(folio) + i;
		if (valid)
			valid = PageAnonExclusive(walk.level == FW_LEVEL_PTE ? walk.page : &folio->page);
		folio_walk_end(&walk, vma);
		if (!valid)
			return -EBUSY;
	}
	return 0;
}

static struct mm_struct *get_mm(pid_t pid)
{
	struct pid *ref = find_get_pid(pid);
	struct task_struct *task = get_pid_task(ref, PIDTYPE_PID);
	struct mm_struct *mm;

	put_pid(ref);
	if (!task)
		return NULL;
	mm = get_task_mm(task);
	put_task_struct(task);
	return mm;
}

static struct folio *get_folio_locked(struct mm_struct *mm, unsigned long address)
{
	struct vm_area_struct *vma = vma_lookup(mm, address);
	struct folio_walk walk;
	struct folio *folio;

	if (!vma || !eligible_vma(vma))
		return ERR_PTR(-EINVAL);
	folio = folio_walk_start(&walk, vma, address, 0);
	if (!folio)
		return ERR_PTR(-ENOENT);
	if (!folio_trylock(folio)) {
		folio_walk_end(&walk, vma);
		return ERR_PTR(-EBUSY);
	}
	folio_get(folio);
	folio_walk_end(&walk, vma);
	return folio;
}

static u64 sum_counts(const u16 *values, unsigned int offset, unsigned int order)
{
	u64 sum = 0;
	unsigned int i;

	for (i = 0; i < (1U << order); i++)
		sum += values[offset + i];
	return sum;
}

static void emit_segment(struct ch_plan *plan, unsigned int offset,
			 unsigned int order, bool dominant)
{
	/* Anonymous order-1 is unrepresentable; emit two actual base folios. */
	if (order == 1) {
		emit_segment(plan, offset, 0, dominant);
		emit_segment(plan, offset + 1, 0, dominant);
		return;
	}
	if (WARN_ON_ONCE(plan->count >= CH_MAX_SEGMENTS))
		return;
	plan->segment[plan->count++] = (struct ch_segment){offset, order, dominant};
}

static void hhh_plan(const u16 *values, unsigned int order, u16 phi,
		     struct ch_plan *plan)
{
	unsigned int offset = 0;
	u64 sum = sum_counts(values, 0, order);

	memset(plan, 0, sizeof(*plan));
	plan->original_order = order;
	if (sum >= (1UL << order) * phi) {
		while (order) {
			u64 left = sum_counts(values, offset, order - 1), right = sum - left;
			unsigned int half = 1U << (order - 1);

			if (1000 * max(left, right) <= hhh_dominance_permille * sum)
				break;
			if (left > right) {
				emit_segment(plan, offset + half, order - 1, false);
				sum = left;
			} else {
				emit_segment(plan, offset, order - 1, false);
				offset += half;
				sum = right;
			}
			order--;
		}
	}
	plan->dominant_offset = offset;
	plan->dominant_order = order == 1 ? 0 : order;
	emit_segment(plan, offset, order, true);
}

/* Memtis htmm_core.c (92487b973d): H_sub = sample_count * 512,
 * skew = sum(H_sub^2) / 11 / hot_utils / hot_utils, then get_skew_idx().
 * Use C1's base-page hot threshold in place of Memtis's emulated-tier one.
 * This is the PMD split mechanism only: no fabricated eHR/rHR tier model. */
static unsigned int memtis_skew_bin(const u16 *values, u16 threshold,
				    unsigned int *hot)
{
	u64 squares = 0, sum = 0, skew;
	unsigned int i, bin = 0;

	*hot = 0;
	for (i = 0; i < HPAGE_PMD_NR; i++) {
		u64 normalized = (u64)values[i] * HPAGE_PMD_NR;

		sum += values[i];
		squares += normalized * normalized;
		*hot += values[i] >= threshold;
	}
	/* Original Memtis excludes no-hot and extremely hot PMDs. */
	if (!*hot || sum >= 8191)
		return 0;
	skew = div64_u64(div64_u64(div64_u64(squares, 11), *hot), *hot);
	if (skew >= 1024) {
		while (skew > 1024 && bin < 9) {
			skew -= 1024;
			bin++;
		}
		return bin + 11;
	}
	return fls64(skew);
}

/* Caller owns head reference/lock and mmap_write. Child references are
 * acquired from the still-current PTE, never from an unowned naked PFN. */
static int memtis_split_locked(struct mm_struct *mm, unsigned long address,
			       struct folio *folio, const u16 *values)
{
	unsigned long pfn = folio_pfn(folio);
	unsigned int i;
	u16 threshold = chameleon_hot_threshold();
	int ret;

	last.plan.original_order = folio_order(folio);
	if (folio_order(folio) != HPAGE_PMD_ORDER)
		return 0;
	memtis_considered++;
	last.memtis_bin = memtis_skew_bin(values, threshold, &last.hot_subpages);
	if (!last.memtis_bin || last.memtis_bin < memtis_min_bin)
		return 0;
	memtis_qualified++;
	if (!folio_test_lru(folio))
		return -EBUSY;
	ret = split_huge_page_to_list_to_order(&folio->page, NULL, 0);
	if (ret)
		return ret;
	last.split_changed = last.uniform_base = true;
	memtis_splits++;
	memtis_pages += HPAGE_PMD_NR;
	for (i = 0; i < HPAGE_PMD_NR; i++) {
		struct folio *part = folio;

		if (i) {
			part = get_folio_locked(mm, address + i * PAGE_SIZE);
			if (IS_ERR(part))
				return PTR_ERR(part);
		}
		if (folio_pfn(part) != pfn + i || folio_order(part))
			ret = -EUCLEAN;
		else if (!linux_lru)
			move_lru(part, values[i] >= threshold, values[i] < threshold);
		if (i) {
			folio_unlock(part);
			folio_put(part);
		}
		if (ret)
			return ret;
	}
	split_ok++;
	return 0;
}

static int split_execute(struct mm_struct *mm, unsigned long address)
{
	struct folio *folio;
	struct vm_area_struct *vma;
	u16 values[HPAGE_PMD_NR];
	unsigned long pfn;
	unsigned int i;
	u64 started = task_sched_runtime(current);
	int ret;

	memset(&last, 0, sizeof(last));
	strscpy(last.operation, "split");
	lru_add_drain_all();
	mmap_write_lock(mm);
	vma = vma_lookup(mm, address);
	if (!vma) { ret = -ENOENT; goto out_mm; }
	vma_start_write(vma);
	folio = get_folio_locked(mm, address);
	if (IS_ERR(folio)) { ret = PTR_ERR(folio); goto out_mm; }
	ret = validate_mapping(mm, address, folio);
	if (ret)
		goto out_folio;
	pfn = folio_pfn(folio);
	last.pfn = pfn;
	ret = chameleon_read_counters(pfn, folio_nr_pages(folio), values);
	if (ret)
		goto out_folio;
	if (memtis_split) {
		ret = memtis_split_locked(mm, address, folio, values);
		goto out_folio;
	}
	hhh_plan(values, folio_order(folio), chameleon_hot_threshold(), &last.plan);
	if (last.plan.count == 1) {
		ret = 0;
		goto out_folio;
	}
	if (!folio_test_lru(folio)) { ret = -EBUSY; goto out_folio; }
	ret = folio_split(folio, last.plan.dominant_order,
			folio_page(folio, last.plan.dominant_offset), NULL);
	if (ret)
		goto out_folio;
	last.split_changed = true;
	for (i = 0; i < last.plan.count; i++) {
		struct ch_segment *seg = &last.plan.segment[i];
		struct folio *part = folio;

		/* Only the original head retains the split caller reference/lock.
		 * Pin each other child through its current PTE before using it. */
		if (seg->offset) {
			part = get_folio_locked(mm, address + seg->offset * PAGE_SIZE);
			if (IS_ERR(part)) { ret = PTR_ERR(part); break; }
		}

		if (folio_pfn(part) != pfn + seg->offset || folio_order(part) != seg->order) {
			ret = -EUCLEAN;
		} else if (!linux_lru) {
			move_lru(part, true, !seg->dominant);
		}
		if (seg->offset) {
			folio_unlock(part);
			folio_put(part);
		}
		if (ret)
			break;
	}
	if (!ret)
		split_ok++;
out_folio:
	folio_unlock(folio);
	folio_put(folio);
out_mm:
	mmap_write_unlock(mm);
	if (ret)
		split_busy++;
	last.error = ret;
	split_cpu_ns += task_sched_runtime(current) - started;
	return ret;
}

struct ch_owner { struct mm_struct *mm; unsigned long address; pid_t pid; };
static bool owner_one(struct folio *folio, struct vm_area_struct *vma,
		      unsigned long address, void *arg)
{
	struct ch_owner *owner = arg;

	if (!eligible_vma(vma) || !IS_ALIGNED(address, folio_size(folio)) ||
	    address < vma->vm_start || folio_size(folio) > vma->vm_end - address)
		return true;
	if (target_mm && (vma->vm_mm != target_mm || address < target_start ||
			 address >= target_end || folio_size(folio) > target_end - address))
		return true;
	if (!mmget_not_zero(vma->vm_mm))
		return true;
	owner->mm = vma->vm_mm;
	owner->address = address;
	owner->pid = target_mm ? target_pid : 0;
	return false;
}

static int find_owner(struct folio *folio, struct ch_owner *owner)
{
	struct rmap_walk_control walk = {.arg = owner, .rmap_one = owner_one,
		.anon_lock = folio_lock_anon_vma_read, .try_lock = true};

	memset(owner, 0, sizeof(*owner));
	if (!folio_trylock(folio))
		return -EBUSY;
	if (eligible_folio(folio))
		rmap_walk(folio, &walk);
	folio_unlock(folio);
	return owner->mm ? 0 : -ENOENT;
}

/* Each call advances a deletion-safe cursor, without changing LRU order. */
static struct folio *cursor_folio(struct lruvec *lruvec, unsigned int active,
				  unsigned int order)
{
	struct list_head *head = &lruvec->chameleon_lists[active][order], *pos;
	struct ch_lru_entry *entry;
	struct folio *folio = NULL;
	unsigned long flags;

	spin_lock_irqsave(&lruvec->lru_lock, flags);
	if (list_empty(head))
		goto out;
	pos = lruvec->chameleon_cursor[active][order];
	if (pos == head)
		pos = head->next;
	lruvec->chameleon_cursor[active][order] = pos->next;
	entry = list_entry(pos, struct ch_lru_entry, link);
	folio = pfn_folio(entry - index_entries);
	if (!folio_test_lru(folio) || !folio_try_get(folio))
		folio = NULL;
out:
	spin_unlock_irqrestore(&lruvec->lru_lock, flags);
	return folio;
}

static void promote_age(void)
{
	pg_data_t *pgdat;
	unsigned int order, active, i;
	u16 values[HPAGE_PMD_NR], phi = chameleon_hot_threshold();

	lru_add_drain_all();
	for_each_online_pgdat(pgdat) {
		struct lruvec *lruvec = mem_cgroup_lruvec(NULL, pgdat);

		for (order = 0; order < CHAMELEON_ORDERS; order++) {
			if (!supported_order(order))
				continue;
			for (active = 0; active < 2; active++) {
				unsigned long total = READ_ONCE(lruvec->chameleon_count[active][order]);
				unsigned long count = target_mm ? total : min_t(unsigned long, scan_folios, total);
				unsigned int processed = 0;
				for (i = 0; i < count && processed < scan_folios; i++) {
					struct folio *folio = cursor_folio(lruvec, active, order);
					struct ch_owner owner;
					bool hot;

					if (!folio)
						continue;
					if (find_owner(folio, &owner))
						goto put;
					mmput(owner.mm);
					processed++;
					if (!folio_trylock(folio))
						goto put;
					if (folio_order(folio) != order ||
					    chameleon_read_counters(folio_pfn(folio), 1U << order, values))
						goto unlock;
					hot = sum_counts(values, 0, order) >= (1UL << order) * phi;
					if (hot != folio_test_active(folio)) {
						move_lru(folio, hot, !hot);
						if (hot) promoted++; else aged++;
					}
				unlock:
					folio_unlock(folio);
				put:
					folio_put(folio);
				}
			}
		}
	}
}

static u64 score(unsigned int order)
{
	u64 direct, trans, total, scaled;

	if (!cost.valid || !(cost.mask & BIT(order)) || !cost.batch)
		return U64_MAX;
	if (check_mul_overflow(cost.entries, cost.active, &trans) ||
	    check_mul_overflow(trans, cost.ptw, &trans) ||
	    check_mul_overflow(cost.reclaim[order], (u64)cost.batch, &direct) ||
	    check_add_overflow(cost.sync, trans, &total) ||
	    check_add_overflow(total, direct, &total) ||
	    check_mul_overflow(total, CH_SCORE_SCALE, &scaled))
		return U64_MAX;
	return div64_u64(scaled, (u64)cost.batch << order);
}

void chameleon_candidate_putback(struct chameleon_candidate *candidate)
{
	node_stat_sub_folio(candidate->folio, NR_ISOLATED_ANON);
	folio_putback_lru(candidate->folio);
	mmput(candidate->mm);
	list_del(&candidate->batch);
	kfree(candidate);
	atomic64_inc(&returned);
}

/* Consume the actual root inactive-anon tail, across page orders. Retain
 * native active/inactive classification: neither PromoteAge nor PEBS heat
 * filtering is used by this selector. Safety/ownership checks stay identical. */
static int select_native_locked(unsigned int maximum, struct list_head *batch)
{
	pg_data_t *pgdat;
	unsigned int nr = 0;
	int error = 0;

	lru_add_drain_all();
	for_each_online_pgdat(pgdat) {
		struct lruvec *lruvec = mem_cgroup_lruvec(NULL, pgdat);
		struct list_head *head = &lruvec->lists[LRU_INACTIVE_ANON];
		unsigned long attempts = lruvec_page_state(lruvec, NR_INACTIVE_ANON), i;

		for (i = 0; i < attempts && nr < maximum; i++) {
			struct chameleon_candidate *candidate;
			struct ch_owner owner;
			struct folio *folio;
			unsigned long flags;
			int ret;

			spin_lock_irqsave(&lruvec->lru_lock, flags);
			if (list_empty(head)) {
				spin_unlock_irqrestore(&lruvec->lru_lock, flags);
				break;
			}
			folio = lru_to_folio(head);
			if (!folio_test_lru(folio) || !folio_try_get(folio)) {
				spin_unlock_irqrestore(&lruvec->lru_lock, flags);
				break;
			}
			/* Advance past an ineligible page as native reclaim does; update
			 * the passive per-order mirror, but never choose through it. */
			list_move(&folio->lru, head);
			chameleon_lru_add(lruvec, folio, false);
			spin_unlock_irqrestore(&lruvec->lru_lock, flags);
			native_scanned++;
			if (find_owner(folio, &owner))
				goto put;
			candidate = kzalloc(sizeof(*candidate), GFP_KERNEL);
			if (!candidate) { mmput(owner.mm); error = -ENOMEM; goto put; }
			mmap_read_lock(owner.mm);
			if (!folio_trylock(folio)) { ret = -EBUSY; goto out_mm; }
			ret = validate_mapping(owner.mm, owner.address, folio);
			if (!ret && (folio_test_active(folio) ||
			    folio_ref_count(folio) != folio_expected_ref_count(folio) + 1))
				ret = -EBUSY;
			if (!ret) {
				if (!folio_isolate_lru(folio))
					ret = -EBUSY;
				else
					node_stat_add_folio(folio, NR_ISOLATED_ANON);
			}
			folio_unlock(folio);
		out_mm:
			mmap_read_unlock(owner.mm);
			if (ret) {
				mmput(owner.mm);
				kfree(candidate);
				goto put;
			}
			candidate->mm = owner.mm;
			candidate->address = owner.address;
			candidate->pid = owner.pid;
			candidate->folio = folio;
			candidate->pfn = folio_pfn(folio);
			candidate->order = folio_order(folio);
			candidate->token = ++next_token;
			list_add_tail(&candidate->batch, batch);
			nr++;
			selected++;
			selected_native++;
		put:
			folio_put(folio);
			cond_resched();
		}
	}
	return nr ? nr : error;
}

/* The manager mutex serializes debug/config operations and the public API. */
static int select_locked(unsigned int maximum, struct list_head *batch)
{
	unsigned int visited = 0, nr = 0;
	int error = 0;

	if (!index_entries || !maximum || maximum > CH_SCAN)
		return -EINVAL;
	if (linux_lru)
		return select_native_locked(maximum, batch);
	if (!cost.valid)
		return -EINVAL;
	promote_age();
	while (visited != CH_SUPPORTED && nr < maximum) {
		unsigned int order, best = CHAMELEON_ORDERS;
		u64 best_score = U64_MAX;
		pg_data_t *pgdat;

		for (order = 0; order < CHAMELEON_ORDERS; order++) {
			bool nonempty = false;
			u64 order_score;

			if (!supported_order(order) || (visited & BIT(order)))
				continue;
			for_each_online_pgdat(pgdat)
				nonempty |= READ_ONCE(mem_cgroup_lruvec(NULL, pgdat)->chameleon_count[0][order]) != 0;
			order_score = score(order);
			if (nonempty && order_score < best_score) {
				best_score = order_score;
				best = order;
			}
		}
		if (best == CHAMELEON_ORDERS)
			break;
		visited |= BIT(best);
		for_each_online_pgdat(pgdat) {
			struct lruvec *lruvec = mem_cgroup_lruvec(NULL, pgdat);
			struct list_head *head = &lruvec->chameleon_lists[0][best];
			unsigned long attempts = READ_ONCE(lruvec->chameleon_count[0][best]), i;

			for (i = 0; i < attempts && nr < maximum; i++) {
				struct chameleon_candidate *candidate;
				struct ch_lru_entry *entry;
				struct ch_owner owner;
				struct folio *folio;
				unsigned long flags;
				u16 values[HPAGE_PMD_NR];
				int ret;

				spin_lock_irqsave(&lruvec->lru_lock, flags);
				if (list_empty(head)) {
					spin_unlock_irqrestore(&lruvec->lru_lock, flags);
					break;
				}
				entry = list_last_entry(head, struct ch_lru_entry, link);
				folio = pfn_folio(entry - index_entries);
				/* Temporarily rotate an ineligible tail to guarantee progress.
				 * Keep the real LRU and its auxiliary ordering consistent. */
				if (!folio_test_lru(folio)) {
					chameleon_lru_del(lruvec, folio);
					folio = NULL;
				} else if (folio_try_get(folio)) {
					list_move(&folio->lru, &lruvec->lists[LRU_INACTIVE_ANON]);
					chameleon_lru_add(lruvec, folio, false);
				} else {
					folio = NULL;
				}
				spin_unlock_irqrestore(&lruvec->lru_lock, flags);
				if (!folio)
					continue;
				if (find_owner(folio, &owner))
					goto put_folio;
				candidate = kzalloc(sizeof(*candidate), GFP_KERNEL);
				if (!candidate) { mmput(owner.mm); error = -ENOMEM; goto put_folio; }
				mmap_read_lock(owner.mm);
				if (!folio_trylock(folio)) { ret = -EBUSY; goto out_mm; }
				ret = validate_mapping(owner.mm, owner.address, folio);
				if (!ret && (folio_order(folio) != best || folio_test_active(folio) ||
				    folio_ref_count(folio) != folio_expected_ref_count(folio) + 1))
					ret = -EBUSY;
				if (!ret)
					ret = chameleon_read_counters(folio_pfn(folio), 1U << best, values);
				if (!ret && sum_counts(values, 0, best) >=
				    (1UL << best) * chameleon_hot_threshold())
					ret = -EAGAIN;
				if (!ret) {
					if (!folio_isolate_lru(folio))
						ret = -EBUSY;
					else
						node_stat_add_folio(folio, NR_ISOLATED_ANON);
				}
				folio_unlock(folio);
			out_mm:
				mmap_read_unlock(owner.mm);
				if (ret) {
					mmput(owner.mm);
					kfree(candidate);
					goto put_folio;
				}
				candidate->mm = owner.mm;
				candidate->address = owner.address;
				candidate->pid = owner.pid;
				candidate->folio = folio;
				candidate->pfn = folio_pfn(folio);
				candidate->order = best;
				candidate->token = ++next_token;
				list_add_tail(&candidate->batch, batch);
				nr++;
				selected++;
				selected_mixed++;
			put_folio:
				folio_put(folio); /* only the isolation reference survives */
			}
		}
	}
	return nr ? nr : error;
}

int chameleon_select_batch(unsigned int maximum, struct list_head *batch)
{
	u64 started = task_sched_runtime(current);
	int ret;

	mutex_lock(&manager_lock);
	ret = select_locked(maximum, batch);
	select_cpu_ns += task_sched_runtime(current) - started;
	mutex_unlock(&manager_lock);
	return ret;
}

int chameleon_select_batch_mm(unsigned int maximum, struct mm_struct *mm,
		unsigned long start, unsigned long end, struct list_head *batch)
{
	struct mm_struct *saved_mm;
	unsigned long saved_start, saved_end;
	pid_t saved_pid;
	u64 started = task_sched_runtime(current);
	int ret;

	if (!mm || start >= end || !PAGE_ALIGNED(start) || !PAGE_ALIGNED(end))
		return -EINVAL;
	mutex_lock(&manager_lock);
	saved_mm = target_mm; saved_start = target_start; saved_end = target_end;
	saved_pid = target_pid;
	target_mm = mm; target_start = start; target_end = end;
	/* The caller's mm_users reference covers this temporary filter. */
	if (saved_mm != mm)
		target_pid = 0;
	ret = select_locked(maximum, batch);
	target_mm = saved_mm; target_start = saved_start; target_end = saved_end;
	target_pid = saved_pid;
	select_cpu_ns += task_sched_runtime(current) - started;
	mutex_unlock(&manager_lock);
	return ret;
}

/* Check the explicit uniform-hot policy; the executor revalidates ownership. */
static int collapse_hot(struct mm_struct *mm, unsigned long address,
			unsigned int order)
{
	unsigned int source_order = order == 2 ? 0 : order - 1, i;
	u16 values[HPAGE_PMD_NR], phi = chameleon_hot_threshold();
	struct ch_plan plan;
	int ret = 0;

	mmap_read_lock(mm);
	for (i = 0; i < (1U << order); i += 1U << source_order) {
		struct folio *folio = get_folio_locked(mm, address + i * PAGE_SIZE);

		if (IS_ERR(folio)) { ret = PTR_ERR(folio); break; }
		if (folio_order(folio) != source_order)
			ret = -EINVAL;
		else
			ret = validate_mapping(mm, address + i * PAGE_SIZE, folio);
		if (!ret)
			ret = chameleon_read_counters(folio_pfn(folio), 1U << source_order, values + i);
		if (!ret && sum_counts(values + i, 0, source_order) < (1UL << source_order) * phi)
			ret = -EAGAIN;
		folio_unlock(folio);
		folio_put(folio);
		if (ret)
			break;
	}
	mmap_read_unlock(mm);
	if (!ret) {
		hhh_plan(values, order, phi, &plan);
		if (plan.count != 1)
			ret = -EAGAIN;
	}
	return ret;
}

static int collapse_execute(struct mm_struct *mm, unsigned long address,
			    unsigned int order, bool automatic)
{
	int ret;

	memset(&last, 0, sizeof(last));
	strscpy(last.operation, "collapse");
	if (!supported_order(order) || !order) {
		ret = -EINVAL;
		goto out;
	}
	if (target_mm && (mm != target_mm || address < target_start ||
	    address >= target_end || (PAGE_SIZE << order) > target_end - address)) {
		ret = -ERANGE;
		goto out;
	}
	lru_add_drain_all();
	ret = automatic ? collapse_hot(mm, address, order) : 0;
	if (!ret)
		ret = chameleon_collapse_anon(mm, address, order);
out:
	if (ret)
		collapse_fail++;
	else
		collapse_ok++;
	last.error = ret;
	return ret;
}

static u64 ptw_pending, ptw_completed;
static bool ptw_initialized;
static int update_ptw(void)
{
	u64 pending, completed, delta_pending, delta_completed;
	bool valid;

	if (!ptw_auto) {
		cost.measured_ptw = false;
		cost.ptw = cost.explicit_ptw;
		return 0;
	}
	valid = chameleon_get_ptw_snapshot(&pending, &completed);
	if (!ptw_initialized || pending < ptw_pending || completed < ptw_completed) {
		ptw_initialized = true;
		ptw_pending = pending;
		ptw_completed = completed;
		cost.measured_ptw = false;
		cost.ptw = cost.explicit_ptw;
		return -ENODATA;
	}
	delta_pending = pending - ptw_pending;
	delta_completed = completed - ptw_completed;
	ptw_pending = pending;
	ptw_completed = completed;
	if (!valid || !delta_completed || !delta_pending) {
		cost.measured_ptw = false;
		cost.ptw = cost.explicit_ptw;
		return -ENODATA;
	}
	cost.ptw = div64_u64(delta_pending, delta_completed);
	cost.measured_ptw = true;
	return 0;
}

struct memtis_choice {
	struct ch_owner owner;
	unsigned int bin;
};

static int memtis_compare(const void *a, const void *b)
{
	const struct memtis_choice *left = a, *right = b;

	return (left->bin < right->bin) - (left->bin > right->bin);
}

/* Select highest skew bins in a bounded rotating snapshot. Unlike the full
 * Memtis tier manager, the split budget and minimum bin are explicit inputs. */
static void memtis_epoch(void)
{
	struct memtis_choice *choices;
	unsigned int nr = 0, active, i, split = 0;
	pg_data_t *pgdat;
	u16 values[HPAGE_PMD_NR];

	choices = kcalloc(CH_SCAN, sizeof(*choices), GFP_KERNEL);
	if (!choices)
		return;
	lru_add_drain_all();
	for_each_online_pgdat(pgdat) {
		struct lruvec *lruvec = mem_cgroup_lruvec(NULL, pgdat);

		for (active = 0; active < 2; active++) {
			unsigned long total = READ_ONCE(lruvec->chameleon_count[active][9]);
			unsigned long attempts = target_mm ? total : min_t(unsigned long, scan_folios, total);

			for (i = 0; i < attempts && nr < scan_folios; i++) {
				struct folio *folio = cursor_folio(lruvec, active, 9);
				struct ch_owner owner;
				unsigned int hot, bin = 0, j;

				if (!folio)
					continue;
				scanned++;
				if (find_owner(folio, &owner))
					goto put;
				for (j = 0; j < nr; j++)
					if (choices[j].owner.mm == owner.mm &&
					    choices[j].owner.address == owner.address)
						break;
				if (j == nr && folio_order(folio) == 9 &&
				    !chameleon_read_counters(folio_pfn(folio), HPAGE_PMD_NR, values))
					bin = memtis_skew_bin(values, chameleon_hot_threshold(), &hot);
				if (bin && bin >= memtis_min_bin)
					choices[nr++] = (struct memtis_choice){owner, bin};
				else
					mmput(owner.mm);
			put:
				folio_put(folio);
				cond_resched();
			}
		}
	}
	sort(choices, nr, sizeof(*choices), memtis_compare, NULL);
	for (i = 0; i < nr; i++) {
		if (split < memtis_budget) {
			split_execute(choices[i].owner.mm, choices[i].owner.address);
			/* A completed native split consumes budget even if subsequent
			 * per-child LRU placement races native reclaim. */
			if (last.split_changed)
				split++;
		}
		mmput(choices[i].owner.mm);
	}
	kfree(choices);
}

/* Runtime conversions execute here, outside reclaim/selection and fault paths. */
static void manager_epoch(void)
{
	unsigned int round;
	pg_data_t *pgdat;

	if (!index_entries)
		return;
	if (memtis_split) {
		memtis_epoch();
		epochs++;
		return;
	}
	if (!linux_lru)
		promote_age();
	for (round = 0; round < CHAMELEON_ORDERS; round++) {
		unsigned int order = (scan_first_order + round) % CHAMELEON_ORDERS;

		if (!supported_order(order))
			continue;
		for_each_online_pgdat(pgdat) {
			struct lruvec *lruvec = mem_cgroup_lruvec(NULL, pgdat);
			unsigned long total = READ_ONCE(lruvec->chameleon_count[1][order]);
			unsigned long count = target_mm ? total : min_t(unsigned long, scan_folios, total), i;
			unsigned int processed = 0;

			for (i = 0; i < count && processed < scan_folios; i++) {
				struct folio *folio = cursor_folio(lruvec, 1, order);
				struct ch_owner owner;
				unsigned int larger = order ? order + 1 : 2;
				unsigned long address;
				int ret;

				if (!folio)
					continue;
				scanned++;
				ret = find_owner(folio, &owner);
				folio_put(folio);
				if (ret)
					continue;
				processed++;
				ret = split_execute(owner.mm, owner.address);
				/* A split moves its real children before the next epoch. */
				if (!ret && !last.split_changed && supported_order(larger)) {
					address = ALIGN_DOWN(owner.address, PAGE_SIZE << larger);
					collapse_execute(owner.mm, address, larger, true);
				}
				mmput(owner.mm);
				cond_resched();
			}
		}
	}
	scan_first_order = (scan_first_order + 1) % CHAMELEON_ORDERS;
	epochs++;
}

static void manager_work(struct work_struct *work);
static DECLARE_DELAYED_WORK(epoch_work, manager_work);

static void manager_work(struct work_struct *work)
{
	mutex_lock(&manager_lock);
	if (manager_enabled) {
		manager_epoch();
		update_ptw();
		if (manager_enabled)
			mod_delayed_work(system_unbound_wq, &epoch_work,
					 msecs_to_jiffies(maintenance_interval_ms));
	}
	mutex_unlock(&manager_lock);
}

static void putback_all(void)
{
	struct chameleon_candidate *candidate, *next;

	list_for_each_entry_safe(candidate, next, &held_candidates, batch)
		chameleon_candidate_putback(candidate);
	lru_add_drain_all();
}

static int manager_stats_show(struct seq_file *m, void *unused)
{
	unsigned int order;
	u64 pages = 0, folios = 0;
	pg_data_t *pgdat;

	mutex_lock(&manager_lock);
	seq_printf(m, "available %u\nenabled %u\nsupported_orders 0,2,3,4,5,6,7,8,9\n",
		!!index_entries, manager_enabled);
	seq_printf(m, "index_bytes %lu\nepochs %llu\nscanned %llu\nsplit_ok %llu\nsplit_busy %llu\n",
		index_pfns * sizeof(*index_entries), epochs, scanned, split_ok, split_busy);
	seq_printf(m, "collapse_ok %llu\ncollapse_fail %llu\npromoted %llu\naged %llu\nselected %llu\nputback %llu\n",
		collapse_ok, collapse_fail, promoted, aged, selected, (u64)atomic64_read(&returned));
	seq_printf(m, "split_mode %s\nselector_mode %s\nmode_changes %llu\n",
		memtis_split ? "memtis" : "hhh", linux_lru ? "linux_lru" : "mixed_cost", mode_changes);
	seq_printf(m, "hhh_dominance_permille %u\nmaintenance_interval_ms %u\nscan_folios %u\nptw_auto %u\n",
		hhh_dominance_permille, maintenance_interval_ms, scan_folios, ptw_auto);
	seq_printf(m, "memtis_min_bin %u\nmemtis_budget %u\nmemtis_considered %llu\nmemtis_qualified %llu\nmemtis_splits %llu\nmemtis_pages %llu\n",
		memtis_min_bin, memtis_budget, memtis_considered, memtis_qualified, memtis_splits, memtis_pages);
	seq_printf(m, "selected_mixed %llu\nselected_native %llu\nnative_scanned %llu\nsplit_cpu_ns %llu\nselect_cpu_ns %llu\n",
		selected_mixed, selected_native, native_scanned, split_cpu_ns, select_cpu_ns);
	seq_printf(m, "target_pid %d\ntarget_start 0x%lx\ntarget_end 0x%lx\n",
		target_pid, target_start, target_end);
	seq_printf(m, "score_scale %llu\ncost_valid %u\ncost_source explicit_parameters\nptw_measured %u\n",
		CH_SCORE_SCALE, cost.valid, cost.measured_ptw);
	seq_printf(m, "batch %u\nCsync %llu\nEtrans %llu\nNactive %llu\nLptw %llu\n",
		cost.batch, cost.sync, cost.entries, cost.active, cost.ptw);
	seq_printf(m, "Lptw_explicit %llu\n", cost.explicit_ptw);
	for (order = 0; order < CHAMELEON_ORDERS; order++) {
		u64 active = 0, inactive = 0;

		if (!supported_order(order))
			continue;
		for_each_online_pgdat(pgdat) {
			struct lruvec *lruvec = mem_cgroup_lruvec(NULL, pgdat);
			unsigned long flags;

			spin_lock_irqsave(&lruvec->lru_lock, flags);
			active += lruvec->chameleon_count[1][order];
			inactive += lruvec->chameleon_count[0][order];
			spin_unlock_irqrestore(&lruvec->lru_lock, flags);
		}
		folios += active + inactive;
		pages += (active + inactive) << order;
		seq_printf(m, "order=%u active=%llu inactive=%llu score=%llu reclaim=%llu\n",
			order, active, inactive, score(order), cost.reclaim[order]);
	}
	seq_printf(m, "queue_folios %llu\nqueue_pages %llu\n", folios, pages);
	mutex_unlock(&manager_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(manager_stats);

static int manager_last_show(struct seq_file *m, void *unused)
{
	unsigned int i;

	mutex_lock(&manager_lock);
	seq_printf(m, "operation %s\nerror %d\nsplit_changed %u\npfn %lu\n",
		last.operation, last.error, last.split_changed, last.pfn);
	seq_printf(m, "original_order %u\ndominant_offset %u\ndominant_order %u\n",
		last.plan.original_order, last.plan.dominant_offset, last.plan.dominant_order);
	seq_printf(m, "uniform_base %u\nmemtis_bin %u\nhot_subpages %u\n",
		last.uniform_base, last.memtis_bin, last.hot_subpages);
	if (last.uniform_base)
		for (i = 0; i < HPAGE_PMD_NR; i++)
			seq_printf(m, "segment offset=%u order=0 dominant=0\n", i);
	for (i = 0; i < last.plan.count; i++) {
		struct ch_segment *segment = &last.plan.segment[i];

		seq_printf(m, "segment offset=%u order=%u dominant=%u\n",
			segment->offset, segment->order, segment->dominant);
	}
	mutex_unlock(&manager_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(manager_last);

static int manager_candidates_show(struct seq_file *m, void *unused)
{
	struct chameleon_candidate *candidate;

	mutex_lock(&manager_lock);
	list_for_each_entry(candidate, &held_candidates, batch)
		seq_printf(m, "candidate token=%llu pid=%d address=0x%lx pfn=%lu order=%u\n",
			candidate->token, candidate->pid, candidate->address,
			candidate->pfn, candidate->order);
	mutex_unlock(&manager_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(manager_candidates);

static ssize_t manager_control(struct file *file, const char __user *buffer,
			       size_t count, loff_t *position)
{
	char *text, **args;
	int argc, ret = -EINVAL, pid;
	unsigned long address, bytes;
	unsigned int order, number;
	unsigned long long values[5];
	struct mm_struct *mm, *target_user = NULL;
	bool cancel = false;

	if (!count || count > 256)
		return -EINVAL;
	text = memdup_user_nul(buffer, count);
	if (IS_ERR(text))
		return PTR_ERR(text);
	args = argv_split(GFP_KERNEL, text, &argc);
	kfree(text);
	if (!args)
		return -ENOMEM;
	if (!argc)
		goto free_args;
	mutex_lock(&command_lock);
	mutex_lock(&manager_lock);
	if (!index_entries) { ret = -EOPNOTSUPP; goto unlock; }
	if (!strcmp(args[0], "target") && argc == 4) {
		if (kstrtoint(args[1], 0, &pid) || pid <= 0 ||
		    kstrtoul(args[2], 0, &address) || kstrtoul(args[3], 0, &bytes) ||
		    !bytes || !PAGE_ALIGNED(address) || !PAGE_ALIGNED(bytes) ||
		    address + bytes < address)
			goto unlock;
		if (!list_empty(&held_candidates)) { ret = -EBUSY; goto unlock; }
		mm = get_mm(pid);
		if (!mm) { ret = -ESRCH; goto unlock; }
		/* The filter preserves identity, not a live address space. */
		mmgrab(mm);
		target_user = mm;
		if (target_mm)
			mmdrop(target_mm);
		target_mm = mm;
		target_pid = pid;
		target_start = address;
		target_end = address + bytes;
		ret = 0;
	} else if (!strcmp(args[0], "clear_target") && argc == 1) {
		if (!list_empty(&held_candidates)) { ret = -EBUSY; goto unlock; }
		if (target_mm)
			mmdrop(target_mm);
		target_mm = NULL;
		target_pid = 0;
		target_start = target_end = 0;
		ret = 0;
	} else if (!strcmp(args[0], "split_mode") && argc == 2) {
		if (strcmp(args[1], "hhh") && strcmp(args[1], "memtis"))
			goto unlock;
		memtis_split = !strcmp(args[1], "memtis");
		mode_changes++;
		ret = 0;
	} else if (!strcmp(args[0], "selector") && argc == 2) {
		if (strcmp(args[1], "mixed_cost") && strcmp(args[1], "linux_lru"))
			goto unlock;
		if (!list_empty(&held_candidates)) { ret = -EBUSY; goto unlock; }
		linux_lru = !strcmp(args[1], "linux_lru");
		mode_changes++;
		ret = 0;
	} else if (!strcmp(args[0], "memtis") && argc == 3) {
		if (kstrtouint(args[1], 0, &order) || order > 20 || !order ||
		    kstrtouint(args[2], 0, &number) || !number || number > CH_SCAN)
			goto unlock;
		memtis_min_bin = order;
		memtis_budget = number;
		ret = 0;
	} else if (!strcmp(args[0], "enable") && argc == 1) {
		manager_enabled = true;
		mod_delayed_work(system_unbound_wq, &epoch_work,
				 msecs_to_jiffies(maintenance_interval_ms));
		ret = 0;
	} else if (!strcmp(args[0], "disable") && argc == 1) {
		manager_enabled = false;
		cancel = true;
		ret = 0;
	} else if (!strcmp(args[0], "epoch") && argc == 1) {
		manager_epoch();
		ret = 0;
	} else if (!strcmp(args[0], "age") && argc == 1) {
		if (!linux_lru)
			promote_age();
		ret = 0;
	} else if (!strcmp(args[0], "ptw") && argc == 1) {
		ret = update_ptw();
	} else if (!strcmp(args[0], "putback") && argc == 1) {
		putback_all();
		ret = 0;
	} else if (!strcmp(args[0], "cost") && argc == 3) {
		if (kstrtouint(args[1], 0, &order) || !supported_order(order) ||
		    kstrtoull(args[2], 0, &values[0]))
			goto unlock;
		cost.reclaim[order] = values[0];
		cost.mask |= BIT(order);
		ret = 0;
	} else if (!strcmp(args[0], "batch") && argc == 6) {
		unsigned int i;

		for (i = 0; i < 5; i++)
			if (kstrtoull(args[i + 1], 0, &values[i]))
				goto unlock;
		if (!values[0] || values[0] > CH_SCAN || !values[2] || !values[4])
			goto unlock;
		cost.batch = values[0]; cost.sync = values[1];
		cost.entries = values[2]; cost.active = values[3];
		cost.ptw = cost.explicit_ptw = values[4];
		cost.valid = true;
		cost.measured_ptw = false;
		ret = 0;
	} else if (!strcmp(args[0], "select") && argc == 2) {
		if (kstrtouint(args[1], 0, &number))
			goto unlock;
		if (!list_empty(&held_candidates)) { ret = -EBUSY; goto unlock; }
		{
			u64 started = task_sched_runtime(current);

			ret = select_locked(number, &held_candidates);
			select_cpu_ns += task_sched_runtime(current) - started;
		}
		if (ret >= 0)
			ret = 0;
#ifdef CONFIG_CHAMELEON_TEST
	} else if (!strcmp(args[0], "lru_tail") && argc == 3) {
		struct folio *folio;

		if (kstrtoint(args[1], 0, &pid) || pid <= 0 ||
		    kstrtoul(args[2], 0, &address))
			goto unlock;
		mm = get_mm(pid);
		if (!mm) { ret = -ESRCH; goto unlock; }
		lru_add_drain_all();
		mmap_write_lock(mm);
		folio = get_folio_locked(mm, address);
		if (IS_ERR(folio)) {
			ret = PTR_ERR(folio);
		} else {
			ret = validate_mapping(mm, address, folio);
			if (!ret)
				move_lru(folio, false, true);
			folio_unlock(folio);
			folio_put(folio);
		}
		mmap_write_unlock(mm);
		mmput(mm);
	} else if (!strcmp(args[0], "fail_collapse") && argc == 2) {
		if (kstrtouint(args[1], 0, &number) || number > 2)
			goto unlock;
		chameleon_collapse_fail_next(number);
		ret = 0;
	} else if ((!strcmp(args[0], "split") && argc == 3) ||
		   (!strcmp(args[0], "collapse") && argc == 4)) {
		if (kstrtoint(args[1], 0, &pid) || pid <= 0 ||
		    kstrtoul(args[2], 0, &address))
			goto unlock;
		if (argc == 4 && (kstrtouint(args[3], 0, &order) || !supported_order(order)))
			goto unlock;
		mm = get_mm(pid);
		if (!mm) { ret = -ESRCH; goto unlock; }
		ret = argc == 3 ? split_execute(mm, address) : collapse_execute(mm, address, order, false);
		mmput(mm);
#endif
	}
unlock:
	mutex_unlock(&manager_lock);
	/* The work itself takes manager_lock. Never wait for it while locked. */
	if (cancel)
		cancel_delayed_work_sync(&epoch_work);
	mutex_unlock(&command_lock);
	/* Last mm_users may run exit_mmap(); hold no manager mutex then. */
	if (target_user)
		mmput(target_user);
free_args:
	argv_free(args);
	return ret ? ret : count;
}

static const struct file_operations manager_control_fops = {
	.owner = THIS_MODULE,
	.write = manager_control,
	.llseek = noop_llseek,
};

enum manager_parameter_id {
	PARAM_DOMINANCE,
	PARAM_INTERVAL,
	PARAM_SCAN,
	PARAM_SPLIT_MODE,
	PARAM_SELECTOR,
	PARAM_MEMTIS_BIN,
	PARAM_MEMTIS_BUDGET,
	PARAM_BATCH,
	PARAM_SYNC,
	PARAM_ENTRIES,
	PARAM_ACTIVE,
	PARAM_PTW_FALLBACK,
	PARAM_PTW_AUTO,
	PARAM_PTW_CURRENT,
	PARAM_RECLAIM_BASE,
};

struct manager_parameter {
	const char *name;
	enum manager_parameter_id id;
	u64 minimum, maximum;
};

/* The cost model deliberately remains unconfigured at boot. These files
 * expose the same state as "batch"/"cost" rather than adding calibrations. */
static const struct manager_parameter manager_parameters[] = {
	{ "hhh_dominance_permille", PARAM_DOMINANCE, 500, 1000 },
	{ "maintenance_interval_ms", PARAM_INTERVAL, 1, 3600000 },
	{ "scan_folios", PARAM_SCAN, 1, CH_SCAN },
	{ "split_mode", PARAM_SPLIT_MODE, 0, 1 },
	{ "selector", PARAM_SELECTOR, 0, 1 },
	{ "memtis_min_bin", PARAM_MEMTIS_BIN, 1, 20 },
	{ "memtis_budget", PARAM_MEMTIS_BUDGET, 1, CH_SCAN },
	{ "batch", PARAM_BATCH, 1, CH_SCAN },
	{ "Csync", PARAM_SYNC, 0, U64_MAX },
	{ "Etrans", PARAM_ENTRIES, 1, U64_MAX },
	{ "Nactive", PARAM_ACTIVE, 0, U64_MAX },
	{ "Lptw_fallback", PARAM_PTW_FALLBACK, 1, U64_MAX },
	{ "ptw_auto", PARAM_PTW_AUTO, 0, 1 },
	{ "Lptw", PARAM_PTW_CURRENT, 0, U64_MAX },
#define RECLAIM_PARAMETER(order) \
	{ "Creclaim_order" #order, PARAM_RECLAIM_BASE + (order), 0, U64_MAX }
	RECLAIM_PARAMETER(0),
	RECLAIM_PARAMETER(2),
	RECLAIM_PARAMETER(3),
	RECLAIM_PARAMETER(4),
	RECLAIM_PARAMETER(5),
	RECLAIM_PARAMETER(6),
	RECLAIM_PARAMETER(7),
	RECLAIM_PARAMETER(8),
	RECLAIM_PARAMETER(9),
#undef RECLAIM_PARAMETER
};

static int manager_parameter_get(void *data, u64 *value)
{
	const struct manager_parameter *parameter = data;

	mutex_lock(&manager_lock);
	switch (parameter->id) {
	case PARAM_DOMINANCE: *value = hhh_dominance_permille; break;
	case PARAM_INTERVAL: *value = maintenance_interval_ms; break;
	case PARAM_SCAN: *value = scan_folios; break;
	case PARAM_SPLIT_MODE: *value = memtis_split; break;
	case PARAM_SELECTOR: *value = linux_lru; break;
	case PARAM_MEMTIS_BIN: *value = memtis_min_bin; break;
	case PARAM_MEMTIS_BUDGET: *value = memtis_budget; break;
	case PARAM_BATCH: *value = cost.batch; break;
	case PARAM_SYNC: *value = cost.sync; break;
	case PARAM_ENTRIES: *value = cost.entries; break;
	case PARAM_ACTIVE: *value = cost.active; break;
	case PARAM_PTW_FALLBACK: *value = cost.explicit_ptw; break;
	case PARAM_PTW_AUTO: *value = ptw_auto; break;
	case PARAM_PTW_CURRENT: *value = cost.ptw; break;
	default:
		*value = cost.reclaim[parameter->id - PARAM_RECLAIM_BASE];
		break;
	}
	mutex_unlock(&manager_lock);
	return 0;
}

static int manager_parameter_set(void *data, u64 value)
{
	const struct manager_parameter *parameter = data;
	int ret = 0;

	if (value < parameter->minimum || value > parameter->maximum)
		return -EINVAL;
	/* Match control's lock order, including disable/cancel serialization. */
	mutex_lock(&command_lock);
	mutex_lock(&manager_lock);
	if (!index_entries) {
		ret = -EOPNOTSUPP;
		goto out;
	}
	switch (parameter->id) {
	case PARAM_DOMINANCE:
		hhh_dominance_permille = value;
		break;
	case PARAM_INTERVAL:
		maintenance_interval_ms = value;
		if (manager_enabled)
			mod_delayed_work(system_unbound_wq, &epoch_work,
					 msecs_to_jiffies(maintenance_interval_ms));
		break;
	case PARAM_SCAN:
		scan_folios = value;
		break;
	case PARAM_SPLIT_MODE:
		memtis_split = value;
		mode_changes++;
		break;
	case PARAM_SELECTOR:
		if (!list_empty(&held_candidates)) {
			ret = -EBUSY;
			goto out;
		}
		linux_lru = value;
		mode_changes++;
		break;
	case PARAM_MEMTIS_BIN:
		memtis_min_bin = value;
		break;
	case PARAM_MEMTIS_BUDGET:
		memtis_budget = value;
		break;
	case PARAM_BATCH:
		cost.batch = value;
		break;
	case PARAM_SYNC:
		cost.sync = value;
		break;
	case PARAM_ENTRIES:
		cost.entries = value;
		break;
	case PARAM_ACTIVE:
		cost.active = value;
		break;
	case PARAM_PTW_FALLBACK:
		/* Same immediate/fallback behavior as the existing batch command. */
		cost.ptw = cost.explicit_ptw = value;
		cost.measured_ptw = false;
		break;
	case PARAM_PTW_AUTO:
		if (ptw_auto != value) {
			ptw_auto = value;
			ptw_initialized = false;
			cost.ptw = cost.explicit_ptw;
			cost.measured_ptw = false;
		}
		break;
	case PARAM_PTW_CURRENT:
		ret = -EPERM;
		goto out;
	default:
		cost.reclaim[parameter->id - PARAM_RECLAIM_BASE] = value;
		cost.mask |= BIT(parameter->id - PARAM_RECLAIM_BASE);
		break;
	}
	/* Zero is permitted for Csync/Nactive and per-order costs, as before.
	 * An unset per-order cost still has its mask clear and cannot be selected. */
	cost.valid = cost.batch && cost.entries && cost.explicit_ptw;
out:
	mutex_unlock(&manager_lock);
	mutex_unlock(&command_lock);
	return ret;
}
DEFINE_DEBUGFS_ATTRIBUTE(manager_parameter_fops, manager_parameter_get,
			 manager_parameter_set, "%llu\n");
DEFINE_DEBUGFS_ATTRIBUTE(manager_parameter_ro_fops, manager_parameter_get,
			 NULL, "%llu\n");

static int __init chameleon_mm_init(void)
{
	struct ch_lru_entry *entries;
	struct dentry *directory;
	pg_data_t *pgdat;
	unsigned int i;

	/* This implementation intentionally indexes traditional, root LRUs. */
	if (IS_ENABLED(CONFIG_MEMCG) || IS_ENABLED(CONFIG_LRU_GEN)) {
		pr_warn("Chameleon C2 requires root traditional LRUs (MEMCG=n, LRU_GEN=n)\n");
		return 0;
	}
	index_pfns = max_pfn;
	entries = vzalloc(array_size(index_pfns, sizeof(*entries)));
	if (!entries)
		return -ENOMEM;
	/* Publish before bootstrapping. Existing and concurrent hooks share lock. */
	smp_store_release(&index_entries, entries);
	for_each_online_pgdat(pgdat) {
		struct lruvec *lruvec = mem_cgroup_lruvec(NULL, pgdat);
		struct folio *folio;
		unsigned long flags;
		unsigned int active;

		spin_lock_irqsave(&lruvec->lru_lock, flags);
		for (active = 0; active < 2; active++)
			list_for_each_entry_reverse(folio,
				&lruvec->lists[active ? LRU_ACTIVE_ANON : LRU_INACTIVE_ANON], lru)
				if (folio_test_lru(folio))
					chameleon_lru_add(lruvec, folio, false);
		spin_unlock_irqrestore(&lruvec->lru_lock, flags);
	}
	directory = debugfs_create_dir("chameleon_mm", NULL);
	debugfs_create_file("control", 0600, directory, NULL, &manager_control_fops);
	debugfs_create_file("stats", 0400, directory, NULL, &manager_stats_fops);
	debugfs_create_file("last", 0400, directory, NULL, &manager_last_fops);
	debugfs_create_file("candidates", 0400, directory, NULL, &manager_candidates_fops);
	for (i = 0; i < ARRAY_SIZE(manager_parameters); i++) {
		const struct manager_parameter *parameter = &manager_parameters[i];
		bool read_only = parameter->id == PARAM_PTW_CURRENT;

		debugfs_create_file(parameter->name, read_only ? 0400 : 0600,
				    directory, (void *)parameter,
				    read_only ? &manager_parameter_ro_fops :
						&manager_parameter_fops);
	}
	pr_info("Chameleon C2: orders 0,2..9, traditional LRU index %lu bytes\n",
		index_pfns * sizeof(*entries));
	return 0;
}
late_initcall(chameleon_mm_init);
