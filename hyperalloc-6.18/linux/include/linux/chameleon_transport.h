/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef _LINUX_CHAMELEON_TRANSPORT_H
#define _LINUX_CHAMELEON_TRANSPORT_H
#include <linux/types.h>
#include <linux/errno.h>
#include <linux/kconfig.h>
#include <uapi/linux/virtio_llfree_balloon.h>

/* Assignment/accounting capacity, deliberately distinct from QEMU RSS. */
struct chameleon_capacity {
	u64 total_bytes, local_bytes, hard_reclaimed_bytes, retired_bytes;
	u64 soft_reclaimed_bytes;
	u32 ept_mode;
	bool leased, reclaim_allowed, uncertain;
};

/* Sleepable synchronous control request; caller holds no folio or PTL lock.
 * Saved-data INSTALL may hold mmap read/write: its response completes directly
 * from the control-virtqueue callback, independently of the MM event worker.
 * Host control processing must not wait for a Guest event acknowledgement;
 * an INSTALL before FINALIZE_ACK returns EBUSY and can be retried. Other MM
 * control requests, including FINALIZE_ACK, are issued after dropping mmap.
 * Transport fills session/request identity and returns per-range status/state.
 * A transport failure is uncertain: callers must retain GPA reservations. */
#if IS_REACHABLE(CONFIG_VIRTIO_LLFREE_BALLOON)
int chameleon_transport_request(u16 op, u64 batch,
		struct ll_chameleon_range *ranges, unsigned int nr);
bool chameleon_transport_available(void);
bool chameleon_data_available(void);
bool chameleon_policy_available(void);
int chameleon_policy_acquire(u64 *lease, struct chameleon_capacity *capacity);
int chameleon_policy_release(u64 lease, struct chameleon_capacity *capacity);
int chameleon_policy_snapshot(u64 lease, struct chameleon_capacity *capacity);
int chameleon_policy_allow_reclaim(u64 lease, bool allowed,
		struct chameleon_capacity *capacity);
int chameleon_policy_reclaim_free(u64 lease, u64 epoch, u64 requested_bytes,
		u64 *completed_bytes, struct chameleon_capacity *capacity);
int chameleon_policy_return_free(u64 lease, u64 epoch, u64 requested_bytes,
		u64 *completed_bytes, struct chameleon_capacity *capacity);
#else
static inline int chameleon_transport_request(u16 op, u64 batch,
		struct ll_chameleon_range *ranges, unsigned int nr)
{
	return -EOPNOTSUPP;
}
static inline bool chameleon_transport_available(void) { return false; }
static inline bool chameleon_data_available(void) { return false; }
static inline bool chameleon_policy_available(void) { return false; }
static inline int chameleon_policy_acquire(u64 *l, struct chameleon_capacity *c) { return -EOPNOTSUPP; }
static inline int chameleon_policy_release(u64 l, struct chameleon_capacity *c) { return -EOPNOTSUPP; }
static inline int chameleon_policy_snapshot(u64 l, struct chameleon_capacity *c) { return -EOPNOTSUPP; }
static inline int chameleon_policy_allow_reclaim(u64 l, bool a, struct chameleon_capacity *c) { return -EOPNOTSUPP; }
static inline int chameleon_policy_reclaim_free(u64 l, u64 e, u64 b, u64 *done,
		struct chameleon_capacity *c) { return -EOPNOTSUPP; }
static inline int chameleon_policy_return_free(u64 l, u64 e, u64 b, u64 *done,
		struct chameleon_capacity *c) { return -EOPNOTSUPP; }
#endif
#endif
