// SPDX-License-Identifier: GPL-2.0-only
/* Actual PSI, virtio capacity, Shadow and backing lifecycle integration. */
#define CHAMELEON_TEST_STAGE "C5"
#define CHAMELEON_CONTROL_MAIN c4_reference_main
#include "chameleon_control.c"

#define POLICY "/sys/kernel/debug/chameleon_policy/"
#define PSI_LOAD "/sys/kernel/debug/chameleon_psi_load/"
#define RAM_BYTES test_ram_bytes()

static uint64_t policy_value(const char *name)
{
    char *text = read_text(POLICY "stats");
    uint64_t value = field(text, name);
    free(text);
    return value;
}

static int policy_try(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    int ret = command_v(POLICY "control", format, ap);
    va_end(ap);
    return ret;
}

static void policy_wait(const char *name, uint64_t value, bool exact)
{
    for (unsigned attempt = 0; attempt < 1500; attempt++) {
        uint64_t current = policy_value(name);
        if (exact ? current == value : current >= value)
            return;
        usleep(10000);
    }
    char *text = read_text(POLICY "stats");
    fprintf(stderr, "C5_WAIT_COUNTER name=%s expected=%" PRIu64 " exact=%u\n%s", name, value, exact, text);
    free(text);
    check(false, "bounded wait for a real policy action completes");
}

static void checkpoint(const char *name)
{
    char *text = read_text(POLICY "stats");
    printf("C5_STATS_BEGIN %s\n%sC5_STATS_END %s\n", name, text, name);
    free(text);
    printf("C5_WAIT %s\n", name);
    fflush(stdout);
    char answer[16];
    check(fgets(answer, sizeof(answer), stdin) && !strcmp(answer, "go\n"),
          "runner observes the actual checkpoint before allowing progress");
}

static void checkpoint_named(const char *prefix, const char *suffix)
{
    char name[80];
    snprintf(name, sizeof(name), "%s_%s", prefix, suffix);
    checkpoint(name);
}

static void configure(unsigned period_us, bool full, unsigned free_pages,
                      unsigned cold_folios, bool discard)
{
    command(POLICY "control", "disable\n");
    command(POLICY "control", "clear_target\n");
    command(POLICY "control", "set epoch_us %u\n", period_us);
    command(POLICY "control", "set threshold_ppm 10000\n");
    command(POLICY "control", "set psi_full %u\n", full);
    command(POLICY "control", "set free_pages %u\n", free_pages);
    command(POLICY "control", "set cold_folios %u\n", cold_folios);
    command(POLICY "control", "set minimum_local_bytes %llu\n", RAM_BYTES - (64ULL << 20));
    command(POLICY "control", "set discard_test %u\n", discard);
}

static void set_policy_target(pid_t pid, unsigned char *area, size_t bytes)
{
    command(POLICY "control", "target %d 0x%lx %zu\n", pid, (unsigned long)area, bytes);
}

static void verify_capacity_feedback(uint64_t local)
{
    char *text = read_text(TRACK "stats");
    check(field(text, "total_bytes") == RAM_BYTES && field(text, "local_bytes") == local,
          "tracker receives the actual completed Host capacity");
    check(field(text, "sample_period") == (4096 * local / RAM_BYTES < 512 ?
                                          512 : 4096 * local / RAM_BYTES) &&
          field(text, "cooling_samples") == local / PAGE,
          "adaptive sampling and cooling consume Mlocal without a manual capacity write");
    free(text);
}

static uint64_t proc_psi(bool full)
{
    char *text = read_text("/proc/pressure/memory");
    char *line = strstr(text, full ? "full " : "some ");
    check(line != NULL, "native proc PSI exposes the selected memory stall class");
    char *value = strstr(line, "total=");
    check(value != NULL, "native proc PSI cumulative total exists");
    uint64_t total = strtoull(value + 6, NULL, 10);
    free(text);
    return total;
}

