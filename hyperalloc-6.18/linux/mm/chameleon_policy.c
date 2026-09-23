// SPDX-License-Identifier: GPL-2.0-only
/* Native PSI decides when; HyperAlloc and the page manager perform actions. */
#include <linux/chameleon.h>
#include <linux/chameleon_policy.h>
#include <linux/chameleon_shadow.h>
#include <linux/chameleon_transport.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/hrtimer.h>
#include <linux/math64.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/pid.h>
#include <linux/psi.h>
#include <linux/sched/mm.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/workqueue.h>

#define POLICY_FREE_UNIT (2ULL << 20)

static DEFINE_MUTEX(policy_control_lock);
static DEFINE_MUTEX(policy_run_lock);
static struct workqueue_struct *policy_wq;
static struct work_struct policy_work;
static struct hrtimer policy_timer;
static bool policy_enabled;
static u64 lease, owner, next_owner;
static struct mm_struct *target_mm; /* mm_count; never keeps a process alive */
static pid_t target_pid;
static unsigned long target_start, target_end;
static u64 epoch_us = 1000, threshold_ppm = 10000, free_batch_pages = 512;
static u64 cold_folios = 8, minimum_local_bytes;
static bool psi_full, discard_test;
static struct chameleon_capacity capacity;
static u64 previous_some, previous_full, previous_stamp;
static u64 feedback_local, feedback_total;
static atomic64_t timer_ticks, coalesced_ticks;

static struct {
	u64 epochs, psi_some_ns, psi_full_ns, psi_some_ppm, psi_full_ppm, delta_ns;
	u64 low_epochs, high_epochs, psi_errors, action_errors;
	u64 free_reclaimed_bytes, free_returned_bytes;
	u64 shadow_prepared_objects, shadow_prepared_pages, shadow_restored_pages;
	u64 discard_ready_objects, discard_installed_pages, capacity_updates, worker_ns;
	int last_error;
} stats;

static void policy_error(int ret)
{
	if (ret) {
		stats.last_error = ret;
		stats.action_errors++;
	}
}

static int policy_feedback(void)
{
	int ret;

	if (capacity.uncertain || !capacity.local_bytes ||
	    capacity.local_bytes > capacity.total_bytes)
		return -EIO;
	if (feedback_local == capacity.local_bytes &&
	    feedback_total == capacity.total_bytes)
		return 0;
	ret = chameleon_set_capacity(capacity.local_bytes, capacity.total_bytes);
	if (!ret) {
		feedback_local = capacity.local_bytes;
		feedback_total = capacity.total_bytes;
		stats.capacity_updates++;
	}
	return ret;
}

static int policy_gate(bool allowed)
{
	if (capacity.reclaim_allowed == allowed)
		return 0;
	return chameleon_policy_allow_reclaim(lease, allowed, &capacity);
}

static u64 policy_budget(void)
{
	u64 owned = (u64)chameleon_shadow_reserved_pages() << PAGE_SHIFT;
	u64 reserved = owned > capacity.retired_bytes ?
		owned - capacity.retired_bytes : 0;
	u64 available;

	if (capacity.local_bytes <= minimum_local_bytes)
		return 0;
	available = capacity.local_bytes - minimum_local_bytes;
	/* READY can precede asynchronous retirement and COMMIT_RESULT delivery.
	 * Use all still-owned folios minus retirement already reflected in this
	 * capacity snapshot. Guest pending credit alone can race that snapshot:
	 * a newer COMMIT_RESULT could otherwise remove a charge before the old
	 * local_bytes includes its discard. Installed-but-unloaded reservations
	 * remain conservatively charged until valid contents are published.
	 */
	return available > reserved ? available - reserved : 0;
}

