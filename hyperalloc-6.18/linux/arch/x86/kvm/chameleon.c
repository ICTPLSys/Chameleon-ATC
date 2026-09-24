// SPDX-License-Identifier: GPL-2.0-only
/* Private, opt-in Chameleon range transactions for anonymous RAM and Intel EPT.
 * Userspace performs discard/populate; this layer never supplies replacement
 * data. A private HVA guard survives BEGIN until REPORT, and exact retired
 * intervals survive until INSTALL verifies existing writable backing.
 */
#include <linux/debugfs.h>
#include <linux/highmem.h>
#include <linux/interval_tree.h>
#include <linux/kvm_host.h>
#include <linux/mm.h>
#include <linux/slab.h>
#include <linux/sort.h>
#include <linux/swap.h>
#include <linux/uaccess.h>
#include <linux/userfaultfd_k.h>
#include <linux/vmalloc.h>
#include "chameleon.h"
#include "mmu.h"
#include "mmu/mmu_internal.h"
#include "x86.h"
#include "trace.h"

struct ch_guard {
	unsigned long start, end;
};

struct ch_range {
	struct interval_tree_node blocked;
	struct kvm_chameleon_range user;
	unsigned long hva;
	u32 slot_id;
	gfn_t slot_gfn;
	unsigned long slot_pages, slot_hva;
};

struct ch_transaction {
	struct rb_node node;
	u64 id;
	u32 nr, nr_guards, remaining;
	bool reported, accounted;
	struct ch_guard *guards; /* Only retained until REPORT. */
	struct ch_range ranges[];
};

struct ch_tuning_file {
	struct kvm *kvm;
	u32 field;
};

struct kvm_chameleon {
	/* Never acquired by faults, MMU notifiers or memslot modification. */
	struct mutex command_lock;
	struct rw_semaphore fault_gate;
	struct rb_root transactions; /* transaction ID; command_lock */
	struct rb_root_cached blocked; /* exact HVA intervals; mmu_lock */
	struct ch_transaction *pending, *history;
	bool vfio_coordinated;
	u64 next_id;
	u64 nr_blocked, metadata_ranges;
	u64 blocked_pages, guard_pages;
	u64 begin_batches, begin_flushes, report_batches, install_ranges;
	atomic64_t transient_faults, retired_faults, hugepage_downgrades;
	/* One source of truth for debugfs and cooperating userspace. */
	struct kvm_chameleon_tuning tuning;
	struct ch_tuning_file tuning_files[3];
};

static int tuning_update(struct kvm_chameleon *ch,
			 struct kvm_chameleon_tuning *tuning)
{
	u32 flags = tuning->flags;

	lockdep_assert_held(&ch->command_lock);
	if (tuning->version != KVM_CHAMELEON_VERSION || tuning->reserved ||
	    (flags & ~KVM_CHAMELEON_TUNING_SET_ALL) ||
	    ((flags & KVM_CHAMELEON_TUNING_SET_MODE) &&
	     tuning->ept_mode > KVM_CHAMELEON_EPT_IMMEDIATE) ||
	    ((flags & KVM_CHAMELEON_TUNING_SET_BATCH) && !tuning->batch_pages))
		return -EINVAL;
	if (!ch->tuning.generation && flags != KVM_CHAMELEON_TUNING_SET_ALL)
		return -ENOTCONN;
	if (flags & KVM_CHAMELEON_TUNING_SET_MODE)
		ch->tuning.ept_mode = tuning->ept_mode;
	if (flags & KVM_CHAMELEON_TUNING_SET_BATCH)
		ch->tuning.batch_pages = tuning->batch_pages;
	if (flags & KVM_CHAMELEON_TUNING_SET_WATERMARK)
		ch->tuning.watermark_bytes = tuning->watermark_bytes;
	if (flags) {
		ch->tuning.version = KVM_CHAMELEON_VERSION;
		if (!++ch->tuning.generation)
			ch->tuning.generation = 1;
	}
	*tuning = ch->tuning;
	return 0;
}

static int tuning_ioctl(struct kvm_chameleon *ch, void __user *argp)
{
	struct kvm_chameleon_tuning tuning;
	int ret;

	if (copy_from_user(&tuning, argp, sizeof(tuning)))
		return -EFAULT;
	mutex_lock(&ch->command_lock);
	ret = tuning_update(ch, &tuning);
	mutex_unlock(&ch->command_lock);
	if (ret)
		return ret;
	/* A copyout failure can follow a committed update; GET recovers it. */
	return copy_to_user(argp, &tuning, sizeof(tuning)) ? -EFAULT : 0;
}

static int tuning_open(struct inode *inode, struct file *file)
{
	struct ch_tuning_file *control = inode->i_private;

	if (!kvm_get_kvm_safe(control->kvm))
		return -ENOENT;
	file->private_data = control;
	return 0;
}

static int tuning_release(struct inode *inode, struct file *file)
{
	struct ch_tuning_file *control = file->private_data;

	kvm_put_kvm(control->kvm);
	return 0;
}

