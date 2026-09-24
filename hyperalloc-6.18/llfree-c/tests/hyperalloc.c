#include "check.h"
#include "llfree_inner.h"

#include <pthread.h>
#include <sched.h>
#include <stdlib.h>

/* Model the actual guest/QEMU sharing: trees and lower are shared, locals are
 * private. The host uses a per-zone mutex for reclaim/return/install. */
struct pair {
	llfree_t guest, host;
	llfree_meta_t meta;
	uint8_t *host_local;
};

static struct pair pair_new(size_t frames, uint8_t init)
{
	struct pair p;
	llfree_meta_size_t sizes = llfree_metadata_size(1, frames);
	p.meta = (llfree_meta_t){
		.local = llfree_ext_alloc(LLFREE_CACHE_SIZE, sizes.local),
		.trees = llfree_ext_alloc(LLFREE_CACHE_SIZE, sizes.trees),
		.lower = llfree_ext_alloc(LLFREE_CACHE_SIZE, sizes.lower),
	};
	p.host_local = llfree_ext_alloc(LLFREE_CACHE_SIZE, sizes.local);
	assert(llfree_is_ok(llfree_init(&p.guest, 1, frames, init, p.meta)));
	llfree_meta_t host_meta = p.meta;
	host_meta.local = p.host_local;
	assert(llfree_is_ok(
		llfree_init(&p.host, 1, frames, LLFREE_INIT_NONE, host_meta)));
	return p;
}

static void pair_drop(struct pair *p)
{
	free(p->meta.local);
	free(p->meta.trees);
	free(p->meta.lower);
	free(p->host_local);
}

#define pair_cleanup __attribute__((cleanup(pair_drop)))

declare_test(hyperalloc_order10_reclaimed)
{
	bool success = true;
	for (unsigned at = 0; at < 2; at++) {
		for (unsigned mask = 0; mask < 4; mask++) {
			pair_cleanup struct pair p =
				pair_new(LLFREE_TREE_SIZE, LLFREE_INIT_ALLOC);
			/* Only this pair is available; get must not avoid it. */
			check(llfree_is_ok(llfree_put(
				&p.guest, 0, 0, llflags(LLFREE_MAX_ORDER))));
			for (unsigned i = 0; i < 2; i++) {
				llfree_result_t reclaimed =
					llfree_reclaim(&p.host, 0, false);
				check(llfree_is_ok(reclaimed));
				check(reclaimed.frame == i * LLFREE_CHILD_SIZE);
			}
			for (unsigned i = 0; i < 2; i++) {
				if (!(mask & (1u << i)))
					check(llfree_is_ok(llfree_install(
						&p.host,
						i * LLFREE_CHILD_SIZE)));
			}
			llfree_result_t got =
				at ? llfree_get_at(&p.guest, 0, 0,
						   llflags(LLFREE_MAX_ORDER)) :
				     llfree_get(&p.guest, 0,
						llflags(LLFREE_MAX_ORDER));
			check(llfree_is_ok(got));
			check(got.frame == 0);
			check_m(got.reclaimed == (mask != 0),
				"order 10 method=%s E-mask=%u",
				at ? "get_at" : "get", mask);
			for (unsigned i = 0; i < 2; i++) {
				uint64_t frame = i * LLFREE_CHILD_SIZE;
				check(llfree_is_reclaimed(&p.guest, frame) ==
				      !!(mask & (1u << i)));
				if (llfree_is_reclaimed(&p.host, frame))
					check(llfree_is_ok(llfree_install(
						&p.host, frame)));
				check(!llfree_is_reclaimed(&p.guest, frame));
			}
			/* An install must not free allocations or touch the next child. */
			check(llfree_free_frames(&p.guest) == 0);
			check(!llfree_is_reclaimed(&p.guest,
						   2 * LLFREE_CHILD_SIZE));
			check(llfree_free_at(&p.guest, 2 * LLFREE_CHILD_SIZE,
					     LLFREE_HUGE_ORDER) == 0);
			check(llfree_is_ok(
				llfree_put(&p.guest, 0, got.frame,
					   llflags(LLFREE_MAX_ORDER))));
			check(llfree_free_frames(&p.guest) ==
			      (1u << LLFREE_MAX_ORDER));
			llfree_validate(&p.guest);
		}
	}
	return success;
}

