// SPDX-License-Identifier: GPL-2.0-only
/* C6 mechanisms: real counters/PMU, real split folios and native LRU isolation. */
#define CHAMELEON_TEST_STAGE "C6"
#define main c2_reference_main
#include "chameleon_manager.c"
#undef main
#include <stdbool.h>

static uint64_t mode_stat(const char *directory, const char *key)
{
    char path[256];
    snprintf(path, sizeof(path), "%sstats", directory);
    char *text = read_text(path);
    uint64_t value = field(text, key);
    free(text);
    return value;
}

static void mode_name(const char *directory, const char *key, const char *value)
{
    char path[256], line[128];
    snprintf(path, sizeof(path), "%sstats", directory);
    snprintf(line, sizeof(line), "%s %s\n", key, value);
    char *text = read_text(path);
    check(strstr(text, line) != NULL, "actual configured mechanism name");
    free(text);
}

static void defaults(void)
{
    command(TRACK "control", "disable\n");
    command(TRACK "control", "sampling adaptive\n");
    command(TRACK "control", "cooling adaptive\n");
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    command(MANAGER "control", "split_mode hhh\n");
    command(MANAGER "control", "selector mixed_cost\n");
    reset_tracking();
}

static unsigned single_heat(unsigned char *area)
{
    unsigned value;
    read_counters(pfn(area), 1, &value);
    return value;
}

static void sampling_cooling(void)
{
    phase = "C6 independent adaptive and fixed sample clocks";
    defaults();
    unsigned char *area = mapping(PMD, 0, 211);
    uint64_t updates = mode_stat(TRACK, "capacity_updates");
    command(TRACK "control", "capacity %lu %lu\n", 2UL << 30, 2UL << 30);
    check(mode_stat(TRACK, "capacity_updates") == updates, "unchanged capacity is a real no-op");
    command(TRACK "control", "sampling fixed 1024\n");
    command(TRACK "control", "capacity %lu %lu\n", 64UL * 1024, 2UL << 30);
    check(mode_stat(TRACK, "sample_period") == 1024 &&
          mode_stat(TRACK, "cooling_samples") == 16, "fixed Ts and adaptive Nc are independent");
    command(TRACK "control", "cooling fixed 8\n");
    command(TRACK "control", "capacity %lu %lu\n", 2UL << 30, 2UL << 30);
    command(TRACK "control", "reset\n");
    inject(area, 7);
    check(single_heat(area) == 7 && mode_stat(TRACK, "cooling_epochs") == 0,
          "seven samples do not reach fixed eight-sample cooling");
    inject(area, 1);
    check(single_heat(area) == 4 && mode_stat(TRACK, "cooling_epochs") == 1,
          "the eighth actual injected sample halves the real counter");
    command(TRACK "control", "capacity %lu %lu\n", 256UL << 20, 2UL << 30);
    inject(area, 8);
    check(single_heat(area) == 6 && mode_stat(TRACK, "cooling_epochs") == 2 &&
          mode_stat(TRACK, "sample_period") == 1024 && mode_stat(TRACK, "cooling_samples") == 8,
          "capacity feedback does not overwrite either fixed mode");
    command(TRACK "control", "sampling adaptive\n");
    check(mode_stat(TRACK, "sample_period") == 512 && mode_stat(TRACK, "cooling_samples") == 8,
          "sampling returns to capacity adaptation while cooling stays fixed");
    command(TRACK "control", "cooling adaptive\n");
    inject(area, 8);
    check(single_heat(area) == 14 && mode_stat(TRACK, "cooling_epochs") == 2 &&
          mode_stat(TRACK, "cooling_samples") == (256UL << 20) / PAGE,
          "adaptive cooling truly stops the old fixed-period halving");
    /* Changing Nc can cross many periods. This must retain exact counter
     * arithmetic without a loop of repeated full-array cooling scans. */
    command(TRACK "control", "reset\n");
    inject(area, 255);
    command(TRACK "control", "cooling fixed 1\n");
    check(single_heat(area) == 0 && mode_stat(TRACK, "cooling_epochs") == 255 &&
          mode_stat(TRACK, "samples_since_cooling") == 0,
          "fixed Nc shrink accounts every crossed epoch with saturated halving");
    (void)heat_stats();
    release_mapping(area, PMD);
    defaults();
    puts("PASS C6 tracking independent_fixed_adaptive exact_counter_halving capacity_noop");
}

