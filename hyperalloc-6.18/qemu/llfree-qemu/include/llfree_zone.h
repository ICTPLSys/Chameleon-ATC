#ifndef QEMU_LLFREE_ZONE_H
#define QEMU_LLFREE_ZONE_H

#include "qemu/osdep.h"
#include "llfree_states.h"
#include "llfree.h"
#include <stdint.h>

#define LL_NODES_MAX 8

// shared with virtio-driver
typedef enum llfree_zone_type {
    LL_ZONE_DMA32,
    LL_ZONE_NORMAL,
    LL_ZONE_MOVABLE,
    LL_ZONE_LEN,
    LLFREE_ZONE_NONE = -1,
} ll_zone_type_t;

// Managed zone, containing an llfree instance
typedef struct ll_zone ll_zone_t;

// Managed numa nodes, each containing one or more zones
typedef struct ll_nodes ll_nodes_t;

ll_nodes_t *ll_nodes_create(void);
void ll_nodes_destroy(ll_nodes_t *self);

size_t ll_nodes_len(ll_nodes_t *self);
void ll_nodes_add_zone(ll_nodes_t *self, void *buffer);
ll_zone_t *ll_nodes_get_zone(ll_nodes_t *self, size_t node,
                             ll_zone_type_t zone);

uint32_t ll_zone_buffer_size(void);

llfree_result_t ll_zone_reclaim(ll_zone_t *zone, bool alloc);
llfree_result_t ll_zone_install(ll_zone_t *zone, uint64_t frame);
llfree_result_t ll_zone_return(ll_zone_t *zone, uint64_t frame);
bool ll_zone_is_evicted(ll_zone_t *zone, uint64_t frame);

int64_t ll_zone_free_huge(ll_zone_t *zone);
uint64_t ll_zone_frames(ll_zone_t *zone);
uint32_t ll_zone_node(ll_zone_t *zone);
uint32_t ll_zone_type(ll_zone_t *zone);

ll_reclaim_states_t *ll_zone_states(ll_zone_t *zone);
int64_t ll_zone_file_pages(ll_zone_t *zone);
uint64_t ll_zone_start_frame(ll_zone_t *zone);

void ll_zone_lock(ll_zone_t *zone);
void ll_zone_unlock(ll_zone_t *zone);

#endif