static void free_capacity(bool full)
{
    const char *prefix = full ? "free_full" : "free_some";
    phase = prefix;
    configure(1000, full, 8192, 0, false);
    uint64_t reclaimed = policy_value("free_reclaimed_bytes");
    uint64_t returned = policy_value("free_returned_bytes");
    command(POLICY "control", "enable\n");
    policy_wait("local_bytes", RAM_BYTES - (64ULL << 20), true);
    check(policy_value("free_reclaimed_bytes") == reclaimed + (64ULL << 20),
          "low PSI reclaims real free capacity and stops at the configured floor");
    check(policy_try("set epoch_us 2000\n") == -EBUSY,
          "active controller rejects parameter changes");
    verify_capacity_feedback(RAM_BYTES - (64ULL << 20));
    checkpoint_named(prefix, "low");

    uint64_t native_before = proc_psi(full);
    uint64_t high = policy_value("high_epochs");
    command(PSI_LOAD "control", "start\n");
    policy_wait("high_epochs", high + 3, false);
    policy_wait("local_bytes", RAM_BYTES, true);
    usleep(50000);
    check(proc_psi(full) > native_before + 10000,
          "pressure increase is independently visible in native /proc PSI totals");
    check(policy_value("free_returned_bytes") == returned + (64ULL << 20),
          "high PSI actively returns the reclaimed HyperAlloc capacity");
    check(policy_value("free_reclaimed_bytes") == reclaimed + (64ULL << 20),
          "sustained high PSI cannot issue another free reclaim");
    verify_capacity_feedback(RAM_BYTES);
    checkpoint_named(prefix, "high");
    command(PSI_LOAD "control", "stop\n");
    policy_wait("local_bytes", RAM_BYTES - (64ULL << 20), true);
    checkpoint_named(prefix, "again");
    command(POLICY "control", "disable\n");
    check(!policy_value("enabled") && !policy_value("lease_active") &&
          policy_value("local_bytes") == RAM_BYTES,
          "disable returns owned capacity and releases exclusive control");
    uint64_t epochs = policy_value("epochs");
    usleep(30000);
    check(policy_value("epochs") == epochs, "disabled timer/work cannot issue later actions");
    verify_capacity_feedback(RAM_BYTES);
    checkpoint_named(prefix, "off");
    printf("PASS C5 pressure metric=%s real_native_totals reclaim_floor return resume disable feedback\n",
           full ? "full" : "some");
}

static struct entry selected_entry(unsigned char *address, uint64_t frame)
{
    struct entry objects[MAX_OBJECTS];
    unsigned count = entries(objects);
    for (unsigned i = 0; i < count; i++)
        if (objects[i].pfn == frame && objects[i].address == (uintptr_t)address)
            return objects[i];
    check(false, "find the actual policy-owned Shadow object by original PFN and VA");
    return (struct entry){0};
}

static void resident_shadow(uint64_t isolated)
{
    phase = "resident Shadow pressure restoration and ownership";
    struct c4_object manual = c4_mapping(2, 201);
    manual.entry = c4_prepare(getpid(), manual.area, 2, manual.frames[0], isolated);
    struct c4_object obj = c4_mapping(4, 202);
    configure(10000, false, 0, 1, false);
    set_policy_target(getpid(), obj.area, 16 * PAGE);
    uint64_t prepared = policy_value("shadow_prepared_objects");
    uint64_t restored = policy_value("shadow_restored_pages");
    command(POLICY "control", "enable\n");
    policy_wait("shadow_prepared_objects", prepared + 1, false);
    obj.entry = selected_entry(obj.area, obj.frames[0]);
    nonpresent(pagemap_fd, obj.area, 16, obj.frames[0]);
    check(!c4_state(obj.entry.token).authorized && !c4_state(obj.entry.token).registered,
          "ordinary automatic cold selection does not authorize data loss");
    checkpoint("shadow_low");
    command(PSI_LOAD "control", "start\n");
    policy_wait("shadow_restored_pages", restored + 16, false);
    command(SHADOW "control", "drain\n");
    same_pfns(obj.area, 512, obj.frames);
    content(obj.area, PMD, 0, obj.salt);
    nonpresent(pagemap_fd, manual.area, 4, manual.frames[0]);
    check(c4_state(manual.entry.token).found && !c4_state(obj.entry.token).found,
          "high pressure restores only this policy owner, not manually prepared objects");
    checkpoint("shadow_high");
    command(PSI_LOAD "control", "stop\n");
    policy_wait("shadow_prepared_objects", prepared + 2, false);
    command(POLICY "control", "disable\n");
    same_pfns(obj.area, 512, obj.frames);
    content(obj.area, PMD, 0, obj.salt);
    check(c4_state(manual.entry.token).found, "disable preserves unrelated manual ownership");
    command(SHADOW "control", "restore %" PRIu64 "\n", manual.entry.token);
    c4_unmap(&manual);
    c4_unmap(&obj);
    idle(isolated, 1);
    checkpoint("shadow_off");
    puts("PASS C5 shadow automatic_prepare pressure_fast_restore original_data owner_isolation");
}

