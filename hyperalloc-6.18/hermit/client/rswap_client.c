// SPDX-License-Identifier: GPL-2.0-only
/* Hermit remoteswap module front end for Chameleon on Linux 6.18.
 * Original Hermit authors: Chenxi Wang and Yifan Qiao. This replaces the
 * removed frontswap hook with the explicit Chameleon folio backend contract.
 */
#include <linux/bitmap.h>
#include <linux/chameleon_shadow.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/highmem.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>
#include <linux/workqueue.h>
#include <linux/xarray.h>
#include "rswap_transport.h"

static char *backend = "dram";
static char *sip = "192.0.2.1";
static unsigned int sport = 9400;
static unsigned int pool_mb = 128;
module_param(backend, charp, 0444);
MODULE_PARM_DESC(backend, "dram (local debug pool) or rdma (Hermit memory server)");
module_param(sip, charp, 0444);
module_param(sport, uint, 0444);
module_param(pool_mb, uint, 0444);
MODULE_PARM_DESC(pool_mb, "Maximum pool size in MiB (1..65536)");

struct saved_object {
	u64 token;
	unsigned long slot, pages;
	struct folio *source;
	struct work_struct work;
	atomic_t readers;
	wait_queue_head_t readers_wait;
	bool canceled, saved;
};
static DEFINE_XARRAY(objects);
static DEFINE_MUTEX(objects_lock);
static DEFINE_MUTEX(registration_lock);
static struct workqueue_struct *save_wq;
static struct dentry *debug_dir;
static unsigned long *slots, capacity_pages;
static void *dram;
static bool registered;
#ifdef RSWAP_HAS_RDMA
static bool use_rdma;
#endif
static unsigned int delay_ms;
static atomic_t inject_store = ATOMIC_INIT(0), inject_load = ATOMIC_INIT(0);
static atomic64_t live_slots, allocated_pages, peak_pages, inflight;
static atomic64_t store_success, store_failures, load_success, load_failures;
static atomic64_t canceled, bytes_written, bytes_read, completions, callback_errors;

static int dram_transfer(unsigned long slot, struct folio *folio, bool write)
{
	unsigned long pages = folio_nr_pages(folio), i;
	if (pages > capacity_pages || slot > capacity_pages - pages)
		return -ERANGE;
	for (i = 0; i < pages; i++) {
		void *local = kmap_local_page(folio_page(folio, i));
		void *remote = (char *)dram + ((slot + i) << PAGE_SHIFT);
		if (write)
			copy_page(remote, local);
		else
			copy_page(local, remote);
		kunmap_local(local);
		cond_resched();
	}
	return 0;
}

static int store_folio(struct saved_object *object)
{
	if (atomic_dec_if_positive(&inject_store) >= 0)
		return -EIO;
#ifdef RSWAP_HAS_RDMA
	if (use_rdma)
		return rswap_rdma_store(object->slot, object->source);
#endif
	return dram_transfer(object->slot, object->source, true);
}

static int load_folio(struct saved_object *object, struct folio *folio)
{
	if (atomic_dec_if_positive(&inject_load) >= 0)
		return -EIO;
#ifdef RSWAP_HAS_RDMA
	if (use_rdma)
		return rswap_rdma_load(object->slot, folio);
#endif
	return dram_transfer(object->slot, folio, false);
}

static void complete_save(struct saved_object *object, int error)
{
	int ret;
	/* Core checks exact source references before FINALIZE. Our borrowed
	 * I/O reference must be dropped before successful publication. */
	if (object->source) {
		folio_put(object->source);
		object->source = NULL;
	}
	if (error)
		atomic64_inc(&store_failures);
	else {
		WRITE_ONCE(object->saved, true);
		atomic64_inc(&store_success);
		atomic64_add((u64)object->pages << PAGE_SHIFT, &bytes_written);
	}
	atomic64_dec(&inflight);
	ret = chameleon_shadow_save_complete(object->token, error,
		(u64)object->slot << PAGE_SHIFT);
	atomic64_inc(&completions);
	if (ret)
		atomic64_inc(&callback_errors);
}

static void save_work(struct work_struct *work)
{
	struct saved_object *object = container_of(work, struct saved_object, work);
	unsigned int delay = READ_ONCE(delay_ms);
	int ret;
	if (delay)
		msleep(delay);
	if (READ_ONCE(object->canceled))
		ret = -ECANCELED;
	else
		ret = store_folio(object);
	if (!ret && READ_ONCE(object->canceled))
		ret = -ECANCELED;
	complete_save(object, ret);
}

