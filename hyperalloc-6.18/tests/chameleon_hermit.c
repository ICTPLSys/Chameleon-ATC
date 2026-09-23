// SPDX-License-Identifier: GPL-2.0-only
/* Real backend contents survive C4 backing discard and application faults. */
#ifndef CHAMELEON_TEST_STAGE
#define CHAMELEON_TEST_STAGE "HERMIT"
#endif
#define CHAMELEON_CONTROL_MAIN c4_reference_main
#include "chameleon_control.c"
#include <sys/syscall.h>
#define HERMIT "/sys/kernel/debug/hermit/"
#define POLICY "/sys/kernel/debug/chameleon_policy/"
#define PSI_LOAD "/sys/kernel/debug/chameleon_psi_load/"

static uint64_t hvalue(const char *key)
{
    char *text = read_text(HERMIT "stats");
    uint64_t value = field(text, key);
    free(text);
    return value;
}

static int htry(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    int ret = command_v(HERMIT "control", format, ap);
    va_end(ap);
    return ret;
}

static void hwait(const char *key, uint64_t minimum)
{
    for (unsigned n = 0; n < 3000; n++) {
        if (hvalue(key) >= minimum)
            return;
        usleep(10000);
    }
    fprintf(stderr, "HERMIT_WAIT key=%s minimum=%" PRIu64 "\n", key, minimum);
    check(false, "bounded backend completion wait");
}

static void hcheckpoint(const char *name, const struct c4_object *obj)
{
    if (obj)
        printf("H_OBJECT phase=%s token=%" PRIu64 " pfn=%" PRIu64
               " pages=%u order=%u\n", name, obj->entry.token, obj->frames[0], obj->pages, obj->order);
    char *text = read_text(HERMIT "stats");
    printf("H_BACKEND_BEGIN %s\n%sH_BACKEND_END %s\n", name, text, name);
    free(text);
    text = read_text(SHADOW "stats");
    printf("H_SHADOW_BEGIN %s\n%sH_SHADOW_END %s\n", name, text, name);
    free(text);
    printf("H_WAIT %s\n", name);
    fflush(stdout);
    char answer[16];
    check(fgets(answer, sizeof(answer), stdin) && !strcmp(answer, "go\n"),
          "runner verifies actual backing before allowing application access");
}

static void hidle(uint64_t isolated)
{
    command(SHADOW "control", "drain\n");
    idle(isolated, 1);
    check(!hvalue("live_slots") && !hvalue("allocated_pages") && !hvalue("inflight"),
          "remote slots, physical reservations and in-flight IO all drain");
    check(!c4_counter("reservation_pages") && !c4_counter("host_reclaimed_pages"),
          "no uninstalled source reservation is leaked");
}

static void hnonpresent(const struct c4_object *obj)
{
    unsigned char resident[512];
    check(!mincore(obj->area, obj->pages * PAGE, resident), "inspect reclaimed application mapping");
    for (unsigned i = 0; i < obj->pages; i++) {
        uint64_t pte = pagemap(pagemap_fd, obj->area + i * PAGE);
        check(!resident[i] && !(pte >> 63) && (pte & (1ULL << 62)) &&
              ((pte & PFN_MASK) >> 5) == obj->entry.token,
              "data-backed object remains a nonpresent exact token until load succeeds");
    }
}

static struct c4_object hprepare(unsigned order, unsigned salt, uint64_t isolated)
{
    struct c4_object obj = c4_mapping(order, salt);
    obj.entry = c4_prepare(getpid(), obj.area, order, obj.frames[0], isolated);
    return obj;
}

static void hsubmit(struct c4_object *obj)
{
    command(SHADOW "control", "submit %" PRIu64 "\n", obj->entry.token);
    c4_wait_finalized(obj->entry.token, false);
    hnonpresent(obj);
    check(residency(obj->area).rss == (512 - obj->pages) * 4,
          "finalization actually removes source RSS before application fault");
}

static void hverify(struct c4_object *obj, uint64_t isolated)
{
    content(obj->area, PMD, 0, obj->salt); /* actual demand fault and full-byte check */
    same_pfns(obj->area, 512, obj->frames);
    folio(obj->area, obj->order);
    check(residency(obj->area).rss == PMD / 1024, "data load restores real anonymous RSS");
    hidle(isolated);
}

