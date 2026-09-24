/* SPDX-License-Identifier: GPL-2.0-only */
/* Hermit remoteswap memory server, ported to current rdma-core.
 * Retains the original AVAILABLE / QUERY / REQUEST_CHUNKS wire exchange and
 * one-sided RDMA memory pool. A single client owns the pool at a time.
 */
#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <poll.h>
#include <rdma/rdma_cma.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "../wire.h"

static volatile sig_atomic_t stopping;
struct session {
	struct rdma_cm_id *id;
	struct ibv_pd *pd;
	struct ibv_comp_channel *channel;
	struct ibv_cq *cq;
	struct ibv_mr *pool_mr, *recv_mr, *send_mr;
	struct rswap_message recv, send;
	int pending[8];
	unsigned int head, tail;
	bool sending;
};
static struct session client;
static void *pool;
static size_t pool_bytes;

static void stop_signal(int number)
{
	(void)number;
	stopping = 1;
}

static int post_receive(struct session *s)
{
	struct ibv_sge sge = { .addr = (uintptr_t)&s->recv,
		.length = sizeof(s->recv), .lkey = s->recv_mr->lkey };
	struct ibv_recv_wr wr = { .wr_id = 1, .sg_list = &sge, .num_sge = 1 };
	struct ibv_recv_wr *bad;
	return ibv_post_recv(s->id->qp, &wr, &bad);
}

static int flush_send(struct session *s)
{
	struct ibv_sge sge = { .addr = (uintptr_t)&s->send,
		.length = sizeof(s->send), .lkey = s->send_mr->lkey };
	struct ibv_send_wr wr = { .wr_id = 2, .sg_list = &sge, .num_sge = 1,
		.opcode = IBV_WR_SEND, .send_flags = IBV_SEND_SIGNALED };
	struct ibv_send_wr *bad;
	if (s->sending || s->head == s->tail)
		return 0;
	memset(&s->send, 0, sizeof(s->send));
	s->send.type = s->pending[s->head++ % 8];
	s->send.mapped_chunk = 1;
	if (s->send.type == RSWAP_GOT_CHUNKS) {
		s->send.buf[0] = (uintptr_t)pool;
		s->send.mapped_size[0] = pool_bytes;
		s->send.rkey[0] = s->pool_mr->rkey;
	}
	s->sending = true;
	return ibv_post_send(s->id->qp, &wr, &bad);
}

static int send_message(struct session *s, int type)
{
	if (s->tail - s->head >= 8)
		return ENOBUFS;
	s->pending[s->tail++ % 8] = type;
	return flush_send(s);
}

static int poll_completions(struct session *s)
{
	struct ibv_wc wc;
	int count;
	while ((count = ibv_poll_cq(s->cq, 1, &wc)) > 0) {
		if (wc.status != IBV_WC_SUCCESS) {
			fprintf(stderr, "completion failed: %s\n", ibv_wc_status_str(wc.status));
			return EIO;
		}
		if (wc.opcode == IBV_WC_RECV) {
			int type;
			if (wc.byte_len != sizeof(s->recv))
				return EPROTO;
			if (s->recv.type == RSWAP_QUERY)
				type = RSWAP_FREE_SIZE;
			else if (s->recv.type == RSWAP_REQUEST_CHUNKS &&
				 s->recv.mapped_chunk == 1)
				type = RSWAP_GOT_CHUNKS;
			else
				return EPROTO;
			if (post_receive(s) || send_message(s, type))
				return EIO;
		} else if (wc.opcode == IBV_WC_SEND) {
			s->sending = false;
			if (flush_send(s))
				return EIO;
		} else {
			return EPROTO;
		}
	}
	return count < 0 ? EIO : 0;
}

static void close_session(struct session *s)
{
	/* Destroy QP before freeing anything to which the NIC may still write. */
	if (s->id && s->id->qp)
		rdma_destroy_qp(s->id);
	if (s->pool_mr)
		ibv_dereg_mr(s->pool_mr);
	if (s->recv_mr)
		ibv_dereg_mr(s->recv_mr);
	if (s->send_mr)
		ibv_dereg_mr(s->send_mr);
	if (s->cq) {
		struct pollfd ready = { .fd = s->channel->fd, .events = POLLIN };
		/* A queued CQ event must be acknowledged before destroy_cq(). */
		while (poll(&ready, 1, 0) > 0 && (ready.revents & POLLIN)) {
			struct ibv_cq *cq;
			void *context;
			if (ibv_get_cq_event(s->channel, &cq, &context))
				break;
			ibv_ack_cq_events(cq, 1);
		}
		ibv_destroy_cq(s->cq);
	}
	if (s->channel)
		ibv_destroy_comp_channel(s->channel);
	if (s->pd)
		ibv_dealloc_pd(s->pd);
	if (s->id)
		rdma_destroy_id(s->id);
	memset(s, 0, sizeof(*s));
}

