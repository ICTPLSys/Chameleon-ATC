// SPDX-License-Identifier: GPL-2.0-only
/*
 * Test-only asynchronous Chameleon backend. This is not Hermit and never
 * authorizes reclamation or releases the resident source folio.
 *
 * insmod creates /sys/kernel/debug/chameleon_shadow_backend, unregistered.
 * control: register | unregister | delay MS | error 0|1 | select TOKEN |
 *          complete TOKEN STATUS OFFSET | reset
 * error injects -EIO into the next submitted job's asynchronous completion.
 * complete directly exercises duplicate/stale completion validation; it does
 * not claim that another copy was saved. reset discards inactive snapshots.
 * snapshot: raw bytes for the selected token; open returns -EAGAIN until copy
 * finishes. Each open pins its immutable snapshot independently of reset.
 * stats: counters and last completion status/return code; manual calls are
 * counted separately from worker completions.
 *
 * The worker deliberately takes no folio reference. The core's isolation
 * reference keeps it valid until cancel() synchronously drains this worker.
 * Copies remain independently readable after cancellation. Registration holds
 * a module reference so rmmod cannot bypass the core's -EBUSY protection.
 */
#include <linux/chameleon_shadow.h>
#include <linux/debugfs.h>
#include <linux/highmem.h>
#include <linux/list.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/refcount.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/workqueue.h>

#define TEST_MAX_JOBS 64
#define TEST_MAX_BYTES (32UL << 20)

struct test_job {
	struct list_head list;
	struct delayed_work work;
	refcount_t refs;
	struct folio *source; /* borrowed only until cancel returns */
	void *data;
	size_t bytes;
	u64 token;
	int status, completion_rc;
	bool active, copied, finished;
};

static LIST_HEAD(jobs);
static DEFINE_MUTEX(jobs_lock);
static DEFINE_MUTEX(control_lock);
static struct dentry *test_dir;
static bool registered;
static unsigned int delay_ms = 100, fail_next, job_count;
static size_t snapshot_bytes;
static u64 selected_token, submit_count, cancel_count, complete_count;
static u64 complete_success, complete_errors, manual_count, last_token;
static int last_rc, last_status;

static struct test_job *find_job(u64 token)
{
	struct test_job *job;

	lockdep_assert_held(&jobs_lock);
	list_for_each_entry(job, &jobs, list)
		if (job->token == token)
			return job;
	return NULL;
}

static void job_put(struct test_job *job)
{
	if (refcount_dec_and_test(&job->refs)) {
		kvfree(job->data);
		kfree(job);
	}
}

static void copy_and_complete(struct work_struct *work)
{
	struct test_job *job = container_of(to_delayed_work(work),
						struct test_job, work);
	size_t offset;
	int ret;

	/* No jobs/control mutex is held while accessing the source or core.
	 * cancel_delayed_work_sync() is the lifetime barrier for source. */
	for (offset = 0; offset < job->bytes; offset += PAGE_SIZE) {
		void *src = kmap_local_page(folio_page(job->source,
						     offset >> PAGE_SHIFT));

		memcpy((char *)job->data + offset, src, PAGE_SIZE);
		kunmap_local(src);
		cond_resched();
	}
	mutex_lock(&jobs_lock);
	job->copied = true;
	mutex_unlock(&jobs_lock);
	ret = chameleon_shadow_save_complete(job->token, job->status, 0);
	mutex_lock(&jobs_lock);
	job->finished = true;
	job->completion_rc = ret;
	complete_count++;
	if (!ret && !job->status)
		complete_success++;
	else
		complete_errors++;
	last_token = job->token;
	last_rc = ret;
	last_status = job->status;
	mutex_unlock(&jobs_lock);
}

