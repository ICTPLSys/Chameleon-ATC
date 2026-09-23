#pragma once

#include "llfree.h"

typedef struct ll_reclaim_states ll_reclaim_states_t;

ll_reclaim_states_t *ll_reclaim_states_create(size_t frames);
void ll_reclaim_states_destroy(ll_reclaim_states_t *self);

/// Returns whether a frame is mapped
bool ll_reclaim_states_is_installed(ll_reclaim_states_t *self, uint64_t frame);
/// Returns whether a frame is hard-reclaimed (unmapped and unavailable)
bool ll_reclaim_states_is_hard(ll_reclaim_states_t *self, uint64_t frame);
/// Returns whether a frame is soft-reclaimed (unmapped but available)
bool ll_reclaim_states_is_soft(ll_reclaim_states_t *self, uint64_t frame);

/// Mark a given frame as reclaimed
bool ll_frame_states_hard_reclaim(ll_reclaim_states_t *self, uint64_t frame,
                                  bool already_soft);
/// Search a hard-reclaimed frame and mark it soft-reclaimed
llfree_result_t ll_frame_states_return_next(ll_reclaim_states_t *self);

/// Mark an soft-reclaimed frame as installed and mapped
bool ll_frame_states_install(ll_reclaim_states_t *self, uint64_t frame);
/// Search a frame and mark it unmapped
bool ll_frame_states_soft_reclaim(ll_reclaim_states_t *self, uint64_t frame);