static int submit(u64 token, struct folio *folio)
{
	struct saved_object *object;
	unsigned long slot, pages = folio_nr_pages(folio);
	int ret = 0;
	if (!token || token > ULONG_MAX || folio_order(folio) > 9)
		return -EINVAL;
	object = kzalloc(sizeof(*object), GFP_KERNEL);
	if (!object)
		return -ENOMEM;
	object->token = token;
	object->pages = pages;
	object->source = folio;
	INIT_WORK(&object->work, save_work);
	init_waitqueue_head(&object->readers_wait);
	atomic_set(&object->readers, 0);
	mutex_lock(&objects_lock);
	if (xa_load(&objects, token)) {
		ret = -EALREADY;
		goto unlock;
	}
	slot = bitmap_find_next_zero_area(slots, capacity_pages, 0, pages, pages - 1);
	if (slot >= capacity_pages) {
		ret = -ENOSPC;
		goto unlock;
	}
	object->slot = slot;
	ret = xa_err(xa_store(&objects, token, object, GFP_KERNEL));
	if (ret)
		goto unlock;
	bitmap_set(slots, slot, pages);
	atomic64_inc(&live_slots);
	atomic64_add(pages, &allocated_pages);
	if (atomic64_read(&allocated_pages) > atomic64_read(&peak_pages))
		atomic64_set(&peak_pages, atomic64_read(&allocated_pages));
	folio_get(folio);
	atomic64_inc(&inflight);
	/* Queue while map-locked: cancel cannot race an unqueued source. */
	queue_work(save_wq, &object->work);
unlock:
	mutex_unlock(&objects_lock);
	if (ret)
		kfree(object);
	return ret;
}

static int load(u64 token, u64 offset, struct folio *folio)
{
	struct saved_object *object;
	int ret;
	mutex_lock(&objects_lock);
	object = xa_load(&objects, token);
	if (!object || object->canceled || !READ_ONCE(object->saved) ||
	    offset != (u64)object->slot << PAGE_SHIFT ||
	    folio_nr_pages(folio) != object->pages) {
		mutex_unlock(&objects_lock);
		return -ESTALE;
	}
	atomic_inc(&object->readers);
	atomic64_inc(&inflight);
	mutex_unlock(&objects_lock);
	ret = load_folio(object, folio);
	if (ret)
		atomic64_inc(&load_failures);
	else {
		atomic64_inc(&load_success);
		atomic64_add((u64)object->pages << PAGE_SHIFT, &bytes_read);
	}
	atomic64_dec(&inflight);
	/* Pair the final wake with cancel's map lock before freeing object. */
	mutex_lock(&objects_lock);
	if (atomic_dec_and_test(&object->readers))
		wake_up_all(&object->readers_wait);
	mutex_unlock(&objects_lock);
	return ret;
}

static void cancel(u64 token)
{
	struct saved_object *object;
	mutex_lock(&objects_lock);
	object = xa_erase(&objects, token);
	if (object)
		WRITE_ONCE(object->canceled, true);
	mutex_unlock(&objects_lock);
	if (!object)
		return;
	/* Called by the core's separate cleanup path, never by this worker.
	 * No source, DMA or load may survive cancel's return. */
	if (cancel_work_sync(&object->work))
		complete_save(object, -ECANCELED);
	wait_event(object->readers_wait, !atomic_read(&object->readers));
	mutex_lock(&objects_lock);
	bitmap_clear(slots, object->slot, object->pages);
	atomic64_dec(&live_slots);
	atomic64_sub(object->pages, &allocated_pages);
	mutex_unlock(&objects_lock);
	atomic64_inc(&canceled);
	kfree(object);
}

static const struct chameleon_shadow_backend_ops operations = {
	.owner = THIS_MODULE,
	.submit = submit,
	.cancel = cancel,
	.load = load,
};

