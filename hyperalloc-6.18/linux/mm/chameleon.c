// SPDX-License-Identifier: GPL-2.0
/* Chameleon C1: guest base-page access counters and precise PEBS sampling. */
#include <linux/atomic.h>
#include <linux/bitmap.h>
#include <linux/chameleon.h>
#include <linux/cpu.h>
#include <linux/debugfs.h>
#include <linux/irq_work.h>
#include <linux/math64.h>
#include <linux/memblock.h>
#include <linux/mm.h>
#include <linux/mutex.h>
#include <linux/perf_event.h>
#include <linux/sched/clock.h>
#include <linux/sched/cputime.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/swap.h>
#include <linux/uaccess.h>
#include <linux/workqueue.h>
#include <asm/intel_ds.h>
#include <asm/kvm_para.h>
#include <asm/msr.h>

#define CH_BINS 16
#define CH_RING_SIZE 1024
#define CH_QUERY_MAX 4096

static u16 *counters;
static unsigned long *managed;
static unsigned long nr_pfns;
static atomic64_t bins[CH_BINS], managed_pages;
/* Writers never wait, including a free reached through an NMI GUP failure.
 * Readers retry across active writers or completed update generations. */
static atomic_t writers;
static atomic64_t generation;
static atomic64_t hardware_samples, accepted_samples, dropped_samples;
static atomic64_t pebs_reload_repairs;
static atomic64_t synthetic_samples, gup_failures, ring_drops, free_clears;
static atomic64_t swapout_pages, swapin_pages;
static atomic64_t unprocessed_drops;
static atomic64_t worker_ns, worker_cpu_ns;
static u64 local_bytes, total_bytes, cooling_samples, cooling_epochs;
static u64 samples_since_cooling, processed_samples;
static u64 ptw_pending, ptw_completed, ptw_pending_delta, ptw_completed_delta;
static u64 ptw_base[2], ptw_on_base[2], ptw_run_base[2];
static u64 ptw_on[2], ptw_run[2], ptw_read_errors;
static bool ptw_multiplexed, hotplug_disabled;
static unsigned long sample_period = 4096;
static bool sampling_adaptive = true, cooling_adaptive = true;
static u64 fixed_sample_period = 4096, fixed_cooling_samples;
static u64 hotset_target_percent = 30;
static u64 capacity_updates, period_updates, mode_updates, pmu_period_updates;
static u16 phi = 1;
static bool enabled;
static DEFINE_MUTEX(control_lock);
static DEFINE_MUTEX(drain_lock);
/* Policy feedback can synchronously drain samples from a reclaim worker.
 * Keep an independent rescuer: the draining worker never needs control_lock
 * or waits for policy work, so policy -> sample completion has no cycle. */
static struct workqueue_struct *sample_wq;

struct ch_cpu {
	struct perf_event *events[3];
	atomic64_t pebs_reload_repairs;
	u64 pebs_reload_last_raw;
	u64 ptw_value[2], ptw_on[2], ptw_run[2];
	int ptw_error;
	bool running;
	struct page *ring[CH_RING_SIZE];
	u32 head, tail;
};
static DEFINE_PER_CPU(struct ch_cpu, ch_cpus);

void chameleon_pebs_reloaded(u64 raw)
{
	struct ch_cpu *pc = this_cpu_ptr(&ch_cpus);

	WRITE_ONCE(pc->pebs_reload_last_raw, raw);
	atomic64_inc(&pc->pebs_reload_repairs);
	atomic64_inc(&pebs_reload_repairs);
}

static unsigned int counter_bin(u16 value)
{
	return value ? fls(value) - 1 : 0;
}

static void update_begin(void)
{
	atomic_inc_return(&writers);
}

static void update_end(void)
{
	atomic64_inc_return(&generation);
	atomic_dec(&writers);
}

static void migrate_bin(u16 old, u16 new)
{
	unsigned int from = counter_bin(old), to = counter_bin(new);

	if (from != to) {
		atomic64_dec(&bins[from]);
		atomic64_inc(&bins[to]);
	}
}

/* mode: negative right shift, 0 set, 1 increment; one CAS/histogram path. */
static void change_counter(unsigned long pfn, int mode, u16 value)
{
	u16 old, new, observed;

	if (pfn >= nr_pfns || !test_bit(pfn, managed))
		return;
	update_begin();
	old = READ_ONCE(counters[pfn]);
	for (;;) {
		new = mode < 0 ? old >> min(-mode, 16) : mode ? min_t(u32, old + 1, U16_MAX) : value;
		observed = cmpxchg(&counters[pfn], old, new);
		if (observed == old)
			break;
		old = observed;
	}
	migrate_bin(old, new);
	update_end();
}

void __init chameleon_early_init(void)
{
	nr_pfns = max_pfn;
	counters = memblock_alloc_or_panic(array_size(nr_pfns, sizeof(u16)), PAGE_SIZE);
	managed = memblock_alloc_or_panic(BITS_TO_LONGS(nr_pfns) * sizeof(long), PAGE_SIZE);
	pr_info("Chameleon: contiguous counters phys=%pa bytes=%lu pfns=%lu\n",
		&(phys_addr_t){__pa(counters)}, nr_pfns * sizeof(u16), nr_pfns);
}

void chameleon_free_pages(unsigned long pfn, unsigned int order)
{
	unsigned long end = pfn + (1UL << order);

	if (!counters || end > nr_pfns)
		return;
	for (; pfn < end; pfn++) {
		u16 old;

		if (test_bit(pfn, managed) && !READ_ONCE(counters[pfn]))
			continue;
		update_begin();
		if (!test_and_set_bit(pfn, managed)) {
			atomic64_inc(&managed_pages);
			atomic64_inc(&bins[0]);
		}
		old = xchg(&counters[pfn], 0);
		migrate_bin(old, 0);
		if (old)
			atomic64_inc(&free_clears);
		update_end();
	}
}