declare_test(hyperalloc_lifecycle_all_orders)
{
	bool success = true;
	for (unsigned order = 0; order <= LLFREE_MAX_ORDER; order++) {
		pair_cleanup struct pair p =
			pair_new(LLFREE_TREE_SIZE, LLFREE_INIT_FREE);
		llfree_result_t first = llfree_reclaim(&p.host, 0, false);
		llfree_result_t neighbor = llfree_reclaim(&p.host, 0, false);
		check(llfree_is_ok(first) && llfree_is_ok(neighbor));
		check(first.frame == 0 && neighbor.frame == LLFREE_CHILD_SIZE);
		check(!first.reclaimed && !neighbor.reclaimed);
		check(llfree_free_frames(&p.guest) == LLFREE_TREE_SIZE);
		llfree_result_t got =
			llfree_get_at(&p.guest, 0, 0, llflags(order));
		check(llfree_is_ok(got) && got.reclaimed);
		check(llfree_free_frames(&p.guest) ==
		      LLFREE_TREE_SIZE - (1u << order));
		check(llfree_is_ok(llfree_install(&p.host, got.frame)));
		check(!llfree_is_reclaimed(&p.guest, 0));
		/* Re-install is rejected by the core (QEMU handles the no-op). */
		check(llfree_install(&p.host, 0).error == LLFREE_ERR_ADDRESS);
		check(llfree_is_reclaimed(&p.guest, LLFREE_CHILD_SIZE));
		if (order == LLFREE_MAX_ORDER)
			check(llfree_is_ok(
				llfree_install(&p.host, LLFREE_CHILD_SIZE)));
		check(llfree_is_ok(
			llfree_put(&p.guest, 0, got.frame, llflags(order))));
		check(llfree_free_frames(&p.guest) == LLFREE_TREE_SIZE);
		llfree_validate(&p.guest);
	}
	return success;
}

declare_test(hyperalloc_hard_return)
{
	bool success = true;
	for (unsigned initially_soft = 0; initially_soft < 2;
	     initially_soft++) {
		pair_cleanup struct pair p =
			pair_new(LLFREE_TREE_SIZE, LLFREE_INIT_FREE);
		if (initially_soft) {
			llfree_result_t soft =
				llfree_reclaim(&p.host, 0, false);
			check(llfree_is_ok(soft) && soft.frame == 0 &&
			      !soft.reclaimed);
		}
		llfree_result_t hard = llfree_reclaim(&p.host, 0, true);
		check(llfree_is_ok(hard) && hard.frame == 0);
		check(hard.reclaimed == !!initially_soft);
		check(llfree_free_frames(&p.guest) ==
		      LLFREE_TREE_SIZE - LLFREE_CHILD_SIZE);
		check(llfree_is_reclaimed(&p.guest, hard.frame));
		check(llfree_get_at(&p.guest, 0, hard.frame, llflags(0)).error ==
		      LLFREE_ERR_MEMORY);
		check(llfree_is_ok(llfree_return(&p.host, hard.frame)));
		check(llfree_return(&p.host, hard.frame).error ==
		      LLFREE_ERR_ADDRESS);
		check(llfree_free_frames(&p.guest) == LLFREE_TREE_SIZE);
		check(llfree_is_reclaimed(&p.guest, hard.frame));
		llfree_result_t got =
			llfree_get_at(&p.guest, 0, hard.frame, llflags(0));
		check(llfree_is_ok(got) && got.reclaimed);
		check(llfree_is_ok(llfree_install(&p.host, got.frame)));
		check(llfree_is_ok(
			llfree_put(&p.guest, 0, got.frame, llflags(0))));
		check(llfree_free_frames(&p.guest) == LLFREE_TREE_SIZE);
		llfree_validate(&p.guest);
	}
	return success;
}

#define STRESS_FRAMES (16u * LLFREE_TREE_SIZE)
#define STRESS_WORKERS 4u
#define STRESS_ITERATIONS 3000u

struct stress {
	struct pair p;
	pthread_mutex_t host_lock;
	_Atomic(unsigned) ready, done, hard_count, soft_count, install_count;
	_Atomic(unsigned) *owners;
};

