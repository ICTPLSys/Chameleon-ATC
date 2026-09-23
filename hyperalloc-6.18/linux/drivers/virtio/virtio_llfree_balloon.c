#include "linux/virtio_llfree_balloon.h"

#include <llfree_alloc.h>
#include <linux/virtio.h>
#include <linux/virtio_config.h>
#include <linux/virtio_ids.h>
#include <linux/workqueue.h>
#include <linux/module.h>
#include <linux/vmscan.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/completion.h>
#include <linux/mutex.h>
#include <linux/chameleon_transport.h>
#include <linux/chameleon_shadow.h>

int ll_request_install(struct zone *zone, u64 frame, unsigned int order, size_t core);

/*-----------------------------------------------------------------------------------------------
| Shared Data
-------------------------------------------------------------------------------------------------*/

enum ll_zone_type {
	LLFREE_ZONE_DMA32,
	LLFREE_ZONE_NORMAL,
	LLFREE_ZONE_MOVABLE,
	LLFREE_ZONE_LEN,
	LLFREE_ZONE_NONE = -1,
};

typedef enum ll_notification {
	PAGECACHE_DROPPED,
} ll_notification_t;

typedef struct ll_install_info {
	uint32_t node;
	uint32_t zone;
	uint64_t frame;
	__le32 order;
	__le32 status;
} ll_install_info_t;

/*-----------------------------------------------------------------------------------------------
| Private Data
-------------------------------------------------------------------------------------------------*/

typedef struct ll_zone_info {
	llfree_meta_t llfree_meta;
	uint32_t start_pfn;
	uint32_t pages;
	uint32_t type;
	uint32_t node;
	_Atomic(int64_t) *free_pages;
	_Atomic(int64_t) *file_pages;
} ll_zone_info_t;

#define LL_CH_MSG_BYTES (sizeof(struct ll_chameleon_header) + \
	LL_CHAMELEON_MAX_RANGES * sizeof(struct ll_chameleon_range))

typedef struct ll_balloon {
	struct virtio_device *vdev;
	struct virtqueue *info_vq;
	struct virtqueue *notify_vq;
	struct virtqueue **install_vqs;
	struct virtqueue *ch_ctrl_vq, *ch_event_vq;
	spinlock_t ch_vq_lock;
	struct completion ch_done;
	struct work_struct ch_event_work;
	void *ch_out, *ch_in, *ch_event;
	u32 ch_reply_len, ch_event_len;
	u64 ch_session, ch_next_request;
	bool ch_broken;

	struct work_struct shrink_pagecache_work;
	ll_zone_info_t info_buf;
	ll_notification_t notify_buf;
	ll_install_info_t *install_buf;
	u32 num_cpus;
	enum ll_zone_type map_zone_type[__MAX_NR_ZONES];
} ll_balloon_t;

static ll_balloon_t *balloon;
static DEFINE_MUTEX(ch_transport_lock);
static atomic64_t ch_requests, ch_failures, ch_events, ch_bad_events;

static atomic64_t install_success = ATOMIC64_INIT(0);
static atomic64_t install_failure = ATOMIC64_INIT(0);
static atomic64_t install_order10 = ATOMIC64_INIT(0);

static int ll_protocol_show(struct seq_file *m, void *v)
{
	seq_puts(m, "abi install-result-v1\n");
	seq_printf(m, "install_success %lld\n", atomic64_read(&install_success));
	seq_printf(m, "install_failure %lld\n", atomic64_read(&install_failure));
	seq_printf(m, "install_order10 %lld\n", atomic64_read(&install_order10));
	seq_printf(m, "chameleon_available %u\nchameleon_requests %lld\nchameleon_failures %lld\nchameleon_events %lld\nchameleon_bad_events %lld\n",
		chameleon_transport_available(), atomic64_read(&ch_requests),
		atomic64_read(&ch_failures), atomic64_read(&ch_events),
		atomic64_read(&ch_bad_events));
	return 0;
}

enum ll_balloon_vq {
	LL_BALLOON_VQ_INFO,
	LL_BALLOON_VQ_NOTIFY,
	LL_BALLOON_VQ_COUNT,
};

/*-----------------------------------------------------------------------------------------------
| Helper Functions
-------------------------------------------------------------------------------------------------*/

static void ll_init_zone_types(enum ll_zone_type *types)
{
	for (uint32_t i = 0; i < __MAX_NR_ZONES; i++) {
		types[i] = LLFREE_ZONE_NONE;
	}

#ifdef CONFIG_ZONE_DMA32
	types[ZONE_DMA32] = LLFREE_ZONE_DMA32;
#endif

	types[ZONE_NORMAL] = LLFREE_ZONE_NORMAL;
	types[ZONE_MOVABLE] = LLFREE_ZONE_MOVABLE;
}