static void orders(uint64_t isolated)
{
    phase = "real data round trip for every supported folio order";
    const unsigned values[] = {0, 2, 3, 4, 5, 6, 7, 8, 9};
    for (unsigned n = 0; n < sizeof(values) / sizeof(values[0]); n++) {
        unsigned order = values[n];
        struct c4_object obj = hprepare(order, 170 + order, isolated);
        uint64_t loads = hvalue("load_success");
        hsubmit(&obj);
        check(htry("unregister\n") == -EBUSY, "live remote data prevents backend detachment");
        errno = 0;
        check(syscall(SYS_delete_module, "rswap_client", O_NONBLOCK) == -1 && errno == EWOULDBLOCK,
              "real module reference blocks rmmod while data remains remote");
        char name[64];
        snprintf(name, sizeof(name), "retired_order_%u", order);
        hcheckpoint(name, &obj);
        hverify(&obj, isolated);
        check(hvalue("load_success") == loads + 1, "one successful full-folio load per first demand fault");
        c4_unmap(&obj);
        hidle(isolated);
    }
    hcheckpoint("orders_done", NULL);
    puts("PASS HERMIT orders 0,2,3,4,5,6,7,8,9 real_backing_discard exact_data_PFN_RSS_restore");
}

static void failed_io(uint64_t isolated)
{
    phase = "store failure before authorization and retry";
    struct c4_object obj = hprepare(4, 181, isolated);
    uint64_t failed = hvalue("store_failures");
    command(HERMIT "control", "fail_store 1\n");
    command(SHADOW "control", "submit %" PRIu64 "\n", obj.entry.token);
    hwait("store_failures", failed + 1);
    for (unsigned n = 0; n < 1000 && c4_state(obj.entry.token).phase != 4; n++)
        usleep(1000);
    check(c4_state(obj.entry.token).phase == 4 && !c4_state(obj.entry.token).registered,
          "failed save leaves original Shadow and cannot authorize Host discard");
    nonpresent(pagemap_fd, obj.area, obj.pages, obj.frames[0]);
    hcheckpoint("store_failed", &obj);
    hsubmit(&obj);
    hcheckpoint("retired_store_retry", &obj);
    hverify(&obj, isolated);
    c4_unmap(&obj);
    hidle(isolated);

    phase = "INSTALL and load failures preserve nonpresent mapping for retry";
    obj = hprepare(4, 182, isolated);
    hsubmit(&obj);
    hcheckpoint("retired_load_failure", &obj); /* runner injects Host INSTALL ENOMEM */
    uint64_t loads = hvalue("load_success");
    check(shadow_try("restore %" PRIu64 "\n", obj.entry.token) == -ENOMEM,
          "actual Host INSTALL failure is propagated before any backend read");
    check(hvalue("load_success") == loads, "failed INSTALL cannot start or complete data restoration");
    hnonpresent(&obj);
    hcheckpoint("install_failed", &obj);
    if (getenv("CHAMELEON_TEST_VFIO")) {
        check(shadow_try("restore %" PRIu64 "\n", obj.entry.token) == -ENOMEM,
              "DMA remap failure keeps Guest mapping nonpresent after Host INSTALL");
        check(hvalue("load_success") == loads && hvalue("live_slots") == 1,
              "DMA remap failure cannot consume the remote saved object");
        hnonpresent(&obj);
        hcheckpoint("dma_map_failed", &obj);
    }
    failed = hvalue("load_failures");
    command(HERMIT "control", "fail_load 1\n");
    check(shadow_try("restore %" PRIu64 "\n", obj.entry.token) == -EIO,
          "backend load failure is propagated without publishing present PTEs");
    check(hvalue("load_failures") == failed + 1 && hvalue("live_slots") == 1,
          "failed read preserves the saved object for retry");
    hnonpresent(&obj);
    check(shadow_try("forget %" PRIu64 "\n", obj.entry.token) == -EBUSY,
          "failed data read cannot forget a still reserved saved object");
    hcheckpoint("load_failed", &obj);
    command(HERMIT "control", "fail_load 1\n");
    int fault_signal = sigsetjmp(permission_jump, 1);
    if (!fault_signal) {
        permission_armed = 1;
        volatile unsigned char value = obj.area[173];
        (void)value;
        permission_armed = 0;
        check(false, "failed real demand read cannot expose incomplete bytes");
    }
    check(fault_signal == SIGBUS, "real demand load failure returns SIGBUS without publishing PTEs");
    check(hvalue("load_failures") == failed + 2, "control and demand failure both invoke actual backend load");
    hnonpresent(&obj);
    command(SHADOW "control", "restore %" PRIu64 "\n", obj.entry.token);
    hverify(&obj, isolated);
    c4_unmap(&obj);
    hidle(isolated);
    hcheckpoint("failures_done", NULL);
    puts("PASS HERMIT failures failed_save_no_discard same_token_retry install_failure load_failure actual_fault_SIGBUS no_bad_PTE retry_data");
}