static ssize_t tuning_read(struct file *file, char __user *buf, size_t size,
			   loff_t *ppos)
{
	struct ch_tuning_file *control = file->private_data;
	struct kvm_chameleon *ch = control->kvm->arch.chameleon;
	struct kvm_chameleon_tuning tuning = { .version = KVM_CHAMELEON_VERSION };
	char value[32];
	int ret, len;

	mutex_lock(&ch->command_lock);
	ret = tuning_update(ch, &tuning);
	mutex_unlock(&ch->command_lock);
	if (ret)
		return ret;
	if (control->field == KVM_CHAMELEON_TUNING_SET_MODE)
		len = scnprintf(value, sizeof(value), "%s\n",
			tuning.ept_mode ? "immediate" : "deferred");
	else
		len = scnprintf(value, sizeof(value), "%llu\n",
			control->field == KVM_CHAMELEON_TUNING_SET_BATCH ?
			tuning.batch_pages : tuning.watermark_bytes);
	return simple_read_from_buffer(buf, size, ppos, value, len);
}

static ssize_t tuning_write(struct file *file, const char __user *buf,
			    size_t size, loff_t *ppos)
{
	struct ch_tuning_file *control = file->private_data;
	struct kvm_chameleon *ch = control->kvm->arch.chameleon;
	struct kvm_chameleon_tuning tuning = {
		.version = KVM_CHAMELEON_VERSION, .flags = control->field,
	};
	char value[32];
	u64 parsed;
	int ret;

	if (!size || size >= sizeof(value))
		return -EINVAL;
	if (copy_from_user(value, buf, size))
		return -EFAULT;
	value[size] = '\0';
	if (control->field == KVM_CHAMELEON_TUNING_SET_MODE) {
		if (sysfs_streq(value, "deferred") || sysfs_streq(value, "0"))
			tuning.ept_mode = KVM_CHAMELEON_EPT_DEFERRED;
		else if (sysfs_streq(value, "immediate") || sysfs_streq(value, "1"))
			tuning.ept_mode = KVM_CHAMELEON_EPT_IMMEDIATE;
		else
			return -EINVAL;
	} else {
		ret = kstrtou64(value, 0, &parsed);
		if (ret)
			return ret;
		if (control->field == KVM_CHAMELEON_TUNING_SET_BATCH)
			tuning.batch_pages = parsed;
		else
			tuning.watermark_bytes = parsed;
	}
	mutex_lock(&ch->command_lock);
	ret = tuning_update(ch, &tuning);
	mutex_unlock(&ch->command_lock);
	return ret ? ret : size;
}

static const struct file_operations tuning_fops = {
	.owner = THIS_MODULE,
	.open = tuning_open,
	.read = tuning_read,
	.write = tuning_write,
	.llseek = default_llseek,
	.release = tuning_release,
};

static void tuning_debugfs_init(struct kvm *kvm, struct kvm_chameleon *ch)
{
	static const char * const names[] = {
		"ept_mode", "batch_pages", "watermark_bytes",
	};
	struct dentry *dir;
	unsigned int i;

	if (IS_ERR_OR_NULL(kvm->debugfs_dentry))
		return;
	dir = debugfs_create_dir("chameleon", kvm->debugfs_dentry);
	if (IS_ERR_OR_NULL(dir))
		return;
	for (i = 0; i < ARRAY_SIZE(names); i++) {
		ch->tuning_files[i].kvm = kvm;
		ch->tuning_files[i].field = BIT(i);
		debugfs_create_file(names[i], 0600, dir,
				    &ch->tuning_files[i], &tuning_fops);
	}
}

bool kvm_chameleon_enabled(struct kvm *kvm)
{
	return READ_ONCE(kvm->arch.chameleon) != NULL;
}

bool kvm_chameleon_vfio_allowed(struct kvm *kvm)
{
	struct kvm_chameleon *ch = READ_ONCE(kvm->arch.chameleon);

	return !ch || ch->vfio_coordinated;
}

bool kvm_chameleon_supported(void)
{
	return IS_ENABLED(CONFIG_X86_64) &&
		boot_cpu_data.x86_vendor == X86_VENDOR_INTEL &&
		tdp_enabled && tdp_mmu_enabled;
}

struct rw_semaphore *kvm_chameleon_fault_gate(struct kvm *kvm)
{
	struct kvm_chameleon *ch = READ_ONCE(kvm->arch.chameleon);

	if (!ch)
		return NULL;
	/* A fault holds vCPU SRCU. Never block behind BEGIN's slots_lock. */
	if (!down_read_trylock(&ch->fault_gate))
		return ERR_PTR(-EAGAIN);
	return &ch->fault_gate;
}

static bool overlap(unsigned long a, unsigned long b,
		    unsigned long c, unsigned long d)
{
	return a < d && c < b;
}

static unsigned long range_end(const struct ch_range *range)
{
	return range->hva + ((unsigned long)range->user.nr_pages << PAGE_SHIFT);
}

/* mmu_lock held. Persistent retirement takes precedence over a neighbouring
 * transaction's temporary guard. HVA identity also protects SMM/slot aliases. */
static u32 blocked_state(struct kvm *kvm, unsigned long start, unsigned long end)
{
	struct kvm_chameleon *ch = READ_ONCE(kvm->arch.chameleon);
	struct interval_tree_node *node;
	u32 i;

	lockdep_assert_held(&kvm->mmu_lock);
	if (!ch)
		return 0;
	for (node = interval_tree_iter_first(&ch->blocked, start, end - 1);
	     node; node = interval_tree_iter_next(node, start, end - 1)) {
		struct ch_range *range = container_of(node, struct ch_range, blocked);

		if (range->user.state != KVM_CHAMELEON_RANGE_PENDING)
			return range->user.state;
	}
	if (ch->pending)
		for (i = 0; i < ch->pending->nr_guards; i++) {
			struct ch_guard *guard = &ch->pending->guards[i];

			if (overlap(start, end, guard->start, guard->end))
				return KVM_CHAMELEON_RANGE_PENDING;
		}
	return 0;
}