static inline int32_t zone_get_type(struct zone *zone)
{
	struct pglist_data *node = zone->zone_pgdat;
	for (uint32_t i = 0; i < MAX_NR_ZONES; i++) {
		if (&node->node_zones[i] == zone) {
			return i;
		}
	}
	return -1;
}

static void llfree_copy_into_buffer(ll_zone_info_t *src, ll_zone_info_t *dest)
{
	llfree_meta_t *src_meta = &src->llfree_meta;

	BUG_ON(src == NULL || dest == NULL);
	*dest = (ll_zone_info_t){
		.llfree_meta = {
			.local = (uint8_t *)virt_to_phys(src_meta->local),
			.trees = (uint8_t *)virt_to_phys(src_meta->trees),
			.lower = (uint8_t *)virt_to_phys(src_meta->lower),
		},
		.start_pfn = src->start_pfn,
		.pages = src->pages,
		.type = src->type,
		.node = src->node,
		.free_pages = (_Atomic(int64_t) *)virt_to_phys(src->free_pages),
		.file_pages = (_Atomic(int64_t) *)virt_to_phys(src->file_pages),
	};
}

/*-----------------------------------------------------------------------------------------------
| Virtqueue Sending Functions
-------------------------------------------------------------------------------------------------*/

/// Send allocator data to host
static void ll_send_info(ll_balloon_t *vb)
{
	ll_zone_info_t zone_info;
	// only UMA is currently supported
	struct pglist_data *pgdat = first_online_pgdat();

	for (uint32_t i = 0; i < MAX_NR_ZONES; i++) {
		struct scatterlist sg;
		uint32_t len;
		struct zone *zone = &pgdat->node_zones[i];
		enum ll_zone_type zone_type = vb->map_zone_type[i];

		if (zone_type == LLFREE_ZONE_NONE || !populated_zone(zone))
			continue;

		zone_info.type = zone_type;
		zone_info.start_pfn = zone->zone_start_pfn;
		zone_info.pages = zone->spanned_pages;
		zone_info.node = pgdat->node_id;
		zone_info.llfree_meta = llfree_metadata(zone->llfree);
		zone_info.free_pages =
			(_Atomic(int64_t) *)&zone->vm_stat[NR_FREE_PAGES];
		zone_info.file_pages = (_Atomic(int64_t) *)&zone->zone_pgdat
					       ->vm_stat[NR_FILE_PAGES];

		llfree_copy_into_buffer(&zone_info, &vb->info_buf);

		sg_init_one(&sg, &vb->info_buf, sizeof(vb->info_buf));
		BUG_ON(virtqueue_add_outbuf(vb->info_vq, &sg, 1, vb, GFP_KERNEL));
		virtqueue_kick(vb->info_vq);

		// sync with virtio-device
		while (!virtqueue_get_buf(vb->info_vq, &len) &&
		       !virtqueue_is_broken(vb->info_vq))
			cpu_relax();
	}
}

/// Notify the host
static void ll_notify(ll_balloon_t *vb, ll_notification_t request)
{
	struct scatterlist sg;
	uint32_t len;

	if (!virtio_has_feature(vb->vdev, LL_BALLOON_F_SHRINK_PAGECACHE)) {
		return;
	}

	vb->notify_buf = request;
	sg_init_one(&sg, &vb->notify_buf, sizeof(ll_notification_t));
	BUG_ON(virtqueue_add_outbuf(vb->notify_vq, &sg, 1, vb, GFP_KERNEL));
	virtqueue_kick(vb->notify_vq);

	// sync with virtio-device
	while (!virtqueue_get_buf(vb->notify_vq, &len) &&
	       !virtqueue_is_broken(vb->notify_vq))
		cpu_relax();
}

/* Called with preemption disabled; completion must precede page exposure. */
int ll_request_install(struct zone *zone, u64 frame, unsigned int order, size_t core)
{
	struct scatterlist out, in, *sgs[] = { &out, &in };
	u32 len = 0;
	unsigned long flags;
	ll_balloon_t *vb = READ_ONCE(balloon);
	ll_install_info_t *info;
	struct virtqueue *vq;
	int zone_type = zone_get_type(zone), ret;
	void *done;

	if (!vb || core >= vb->num_cpus || zone_type < 0 ||
	    vb->map_zone_type[zone_type] == LLFREE_ZONE_NONE ||
	    order > LLFREE_MAX_ORDER)
		return -ENODEV;
	if (frame + (1UL << order) > llfree_frames(zone->llfree))
		return -EINVAL;

	local_irq_save(flags);
	info = &vb->install_buf[core];
	vq = vb->install_vqs[core];
	*info = (ll_install_info_t) {
		.node = zone_to_nid(zone),
		.zone = vb->map_zone_type[zone_type],
		.frame = frame,
		.order = cpu_to_le32(order),
		.status = cpu_to_le32(EIO),
	};
	BUILD_BUG_ON(offsetof(ll_install_info_t, status) != 20);
	sg_init_one(&out, info, offsetof(ll_install_info_t, status));
	sg_init_one(&in, &info->status, sizeof(info->status));
	ret = virtqueue_add_sgs(vq, sgs, 1, 1, info, GFP_ATOMIC);
	if (ret)
		goto out;
	virtqueue_kick(vq);
	do {
		done = virtqueue_get_buf(vq, &len);
		if (done)
			break;
		if (virtqueue_is_broken(vq)) {
			ret = -EIO;
			goto out;
		}
		cpu_relax();
	} while (1);
	ret = len == sizeof(info->status) ? -(int)le32_to_cpu(info->status) : -EIO;
out:
	if (ret)
		atomic64_inc(&install_failure);
	else {
		atomic64_inc(&install_success);
		if (order == LLFREE_MAX_ORDER)
			atomic64_inc(&install_order10);
	}
	local_irq_restore(flags);
	return ret;
}
EXPORT_SYMBOL_GPL(ll_request_install);