/* Caller must retain ownership/pins when snapshotting a folio for C2. */
int chameleon_read_counters(unsigned long pfn, unsigned long nr, u16 *out)
{
	u64 seq;
	unsigned long i;

	if (!counters || pfn >= nr_pfns || nr > nr_pfns - pfn)
		return -EINVAL;
	for (;;) {
		if (atomic_read(&writers)) {
			cond_resched();
			continue;
		}
		seq = atomic64_read(&generation);
		smp_rmb();
		for (i = 0; i < nr; i++)
			out[i] = READ_ONCE(counters[pfn + i]);
		smp_rmb();
		if (!atomic_read(&writers) && seq == atomic64_read(&generation))
			return 0;
		cond_resched();
	}
}

u16 chameleon_hot_threshold(void)
{
	return READ_ONCE(phi);
}

/* Source mappings must be inaccessible, without external pins, and the
 * caller owns both source folios and the unpublished destination. No PTL
 * or LRU spinlock may be held: cooling and the sample worker are serialized
 * here. A transfer is not a new sample and never advances the sample clock. */
int chameleon_transfer_counters(unsigned long dst, const unsigned long *src,
			       unsigned long nr)
{
	unsigned long i, j;

	if (!counters || !nr || nr > HPAGE_PMD_NR || dst >= nr_pfns ||
	    nr > nr_pfns - dst)
		return -EINVAL;
	for (i = 0; i < nr; i++) {
		if (!test_bit(dst + i, managed) || src[i] >= nr_pfns ||
		    !test_bit(src[i], managed) ||
		    (src[i] >= dst && src[i] < dst + nr))
			return -EINVAL;
		for (j = 0; j < i; j++)
			if (src[i] == src[j])
				return -EINVAL;
	}
	mutex_lock(&drain_lock);
	/* Keep snapshots from observing the intermediate duplicated heat. */
	update_begin();
	for (i = 0; i < nr; i++) {
		change_counter(dst + i, 0, READ_ONCE(counters[src[i]]));
		change_counter(src[i], 0, 0);
	}
	update_end();
	mutex_unlock(&drain_lock);
	return 0;
}

/* The same ordering as collapse transfer: mmap/folio -> drain_lock -> PTL.
 * Sample draining and cooling never acquire mmap, folio or PTL locks. The
 * guard spans validation/publication, excluding a cooling scan that could
 * otherwise decay only a suffix of a newly initialized folio. Free remains
 * lockless: it can run from fast-GUP failure in NMI context, but an owned
 * folio cannot be freed/reused while either lifecycle hook operates on it. */
void chameleon_lifecycle_begin(void)
{
	mutex_lock(&drain_lock);
}

void chameleon_lifecycle_end(void)
{
	mutex_unlock(&drain_lock);
}

/* Only a successful finalization may clear source heat. Shadow preparation
 * waits out pre-existing fast-GUP readers; finalization then requires exact
 * mapping + owner references. Every queued sample holds its page reference
 * until after the increment, so it either completes before this boundary or
 * prevents finalization. No queued sample can reheat a retired PFN. Native
 * split keeps the PFN counters; collapse transfers them under drain_lock. */
void chameleon_swapout(unsigned long pfn, unsigned long nr)
{
	unsigned long i;

	lockdep_assert_held(&drain_lock);
	if (WARN_ON_ONCE(!counters || !nr || nr > HPAGE_PMD_NR ||
			pfn >= nr_pfns || nr > nr_pfns - pfn))
		return;
	/* A range snapshot must not expose a partly retired folio. */
	update_begin();
	for (i = 0; i < nr; i++)
		change_counter(pfn + i, 0, 0);
	atomic64_add(nr, &swapout_pages);
	update_end();
}

/* Successful load and complete live-PTE validation precede this hook; no
 * present PTE may be published until it returns. Unmapped slots keep zero
 * heat. A failed load and resident Shadow restore must never call it. */
void chameleon_swapin(unsigned long pfn, unsigned long nr,
		      const unsigned long *live)
{
	u16 initial = chameleon_hot_threshold();
	unsigned long i, initialized = 0;

	lockdep_assert_held(&drain_lock);
	if (WARN_ON_ONCE(!counters || !nr || nr > HPAGE_PMD_NR ||
			pfn >= nr_pfns || nr > nr_pfns - pfn))
		return;
	update_begin();
	for (i = 0; i < nr; i++) {
		if (live && !test_bit(i, live))
			continue;
		change_counter(pfn + i, 0, initial);
		initialized++;
	}
	atomic64_add(initialized, &swapin_pages);
	update_end();
}

static void histogram_snapshot(u64 *out, u64 *pages)
{
	u64 seq;
	int i;

	for (;;) {
		if (atomic_read(&writers)) {
			cond_resched();
			continue;
		}
		seq = atomic64_read(&generation);
		smp_rmb();
		for (i = 0; i < CH_BINS; i++)
			out[i] = atomic64_read(&bins[i]);
		*pages = atomic64_read(&managed_pages);
		smp_rmb();
		if (!atomic_read(&writers) && seq == atomic64_read(&generation))
			return;
		cond_resched();
	}
}

static void update_phi(void)
{
	u64 hist[CH_BINS], pages, sum = 0, target;
	int bin;

	histogram_snapshot(hist, &pages);
	target = max_t(u64, 1,
		DIV_ROUND_UP_ULL((local_bytes >> PAGE_SHIFT) * hotset_target_percent, 100));
	for (bin = CH_BINS - 1; bin > 0; bin--) {
		sum += hist[bin];
		if (sum >= target)
			break;
	}
	WRITE_ONCE(phi, (1U << (bin + 1)) - 1);
}

static void cool_steps(u64 steps)
{
	unsigned long pfn;
	int shift = min_t(u64, steps, 16);

	for (pfn = 0; pfn < nr_pfns; pfn++) {
		if (READ_ONCE(counters[pfn]))
			change_counter(pfn, -shift, 0);
		if (!(pfn & 4095))
			cond_resched();
	}
	cooling_epochs += steps;
}

static void account_sample(void)
{
	processed_samples++;
	if (++samples_since_cooling >= cooling_samples) {
		samples_since_cooling -= cooling_samples;
		cool_steps(1);
	}
}

