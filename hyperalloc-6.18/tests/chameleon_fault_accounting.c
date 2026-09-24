// SPDX-License-Identifier: GPL-2.0-only
/* Real Hermit demand faults drive PSI; no synthetic memory-stall worker. */
#define CHAMELEON_TEST_STAGE "R2"
#define CHAMELEON_CONTROL_MAIN c4_reference_main
#include "chameleon_control.c"

#define HERMIT "/sys/kernel/debug/hermit/"
#define POLICY "/sys/kernel/debug/chameleon_policy/"

static uint64_t r2_value(const char *path, const char *key)
{
    char *text = read_text(path);
    uint64_t value = field(text, key);
    free(text);
    return value;
}

static uint64_t r2_psi(void)
{
    char *text = read_text("/proc/pressure/memory");
    char *some = strstr(text, "some "), *total = some ? strstr(some, "total=") : NULL;
    check(total != NULL, "read native memory PSI some total");
    uint64_t value = strtoull(total + 6, NULL, 10);
    free(text);
    return value;
}

static uint64_t r2_major(void)
{
    /* Fold per-CPU vmstat before comparing an actual global event count. */
    (void)isolated_anon();
    return r2_value("/proc/vmstat", "pgmajfault");
}

static long r2_task_major(void)
{
    struct rusage usage;
    check(!getrusage(RUSAGE_THREAD, &usage), "read this faulting thread's native major faults");
    return usage.ru_majflt;
}

static void r2_balanced(void)
{
    check(c4_counter("psi_fault_enter") == c4_counter("psi_fault_leave"),
          "every demand PSI section ends after success, failure and concurrent wait");
    check(c4_counter("demand_faults") == c4_counter("demand_fault_successes") +
          c4_counter("demand_fault_failures"), "all reclaimed fault calls have an outcome");
    check(c4_counter("load_attempts") == c4_counter("load_demand_attempts") +
          c4_counter("load_background_attempts"), "backend callbacks separate demand and background IO");
    check(c4_counter("demand_major_faults") == c4_counter("load_demand_attempts"),
          "one native major event per initiating demand backend read");
}

static void r2_idle(uint64_t isolated)
{
    idle(isolated, 1);
    check(!r2_value(HERMIT "stats", "live_slots") &&
          !r2_value(HERMIT "stats", "allocated_pages") &&
          !r2_value(HERMIT "stats", "inflight"), "all saved objects and RDMA operations drain");
    check(!c4_counter("reservation_pages") && !c4_counter("host_reclaimed_pages"),
          "all original PFN reservations and Host retirement drain");
    r2_balanced();
}

static void r2_checkpoint(const char *name, const struct c4_object *obj)
{
    if (obj)
        printf("R2_OBJECT phase=%s token=%" PRIu64 " pfn=%" PRIu64 " pages=%u order=%u\n",
               name, obj->entry.token, obj->frames[0], obj->pages, obj->order);
    for (unsigned i = 0; i < 3; i++) {
        const char *paths[] = {SHADOW "stats", HERMIT "stats", POLICY "stats"};
        const char *tags[] = {"SHADOW", "BACKEND", "POLICY"};
        char *text = read_text(paths[i]);
        printf("R2_%s_BEGIN %s\n%sR2_%s_END %s\n", tags[i], name, text, tags[i], name);
        free(text);
    }
    printf("R2_WAIT %s\n", name);
    fflush(stdout);
    char answer[16];
    check(fgets(answer, sizeof(answer), stdin) && !strcmp(answer, "go\n"),
          "runner observes real backing before permitting the next phase");
}

static struct c4_object r2_prepare(unsigned order, unsigned salt, uint64_t isolated)
{
    struct c4_object obj = c4_mapping(order, salt);
    obj.entry = c4_prepare(getpid(), obj.area, order, obj.frames[0], isolated);
    return obj;
}

static void r2_submit(struct c4_object *obj)
{
    command(SHADOW "control", "submit %" PRIu64 "\n", obj->entry.token);
    c4_wait_finalized(obj->entry.token, false);
    for (unsigned i = 0; i < obj->pages; i++) {
        uint64_t entry = pagemap(pagemap_fd, obj->area + i * PAGE);
        check(!(entry >> 63) && (entry & (1ULL << 62)) &&
              ((entry & PFN_MASK) >> 5) == obj->entry.token,
              "saved source has actual nonpresent reclaimed token PTEs");
    }
}

