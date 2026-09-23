#include "llfree_states.h"

#include "llfree.h"
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/error-report.h"

#include "llfree_platform.h"
#include "utils.h"

#define LL_BITS 2u

enum ll_reclaim {
    ll_state_installed,
    ll_state_soft,
    ll_state_hard,
    ll_state_len,
};

/// each entry contains the states for all huge frames of a tree
typedef uint16_t ll_reclaim_entry;

struct ll_reclaim_states {
    ll_reclaim_entry *trees;
    size_t frames;
    size_t last_tree;
};

_Static_assert(sizeof(uint16_t) * 8 / LL_BITS == LLFREE_TREE_CHILDREN,
               "invalid tree size");

_Static_assert(sizeof(uint8_t) * 8 == LLFREE_TREE_CHILDREN,
               "invalid tree size");

ll_reclaim_states_t *ll_reclaim_states_create(size_t frames)
{
    // only whole huge frames!
    frames = align_down(frames, 1 << LLFREE_HUGE_ORDER);
    size_t huge = div_ceil(frames, 1 << LLFREE_HUGE_ORDER);
    size_t trees = div_ceil(huge, LLFREE_TREE_CHILDREN);

    ll_reclaim_states_t *states = g_malloc(sizeof(ll_reclaim_states_t));
    g_assert(states != NULL);
    states->frames = frames;
    // zero -> mapped
    states->trees = g_malloc0_n(trees, sizeof(ll_reclaim_entry));
    states->last_tree = 0;
    g_assert(states->trees != NULL);
    return states;
}

void ll_reclaim_states_destroy(ll_reclaim_states_t *self)
{
    g_free(self->trees);
    g_free(self);
}

static size_t ll_trees(ll_reclaim_states_t *self)
{
    return div_ceil(self->frames, LLFREE_TREE_SIZE);
}

static enum ll_reclaim state_get(uint16_t states, size_t child)
{
    g_assert(child < LLFREE_TREE_CHILDREN);
    return LL_MASK(LL_BITS) & (states >> (LL_BITS * child));
}
static uint16_t state_set(uint16_t states, size_t child, enum ll_reclaim state)
{
    uint16_t mask = ~(LL_MASK(LL_BITS) << (2 * child));
    return (states & mask) | (state << (LL_BITS * child));
}

static bool ll_state_transition(uint16_t *tree_states, size_t child,
                                enum ll_reclaim from, enum ll_reclaim to)
{
    enum ll_reclaim state = state_get(*tree_states, child);
    if (state == from) {
        *tree_states = state_set(*tree_states, child, to);
        return true;
    }
    return false;
}

static bool ll_state_transition_next(uint16_t *tree_states, size_t *child,
                                     enum ll_reclaim from, enum ll_reclaim to)
{
    for (size_t i = 0; i < LLFREE_TREE_CHILDREN; i++) {
        enum ll_reclaim state = state_get(*tree_states, i);
        if (state == from) {
            *tree_states = state_set(*tree_states, i, to);
            *child = i;
            return true;
        }
    }
    return false;
}

/// If the huge frame is in the `from` state, set it to `to`
static bool ll_transition(ll_reclaim_states_t *self, uint64_t frame,
                          enum ll_reclaim from, enum ll_reclaim to)
{
    if (frame >= self->frames)
        return false;

    size_t tree = tree_from_frame(frame);
    g_assert(tree < ll_trees(self));
    size_t child = child_from_frame(frame) % LLFREE_TREE_CHILDREN;
    return ll_state_transition(&self->trees[tree], child, from, to);
}

/// Find a suitable huge frame that has the `from` state and set it to `to`
static llfree_result_t ll_transition_next(ll_reclaim_states_t *self,
                                          enum ll_reclaim from,
                                          enum ll_reclaim to)
{
    size_t offset = self->last_tree;
    for (size_t i = 0; i < ll_trees(self); i++) {
        size_t tree = (i + offset) % ll_trees(self);
        size_t child = 0;
        if (ll_state_transition_next(&self->trees[tree], &child, from, to)) {
            self->last_tree = tree;
            return llfree_ok(frame_from_tree(tree) + frame_from_child(child),
                             false);
        }
    }
    return llfree_err(LLFREE_ERR_MEMORY);
}

bool ll_reclaim_states_is_installed(ll_reclaim_states_t *self, uint64_t frame)
{
    size_t tree = tree_from_frame(frame);
    g_assert(tree < ll_trees(self));
    size_t child = child_from_frame(frame) % LLFREE_TREE_CHILDREN;
    return state_get(self->trees[tree], child) == ll_state_installed;
}

bool ll_reclaim_states_is_hard(ll_reclaim_states_t *self, uint64_t frame)
{
    size_t tree = tree_from_frame(frame);
    g_assert(tree < ll_trees(self));
    size_t child = child_from_frame(frame) % LLFREE_TREE_CHILDREN;
    return state_get(self->trees[tree], child) == ll_state_hard;
}

bool ll_reclaim_states_is_soft(ll_reclaim_states_t *self, uint64_t frame)
{
    size_t tree = tree_from_frame(frame);
    g_assert(tree < ll_trees(self));
    size_t child = child_from_frame(frame) % LLFREE_TREE_CHILDREN;
    return state_get(self->trees[tree], child) == ll_state_soft;
}

bool ll_frame_states_hard_reclaim(ll_reclaim_states_t *self, uint64_t frame,
                                  bool already_soft)
{
    enum ll_reclaim from = already_soft ? ll_state_soft : ll_state_installed;
    return ll_transition(self, frame, from, ll_state_hard);
}

llfree_result_t ll_frame_states_return_next(ll_reclaim_states_t *self)
{
    return ll_transition_next(self, ll_state_hard, ll_state_soft);
}

bool ll_frame_states_install(ll_reclaim_states_t *self, uint64_t frame)
{
    return ll_transition(self, frame, ll_state_soft, ll_state_installed);
}

bool ll_frame_states_soft_reclaim(ll_reclaim_states_t *self, uint64_t frame)
{
    return ll_transition(self, frame, ll_state_installed, ll_state_soft);
}