static int open_session(struct rdma_cm_id *id)
{
	struct session *s = &client;
	struct ibv_qp_init_attr qp = { .qp_type = IBV_QPT_RC,
		.cap = { .max_send_wr = 8, .max_recv_wr = 8,
			.max_send_sge = 1, .max_recv_sge = 1 } };
	struct rdma_conn_param conn = { .responder_resources = 1,
		.initiator_depth = 1, .rnr_retry_count = 7 };
	if (s->id) {
		rdma_reject(id, NULL, 0);
		rdma_destroy_id(id);
		return 0;
	}
	s->id = id;
	s->pd = ibv_alloc_pd(id->verbs);
	s->channel = ibv_create_comp_channel(id->verbs);
	if (!s->pd || !s->channel)
		goto fail;
	s->cq = ibv_create_cq(id->verbs, 32, NULL, s->channel, 0);
	if (!s->cq || ibv_req_notify_cq(s->cq, 0))
		goto fail;
	qp.send_cq = qp.recv_cq = s->cq;
	if (rdma_create_qp(id, s->pd, &qp))
		goto fail;
	s->pool_mr = ibv_reg_mr(s->pd, pool, pool_bytes,
		IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_READ);
	s->recv_mr = ibv_reg_mr(s->pd, &s->recv, sizeof(s->recv), IBV_ACCESS_LOCAL_WRITE);
	s->send_mr = ibv_reg_mr(s->pd, &s->send, sizeof(s->send), 0);
	if (!s->pool_mr || !s->recv_mr || !s->send_mr || post_receive(s))
		goto fail;
	if (rdma_accept(id, &conn))
		goto fail;
	return 0;
fail:
	fprintf(stderr, "RDMA session setup failed: %s\n", strerror(errno));
	rdma_reject(id, NULL, 0);
	close_session(s);
	return -1;
}

static int parse_number(const char *text, unsigned long maximum, unsigned long *value)
{
	char *end;
	errno = 0;
	*value = strtoul(text, &end, 10);
	return errno || !*text || *end || !*value || *value > maximum ? -1 : 0;
}

int main(int argc, char **argv)
{
	struct rdma_event_channel *ec;
	struct rdma_cm_id *listener = NULL;
	struct sockaddr_storage address = { 0 };
	unsigned long port, mib;
	int result = 0;
	_Static_assert(sizeof(struct rswap_message) == 328, "Hermit wire layout");
	if (argc != 4 || parse_number(argv[2], 65535, &port) ||
	    parse_number(argv[3], 65536, &mib)) {
		fprintf(stderr, "Usage: %s <bind IPv4/IPv6> <port> <pool MiB, 1..65536>\n", argv[0]);
		return 2;
	}
	struct sockaddr_in *v4 = (void *)&address;
	struct sockaddr_in6 *v6 = (void *)&address;
	if (inet_pton(AF_INET, argv[1], &v4->sin_addr) == 1) {
		v4->sin_family = AF_INET;
		v4->sin_port = htons(port);
	} else if (inet_pton(AF_INET6, argv[1], &v6->sin6_addr) == 1) {
		v6->sin6_family = AF_INET6;
		v6->sin6_port = htons(port);
	} else {
		fprintf(stderr, "Invalid bind address\n");
		return 2;
	}
	pool_bytes = (size_t)mib << 20;
	if (posix_memalign(&pool, 4096, pool_bytes)) {
		fprintf(stderr, "Unable to allocate memory pool\n");
		return 1;
	}
	memset(pool, 0, pool_bytes);
	ec = rdma_create_event_channel();
	if (!ec || rdma_create_id(ec, &listener, NULL, RDMA_PS_TCP) ||
	    rdma_bind_addr(listener, (struct sockaddr *)&address) ||
	    rdma_listen(listener, 4)) {
		fprintf(stderr, "RDMA listener setup failed: %s\n", strerror(errno));
		result = 1;
		goto done;
	}
	signal(SIGINT, stop_signal);
	signal(SIGTERM, stop_signal);
	printf("READY Hermit RDMA %s:%lu pool_bytes=%zu\n", argv[1], port, pool_bytes);
	fflush(stdout);
	while (!stopping) {
		struct pollfd fds[2] = { { .fd = ec->fd, .events = POLLIN },
			{ .fd = client.channel ? client.channel->fd : -1, .events = POLLIN } };
		int ready = poll(fds, 2, 250);
		if (ready < 0) {
			if (errno == EINTR)
				continue;
			result = 1;
			break;
		}
		/* CQ first: CM disconnect may destroy the channel. */
		if (fds[1].revents & POLLIN) {
			struct ibv_cq *cq;
			void *context;
			if (ibv_get_cq_event(client.channel, &cq, &context)) {
				result = 1;
				break;
			}
			ibv_ack_cq_events(cq, 1);
			if (ibv_req_notify_cq(cq, 0) || poll_completions(&client))
				rdma_disconnect(client.id);
		}
		if (fds[0].revents & POLLIN) {
			struct rdma_cm_event *event;
			if (rdma_get_cm_event(ec, &event)) {
				result = 1;
				break;
			}
			enum rdma_cm_event_type type = event->event;
			struct rdma_cm_id *id = event->id;
			int status = event->status;
			rdma_ack_cm_event(event);
			if (type == RDMA_CM_EVENT_CONNECT_REQUEST) {
				open_session(id);
			} else if (type == RDMA_CM_EVENT_ESTABLISHED && id == client.id) {
				if (send_message(&client, RSWAP_AVAILABLE))
					rdma_disconnect(id);
			} else if (type == RDMA_CM_EVENT_DISCONNECTED && id == client.id) {
				close_session(&client);
				printf("DISCONNECTED Hermit RDMA; pool available\n");
				fflush(stdout);
			} else if (type != RDMA_CM_EVENT_TIMEWAIT_EXIT) {
				fprintf(stderr, "RDMA event %s status=%d\n", rdma_event_str(type), status);
				if (id == client.id)
					close_session(&client);
			}
		}
	}
done:
	if (client.id)
		rdma_disconnect(client.id);
	close_session(&client);
	if (listener)
		rdma_destroy_id(listener);
	if (ec)
		rdma_destroy_event_channel(ec);
	free(pool);
	return result;
}