bool kvm_chameleon_fault_blocked(struct kvm *kvm,
		const struct kvm_memory_slot *slot, gfn_t gfn)
{
	unsigned long hva;

	if (!kvm_chameleon_enabled(kvm) || !slot)
		return false;
	hva = __gfn_to_hva_memslot(slot, gfn);
	return blocked_state(kvm, hva, hva + PAGE_SIZE) != 0;
}

int kvm_chameleon_fault(struct kvm_vcpu *vcpu, struct kvm_page_fault *fault)
{
	struct kvm *kvm = vcpu->kvm;
	struct kvm_chameleon *ch = READ_ONCE(kvm->arch.chameleon);
	unsigned long hva;
	u32 state;

	if (!ch || !fault->slot)
		return RET_PF_CONTINUE;
	hva = __gfn_to_hva_memslot(fault->slot, fault->gfn);
	read_lock(&kvm->mmu_lock);
	state = blocked_state(kvm, hva, hva + PAGE_SIZE);
	read_unlock(&kvm->mmu_lock);
	if (!state)
		return RET_PF_CONTINUE;
	if (state == KVM_CHAMELEON_RANGE_PENDING) {
		atomic64_inc(&ch->transient_faults);
		/* Do not sleep holding vCPU SRCU: a memslot updater could wait
		 * for that reader while REPORT waits for slots_lock. */
		return fault->prefetch ? -EAGAIN : RET_PF_RETRY;
	}
	atomic64_inc(&ch->retired_faults);
	kvm_mmu_prepare_memory_fault_exit(vcpu, fault);
	vcpu->run->memory_fault.flags |= KVM_MEMORY_EXIT_FLAG_CHAMELEON;
	if (state == KVM_CHAMELEON_RANGE_ERROR_UNKNOWN)
		vcpu->run->memory_fault.flags |= KVM_MEMORY_EXIT_FLAG_CHAMELEON_ERROR;
	return -EFAULT;
}

int kvm_chameleon_max_level(struct kvm *kvm,
		const struct kvm_memory_slot *slot, gfn_t gfn, int level)
{
	struct kvm_chameleon *ch = READ_ONCE(kvm->arch.chameleon);
	int original = level;

	if (!ch)
		return level;
	lockdep_assert_held(&kvm->mmu_lock);
	while (level > PG_LEVEL_4K) {
		unsigned long pages = KVM_PAGES_PER_HPAGE(level);
		gfn_t first = gfn & ~(pages - 1);
		unsigned long hva;

		if (first >= slot->base_gfn &&
		    first + pages <= slot->base_gfn + slot->npages) {
			hva = __gfn_to_hva_memslot(slot, first);
			if (!blocked_state(kvm, hva, hva + (pages << PAGE_SHIFT)))
				break;
		}
		level--;
	}
	if (level != original)
		atomic64_inc(&ch->hugepage_downgrades);
	return level;
}

int kvm_chameleon_memslot_change(struct kvm *kvm,
		const struct kvm_memory_slot *old,
		const struct kvm_memory_slot *new)
{
	int ret = 0;

	lockdep_assert_held(&kvm->slots_lock);
	if (!kvm_chameleon_enabled(kvm))
		return 0;
	if (new && new->npages &&
	    (new->flags & (KVM_MEM_LOG_DIRTY_PAGES | KVM_MEM_GUEST_MEMFD)))
		return -EOPNOTSUPP;
	read_lock(&kvm->mmu_lock);
	if ((old && old->npages && blocked_state(kvm, old->userspace_addr,
			old->userspace_addr + (old->npages << PAGE_SHIFT))) ||
	    (new && new->npages && blocked_state(kvm, new->userspace_addr,
			new->userspace_addr + (new->npages << PAGE_SHIFT))))
		ret = -EBUSY;
	read_unlock(&kvm->mmu_lock);
	return ret;
}