static volatile unsigned char modes_sink;
static void pebs_work(unsigned char *area, unsigned long loads)
{
    for (unsigned long i = 0; i < loads; i++) {
        volatile unsigned char *address = area + (i & 511) * PAGE + 173;
        asm volatile("clflush (%0); mfence" :: "r"(address) : "memory");
        modes_sink ^= *address;
    }
}

static uint64_t sampled_region_heat(unsigned char *area)
{
    uint64_t frames[512], sum = 0;
    unsigned values[512];
    snapshot_pfns(area, 512, frames);
    mapped_counters(frames, 512, values);
    for (unsigned i = 0; i < 512; i++)
        sum += values[i];
    return sum;
}

static void hardware_modes(void)
{
    phase = "C6 real PEBS period changes on existing events";
    defaults();
    unsigned char *area = mapping(PMD, 4, 212);
    command(TRACK "control", "cooling fixed 1073741824\n");
    command(TRACK "control", "capacity %lu %lu\n", 256UL << 20, 2UL << 30);
    cpu_set_t old, one;
    check(!sched_getaffinity(0, sizeof(old), &old), "save CPU affinity");
    CPU_ZERO(&one); CPU_SET(0, &one);
    check(!sched_setaffinity(0, sizeof(one), &one), "bind precise load workload to CPU0");
    command(TRACK "control", "reset\n");
    command(TRACK "control", "enable\n");
    check(mode_stat(TRACK, "sampling_events") > 0 && !mode_stat(TRACK, "period_mismatches"),
          "every real sampling event is programmed with adaptive Ts");
    pebs_work(area, 4UL << 20);
    command(TRACK "control", "drain\n");
    uint64_t first = mode_stat(TRACK, "hardware_samples");
    uint64_t programmed = mode_stat(TRACK, "pmu_period_updates");
    uint64_t heat1 = sampled_region_heat(area);
    check(first > 16 && heat1 > 0 && !mode_stat(TRACK, "synthetic_samples"),
          "adaptive period produces real PEBS samples attributed to source PFNs");
    command(TRACK "control", "sampling fixed 2048\n");
    check(mode_stat(TRACK, "sample_period") == 2048 && !mode_stat(TRACK, "period_mismatches") &&
          mode_stat(TRACK, "pmu_period_updates") > programmed,
          "fixed mode reprograms existing event attrs and hardware period");
    programmed = mode_stat(TRACK, "pmu_period_updates");
    command(TRACK "control", "capacity %lu %lu\n", 2UL << 30, 2UL << 30);
    check(mode_stat(TRACK, "sample_period") == 2048 &&
          mode_stat(TRACK, "pmu_period_updates") == programmed,
          "fixed sampling capacity feedback avoids pointless PMU reprogramming");
    pebs_work(area, 4UL << 20);
    command(TRACK "control", "drain\n");
    uint64_t second = mode_stat(TRACK, "hardware_samples");
    check(second > first + 16 && sampled_region_heat(area) > heat1 && !mode_stat(TRACK, "period_mismatches"),
          "fixed period continues real precise PFN heat rather than changing a label");
    command(TRACK "control", "sampling adaptive\n");
    check(mode_stat(TRACK, "sample_period") == 4096 && !mode_stat(TRACK, "period_mismatches"),
          "return to adaptive mode reprograms active events using current capacity");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "drain\n");
    check(!sched_setaffinity(0, sizeof(old), &old), "restore CPU affinity");
    printf("HARDWARE C6 adaptive_samples=%" PRIu64 " fixed_samples=%" PRIu64 " periods=512,2048,4096\n",
           first, second - first);
    release_mapping(area, PMD);
    defaults();
    puts("PASS C6 hardware real_PEBS attribution live_period_change no_event_recreation");
}