static void *guest_stress(void *arg)
{
	struct stress *s = arg;
	atomic_fetch_add(&s->ready, 1);
	while (atomic_load(&s->ready) != STRESS_WORKERS + 1)
		sched_yield();
	for (unsigned i = 0; i < STRESS_ITERATIONS; i++) {
		unsigned order = i % (LLFREE_MAX_ORDER + 1);
		llfree_result_t got =
			llfree_get(&s->p.guest, 0, llflags(order));
		assert(llfree_is_ok(got));
		assert(got.frame + (1u << order) <= STRESS_FRAMES);
		for (uint64_t f = got.frame; f < got.frame + (1u << order); f++)
			assert(atomic_exchange(&s->owners[f], 1) == 0);
		assert(pthread_mutex_lock(&s->host_lock) == 0);
		for (uint64_t f = align_down(got.frame, LLFREE_CHILD_SIZE);
		     f < got.frame + (1u << order); f += LLFREE_CHILD_SIZE) {
			if (llfree_is_reclaimed(&s->p.host, f)) {
				assert(got.reclaimed);
				assert(llfree_is_ok(
					llfree_install(&s->p.host, f)));
				atomic_fetch_add(&s->install_count, 1);
			}
		}
		assert(pthread_mutex_unlock(&s->host_lock) == 0);
		for (uint64_t f = got.frame; f < got.frame + (1u << order); f++)
			assert(atomic_exchange(&s->owners[f], 0) == 1);
		assert(llfree_is_ok(
			llfree_put(&s->p.guest, 0, got.frame, llflags(order))));
	}
	atomic_fetch_add(&s->done, 1);
	return NULL;
}

static void *host_stress(void *arg)
{
	struct stress *s = arg;
	atomic_fetch_add(&s->ready, 1);
	while (atomic_load(&s->ready) != STRESS_WORKERS + 1)
		sched_yield();
	/* Bound host work even when an instrumented mutex favors its owner. */
	for (unsigned iteration = 0; iteration < STRESS_ITERATIONS * 4;
	     iteration++) {
		bool hard = (iteration % 2) == 0;
		assert(pthread_mutex_lock(&s->host_lock) == 0);
		llfree_result_t reclaimed = llfree_reclaim(&s->p.host, 0, hard);
		if (llfree_is_ok(reclaimed)) {
			if (hard) {
				for (uint64_t f = reclaimed.frame;
				     f < reclaimed.frame + LLFREE_CHILD_SIZE;
				     f++)
					assert(atomic_load(&s->owners[f]) == 0);
				assert(llfree_is_ok(llfree_return(
					&s->p.host, reclaimed.frame)));
				atomic_fetch_add(&s->hard_count, 1);
			} else {
				atomic_fetch_add(&s->soft_count, 1);
			}
		} else {
			assert(reclaimed.error == LLFREE_ERR_MEMORY);
		}
		assert(pthread_mutex_unlock(&s->host_lock) == 0);
		sched_yield();
	}
	return NULL;
}

declare_test(hyperalloc_concurrent_reclaim_alloc)
{
	bool success = true;
	struct stress s = { .p = pair_new(STRESS_FRAMES, LLFREE_INIT_FREE) };
	s.owners = calloc(STRESS_FRAMES, sizeof(*s.owners));
	check(s.owners != NULL);
	check(pthread_mutex_init(&s.host_lock, NULL) == 0);
	/* Every first allocation requires install, regardless of scheduling. */
	while (llfree_is_ok(llfree_reclaim(&s.p.host, 0, false)))
		;
	pthread_t guests[STRESS_WORKERS], host;
	for (unsigned i = 0; i < STRESS_WORKERS; i++)
		check(pthread_create(&guests[i], NULL, guest_stress, &s) == 0);
	check(pthread_create(&host, NULL, host_stress, &s) == 0);
	for (unsigned i = 0; i < STRESS_WORKERS; i++)
		check(pthread_join(guests[i], NULL) == 0);
	check(pthread_join(host, NULL) == 0);
	check(llfree_free_frames(&s.p.guest) == STRESS_FRAMES);
	for (unsigned f = 0; f < STRESS_FRAMES; f++)
		check(atomic_load(&s.owners[f]) == 0);
	check(atomic_load(&s.hard_count) > 0);
	check(atomic_load(&s.install_count) > 0);
	printf("\tconcurrent transitions: hard=%u soft=%u install=%u\n",
	       atomic_load(&s.hard_count), atomic_load(&s.soft_count),
	       atomic_load(&s.install_count));
	llfree_validate(&s.p.guest);
	check(pthread_mutex_destroy(&s.host_lock) == 0);
	free(s.owners);
	pair_drop(&s.p);
	return success;
}