int kvm_chameleon_enable(struct kvm *kvm, struct kvm_enable_cap *cap)
{
	struct kvm_chameleon *ch;
	struct kvm_device *dev;
	struct kvm_memory_slot *slot;
	struct kvm_vcpu *vcpu;
	unsigned long vcpu_index;
	int as_id, bkt, ret = 0;

	if (cap->flags || cap->args[0] != 1 ||
	    (cap->args[1] & ~KVM_CHAMELEON_ENABLE_VFIO) ||
	    cap->args[2] || cap->args[3])
		return -EINVAL;
	if (!kvm_chameleon_supported() ||
	    kvm->arch.vm_type != KVM_X86_DEFAULT_VM)
		return -EOPNOTSUPP;
	ch = kzalloc(sizeof(*ch), GFP_KERNEL_ACCOUNT);
	if (!ch)
		return -ENOMEM;
	mutex_init(&ch->command_lock);
	init_rwsem(&ch->fault_gate);
	ch->transactions = RB_ROOT;
	ch->blocked = RB_ROOT_CACHED;
	ch->vfio_coordinated = cap->args[1] & KVM_CHAMELEON_ENABLE_VFIO;
	mutex_lock(&kvm->lock);
	if (kvm_chameleon_enabled(kvm)) {
		if (ch->vfio_coordinated != kvm->arch.chameleon->vfio_coordinated)
			ret = -EBUSY;
		goto unlock_vm;
	}
	ret = kvm_trylock_all_vcpus(kvm);
	if (ret) {
		ret = -EBUSY;
		goto unlock_vm;
	}
	kvm_for_each_vcpu(vcpu_index, vcpu, kvm)
		if (kvm_vcpu_has_run(vcpu)) {
			ret = -EBUSY;
			goto unlock_vcpus;
		}
	mutex_lock(&kvm->slots_lock);
	if (kvm->dirty_ring_size) {
		ret = -EOPNOTSUPP;
		goto unlock;
	}
	list_for_each_entry(dev, &kvm->devices, vm_node)
		if (!ch->vfio_coordinated && !strcmp(dev->ops->name, "kvm-vfio")) {
			ret = -EOPNOTSUPP;
			goto unlock;
		}
	for (as_id = 0; as_id < kvm_arch_nr_memslot_as_ids(kvm); as_id++)
		kvm_for_each_memslot(slot, bkt, __kvm_memslots(kvm, as_id))
			if (slot->flags & (KVM_MEM_LOG_DIRTY_PAGES | KVM_MEM_GUEST_MEMFD)) {
				ret = -EOPNOTSUPP;
				goto unlock;
			}
	write_lock(&kvm->mmu_lock);
	WRITE_ONCE(kvm->arch.chameleon, ch);
	write_unlock(&kvm->mmu_lock);
	tuning_debugfs_init(kvm, ch);
	ch = NULL;
unlock:
	mutex_unlock(&kvm->slots_lock);
unlock_vcpus:
	kvm_unlock_all_vcpus(kvm);
unlock_vm:
	mutex_unlock(&kvm->lock);
	kfree(ch);
	return ret;
}

static bool identity_equal(const struct kvm_chameleon_range *a,
			   const struct kvm_chameleon_range *b)
{
	return a->gpa == b->gpa && a->nr_pages == b->nr_pages &&
		a->order == b->order && a->flags == b->flags && a->cookie == b->cookie;
}

static bool valid_identity(const struct kvm_chameleon_range *range)
{
	u64 bytes = (u64)range->nr_pages << PAGE_SHIFT;

	return !range->flags && range->order <= KVM_CHAMELEON_MAX_ORDER &&
		range->order != 1 && range->nr_pages == (1U << range->order) &&
		IS_ALIGNED(range->gpa, bytes) && range->gpa + bytes > range->gpa;
}

/* mmap read and slots_lock held. No faults or backing allocations here. */
static int validate_ram(struct kvm *kvm, struct ch_range *range, bool initial)
{
	gfn_t gfn = range->user.gpa >> PAGE_SHIFT;
	struct kvm_memory_slot *slot = gfn_to_memslot(kvm, gfn);
	struct vm_area_struct *vma;
	unsigned long addr, end;

	if (!slot || slot->id >= KVM_USER_MEM_SLOTS ||
	    range->user.nr_pages > slot->base_gfn + slot->npages - gfn)
		return -EINVAL;
	if (slot->flags & (KVM_MEM_LOG_DIRTY_PAGES | KVM_MEM_GUEST_MEMFD |
			   KVM_MEM_READONLY | KVM_MEMSLOT_INVALID))
		return -EOPNOTSUPP;
	addr = __gfn_to_hva_memslot(slot, gfn);
	end = addr + ((unsigned long)range->user.nr_pages << PAGE_SHIFT);
	if (end <= addr)
		return -EINVAL;
	if (!initial && (slot->id != range->slot_id ||
	    slot->base_gfn != range->slot_gfn || slot->npages != range->slot_pages ||
	    slot->userspace_addr != range->slot_hva || addr != range->hva))
		return -ESTALE;
	while (addr < end) {
		vma = vma_lookup(kvm->mm, addr);
		if (!vma || !vma_is_anonymous(vma) ||
		    (vma->vm_flags & (VM_SHARED | VM_MAYSHARE | VM_IO | VM_PFNMAP |
			VM_MIXEDMAP | VM_HUGETLB | VM_LOCKED | VM_MERGEABLE)) ||
		    (vma->vm_flags & (VM_READ | VM_WRITE)) != (VM_READ | VM_WRITE) ||
		    userfaultfd_armed(vma))
			return -EOPNOTSUPP;
		addr = min(end, vma->vm_end);
	}
	if (initial) {
		range->slot_id = slot->id;
		range->slot_gfn = slot->base_gfn;
		range->slot_pages = slot->npages;
		range->slot_hva = slot->userspace_addr;
		range->hva = __gfn_to_hva_memslot(slot, gfn);
	}
	return 0;
}

static int guard_compare(const void *a, const void *b)
{
	const struct ch_guard *left = a, *right = b;

	return (left->start > right->start) - (left->start < right->start);
}

static void merge_guards(struct ch_transaction *tx)
{
	u32 i, n = 0;

	sort(tx->guards, tx->nr, sizeof(tx->guards[0]), guard_compare, NULL);
	for (i = 0; i < tx->nr; i++) {
		if (n && tx->guards[i].start <= tx->guards[n - 1].end)
			tx->guards[n - 1].end = max(tx->guards[n - 1].end, tx->guards[i].end);
		else
			tx->guards[n++] = tx->guards[i];
	}
	tx->nr_guards = n;
}

