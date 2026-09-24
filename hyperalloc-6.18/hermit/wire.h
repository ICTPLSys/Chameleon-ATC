/* SPDX-License-Identifier: GPL-2.0-only */
/* Hermit remoteswap's existing x86 message layout (utils.h/constants.h).
 * Integer fields retain the original native byte order. Both peers are x86.
 */
#ifndef CHAMELEON_HERMIT_WIRE_H
#define CHAMELEON_HERMIT_WIRE_H
#ifdef __KERNEL__
#include <linux/types.h>
typedef u64 rswap_u64;
typedef u32 rswap_u32;
#else
#include <stdint.h>
typedef uint64_t rswap_u64;
typedef uint32_t rswap_u32;
#endif
#define RSWAP_MAX_REGIONS 16
#define RSWAP_DONE 1
#define RSWAP_GOT_CHUNKS 2
#define RSWAP_FREE_SIZE 4
#define RSWAP_REQUEST_CHUNKS 8
#define RSWAP_QUERY 10
#define RSWAP_AVAILABLE 11
struct rswap_message {
	rswap_u64 buf[RSWAP_MAX_REGIONS];
	rswap_u64 mapped_size[RSWAP_MAX_REGIONS];
	rswap_u32 rkey[RSWAP_MAX_REGIONS];
	int mapped_chunk;
	int type;
};
#endif