static void r2_verify(struct c4_object *obj, uint64_t isolated)
{
    content(obj->area, PMD, 0, obj->salt);
    same_pfns(obj->area, 512, obj->frames);
    folio(obj->area, obj->order);
    c4_unmap(obj);
    r2_idle(isolated);
}

static void fast_shadow(uint64_t isolated)
{
    phase = "resident Shadow fast restore has no remote read accounting";
    struct c4_object obj = r2_prepare(4, 220, isolated);
    uint64_t loads = c4_counter("load_attempts"), major = c4_counter("demand_major_faults");
    uint64_t enters = c4_counter("psi_fault_enter");
    content(obj.area, PMD, 0, obj.salt);
    check(c4_counter("load_attempts") == loads && c4_counter("demand_major_faults") == major &&
          c4_counter("psi_fault_enter") == enters, "resident fast restore is not remote IO or demand memory stall");
    r2_verify(&obj, isolated);
    puts("PASS R2 fast_shadow no_backend_IO no_demand_PSI no_remote_major");
}

static void demand_read(uint64_t isolated)
{
    phase = "real RDMA demand read is a native major fault and memory stall";
    struct c4_object obj = r2_prepare(9, 221, isolated);
    r2_submit(&obj);
    r2_checkpoint("retired_demand", &obj);
    uint64_t attempts = c4_counter("load_demand_attempts"), majors = r2_major();
    uint64_t psi = r2_psi(), stall = c4_counter("psi_fault_ns");
    uint64_t loads = r2_value(HERMIT "stats", "load_success");
    long task = r2_task_major();
    content(obj.area, PMD, 0, obj.salt);
    check(r2_task_major() == task + 1, "one successful initiating read is one native task major fault");
    check(r2_major() >= majors + 1 && c4_counter("load_demand_attempts") == attempts + 1,
          "global major event and exact demand IO counter record the read");
    check(r2_value(HERMIT "stats", "load_success") == loads + 1,
          "major accounting corresponds to one real full-folio Hermit read");
    check(c4_counter("psi_fault_ns") > stall && r2_psi() > psi,
          "actual demand RDMA wait grows native memory PSI without a stall fixture");
    r2_verify(&obj, isolated);
    r2_checkpoint("demand_done", NULL);
    puts("PASS R2 demand native_task_major global_major actual_RDMA native_memory_PSI");
}

static void background_read(uint64_t isolated)
{
    phase = "explicit background read is separate from demand PSI and major accounting";
    struct c4_object obj = r2_prepare(9, 222, isolated);
    r2_submit(&obj);
    r2_checkpoint("retired_background", &obj);
    uint64_t background = c4_counter("load_background_attempts");
    uint64_t demand = c4_counter("demand_major_faults"), enter = c4_counter("psi_fault_enter");
    long task = r2_task_major();
    command(SHADOW "control", "restore %" PRIu64 "\n", obj.entry.token);
    check(r2_task_major() == task && c4_counter("demand_major_faults") == demand &&
          c4_counter("psi_fault_enter") == enter, "background restoration cannot fabricate demand faults or PSI");
    check(c4_counter("load_background_attempts") == background + 1,
          "actual proactive backend read remains observable separately");
    r2_verify(&obj, isolated);
    r2_checkpoint("background_done", NULL);
    puts("PASS R2 background distinct_IO no_task_major no_demand_PSI");
}

static void failed_read(uint64_t isolated)
{
    phase = "failed demand read preserves native attempted-major and balanced PSI semantics";
    struct c4_object obj = r2_prepare(4, 223, isolated);
    r2_submit(&obj);
    r2_checkpoint("retired_failure", &obj);
    uint64_t faults = c4_counter("demand_fault_failures"), attempts = c4_counter("load_demand_attempts");
    uint64_t majors = r2_major();
    command(HERMIT "control", "fail_load 1\n");
    long task = r2_task_major();
    c4_bus(obj.area + 173, false);
    check(r2_task_major() == task, "SIGBUS error is not a completed task major fault");
    check(c4_counter("demand_fault_failures") == faults + 1 &&
          c4_counter("load_demand_attempts") == attempts + 1 && r2_major() >= majors + 1,
          "failed initiating read counts one attempted major IO and one failed demand fault");
    r2_balanced();
    task = r2_task_major();
    content(obj.area, PMD, 0, obj.salt);
    check(r2_task_major() == task + 1 && c4_counter("load_demand_attempts") == attempts + 2,
          "real retry performs one new demand IO and completes one task major fault");
    r2_verify(&obj, isolated);
    r2_checkpoint("failure_done", NULL);
    puts("PASS R2 failure native_attempt_vs_completed_fault SIGBUS retained_data balanced_PSI retry");
}

