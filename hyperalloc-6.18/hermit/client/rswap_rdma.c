// SPDX-License-Identifier: GPL-2.0-only
/* Hermit remoteswap transport using Linux 6.18's in-tree RDMA API.
 * The existing Hermit memory-pool protocol is unchanged. A serialized RC QP
 * transfers each contiguous folio/MR segment in one WR when DMA limits permit.
 */
#include <linux/completion.h>
#include <linux/debugfs.h>
#include <linux/dma-mapping.h>
#include <linux/inet.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/net.h>
#include <linux/overflow.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <net/net_namespace.h>
#include <rdma/ib_verbs.h>
#include <rdma/rdma_cm.h>
#include "rswap_transport.h"
#include "wire.h"

#define RSWAP_TIMEOUT_MS 10000
struct rswap_completion {
	struct ib_cqe cqe;
	struct completion complete;
	int error;
	u32 bytes;
};
struct rswap_connection {
	struct rdma_cm_id *id;
	struct ib_pd *pd;
	struct ib_cq *cq;
	wait_queue_head_t cm_wait;
	int state, error;
	bool broken;
	struct rswap_message *send, *recv;
	u64 send_dma, recv_dma;
	bool send_mapped, recv_mapped;
	struct rswap_completion rx;
	struct rswap_message regions;
	unsigned long pages;
	size_t max_transfer;
	u64 segment_boundary;
	u32 max_msg_bytes;
	struct dentry *debug_dir;
};
static struct rswap_connection remote;
static DEFINE_MUTEX(io_lock);
static atomic64_t write_wrs, read_wrs, write_completions, read_completions;
static atomic64_t write_bytes, read_bytes, map_failures, map_retries;
static atomic64_t transfer_errors, region_splits, limit_splits;
static u32 largest_write_wr, largest_read_wr;
enum { RESOLVING, ADDRESS, ROUTE, CONNECTED };

