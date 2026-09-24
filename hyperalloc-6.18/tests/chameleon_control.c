// SPDX-License-Identifier: GPL-2.0-only
/* C4 disposable-page control tests. No claim of an application data round trip. */
#ifndef CHAMELEON_TEST_STAGE
#define CHAMELEON_TEST_STAGE "C4"
#endif
#ifndef CHAMELEON_CONTROL_MAIN
#define CHAMELEON_CONTROL_MAIN main
#endif
#define main c3_reference_main
#include "chameleon_shadow.c"
#undef main
#include <sched.h>
#include <time.h>

static uint64_t c4_retired_token;

/* Nested fixtures default to 2 GiB; disk Guests pass their QMP RAM size. */
static unsigned long long test_ram_bytes(void)
{
    const char *text = getenv("CHAMELEON_TEST_RAM_BYTES");
    char *end;
    unsigned long long bytes;

    if (!text)
        return 2ULL << 30;
    errno = 0;
    bytes = strtoull(text, &end, 10);
    check(!errno && end != text && !*end && bytes >= (512ULL << 20) &&
          !(bytes % PAGE), "valid actual VM RAM size from runner");
    return bytes;
}

struct c4_object {
    unsigned char *allocation, *area;
    unsigned order, pages, salt;
    uint64_t frames[512];
    struct entry entry;
};

struct c4_state {
    bool found;
    char kind[24];
    unsigned authorized, registered, ready, host, reserved;
    int phase;
    uint64_t batch;
};

static uint64_t c4_counter(const char *name)
{
    char *text = read_text(SHADOW "stats");
    uint64_t value = field(text, name);
    free(text);
    return value;
}