static int stats_show(struct seq_file *seq, void *unused)
{
	(void)unused;
	seq_printf(seq, "backend %s\nregistered %u\ncapacity_pages %lu\ndelay_ms %u\n",
		backend, READ_ONCE(registered), capacity_pages, READ_ONCE(delay_ms));
#define STAT(name) seq_printf(seq, #name " %lld\n", atomic64_read(&name))
	STAT(live_slots); STAT(allocated_pages); STAT(peak_pages); STAT(inflight);
	STAT(store_success); STAT(store_failures); STAT(load_success); STAT(load_failures);
	STAT(canceled); STAT(bytes_written); STAT(bytes_read); STAT(completions);
	STAT(callback_errors);
#undef STAT
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(stats);

static ssize_t control_write(struct file *file, const char __user *buffer,
		size_t size, loff_t *position)
{
	char command[80], extra;
	unsigned int value;
	int ret = 0;
	(void)file;
	(void)position;
	if (!size || size >= sizeof(command))
		return -EINVAL;
	if (copy_from_user(command, buffer, size))
		return -EFAULT;
	command[size] = '\0';
	strim(command);
	mutex_lock(&registration_lock);
	if (!strcmp(command, "register")) {
		if (registered)
			ret = -EALREADY;
		else {
			ret = chameleon_shadow_backend_register(&operations);
			if (!ret)
				WRITE_ONCE(registered, true);
		}
	} else if (!strcmp(command, "unregister")) {
		if (!registered)
			ret = -EINVAL;
		else {
			ret = chameleon_shadow_backend_unregister(&operations);
			if (!ret)
				WRITE_ONCE(registered, false);
		}
#ifdef CONFIG_CHAMELEON_TEST
	} else if (sscanf(command, "delay %u %c", &value, &extra) == 1 && value <= 5000) {
		WRITE_ONCE(delay_ms, value);
	} else if (sscanf(command, "fail_store %u %c", &value, &extra) == 1 && value <= 100000) {
		atomic_set(&inject_store, value);
	} else if (sscanf(command, "fail_load %u %c", &value, &extra) == 1 && value <= 100000) {
		atomic_set(&inject_load, value);
#endif
	} else {
		ret = -EINVAL;
	}
	mutex_unlock(&registration_lock);
	return ret ? ret : size;
}
static const struct file_operations control_fops = {
	.owner = THIS_MODULE,
	.write = control_write,
	.llseek = noop_llseek,
};

static int __init rswap_init(void)
{
	int ret;
#ifdef RSWAP_HAS_RDMA
	unsigned long remote_pages = 0;
#endif
	if (!pool_mb || pool_mb > 65536)
		return -EINVAL;
	capacity_pages = (unsigned long)pool_mb << (20 - PAGE_SHIFT);
	if (!strcmp(backend, "rdma")) {
#ifdef RSWAP_HAS_RDMA
		use_rdma = true;
		ret = rswap_rdma_init(sip, sport, &remote_pages);
		if (ret)
			return ret;
		capacity_pages = min(capacity_pages, remote_pages);
#else
		pr_err("hermit: RDMA needs CONFIG_INFINIBAND in the target kernel\n");
		return -EOPNOTSUPP;
#endif
	} else if (!strcmp(backend, "dram")) {
		dram = vzalloc((size_t)capacity_pages << PAGE_SHIFT);
		if (!dram)
			return -ENOMEM;
	} else {
		return -EINVAL;
	}
	slots = bitmap_zalloc(capacity_pages, GFP_KERNEL);
	save_wq = alloc_workqueue("hermit-save", WQ_UNBOUND | WQ_MEM_RECLAIM, 4);
	if (!slots || !save_wq) {
		ret = -ENOMEM;
		goto fail;
	}
	debug_dir = debugfs_create_dir("hermit", NULL);
	if (IS_ERR(debug_dir)) {
		ret = PTR_ERR(debug_dir);
		debug_dir = NULL;
		goto fail;
	}
	debugfs_create_file("stats", 0400, debug_dir, NULL, &stats_fops);
	debugfs_create_file("control", 0200, debug_dir, NULL, &control_fops);
	ret = chameleon_shadow_backend_register(&operations);
	if (ret)
		goto fail;
	registered = true;
	pr_info("hermit: rswap-client registered backend=%s capacity_pages=%lu\n",
		backend, capacity_pages);
	return 0;
fail:
	debugfs_remove_recursive(debug_dir);
	if (save_wq)
		destroy_workqueue(save_wq);
	bitmap_free(slots);
	vfree(dram);
#ifdef RSWAP_HAS_RDMA
	if (use_rdma)
		rswap_rdma_exit();
#endif
	return ret;
}

static void __exit rswap_exit(void)
{
	/* Per-object module pins make an active backend non-unloadable. */
	if (registered)
		WARN_ON(chameleon_shadow_backend_unregister(&operations));
	debugfs_remove_recursive(debug_dir);
	destroy_workqueue(save_wq);
	WARN_ON(!xa_empty(&objects) || atomic64_read(&inflight));
	xa_destroy(&objects);
	bitmap_free(slots);
	vfree(dram);
#ifdef RSWAP_HAS_RDMA
	if (use_rdma)
		rswap_rdma_exit();
#endif
	pr_info("hermit: rswap-client unloaded, all saved slots released\n");
}
module_init(rswap_init);
module_exit(rswap_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("Chameleon; based on Hermit remoteswap by Chenxi Wang and Yifan Qiao");
MODULE_DESCRIPTION("Hermit DRAM/RDMA folio data backend for Chameleon Linux 6.18");
