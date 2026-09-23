// SPDX-License-Identifier: GPL-2.0-only
/* Scalar debugfs tuning, observed through real counters, folios and policy. */
#define CHAMELEON_TEST_STAGE "TUNING"
#define main c2_reference_main
#include "chameleon_manager.c"
#undef main
#include <stdbool.h>
#define POLICY "/sys/kernel/debug/chameleon_policy/"

static uint64_t value(const char *path)
{
    char *text = read_text(path), *end;
    uint64_t result = strtoull(text, &end, 10);
    check(end != text && (*end == '\n' || !*end), "scalar debugfs numeric read");
    free(text);
    return result;
}

static uint64_t statistic(const char *path, const char *key)
{
    char *text = read_text(path);
    uint64_t result = field(text, key);
    free(text);
    return result;
}

static int try_write(const char *path, const char *text)
{
    int fd = open(path, O_WRONLY);
    check(fd >= 0, "open writable scalar");
    int result = write(fd, text, strlen(text));
    int error = errno;
    close(fd);
    return result < 0 ? -error : 0;
}

static void invalid(const char *path, const char *text)
{
    uint64_t before = value(path);
    check(try_write(path, text) < 0, "invalid scalar write rejected");
    check(value(path) == before, "rejected write preserves configuration");
}

static void put(const char *path, uint64_t number)
{
    command(path, "%" PRIu64 "\n", number);
    check(value(path) == number, "scalar write/read agrees");
}

static void tracker_tests(void)
{
    phase = "tracker scalar validation and actual counter cooling";
    check(value(TRACK "sampling_adaptive") == 1 && value(TRACK "cooling_adaptive") == 1 &&
          value(TRACK "fixed_sample_period") == 4096 && value(TRACK "hotset_target_percent") == 30,
          "tracker compiled defaults unchanged");
    invalid(TRACK "sampling_adaptive", "2\n");
    invalid(TRACK "fixed_sample_period", "511\n");
    invalid(TRACK "fixed_sample_period", "4294967296\n");
    invalid(TRACK "fixed_cooling_samples", "0\n");
    invalid(TRACK "fixed_cooling_samples", "1099511627777\n");
    invalid(TRACK "hotset_target_percent", "0\n");
    invalid(TRACK "hotset_target_percent", "101\n");
    invalid(TRACK "hotset_target_percent", "-1\n");
    invalid(TRACK "hotset_target_percent", "30 garbage\n");
    put(TRACK "fixed_sample_period", 8192);
    check(statistic(TRACK "stats", "sample_period") == 4096, "fixed fallback does not switch adaptive mode");
    put(TRACK "sampling_adaptive", 0);
    check(statistic(TRACK "stats", "sample_period") == 8192, "fixed mode applies saved value");
    put(TRACK "fixed_cooling_samples", 8);
    put(TRACK "cooling_adaptive", 0);
    unsigned char *area = mapping(PMD, 0, 71);
    reset_tracking();
    inject(area, 7);
    unsigned count;
    read_counters(pfn(area), 1, &count);
    check(count == 7, "seven samples remain before fixed cooling epoch");
    inject(area, 1);
    read_counters(pfn(area), 1, &count);
    check(count == 4 && statistic(TRACK "stats", "cooling_epochs") == 1,
          "echo cooling=8 halves actual page counter on eighth sample");
    put(TRACK "fixed_cooling_samples", 1ULL << 30);
    command(TRACK "control", "reset\n");
    inject(area, 64); inject(area + PAGE, 8);
    command(TRACK "control", "capacity %lu %lu\n", 4 * PAGE, 2UL << 30);
    put(TRACK "hotset_target_percent", 25);
    uint64_t narrow = statistic(TRACK "stats", "phi");
    put(TRACK "hotset_target_percent", 50);
    check(statistic(TRACK "stats", "phi") < narrow, "larger hot-set fraction lowers actual histogram threshold");
    put(TRACK "hotset_target_percent", 30);
    reset_tracking();
    put(TRACK "fixed_sample_period", 1024);
    command(TRACK "control", "enable\n");
    uint64_t hardware = statistic(TRACK "stats", "hardware_samples");
    volatile unsigned char sink = 0;
    for (unsigned long i = 0; i < (4UL << 20); i++) {
        volatile unsigned char *address = area + (i & 511) * PAGE + 173;
        asm volatile("clflush (%0); mfence" :: "r"(address) : "memory");
        sink ^= *address;
    }
    command(TRACK "control", "drain\n");
    check(statistic(TRACK "stats", "hardware_samples") > hardware, "actual PEBS samples received");
    uint64_t updates = statistic(TRACK "stats", "pmu_period_updates");
    put(TRACK "fixed_sample_period", 2048);
    check(statistic(TRACK "stats", "pmu_period_updates") > updates &&
          !statistic(TRACK "stats", "period_mismatches"), "live echo reprograms existing PMU events");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "drain\n");
    release_mapping(area, PMD);
    (void)sink;
    put(TRACK "sampling_adaptive", 1);
    put(TRACK "cooling_adaptive", 1);
    put(TRACK "fixed_sample_period", 4096);
    puts("PASS TUNING tracker scalar_validation actual_cooling hotset_phi live_PEBS_period");
}