static void concurrent_read(uint64_t isolated)
{
    phase = "concurrent demand faults share one IO while accounting each real waiter";
    uint64_t waiters = c4_counter("demand_waiters");
    unsigned rounds;
    for (rounds = 0; rounds < 8; rounds++) {
        struct c4_object obj = r2_prepare(9, 224 + rounds, isolated);
        pthread_barrier_t barrier;
        pthread_t threads[8];
        struct fault_thread args[8];
        check(!pthread_barrier_init(&barrier, NULL, 9), "create eight-reader demand barrier");
        for (unsigned i = 0; i < 8; i++) {
            args[i] = (struct fault_thread){.barrier=&barrier, .address=obj.area + i * PAGE + 173};
            check(!pthread_create(&threads[i], NULL, fault_reader, &args[i]), "start actual concurrent readers");
        }
        r2_submit(&obj);
        if (!rounds)
            r2_checkpoint("retired_concurrent", &obj);
        uint64_t attempts = c4_counter("load_demand_attempts");
        uint64_t reads = r2_value(HERMIT "stats", "load_success");
        pthread_barrier_wait(&barrier);
        for (unsigned i = 0; i < 8; i++) {
            check(!pthread_join(threads[i], NULL), "join actual faulting reader");
            check(args[i].value == pattern(i * PAGE + 173, obj.salt), "every concurrent reader sees saved data");
        }
        check(!pthread_barrier_destroy(&barrier), "destroy finished demand barrier");
        check(c4_counter("load_demand_attempts") == attempts + 1 &&
              r2_value(HERMIT "stats", "load_success") == reads + 1,
              "eight readers generate exactly one initiating backend IO and major event");
        r2_verify(&obj, isolated);
        if (c4_counter("demand_waiters") > waiters)
            break;
    }
    check(rounds < 8, "at least one real concurrent demand waiter is exercised");
    r2_checkpoint("concurrent_done", NULL);
    puts("PASS R2 concurrent one_IO_one_major_event actual_waiters balanced_PSI");
}