/// Shrink the page cache
static void shrink_pagecache_func(struct work_struct *work)
{
	uint32_t reclaimed_pages = 0;
	uint32_t shrink_pagecache = 0;
	uint32_t node = 0;

	struct ll_balloon *vb =
		container_of(work, struct ll_balloon, shrink_pagecache_work);

	virtio_cread_le(vb->vdev, struct ll_balloon_config,
			shrink_pagecache, &shrink_pagecache);

	for_each_node(node) {
		reclaimed_pages +=
			shrink_pagecache_for_reclaim(node, shrink_pagecache - min(shrink_pagecache, reclaimed_pages));
	}

	shrink_pagecache -= min(shrink_pagecache, reclaimed_pages);
	virtio_cwrite_le(vb->vdev, struct ll_balloon_config,
			 shrink_pagecache, &shrink_pagecache);

	// tell virtio-device to retry reclamation
	ll_notify(vb, PAGECACHE_DROPPED);
}

/// Executed if the config changes
static void ll_config_changed(struct virtio_device *vdev)
{
	ll_balloon_t *vb;
	uint32_t shrink_pagecache;

	if (!virtio_has_feature(vdev, LL_BALLOON_F_SHRINK_PAGECACHE)) {
		return;
	}

	vb = (ll_balloon_t *)vdev->priv;

	// dispatch
	virtio_cread_le(vdev, struct ll_balloon_config,
			shrink_pagecache,
			&shrink_pagecache);

	if (shrink_pagecache > 0) {
		queue_work(system_freezable_wq, &vb->shrink_pagecache_work);
	}
}

/* The two C4 queues are optional and never change legacy queue indices. */
static void ch_control_done(struct virtqueue *vq)
{
	ll_balloon_t *vb = vq->vdev->priv;
	unsigned long flags;
	u32 len;
	void *done;

	spin_lock_irqsave(&vb->ch_vq_lock, flags);
	done = virtqueue_get_buf(vq, &len);
	if (done) {
		vb->ch_reply_len = len;
		complete(&vb->ch_done);
	}
	spin_unlock_irqrestore(&vb->ch_vq_lock, flags);
}

static int ch_post_event(ll_balloon_t *vb)
{
	struct scatterlist sg;
	unsigned long flags;
	int ret;

	memset(vb->ch_event, 0, LL_CH_MSG_BYTES);
	sg_init_one(&sg, vb->ch_event, LL_CH_MSG_BYTES);
	spin_lock_irqsave(&vb->ch_vq_lock, flags);
	ret = virtqueue_add_inbuf(vb->ch_event_vq, &sg, 1, vb, GFP_ATOMIC);
	if (!ret)
		virtqueue_kick(vb->ch_event_vq);
	spin_unlock_irqrestore(&vb->ch_vq_lock, flags);
	return ret;
}