static void policy_low(void)
{
	struct chameleon_shadow_policy_result result = {};
	struct mm_struct *mm = NULL;
	u64 requested, done = 0, budget;
	int ret;

	stats.low_epochs++;
	ret = policy_gate(true);
	if (ret) { policy_error(ret); return; }
	budget = policy_budget();
	requested = round_down(min(free_batch_pages << PAGE_SHIFT, budget), POLICY_FREE_UNIT);
	if (requested) {
		ret = chameleon_policy_reclaim_free(lease, stats.epochs, requested,
						  &done, &capacity);
		if (ret) { policy_error(ret); return; }
		stats.free_reclaimed_bytes += done;
	}
	if (!cold_folios)
		return;
	/* A real backend can authorize any ordinary Shadow after asynchronous
	 * saving. Reserve its eventual retirement budget before preparation,
	 * including when a backend is registered while this worker is enabled. */
	budget = policy_budget() >> PAGE_SHIFT;
	if (!budget)
		return;
	if (target_mm) {
		if (!mmget_not_zero(target_mm)) {
			policy_error(-ESRCH);
			return;
		}
		mm = target_mm;
	}
	ret = chameleon_shadow_policy_prepare(owner, mm, target_start, target_end,
					     cold_folios, budget, discard_test, &result);
	if (mm)
		mmput(mm);
	stats.shadow_prepared_objects += result.prepared_objects;
	stats.shadow_prepared_pages += result.prepared_pages;
	stats.discard_ready_objects += result.ready_objects;
	/* An empty or temporarily busy candidate set is ordinary best effort. */
	if (ret != -ENOENT && ret != -ENOSPC && ret != -EAGAIN && ret != -EBUSY)
		policy_error(ret);
}

static void policy_high(void)
{
	struct chameleon_shadow_policy_result result = {};
	u64 done = 0, requested;
	int ret;

	stats.high_epochs++;
	ret = policy_gate(false);
	policy_error(ret);
	/* Return is active even in cold-only mode; one free-capacity action
	 * at most in this actual worker epoch, using completion feedback. */
	requested = min(max(free_batch_pages << PAGE_SHIFT, POLICY_FREE_UNIT),
			capacity.hard_reclaimed_bytes);
	requested = round_down(requested, POLICY_FREE_UNIT);
	if (requested && !ret) {
		ret = chameleon_policy_return_free(lease, stats.epochs, requested,
						 &done, &capacity);
		policy_error(ret);
		if (!ret)
			stats.free_returned_bytes += done;
	}
	ret = chameleon_shadow_policy_restore(owner, max_t(u64, cold_folios, 1), &result);
	stats.shadow_restored_pages += result.restored_pages;
	stats.discard_installed_pages += result.installed_pages;
	if (ret != -EAGAIN && ret != -EBUSY && ret != -EOPNOTSUPP)
		policy_error(ret);
}

static void policy_worker(struct work_struct *work)
{
	u64 started = ktime_get_ns(), some, full, stamp, delta;
	int ret;

	mutex_lock(&policy_run_lock);
	if (!READ_ONCE(policy_enabled))
		goto out;
	stats.epochs++;
	ret = psi_memory_snapshot(&some, &full, &stamp);
	if (ret && ret != -EAGAIN) {
		stats.psi_errors++;
		policy_error(ret);
		policy_error(policy_gate(false));
		goto out;
	}
	delta = stamp - previous_stamp;
	stats.psi_some_ns = some;
	stats.psi_full_ns = full;
	stats.delta_ns = delta;
	if (ret || !delta || some < previous_some || full < previous_full) {
		stats.psi_errors++;
		policy_error(policy_gate(false));
		goto rebaseline;
	}
	stats.psi_some_ppm = min_t(u64, 1000000,
		mul_u64_u64_div_u64(some - previous_some, 1000000, delta));
	stats.psi_full_ppm = min_t(u64, 1000000,
		mul_u64_u64_div_u64(full - previous_full, 1000000, delta));
	ret = chameleon_policy_snapshot(lease, &capacity);
	if (ret || capacity.uncertain) {
		policy_error(ret ?: -EIO);
		policy_error(policy_gate(false));
		goto rebaseline;
	}
	if ((psi_full ? stats.psi_full_ppm : stats.psi_some_ppm) >= threshold_ppm)
		policy_high();
	else
		policy_low();
	/* Cold READY is asynchronous: obtain authoritative post-action feedback.
	 * Accounted local capacity is not an RSS estimate or a requested target. */
	ret = chameleon_policy_snapshot(lease, &capacity);
	policy_error(ret);
	if (!ret)
		policy_error(policy_feedback());
rebaseline:
	previous_some = some;
	previous_full = full;
	previous_stamp = stamp;
out:
	stats.worker_ns += ktime_get_ns() - started;
	mutex_unlock(&policy_run_lock);
}

static enum hrtimer_restart policy_tick(struct hrtimer *timer)
{
	if (!READ_ONCE(policy_enabled))
		return HRTIMER_NORESTART;
	atomic64_inc(&timer_ticks);
	if (!queue_work(policy_wq, &policy_work))
		atomic64_inc(&coalesced_ticks);
	hrtimer_forward_now(timer, ns_to_ktime(READ_ONCE(epoch_us) * NSEC_PER_USEC));
	return HRTIMER_RESTART;
}