static void async_cancel(uint64_t isolated)
{
    phase = "fast restore and exit while backend store is delayed";
    hcheckpoint("fast_ready", NULL); /* runner holds READY below a large batch threshold */
    command(HERMIT "control", "delay 200\n");
    uint64_t loads = hvalue("load_success");
    struct c4_object obj = hprepare(4, 183, isolated);
    command(SHADOW "control", "submit %" PRIu64 "\n", obj.entry.token);
    hverify(&obj, isolated);
    check(hvalue("load_success") == loads, "resident fast restore does not read remote data");
    c4_unmap(&obj);
    hidle(isolated);
    struct child_owner child = child_owner(4, 184);
    struct entry entry = c4_prepare(child.pid, child.area, 4, child.first_pfn, isolated);
    command(SHADOW "control", "submit %" PRIu64 "\n", entry.token);
    child_finish(&child, 'X');
    hidle(isolated);
    command(HERMIT "control", "delay 0\n");
    hcheckpoint("fast_done", NULL);
    puts("PASS HERMIT async_cancel delayed_store fast_restore exit drains_source_and_remote_slots");
}

static void lifecycle(uint64_t isolated)
{
    phase = "concurrent demand faults load the object once";
    struct c4_object obj = hprepare(5, 185, isolated);
    hsubmit(&obj);
    hcheckpoint("retired_concurrent", &obj);
    uint64_t loads = hvalue("load_success");
    pthread_barrier_t barrier;
    check(!pthread_barrier_init(&barrier, NULL, 3), "create demand fault barrier");
    struct fault_thread args[2] = {
        {.barrier = &barrier, .address = obj.area + 173},
        {.barrier = &barrier, .address = obj.area + 31 * PAGE + 173},
    };
    pthread_t threads[2];
    for (unsigned i = 0; i < 2; i++)
        check(!pthread_create(&threads[i], NULL, fault_reader, &args[i]), "start simultaneous true page fault");
    pthread_barrier_wait(&barrier);
    for (unsigned i = 0; i < 2; i++)
        check(!pthread_join(threads[i], NULL), "join restored application thread");
    check(!pthread_barrier_destroy(&barrier), "destroy fault barrier");
    check(args[0].value == pattern(173, obj.salt) && args[1].value == pattern(31 * PAGE + 173, obj.salt),
          "both racing faults observe saved data");
    hverify(&obj, isolated);
    check(hvalue("load_success") == loads + 1, "simultaneous faults share one load and one publication");
    c4_unmap(&obj);

    phase = "partial unmap of reclaimed object preserves remaining data";
    obj = hprepare(4, 186, isolated);
    hsubmit(&obj);
    hcheckpoint("retired_partial", &obj);
    check(!munmap(obj.area + 4 * PAGE, 4 * PAGE), "unmap real interior reclaimed slots");
    content(obj.area, 4 * PAGE, 0, obj.salt);
    content(obj.area + 8 * PAGE, PMD - 8 * PAGE, 8 * PAGE, obj.salt);
    same_pfns(obj.area, 4, obj.frames);
    same_pfns(obj.area + 8 * PAGE, 512 - 8, obj.frames + 8);
    unsigned char vec;
    check(mincore(obj.area + 4 * PAGE, PAGE, &vec) == -1 && errno == ENOMEM,
          "restoration never resurrects unmapped holes");
    hidle(isolated);
    c4_unmap(&obj);

    phase = "mprotect restores remote data and native write permissions";
    obj = hprepare(4, 187, isolated);
    hsubmit(&obj);
    hcheckpoint("retired_mprotect", &obj);
    check(!mprotect(obj.area, PAGE, PROT_READ), "mprotect safely restores data before permission changes");
    content(obj.area, PMD, 0, obj.salt);
    denied(obj.area, true);
    check(!mprotect(obj.area, PAGE, PROT_READ | PROT_WRITE), "restore writable permissions");
    hverify(&obj, isolated);
    c4_unmap(&obj);

    phase = "mremap restores reclaimed data before moving native mappings";
    for (unsigned full = 0; full < 2; full++) {
        obj = hprepare(full ? 9 : 4, 194 + full, isolated);
        hsubmit(&obj);
        hcheckpoint(full ? "retired_mremap_pmd" : "retired_mremap_partial", &obj);
        size_t bytes = full ? PMD : 16 * PAGE;
        unsigned char *destination = aligned_map(PMD);
        check(mremap(obj.area, bytes, bytes, MREMAP_MAYMOVE | MREMAP_FIXED, destination) == destination,
              "move actual reclaimed mapping to another virtual address");
        content(destination, bytes, 0, obj.salt);
        same_pfns(destination, bytes / PAGE, obj.frames);
        folio(destination, obj.order);
        denied(obj.area + 173, false);
        if (!full) {
            content(obj.area + bytes, PMD - bytes, bytes, obj.salt);
            same_pfns(obj.area + bytes, (PMD - bytes) / PAGE, obj.frames + bytes / PAGE);
        }
        hidle(isolated);
        check(!munmap(destination, PMD), "release data at new virtual address");
        c4_unmap(&obj);
    }

    phase = "fork resolves reclaimed private entries before native COW";
    obj = hprepare(4, 188, isolated);
    hsubmit(&obj);
    hcheckpoint("retired_fork", &obj);
    pid_t pid = fork();
    check(pid >= 0, "fork actual reclaimed owner");
    if (!pid) {
        content(obj.area, PMD, 0, obj.salt);
        obj.area[173] ^= 0xff;
        _exit(obj.area[173] == (unsigned char)(pattern(173, obj.salt) ^ 0xff) ? 0 : 3);
    }
    int status;
    check(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && !WEXITSTATUS(status),
          "child reads saved data and native private write succeeds");
    hverify(&obj, isolated);
    c4_unmap(&obj);

    phase = "exit releases remote storage after real Host installation";
    struct child_owner child = child_owner(4, 189);
    struct entry e = c4_prepare(child.pid, child.area, 4, child.first_pfn, isolated);
    command(SHADOW "control", "submit %" PRIu64 "\n", e.token);
    c4_wait_finalized(e.token, false);
    obj = (struct c4_object){.entry=e,.order=4,.pages=16};
    obj.frames[0] = e.pfn;
    hcheckpoint("retired_exit", &obj);
    loads = hvalue("load_success");
    child_finish(&child, 'X');
    hidle(isolated);
    check(hvalue("load_success") == loads, "dead mm needs no unnecessary data read");
    hcheckpoint("lifecycle_done", NULL);
    puts("PASS HERMIT lifecycle concurrent_faults partial_unmap permissions partial_PMD_mremap fork_COW exit cleanup");
}