static int rdma_stats_show(struct seq_file *seq, void *unused)
{
	seq_printf(seq, "max_msg_bytes %u\nmax_transfer_bytes %zu\nsegment_boundary 0x%llx\n",
		remote.max_msg_bytes, remote.max_transfer, remote.segment_boundary);
	seq_printf(seq, "broken %u\nlargest_write_wr_bytes %u\nlargest_read_wr_bytes %u\n",
		READ_ONCE(remote.broken), READ_ONCE(largest_write_wr), READ_ONCE(largest_read_wr));
#define RSTAT(name) seq_printf(seq, #name " %lld\n", atomic64_read(&name))
	RSTAT(write_wrs); RSTAT(read_wrs);
	RSTAT(write_completions); RSTAT(read_completions);
	RSTAT(write_bytes); RSTAT(read_bytes);
	RSTAT(map_failures); RSTAT(map_retries); RSTAT(transfer_errors);
	RSTAT(region_splits); RSTAT(limit_splits);
#undef RSTAT
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(rdma_stats);

static void wr_done(struct ib_cq *cq, struct ib_wc *wc)
{
	struct rswap_completion *request = container_of(wc->wr_cqe,
		struct rswap_completion, cqe);
	(void)cq;
	request->error = wc->status == IB_WC_SUCCESS ? 0 : -EIO;
	request->bytes = wc->byte_len;
	complete(&request->complete);
}

static void init_request(struct rswap_completion *request)
{
	memset(request, 0, sizeof(*request));
	request->cqe.done = wr_done;
	init_completion(&request->complete);
}

static int wait_request(struct rswap_completion *request)
{
	if (!wait_for_completion_timeout(&request->complete,
			msecs_to_jiffies(RSWAP_TIMEOUT_MS))) {
		WRITE_ONCE(remote.broken, true);
		/* No buffer or stack CQE can outlive a failed request. Draining
		 * consumes every completion before DMA unmap / caller return. */
		ib_drain_qp(remote.id->qp);
		return -ETIMEDOUT;
	}
	if (request->error)
		WRITE_ONCE(remote.broken, true);
	return request->error;
}

static int cm_event(struct rdma_cm_id *id, struct rdma_cm_event *event)
{
	struct rswap_connection *r = id->context;
	switch (event->event) {
	case RDMA_CM_EVENT_ADDR_RESOLVED:
		WRITE_ONCE(r->state, ADDRESS);
		break;
	case RDMA_CM_EVENT_ROUTE_RESOLVED:
		WRITE_ONCE(r->state, ROUTE);
		break;
	case RDMA_CM_EVENT_ESTABLISHED:
		WRITE_ONCE(r->state, CONNECTED);
		break;
	case RDMA_CM_EVENT_TIMEWAIT_EXIT:
		return 0;
	default:
		WRITE_ONCE(r->error, event->status ? -abs(event->status) : -ECONNRESET);
		WRITE_ONCE(r->broken, true);
		break;
	}
	wake_up_all(&r->cm_wait);
	return 0;
}

static int wait_cm(int state)
{
	if (!wait_event_timeout(remote.cm_wait,
		READ_ONCE(remote.state) >= state || READ_ONCE(remote.error),
		msecs_to_jiffies(RSWAP_TIMEOUT_MS)))
		return -ETIMEDOUT;
	return READ_ONCE(remote.error);
}

static int receive_message(void)
{
	struct ib_sge sge = { .addr = remote.recv_dma,
		.length = sizeof(*remote.recv), .lkey = remote.pd->local_dma_lkey };
	struct ib_recv_wr wr = { .sg_list = &sge, .num_sge = 1 };
	const struct ib_recv_wr *bad;
	init_request(&remote.rx);
	wr.wr_cqe = &remote.rx.cqe;
	ib_dma_sync_single_for_device(remote.id->device, remote.recv_dma,
		sizeof(*remote.recv), DMA_BIDIRECTIONAL);
	return ib_post_recv(remote.id->qp, &wr, &bad);
}

static int finish_receive(int expected)
{
	int ret = wait_request(&remote.rx);
	if (ret)
		return ret;
	ib_dma_sync_single_for_cpu(remote.id->device, remote.recv_dma,
		sizeof(*remote.recv), DMA_BIDIRECTIONAL);
	if (remote.rx.bytes != sizeof(*remote.recv) || remote.recv->type != expected)
		return -EPROTO;
	return 0;
}

static int exchange(int request_type, int expected)
{
	struct rswap_completion completion;
	struct ib_sge sge = { .addr = remote.send_dma,
		.length = sizeof(*remote.send), .lkey = remote.pd->local_dma_lkey };
	struct ib_send_wr wr = { .sg_list = &sge, .num_sge = 1,
		.opcode = IB_WR_SEND, .send_flags = IB_SEND_SIGNALED };
	const struct ib_send_wr *bad;
	int ret;
	ret = receive_message();
	if (ret)
		return ret;
	init_request(&completion);
	wr.wr_cqe = &completion.cqe;
	ib_dma_sync_single_for_cpu(remote.id->device, remote.send_dma,
		sizeof(*remote.send), DMA_BIDIRECTIONAL);
	memset(remote.send, 0, sizeof(*remote.send));
	remote.send->type = request_type;
	remote.send->mapped_chunk = remote.regions.mapped_chunk;
	ib_dma_sync_single_for_device(remote.id->device, remote.send_dma,
		sizeof(*remote.send), DMA_BIDIRECTIONAL);
	ret = ib_post_send(remote.id->qp, &wr, &bad);
	if (!ret)
		ret = wait_request(&completion);
	if (!ret)
		ret = finish_receive(expected);
	return ret;
}

void rswap_rdma_exit(void)
{
	debugfs_remove_recursive(remote.debug_dir);
	remote.debug_dir = NULL;
	mutex_lock(&io_lock);
	WRITE_ONCE(remote.broken, true);
	if (remote.id && remote.id->qp) {
		rdma_disconnect(remote.id);
		ib_drain_qp(remote.id->qp);
		rdma_destroy_qp(remote.id);
	}
	if (remote.send_mapped)
		ib_dma_unmap_single(remote.id->device, remote.send_dma,
			sizeof(*remote.send), DMA_BIDIRECTIONAL);
	if (remote.recv_mapped)
		ib_dma_unmap_single(remote.id->device, remote.recv_dma,
			sizeof(*remote.recv), DMA_BIDIRECTIONAL);
	kfree(remote.send);
	kfree(remote.recv);
	if (remote.cq)
		ib_free_cq(remote.cq);
	if (remote.pd)
		ib_dealloc_pd(remote.pd);
	if (remote.id)
		rdma_destroy_id(remote.id);
	memset(&remote, 0, sizeof(remote));
	mutex_unlock(&io_lock);
}

int rswap_rdma_init(const char *ip, unsigned int port, unsigned long *pages)
{
	struct ib_port_attr port_attr;
	struct sockaddr_storage address = { 0 };
	struct sockaddr_in *v4 = (void *)&address;
	struct sockaddr_in6 *v6 = (void *)&address;
	struct ib_qp_init_attr qp = { .qp_type = IB_QPT_RC,
		.sq_sig_type = IB_SIGNAL_REQ_WR,
		.cap = { .max_send_wr = 8, .max_recv_wr = 8,
			.max_send_sge = 1, .max_recv_sge = 1 } };
	struct rdma_conn_param conn = { .responder_resources = 1,
		.initiator_depth = 1, .retry_count = 3, .rnr_retry_count = 3 };
	int ret, i;
	unsigned long total = 0;
	BUILD_BUG_ON(sizeof(struct rswap_message) != 328);
	if (!ip || !*ip || !port || port > 65535)
		return -EINVAL;
	if (in4_pton(ip, -1, (u8 *)&v4->sin_addr, -1, NULL)) {
		v4->sin_family = AF_INET;
		v4->sin_port = htons(port);
	} else if (in6_pton(ip, -1, (u8 *)&v6->sin6_addr, -1, NULL)) {
		v6->sin6_family = AF_INET6;
		v6->sin6_port = htons(port);
	} else {
		return -EINVAL;
	}
	init_waitqueue_head(&remote.cm_wait);
	remote.id = rdma_create_id(&init_net, cm_event, &remote, RDMA_PS_TCP, IB_QPT_RC);
	if (IS_ERR(remote.id)) {
		ret = PTR_ERR(remote.id);
		remote.id = NULL;
		return ret;
	}
	ret = rdma_resolve_addr(remote.id, NULL, (struct sockaddr *)&address,
		RSWAP_TIMEOUT_MS);
	if (ret || (ret = wait_cm(ADDRESS)))
		goto fail;
	ret = rdma_resolve_route(remote.id, RSWAP_TIMEOUT_MS);
	if (ret || (ret = wait_cm(ROUTE)))
		goto fail;
	ret = ib_query_port(remote.id->device, remote.id->port_num, &port_attr);
	if (ret)
		goto fail;
	remote.max_msg_bytes = port_attr.max_msg_sz;
	remote.max_transfer = min_t(size_t, port_attr.max_msg_sz,
		ib_dma_max_seg_size(remote.id->device));
	remote.segment_boundary = U64_MAX;
	if (!ib_uses_virt_dma(remote.id->device)) {
		struct device *dma_device = remote.id->device->dma_device;

		remote.max_transfer = min(remote.max_transfer,
			dma_max_mapping_size(dma_device));
		remote.segment_boundary = dma_get_seg_boundary(dma_device);
		if (remote.segment_boundary != U64_MAX)
			remote.max_transfer = min_t(u64, remote.max_transfer,
				remote.segment_boundary + 1);
	}
	remote.max_transfer = round_down(remote.max_transfer, PAGE_SIZE);
	if (remote.max_transfer < PAGE_SIZE) {
		ret = -EOPNOTSUPP;
		goto fail;
	}
	remote.pd = ib_alloc_pd(remote.id->device, 0);
	if (IS_ERR(remote.pd)) {
		ret = PTR_ERR(remote.pd);
		remote.pd = NULL;
		goto fail;
	}
	remote.cq = ib_alloc_cq(remote.id->device, &remote, 32, 0, IB_POLL_WORKQUEUE);
	if (IS_ERR(remote.cq)) {
		ret = PTR_ERR(remote.cq);
		remote.cq = NULL;
		goto fail;
	}
	qp.send_cq = qp.recv_cq = remote.cq;
	ret = rdma_create_qp(remote.id, remote.pd, &qp);
	if (ret)
		goto fail;
	remote.send = kzalloc(sizeof(*remote.send), GFP_KERNEL);
	remote.recv = kzalloc(sizeof(*remote.recv), GFP_KERNEL);
	if (!remote.send || !remote.recv) {
		ret = -ENOMEM;
		goto fail;
	}
	remote.send_dma = ib_dma_map_single(remote.id->device, remote.send,
		sizeof(*remote.send), DMA_BIDIRECTIONAL);
	if (ib_dma_mapping_error(remote.id->device, remote.send_dma)) {
		ret = -EIO;
		goto fail;
	}
	remote.send_mapped = true;
	remote.recv_dma = ib_dma_map_single(remote.id->device, remote.recv,
		sizeof(*remote.recv), DMA_BIDIRECTIONAL);
	if (ib_dma_mapping_error(remote.id->device, remote.recv_dma)) {
		ret = -EIO;
		goto fail;
	}
	remote.recv_mapped = true;
	ret = receive_message();
	if (ret)
		goto fail;
	ret = rdma_connect(remote.id, &conn);
	if (ret || (ret = wait_cm(CONNECTED)) || (ret = finish_receive(RSWAP_AVAILABLE)))
		goto fail;
	ret = exchange(RSWAP_QUERY, RSWAP_FREE_SIZE);
	if (ret)
		goto fail;
	if (remote.recv->mapped_chunk < 1 || remote.recv->mapped_chunk > RSWAP_MAX_REGIONS) {
		ret = -EPROTO;
		goto fail;
	}
	remote.regions.mapped_chunk = remote.recv->mapped_chunk;
	ret = exchange(RSWAP_REQUEST_CHUNKS, RSWAP_GOT_CHUNKS);
	if (ret)
		goto fail;
	if (remote.recv->mapped_chunk != remote.regions.mapped_chunk) {
		ret = -EPROTO;
		goto fail;
	}
	remote.regions = *remote.recv;
	for (i = 0; i < remote.regions.mapped_chunk; i++) {
		u64 size = remote.regions.mapped_size[i], end;
		if (!size || !IS_ALIGNED(size, PAGE_SIZE) || !remote.regions.rkey[i] ||
		    check_add_overflow(remote.regions.buf[i], size, &end) ||
		    size >> PAGE_SHIFT > ULONG_MAX - total) {
			ret = -EPROTO;
			goto fail;
		}
		total += size >> PAGE_SHIFT;
	}
	remote.pages = total;
	*pages = total;
	remote.debug_dir = debugfs_create_dir("hermit_rdma", NULL);
	if (IS_ERR(remote.debug_dir))
		remote.debug_dir = NULL;
	if (remote.debug_dir)
		debugfs_create_file("stats", 0400, remote.debug_dir, NULL, &rdma_stats_fops);
	pr_info("hermit: RDMA connected %s:%u remote_pages=%lu regions=%d max_transfer=%zu\n",
		ip, port, total, remote.regions.mapped_chunk, remote.max_transfer);
	return 0;
fail:
	pr_err("hermit: RDMA connect failed error=%d state=%d\n", ret, remote.state);
	rswap_rdma_exit();
	return ret;
}

/* io_lock serializes the QP and pins the connection. A folio is physically
 * contiguous, so dma_map_page can map several of its pages in one segment.
 * The caller bounds this segment by both the folio and the remote MR. */
static int transfer_segment(unsigned int region, unsigned long region_page,
		struct page *page, size_t *bytes, bool write)
{
	struct ib_device *device = remote.id->device;
	struct rswap_completion completion;
	struct ib_sge sge;
	struct ib_rdma_wr wr = { .wr = { .sg_list = &sge, .num_sge = 1,
		.send_flags = IB_SEND_SIGNALED } };
	const struct ib_send_wr *bad;
	enum dma_data_direction direction = write ? DMA_TO_DEVICE : DMA_FROM_DEVICE;
	u64 dma, last;
	size_t length = *bytes, smaller;
	int ret;

	for (;;) {
		dma = ib_dma_map_page(device, page, 0, length, direction);
		if (ib_dma_mapping_error(device, dma)) {
			atomic64_inc(&map_failures);
			smaller = round_down(length / 2, PAGE_SIZE);
		} else if (check_add_overflow(dma, (u64)length - 1, &last)) {
			ib_dma_unmap_page(device, dma, length, direction);
			return -EOVERFLOW;
		} else if ((dma & ~remote.segment_boundary) !=
			   (last & ~remote.segment_boundary)) {
			/* DMA addresses need not preserve the physical alignment.
			 * Check the actual mapping before submitting any operation. */
			smaller = round_down(remote.segment_boundary -
				(dma & remote.segment_boundary) + 1, PAGE_SIZE);
			ib_dma_unmap_page(device, dma, length, direction);
		} else {
			break;
		}
		if (smaller < PAGE_SIZE)
			return -EIO;
		length = smaller;
		atomic64_inc(&map_retries);
	}
	init_request(&completion);
	sge.addr = dma;
	sge.length = length;
	sge.lkey = remote.pd->local_dma_lkey;
	wr.wr.wr_cqe = &completion.cqe;
	wr.wr.opcode = write ? IB_WR_RDMA_WRITE : IB_WR_RDMA_READ;
	wr.remote_addr = remote.regions.buf[region] + ((u64)region_page << PAGE_SHIFT);
	wr.rkey = remote.regions.rkey[region];
	ret = ib_post_send(remote.id->qp, &wr.wr, &bad);
	if (!ret) {
		atomic64_inc(write ? &write_wrs : &read_wrs);
		if (write)
			WRITE_ONCE(largest_write_wr, max_t(u32, largest_write_wr, length));
		else
			WRITE_ONCE(largest_read_wr, max_t(u32, largest_read_wr, length));
		ret = wait_request(&completion);
	}
	/* Completion, or timeout's QP drain, ends all access to the DMA mapping.
	 * DMA_FROM_DEVICE unmap also makes a successful read visible to the CPU.
	 * An error after earlier segments still fails the entire folio load. */
	ib_dma_unmap_page(device, dma, length, direction);
	if (!ret) {
		atomic64_inc(write ? &write_completions : &read_completions);
		atomic64_add(length, write ? &write_bytes : &read_bytes);
		*bytes = length;
	}
	return ret;
}

static int transfer(unsigned long slot, struct folio *folio, bool write)
{
	unsigned long pages = folio_nr_pages(folio), done = 0, base = 0;
	unsigned int region = 0;
	int ret = 0;
	mutex_lock(&io_lock);
	if (READ_ONCE(remote.broken) || !remote.id)
		ret = -ENOTCONN;
	else if (pages > remote.pages || slot > remote.pages - pages)
		ret = -ERANGE;
	while (!ret && done < pages) {
		unsigned long region_pages, offset, nr;
		size_t bytes;

		if (READ_ONCE(remote.broken)) {
			ret = -ENOTCONN;
			break;
		}
		while (region < remote.regions.mapped_chunk) {
			region_pages = remote.regions.mapped_size[region] >> PAGE_SHIFT;
			if (slot + done - base < region_pages)
				break;
			base += region_pages;
			region++;
		}
		if (region == remote.regions.mapped_chunk) {
			ret = -ERANGE;
			break;
		}
		offset = slot + done - base;
		nr = min(pages - done, region_pages - offset);
		if (nr < pages - done)
			atomic64_inc(&region_splits);
		bytes = nr << PAGE_SHIFT;
		if (bytes > remote.max_transfer) {
			bytes = remote.max_transfer;
			atomic64_inc(&limit_splits);
		}
		ret = transfer_segment(region, offset, folio_page(folio, done), &bytes, write);
		if (!ret)
			done += bytes >> PAGE_SHIFT;
	}
	if (ret)
		atomic64_inc(&transfer_errors);
	mutex_unlock(&io_lock);
	return ret;
}

int rswap_rdma_store(unsigned long slot, struct folio *folio)
{
	return transfer(slot, folio, true);
}

int rswap_rdma_load(unsigned long slot, struct folio *folio)
{
	return transfer(slot, folio, false);
}