static int policy_enable(void)
{
	int ret;

	if (policy_enabled || lease)
		return -EBUSY;
	/* Owner zero belongs to manual Shadow objects; never wrap into it. */
	if (next_owner == U64_MAX)
		return -EOVERFLOW;
	if ((!free_batch_pages && !cold_folios) || (discard_test && !target_mm))
		return -EINVAL;
	if (!chameleon_policy_available())
		return -EOPNOTSUPP;
	ret = psi_memory_snapshot(&previous_some, &previous_full, &previous_stamp);
	if (ret && ret != -EAGAIN)
		return ret;
	ret = chameleon_policy_acquire(&lease, &capacity);
	if (ret)
		return ret;
	/* A failed enable may retain its lease if RELEASE fails. Give that
	 * lease its own owner before any later failure can require disable.
	 */
	owner = ++next_owner;
	if (!minimum_local_bytes)
		minimum_local_bytes = capacity.total_bytes / 2;
	if (minimum_local_bytes < POLICY_FREE_UNIT ||
	    minimum_local_bytes > capacity.total_bytes) {
		ret = -EINVAL;
		goto release;
	}
	ret = policy_feedback();
	if (ret)
		goto release;
	WRITE_ONCE(policy_enabled, true);
	hrtimer_start(&policy_timer, ns_to_ktime(epoch_us * NSEC_PER_USEC), HRTIMER_MODE_REL);
	return 0;
release:
	if (!chameleon_policy_release(lease, &capacity))
		lease = 0;
	return ret;
}

static int policy_disable(void)
{
	struct chameleon_shadow_policy_result result = {};
	int ret = 0, rc;
	unsigned int retries = 0;
	u64 hard_before;

	WRITE_ONCE(policy_enabled, false);
	hrtimer_cancel(&policy_timer);
	cancel_work_sync(&policy_work);
	mutex_lock(&policy_run_lock);
	if (!lease)
		goto out;
	ret = policy_gate(false);
	if (ret)
		goto failed;
	do {
		rc = chameleon_shadow_policy_restore(owner, UINT_MAX, &result);
		stats.shadow_restored_pages += result.restored_pages;
		stats.discard_installed_pages += result.installed_pages;
		/* A C4 event already holding the folio may finish after the
		 * policy worker stopped. The closed host gate rejects its discard;
		 * wait for that ACK and then install/restore the owned reservation.
		 */
		if (!result.pending_objects || (rc && rc != -EBUSY &&
		    rc != -EAGAIN && rc != -EOPNOTSUPP))
			break;
		msleep(10);
	} while (++retries < 100);
	if (rc || result.pending_objects) {
		/* Preserve both lease and owner for an explicit disable retry.
		 * A new enable must not orphan this generation's live reservation. */
		ret = rc ?: -EBUSY;
		goto failed;
	}
	/* RELEASE restores this lease's new hard-reclaimed capacity to its
	 * acquisition baseline, then hands control back. Pending C4 cleanup is
	 * independently allowed to INSTALL; release never fabricates data restore.
	 */
	hard_before = capacity.hard_reclaimed_bytes;
	rc = chameleon_policy_release(lease, &capacity);
	if (!rc) {
		if (hard_before > capacity.hard_reclaimed_bytes)
			stats.free_returned_bytes += hard_before - capacity.hard_reclaimed_bytes;
		lease = 0;
		policy_error(policy_feedback());
	} else if (!ret) {
		ret = rc;
	}
failed:
	policy_error(ret);
out:
	mutex_unlock(&policy_run_lock);
	return ret;
}

static int policy_set(const char *key, u64 value)
{
	if (!strcmp(key, "epoch_us")) {
		if (value < 1000 || value > 1000000) return -EINVAL;
		WRITE_ONCE(epoch_us, value);
	} else if (!strcmp(key, "threshold_ppm")) {
		if (value > 1000000) return -EINVAL;
		threshold_ppm = value;
	} else if (!strcmp(key, "psi_full")) {
		if (value > 1) return -EINVAL;
		psi_full = value;
	} else if (!strcmp(key, "free_pages")) {
		if (value > (1ULL << 32) || value % (POLICY_FREE_UNIT >> PAGE_SHIFT)) return -EINVAL;
		free_batch_pages = value;
	} else if (!strcmp(key, "cold_folios")) {
		if (value > LL_CHAMELEON_MAX_RANGES) return -EINVAL;
		cold_folios = value;
	} else if (!strcmp(key, "minimum_local_bytes")) {
		if (value && (value < POLICY_FREE_UNIT || value > (1ULL << 52))) return -EINVAL;
		minimum_local_bytes = value;
	} else if (!strcmp(key, "discard_test")) {
		if (value > 1 || (value && !IS_ENABLED(CONFIG_CHAMELEON_TEST))) return -EINVAL;
		discard_test = value;
	} else {
		return -EINVAL;
	}
	return 0;
}

