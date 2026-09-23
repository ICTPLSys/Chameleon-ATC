/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_CHAMELEON_H
#define _LINUX_CHAMELEON_H
#include <linux/types.h>
#include <linux/init.h>
struct folio;
struct lruvec;
struct mm_struct;
#define CHAMELEON_ORDERS 10
/* Owns mm_users + one LRU isolation reference and NR_ISOLATED_ANON charge.
 * Putback consumes all three and the batch link. A successful C3 transfer
 * must explicitly discharge/transfer the isolation accounting exactly once. */
struct chameleon_candidate {
	struct list_head batch;
	struct mm_struct *mm;
	struct folio *folio;
	unsigned long address, pfn;
	unsigned int order;
	u64 token;
	pid_t pid;
};
#ifdef CONFIG_CHAMELEON
void __init chameleon_early_init(void);
/* CPU-local PEBS drain, including NMI context; diagnostic accounting only. */
void chameleon_pebs_reloaded(u64 raw);
void chameleon_free_pages(unsigned long pfn, unsigned int order);
int chameleon_read_counters(unsigned long pfn, unsigned long nr, u16 *out);
u16 chameleon_hot_threshold(void);
/* Process context: begin may sleep and must precede PTL acquisition. Caller
 * owns an inaccessible folio without external pins. Keep the guard across
 * PTE revalidation and publication, then end after dropping PTL. The hooks
 * themselves do not sleep. A NULL live bitmap means the entire range. */
void chameleon_lifecycle_begin(void);
void chameleon_lifecycle_end(void);
void chameleon_swapout(unsigned long pfn, unsigned long nr);
void chameleon_swapin(unsigned long pfn, unsigned long nr,
		      const unsigned long *live);
int chameleon_transfer_counters(unsigned long dst, const unsigned long *src,
				unsigned long nr);
bool chameleon_get_ptw_snapshot(u64 *pending, u64 *completed);
/* Process context, no mmap/folio/PTL/LRU locks; unchanged capacity is a no-op. */
int chameleon_set_capacity(u64 local_bytes, u64 total_bytes);
int chameleon_collapse_anon(struct mm_struct *mm, unsigned long address,
			   unsigned int order);
#ifdef CONFIG_CHAMELEON_TEST
void chameleon_collapse_fail_next(unsigned int phase);
#endif
void chameleon_lru_add(struct lruvec *lruvec, struct folio *folio, bool tail);
void chameleon_lru_del(struct lruvec *lruvec, struct folio *folio);
void chameleon_lru_add_split(struct lruvec *lruvec, struct folio *folio);
int chameleon_select_batch(unsigned int max_folios, struct list_head *batch);
/* Caller owns live mm_users, holds no mmap/folio/PTL/LRU locks. Selection is
 * constrained to this mm/range without modifying the debug target filter. */
int chameleon_select_batch_mm(unsigned int max_folios, struct mm_struct *mm,
		unsigned long start, unsigned long end, struct list_head *batch);
void chameleon_candidate_putback(struct chameleon_candidate *candidate);
#else
static inline void chameleon_early_init(void) { }
static inline void chameleon_pebs_reloaded(u64 raw) { }
static inline void chameleon_free_pages(unsigned long pfn, unsigned int order) { }
static inline void chameleon_lru_add(struct lruvec *lruvec,
				    struct folio *folio, bool tail) { }
static inline void chameleon_lru_del(struct lruvec *lruvec, struct folio *folio) { }
static inline void chameleon_lru_add_split(struct lruvec *lruvec, struct folio *folio) { }
#endif
#endif
