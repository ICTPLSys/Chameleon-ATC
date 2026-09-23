// SPDX-License-Identifier: GPL-2.0-only
/*
 * Controlled native PSI accounting fixture, not a memory-exhaustion benchmark.
 * Each CPU-bound worker enters the kernel's real memory-stall section, does
 * bounded memory work and sleeps while stalled. The scheduler updates its
 * native per-CPU PSI state. No policy counter, pressure value, or decision is
 * injected. control: start | stop. stop/rmmod join all workers synchronously.
 */
#include <linux/atomic.h>
#include <linux/cpu.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/kthread.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/psi.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

struct load_worker {
	struct task_struct *task;
	unsigned long memory;
};

static DEFINE_MUTEX(control_lock);
static struct load_worker *workers;
static struct dentry *directory;
static unsigned int running;
static atomic64_t sections = ATOMIC64_INIT(0);

static int produce_stalls(void *arg)
{
	struct load_worker *worker = arg;
	u64 value = 0;

	while (!kthread_should_stop()) {
		unsigned long flags;

		psi_memstall_enter(&flags);
		WRITE_ONCE(*(u64 *)worker->memory, ++value);
		/* A bounded wait keeps the native MEMSTALL state observable across
		 * several 1 ms policy samples without consuming an entire CPU. */
		usleep_range(2000, 3000);
		psi_memstall_leave(&flags);
		atomic64_inc(&sections);
		cond_resched();
	}
	return 0;
}

static void stop_load(void)
{
	unsigned int cpu;

	lockdep_assert_held(&control_lock);
	for_each_possible_cpu(cpu) {
		struct load_worker *worker = &workers[cpu];

		if (worker->task) {
			kthread_stop(worker->task);
			worker->task = NULL;
		}
		if (worker->memory) {
			free_page(worker->memory);
			worker->memory = 0;
		}
	}
	running = 0;
}

static int start_load(void)
{
	unsigned int cpu;
	int ret = 0;

	lockdep_assert_held(&control_lock);
	if (running)
		return -EALREADY;
	cpus_read_lock();
	for_each_online_cpu(cpu) {
		struct load_worker *worker = &workers[cpu];

		worker->memory = get_zeroed_page(GFP_KERNEL);
		if (!worker->memory) {
			ret = -ENOMEM;
			break;
		}
		worker->task = kthread_run_on_cpu(produce_stalls, worker, cpu,
						"ch_psi_test/%u");
		if (IS_ERR(worker->task)) {
			ret = PTR_ERR(worker->task);
			worker->task = NULL;
			break;
		}
		running++;
	}
	cpus_read_unlock();
	if (ret)
		stop_load();
	return ret;
}

static ssize_t control_write(struct file *file, const char __user *buf,
			     size_t count, loff_t *offset)
{
	char text[32];
	int ret;

	if (!count || count >= sizeof(text))
		return -EINVAL;
	if (copy_from_user(text, buf, count))
		return -EFAULT;
	text[count] = '\0';
	mutex_lock(&control_lock);
	if (!strcmp(strim(text), "start"))
		ret = start_load();
	else if (!strcmp(strim(text), "stop")) {
		stop_load();
		ret = 0;
	} else {
		ret = -EINVAL;
	}
	mutex_unlock(&control_lock);
	return ret ? ret : count;
}

static const struct file_operations control_fops = {
	.owner = THIS_MODULE,
	.write = control_write,
};

static int stats_show(struct seq_file *seq, void *unused)
{
	mutex_lock(&control_lock);
	seq_printf(seq, "workers %u\nsections %lld\nsource native_psi_memstall\n",
		   running, atomic64_read(&sections));
	mutex_unlock(&control_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(stats);

static int __init load_init(void)
{
	if (!IS_ENABLED(CONFIG_CHAMELEON_TEST) || !IS_ENABLED(CONFIG_PSI))
		return -EOPNOTSUPP;
	workers = kcalloc(nr_cpu_ids, sizeof(*workers), GFP_KERNEL);
	if (!workers)
		return -ENOMEM;
	directory = debugfs_create_dir("chameleon_psi_load", NULL);
	if (IS_ERR(directory)) {
		kfree(workers);
		return PTR_ERR(directory);
	}
	debugfs_create_file("control", 0200, directory, NULL, &control_fops);
	debugfs_create_file("stats", 0400, directory, NULL, &stats_fops);
	return 0;
}

static void __exit load_exit(void)
{
	debugfs_remove_recursive(directory);
	mutex_lock(&control_lock);
	stop_load();
	mutex_unlock(&control_lock);
	kfree(workers);
}

module_init(load_init);
module_exit(load_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Controlled native PSI stall producer for Chameleon tests");