/* Both counters exclude kernel execution. One IRQ-disabled local callback
 * means no user instruction can execute between their reads or toggles. */
static void snapshot_ptw_local(void *unused)
{
	struct ch_cpu *pc = this_cpu_ptr(&ch_cpus);
	u64 values[2], on[2], running[2];
	int i;

	if (!pc->running)
		return;
	for (i = 0; i < 2; i++) {
		int err = perf_event_read_local(pc->events[i + 1], &values[i],
						 &on[i], &running[i]);
		if (err) {
			pc->ptw_error = err;
			return;
		}
	}
	for (i = 0; i < 2; i++) {
		pc->ptw_value[i] = values[i];
		pc->ptw_on[i] = on[i];
		pc->ptw_run[i] = running[i];
	}
}

static void collect_ptw(void)
{
	u64 values[2] = { ptw_base[0], ptw_base[1] };
	int cpu, i;

	for (i = 0; i < 2; i++) {
		ptw_on[i] = ptw_on_base[i];
		ptw_run[i] = ptw_run_base[i];
	}
	for_each_possible_cpu(cpu) {
		struct ch_cpu *pc = per_cpu_ptr(&ch_cpus, cpu);

		if (!pc->events[1] || !pc->events[2])
			continue;
		if (pc->running)
			smp_call_function_single(cpu, snapshot_ptw_local, NULL, 1);
		if (pc->ptw_error) {
			ptw_read_errors++;
			pc->ptw_error = 0;
		}
		for (i = 0; i < 2; i++) {
			values[i] += pc->ptw_value[i];
			ptw_on[i] += pc->ptw_on[i];
			ptw_run[i] += pc->ptw_run[i];
			if (pc->ptw_on[i] != pc->ptw_run[i])
				ptw_multiplexed = true;
		}
	}
	ptw_pending_delta = values[0] - ptw_pending;
	ptw_completed_delta = values[1] - ptw_completed;
	ptw_pending = values[0];
	ptw_completed = values[1];
}

bool chameleon_get_ptw_snapshot(u64 *pending, u64 *completed)
{
	bool valid;

	mutex_lock(&control_lock);
	if (enabled)
		collect_ptw();
	*pending = ptw_pending;
	*completed = ptw_completed;
	valid = !ptw_read_errors && !ptw_multiplexed;
	mutex_unlock(&control_lock);
	return valid;
}

static void drain_samples(struct work_struct *work)
{
	u64 start = ktime_get_ns(), cpu_start = task_sched_runtime(current), drops;
	int cpu;

	mutex_lock(&drain_lock);
	for_each_possible_cpu(cpu) {
		struct ch_cpu *pc = per_cpu_ptr(&ch_cpus, cpu);
		u32 tail = READ_ONCE(pc->tail), head = smp_load_acquire(&pc->head);

		while (tail != head) {
			struct page *page = pc->ring[tail & (CH_RING_SIZE - 1)];
			unsigned long pfn = page_to_pfn(page);

			if (pfn < nr_pfns && test_bit(pfn, managed)) {
				change_counter(pfn, 1, 0);
				atomic64_inc(&accepted_samples);
			} else {
				atomic64_inc(&dropped_samples);
			}
			/* Pin survives the counter update, so reuse cannot inherit it. */
			put_page(page);
			smp_store_release(&pc->tail, ++tail);
			account_sample();
		}
	}
	/* Dropped hardware samples still advance the paper's sample clock. */
	drops = atomic64_xchg(&unprocessed_drops, 0);
	while (drops--)
		account_sample();
	update_phi();
	mutex_unlock(&drain_lock);
	atomic64_add(ktime_get_ns() - start, &worker_ns);
	atomic64_add(task_sched_runtime(current) - cpu_start, &worker_cpu_ns);
}

static DECLARE_WORK(sample_work, drain_samples);

static void sample_irq_work(struct irq_work *irq)
{
	queue_work(sample_wq, &sample_work);
}
static DEFINE_IRQ_WORK(sample_irq, sample_irq_work);

static void pebs_sample(struct perf_event *event, struct perf_sample_data *data,
			struct pt_regs *regs)
{
	struct ch_cpu *pc = this_cpu_ptr(&ch_cpus);
	struct page *page;
	u32 head = READ_ONCE(pc->head);

	atomic64_inc(&hardware_samples);
	/* Reserve room before taking a reference: no put_page() in this NMI. */
	if ((u32)(head - smp_load_acquire(&pc->tail)) == CH_RING_SIZE) {
		atomic64_inc(&ring_drops);
		goto drop;
	}
	if (!current->mm || !data->addr || data->addr >= TASK_SIZE ||
	    !(data->sample_flags & PERF_SAMPLE_ADDR))
		goto gup_fail;
	pagefault_disable();
	if (!get_user_page_fast_only(data->addr & PAGE_MASK, 0, &page)) {
		pagefault_enable();
		goto gup_fail;
	}
	pagefault_enable();
	pc->ring[head & (CH_RING_SIZE - 1)] = page;
	smp_store_release(&pc->head, head + 1);
	irq_work_queue(&sample_irq);
	return;
gup_fail:
	atomic64_inc(&gup_failures);
drop:
	atomic64_inc(&dropped_samples);
	atomic64_inc(&unprocessed_drops);
	irq_work_queue(&sample_irq);
}

static void toggle_events_local(void *arg)
{
	struct ch_cpu *pc = this_cpu_ptr(&ch_cpus);
	bool on = *(bool *)arg;
	int i;

	if (!on)
		snapshot_ptw_local(NULL);
	for (i = 0; i < 3; i++) {
		struct perf_event *event = pc->events[i];

		if (event) {
			if (on)
				perf_event_enable_local(event);
			else
				perf_event_disable_local(event);
		}
	}
	pc->running = on && pc->events[1] && pc->events[2] &&
		pc->events[1]->state == PERF_EVENT_STATE_ACTIVE &&
		pc->events[2]->state == PERF_EVENT_STATE_ACTIVE;
}

