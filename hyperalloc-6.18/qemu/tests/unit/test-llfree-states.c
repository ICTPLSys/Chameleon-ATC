#include "qemu/osdep.h"
#include "llfree_states.h"

#define HUGE_FRAMES 512
#define TREE_FRAMES (8 * HUGE_FRAMES)

static void test_lifecycle(void)
{
    ll_reclaim_states_t *states = ll_reclaim_states_create(2 * TREE_FRAMES);
    uint64_t frame = TREE_FRAMES + HUGE_FRAMES;
    llfree_result_t res;

    g_assert_true(ll_reclaim_states_is_installed(states, frame));
    g_assert_true(ll_frame_states_soft_reclaim(states, frame));
    g_assert_false(ll_frame_states_soft_reclaim(states, frame));
    g_assert_false(ll_frame_states_hard_reclaim(states, frame, false));
    g_assert_true(ll_frame_states_hard_reclaim(states, frame, true));
    g_assert_true(ll_reclaim_states_is_hard(states, frame));
    g_assert_false(ll_frame_states_install(states, frame));

    res = ll_frame_states_return_next(states);
    g_assert_true(llfree_is_ok(res));
    g_assert_cmpuint(res.frame, ==, frame);
    g_assert_true(ll_reclaim_states_is_soft(states, frame));
    g_assert_false(llfree_is_ok(ll_frame_states_return_next(states)));
    g_assert_true(ll_frame_states_install(states, frame));
    g_assert_true(ll_reclaim_states_is_installed(states, frame));
    g_assert_false(ll_frame_states_install(states, frame));
    /* A full lifecycle must not alter the adjacent 2 MiB child. */
    g_assert_true(ll_reclaim_states_is_installed(states, frame - HUGE_FRAMES));
    g_assert_true(ll_reclaim_states_is_installed(states, frame + HUGE_FRAMES));
    ll_reclaim_states_destroy(states);
}

static void test_mixed_order10(void)
{
    ll_reclaim_states_t *states = ll_reclaim_states_create(TREE_FRAMES);
    uint64_t first = 2 * HUGE_FRAMES;
    uint64_t second = first + HUGE_FRAMES;

    /* An order-10 allocation can have only its second child reclaimed. */
    g_assert_true(ll_frame_states_soft_reclaim(states, second));
    g_assert_true(ll_reclaim_states_is_installed(states, first));
    g_assert_true(ll_reclaim_states_is_soft(states, second));
    g_assert_true(ll_frame_states_install(states, second));
    g_assert_true(ll_reclaim_states_is_installed(states, first));
    g_assert_true(ll_reclaim_states_is_installed(states, second));
    ll_reclaim_states_destroy(states);
}

static void test_partial_tail(void)
{
    ll_reclaim_states_t *states =
        ll_reclaim_states_create(TREE_FRAMES + HUGE_FRAMES + 17);
    uint64_t frame = TREE_FRAMES;
    llfree_result_t res;

    g_assert_true(ll_frame_states_hard_reclaim(states, frame, false));
    g_assert_false(ll_frame_states_hard_reclaim(states, frame + HUGE_FRAMES,
                                              false));
    res = ll_frame_states_return_next(states);
    g_assert_true(llfree_is_ok(res));
    g_assert_cmpuint(res.frame, ==, frame);
    g_assert_false(llfree_is_ok(ll_frame_states_return_next(states)));
    ll_reclaim_states_destroy(states);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/llfree/lifecycle", test_lifecycle);
    g_test_add_func("/llfree/mixed-order10", test_mixed_order10);
    g_test_add_func("/llfree/partial-tail", test_partial_tail);
    return g_test_run();
}