static void manager_tests(void)
{
    phase = "manager cost and HHH tuning";
    check(value(MANAGER "hhh_dominance_permille") == 700 && value(MANAGER "maintenance_interval_ms") == 5000 &&
          value(MANAGER "scan_folios") == 128 && value(MANAGER "ptw_auto") == 1 &&
          !statistic(MANAGER "stats", "cost_valid"), "manager compiled defaults unchanged");
    invalid(MANAGER "hhh_dominance_permille", "499\n");
    invalid(MANAGER "hhh_dominance_permille", "1001\n");
    invalid(MANAGER "maintenance_interval_ms", "0\n");
    invalid(MANAGER "scan_folios", "129\n");
    invalid(MANAGER "batch", "0\n");
    invalid(MANAGER "Etrans", "0\n");
    invalid(MANAGER "Lptw_fallback", "0\n");
    invalid(MANAGER "selector", "2\n");
    invalid(MANAGER "split_mode", "2\n");
    put(MANAGER "batch", 32); put(MANAGER "Csync", 6400);
    put(MANAGER "Etrans", 128); put(MANAGER "Nactive", 4);
    put(MANAGER "Lptw_fallback", 10);
    check(statistic(MANAGER "stats", "cost_valid"), "scalar cost configuration activates cost model");
    for (unsigned order = 0; order <= 9; order++) {
        if (order == 1) continue;
        char path[128]; snprintf(path, sizeof(path), MANAGER "Creclaim_order%u", order);
        put(path, 1U << order);
    }
    put(MANAGER "ptw_auto", 0); put(MANAGER "Lptw_fallback", 17);
    command(MANAGER "control", "ptw\n");
    check(value(MANAGER "Lptw") == 17 && !statistic(MANAGER "stats", "ptw_measured"), "manual PTW keeps explicit latency");
    put(MANAGER "ptw_auto", 1); put(MANAGER "Lptw_fallback", 10);
    put(MANAGER "split_mode", 1); put(MANAGER "split_mode", 0);
    put(MANAGER "selector", 1); put(MANAGER "selector", 0);
    for (unsigned trial = 0; trial < 2; trial++) {
        reset_tracking();
        unsigned char *area = mapping(PMD, 9, 79);
        uint64_t frames[512]; unsigned expected[512];
        snapshot_pfns(area, 512, frames);
        for (unsigned i = 0; i < 512; i++) {
            inject(area + i * PAGE, i < 256 ? 7 : 3);
            expected[i] = trial ? 8 : 9;
        }
        put(MANAGER "hhh_dominance_permille", trial ? 600 : 700);
        target(area, PMD);
        command(MANAGER "control", "split %d 0x%lx\n", getpid(), (unsigned long)area);
        verify_segments(area, 512, expected, frames);
        check_content(area, PMD, 79);
        release_mapping(area, PMD);
    }
    put(MANAGER "hhh_dominance_permille", 700);
    unsigned char *area = mapping(PMD, 0, 85); target(area, PMD);
    put(MANAGER "scan_folios", 1);
    command(MANAGER "control", "enable\n");
    usleep(100000);
    uint64_t epochs = statistic(MANAGER "stats", "epochs");
    put(MANAGER "maintenance_interval_ms", 25);
    for (unsigned i = 0; i < 300 && statistic(MANAGER "stats", "epochs") < epochs + 2; i++) usleep(10000);
    check(statistic(MANAGER "stats", "epochs") >= epochs + 2, "shorter live maintenance interval executes real epochs");
    command(MANAGER "control", "disable\n");
    release_mapping(area, PMD);
    command(MANAGER "control", "clear_target\n");
    put(MANAGER "scan_folios", 128); put(MANAGER "maintenance_interval_ms", 5000);
    puts("PASS TUNING manager real_HHH_layout live_maintenance explicit_cost PTW_modes");
}