static void toggle_events(bool on)
{
	int cpu;

	for_each_possible_cpu(cpu)
		if (per_cpu(ch_cpus, cpu).events[0])
			smp_call_function_single(cpu, toggle_events_local, &on, 1);
}

static void flush_samples(void)
{
	irq_work_sync(&sample_irq);
	queue_work(sample_wq, &sample_work);
	flush_work(&sample_work);
}

static void release_events(void)
{
	int cpu, i;

	toggle_events(false);
	flush_samples();
	collect_ptw();
	ptw_base[0] = ptw_pending;
	ptw_base[1] = ptw_completed;
	for (i = 0; i < 2; i++) {
		ptw_on_base[i] = ptw_on[i];
		ptw_run_base[i] = ptw_run[i];
	}
	for_each_possible_cpu(cpu)
		for (i = 0; i < 3; i++) {
			struct perf_event **event = &per_cpu(ch_cpus, cpu).events[i];

			if (*event) {
				perf_event_release_kernel(*event);
				*event = NULL;
			}
		}
	enabled = false;
	if (hotplug_disabled) {
		cpu_hotplug_enable();
		hotplug_disabled = false;
	}
}

static int start_events(void)
{
	const u64 config[] = { 0x20d1, 0x1008, 0x0e08 };
	u64 abi;
	int cpu, i, err = 0;

	if (rdmsrq_safe(MSR_KVM_HYPERALLOC_PEBS_MEMINFO, &abi) || abi != 1)
		return -EOPNOTSUPP;
	/* Stable CPU coverage for the lifetime of these per-CPU events. */
	cpu_hotplug_disable();
	hotplug_disabled = true;
	cpus_read_lock();
	for_each_online_cpu(cpu) {
		struct ch_cpu *pc = per_cpu_ptr(&ch_cpus, cpu);

		memset(pc->ptw_value, 0, sizeof(pc->ptw_value));
		memset(pc->ptw_on, 0, sizeof(pc->ptw_on));
		memset(pc->ptw_run, 0, sizeof(pc->ptw_run));
		pc->ptw_error = 0;
		for (i = 0; i < 3; i++) {
			struct perf_event_attr attr = {
				.type = PERF_TYPE_RAW, .size = sizeof(attr),
				.config = config[i], .pinned = 1, .disabled = 1,
				.exclude_kernel = 1, .exclude_hv = 1,
			};
			struct perf_event *event;

			if (!i) {
				attr.precise_ip = 2;
				attr.sample_period = sample_period;
				attr.sample_type = PERF_SAMPLE_IP | PERF_SAMPLE_ADDR | PERF_SAMPLE_TID;
				/* Keep LARGE_PEBS and its native task-switch drain. The
				 * private MEMINFO drain repairs an invalid virtual reload
				 * interval without relying on another PMI. Address
				 * resolution still occurs at callback time. */
			}
			event = perf_event_create_kernel_counter(&attr, cpu, NULL,
							 i ? NULL : pebs_sample, NULL);
			if (IS_ERR(event)) {
				err = PTR_ERR(event);
				goto out;
			}
			/* Disabled, kernel-owned event: bulk samples must reach C1
			 * too, not only the final record handled by the PMI path. */
			if (!i)
				event->attach_state |= PERF_ATTACH_PEBS_CALLBACK;
			per_cpu(ch_cpus, cpu).events[i] = event;
		}
	}
	toggle_events(true);
	for_each_online_cpu(cpu)
		for (i = 0; i < 3; i++)
			if (per_cpu(ch_cpus, cpu).events[i]->state == PERF_EVENT_STATE_ERROR)
				err = -EBUSY;
out:
	cpus_read_unlock();
	if (err)
		release_events();
	else
		enabled = true;
	return err;
}

/* control_lock held. Retain events and update their actual programmed period.
 * Capacity feedback is a no-op when unchanged; no per-epoch PMU recreation. */
static int configure_tracker(u64 local, u64 total, bool adaptive_sample,
			     bool adaptive_cool, u64 fixed_ts, u64 fixed_nc)
{
	u64 period, interval, steps;
	bool capacity_changed, modes_changed, period_changed;
	int cpu, ret = 0;

	if (!sample_wq)
		return -ENODEV;
	if (local < PAGE_SIZE || local > total || total > (1ULL << 52) ||
	    fixed_ts < 512 || fixed_ts > U32_MAX || !fixed_nc || fixed_nc > (1ULL << 40))
		return -EINVAL;
	period = adaptive_sample ? max_t(u64, 512,
		mul_u64_u64_div_u64(local, 4096, total)) : fixed_ts;
	interval = adaptive_cool ? max_t(u64, 1, local >> PAGE_SHIFT) : fixed_nc;
	capacity_changed = local != local_bytes || total != total_bytes;
	modes_changed = adaptive_sample != sampling_adaptive ||
		adaptive_cool != cooling_adaptive || fixed_ts != fixed_sample_period ||
		fixed_nc != fixed_cooling_samples;
	if (!capacity_changed && !modes_changed)
		return 0;
	period_changed = period != sample_period;
	if (period_changed) {
		toggle_events(false);
		flush_samples();
		for_each_possible_cpu(cpu) {
			struct perf_event *event = per_cpu(ch_cpus, cpu).events[0];

			if (event) {
				if (perf_event_period(event, period)) {
					ret = -EIO;
					break;
				}
				pmu_period_updates++;
			}
		}
		if (ret) {
			/* Do not publish new capacity/modes after a partial PMU update. */
			for_each_possible_cpu(cpu) {
				struct perf_event *event = per_cpu(ch_cpus, cpu).events[0];

				if (event && perf_event_period(event, sample_period)) {
					/* A failed rollback must not leave mixed CPU periods. */
					release_events();
					break;
				}
			}
			if (enabled)
				toggle_events(true);
			return ret;
		}
		period_updates++;
	}
	mutex_lock(&drain_lock);
	local_bytes = local;
	total_bytes = total;
	sampling_adaptive = adaptive_sample;
	cooling_adaptive = adaptive_cool;
	fixed_sample_period = fixed_ts;
	fixed_cooling_samples = fixed_nc;
	sample_period = period;
	cooling_samples = interval;
	steps = div64_u64(samples_since_cooling, interval);
	if (steps) {
		samples_since_cooling %= interval;
		/* Capacity/mode changes may cross many epochs. One saturated shift
		 * gives the exact final counters without repeated full-array scans. */
		cool_steps(steps);
	}
	update_phi();
	capacity_updates += capacity_changed;
	mode_updates += modes_changed;
	mutex_unlock(&drain_lock);
	if (period_changed && enabled)
		toggle_events(true);
	return 0;
}