/* One non-yielding MMU write section and one actual flush for the entire batch.
 * Private guards, not the generic invalidate counter, span ioctl boundaries. */
static void begin_zap(struct kvm *kvm, struct ch_transaction *tx)
{
	struct kvm_chameleon *ch = kvm->arch.chameleon;
	struct kvm_memory_slot *slot;
	unsigned int i;
	int as_id, bkt;
	u64 pages = 0;

	write_lock(&kvm->mmu_lock);
	ch->pending = tx;
	for (i = 0; i < tx->nr; i++) {
		interval_tree_insert(&tx->ranges[i].blocked, &ch->blocked);
		ch->blocked_pages += tx->ranges[i].user.nr_pages;
		ch->nr_blocked++;
		pages += tx->ranges[i].user.nr_pages;
	}
	kvm_mmu_invalidate_begin(kvm);
	trace_kvm_chameleon(tx->id, 1, tx->nr, pages,
			    kvm->stat.generic.remote_tlb_flush_requests);
	for (i = 0; i < tx->nr_guards; i++) {
		const struct ch_guard *guard = &tx->guards[i];

		ch->guard_pages += (guard->end - guard->start) >> PAGE_SHIFT;
		for (as_id = 0; as_id < kvm_arch_nr_memslot_as_ids(kvm); as_id++) {
			kvm_for_each_memslot(slot, bkt, __kvm_memslots(kvm, as_id)) {
				unsigned long start = max(guard->start, slot->userspace_addr);
				unsigned long end = min(guard->end, slot->userspace_addr +
						       (slot->npages << PAGE_SHIFT));
				struct kvm_gfn_range range = { .slot = slot, .may_block = false };

				if (start >= end)
					continue;
				range.start = slot->base_gfn + ((start - slot->userspace_addr) >> PAGE_SHIFT);
				range.end = slot->base_gfn + ((end - slot->userspace_addr) >> PAGE_SHIFT);
				kvm_mmu_invalidate_range_add(kvm, range.start, range.end);
				kvm_unmap_gfn_range(kvm, &range);
			}
		}
	}
	kvm_flush_remote_tlbs(kvm);
	ch->begin_flushes++;
	trace_kvm_chameleon(tx->id, 2, tx->nr, pages,
			    kvm->stat.generic.remote_tlb_flush_requests);
	kvm_mmu_invalidate_end(kvm);
	ch->begin_batches++;
	write_unlock(&kvm->mmu_lock);
}

static void unlink_range(struct kvm_chameleon *ch, struct ch_transaction *tx,
			 struct ch_range *range)
{
	interval_tree_remove(&range->blocked, &ch->blocked);
	ch->blocked_pages -= range->user.nr_pages;
	ch->nr_blocked--;
	tx->remaining--;
}

static void free_transaction(struct kvm_chameleon *ch, struct ch_transaction *tx)
{
	if (tx) {
		if (tx->accounted)
			ch->metadata_ranges -= tx->nr;
		kfree(tx->guards);
		kvfree(tx);
	}
}

/* command_lock held; completed transactions need no MMU-visible storage. */
static void keep_history(struct kvm_chameleon *ch, struct ch_transaction *tx)
{
	if (tx->remaining)
		return;
	if (ch->history && ch->history != tx) {
		rb_erase(&ch->history->node, &ch->transactions);
		free_transaction(ch, ch->history);
	}
	ch->history = tx;
}

static int check_source_pages(struct page **pages, unsigned long nr)
{
	unsigned long i;

	for (i = 0; i < nr; i++) {
		struct folio *folio = page_folio(pages[i]);

		if (!folio_test_anon(folio) || folio_test_ksm(folio))
			return -EOPNOTSUPP;
		if (folio_maybe_dma_pinned(folio))
			return -EBUSY;
	}
	return 0;
}

static void abort_begin(struct kvm *kvm, struct ch_transaction *tx);

/* A partially installed transaction retains compact identity/history slots
 * until its final member completes. Charge those slots too, so repeatedly
 * reusing installed siblings cannot grow metadata without a RAM-derived
 * bound. One extra batch permits the single completed replay history. */
static bool metadata_available(struct kvm *kvm, struct kvm_chameleon *ch, u32 nr)
{
	struct kvm_memory_slot *slot;
	u64 limit = KVM_CHAMELEON_MAX_RANGES;
	int bkt;

	lockdep_assert_held(&kvm->slots_lock);
	kvm_for_each_memslot(slot, bkt, __kvm_memslots(kvm, 0))
		limit += slot->npages;
	return ch->metadata_ranges + nr <= limit;
}

static int do_begin(struct kvm *kvm, struct kvm_chameleon_batch *batch,
		    struct kvm_chameleon_range *ranges)
{
	struct kvm_chameleon *ch = kvm->arch.chameleon;
	struct ch_transaction *tx;
	struct page **pages;
	unsigned long nr_pages = 0, got = 0;
	u32 i, j;
	int ret = 0;

