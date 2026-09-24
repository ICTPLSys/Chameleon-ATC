#ifndef _LINUX_VIRTIO_LLFREE_BALLOON_H
#define _LINUX_VIRTIO_LLFREE_BALLOON_H
/* This header is BSD licensed so anyone can use the definitions to implement
 * compatible drivers/servers.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 * 3. Neither the name of IBM nor the names of its contributors
 *    may be used to endorse or promote products derived from this software
 *    without specific prior written permission.
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS ``AS IS'' AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED.  IN NO EVENT SHALL IBM OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
 * OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
 * SUCH DAMAGE. */
#include "standard-headers/linux/types.h"
#include "standard-headers/linux/virtio_types.h"
#include "standard-headers/linux/virtio_ids.h"
#include "standard-headers/linux/virtio_config.h"

/* The feature bitmap for virtio llfree balloon */
#define LL_BALLOON_F_SHRINK_PAGECACHE 1
#define LL_BALLOON_F_AUTO_MODE 4
#define LL_BALLOON_F_VFIO 6
#define LL_BALLOON_F_INSTALL_RESULT 8
// #define LL_BALLOON_F_IOCTL 7

/* Optional Chameleon control transport. Old queue indices remain unchanged. */
#define LL_BALLOON_F_CHAMELEON_RANGE 9
#define LL_BALLOON_F_CHAMELEON_POLICY 10
#define LL_BALLOON_F_CHAMELEON_DATA 11
#define LL_CHAMELEON_VERSION 1
#define LL_CHAMELEON_MAX_RANGES 128
#define LL_CHAMELEON_CTRL_VQ(n) (2U + (n))
#define LL_CHAMELEON_EVENT_VQ(n) (3U + (n))
#define LL_CHAMELEON_EXTRA_VQS 2

#define LL_CH_MSG_RESPONSE 1U
#define LL_CH_MSG_EVENT 2U
#define LL_CH_RANGE_DISCARD_TEST 1U
/* A real backend has completed saving this exact immutable source. */
#define LL_CH_RANGE_DATA_SAVED 2U

enum ll_chameleon_op {
	LL_CH_OP_HELLO = 1,
	LL_CH_OP_REGISTER = 2,
	LL_CH_OP_READY = 3,
	LL_CH_OP_CANCEL = 4,
	LL_CH_OP_FINALIZE_REQUEST = 5,
	LL_CH_OP_FINALIZE_ACK = 6,
	LL_CH_OP_COMMIT_RESULT = 7,
	LL_CH_OP_INSTALL = 8,
	LL_CH_OP_QUERY = 9,
	LL_CH_OP_POLICY_ACQUIRE = 10,
	LL_CH_OP_POLICY_RELEASE = 11,
	LL_CH_OP_POLICY_SNAPSHOT = 12,
	LL_CH_OP_POLICY_RECLAIM_FREE = 13,
	LL_CH_OP_POLICY_RETURN_FREE = 14,
	LL_CH_OP_POLICY_GATE = 15,
	LL_CH_OP_FORGET = 16,
};

enum ll_chameleon_state {
	LL_CH_STATE_UNKNOWN = 0,
	LL_CH_STATE_REGISTERED = 1,
	LL_CH_STATE_READY = 2,
	LL_CH_STATE_FINALIZING = 3,
	LL_CH_STATE_BLOCKED = 4,
	LL_CH_STATE_RETIRED = 5,
	LL_CH_STATE_INSTALLED = 6,
	LL_CH_STATE_CANCELED = 7,
	LL_CH_STATE_ERROR = 8,
};

/* 48 bytes. All wire fields are little endian; status is positive errno.
 * Requests have flags/status/reserved zero. Responses echo request_id/op;
 * events have EVENT and a host batch_id. HELLO requests use session zero.
 * Every later message carries the returned session, including CANCEL.
 */
struct ll_chameleon_header {
	uint16_t version;
	uint16_t op;
	uint32_t flags;
	uint64_t session;
	uint64_t request_id;
	uint64_t batch_id;
	uint32_t nr_ranges;
	uint32_t status;
	uint64_t reserved;
};

/* 32 bytes. token is the guest's unique object identity for this session.
 * GPA is page aligned; nr_pages == 1<<order, with order 0 or 2..9.
 * The descriptor is immutable after REGISTER apart from status/state.
 * REGISTER only admits pending objects. READY makes them eligible.
 * FINALIZE_ACK uses status=0 to accept, positive errno to exclude a range.
 * INSTALL is legal only after COMMIT_RESULT; during FINALIZING/BLOCKED it
 * returns EBUSY. Partial failures are reported per range, never rolled back
 * implicitly. QUERY is read-only and cannot unblock retained ranges.
 */
struct ll_chameleon_range {
	uint64_t token;
	uint64_t gpa;
	uint32_t nr_pages;
	uint16_t order;
	uint16_t flags;
	uint32_t status;
	uint32_t state;
};

/* Feature 10 policy commands have nr_ranges=0 and this 80-byte payload.
 * The response echoes epoch/requested_bytes; completed_bytes is the actual
 * synchronous free-capacity change. Local capacity is nominal total minus
 * hard balloon and confirmed cold retirement, NOT RSS or fully populated RAM.
 * RETURN restores HyperAlloc availability; its soft pages install on demand.
 * Each nonzero lease admits at most one free action for a given epoch.
 */
#define LL_CH_POLICY_ALLOW_RECLAIM 1U
#define LL_CH_POLICY_LEASED 2U
#define LL_CH_POLICY_UNCERTAIN 4U
#define LL_CH_EPT_DEFERRED 0U
#define LL_CH_EPT_IMMEDIATE 1U
struct ll_chameleon_policy {
	uint64_t lease;
	uint64_t epoch;
	uint64_t requested_bytes;
	uint64_t completed_bytes;
	uint64_t total_bytes;
	uint64_t local_bytes;
	uint64_t hard_reclaimed_bytes;
	uint64_t retired_bytes;
	uint64_t soft_reclaimed_bytes;
	uint32_t flags;
	uint32_t ept_mode;
};

struct ll_balloon_config {
	/* Legacy writable field; the Chameleon fields below are read-only. */
	uint32_t shrink_pagecache;
	uint16_t chameleon_version;
	uint16_t chameleon_max_ranges;
	uint64_t chameleon_session;
	uint64_t chameleon_pool_pages;
	uint64_t chameleon_batch_pages;
	uint64_t chameleon_watermark_bytes;
};


#endif /* _LINUX_VIRTIO_LLFREE_BALLOON_H */