int chameleon_set_capacity(u64 local, u64 total)
{
	int ret;

	mutex_lock(&control_lock);
	ret = configure_tracker(local, total, sampling_adaptive, cooling_adaptive,
				fixed_sample_period, fixed_cooling_samples);
	mutex_unlock(&control_lock);
	return ret;
}

enum tracker_parameter {
	TRACKER_SAMPLING_ADAPTIVE,
	TRACKER_COOLING_ADAPTIVE,
	TRACKER_FIXED_SAMPLE_PERIOD,
	TRACKER_FIXED_COOLING_SAMPLES,
	TRACKER_HOTSET_TARGET_PERCENT,
};

static const char * const tracker_parameter_names[] = {
	[TRACKER_SAMPLING_ADAPTIVE] = "sampling_adaptive",
	[TRACKER_COOLING_ADAPTIVE] = "cooling_adaptive",
	[TRACKER_FIXED_SAMPLE_PERIOD] = "fixed_sample_period",
	[TRACKER_FIXED_COOLING_SAMPLES] = "fixed_cooling_samples",
	[TRACKER_HOTSET_TARGET_PERCENT] = "hotset_target_percent",
};

static int tracker_parameter_get(void *data, u64 *value)
{
	mutex_lock(&control_lock);
	switch ((unsigned long)data) {
	case TRACKER_SAMPLING_ADAPTIVE: *value = sampling_adaptive; break;
	case TRACKER_COOLING_ADAPTIVE: *value = cooling_adaptive; break;
	case TRACKER_FIXED_SAMPLE_PERIOD: *value = fixed_sample_period; break;
	case TRACKER_FIXED_COOLING_SAMPLES: *value = fixed_cooling_samples; break;
	case TRACKER_HOTSET_TARGET_PERCENT: *value = hotset_target_percent; break;
	}
	mutex_unlock(&control_lock);
	return 0;
}

static int tracker_parameter_set(void *data, u64 value)
{
	bool adaptive_sample, adaptive_cool;
	u64 fixed_ts, fixed_nc;
	int ret = 0;

	/* Reuse the control path's validation, PMU rollback and drain locking.
	 * A fixed value can be staged while its adaptive mode remains active. */
	mutex_lock(&control_lock);
	adaptive_sample = sampling_adaptive;
	adaptive_cool = cooling_adaptive;
	fixed_ts = fixed_sample_period;
	fixed_nc = fixed_cooling_samples;
	switch ((unsigned long)data) {
	case TRACKER_SAMPLING_ADAPTIVE:
		if (value > 1) { ret = -EINVAL; goto out; }
		adaptive_sample = value;
		break;
	case TRACKER_COOLING_ADAPTIVE:
		if (value > 1) { ret = -EINVAL; goto out; }
		adaptive_cool = value;
		break;
	case TRACKER_FIXED_SAMPLE_PERIOD:
		fixed_ts = value;
		break;
	case TRACKER_FIXED_COOLING_SAMPLES:
		fixed_nc = value;
		break;
	case TRACKER_HOTSET_TARGET_PERCENT:
		if (!value || value > 100) { ret = -EINVAL; goto out; }
		mutex_lock(&drain_lock);
		hotset_target_percent = value;
		update_phi();
		mutex_unlock(&drain_lock);
		goto out;
	default:
		ret = -EINVAL;
		goto out;
	}
	ret = configure_tracker(local_bytes, total_bytes, adaptive_sample,
				adaptive_cool, fixed_ts, fixed_nc);
out:
	mutex_unlock(&control_lock);
	return ret;
}
DEFINE_DEBUGFS_ATTRIBUTE(tracker_parameter_fops, tracker_parameter_get,
			 tracker_parameter_set, "%llu\n");