static void split_modes(void)
{
    phase = "C6 HHH mixed folios versus Memtis PMD-to-base split";
    for (unsigned mode = 0; mode < 2; mode++) {
        defaults();
        unsigned char *area = mapping(PMD, 9, 220 + mode);
        check(!madvise(area, PMD, MADV_NOHUGEPAGE), "prevent native recollapse of split fixture");
        uint64_t frames[512];
        unsigned expected[512], before[512], after[512];
        snapshot_pfns(area, 512, frames);
        inject(area, 4096);
        mapped_counters(frames, 512, before);
        target(area, PMD);
        command(MANAGER "control", "memtis 20 1\n");
        command(MANAGER "control", "split_mode %s\n", mode ? "memtis" : "hhh");
        command(MANAGER "control", "split %d 0x%lx\n", getpid(), (unsigned long)area);
        if (mode)
            memset(expected, 0, sizeof(expected));
        else
            hotspot_orders(512, 0, expected);
        verify_segments(area, 512, expected, frames);
        verify_last(512, expected, 1, 0);
        mapped_counters(frames, 512, after);
        check(!memcmp(before, after, sizeof(before)), "both real split mechanisms preserve every heat value");
        check_content(area, PMD, 220 + mode);
        release_mapping(area, PMD);
    }
    for (unsigned fixture = 0; fixture < 4; fixture++) {
        defaults();
        unsigned order = fixture == 3 ? 4 : 9;
        unsigned char *area = mapping(PMD, order, 225 + fixture);
        check(!madvise(area, PMD, MADV_NOHUGEPAGE), "keep no-split fixture stable");
        if (fixture == 1)
            for (unsigned i = 0; i < 512; i++)
                inject(area + i * PAGE, 3);
        else if (fixture == 2)
            inject(area, 10000);
        else if (fixture == 3)
            inject(area, 4096);
        target(area, PMD);
        command(MANAGER "control", "memtis 20 1\n");
        command(MANAGER "control", "split_mode memtis\n");
        command(MANAGER "control", "split %d 0x%lx\n", getpid(), (unsigned long)area);
        folio(area, order);
        char *last = read_text(MANAGER "last");
        check(!field(last, "split_changed"), "cold/uniform/extremely-hot/non-PMD sources are not split");
        free(last);
        check_content(area, PMD, 225 + fixture);
        release_mapping(area, PMD);
    }
    defaults();
    unsigned char *area = mapping(2 * PMD, 9, 230);
    check(!madvise(area, 2 * PMD, MADV_NOHUGEPAGE), "prevent quota fixture recollapse");
    for (unsigned i = 0; i < 256; i++)
        inject(area + i * PAGE, 1); /* lower skew bin */
    inject(area + PMD, 512);        /* higher skew bin */
    target(area, 2 * PMD);
    command(MANAGER "control", "split_mode memtis\n");
    command(MANAGER "control", "memtis 1 1\n");
    uint64_t splits = mode_stat(MANAGER, "memtis_splits");
    command(MANAGER "control", "epoch\n");
    folio(area, 9);
    for (unsigned i = 0; i < 512; i++)
        folio(area + PMD + i * PAGE, 0);
    check(mode_stat(MANAGER, "memtis_splits") == splits + 1,
          "one-folio budget selects the genuinely higher skew PMD first");
    command(MANAGER "control", "epoch\n");
    for (unsigned i = 0; i < 1024; i++)
        folio(area + i * PAGE, 0);
    check(mode_stat(MANAGER, "memtis_splits") == splits + 2,
          "next epoch consumes the next skew candidate without fake collapse");
    check_content(area, 2 * PMD, 230);
    release_mapping(area, 2 * PMD);
    defaults();
    puts("PASS C6 split actual_HHH_mixed Memtis_PMD_to_512_base_pages skew_priority quota heat_data_preserved");
}

static uint64_t modes_isolated_anon(void)
{
    /* A one-page isolation charge can remain in a per-CPU vmstat bucket.
     * The native stat_refresh handler synchronously folds all CPU buckets;
     * refresh both the baseline and observations before comparing them. */
    char *text = read_text("/proc/sys/vm/stat_refresh");
    free(text);
    return isolated_anon();
}

