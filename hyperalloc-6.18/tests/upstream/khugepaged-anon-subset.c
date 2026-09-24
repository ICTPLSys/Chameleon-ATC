// SPDX-License-Identifier: GPL-2.0
/* Select original Linux 6.18 test functions for a no-swap initramfs.
 * This is an explicit subset, not the complete upstream khugepaged suite.
 * Run inside the test VM: this program changes/restores global THP sysfs. */
#define main unused_upstream_main
#include "../../linux/tools/testing/selftests/mm/khugepaged.c"
#undef main

int main(int argc, char **argv)
{
    struct collapse_context *context = &__madvise_context;
    if (argc != 2 || (strcmp(argv[1], "0") && strcmp(argv[1], "4"))) {
        fprintf(stderr, "usage: %s {0|4} (anonymous fault order)\n", argv[0]);
        return 2;
    }
    anon_order = atoi(argv[1]);
    setbuf(stdout, NULL);
    if (!thp_available()) {
        fprintf(stderr, "FAIL required THP sysfs is unavailable\n");
        return 1;
    }
    page_size = getpagesize();
    hpage_pmd_size = read_pmd_pagesize();
    if (!hpage_pmd_size || page_size != 4096) {
        fprintf(stderr, "FAIL required 4KiB base / PMD THP sizing\n");
        return 1;
    }
    hpage_pmd_nr = hpage_pmd_size / page_size;
    unsigned int pmd_order = __builtin_ctz(hpage_pmd_nr);
    if (anon_order && !(thp_supported_orders() & (1UL << anon_order))) {
        fprintf(stderr, "FAIL requested anonymous THP order is unsupported\n");
        return 1;
    }
    struct thp_settings defaults = {
        .thp_enabled = THP_MADVISE,
        .thp_defrag = THP_DEFRAG_ALWAYS,
        .shmem_enabled = SHMEM_ADVISE,
        .use_zero_page = 0,
        .khugepaged = {
            .defrag = 1,
            .alloc_sleep_millisecs = 10,
            .scan_sleep_millisecs = 10,
            .max_ptes_none = hpage_pmd_nr - 1,
            .max_ptes_swap = hpage_pmd_nr / 8,
            .max_ptes_shared = hpage_pmd_nr / 2,
            .pages_to_scan = hpage_pmd_nr * 8,
        },
    };
    defaults.hugepages[pmd_order].enabled = THP_INHERIT;
    defaults.hugepages[anon_order].enabled = THP_ALWAYS;
    defaults.shmem_hugepages[pmd_order].enabled = SHMEM_INHERIT;
    defaults.shmem_hugepages[anon_order].enabled = SHMEM_ALWAYS;
    save_settings();
    thp_push_settings(&defaults);
    alloc_at_fault();

    unsigned int completed = 0;
#define RUN_CASE(name) do { \
        printf("Run upstream case: " #name " (madvise:anon order=%d)\n", anon_order); \
        name(context, &__anon_ops); \
        completed++; \
    } while (0)
    RUN_CASE(collapse_full);
    RUN_CASE(collapse_empty);
    RUN_CASE(collapse_single_pte_entry);
    RUN_CASE(collapse_max_ptes_none);
    RUN_CASE(collapse_single_pte_entry_compound);
    RUN_CASE(collapse_full_of_compound);
    RUN_CASE(collapse_fork);
    RUN_CASE(collapse_fork_compound);
    RUN_CASE(collapse_max_ptes_shared);
    RUN_CASE(madvise_collapse_existing_thps);
#undef RUN_CASE
    /* Restore before emitting PASS: restoration is part of successful exit. */
    restore_settings_atexit();
    if (exit_status) {
        fprintf(stderr, "FAIL collapse subset failures=%d\n", exit_status);
        return 1;
    }
    printf("PASS upstream-collapse-subset order=%d cases=%u plus_alloc_at_fault\n",
           anon_order, completed);
    return 0;
}