enum policy_parameter {
	POLICY_EPOCH_US,
	POLICY_THRESHOLD_PPM,
	POLICY_PSI_FULL,
	POLICY_FREE_PAGES,
	POLICY_COLD_FOLIOS,
	POLICY_MINIMUM_LOCAL_BYTES,
	POLICY_DISCARD_TEST,
};

static const char * const policy_parameter_names[] = {
	[POLICY_EPOCH_US] = "epoch_us",
	[POLICY_THRESHOLD_PPM] = "threshold_ppm",
	[POLICY_PSI_FULL] = "psi_full",
	[POLICY_FREE_PAGES] = "free_pages",
	[POLICY_COLD_FOLIOS] = "cold_folios",
	[POLICY_MINIMUM_LOCAL_BYTES] = "minimum_local_bytes",
	[POLICY_DISCARD_TEST] = "discard_test",
};

static int policy_parameter_get(void *data, u64 *value)
{
	mutex_lock(&policy_run_lock);
	switch ((unsigned long)data) {
	case POLICY_EPOCH_US: *value = epoch_us; break;
	case POLICY_THRESHOLD_PPM: *value = threshold_ppm; break;
	case POLICY_PSI_FULL: *value = psi_full; break;
	case POLICY_FREE_PAGES: *value = free_batch_pages; break;
	case POLICY_COLD_FOLIOS: *value = cold_folios; break;
	case POLICY_MINIMUM_LOCAL_BYTES: *value = minimum_local_bytes; break;
	case POLICY_DISCARD_TEST: *value = discard_test; break;
	}
	mutex_unlock(&policy_run_lock);
	return 0;
}

static int policy_parameter_set(void *data, u64 value)
{
	unsigned long parameter = (unsigned long)data;
	int ret = -EINVAL;

	if (parameter >= ARRAY_SIZE(policy_parameter_names))
		return -EINVAL;
	/* Serialize with enable/disable first, then with each complete policy
	 * epoch. The legacy control ABI still requires disabled configuration. */
	mutex_lock(&policy_control_lock);
	if (lease && !policy_enabled) {
		ret = -EBUSY;
		goto out;
	}
	mutex_lock(&policy_run_lock);
	if (policy_enabled) {
		/* A test transport mode may not change under existing objects. */
		if (parameter == POLICY_DISCARD_TEST) {
			ret = -EBUSY;
			goto unlock;
		}
		if ((parameter == POLICY_FREE_PAGES && !value && !cold_folios) ||
		    (parameter == POLICY_COLD_FOLIOS && !value && !free_batch_pages))
			goto unlock;
		if (parameter == POLICY_MINIMUM_LOCAL_BYTES) {
			/* Zero retains the existing automatic half-capacity meaning. */
			if (!value)
				value = capacity.total_bytes / 2;
			if (value > capacity.total_bytes)
				goto unlock;
		}
	}
	ret = policy_set(policy_parameter_names[parameter], value);
unlock:
	mutex_unlock(&policy_run_lock);
out:
	mutex_unlock(&policy_control_lock);
	return ret;
}
DEFINE_DEBUGFS_ATTRIBUTE(policy_parameter_fops, policy_parameter_get,
			 policy_parameter_set, "%llu\n");