static ssize_t control_write(struct file *file, const char __user *buf,
			     size_t len, loff_t *pos)
{
	char text[128], extra;
	u64 local, total;
	int ret = 0;
	unsigned long pfn;

	if (!len || len >= sizeof(text))
		return -EINVAL;
	if (copy_from_user(text, buf, len))
		return -EFAULT;
	text[len] = 0;
	strim(text);
	mutex_lock(&control_lock);
	if (!strcmp(text, "enable")) {
		if (!enabled)
			ret = start_events();
	} else if (!strcmp(text, "disable")) {
		if (enabled)
			release_events();
	} else if (!strcmp(text, "drain")) {
		toggle_events(false);
		lru_add_drain_all();
		flush_samples();
		lru_add_drain_all();
		if (enabled) {
			collect_ptw();
			toggle_events(true);
		}
	} else if (!strcmp(text, "reset")) {
		if (enabled) {
			ret = -EBUSY;
			goto out;
		}
		flush_samples();
		mutex_lock(&drain_lock);
		for (pfn = 0; pfn < nr_pfns; pfn++) {
			if (READ_ONCE(counters[pfn]))
				change_counter(pfn, 0, 0);
			if (!(pfn & 4095))
				cond_resched();
		}
		atomic64_set(&hardware_samples, 0);
		atomic64_set(&pebs_reload_repairs, 0);
		{
			int cpu;

			for_each_possible_cpu(cpu) {
				struct ch_cpu *pc = &per_cpu(ch_cpus, cpu);

				atomic64_set(&pc->pebs_reload_repairs, 0);
				WRITE_ONCE(pc->pebs_reload_last_raw, 0);
			}
		}
		atomic64_set(&accepted_samples, 0);
		atomic64_set(&dropped_samples, 0);
		atomic64_set(&synthetic_samples, 0);
		atomic64_set(&gup_failures, 0);
		atomic64_set(&ring_drops, 0);
		atomic64_set(&unprocessed_drops, 0);
		atomic64_set(&free_clears, 0);
		atomic64_set(&swapout_pages, 0);
		atomic64_set(&swapin_pages, 0);
		atomic64_set(&worker_ns, 0);
		atomic64_set(&worker_cpu_ns, 0);
		cooling_epochs = samples_since_cooling = processed_samples = 0;
		ptw_pending = ptw_completed = ptw_pending_delta = ptw_completed_delta = 0;
		memset(ptw_base, 0, sizeof(ptw_base));
		memset(ptw_on_base, 0, sizeof(ptw_on_base));
		memset(ptw_run_base, 0, sizeof(ptw_run_base));
		memset(ptw_on, 0, sizeof(ptw_on));
		memset(ptw_run, 0, sizeof(ptw_run));
		ptw_read_errors = 0;
		ptw_multiplexed = false;
		update_phi();
		mutex_unlock(&drain_lock);
	} else if (sscanf(text, "capacity %llu %llu %c", &local, &total, &extra) == 2) {
		ret = configure_tracker(local, total, sampling_adaptive, cooling_adaptive,
					fixed_sample_period, fixed_cooling_samples);
	} else if (!strcmp(text, "sampling adaptive")) {
		ret = configure_tracker(local_bytes, total_bytes, true, cooling_adaptive,
					fixed_sample_period, fixed_cooling_samples);
	} else if (sscanf(text, "sampling fixed %llu %c", &local, &extra) == 1) {
		ret = configure_tracker(local_bytes, total_bytes, false, cooling_adaptive,
					local, fixed_cooling_samples);
	} else if (!strcmp(text, "cooling adaptive")) {
		ret = configure_tracker(local_bytes, total_bytes, sampling_adaptive, true,
					fixed_sample_period, fixed_cooling_samples);
	} else if (sscanf(text, "cooling fixed %llu %c", &local, &extra) == 1) {
		ret = configure_tracker(local_bytes, total_bytes, sampling_adaptive, false,
					fixed_sample_period, local);
	} else {
		ret = -EINVAL;
	}
out:
	mutex_unlock(&control_lock);
	return ret ? ret : len;
}

static const struct file_operations control_fops = {
	.owner = THIS_MODULE, .write = control_write, .llseek = noop_llseek,
};

