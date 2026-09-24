// SPDX-License-Identifier: GPL-2.0-only
/* A refused Host BEGIN must recover finalized data without application faults. */
#define CHAMELEON_TEST_STAGE "COMMIT_ROLLBACK"
#define CHAMELEON_HERMIT_MAIN h_reference_main
#include "chameleon_hermit.c"

static const char * const demand_keys[] = {
    "demand_faults", "demand_fault_successes", "demand_fault_failures",
    "demand_major_faults", "demand_waiters", "psi_fault_enter",
    "psi_fault_leave", "psi_fault_ns", "load_demand_attempts",
};

static void demand_snapshot(uint64_t *values)
{
    for (unsigned i = 0; i < sizeof(demand_keys) / sizeof(demand_keys[0]); i++)
        values[i] = c4_counter(demand_keys[i]);
}

static void no_demand(const uint64_t *values)
{
    for (unsigned i = 0; i < sizeof(demand_keys) / sizeof(demand_keys[0]); i++)
        check(c4_counter(demand_keys[i]) == values[i],
              "background rollback does not charge a demand fault, major event or demand PSI");
}

static void wait_counter(const char *key, uint64_t minimum)
{
    for (unsigned i = 0; i < 3000; i++) {
        if (c4_counter(key) >= minimum)
            return;
        usleep(10000);
    }
    fprintf(stderr, "ROLLBACK_WAIT key=%s minimum=%" PRIu64 " actual=%" PRIu64 "\n",
            key, minimum, c4_counter(key));
    check(false, "bounded automatic commit rollback completion");
}

static void rollback(unsigned order, bool fail_load, uint64_t isolated)
{
    phase = fail_load ? "failed background read retains data and automatically retries" :
                        "refused BEGIN restores saved data without an application fault";
    struct c4_object obj = hprepare(order, 213 + order, isolated);
    uint64_t before[sizeof(demand_keys) / sizeof(demand_keys[0])];
    demand_snapshot(before);
    uint64_t attempts = c4_counter("commit_rollback_attempts");
    uint64_t successes = c4_counter("commit_rollback_success");
    uint64_t failures = c4_counter("commit_rollback_failures");
    uint64_t background = c4_counter("load_background_attempts");
    uint64_t restored = c4_counter("data_restored_pages");
    uint64_t accepted = c4_counter("finalize_accepted");
    uint64_t loads = hvalue("load_success");
    uint64_t failed_loads = hvalue("load_failures");
    char name[64];

    if (fail_load)
        command(HERMIT "control", "fail_load 100000\n");
    snprintf(name, sizeof(name), "rollback_ready_order_%u", order);
    hcheckpoint(name, &obj); /* QMP injects exactly one BEGIN/EAGAIN refusal. */
    command(SHADOW "control", "submit %" PRIu64 "\n", obj.entry.token);
    if (fail_load) {
        wait_counter("commit_rollback_failures", failures + 1);
        check(c4_counter("commit_rollback_success") == successes &&
              hvalue("load_success") == loads, "failed read cannot count or publish restored data");
        hnonpresent(&obj);
        struct c4_state state = c4_state(obj.entry.token);
        check(state.found && state.host == 6 && state.reserved,
              "BEGIN refusal retains installed backing and the finalized Guest reservation");
        check(c4_counter("shadow_pages") == obj.pages &&
              c4_counter("owned_pages") == obj.pages &&
              c4_counter("reservation_pages") == obj.pages &&
              c4_counter("reclaimed_slots") == obj.pages &&
              !c4_counter("host_reclaimed_pages"),
              "retryable load failure neither leaks nor prematurely releases pending ownership");
        check(hvalue("live_slots") == 1 && hvalue("allocated_pages") == obj.pages,
              "failed background read keeps the full saved remote object");
        no_demand(before);
        hcheckpoint("rollback_load_failed_order_4", &obj);
        command(HERMIT "control", "fail_load 0\n");
    }
    wait_counter("commit_rollback_success", successes + 1);
    /* Only workqueue draining and diagnostics precede this check; neither
     * explicit restore nor a userspace read is allowed to hide the leak. */
    hidle(isolated);
    check(!c4_counter("owned_pages") && !c4_counter("shadow_pages"),
          "background restoration returns ownership and pending credits to zero");
    check(c4_counter("finalize_accepted") == accepted + 1 &&
          c4_counter("data_restored_pages") == restored + obj.pages,
          "one truly finalized object is completely restored by the rollback worker");
    uint64_t failure_delta = c4_counter("commit_rollback_failures") - failures;
    check(c4_counter("commit_rollback_attempts") - attempts == failure_delta + 1 &&
          c4_counter("load_background_attempts") - background == failure_delta + 1,
          "each rollback attempt performs one background load and has exactly one outcome");
    check((fail_load ? failure_delta >= 1 : failure_delta == 0) &&
          hvalue("load_failures") - failed_loads == failure_delta &&
          hvalue("load_success") == loads + 1,
          "failure injection is exercised, retried, and followed by exactly one successful data read");
    no_demand(before);
    unsigned char resident[512];
    check(!mincore(obj.area, obj.pages * PAGE, resident), "inspect automatically restored mapping");
    for (unsigned i = 0; i < obj.pages; i++)
        check(resident[i] && (pagemap(pagemap_fd, obj.area + i * PAGE) >> 63),
              "the complete mapping is present before application access");
    snprintf(name, sizeof(name), "rollback_done_order_%u", order);
    hcheckpoint(name, &obj);
    content(obj.area, PMD, 0, obj.salt);
    same_pfns(obj.area, 512, obj.frames);
    folio(obj.area, obj.order);
    check(hvalue("load_success") == loads + 1, "full-byte and original-PFN validation triggers no data read");
    no_demand(before);
    c4_unmap(&obj);
    hidle(isolated);
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "open actual PFN and folio diagnostics");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "reset\n");
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    command(MANAGER "control", "batch 128 6400 128 4 10\n");
    for (unsigned order = 0; order <= 9; order++)
        if (order != 1)
            command(MANAGER "control", "cost %u %lu\n", order, 1UL << order);
    command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
    check(hvalue("registered") == 1, "real Hermit backend is registered");
    uint64_t isolated = isolated_anon();
    hidle(isolated);
    hcheckpoint("initial", NULL);
    rollback(9, false, isolated);
    rollback(4, true, isolated);

    phase = "recovered credits and remote slots permit a later actual reclaim";
    struct c4_object obj = hprepare(9, 239, isolated);
    uint64_t loads = hvalue("load_success");
    uint64_t major = c4_counter("demand_major_faults");
    uint64_t successes = c4_counter("commit_rollback_success");
    hsubmit(&obj);
    hcheckpoint("retired_retry", &obj);
    hverify(&obj, isolated);
    check(hvalue("load_success") == loads + 1 &&
          c4_counter("demand_major_faults") == major + 1 &&
          c4_counter("commit_rollback_success") == successes,
          "successful subsequent retirement follows the normal single demand-read path");
    c4_unmap(&obj);
    hidle(isolated);
    check(!c4_counter("owned_pages"), "final reservation ownership is zero");
    hcheckpoint("finished", NULL);
    close(flags_fd);
    close(pagemap_fd);
    printf("PASS CHAMELEON_COMMIT_ROLLBACK checks=%u\n", checks);
    return 0;
}