static void ch_event_work(struct work_struct *work)
{
	ll_balloon_t *vb = container_of(work, ll_balloon_t, ch_event_work);
	struct ll_chameleon_header *hdr = vb->ch_event;
	unsigned int nr = le32_to_cpu(hdr->nr_ranges), i;
	u16 op = le16_to_cpu(hdr->op);

	if (READ_ONCE(vb->ch_broken))
		return;
	if (vb->ch_event_len < sizeof(*hdr) ||
	    !nr || nr > LL_CHAMELEON_MAX_RANGES ||
	    vb->ch_event_len != sizeof(*hdr) + nr * sizeof(struct ll_chameleon_range) ||
	    le16_to_cpu(hdr->version) != LL_CHAMELEON_VERSION ||
	    le32_to_cpu(hdr->flags) != LL_CH_MSG_EVENT ||
	    le64_to_cpu(hdr->session) != READ_ONCE(vb->ch_session) ||
	    hdr->reserved || le32_to_cpu(hdr->status) > MAX_ERRNO ||
	    !le64_to_cpu(hdr->batch_id) ||
	    (op != LL_CH_OP_FINALIZE_REQUEST && op != LL_CH_OP_COMMIT_RESULT)) {
		atomic64_inc(&ch_bad_events);
	} else {
		const struct ll_chameleon_range *range = (const void *)(hdr + 1);

		for (i = 0; i < nr; i++) {
			if (le32_to_cpu(range[i].status) > MAX_ERRNO ||
			    le32_to_cpu(range[i].state) > LL_CH_STATE_ERROR) {
				atomic64_inc(&ch_bad_events);
				goto repost;
			}
		}
		atomic64_inc(&ch_events);
		chameleon_shadow_host_event(op, le64_to_cpu(hdr->batch_id),
			(const struct ll_chameleon_range *)(hdr + 1), nr);
	}
repost:
	if (!READ_ONCE(vb->ch_broken) && ch_post_event(vb))
		WRITE_ONCE(vb->ch_broken, true);
}

static void ch_event_done(struct virtqueue *vq)
{
	ll_balloon_t *vb = vq->vdev->priv;
	unsigned long flags;
	u32 len;

	spin_lock_irqsave(&vb->ch_vq_lock, flags);
	if (virtqueue_get_buf(vq, &len)) {
		vb->ch_event_len = len;
		/* Repost only after work finishes; host retains events while the
		 * single RX buffer is owned by this worker (bounded backpressure). */
		if (!READ_ONCE(vb->ch_broken))
			schedule_work(&vb->ch_event_work);
	}
	spin_unlock_irqrestore(&vb->ch_vq_lock, flags);
}

bool chameleon_transport_available(void)
{
	ll_balloon_t *vb;
	bool available;

	mutex_lock(&ch_transport_lock);
	vb = READ_ONCE(balloon);
	available = vb && READ_ONCE(vb->ch_session) && !READ_ONCE(vb->ch_broken);
	mutex_unlock(&ch_transport_lock);
	return available;
}
EXPORT_SYMBOL_GPL(chameleon_transport_available);

int chameleon_transport_request(u16 op, u64 batch,
		struct ll_chameleon_range *ranges, unsigned int nr)
{
	struct ll_chameleon_header *out, *in;
	struct ll_chameleon_range *reply;
	struct scatterlist sg_out, sg_in, *sgs[] = { &sg_out, &sg_in };
	ll_balloon_t *vb;
	unsigned long flags;
	unsigned int i;
	size_t bytes;
	u64 id;
	int ret = -EOPNOTSUPP;