static struct c4_state c4_state(uint64_t token)
{
    struct c4_state state = {0};
    char *text = read_text(SHADOW "states");
    for (char *line = text; line && *line; ) {
        uint64_t found;
        struct c4_state s = {0};
        if (sscanf(line, "state token=%" SCNu64 " kind=%23s phase=%d authorized=%u registered=%u ready=%u host_state=%u reserved=%u batch=%" SCNu64,
                   &found, s.kind, &s.phase, &s.authorized, &s.registered,
                   &s.ready, &s.host, &s.reserved, &s.batch) == 9 && found == token) {
            state = s;
            state.found = true;
            break;
        }
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    free(text);
    return state;
}

static void c4_checkpoint(const char *name)
{
    static const char *keys[] = {
        "live_objects", "live_slots", "metadata_pages", "shadow_pages",
        "reservation_pages", "host_reclaimed_pages", "reclaimed_slots",
        "finalize_accepted", "finalize_rejected", "finalize_mm_groups",
        "finalize_guest_flushes", "range_install_success", "range_install_failure",
        "zeroed_pages", "cleanup_retries",
    };
    char *text = read_text(SHADOW "stats");
    printf("C4_GUEST_STATS phase=%s", name);
    for (unsigned i = 0; i < sizeof(keys) / sizeof(keys[0]); i++)
        printf(" %s=%" PRIu64, keys[i], field(text, keys[i]));
    putchar('\n');
    free(text);
    printf("C4_WAIT %s\n", name);
    fflush(stdout);
    char reply[32];
    check(fgets(reply, sizeof(reply), stdin) != NULL && !strcmp(reply, "go\n"),
          "runner acknowledges the exact checkpoint with go");
}

/* Exercise real source mappings on every online CPU before removing PTEs. */
static void c4_touch_cpus(unsigned char *area, size_t bytes)
{
    cpu_set_t original;
    check(!sched_getaffinity(0, sizeof(original), &original), "read online CPU affinity");
    for (unsigned cpu = 0; cpu < CPU_SETSIZE; cpu++) {
        if (!CPU_ISSET(cpu, &original))
            continue;
        cpu_set_t only;
        CPU_ZERO(&only);
        CPU_SET(cpu, &only);
        check(!sched_setaffinity(0, sizeof(only), &only), "execute source loads on each CPU");
        volatile unsigned char checksum = 0;
        for (size_t offset = 0; offset < bytes; offset += PAGE)
            checksum ^= *(volatile unsigned char *)(area + offset);
        (void)checksum;
    }
    check(!sched_setaffinity(0, sizeof(original), &original), "restore original CPU affinity");
}

static struct c4_object c4_mapping(unsigned order, unsigned salt)
{
    struct c4_object obj = {.order = order, .pages = 1U << order, .salt = salt};
    policy(order);
    obj.allocation = aligned_map(3 * PMD);
    obj.area = obj.allocation + PMD;
    check(!mprotect(obj.allocation, PMD, PROT_NONE) &&
          !mprotect(obj.area + PMD, PMD, PROT_NONE), "guard VMAs isolate per-arena RSS accounting");
    check(!madvise(obj.area, PMD, MADV_HUGEPAGE), "keep native mTHP enabled for source VMA");
    for (size_t i = 0; i < PMD; i++)
        obj.area[i] = pattern(i, salt);
    command(TRACK "control", "drain\n");
    for (unsigned i = 0; i < 512; i += obj.pages)
        folio(obj.area + i * PAGE, order);
    snapshot(obj.area, 512, obj.frames);
    c4_touch_cpus(obj.area, PMD);
    check(residency(obj.area).rss == PMD / 1024, "all source and neighbour bytes are resident");
    return obj;
}

static struct entry c4_prepare(pid_t pid, unsigned char *area, unsigned order,
                              uint64_t original, uint64_t isolated)
{
    unsigned pages = 1U << order;
    int source_fd = pid == getpid() ? pagemap_fd : child_pagemap(pid);
    uint64_t current = pagemap(source_fd, area) & PFN_MASK;
    if (source_fd != pagemap_fd)
        close(source_fd);
    printf("C4_PREPARE pid=%d order=%u address=0x%lx expected_pfn=%" PRIu64
           " current_pfn=%" PRIu64 " flags=0x%" PRIx64 "\n",
           pid, order, (unsigned long)area, original, current, page_flags(current));
    check(current == original, "source was not relocated by asynchronous native collapse");
    physical_folio(current, order);
    set_target(pid, area, pages * PAGE);
    int result = shadow_try("prepare 1\n");
    if (result) {
        char *manager = read_text(MANAGER "stats");
        char *tracker = read_text(TRACK "stats");
        fprintf(stderr, "C4_PREPARE_ERROR errno=%d source_flags=0x%" PRIx64
                "\nMANAGER\n%sTRACKER\n%s", -result, page_flags(original), manager, tracker);
        free(manager);
        free(tracker);
        errno = -result;
    }
    check(!result, "real C2 selector prepares this exact target folio");
    command(MANAGER "control", "clear_target\n");
    struct entry list[MAX_OBJECTS], found = {0};
    unsigned count = entries(list), matches = 0;
    for (unsigned i = 0; i < count; i++) {
        if (list[i].pfn == original) {
            found = list[i];
            matches++;
        }
    }
    check(matches == 1 && found.pid == pid && found.address == (uintptr_t)area &&
          found.order == order && found.slots == pages,
          "find selected real folio by original PFN while other Shadow objects stay live");
    check(found.table_pfn && found.slot == (((uintptr_t)area / PAGE) & 511),
          "Shadow metadata refers to real PTE table and slot");
    int fd = pid == getpid() ? pagemap_fd : child_pagemap(pid);
    nonpresent(fd, area, pages, original);
    if (fd != pagemap_fd)
        close(fd);
    check(isolated_anon() == isolated, "selection isolation charge moves to Shadow pool");
    check(!(page_flags(original) & (1ULL << KPF_LRU)), "reservation remains outside LRU");
    physical_folio(original, order);
    return found;
}

static void c4_register(struct c4_object *obj, uint64_t isolated, const char *name)
{
    obj->entry = c4_prepare(getpid(), obj->area, obj->order, obj->frames[0], isolated);
    check(shadow_try("commit %" PRIu64 "\n", obj->entry.token) == -EOPNOTSUPP,
          "ordinary Shadow commit cannot claim absent backing persistence");
    command(SHADOW "control", "discard %" PRIu64 "\n", obj->entry.token);
    check(shadow_try("submit %" PRIu64 "\n", obj->entry.token) == -EBUSY,
          "discard authorization and save submission cannot be mixed");
    struct c4_state state = c4_state(obj->entry.token);
    check(state.found && state.authorized && state.registered && !state.ready &&
          state.host == 1 && !state.reserved && !strcmp(state.kind, "shadow"),
          "REGISTER only creates pending host eligibility, not reclaim");
    printf("C4_OBJECT token=%" PRIu64 " order=%u pfn=%" PRIu64
           " addr=0x%lx pages=%u case=%s pid=%d\n", obj->entry.token, obj->order,
           obj->frames[0], (unsigned long)obj->area, obj->pages, name, getpid());
}

static void c4_bus(unsigned char *address, bool write_access)
{
    int caught = sigsetjmp(permission_jump, 1);
    if (!caught) {
        permission_armed = 1;
        if (write_access)
            *(volatile unsigned char *)address ^= 1;
        else {
            volatile unsigned char value = *(volatile unsigned char *)address;
            (void)value;
        }
        permission_armed = 0;
        check(false, "discard-only token access raises SIGBUS");
    }
    check(caught == SIGBUS, "real read/write SIGBUS prevents implicit zero-page restoration");
}

static struct c4_state c4_wait_finalized(uint64_t token, bool allow_not_attempted)
{
    struct c4_state state = {0};
    for (unsigned attempt = 0; attempt < 1000; attempt++) {
        state = c4_state(token);
        if (state.found && !strcmp(state.kind, "reclaimed") && state.reserved &&
            (state.host == 5 || (allow_not_attempted && state.host == 6)))
            return state;
        usleep(10000);
    }
    char *text = read_text(SHADOW "states");
    fprintf(stderr, "Finalization timeout token=%" PRIu64 "\n%s", token, text);
    free(text);
    check(false, "host finalization completes with an explicit per-range result");
    return state;
}

static void c4_reclaimed(struct c4_object *obj)
{
    struct c4_state state = c4_state(obj->entry.token);
    check(state.found && !strcmp(state.kind, "reclaimed") && state.reserved,
          "finalized object retains its original physical allocation until INSTALL");
    unsigned char resident[512];
    check(!mincore(obj->area, obj->pages * PAGE, resident), "mincore reclaimed user VA");
    for (unsigned i = 0; i < obj->pages; i++) {
        check(!(resident[i] & 1), "reclaimed token PTE never reports resident memory");
        uint64_t entry = pagemap(pagemap_fd, obj->area + i * PAGE);
        check(!(entry >> 63) && (entry & (1ULL << 62)), "reclaimed user PTE is really nonpresent");
        check(((entry & PFN_MASK) >> 5) == obj->entry.token,
              "reclaimed PTE encodes the owning token rather than an accessible old PFN");
    }
    c4_bus(obj->area, false);
    c4_bus(obj->area + obj->pages * PAGE - 1, true);
    struct resident_stats rss = residency(obj->area);
    check(rss.rss == (512 - obj->pages) * 4 && !rss.swap,
          "finalize removes only target RSS without inventing swap backing");
    size_t offset = obj->pages * PAGE;
    if (offset < PMD) {
        content(obj->area + offset, PMD - offset, offset, obj->salt);
        same_pfns(obj->area + offset, 512 - obj->pages, obj->frames + obj->pages);
    }
    check(shadow_try("restore %" PRIu64 "\n", obj->entry.token) == -EOPNOTSUPP &&
          shadow_try("cancel %" PRIu64 "\n", obj->entry.token) == -EOPNOTSUPP,
          "finalized discard-only mapping cannot be restored as original application data");
}

static void c4_install(struct c4_object *obj)
{
    command(SHADOW "control", "install %" PRIu64 "\n", obj->entry.token);
    struct c4_state state = c4_state(obj->entry.token);
    check(state.found && state.host == 6 && !state.reserved,
          "successful INSTALL releases the physical reservation exactly once");
    c4_bus(obj->area, false);
    c4_bus(obj->area + obj->pages * PAGE - 1, true);
    if (obj->pages < 512) {
        size_t offset = obj->pages * PAGE;
        content(obj->area + offset, PMD - offset, offset, obj->salt);
        same_pfns(obj->area + offset, 512 - obj->pages, obj->frames + obj->pages);
    }
}

static void c4_unmap(struct c4_object *obj)
{
    check(!munmap(obj->allocation, 3 * PMD), "unmap token mapping and neighbour arena");
    obj->allocation = NULL;
}

static void c4_mixed(uint64_t isolated)
{
    phase = "mixed 4 KiB/64 KiB/2 MiB finalization";
    struct c4_object mixed[3], canceled;
    const unsigned orders[3] = {0, 4, 9};
    uint64_t zeroed = c4_counter("zeroed_pages");
    uint64_t flushes = c4_counter("finalize_guest_flushes");
    for (unsigned i = 0; i < 3; i++) {
        mixed[i] = c4_mapping(orders[i], 181 + i);
        c4_register(&mixed[i], isolated, "mixed");
    }
    canceled = c4_mapping(2, 184);
    c4_register(&canceled, isolated, "cancel");
    check(stats().pages == 533 && !c4_counter("reservation_pages"),
          "pending mixed objects retain source pages without finalization");
    c4_checkpoint("registered");
    check(*(volatile unsigned char *)canceled.area == pattern(0, canceled.salt),
          "actual fault cancels registered but unready object without data loss");
    command(SHADOW "control", "drain\n");
    command(TRACK "control", "drain\n");
    check(!c4_state(canceled.entry.token).found && stats().pages == 529,
          "canceled range cleans up without draining the other live objects");
    same_pfns(canceled.area, 512, canceled.frames);
    content(canceled.area, PMD, 0, canceled.salt);
    c4_checkpoint("cancelled");
    for (unsigned i = 0; i < 2; i++)
        command(SHADOW "control", "ready %" PRIu64 "\n", mixed[i].entry.token);
    check(!c4_counter("reservation_pages"), "17 ready pages stay below the 529-page batch threshold");
    c4_checkpoint("below_threshold");
    command(SHADOW "control", "ready %" PRIu64 "\n", mixed[2].entry.token);
    for (unsigned i = 0; i < 3; i++) {
        c4_wait_finalized(mixed[i].entry.token, false);
        c4_reclaimed(&mixed[i]);
    }
    check(c4_counter("finalize_guest_flushes") == flushes + 1,
          "mixed orders in one mm use one real second guest TLB flush");
    check(c4_counter("reservation_pages") == 529 && c4_counter("host_reclaimed_pages") == 529,
          "exact mixed physical reservations remain allocated after Host discard");
    content(canceled.area, PMD, 0, canceled.salt);
    same_pfns(canceled.area, 512, canceled.frames);
    c4_checkpoint("mixed_retired");
    check(shadow_try("install %" PRIu64 "\n", mixed[0].entry.token) == -ENOMEM,
          "injected population failure propagates the real errno");
    check(c4_state(mixed[0].entry.token).reserved && c4_state(mixed[0].entry.token).host == 5 &&
          c4_counter("reservation_pages") == 529 && c4_counter("host_reclaimed_pages") == 529,
          "failed install retains reservation and missing-backing accounting");
    c4_reclaimed(&mixed[0]);
    c4_checkpoint("install_failed");
    for (unsigned i = 0; i < 3; i++)
        c4_install(&mixed[i]);
    check(!c4_counter("reservation_pages") && !c4_counter("host_reclaimed_pages") &&
          c4_counter("zeroed_pages") == zeroed + 529,
          "successful installs zero all 529 pages before releasing reservations");
    for (unsigned i = 0; i < 3; i++)
        c4_unmap(&mixed[i]);
    c4_unmap(&canceled);
    idle(isolated, 1);
    c4_retired_token = mixed[0].entry.token;
    check(shadow_try("ready %" PRIu64 "\n", mixed[0].entry.token) == -ESTALE,
          "retired guest token cannot authorize future backing");
    c4_checkpoint("mixed_installed");
    puts("PASS C4 mixed cancellation guest_flush SIGBUS neighbours install_failure zeroed_reservations");
}

static void c4_watermark(uint64_t isolated)
{
    phase = "independent Host free-memory watermark";
    struct c4_object obj = c4_mapping(3, 185);
    uint64_t zeroed = c4_counter("zeroed_pages");
    c4_register(&obj, isolated, "watermark");
    check(shadow_try("ready %" PRIu64 "\n", c4_retired_token) == -ESTALE &&
          c4_state(obj.entry.token).host == 1 && !c4_state(obj.entry.token).ready,
          "stale token cannot alter a newly registered physical allocation");
    c4_checkpoint("watermark_registered");
    struct c4_state pending = c4_state(obj.entry.token);
    check(pending.host == 1 && !pending.reserved && !strcmp(pending.kind, "shadow"),
          "high Host watermark alone cannot commit a pending unready object");
    command(SHADOW "control", "ready %" PRIu64 "\n", obj.entry.token);
    c4_wait_finalized(obj.entry.token, false);
    c4_reclaimed(&obj);
    c4_checkpoint("watermark_retired");
    c4_install(&obj);
    check(c4_counter("zeroed_pages") == zeroed + 8, "watermark case zeroes its exact reservation");
    c4_unmap(&obj);
    idle(isolated, 1);
    c4_checkpoint("watermark_installed");
    puts("PASS C4 watermark pending_ineligible below_batch_host_trigger");
}

static void c4_orders(uint64_t isolated)
{
    phase = "16/128/256/512/1024 KiB successful range coverage";
    struct c4_object objects[5];
    const unsigned orders[5] = {2, 5, 6, 7, 8};
    uint64_t zeroed = c4_counter("zeroed_pages");
    uint64_t flushes = c4_counter("finalize_guest_flushes");
    for (unsigned i = 0; i < 5; i++) {
        /* Reserve each exact folio before changing the next THP policy. */
        objects[i] = c4_mapping(orders[i], 190 + i);
        c4_register(&objects[i], isolated, "orders");
    }
    check(stats().pages == 484 && !c4_counter("reservation_pages"),
          "five explicit folio orders remain pending as 484 source pages");
    c4_checkpoint("orders_registered");
    for (unsigned i = 0; i < 5; i++)
        command(SHADOW "control", "ready %" PRIu64 "\n", objects[i].entry.token);
    for (unsigned i = 0; i < 5; i++) {
        c4_wait_finalized(objects[i].entry.token, false);
        c4_reclaimed(&objects[i]);
    }
    check(c4_counter("finalize_guest_flushes") == flushes + 1,
          "one mm with five explicit orders needs one second guest TLB flush");
    check(c4_counter("reservation_pages") == 484 && c4_counter("host_reclaimed_pages") == 484,
          "all intermediate-order physical reservations survive actual Host discard");
    c4_checkpoint("orders_retired");
    for (unsigned i = 0; i < 5; i++) {
        c4_install(&objects[i]);
        c4_unmap(&objects[i]);
    }
    idle(isolated, 1);
    check(c4_counter("zeroed_pages") == zeroed + 484 &&
          !c4_counter("reservation_pages") && !c4_counter("host_reclaimed_pages"),
          "all 484 intermediate-order pages are zeroed before reservation release");
    c4_checkpoint("orders_installed");
    puts("PASS C4 orders order2_5_6_7_8 484_pages guest_flush SIGBUS neighbours zeroed_reservations");
}

static void c4_partial(uint64_t isolated)
{
    phase = "honest partial discard result";
    struct c4_object objects[2];
    uint64_t zeroed = c4_counter("zeroed_pages");
    for (unsigned i = 0; i < 2; i++) {
        objects[i] = c4_mapping(2 + i, 186 + i);
        c4_register(&objects[i], isolated, "partial");
    }
    c4_checkpoint("partial_registered");
    for (unsigned i = 0; i < 2; i++)
        command(SHADOW "control", "ready %" PRIu64 "\n", objects[i].entry.token);
    unsigned retired = 0, intact = 0, retired_pages = 0;
    for (unsigned i = 0; i < 2; i++) {
        struct c4_state state = c4_wait_finalized(objects[i].entry.token, true);
        retired += state.host == 5;
        intact += state.host == 6;
        if (state.host == 5)
            retired_pages += objects[i].pages;
        c4_reclaimed(&objects[i]);
        printf("C4_PARTIAL token=%" PRIu64 " pages=%u host_state=%u\n",
               objects[i].entry.token, objects[i].pages, state.host);
    }
    check(retired == 1 && intact == 1 && c4_counter("reservation_pages") == 12 &&
          c4_counter("host_reclaimed_pages") == retired_pages,
          "one honest discard success and one untouched range retain separate outcomes");
    c4_checkpoint("partial_result");
    for (unsigned i = 0; i < 2; i++) {
        c4_install(&objects[i]);
        c4_unmap(&objects[i]);
    }
    idle(isolated, 1);
    check(c4_counter("zeroed_pages") == zeroed + 12 && !c4_counter("reservation_pages"),
          "partial result cleanup zeroes both reservations only after valid install ACKs");
    puts("PASS C4 partial honest_per_range_status retry_and_cleanup");
}

static void c4_exit(uint64_t isolated)
{
    phase = "owner exit retries Host install before freeing reservation";
    uint64_t zeroed = c4_counter("zeroed_pages");
    uint64_t retries = c4_counter("cleanup_retries");
    struct child_owner child = child_owner(4, 188);
    struct entry e = c4_prepare(child.pid, child.area, 4, child.first_pfn, isolated);
    command(SHADOW "control", "discard %" PRIu64 "\n", e.token);
    printf("C4_OBJECT token=%" PRIu64 " order=4 pfn=%" PRIu64
           " addr=0x%lx pages=16 case=exit pid=%d\n", e.token, child.first_pfn,
           (unsigned long)child.area, child.pid);
    c4_checkpoint("exit_registered");
    command(SHADOW "control", "ready %" PRIu64 "\n", e.token);
    c4_wait_finalized(e.token, false);
    c4_checkpoint("exit_retired");
    check(write(child.finish, "X", 1) == 1, "release owner for actual exit_mmap");
    close(child.finish);
    int status = 0;
    bool exited = false;
    for (unsigned i = 0; i < 1000; i++) {
        pid_t result = waitpid(child.pid, &status, WNOHANG);
        check(result >= 0, "poll child exit without an unbounded wait");
        if (result == child.pid) {
            exited = true;
            break;
        }
        usleep(10000);
    }
    check(exited && WIFEXITED(status) && !WEXITSTATUS(status),
          "retained mm_count and host reservation do not prevent owner exit");
    idle(isolated, 1);
    check(c4_counter("cleanup_retries") > retries && c4_counter("zeroed_pages") == zeroed + 16 &&
          !c4_counter("reservation_pages") && !c4_counter("host_reclaimed_pages"),
          "exit cleanup retries injected install error before zeroing and releasing pages");
    c4_checkpoint("exit_clean");
    puts("PASS C4 lifecycle owner_exit install_retry no_mm_users_leak no_metadata_leak");
}

int CHAMELEON_CONTROL_MAIN(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    check(sysconf(_SC_PAGESIZE) == PAGE, "4 KiB base-page platform");
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "privileged physical page inspection");
    struct sigaction action = {.sa_handler = permission_handler};
    sigemptyset(&action.sa_mask);
    check(!sigaction(SIGSEGV, &action, NULL) && !sigaction(SIGBUS, &action, NULL),
          "install real permission and reclaimed-token fault probes");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "capacity %llu %llu\n", test_ram_bytes(), test_ram_bytes());
    command(TRACK "control", "reset\n");
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    command(MANAGER "control", "batch 128 6400 128 4 10\n");
    for (unsigned order = 0; order <= 9; order++)
        if (order != 1)
            command(MANAGER "control", "cost %u %lu\n", order, 1UL << order);
    command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
    check(c4_counter("transport_available") == 1 && !c4_counter("backend_present"),
          "real C4 virtio transport is active without an application data backend");
    uint64_t isolated = isolated_anon();
    idle(isolated, 1);
    c4_mixed(isolated);
    c4_watermark(isolated);
    c4_orders(isolated);
    c4_partial(isolated);
    c4_exit(isolated);
    idle(isolated, 1);
    char *text = read_text(SHADOW "stats");
    printf("FINAL_C4_SHADOW_STATS\n%s", text);
    free(text);
    close(flags_fd);
    close(pagemap_fd);
    printf("PASS CHAMELEON_C4 checks=%u\n", checks);
    return 0;
}