static void fault_driven_policy(uint64_t isolated)
{
    phase = "actual remote fault PSI closes reclaim gate and proactively restores cold data";
    policy(9);
    unsigned char *area = aligned_map(4 * PMD);
    check(!madvise(area, 4 * PMD, MADV_HUGEPAGE), "allow four actual application huge folios");
    /* Populate three folios now. The fourth stays a genuine demand-zero
     * VMA until after a high-pressure epoch: its later first touch provides
     * new cold work after swap-in has correctly initialized old folios to
     * phi. No injected heat or manual aging makes restored data cold. */
    for (size_t i = 0; i < 3 * PMD; i++) area[i] = pattern(i, 240);
    command(TRACK "control", "drain\n");
    uint64_t frames[2048];
    snapshot(area, 1536, frames);
    for (unsigned i = 0; i < 3; i++) folio(area + i * PMD, 9);
    command(POLICY "control", "disable\n");
    command(POLICY "control", "clear_target\n");
    /* Keep the pressure/recovery epochs observable between userspace
     * snapshots; native PSI still accumulates the real read's stall time. */
    command(POLICY "control", "set epoch_us 10000\n");
    command(POLICY "control", "set threshold_ppm 1\n");
    command(POLICY "control", "set psi_full 0\n");
    command(POLICY "control", "set free_pages 0\n");
    command(POLICY "control", "set cold_folios 2\n");
    command(POLICY "control", "set minimum_local_bytes %llu\n", test_ram_bytes() - 2 * PMD);
    command(POLICY "control", "set discard_test 0\n");
    command(POLICY "control", "target %d 0x%lx %lu\n", getpid(), (unsigned long)area, 4 * PMD);
    command(POLICY "control", "enable\n");
    struct entry list[MAX_OBJECTS];
    uint64_t tokens[3] = {0};
    unsigned selected = 0, demand_index = 3;
    for (unsigned retry = 0; retry < 3000 && selected < 2; retry++) {
        unsigned count = entries(list);
        for (unsigned i = 0; i < count; i++)
            for (unsigned j = 0; j < 3; j++)
                if (list[i].pfn == frames[j * 512]) tokens[j] = list[i].token;
        selected = !!tokens[0] + !!tokens[1] + !!tokens[2];
        if (selected < 2) usleep(10000);
    }
    check(selected == 2, "policy autonomously saves two folios while preserving another cold candidate");
    for (unsigned i = 0; i < 3; i++) {
        if (!tokens[i]) continue;
        c4_wait_finalized(tokens[i], false);
        if (demand_index == 3) demand_index = i;
    }
    r2_checkpoint("retired_policy", NULL);
    uint64_t high = r2_value(POLICY "stats", "high_epochs");
    uint64_t proactive = c4_counter("load_background_attempts");
    uint64_t demand = c4_counter("load_demand_attempts"), psi = r2_psi();
    /* Fault a known retired folio, regardless of native LRU selection order. */
    content(area + demand_index * PMD, PMD, demand_index * PMD, 240);
    for (unsigned retry = 0; retry < 3000; retry++) {
        if (r2_value(POLICY "stats", "high_epochs") > high &&
            c4_counter("load_background_attempts") > proactive)
            break;
        usleep(1000);
    }
    check(c4_counter("load_demand_attempts") > demand && r2_psi() > psi,
          "actual demand IO creates measurable native PSI with no loaded stall fixture");
    check(r2_value(POLICY "stats", "high_epochs") > high &&
          c4_counter("load_background_attempts") > proactive,
          "fault pressure triggers policy high path and a separate proactive backend read");
    uint64_t low = r2_value(POLICY "stats", "low_epochs");
    uint64_t prepared = r2_value(POLICY "stats", "shadow_prepared_objects");
    check(!mlock(area + 3 * PMD, PMD),
          "keep new application data resident while recording its original contents and PFNs");
    for (size_t i = 3 * PMD; i < 4 * PMD; i++) area[i] = pattern(i, 240);
    command(TRACK "control", "drain\n");
    snapshot(area + 3 * PMD, 512, frames + 1536);
    folio(area + 3 * PMD, 9);
    check(!munlock(area + 3 * PMD, PMD), "make the newly initialized cold application folio reclaimable");
    uint64_t fresh_token = 0;
    for (unsigned retry = 0; retry < 3000; retry++) {
        unsigned count = entries(list);
        for (unsigned i = 0; i < count; i++)
            if (list[i].pfn == frames[1536]) fresh_token = list[i].token;
        if (r2_value(POLICY "stats", "low_epochs") > low &&
            r2_value(POLICY "stats", "shadow_prepared_objects") > prepared && fresh_token)
            break;
        usleep(1000);
    }
    check(r2_value(POLICY "stats", "low_epochs") > low &&
          r2_value(POLICY "stats", "shadow_prepared_objects") > prepared && fresh_token,
          "after a verified high epoch, low-pressure policy reclaims genuinely new cold application data");
    c4_wait_finalized(fresh_token, false);
    command(POLICY "control", "disable\n");
    command(POLICY "control", "clear_target\n");
    content(area, 4 * PMD, 0, 240);
    same_pfns(area, 2048, frames);
    release(area, 4 * PMD, isolated);
    r2_idle(isolated);
    r2_checkpoint("policy_done", NULL);
    puts("PASS R2 real_fault_policy RDMA_PSI high_proactive_restore low_resume no_memstall_fixture");
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "open actual PFN/folio diagnostics");
    struct sigaction action = {.sa_handler=permission_handler};
    sigemptyset(&action.sa_mask);
    check(!sigaction(SIGSEGV, &action, NULL) && !sigaction(SIGBUS, &action, NULL), "install fault probes");
    check(access("/sys/module/chameleon_psi_load", F_OK) != 0, "PSI fixture must not be loaded for real-fault acceptance");
    char *backend = read_text(HERMIT "stats");
    check(strstr(backend, "backend rdma\n") && field(backend, "registered") == 1 &&
          field(backend, "capacity_pages") >= 1024, "real connected RDMA backend has at least 4 MiB pool");
    free(backend);
    command(POLICY "control", "disable\n");
    command(POLICY "control", "clear_target\n");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "reset\n");
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    command(MANAGER "control", "batch 128 6400 128 4 10\n");
    for (unsigned order = 0; order <= 9; order++)
        if (order != 1) command(MANAGER "control", "cost %u %lu\n", order, 1UL << order);
    command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
    uint64_t isolated = isolated_anon();
    r2_idle(isolated);
    r2_checkpoint("initial", NULL);
    fast_shadow(isolated);
    demand_read(isolated);
    background_read(isolated);
    failed_read(isolated);
    concurrent_read(isolated);
    fault_driven_policy(isolated);
    r2_idle(isolated);
    r2_checkpoint("finished", NULL);
    close(flags_fd);
    close(pagemap_fd);
    printf("PASS CHAMELEON_R2 checks=%u\n", checks);
    return 0;
}