static void remote_pool(uint64_t isolated)
{
    phase = "bounded remote pool exhaustion and real slot reuse";
    check(hvalue("capacity_pages") == 1024, "test backend pool is precisely 4 MiB");
    struct c4_object a = hprepare(9, 190, isolated);
    hsubmit(&a);
    struct c4_object b = hprepare(9, 191, isolated);
    hsubmit(&b);
    struct c4_object c = hprepare(9, 192, isolated);
    check(shadow_try("submit %" PRIu64 "\n", c.entry.token) == -ENOSPC,
          "third 2 MiB save rejects exhaustion before authorization");
    check(!c4_state(c.entry.token).registered && hvalue("allocated_pages") == 1024,
          "failed allocation neither discards new backing nor aliases remote slots");
    hcheckpoint("pool_full", NULL);
    content(a.area, PMD, 0, a.salt);
    same_pfns(a.area, 512, a.frames);
    command(SHADOW "control", "drain\n");
    check(hvalue("allocated_pages") == 512, "restored first object releases its exact remote allocation");
    hsubmit(&c);
    check(hvalue("allocated_pages") == 1024, "released slot is reusable by the failed source");
    content(b.area, PMD, 0, b.salt);
    content(c.area, PMD, 0, c.salt);
    same_pfns(b.area, 512, b.frames);
    same_pfns(c.area, 512, c.frames);
    c4_unmap(&a); c4_unmap(&b); c4_unmap(&c);
    hidle(isolated);
    hcheckpoint("pool_done", NULL);
    puts("PASS HERMIT pool ENOSPC exact_capacity distinct_contents slot_reuse");
}