	if (batch->transaction)
		return -EINVAL;
	if (ch->pending)
		return -EBUSY;
	for (i = 0; i < batch->nr_ranges; i++) {
		if (!valid_identity(&ranges[i]) || ranges[i].status || ranges[i].state)
			return -EINVAL;
		nr_pages += ranges[i].nr_pages;
	}
	tx = kvzalloc(struct_size(tx, ranges, batch->nr_ranges), GFP_KERNEL_ACCOUNT);
	if (!tx)
		return -ENOMEM;
	tx->guards = kmalloc_array(batch->nr_ranges, sizeof(*tx->guards), GFP_KERNEL_ACCOUNT);
	if (!tx->guards) {
		free_transaction(ch, tx);
		return -ENOMEM;
	}
	pages = kvmalloc_array(nr_pages, sizeof(*pages), GFP_KERNEL_ACCOUNT);
	if (!pages) {
		free_transaction(ch, tx);
		return -ENOMEM;
	}
	tx->nr = tx->remaining = batch->nr_ranges;
	/* Wait out old fault-in readers before publishing the guard. A stale
	 * GUP is otherwise capable of repopulating RAM after userspace discard,
	 * even if the final SPTE stale check prevents installing its result. */
	down_write(&ch->fault_gate);
	mutex_lock(&kvm->slots_lock);
	mmap_read_lock(kvm->mm);
	if (!metadata_available(kvm, ch, tx->nr)) {
		ret = -ENOSPC;
		goto unlock;
	}
	for (i = 0; i < tx->nr; i++) {
		struct ch_range *range = &tx->ranges[i];

		range->user = ranges[i];
		range->user.state = KVM_CHAMELEON_RANGE_PENDING;
		ret = validate_ram(kvm, range, true);
		if (ret)
			goto unlock;
		range->blocked.start = range->hva;
		range->blocked.last = range_end(range) - 1;
		for (j = 0; j < i; j++)
			if (overlap(range->hva, range_end(range), tx->ranges[j].hva,
				    range_end(&tx->ranges[j]))) {
				ret = -EINVAL;
				goto unlock;
			}
		/* command_lock serializes tree mutation; the descriptor budget
		 * above also charges completed siblings in partial transactions. */
		if (interval_tree_iter_first(&ch->blocked, range->hva, range_end(range) - 1)) {
			ret = -EBUSY;
			goto unlock;
		}
		/* Anonymous THP can be promoted after BEGIN. Always cover the
		 * PMD-aligned HVA extent, not merely the current host leaf size. */
		tx->guards[i].start = range->hva & HPAGE_PMD_MASK;
		tx->guards[i].end = ALIGN(range_end(range), HPAGE_PMD_SIZE);
	}
	for (i = 0; i < tx->nr; i++) {
		struct ch_range *range = &tx->ranges[i];
		int n = get_user_pages_fast_only(range->hva, range->user.nr_pages,
						FOLL_WRITE, pages + got);

		if (n > 0)
			got += n;
		if (n != range->user.nr_pages) {
			ret = -EAGAIN;
			goto unlock;
		}
	}
	ret = check_source_pages(pages, got);
	if (ret)
		goto unlock;
	if (ch->next_id == U64_MAX) {
		ret = -EOVERFLOW;
		goto unlock;
	}
	tx->id = ++ch->next_id;
	merge_guards(tx);
	/* IDs increase monotonically; insert at the right edge and rebalance. */
	{
		struct rb_node **link = &ch->transactions.rb_node, *parent = NULL;

		while (*link) {
			parent = *link;
			link = &parent->rb_right;
		}
		rb_link_node(&tx->node, parent, link);
		rb_insert_color(&tx->node, &ch->transactions);
	}
	tx->accounted = true;
	ch->metadata_ranges += tx->nr;
	begin_zap(kvm, tx);
	ret = check_source_pages(pages, got);
	if (ret) {
		abort_begin(kvm, tx);
		tx = NULL;
		goto unlock;
	}
	batch->transaction = tx->id;
	for (i = 0; i < tx->nr; i++)
		ranges[i] = tx->ranges[i].user;
unlock:
	mmap_read_unlock(kvm->mm);
	mutex_unlock(&kvm->slots_lock);
	up_write(&ch->fault_gate);
	while (got)
		put_page(pages[--got]);
	kvfree(pages);
	if (ret)
		free_transaction(ch, tx);
	return ret;
}

static struct ch_transaction *find_transaction(struct kvm_chameleon *ch, u64 id)
{
	struct rb_node *node = ch->transactions.rb_node;

	while (node) {
		struct ch_transaction *tx = rb_entry(node, struct ch_transaction, node);

		if (tx->id == id)
			return tx;
		node = id < tx->id ? node->rb_left : node->rb_right;
	}
	return NULL;
}

static int do_report(struct kvm *kvm, struct ch_transaction *tx,
		     struct kvm_chameleon_batch *batch,
		     struct kvm_chameleon_range *ranges)
{
	struct kvm_chameleon *ch = kvm->arch.chameleon;
	u32 i;