static void wait_policy(const char *key, uint64_t expected)
{
    for (unsigned i = 0; i < 1000; i++) {
        if (statistic(POLICY "stats", key) == expected) return;
        usleep(10000);
    }
    char *text = read_text(POLICY "stats");
    fprintf(stderr, "WAIT_TIMEOUT key=%s expected=%" PRIu64 "\n%s", key, expected, text);
    free(text);
    check(0, "policy reaches requested real capacity within timeout");
}

static void policy_tests(void)
{
    phase = "live native PSI scalar tuning";
    check(value(POLICY "epoch_us") == 1000 && value(POLICY "threshold_ppm") == 10000 &&
          value(POLICY "free_pages") == 512 && value(POLICY "cold_folios") == 8 &&
          value(POLICY "minimum_local_bytes") == 0, "policy compiled defaults unchanged");
    invalid(POLICY "epoch_us", "999\n"); invalid(POLICY "threshold_ppm", "1000001\n");
    invalid(POLICY "free_pages", "511\n"); invalid(POLICY "cold_folios", "129\n");
    invalid(POLICY "minimum_local_bytes", "1\n"); invalid(POLICY "psi_full", "2\n");
    /* Boot tracker starts from totalram_pages(), which excludes reservations.
     * This runner configures exactly 2 GiB; the lease reports full VM RAM. */
    uint64_t ram = 2ULL << 30;
    put(POLICY "cold_folios", 0); put(POLICY "free_pages", 512);
    put(POLICY "minimum_local_bytes", ram - (8ULL << 20));
    put(POLICY "threshold_ppm", 1000000);
    command(POLICY "control", "enable\n");
    wait_policy("hard_reclaimed_bytes", 8ULL << 20);
    invalid(POLICY "free_pages", "0\n");
    invalid(POLICY "minimum_local_bytes", "4503599627370496\n");
    check(try_write(POLICY "discard_test", "0\n") == -EBUSY, "online test-mode changes rejected");
    put(POLICY "epoch_us", 10000);
    put(POLICY "minimum_local_bytes", ram - (16ULL << 20));
    wait_policy("hard_reclaimed_bytes", 16ULL << 20);
    put(POLICY "psi_full", 1); put(POLICY "psi_full", 0);
    put(POLICY "threshold_ppm", 0);
    wait_policy("hard_reclaimed_bytes", 0);
    check(statistic(POLICY "stats", "free_reclaimed_bytes") >= (16ULL << 20) &&
          statistic(POLICY "stats", "free_returned_bytes") >= (16ULL << 20),
          "live threshold changes reclaim and return actual free backing");
    command(POLICY "control", "disable\n");
    check(!statistic(POLICY "stats", "lease_active") && !statistic(POLICY "stats", "action_errors"),
          "policy lease released with no action errors");
    puts("PASS TUNING policy live_epoch threshold capacity_floor actual_free_return");
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc == 2 && !strcmp(argv[1], "--policy-only")) {
        policy_tests();
    } else {
        check(argc == 1, "usage: chameleon_tuning [--policy-only]");
        pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
        flags_fd = open("/proc/kpageflags", O_RDONLY);
        check(pagemap_fd >= 0 && flags_fd >= 0, "real physical folio inspection available");
        command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
        tracker_tests(); manager_tests();
        close(pagemap_fd); close(flags_fd);
    }
    printf("PASS CHAMELEON_TUNING checks=%u\n", checks);
    return 0;
}