static int test_submit(u64 token, struct folio *folio)
{
	struct test_job *job;
	unsigned int delay;
	size_t bytes = folio_size(folio);
	int ret = 0;

	if (bytes > TEST_MAX_BYTES)
		return -E2BIG;
	job = kzalloc(sizeof(*job), GFP_KERNEL);
	if (!job)
		return -ENOMEM;
	job->data = kvmalloc(bytes, GFP_KERNEL);
	if (!job->data) {
		kfree(job);
		return -ENOMEM;
	}
	refcount_set(&job->refs, 1); /* jobs list */
	INIT_DELAYED_WORK(&job->work, copy_and_complete);
	job->token = token;
	job->source = folio;
	job->bytes = bytes;
	job->active = true;
	mutex_lock(&jobs_lock);
	if (find_job(token))
		ret = -EALREADY;
	else if (job_count >= TEST_MAX_JOBS ||
		 snapshot_bytes + bytes > TEST_MAX_BYTES)
		ret = -ENOSPC;
	if (ret) {
		mutex_unlock(&jobs_lock);
		job_put(job);
		return ret;
	}
	delay = delay_ms;
	job->status = fail_next ? -EIO : 0;
	fail_next = 0;
	list_add_tail(&job->list, &jobs);
	job_count++;
	snapshot_bytes += bytes;
	submit_count++;
	/* List ownership remains until cancel has drained the work. */
	schedule_delayed_work(&job->work, msecs_to_jiffies(delay));
	mutex_unlock(&jobs_lock);
	return 0;
}

static void test_cancel(u64 token)
{
	struct test_job *job;

	mutex_lock(&jobs_lock);
	job = find_job(token);
	if (job)
		refcount_inc(&job->refs);
	mutex_unlock(&jobs_lock);
	if (WARN_ON_ONCE(!job))
		return;
	/* Never hold jobs_lock here: a running worker needs it to finish. */
	cancel_delayed_work_sync(&job->work);
	mutex_lock(&jobs_lock);
	job->source = NULL;
	job->active = false;
	cancel_count++;
	mutex_unlock(&jobs_lock);
	job_put(job);
}

static const struct chameleon_shadow_backend_ops test_ops = {
	.submit = test_submit,
	.cancel = test_cancel,
};

static int reset_snapshots(void)
{
	LIST_HEAD(discard);
	struct test_job *job, *next;

	mutex_lock(&jobs_lock);
	list_for_each_entry(job, &jobs, list) {
		if (job->active) {
			mutex_unlock(&jobs_lock);
			return -EBUSY;
		}
	}
	list_splice_init(&jobs, &discard);
	job_count = 0;
	snapshot_bytes = 0;
	selected_token = 0;
	mutex_unlock(&jobs_lock);
	list_for_each_entry_safe(job, next, &discard, list) {
		list_del(&job->list);
		job_put(job);
	}
	return 0;
}

static ssize_t control_write(struct file *file, const char __user *buffer,
			     size_t count, loff_t *position)
{
	char command[128], op[24], extra;
	unsigned long long token, offset;
	unsigned int number;
	int status, fields, ret = -EINVAL;

	if (!count || count >= sizeof(command))
		return -EINVAL;
	if (copy_from_user(command, buffer, count))
		return -EFAULT;
	command[count] = 0;
	mutex_lock(&control_lock);
	fields = sscanf(command, "%23s %c", op, &extra);
	if (fields == 1 && !strcmp(op, "register")) {
		if (registered) {
			ret = -EALREADY;
		} else if (!try_module_get(THIS_MODULE)) {
			ret = -ENODEV;
		} else {
			ret = chameleon_shadow_backend_register(&test_ops);
			if (ret)
				module_put(THIS_MODULE);
			else
				WRITE_ONCE(registered, true);
		}
	} else if (fields == 1 && !strcmp(op, "unregister")) {
		if (!registered) {
			ret = -ENOENT;
		} else {
			ret = chameleon_shadow_backend_unregister(&test_ops);
			if (!ret) {
				WRITE_ONCE(registered, false);
				module_put(THIS_MODULE);
			}
		}
	} else if (fields == 1 && !strcmp(op, "reset")) {
		ret = reset_snapshots();
	} else if (sscanf(command, "delay %u %c", &number, &extra) == 1 &&
		   number <= 60000) {
		mutex_lock(&jobs_lock);
		delay_ms = number;
		mutex_unlock(&jobs_lock);
		ret = 0;
	} else if (sscanf(command, "error %u %c", &number, &extra) == 1 &&
		   number <= 1) {
		mutex_lock(&jobs_lock);
		fail_next = number;
		mutex_unlock(&jobs_lock);
		ret = 0;
	} else if (sscanf(command, "select %llu %c", &token, &extra) == 1) {
		mutex_lock(&jobs_lock);
		if (find_job(token)) {
			selected_token = token;
			ret = 0;
		} else {
			ret = -ENOENT;
		}
		mutex_unlock(&jobs_lock);
	} else if (sscanf(command, "complete %llu %d %llu %c", &token,
			   &status, &offset, &extra) == 3) {
		ret = chameleon_shadow_save_complete(token, status, offset);
		mutex_lock(&jobs_lock);
		manual_count++;
		last_token = token;
		last_rc = ret;
		last_status = status;
		mutex_unlock(&jobs_lock);
	}
	mutex_unlock(&control_lock);
	return ret ? ret : count;
}