static void token_mapping(unsigned char *address, unsigned pages, uint64_t token)
{
    unsigned char resident[512];
    check(!mincore(address, pages * PAGE, resident), "inspect actual reclaimed user mapping");
    for (unsigned i = 0; i < pages; i++) {
        uint64_t pte = pagemap(pagemap_fd, address + i * PAGE);
        check(!(resident[i] & 1) && !(pte >> 63) && (pte & (1ULL << 62)) &&
              ((pte & PFN_MASK) >> 5) == token,
              "actual nonresident PTE identifies its policy-owned token");
    }
    c4_bus(address, false);
    c4_bus(address + pages * PAGE - 1, true);
}

static void discard_policy(uint64_t isolated, const char *mode)
{
    phase = mode;
    struct c4_object obj = c4_mapping(4, 203);
    configure(10000, false, 0, 2, true);
    set_policy_target(getpid(), obj.area, 32 * PAGE);
    uint64_t prepared = policy_value("shadow_prepared_objects");
    uint64_t installed = policy_value("discard_installed_pages");
    uint64_t failures = c4_counter("range_install_failure");
    checkpoint_named(mode, "ready");
    command(POLICY "control", "enable\n");
    policy_wait("shadow_prepared_objects", prepared + 2, false);
    struct entry object[2];
    for (unsigned i = 0; i < 2; i++) {
        object[i] = selected_entry(obj.area + i * 16 * PAGE, obj.frames[i * 16]);
        c4_wait_finalized(object[i].token, false);
        token_mapping(obj.area + i * 16 * PAGE, 16, object[i].token);
        printf("C5_OBJECT mode=%s token=%" PRIu64 " pfn=%" PRIu64 " pages=16 order=4\n",
               mode, object[i].token, object[i].pfn);
    }
    check(residency(obj.area).rss == (512 - 32) * 4,
          "real policy finalization removes exactly the source RSS");
    content(obj.area + 32 * PAGE, PMD - 32 * PAGE, 32 * PAGE, obj.salt);
    same_pfns(obj.area + 32 * PAGE, 512 - 32, obj.frames + 32);
    checkpoint_named(mode, "retired");
    command(PSI_LOAD "control", "start\n");
    policy_wait("discard_installed_pages", installed + 32, false);
    check(c4_counter("range_install_failure") > failures,
          "automatic high-pressure install retries an injected real Host failure");
    check(!c4_counter("reservation_pages") && !c4_counter("host_reclaimed_pages"),
          "pressure-driven install releases every missing-backing reservation");
    for (unsigned i = 0; i < 2; i++)
        token_mapping(obj.area + i * 16 * PAGE, 16, object[i].token);
    content(obj.area + 32 * PAGE, PMD - 32 * PAGE, 32 * PAGE, obj.salt);
    checkpoint_named(mode, "restored");
    command(POLICY "control", "disable\n");
    command(PSI_LOAD "control", "stop\n");
    c4_unmap(&obj);
    idle(isolated, 1);
    checkpoint_named(mode, "off");
    printf("PASS C5 discard mode=%s automatic_ready actual_backing high_pressure_install failure_retry\n", mode);
}

static void owner_exit_policy(uint64_t isolated)
{
    phase = "target process exits during automatic control";
    struct child_owner child = child_owner(4, 204);
    configure(10000, false, 0, 1, false);
    set_policy_target(child.pid, child.area, 16 * PAGE);
    uint64_t prepared = policy_value("shadow_prepared_objects");
    command(POLICY "control", "enable\n");
    policy_wait("shadow_prepared_objects", prepared + 1, false);
    checkpoint("exit_low");
    child_finish(&child, 'X');
    command(POLICY "control", "disable\n");
    command(POLICY "control", "clear_target\n");
    idle(isolated, 1);
    checkpoint("exit_off");
    puts("PASS C5 lifecycle target_exit disable no_mm_users_or_shadow_leak");
}