	if (tx->reported)
		return -EALREADY;
	if (ch->pending != tx || batch->nr_ranges != tx->nr)
		return -EINVAL;
	for (i = 0; i < tx->nr; i++) {
		if (!identity_equal(&ranges[i], &tx->ranges[i].user))
			return -EINVAL;
		switch (ranges[i].state) {
		case KVM_CHAMELEON_RANGE_DISCARDED:
		case KVM_CHAMELEON_RANGE_NOT_ATTEMPTED:
			if (ranges[i].status)
				return -EINVAL;
			break;
		case KVM_CHAMELEON_RANGE_ERROR_UNKNOWN:
			if (ranges[i].status >= 0 || ranges[i].status < -MAX_ERRNO)
				return -EINVAL;
			break;
		default:
			return -EINVAL;
		}
	}
	write_lock(&kvm->mmu_lock);
	for (i = 0; i < tx->nr; i++) {
		tx->ranges[i].user = ranges[i];
		if (ranges[i].state == KVM_CHAMELEON_RANGE_NOT_ATTEMPTED)
			unlink_range(ch, tx, &tx->ranges[i]);
	}
	ch->pending = NULL;
	ch->guard_pages = 0;
	tx->reported = true;
	ch->report_batches++;
	trace_kvm_chameleon(tx->id, 3, tx->nr, ch->blocked_pages,
			    kvm->stat.generic.remote_tlb_flush_requests);
	write_unlock(&kvm->mmu_lock);
	kfree(tx->guards);
	tx->guards = NULL;
	tx->nr_guards = 0;
	keep_history(ch, tx);
	return 0;
}

static int match_subset(struct ch_transaction *tx,
		       struct kvm_chameleon_batch *batch,
		       struct kvm_chameleon_range *ranges, u16 *indices)
{
	DECLARE_BITMAP(seen, KVM_CHAMELEON_MAX_RANGES);
	u32 i, j;

	bitmap_zero(seen, KVM_CHAMELEON_MAX_RANGES);
	for (i = 0; i < batch->nr_ranges; i++) {
		if (ranges[i].state || ranges[i].status)
			return -EINVAL;
		for (j = 0; j < tx->nr; j++)
			if (identity_equal(&ranges[i], &tx->ranges[j].user))
				break;
		if (j == tx->nr || test_and_set_bit(j, seen))
			return -EINVAL;
		indices[i] = j;
	}
	return 0;
}

static int do_install(struct kvm *kvm, struct ch_transaction *tx,
		      struct kvm_chameleon_batch *batch,
		      struct kvm_chameleon_range *ranges, const u16 *indices)
{
	struct kvm_chameleon *ch = kvm->arch.chameleon;
	struct page **pages;
	unsigned long nr = 0, got = 0, seq;
	u32 i;
	int ret = 0;

	if (!tx->reported)
		return -EBUSY;
	for (i = 0; i < batch->nr_ranges; i++) {
		struct ch_range *range = &tx->ranges[indices[i]];

		if (range->user.state == KVM_CHAMELEON_RANGE_INSTALLED ||
		    range->user.state == KVM_CHAMELEON_RANGE_NOT_ATTEMPTED)
			return -EALREADY;
		nr += range->user.nr_pages;
	}
	pages = kvmalloc_array(nr, sizeof(*pages), GFP_KERNEL_ACCOUNT);
	if (!pages)
		return -ENOMEM;
	mutex_lock(&kvm->slots_lock);
	mmap_read_lock(kvm->mm);
	seq = READ_ONCE(kvm->mmu_invalidate_seq);
	smp_rmb();
	for (i = 0; i < batch->nr_ranges; i++) {
		struct ch_range *range = &tx->ranges[indices[i]];
		int n;

		ret = validate_ram(kvm, range, false);
		if (ret)
			goto unlock;
		/* This API is explicitly fast-only: never silently fault in a
		 * discarded hole, zero page or COW mapping on behalf of INSTALL. */
		n = get_user_pages_fast_only(range->hva, range->user.nr_pages,
					   FOLL_WRITE, pages + got);
		if (n > 0)
			got += n;
		if (n != range->user.nr_pages) {
			ret = -EAGAIN;
			goto unlock;
		}
	}
	write_lock(&kvm->mmu_lock);
	ret = check_source_pages(pages, got);
	if (ret) {
		/* Preserve all blocked ranges when any source is pinned. */
	} else if (seq != kvm->mmu_invalidate_seq || kvm->mmu_invalidate_in_progress) {
		ret = -EAGAIN;
	} else {
		for (i = 0; i < batch->nr_ranges; i++) {
			struct ch_range *range = &tx->ranges[indices[i]];

			range->user.state = KVM_CHAMELEON_RANGE_INSTALLED;
			range->user.status = 0;
			ranges[i] = range->user;
			unlink_range(ch, tx, range);
			ch->install_ranges++;
		}
		trace_kvm_chameleon(tx->id, 4, batch->nr_ranges, nr,
				    kvm->stat.generic.remote_tlb_flush_requests);
	}
	write_unlock(&kvm->mmu_lock);
unlock:
	mmap_read_unlock(kvm->mm);
	mutex_unlock(&kvm->slots_lock);
	while (got)
		put_page(pages[--got]);
	kvfree(pages);
	if (!ret)
		keep_history(ch, tx);
	return ret;
}