static const struct file_operations control_fops = {
	.owner = THIS_MODULE,
	.write = control_write,
	.llseek = noop_llseek,
};

static int stats_show(struct seq_file *seq, void *unused)
{
	struct test_job *job;
	unsigned int active = 0;

	mutex_lock(&jobs_lock);
	list_for_each_entry(job, &jobs, list)
		active += job->active;
	seq_printf(seq, "registered %u\ndelay_ms %u\nerror_next %u\n",
		   READ_ONCE(registered), delay_ms, fail_next);
	seq_printf(seq, "jobs %u\nactive_jobs %u\nsnapshot_bytes %zu\n",
		   job_count, active, snapshot_bytes);
	seq_printf(seq, "submit_count %llu\ncancel_count %llu\ncomplete_count %llu\n",
		   submit_count, cancel_count, complete_count);
	seq_printf(seq, "complete_success %llu\ncomplete_errors %llu\nmanual_count %llu\n",
		   complete_success, complete_errors, manual_count);
	seq_printf(seq, "last_token %llu\nlast_rc %d\nlast_status %d\n",
		   last_token, last_rc, last_status);
	list_for_each_entry(job, &jobs, list)
		seq_printf(seq, "job token=%llu bytes=%zu active=%u copied=%u finished=%u status=%d rc=%d\n",
			   job->token, job->bytes, job->active, job->copied,
			   job->finished, job->status, job->completion_rc);
	mutex_unlock(&jobs_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(stats);

static int snapshot_open(struct inode *inode, struct file *file)
{
	struct test_job *job;
	int ret = 0;

	mutex_lock(&jobs_lock);
	job = find_job(selected_token);
	if (!job)
		ret = -ENOENT;
	else if (!job->copied)
		ret = -EAGAIN;
	else {
		refcount_inc(&job->refs);
		file->private_data = job;
	}
	mutex_unlock(&jobs_lock);
	return ret;
}

static ssize_t snapshot_read(struct file *file, char __user *buffer,
			     size_t count, loff_t *position)
{
	struct test_job *job = file->private_data;

	/* No mutex held across copy_to_user: its target may itself fault. */
	return simple_read_from_buffer(buffer, count, position, job->data, job->bytes);
}

static int snapshot_release(struct inode *inode, struct file *file)
{
	job_put(file->private_data);
	return 0;
}

static const struct file_operations snapshot_fops = {
	.owner = THIS_MODULE,
	.open = snapshot_open,
	.read = snapshot_read,
	.release = snapshot_release,
	.llseek = default_llseek,
};

static int __init test_init(void)
{
	test_dir = debugfs_create_dir("chameleon_shadow_backend", NULL);
	if (IS_ERR(test_dir))
		return PTR_ERR(test_dir);
	debugfs_create_file("control", 0600, test_dir, NULL, &control_fops);
	debugfs_create_file("stats", 0400, test_dir, NULL, &stats_fops);
	debugfs_create_file("snapshot", 0400, test_dir, NULL, &snapshot_fops);
	return 0;
}

static void __exit test_exit(void)
{
	/* A successful explicit unregister releases the registration self-ref. */
	WARN_ON_ONCE(registered);
	debugfs_remove_recursive(test_dir);
	WARN_ON_ONCE(reset_snapshots());
}

module_init(test_init);
module_exit(test_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Test-only asynchronous Chameleon Shadow snapshot backend");