static ssize_t policy_control_write(struct file *file, const char __user *buffer,
				   size_t count, loff_t *position)
{
	char command[192], key[48], extra;
	u64 value;
	unsigned long start, bytes;
	struct mm_struct *old = NULL, *new = NULL;
	struct task_struct *task;
	pid_t pid;
	int ret = -EINVAL;

	if (!count || count >= sizeof(command))
		return -EINVAL;
	if (copy_from_user(command, buffer, count))
		return -EFAULT;
	command[count] = 0;
	strim(command);
	mutex_lock(&policy_control_lock);
	if (!strcmp(command, "disable")) {
		ret = policy_disable();
		goto out;
	}
	if (policy_enabled || lease) { ret = -EBUSY; goto out; }
	mutex_lock(&policy_run_lock);
	if (!strcmp(command, "enable")) {
		ret = policy_enable();
	} else if (sscanf(command, "set %47s %llu %c", key, &value, &extra) == 2) {
		ret = policy_set(key, value);
	} else if (!strcmp(command, "clear_target")) {
		old = target_mm;
		target_mm = NULL;
		target_pid = 0;
		target_start = target_end = 0;
		ret = 0;
	} else if (sscanf(command, "target %d %lx %lu %c", &pid, &start, &bytes, &extra) == 3) {
		if (!bytes || !IS_ALIGNED(start, PAGE_SIZE) || !IS_ALIGNED(bytes, PAGE_SIZE) ||
		    start + bytes < start)
			goto unlock;
		rcu_read_lock();
		task = find_task_by_vpid(pid);
		if (task)
			get_task_struct(task);
		rcu_read_unlock();
		if (!task) { ret = -ESRCH; goto unlock; }
		new = get_task_mm(task);
		put_task_struct(task);
		if (!new) { ret = -ESRCH; goto unlock; }
		mmgrab(new);
		old = target_mm;
		target_mm = new;
		target_pid = pid;
		target_start = start;
		target_end = start + bytes;
		ret = 0;
	}
unlock:
	mutex_unlock(&policy_run_lock);
out:
	mutex_unlock(&policy_control_lock);
	/* mmput can run exit_mmap and must not inherit policy mutexes. */
	if (new)
		mmput(new);
	if (old)
		mmdrop(old);
	return ret ? ret : count;
}

static const struct file_operations policy_control_fops = {
	.owner = THIS_MODULE,
	.write = policy_control_write,
	.llseek = noop_llseek,
};

static int policy_stats_show(struct seq_file *seq, void *unused)
{
	mutex_lock(&policy_run_lock);
	seq_printf(seq, "enabled %u\nlease_active %u\nowner %llu\n",
		   READ_ONCE(policy_enabled), !!lease, owner);
	seq_printf(seq, "epoch_us %llu\nthreshold_ppm %llu\npsi_full %u\nfree_pages %llu\n"
		   "cold_folios %llu\nminimum_local_bytes %llu\ndiscard_test %u\n",
		   epoch_us, threshold_ppm, psi_full, free_batch_pages, cold_folios,
		   minimum_local_bytes, discard_test);
	seq_printf(seq, "target_pid %d\ntarget_start 0x%lx\ntarget_end 0x%lx\n",
		   target_pid, target_start, target_end);
	seq_printf(seq, "timer_ticks %lld\ncoalesced_ticks %lld\n",
		   atomic64_read(&timer_ticks), atomic64_read(&coalesced_ticks));
#define STAT(name) seq_printf(seq, #name " %llu\n", stats.name)
	STAT(epochs); STAT(psi_some_ns); STAT(psi_full_ns);
	STAT(psi_some_ppm); STAT(psi_full_ppm); STAT(delta_ns);
	STAT(low_epochs); STAT(high_epochs); STAT(psi_errors); STAT(action_errors);
	STAT(free_reclaimed_bytes); STAT(free_returned_bytes);
	STAT(shadow_prepared_objects); STAT(shadow_prepared_pages);
	STAT(shadow_restored_pages); STAT(discard_ready_objects);
	STAT(discard_installed_pages); STAT(capacity_updates); STAT(worker_ns);
#undef STAT
#define CAP(name) seq_printf(seq, #name " %llu\n", capacity.name)
	CAP(total_bytes); CAP(local_bytes); CAP(hard_reclaimed_bytes);
	CAP(retired_bytes); CAP(soft_reclaimed_bytes);
#undef CAP
	seq_printf(seq, "last_error %d\nreclaim_allowed %u\nuncertain %u\n",
		   stats.last_error, capacity.reclaim_allowed, capacity.uncertain);
	mutex_unlock(&policy_run_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(policy_stats);

static int __init chameleon_policy_init(void)
{
	struct dentry *dir;
	unsigned long i;

	policy_wq = alloc_ordered_workqueue("chameleon_policy", WQ_MEM_RECLAIM);
	if (!policy_wq)
		return -ENOMEM;
	INIT_WORK(&policy_work, policy_worker);
	hrtimer_setup(&policy_timer, policy_tick, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	dir = debugfs_create_dir("chameleon_policy", NULL);
	debugfs_create_file("control", 0600, dir, NULL, &policy_control_fops);
	debugfs_create_file("stats", 0400, dir, NULL, &policy_stats_fops);
	for (i = 0; i < ARRAY_SIZE(policy_parameter_names); i++)
		debugfs_create_file(policy_parameter_names[i], 0600, dir,
				    (void *)i, &policy_parameter_fops);
	return 0;
}
late_initcall(chameleon_policy_init);