static int stats_show(struct seq_file *m, void *unused)
{
	u64 hist[CH_BINS], pages, sum = 0;
	int i;

	mutex_lock(&control_lock);
	mutex_lock(&drain_lock);
	if (enabled)
		collect_ptw();
	histogram_snapshot(hist, &pages);
	for (i = 0; i < CH_BINS; i++)
		sum += hist[i];
#define CH_STAT(name, value) seq_printf(m, name " %llu\n", (u64)(value))
	CH_STAT("enabled", enabled);
	CH_STAT("metadata_phys", __pa(counters));
	CH_STAT("metadata_bytes", nr_pfns * sizeof(u16));
	CH_STAT("coverage_bytes", BITS_TO_LONGS(nr_pfns) * sizeof(long));
	CH_STAT("max_pfn", nr_pfns);
	CH_STAT("managed_pages", pages);
	CH_STAT("histogram_sum", sum);
	CH_STAT("local_bytes", local_bytes);
	CH_STAT("total_bytes", total_bytes);
	CH_STAT("sample_period", sample_period);
	seq_puts(m, "pebs_reload_mode guarded_auto\npebs_buffer_mode large\n");
	CH_STAT("pebs_reload_repairs", atomic64_read(&pebs_reload_repairs));
	CH_STAT("pebs_event_count_approximate", atomic64_read(&pebs_reload_repairs) != 0);
	CH_STAT("cooling_samples", cooling_samples);
	seq_printf(m, "sampling_mode %s\ncooling_mode %s\n",
		   sampling_adaptive ? "adaptive" : "fixed",
		   cooling_adaptive ? "adaptive" : "fixed");
	CH_STAT("sampling_adaptive", sampling_adaptive);
	CH_STAT("cooling_adaptive", cooling_adaptive);
	CH_STAT("fixed_sample_period", fixed_sample_period);
	CH_STAT("fixed_cooling_samples", fixed_cooling_samples);
	CH_STAT("hotset_target_percent", hotset_target_percent);
	CH_STAT("capacity_updates", capacity_updates);
	CH_STAT("period_updates", period_updates);
	CH_STAT("mode_updates", mode_updates);
	CH_STAT("pmu_period_updates", pmu_period_updates);
	{
		unsigned int events = 0, mismatches = 0;
		int cpu;

		for_each_possible_cpu(cpu) {
			struct perf_event *event = per_cpu(ch_cpus, cpu).events[0];

			if (event) {
				events++;
				mismatches += READ_ONCE(event->attr.sample_period) != sample_period ||
					READ_ONCE(event->hw.sample_period) != sample_period;
			}
		}
		CH_STAT("sampling_events", events);
		CH_STAT("period_mismatches", mismatches);
	}
	CH_STAT("phi", phi);
	CH_STAT("cooling_epochs", cooling_epochs);
	CH_STAT("samples_since_cooling", samples_since_cooling);
	CH_STAT("processed_samples", processed_samples);
	CH_STAT("hardware_samples", atomic64_read(&hardware_samples));
	CH_STAT("synthetic_samples", atomic64_read(&synthetic_samples));
	CH_STAT("accepted_samples", atomic64_read(&accepted_samples));
	CH_STAT("dropped_samples", atomic64_read(&dropped_samples));
	CH_STAT("gup_failures", atomic64_read(&gup_failures));
	CH_STAT("ring_drops", atomic64_read(&ring_drops));
	CH_STAT("free_clears", atomic64_read(&free_clears));
	CH_STAT("swapout_pages", atomic64_read(&swapout_pages));
	CH_STAT("swapin_pages", atomic64_read(&swapin_pages));
	CH_STAT("worker_ns", atomic64_read(&worker_ns));
	CH_STAT("worker_cpu_ns", atomic64_read(&worker_cpu_ns));
	CH_STAT("ptw_pending", ptw_pending);
	CH_STAT("ptw_completed", ptw_completed);
	CH_STAT("ptw_pending_delta", ptw_pending_delta);
	CH_STAT("ptw_completed_delta", ptw_completed_delta);
	CH_STAT("ptw_pending_enabled_ns", ptw_on[0]);
	CH_STAT("ptw_pending_running_ns", ptw_run[0]);
	CH_STAT("ptw_completed_enabled_ns", ptw_on[1]);
	CH_STAT("ptw_completed_running_ns", ptw_run[1]);
	CH_STAT("ptw_read_errors", ptw_read_errors);
	CH_STAT("ptw_multiplexed", ptw_multiplexed);
	CH_STAT("ptw_cycles", ptw_completed_delta ? div64_u64(ptw_pending_delta, ptw_completed_delta) : 0);
	for (i = 0; i < CH_BINS; i++)
		seq_printf(m, "bin%d %llu\n", i, hist[i]);
#undef CH_STAT
	mutex_unlock(&drain_lock);
	mutex_unlock(&control_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(stats);

/* Separate from ordinary stats: reading a perf counter through perf itself can
 * drain PEBS and change the state under investigation. Take an observational
 * CPU-local snapshot instead; NMIs can still run between individual reads. */
struct ch_pmu_snapshot {
	int present, state, hw_state, idx, event_limit;
	u32 msr_errors;
	u64 flags, interrupts, interrupts_seq, attr_period, period, last_period;
	u64 reload_repairs, reload_last_raw;
	s64 period_left, prev_count, count;
	u64 counter_msr, raw, eventsel, global_ctrl, global_status, pebs_enable;
	u64 ds_area, ds_base, ds_index, ds_max, ds_threshold, ds_reset;
};

static void snapshot_pmu_local(void *arg)
{
	struct ch_pmu_snapshot *s = arg;
	struct perf_event *event = this_cpu_ptr(&ch_cpus)->events[0];
	struct hw_perf_event *hwc;

	if (!event)
		return;
	hwc = &event->hw;
	s->present = 1;
	s->state = READ_ONCE(event->state);
	s->hw_state = READ_ONCE(hwc->state);
	s->idx = READ_ONCE(hwc->idx);
	s->event_limit = atomic_read(&event->event_limit);
	s->flags = (u32)READ_ONCE(hwc->flags);
	s->reload_repairs = atomic64_read(&this_cpu_ptr(&ch_cpus)->pebs_reload_repairs);
	s->reload_last_raw = READ_ONCE(this_cpu_ptr(&ch_cpus)->pebs_reload_last_raw);
	s->interrupts = READ_ONCE(hwc->interrupts);
	s->interrupts_seq = READ_ONCE(hwc->interrupts_seq);
	s->attr_period = READ_ONCE(event->attr.sample_period);
	s->period = READ_ONCE(hwc->sample_period);
	s->last_period = READ_ONCE(hwc->last_period);
	s->period_left = local64_read(&hwc->period_left);
	s->prev_count = local64_read(&hwc->prev_count);
	s->count = local64_read(&event->count);
	s->counter_msr = READ_ONCE(hwc->event_base);
#define CH_PMU_MSR(msr, member, bit) do { \
	if (rdmsrq_safe(msr, &s->member)) \
		s->msr_errors |= BIT(bit); \
} while (0)
	if (s->idx >= 0 && s->counter_msr) {
		CH_PMU_MSR(s->counter_msr, raw, 0);
		CH_PMU_MSR(hwc->config_base, eventsel, 1);
	} else {
		s->msr_errors |= BIT(0) | BIT(1);
	}
	CH_PMU_MSR(MSR_CORE_PERF_GLOBAL_CTRL, global_ctrl, 2);
	CH_PMU_MSR(MSR_CORE_PERF_GLOBAL_STATUS, global_status, 3);
	CH_PMU_MSR(MSR_IA32_PEBS_ENABLE, pebs_enable, 4);
	CH_PMU_MSR(MSR_IA32_DS_AREA, ds_area, 5);
#undef CH_PMU_MSR
#ifdef CONFIG_CPU_SUP_INTEL
	{
		/* The CPU entry area aliases this known per-CPU backing store.
		 * Do not dereference an address obtained from the virtual MSR. */
		struct debug_store *ds = this_cpu_ptr(&cpu_debug_store);

		s->ds_base = READ_ONCE(ds->pebs_buffer_base);
		s->ds_index = READ_ONCE(ds->pebs_index);
		s->ds_max = READ_ONCE(ds->pebs_absolute_maximum);
		s->ds_threshold = READ_ONCE(ds->pebs_interrupt_threshold);
		if (s->idx >= 0 && s->idx < MAX_PEBS_EVENTS)
			s->ds_reset = READ_ONCE(ds->pebs_event_reset[s->idx]);
	}
#endif
}

static int pmu_show(struct seq_file *m, void *unused)
{
	int cpu;

	/* Event lifetime and CPU hotplug are stable while control_lock is held
	 * and tracker events exist. No event is stopped, read through perf, or
	 * reprogrammed by this interface. Raw MSRs inside KVM remain virtual. */
	mutex_lock(&control_lock);
	for_each_possible_cpu(cpu) {
		struct ch_pmu_snapshot s = {};
		int ret;

		if (!per_cpu(ch_cpus, cpu).events[0])
			continue;
		ret = smp_call_function_single(cpu, snapshot_pmu_local, &s, 1);
		seq_printf(m, "cpu %d snapshot_error %d present %d state %d hw_state %d idx %d event_limit %d\n",
			   cpu, ret, s.present, s.state, s.hw_state, s.idx, s.event_limit);
		seq_printf(m, "cpu %d flags %#llx interrupts %llu interrupts_seq %llu attr_period %llu sample_period %llu last_period %llu period_left %lld prev_count %lld count %lld\n",
			   cpu, s.flags, s.interrupts, s.interrupts_seq, s.attr_period,
			   s.period, s.last_period, s.period_left, s.prev_count, s.count);
		seq_printf(m, "cpu %d reload_repairs %llu reload_last_raw %#llx\n",
			   cpu, s.reload_repairs, s.reload_last_raw);
		seq_printf(m, "cpu %d msr_errors %#x counter_msr %#llx raw %#llx eventsel %#llx global_ctrl %#llx global_status %#llx pebs_enable %#llx\n",
			   cpu, s.msr_errors, s.counter_msr, s.raw, s.eventsel,
			   s.global_ctrl, s.global_status, s.pebs_enable);
		seq_printf(m, "cpu %d ds_area %#llx ds_base %#llx ds_index %#llx ds_max %#llx ds_threshold %#llx ds_reset %#llx\n",
			   cpu, s.ds_area, s.ds_base, s.ds_index, s.ds_max,
			   s.ds_threshold, s.ds_reset);
	}
	mutex_unlock(&control_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(pmu);

struct ch_range { unsigned long pfn, nr; };

static int range_show(struct seq_file *m, void *unused)
{
	struct ch_range *range = m->private;
	u16 *values;
	unsigned long i;
	int ret;

	if (!range->nr)
		return 0;
	values = kmalloc_array(range->nr, sizeof(*values), GFP_KERNEL);
	if (!values)
		return -ENOMEM;
	ret = chameleon_read_counters(range->pfn, range->nr, values);
	if (!ret)
		for (i = 0; i < range->nr; i++)
			seq_printf(m, "%lu %u\n", range->pfn + i, values[i]);
	kfree(values);
	return ret;
}

static int range_open(struct inode *inode, struct file *file)
{
	struct ch_range *range = kzalloc(sizeof(*range), GFP_KERNEL);
	int ret;

	if (!range)
		return -ENOMEM;
	ret = single_open(file, range_show, range);
	if (ret)
		kfree(range);
	return ret;
}

static ssize_t range_write(struct file *file, const char __user *buf,
			   size_t len, loff_t *pos)
{
	struct seq_file *m = file->private_data;
	struct ch_range *range = m->private;
	unsigned long pfn, nr;
	char text[80], extra;

	if (!len || len >= sizeof(text))
		return -EINVAL;
	if (copy_from_user(text, buf, len))
		return -EFAULT;
	text[len] = 0;
	if (sscanf(text, "%lu %lu %c", &pfn, &nr, &extra) != 2 ||
	    !nr || nr > CH_QUERY_MAX || pfn >= nr_pfns || nr > nr_pfns - pfn)
		return -EINVAL;
	mutex_lock(&m->lock);
	range->pfn = pfn;
	range->nr = nr;
	m->count = m->from = m->index = m->read_pos = 0;
	file->f_pos = 0;
	mutex_unlock(&m->lock);
	return len;
}

static int range_release(struct inode *inode, struct file *file)
{
	struct seq_file *m = file->private_data;

	kfree(m->private);
	return single_release(inode, file);
}

static const struct file_operations range_fops = {
	.owner = THIS_MODULE, .open = range_open, .read = seq_read,
	.write = range_write, .llseek = seq_lseek, .release = range_release,
};

#ifdef CONFIG_CHAMELEON_TEST
static ssize_t inject_write(struct file *file, const char __user *buf,
			    size_t len, loff_t *pos)
{
	char text[96], extra;
	unsigned long addr, count, i, pfn;
	struct page *page;
	int ret = 0;

	if (!len || len >= sizeof(text))
		return -EINVAL;
	if (copy_from_user(text, buf, len))
		return -EFAULT;
	text[len] = 0;
	if (sscanf(text, "%lx %lu %c", &addr, &count, &extra) != 2 ||
	    !count || count > 10000000 || addr >= TASK_SIZE)
		return -EINVAL;
	mutex_lock(&control_lock);
	if (enabled) {
		ret = -EBUSY;
		goto out;
	}
	if (!get_user_page_fast_only(addr & PAGE_MASK, 0, &page)) {
		ret = -EFAULT;
		goto out;
	}
	pfn = page_to_pfn(page);
	if (pfn >= nr_pfns || !test_bit(pfn, managed)) {
		ret = -EINVAL;
		goto put;
	}
	mutex_lock(&drain_lock);
	for (i = 0; i < count; i++) {
		change_counter(pfn, 1, 0);
		atomic64_inc(&synthetic_samples);
		account_sample();
		if (!(i & 4095))
			cond_resched();
	}
	update_phi();
	mutex_unlock(&drain_lock);
put:
	put_page(page);
out:
	mutex_unlock(&control_lock);
	return ret ? ret : len;
}
static const struct file_operations inject_fops = {
	.owner = THIS_MODULE, .write = inject_write, .llseek = noop_llseek,
};
#endif

static int __init chameleon_init(void)
{
	struct dentry *dir;
	unsigned long i;

	if (!counters)
		return -ENOMEM;
	sample_wq = alloc_ordered_workqueue("chameleon_samples", WQ_MEM_RECLAIM);
	if (!sample_wq)
		return -ENOMEM;
	local_bytes = total_bytes = (u64)totalram_pages() << PAGE_SHIFT;
	cooling_samples = max_t(u64, 1, local_bytes >> PAGE_SHIFT);
	fixed_cooling_samples = cooling_samples;
	dir = debugfs_create_dir("chameleon", NULL);
	debugfs_create_file("control", 0200, dir, NULL, &control_fops);
	debugfs_create_file("stats", 0400, dir, NULL, &stats_fops);
	debugfs_create_file("pmu", 0400, dir, NULL, &pmu_fops);
	debugfs_create_file("range", 0600, dir, NULL, &range_fops);
	for (i = 0; i < ARRAY_SIZE(tracker_parameter_names); i++)
		debugfs_create_file(tracker_parameter_names[i], 0600, dir,
				    (void *)i, &tracker_parameter_fops);
#ifdef CONFIG_CHAMELEON_TEST
	debugfs_create_file("inject", 0200, dir, NULL, &inject_fops);
#endif
	return 0;
}
late_initcall(chameleon_init);