static void repeated_ownership(uint64_t isolated)
{
    phase = "repeated policy ownership and completed token forget";
    checkpoint("cycles_ready");
    for (unsigned round = 0; round < 32; round++) {
        struct c4_object obj = c4_mapping(2, 205 + round);
        configure(1000, false, 0, 1, true);
        set_policy_target(getpid(), obj.area, 4 * PAGE);
        uint64_t prepared = policy_value("shadow_prepared_objects");
        command(POLICY "control", "enable\n");
        policy_wait("shadow_prepared_objects", prepared + 1, false);
        obj.entry = selected_entry(obj.area, obj.frames[0]);
        c4_wait_finalized(obj.entry.token, false);
        token_mapping(obj.area, 4, obj.entry.token);
        command(POLICY "control", "disable\n");
        check(!policy_value("lease_active") && !c4_counter("reservation_pages"),
              "each disable completes install and relinquishes the lease");
        token_mapping(obj.area, 4, obj.entry.token);
        c4_unmap(&obj);
        idle(isolated, 1);
    }
    checkpoint("cycles_done");
    puts("PASS C5 repeated rounds=32 real_discard_install owner_release bounded_registry");
}

static void failed_disable(uint64_t isolated)
{
    phase = "failed disable retains the only owner able to finish restoration";
    struct c4_object obj = c4_mapping(2, 240);
    configure(10000, false, 0, 1, true);
    set_policy_target(getpid(), obj.area, 4 * PAGE);
    uint64_t prepared = policy_value("shadow_prepared_objects");
    checkpoint("disable_fail_ready");
    command(POLICY "control", "enable\n");
    policy_wait("shadow_prepared_objects", prepared + 1, false);
    obj.entry = selected_entry(obj.area, obj.frames[0]);
    c4_wait_finalized(obj.entry.token, false);
    checkpoint("disable_fail_retired");
    check(policy_try("disable\n") == -ENOMEM,
          "disable propagates an actual install failure");
    check(!policy_value("enabled") && policy_value("lease_active") &&
          c4_counter("reservation_pages") == 4 && c4_counter("host_reclaimed_pages") == 4,
          "failed disable stops new work but retains its lease and physical reservation");
    check(policy_try("enable\n") == -EBUSY && policy_try("clear_target\n") == -EBUSY,
          "a new owner cannot orphan a failed-disable reservation");
    token_mapping(obj.area, 4, obj.entry.token);
    checkpoint("disable_failed");
    command(POLICY "control", "disable\n");
    check(!policy_value("lease_active") && !c4_counter("reservation_pages"),
          "retry installs backing before releasing the retained lease");
    c4_unmap(&obj);
    idle(isolated, 1);
    checkpoint("disable_retried");
    puts("PASS C5 disable_failure retained_lease no_orphan retry_install_release");
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc == 2 && !strcmp(argv[1], "--expect-unavailable")) {
        check(policy_try("enable\n") == -EOPNOTSUPP &&
              !policy_value("enabled") && !policy_value("lease_active"),
              "missing negotiated policy capability rejects enable without side effects");
        puts("PASS CHAMELEON_C5_UNAVAILABLE errno=EOPNOTSUPP");
        return 0;
    }
    check(argc == 1, "known policy-test arguments");
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "privileged physical mapping inspection");
    struct sigaction action = {.sa_handler = permission_handler};
    sigemptyset(&action.sa_mask);
    check(!sigaction(SIGSEGV, &action, NULL) && !sigaction(SIGBUS, &action, NULL),
          "install actual token fault probes");
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
    check(!policy_value("enabled") && !policy_value("lease_active"), "policy is disabled by default");
    check(policy_try("set threshold_ppm 1000001\n") == -EINVAL &&
          policy_try("set epoch_us 0\n") == -EINVAL,
          "invalid policy configuration is rejected");
    uint64_t isolated = isolated_anon();
    idle(isolated, 1);
    checkpoint("off");
    free_capacity(false);
    free_capacity(true);
    resident_shadow(isolated);
    discard_policy(isolated, "deferred");
    discard_policy(isolated, "immediate");
    owner_exit_policy(isolated);
    failed_disable(isolated);
    repeated_ownership(isolated);
    command(POLICY "control", "clear_target\n");
    idle(isolated, 1);
    char *text = read_text(POLICY "stats");
    printf("FINAL_C5_POLICY_STATS\n%s", text);
    free(text);
    close(flags_fd);
    close(pagemap_fd);
    printf("PASS CHAMELEON_C5 checks=%u\n", checks);
    return 0;
}
