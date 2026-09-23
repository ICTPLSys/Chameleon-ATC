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
#include <linux/virtio_types.h>

/* The feature bitmap for virtio llfree balloon */
#define LL_BALLOON_F_SHRINK_PAGECACHE 1
#define LL_BALLOON_F_AUTO_MODE 4
#define LL_BALLOON_F_VFIO 6
#define LL_BALLOON_F_KVM_MAP_IOCTL 7
#define LL_BALLOON_F_INSTALL_RESULT 8


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
	__le16 version;
	__le16 op;
	__le32 flags;
	__le64 session;
	__le64 request_id;
	__le64 batch_id;
	__le32 nr_ranges;
	__le32 status;
	__le64 reserved;
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
	__le64 token;
	__le64 gpa;
	__le32 nr_pages;
	__le16 order;
	__le16 flags;
	__le32 status;
	__le32 state;
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
	__le64 lease;
	__le64 epoch;
	__le64 requested_bytes;
	__le64 completed_bytes;
	__le64 total_bytes;
	__le64 local_bytes;
	__le64 hard_reclaimed_bytes;
	__le64 retired_bytes;
	__le64 soft_reclaimed_bytes;
	__le32 flags;
	__le32 ept_mode;
};

struct ll_balloon_config {
	/* Legacy writable field; the Chameleon fields below are read-only. */
	__le32 shrink_pagecache;
	__le16 chameleon_version;
	__le16 chameleon_max_ranges;
	__le64 chameleon_session;
	__le64 chameleon_pool_pages;
	__le64 chameleon_batch_pages;
	__le64 chameleon_watermark_bytes;
};


#endif /* _LINUX_VIRTIO_LLFREE_BALLOON_H */