static void automatic_policy(uint64_t isolated)
{
    phase = "C5 automatic real backend store and pressure-driven content restoration";
    struct c4_object obj = c4_mapping(4, 193);
    command(POLICY "control", "disable\n");
    command(POLICY "control", "clear_target\n");
    command(POLICY "control", "set epoch_us 10000\n");
    command(POLICY "control", "set threshold_ppm 10000\n");
    command(POLICY "control", "set psi_full 0\n");
    command(POLICY "control", "set free_pages 0\n");
    command(POLICY "control", "set cold_folios 2\n");
    command(POLICY "control", "set minimum_local_bytes %llu\n", test_ram_bytes()-(16ULL*PAGE));
    command(POLICY "control", "set discard_test 0\n");
    command(POLICY "control", "target %d 0x%lx %lu\n", getpid(), (unsigned long)obj.area, 32*PAGE);
    command(POLICY "control", "enable\n");
    struct entry list[MAX_OBJECTS];
    for (unsigned attempt = 0; attempt < 3000; attempt++) {
        unsigned n = entries(list);
        for (unsigned i = 0; i < n; i++)
            if (list[i].pfn == obj.frames[0]) obj.entry = list[i];
        if (obj.entry.token) break;
        usleep(10000);
    }
    check(obj.entry.token != 0, "C5 automatically selects and submits ordinary application data");
    c4_wait_finalized(obj.entry.token, false);
    hnonpresent(&obj);
    usleep(200000); /* several further low-pressure epochs must respect the floor */
    check(hvalue("live_slots") == 1 && hvalue("allocated_pages") == 16,
          "real backend preparation cannot spend beyond the remaining local-capacity floor");
    same_pfns(obj.area + 16 * PAGE, 16, obj.frames + 16);
    hcheckpoint("retired_policy", &obj);
    uint64_t loads = hvalue("load_success");
    command(PSI_LOAD "control", "start\n");
    hwait("load_success", loads + 1);
    hverify(&obj, isolated);
    command(POLICY "control", "disable\n");
    command(PSI_LOAD "control", "stop\n");
    command(POLICY "control", "clear_target\n");
    c4_unmap(&obj);
    hidle(isolated);
    hcheckpoint("policy_done", NULL);
    puts("PASS HERMIT policy native_PSI automatic_saved_data_commit exact_capacity_floor active_original_data_restore");
}

static void repeated(uint64_t isolated)
{
    phase = "repeated real save/discard/load lifecycle";
    for (unsigned round = 0; round < 16; round++) {
        struct c4_object obj = hprepare(2, 200 + round, isolated);
        hsubmit(&obj);
        hverify(&obj, isolated);
        c4_unmap(&obj);
        hidle(isolated);
    }
    hcheckpoint("cycles_done", NULL);
    puts("PASS HERMIT repeated rounds=16 real_data no_remote_slot_or_backing_leak");
}

#ifndef CHAMELEON_HERMIT_MAIN
#define CHAMELEON_HERMIT_MAIN main
#endif
int CHAMELEON_HERMIT_MAIN(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "open real PFN and folio diagnostics");
    struct sigaction action = {.sa_handler=permission_handler};
    sigemptyset(&action.sa_mask);
    check(!sigaction(SIGSEGV, &action, NULL) && !sigaction(SIGBUS, &action, NULL), "install native fault probes");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "reset\n");
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    command(MANAGER "control", "batch 128 6400 128 4 10\n");
    for (unsigned order = 0; order <= 9; order++)
        if (order != 1) command(MANAGER "control", "cost %u %lu\n", order, 1UL<<order);
    command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
    check(hvalue("registered") == 1, "actual loadable Hermit backend is registered");
    uint64_t isolated = isolated_anon();
    hidle(isolated);
    hcheckpoint("initial", NULL);
    orders(isolated);
    failed_io(isolated);
    async_cancel(isolated);
    lifecycle(isolated);
    remote_pool(isolated);
    automatic_policy(isolated);
    repeated(isolated);
    command(HERMIT "control", "unregister\n");
    check(!hvalue("registered"), "backend cleanly detaches after the final data object");
    hcheckpoint("finished", NULL);
    close(flags_fd); close(pagemap_fd);
    printf("PASS CHAMELEON_HERMIT checks=%u\n", checks);
    return 0;
}