static void real_lru_modes(void)
{
    phase = "C6 actual native LRU order versus mixed-order cost selection";
    defaults();
    unsigned char *area = mapping(PMD, 9, 240);
    check(!madvise(area, PMD, MADV_NOHUGEPAGE), "prevent native fixture recollapse");
    target(area, PMD);
    inject(area, 1024);
    command(MANAGER "control", "split %d 0x%lx\n", getpid(), (unsigned long)area);
    folio(area, 0); folio(area + 4 * PAGE, 2);
    reset_tracking();
    command(MANAGER "control", "age\n");
    for (unsigned order = 0; order <= 9; order++)
        if (order != 1)
            command(MANAGER "control", "cost %u %llu\n", order,
                    order == 2 ? 0ULL : 1000000ULL << order);
    command(MANAGER "control", "batch 4 6400 128 4 10\n");
    uint64_t baseline = modes_isolated_anon();
    struct candidate held[128];
    command(MANAGER "control", "selector linux_lru\n");
    /* This CONFIG_CHAMELEON_TEST operation moves the actual folio to the
     * actual inactive-anon tail, without altering its heat counter. */
    inject(area, 4096);
    command(MANAGER "control", "lru_tail %d 0x%lx\n", getpid(), (unsigned long)(area + 4 * PAGE));
    command(MANAGER "control", "lru_tail %d 0x%lx\n", getpid(), (unsigned long)area);
    command(MANAGER "control", "age\n");
    check(!(page_flags(pfn(area)) & (1ULL << KPF_ACTIVE)), "native selector does not run PEBS PromoteAge");
    command(MANAGER "control", "select 1\n");
    check(candidates(held) == 1 && held[0].order == 0 && held[0].pfn == pfn(area),
          "native tail wins across orders even when PEBS hot and cost favors order2");
    uint64_t flags = page_flags(held[0].pfn);
    uint64_t isolated = modes_isolated_anon();
    printf("ISOLATION C6 native_pfn=%" PRIu64 " flags=0x%" PRIx64
           " baseline=%" PRIu64 " isolated=%" PRIu64 "\n",
           held[0].pfn, flags, baseline, isolated);
    check(!(flags & (1ULL << KPF_LRU)), "native mode truly removes the candidate from LRU");
    check(isolated == baseline + 1, "native mode charges exactly one isolated page");
    check(manager_try("selector mixed_cost\n") == -EBUSY, "held debug candidates reject selector changes");
    command(MANAGER "control", "putback\n");
    check(modes_isolated_anon() == baseline, "native putback balances isolation");
    command(MANAGER "control", "selector mixed_cost\n");
    command(MANAGER "control", "select 1\n");
    check(candidates(held) == 1 && held[0].order == 2 && held[0].pfn == pfn(area + 4 * PAGE),
          "mixed-cost selector instead chooses the lowest-cost cold real folio");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "selector linux_lru\n");
    target(area, PAGE);
    command(MANAGER "control", "lru_tail %d 0x%lx\n", getpid(), (unsigned long)area);
    int fd = open("/sys/kernel/debug/gup_test", O_RDWR);
    check(fd >= 0, "open real FOLL_PIN probe");
    struct pin_longterm_test pin = {.addr = (uintptr_t)area, .size = PAGE,
        .flags = PIN_LONGTERM_TEST_FLAG_USE_WRITE | PIN_LONGTERM_TEST_FLAG_USE_FAST};
    check(!ioctl(fd, PIN_LONGTERM_TEST_START, &pin), "pin native-tail source with real long-term GUP");
    command(MANAGER "control", "select 1\n");
    check(candidates(held) == 0 && modes_isolated_anon() == baseline,
          "native ordering never overrides pin or exact target ownership constraints");
    check(!ioctl(fd, PIN_LONGTERM_TEST_STOP), "release native-tail pin");
    close(fd);
    command(MANAGER "control", "select 1\n");
    check(candidates(held) == 1 && held[0].pfn == pfn(area), "unpinned exact target remains selectable");
    command(MANAGER "control", "putback\n");
    check(modes_isolated_anon() == baseline, "all native/cost candidate ownership is balanced");
    check_content(area, PMD, 240);
    release_mapping(area, PMD);
    defaults();
    puts("PASS C6 selector real_native_tail cost_difference no_PEBS_filter pin_reject exact_target balanced_refs");
}

int main(int argc, char **argv)
{
    bool without_pebs = argc == 2 && !strcmp(argv[1], "--without-pebs");
    check(argc == 1 || without_pebs, "supported C6 command line");
    setvbuf(stdout, NULL, _IOLBF, 0);
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "privileged physical folio inspection");
    mode_name(TRACK, "sampling_mode", "adaptive");
    mode_name(TRACK, "cooling_mode", "adaptive");
    mode_name(MANAGER, "split_mode", "hhh");
    mode_name(MANAGER, "selector_mode", "mixed_cost");
    sampling_cooling();
    if (!without_pebs)
        hardware_modes();
    else
        puts("SKIP C6 hardware explicit --without-pebs; no PEBS hardware claim");
    split_modes();
    real_lru_modes();
    mode_name(TRACK, "sampling_mode", "adaptive");
    mode_name(TRACK, "cooling_mode", "adaptive");
    mode_name(MANAGER, "split_mode", "hhh");
    mode_name(MANAGER, "selector_mode", "mixed_cost");
    check(mode_stat(MANAGER, "split_cpu_ns") > 0 && mode_stat(MANAGER, "select_cpu_ns") > 0,
          "actual mechanism CPU time is observable");
    close(flags_fd); close(pagemap_fd);
    printf("PASS CHAMELEON_C6 checks=%u\n", checks);
    return 0;
}