	might_sleep();
	if (nr > LL_CHAMELEON_MAX_RANGES || (nr && !ranges))
		return -EINVAL;
	mutex_lock(&ch_transport_lock);
	vb = READ_ONCE(balloon);
	if (!vb || !vb->ch_ctrl_vq || READ_ONCE(vb->ch_broken))
		goto unlock;
	if (op != LL_CH_OP_HELLO && !vb->ch_session)
		goto unlock;
	bytes = sizeof(*out) + nr * sizeof(*ranges);
	out = vb->ch_out;
	in = vb->ch_in;
	memset(out, 0, LL_CH_MSG_BYTES);
	memset(in, 0, LL_CH_MSG_BYTES);
	id = ++vb->ch_next_request;
	out->version = cpu_to_le16(LL_CHAMELEON_VERSION);
	out->op = cpu_to_le16(op);
	out->session = cpu_to_le64(op == LL_CH_OP_HELLO ? 0 : vb->ch_session);
	out->request_id = cpu_to_le64(id);
	out->batch_id = cpu_to_le64(batch);
	out->nr_ranges = cpu_to_le32(nr);
	if (nr)
		memcpy(out + 1, ranges, nr * sizeof(*ranges));
	reinit_completion(&vb->ch_done);
	vb->ch_reply_len = 0;
	sg_init_one(&sg_out, out, bytes);
	sg_init_one(&sg_in, in, bytes);
	spin_lock_irqsave(&vb->ch_vq_lock, flags);
	ret = virtqueue_add_sgs(vb->ch_ctrl_vq, sgs, 1, 1, vb, GFP_ATOMIC);
	if (!ret)
		virtqueue_kick(vb->ch_ctrl_vq);
	spin_unlock_irqrestore(&vb->ch_vq_lock, flags);
	if (ret)
		goto failed;
	atomic64_inc(&ch_requests);
	if (!wait_for_completion_timeout(&vb->ch_done, 10 * HZ)) {
		/* Persistent buffers remain allocated: a late device DMA must
		 * never target freed or reused storage. Do not reuse this queue. */
		WRITE_ONCE(vb->ch_broken, true);
		ret = -ETIMEDOUT;
		goto failed;
	}
	ret = -EPROTO;
	if (vb->ch_reply_len != bytes ||
	    le16_to_cpu(in->version) != LL_CHAMELEON_VERSION ||
	    le16_to_cpu(in->op) != op || le64_to_cpu(in->request_id) != id ||
	    le64_to_cpu(in->batch_id) != batch ||
	    le32_to_cpu(in->flags) != LL_CH_MSG_RESPONSE ||
	    le32_to_cpu(in->nr_ranges) != nr || in->reserved ||
	    le32_to_cpu(in->status) > MAX_ERRNO)
		goto failed;
	if (op == LL_CH_OP_HELLO) {
		if (!le64_to_cpu(in->session))
			goto failed;
		WRITE_ONCE(vb->ch_session, le64_to_cpu(in->session));
	} else if (le64_to_cpu(in->session) != vb->ch_session) {
		goto failed;
	}
	reply = (struct ll_chameleon_range *)(in + 1);
	for (i = 0; i < nr; i++) {
		if (reply[i].token != ranges[i].token || reply[i].gpa != ranges[i].gpa ||
		    reply[i].nr_pages != ranges[i].nr_pages || reply[i].order != ranges[i].order ||
		    reply[i].flags != ranges[i].flags ||
		    le32_to_cpu(reply[i].status) > MAX_ERRNO ||
		    le32_to_cpu(reply[i].state) > LL_CH_STATE_ERROR)
			goto failed;
	}
	if (nr)
		memcpy(ranges, reply, nr * sizeof(*ranges));
	ret = -(int)le32_to_cpu(in->status);
	if (!ret)
		goto unlock;
failed:
	atomic64_inc(&ch_failures);
unlock:
	mutex_unlock(&ch_transport_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(chameleon_transport_request);

bool chameleon_data_available(void)
{
	ll_balloon_t *vb;
	bool available;

	mutex_lock(&ch_transport_lock);
	vb = READ_ONCE(balloon);
	available = vb && vb->ch_session && !vb->ch_broken &&
		virtio_has_feature(vb->vdev, LL_BALLOON_F_CHAMELEON_DATA);
	mutex_unlock(&ch_transport_lock);
	return available;
}
EXPORT_SYMBOL_GPL(chameleon_data_available);

bool chameleon_policy_available(void)
{
	ll_balloon_t *vb;
	bool available;
	mutex_lock(&ch_transport_lock);
	vb = READ_ONCE(balloon);
	available = vb && vb->ch_session && !vb->ch_broken &&
		virtio_has_feature(vb->vdev, LL_BALLOON_F_CHAMELEON_POLICY);
	mutex_unlock(&ch_transport_lock);
	return available;
}
EXPORT_SYMBOL_GPL(chameleon_policy_available);

/* One exchange owns the persistent DMA buffers until its exact response is
 * validated. Timeouts permanently break the transport, as for C4 ranges. */
static int ch_policy_exchange(u16 op, struct ll_chameleon_policy *policy)
{
	struct ll_chameleon_header *out, *in;
	struct ll_chameleon_policy *reply;
	struct scatterlist sg_out, sg_in, *sgs[] = {&sg_out, &sg_in};
	ll_balloon_t *vb;
	unsigned long flags;
	u64 id;
	size_t bytes = sizeof(*out) + sizeof(*policy);
	int ret = -EOPNOTSUPP;

	might_sleep();
	BUILD_BUG_ON(sizeof(*policy) != 80);
	mutex_lock(&ch_transport_lock);
	vb = READ_ONCE(balloon);
	if (!vb || !vb->ch_session || vb->ch_broken ||
	    !virtio_has_feature(vb->vdev, LL_BALLOON_F_CHAMELEON_POLICY))
		goto unlock;
	out = vb->ch_out;
	in = vb->ch_in;
	memset(out, 0, LL_CH_MSG_BYTES);
	memset(in, 0, LL_CH_MSG_BYTES);
	id = ++vb->ch_next_request;
	out->version = cpu_to_le16(LL_CHAMELEON_VERSION);
	out->op = cpu_to_le16(op);
	out->session = cpu_to_le64(vb->ch_session);
	out->request_id = cpu_to_le64(id);
	memcpy(out + 1, policy, sizeof(*policy));
	reinit_completion(&vb->ch_done);
	vb->ch_reply_len = 0;
	sg_init_one(&sg_out, out, bytes);
	sg_init_one(&sg_in, in, bytes);
	spin_lock_irqsave(&vb->ch_vq_lock, flags);
	ret = virtqueue_add_sgs(vb->ch_ctrl_vq, sgs, 1, 1, vb, GFP_ATOMIC);
	if (!ret)
		virtqueue_kick(vb->ch_ctrl_vq);
	spin_unlock_irqrestore(&vb->ch_vq_lock, flags);
	if (ret)
		goto failed;
	atomic64_inc(&ch_requests);
	if (!wait_for_completion_timeout(&vb->ch_done, 10 * HZ)) {
		WRITE_ONCE(vb->ch_broken, true);
		ret = -ETIMEDOUT;
		goto failed;
	}
	ret = -EPROTO;
	if (vb->ch_reply_len != bytes || le16_to_cpu(in->version) != LL_CHAMELEON_VERSION ||
	    le16_to_cpu(in->op) != op || le64_to_cpu(in->request_id) != id ||
	    le64_to_cpu(in->session) != vb->ch_session || in->batch_id || in->nr_ranges ||
	    le32_to_cpu(in->flags) != LL_CH_MSG_RESPONSE || in->reserved ||
	    le32_to_cpu(in->status) > MAX_ERRNO)
		goto malformed;
	reply = (void *)(in + 1);
	if (reply->epoch != policy->epoch || reply->requested_bytes != policy->requested_bytes ||
	    le32_to_cpu(reply->ept_mode) > LL_CH_EPT_IMMEDIATE ||
	    (le32_to_cpu(reply->flags) & ~(LL_CH_POLICY_ALLOW_RECLAIM |
		LL_CH_POLICY_LEASED | LL_CH_POLICY_UNCERTAIN)))
		goto malformed;
	if (op != LL_CH_OP_POLICY_ACQUIRE && reply->lease != policy->lease)
		goto malformed;
	if (!in->status) {
		u64 total = le64_to_cpu(reply->total_bytes);
		u64 hard = le64_to_cpu(reply->hard_reclaimed_bytes);
		u64 retired = le64_to_cpu(reply->retired_bytes);
		u64 completed = le64_to_cpu(reply->completed_bytes);

		if (hard > total || retired > total - hard ||
		    le64_to_cpu(reply->local_bytes) != total - hard - retired ||
		    (op == LL_CH_OP_POLICY_ACQUIRE && !reply->lease))
			goto malformed;
		if ((op == LL_CH_OP_POLICY_RECLAIM_FREE || op == LL_CH_OP_POLICY_RETURN_FREE) &&
		    (completed > le64_to_cpu(policy->requested_bytes) ||
		     !IS_ALIGNED(completed, 1UL << 21)))
			goto malformed;
	}
	memcpy(policy, reply, sizeof(*policy));
	ret = -(int)le32_to_cpu(in->status);
	if (ret)
		goto failed;
	goto unlock;
malformed:
	WRITE_ONCE(vb->ch_broken, true);
failed:
	atomic64_inc(&ch_failures);
unlock:
	mutex_unlock(&ch_transport_lock);
	return ret;
}

static void ch_policy_capacity(const struct ll_chameleon_policy *wire,
		struct chameleon_capacity *capacity)
{
	if (!capacity)
		return;
	*capacity = (struct chameleon_capacity){
		.total_bytes = le64_to_cpu(wire->total_bytes),
		.local_bytes = le64_to_cpu(wire->local_bytes),
		.hard_reclaimed_bytes = le64_to_cpu(wire->hard_reclaimed_bytes),
		.retired_bytes = le64_to_cpu(wire->retired_bytes),
		.soft_reclaimed_bytes = le64_to_cpu(wire->soft_reclaimed_bytes),
		.ept_mode = le32_to_cpu(wire->ept_mode),
		.leased = !!(le32_to_cpu(wire->flags) & LL_CH_POLICY_LEASED),
		.reclaim_allowed = !!(le32_to_cpu(wire->flags) & LL_CH_POLICY_ALLOW_RECLAIM),
		.uncertain = !!(le32_to_cpu(wire->flags) & LL_CH_POLICY_UNCERTAIN),
	};
}

int chameleon_policy_acquire(u64 *lease, struct chameleon_capacity *capacity)
{
	struct ll_chameleon_policy policy = {};
	int ret;
	if (!lease)
		return -EINVAL;
	ret = ch_policy_exchange(LL_CH_OP_POLICY_ACQUIRE, &policy);
	if (!ret) {
		*lease = le64_to_cpu(policy.lease);
		ch_policy_capacity(&policy, capacity);
	}
	return ret;
}
EXPORT_SYMBOL_GPL(chameleon_policy_acquire);

static int ch_policy_command(u16 op, u64 lease, u64 epoch, u64 bytes,
		bool allowed, u64 *completed, struct chameleon_capacity *capacity)
{
	struct ll_chameleon_policy policy = {
		.lease = cpu_to_le64(lease), .epoch = cpu_to_le64(epoch),
		.requested_bytes = cpu_to_le64(bytes),
		.flags = cpu_to_le32(allowed ? LL_CH_POLICY_ALLOW_RECLAIM : 0),
	};
	int ret;
	if (completed)
		*completed = 0;
	ret = ch_policy_exchange(op, &policy);
	if (!ret) {
		if (completed)
			*completed = le64_to_cpu(policy.completed_bytes);
		ch_policy_capacity(&policy, capacity);
	}
	return ret;
}

int chameleon_policy_release(u64 lease, struct chameleon_capacity *capacity)
{
	return ch_policy_command(LL_CH_OP_POLICY_RELEASE, lease, 0, 0, false, NULL, capacity);
}
EXPORT_SYMBOL_GPL(chameleon_policy_release);
int chameleon_policy_snapshot(u64 lease, struct chameleon_capacity *capacity)
{
	return ch_policy_command(LL_CH_OP_POLICY_SNAPSHOT, lease, 0, 0, false, NULL, capacity);
}
EXPORT_SYMBOL_GPL(chameleon_policy_snapshot);
int chameleon_policy_allow_reclaim(u64 lease, bool allowed, struct chameleon_capacity *capacity)
{
	return ch_policy_command(LL_CH_OP_POLICY_GATE, lease, 0, 0, allowed, NULL, capacity);
}
EXPORT_SYMBOL_GPL(chameleon_policy_allow_reclaim);
int chameleon_policy_reclaim_free(u64 lease, u64 epoch, u64 bytes,
		u64 *completed, struct chameleon_capacity *capacity)
{
	return ch_policy_command(LL_CH_OP_POLICY_RECLAIM_FREE, lease, epoch, bytes,
			false, completed, capacity);
}
EXPORT_SYMBOL_GPL(chameleon_policy_reclaim_free);
int chameleon_policy_return_free(u64 lease, u64 epoch, u64 bytes,
		u64 *completed, struct chameleon_capacity *capacity)
{
	return ch_policy_command(LL_CH_OP_POLICY_RETURN_FREE, lease, epoch, bytes,
			false, completed, capacity);
}
EXPORT_SYMBOL_GPL(chameleon_policy_return_free);

static int init_vqs(ll_balloon_t *vb)
{
	unsigned int num_vqs = LL_BALLOON_VQ_COUNT + vb->num_cpus;
	bool ch = virtio_has_feature(vb->vdev, LL_BALLOON_F_CHAMELEON_RANGE);
	struct virtqueue **vqs;
	struct virtqueue_info *info;
	int err, i;

	if (ch)
		num_vqs += LL_CHAMELEON_EXTRA_VQS;
	vqs = kcalloc(num_vqs, sizeof(*vqs), GFP_KERNEL);
	info = kcalloc(num_vqs, sizeof(*info), GFP_KERNEL);
	if (!vqs || !info) {
		err = -ENOMEM;
		goto out;
	}
	info[LL_BALLOON_VQ_INFO].name = "llfree info";
	info[LL_BALLOON_VQ_NOTIFY].name = "llfree notify";
	for (i = 0; i < vb->num_cpus; i++)
		info[LL_BALLOON_VQ_COUNT + i].name = "llfree install";
	if (ch) {
		info[LL_CHAMELEON_CTRL_VQ(vb->num_cpus)].name = "chameleon control";
		info[LL_CHAMELEON_CTRL_VQ(vb->num_cpus)].callback = ch_control_done;
		info[LL_CHAMELEON_EVENT_VQ(vb->num_cpus)].name = "chameleon event";
		info[LL_CHAMELEON_EVENT_VQ(vb->num_cpus)].callback = ch_event_done;
	}
	err = virtio_find_vqs(vb->vdev, num_vqs, vqs, info, NULL);
	if (err)
		goto out;
	vb->info_vq = vqs[LL_BALLOON_VQ_INFO];
	vb->notify_vq = vqs[LL_BALLOON_VQ_NOTIFY];
	for (i = 0; i < vb->num_cpus; i++)
		vb->install_vqs[i] = vqs[LL_BALLOON_VQ_COUNT + i];
	if (ch) {
		vb->ch_ctrl_vq = vqs[LL_CHAMELEON_CTRL_VQ(vb->num_cpus)];
		vb->ch_event_vq = vqs[LL_CHAMELEON_EVENT_VQ(vb->num_cpus)];
	}
out:
	kfree(info);
	kfree(vqs);
	return err;
}

static int ll_probe(struct virtio_device *vdev)
{
	ll_balloon_t *ll_b;
	int err;
	uint32_t num_cores;

	if (!virtio_has_feature(vdev, LL_BALLOON_F_INSTALL_RESULT))
		return -ENODEV;
	if (num_online_nodes() != 1)
		return -EOPNOTSUPP;
	if (!vdev->config->get)
		return -ENODEV;

	ll_b = kzalloc(sizeof(*ll_b), GFP_KERNEL);
	if (!ll_b)
		return -ENOMEM;
	vdev->priv = ll_b;

	INIT_WORK(&ll_b->shrink_pagecache_work, shrink_pagecache_func);
	INIT_WORK(&ll_b->ch_event_work, ch_event_work);
	init_completion(&ll_b->ch_done);
	spin_lock_init(&ll_b->ch_vq_lock);
	if (virtio_has_feature(vdev, LL_BALLOON_F_CHAMELEON_RANGE)) {
		ll_b->ch_out = kzalloc(LL_CH_MSG_BYTES, GFP_KERNEL);
		ll_b->ch_in = kzalloc(LL_CH_MSG_BYTES, GFP_KERNEL);
		ll_b->ch_event = kzalloc(LL_CH_MSG_BYTES, GFP_KERNEL);
		if (!ll_b->ch_out || !ll_b->ch_in || !ll_b->ch_event) {
			err = -ENOMEM;
			goto free_ch;
		}
	}

	ll_init_zone_types((enum ll_zone_type *)&ll_b->map_zone_type);

	ll_b->vdev = vdev;

	num_cores = num_online_cpus();
	ll_b->num_cpus = num_cores;
	ll_b->install_buf =
		kzalloc(sizeof(ll_install_info_t) * num_cores, GFP_KERNEL);
	ll_b->install_vqs =
		kzalloc(sizeof(struct virtqueue *) * num_cores, GFP_KERNEL);
	if (!ll_b->install_buf || !ll_b->install_vqs) {
		kfree(ll_b->install_buf);
		kfree(ll_b->install_vqs);
		err = -ENOMEM;
		goto free_ch;
	}

	err = init_vqs(ll_b);
	if (err) {
		kfree(ll_b->install_buf);
		kfree(ll_b->install_vqs);
		goto free_ch;
	}

	balloon = ll_b;

	virtio_device_ready(vdev);

	// directly send our zone info to our virtio-device
	ll_send_info(ll_b);
	if (ll_b->ch_ctrl_vq) {
		err = ch_post_event(ll_b);
		if (!err)
			err = chameleon_transport_request(LL_CH_OP_HELLO, 0, NULL, 0);
		if (err) {
			WRITE_ONCE(ll_b->ch_broken, true);
			dev_warn(&vdev->dev, "Chameleon transport handshake failed: %d\n", err);
		}
	}
	proc_create_single("llfree_protocol", 0444, NULL, ll_protocol_show);
	dev_info(&vdev->dev, "HyperAlloc install-result-v1 ready, %u CPUs\n", num_cores);

	return 0;
free_ch:
	kfree(ll_b->ch_out);
	kfree(ll_b->ch_in);
	kfree(ll_b->ch_event);
	kfree(ll_b);
	return err;
}

static void remove_common(ll_balloon_t *vb)
{
	/* Now we reset the device so we can clean up the queues. */
	virtio_reset_device(vb->vdev);
	vb->vdev->config->del_vqs(vb->vdev);
}

static void ll_remove(struct virtio_device *vdev)
{
	ll_balloon_t *vb = vdev->priv;

	remove_proc_entry("llfree_protocol", NULL);
	mutex_lock(&ch_transport_lock);
	WRITE_ONCE(balloon, NULL);
	WRITE_ONCE(vb->ch_broken, true);
	virtio_reset_device(vdev);
	mutex_unlock(&ch_transport_lock);
	virtio_synchronize_cbs(vdev);
	cancel_work_sync(&vb->ch_event_work);
	cancel_work_sync(&vb->shrink_pagecache_work);
	remove_common(vb);
	kfree(vb->install_vqs);
	kfree(vb->install_buf);
	kfree(vb->ch_out);
	kfree(vb->ch_in);
	kfree(vb->ch_event);
	kfree(vb);
}

static unsigned int features[] = {
	LL_BALLOON_F_CHAMELEON_POLICY,
	LL_BALLOON_F_CHAMELEON_DATA,
	LL_BALLOON_F_CHAMELEON_RANGE,
	LL_BALLOON_F_INSTALL_RESULT,
	LL_BALLOON_F_SHRINK_PAGECACHE,
	LL_BALLOON_F_AUTO_MODE,
};

// TODO: try again with own id
// IDs can't be arbitrarily chosen as it seems, for now
// say that we are virtio-balloon
static const struct virtio_device_id id_table[] = {
	{ VIRTIO_ID_BALLOON, VIRTIO_DEV_ANY_ID },
	{ 0 },
};

static struct virtio_driver virtio_llfree_balloon_driver = {
	.feature_table = features,
	.feature_table_size = ARRAY_SIZE(features),
	.driver.name = KBUILD_MODNAME,
	.driver.owner = THIS_MODULE,
	.driver.suppress_bind_attrs = true,
	.id_table = id_table,
	.probe = ll_probe,
	.remove = ll_remove,
	.config_changed = ll_config_changed
};

module_virtio_driver(virtio_llfree_balloon_driver);
MODULE_DEVICE_TABLE(virtio, id_table);
MODULE_DESCRIPTION("Virtio llfree balloon driver");
MODULE_LICENSE("GPL");