static int get_stats(struct kvm *kvm, void __user *argp)
{
	struct kvm_chameleon *ch = kvm->arch.chameleon;
	struct kvm_chameleon_stats stats;

	if (copy_from_user(&stats, argp, sizeof(stats)))
		return -EFAULT;
	if (stats.version != KVM_CHAMELEON_VERSION || stats.flags || stats.reserved)
		return -EINVAL;
	memset(&stats, 0, sizeof(stats));
	stats.version = KVM_CHAMELEON_VERSION;
	mutex_lock(&ch->command_lock);
	read_lock(&kvm->mmu_lock);
	stats.pending_transaction = ch->pending ? ch->pending->id : 0;
	stats.blocked_pages = ch->blocked_pages;
	stats.guard_pages = ch->guard_pages;
	stats.retired_ranges = ch->nr_blocked - (ch->pending ? ch->pending->nr : 0);
	stats.begin_batches = ch->begin_batches;
	stats.begin_flushes = ch->begin_flushes;
	stats.report_batches = ch->report_batches;
	stats.install_ranges = ch->install_ranges;
	stats.remote_tlb_flush_requests = READ_ONCE(kvm->stat.generic.remote_tlb_flush_requests);
	stats.remote_tlb_flush = READ_ONCE(kvm->stat.generic.remote_tlb_flush);
	read_unlock(&kvm->mmu_lock);
	mutex_unlock(&ch->command_lock);
	stats.transient_faults = atomic64_read(&ch->transient_faults);
	stats.retired_faults = atomic64_read(&ch->retired_faults);
	stats.hugepage_downgrades = atomic64_read(&ch->hugepage_downgrades);
	stats.host_available_pages = max_t(long, si_mem_available(), 0);
	stats.max_ranges = KVM_CHAMELEON_MAX_RANGES;
	return copy_to_user(argp, &stats, sizeof(stats)) ? -EFAULT : 0;
}

/* If BEGIN copyout fails, userspace has not been authorized to discard. */
static void abort_begin(struct kvm *kvm, struct ch_transaction *tx)
{
	struct kvm_chameleon *ch = kvm->arch.chameleon;
	u32 i;

	write_lock(&kvm->mmu_lock);
	for (i = 0; i < tx->nr; i++)
		unlink_range(ch, tx, &tx->ranges[i]);
	ch->pending = NULL;
	ch->guard_pages = 0;
	write_unlock(&kvm->mmu_lock);
	rb_erase(&tx->node, &ch->transactions);
	free_transaction(ch, tx);
}

long kvm_chameleon_ioctl(struct kvm *kvm, unsigned int ioctl, unsigned long arg)
{
	struct kvm_chameleon *ch = READ_ONCE(kvm->arch.chameleon);
	void __user *argp = (void __user *)arg;
	struct kvm_chameleon_batch batch;
	struct kvm_chameleon_range *ranges;
	struct ch_transaction *tx = NULL;
	u16 indices[KVM_CHAMELEON_MAX_RANGES];
	u32 i;
	long ret;

	if (!ch)
		return -EOPNOTSUPP;
	if (ioctl == KVM_CHAMELEON_STATS)
		return get_stats(kvm, argp);
	if (ioctl == KVM_CHAMELEON_TUNING)
		return tuning_ioctl(ch, argp);
	if (copy_from_user(&batch, argp, sizeof(batch)))
		return -EFAULT;
	if (batch.version != KVM_CHAMELEON_VERSION || batch.flags || batch.reserved ||
	    !batch.nr_ranges || batch.nr_ranges > KVM_CHAMELEON_MAX_RANGES)
		return -EINVAL;
	ranges = memdup_user(u64_to_user_ptr(batch.ranges),
			   array_size(batch.nr_ranges, sizeof(*ranges)));
	if (IS_ERR(ranges))
		return PTR_ERR(ranges);
	mutex_lock(&ch->command_lock);
	if (ioctl == KVM_CHAMELEON_BEGIN) {
		ret = do_begin(kvm, &batch, ranges);
	} else {
		tx = find_transaction(ch, batch.transaction);
		if (!tx) { ret = -ESTALE; goto unlock; }
		if (ioctl == KVM_CHAMELEON_REPORT) {
			ret = do_report(kvm, tx, &batch, ranges);
		} else {
			ret = match_subset(tx, &batch, ranges, indices);
			if (ret)
				goto unlock;
			if (ioctl == KVM_CHAMELEON_INSTALL)
				ret = do_install(kvm, tx, &batch, ranges, indices);
			else if (ioctl == KVM_CHAMELEON_QUERY)
				for (i = 0; i < batch.nr_ranges; i++)
					ranges[i] = tx->ranges[indices[i]].user;
			else
				ret = -ENOTTY;
		}
	}
	if (!ret && (copy_to_user(u64_to_user_ptr(batch.ranges), ranges,
				 array_size(batch.nr_ranges, sizeof(*ranges))) ||
		     copy_to_user(argp, &batch, sizeof(batch)))) {
		if (ioctl == KVM_CHAMELEON_BEGIN)
			abort_begin(kvm, ch->pending);
		ret = -EFAULT;
	}
unlock:
	mutex_unlock(&ch->command_lock);
	kfree(ranges);
	return ret;
}

void kvm_chameleon_destroy(struct kvm *kvm)
{
	struct kvm_chameleon *ch = kvm->arch.chameleon;
	struct ch_transaction *tx, *next;

	if (!ch)
		return;
	/* VM final destruction: no vCPU/ioctl readers or userspace discard
	 * transactions can execute; no generic invalidate was left outstanding. */
	rbtree_postorder_for_each_entry_safe(tx, next, &ch->transactions, node)
		free_transaction(ch, tx);
	kfree(ch);
	kvm->arch.chameleon = NULL;
}
