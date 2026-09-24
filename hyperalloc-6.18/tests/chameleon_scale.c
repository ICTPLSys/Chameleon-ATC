// SPDX-License-Identifier: GPL-2.0-only
/* Keep more than the transient Shadow quota remotely resident at once. */
#define CHAMELEON_TEST_STAGE "R1"
#define CHAMELEON_HERMIT_MAIN hermit_reference_main
#include "chameleon_hermit.c"

#define SCALE_BASE_BYTES (40UL << 20)
#define SCALE_BYTES (56UL << 20)
#define SCALE_REMOTE_BYTES (48UL << 20)
#define SCALE_BASE_RANGES (32UL << 20 >> 12)
#define SCALE_RANGES (SCALE_BASE_RANGES + 255)

static uint64_t scale_value(const char *path, const char *key)
{
    char *text = read_text(path);
    uint64_t value = field(text, key);
    free(text);
    return value;
}

static uint64_t scale_time(void)
{
    struct timespec now;
    check(!clock_gettime(CLOCK_MONOTONIC, &now), "read bounded scale-test clock");
    return (uint64_t)now.tv_sec * 1000000000ULL + now.tv_nsec;
}

static void scale_checkpoint(const char *name)
{
    const char *paths[] = {SHADOW "stats", HERMIT "stats", POLICY "stats"};
    const char *tags[] = {"SHADOW", "BACKEND", "POLICY"};
    for (unsigned i = 0; i < 3; i++) {
        char *text = read_text(paths[i]);
        printf("R1_%s_BEGIN %s\n%sR1_%s_END %s\n", tags[i], name, text, tags[i], name);
        free(text);
    }
    printf("R1_WAIT %s\n", name);
    fflush(stdout);
    char answer[16];
    check(fgets(answer, sizeof(answer), stdin) && !strcmp(answer, "go\n"),
          "runner checks all real Host ranges before advancing scale test");
}

static void scale_idle(uint64_t isolated)
{
    hidle(isolated);
    check(!c4_counter("owned_pages") && !c4_counter("shadow_pages") &&
          !c4_counter("reservation_pages"), "pending credit, original ownership and reservations all drain");
}

static unsigned scale_order(unsigned long page)
{
    if (page < SCALE_BASE_BYTES / PAGE)
        return 0;
    return 2 + (page * PAGE - SCALE_BASE_BYTES) / PMD;
}

static void scale_folio(unsigned char *area, unsigned long page, unsigned order,
                        const char *stage)
{
    unsigned char *address = area + page * PAGE;
    uint64_t first = pfn(address);
    unsigned count = 1U << order;

    /* Keep the original strict folio() assertions below. Print the exact
     * offending PFN, mapping and flags before an assertion can terminate
     * this much larger fixture without identifying its order/chunk. */
    for (unsigned i = 0; i < count; i++) {
        uint64_t flags = page_flags(first + i);
        uint64_t entry = pagemap(pagemap_fd, address + i * PAGE);
        bool shape = order ?
            ((flags & (1ULL << KPF_THP)) &&
             (!!(flags & (1ULL << KPF_COMPOUND_HEAD)) == !i) &&
             (!!(flags & (1ULL << KPF_COMPOUND_TAIL)) == !!i)) :
            !(flags & ((1ULL << KPF_THP) | (1ULL << KPF_COMPOUND_HEAD) |
                       (1ULL << KPF_COMPOUND_TAIL)));
        if ((first & (count - 1)) || !(flags & (1ULL << KPF_ANON)) || !shape ||
            !(entry >> 63) || (entry & PFN_MASK) != first + i) {
            fprintf(stderr, "R1_FOLIO_DIAGNOSTIC stage=%s offset=%lu address=0x%lx"
                    " expected_order=%u index=%u first_pfn=%" PRIu64
                    " physical_pfn=%" PRIu64 " flags=0x%" PRIx64
                    " pagemap=0x%" PRIx64 " head=%u tail=%u thp=%u anon=%u\n",
                    stage, page * PAGE, (unsigned long)address, order, i, first,
                    first + i, flags, entry, !!(flags & (1ULL << KPF_COMPOUND_HEAD)),
                    !!(flags & (1ULL << KPF_COMPOUND_TAIL)), !!(flags & (1ULL << KPF_THP)),
                    !!(flags & (1ULL << KPF_ANON)));
            break;
        }
    }
    folio(address, order);
}

static void scale_objects(unsigned char *area, const uint64_t *frames, unsigned long pages)
{
    /* The real list exceeds the small integration fixture's MAX_OBJECTS
     * and TEXT_SIZE. Stream it without a fixed object-count limit. */
    FILE *stream = fopen(SHADOW "entries", "r");
    check(stream != NULL, "stream all simultaneous Shadow objects");
    char *line = NULL;
    size_t capacity = 0;
    unsigned long count = 0;
    unsigned char *seen = calloc(pages, 1);
    check(seen != NULL, "track exact original source identity for every range");
    while (getline(&line, &capacity, stream) >= 0) {
        struct entry entry;
        if (sscanf(line, "entry token=%" SCNu64 " pid=%d address=0x%lx pfn=%" SCNu64
                        " order=%u live_slots=%u table_pfn=%" SCNu64 " slot=%u",
                   &entry.token, &entry.pid, &entry.address, &entry.pfn, &entry.order,
                   &entry.slots, &entry.table_pfn, &entry.slot) != 8)
            continue;
        check(entry.address >= (uintptr_t)area && entry.address < (uintptr_t)area + SCALE_BYTES &&
              !(entry.address % PAGE), "every live range belongs to the exact scale-test VMA");
        unsigned long page = (entry.address - (uintptr_t)area) / PAGE;
        check(!seen[page] && entry.pfn == frames[page] && entry.order == scale_order(page) &&
              entry.slots == (1U << entry.order), "range metadata matches a unique original physical folio");
        uint64_t pte = pagemap(pagemap_fd, area + page * PAGE);
        check(!(pte >> 63) && (pte & (1ULL << 62)) && ((pte & PFN_MASK) >> 5) == entry.token,
              "streamed range token matches the actual nonpresent application PTE");
        seen[page] = 1;
        count++;
        printf("R1_OBJECT token=%" PRIu64 " pfn=%" PRIu64 " pages=%u order=%u\n",
               entry.token, entry.pfn, 1U << entry.order, entry.order);
    }
    check(!ferror(stream), "complete untruncated Shadow object stream");
    check(!fclose(stream), "close streamed object metadata");
    free(line);
    free(seen);
    check(count == SCALE_RANGES, "all 8447 simultaneous ranges have exact original identities");
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "open actual PFN/folio diagnostics");
    check(access("/sys/module/chameleon_psi_load", F_OK) != 0,
          "scale acceptance does not manufacture PSI pressure");
    check(hvalue("registered") == 1 && hvalue("capacity_pages") >= SCALE_REMOTE_BYTES / PAGE,
          "connected backend pool can retain the full 48 MiB working set");
    check(test_ram_bytes() > 2 * SCALE_BYTES, "VM has capacity for the scale fixture");
    command(POLICY "control", "disable\n");
    command(POLICY "control", "clear_target\n");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "reset\n");
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    command(MANAGER "control", "selector mixed_cost\n");
    command(MANAGER "control", "batch 128 6400 128 4 10\n");
    for (unsigned order = 0; order <= 9; order++)
        if (order != 1) command(MANAGER "control", "cost %u %lu\n", order, 1UL << order);
    command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
    uint64_t isolated = isolated_anon();
    scale_idle(isolated);
    unsigned long pages = SCALE_BYTES / PAGE;
    uint64_t *frames = malloc(pages * sizeof(*frames));
    check(frames != NULL, "retain original PFN of every scale-fixture base page");
    policy(0);
    unsigned char *area = aligned_map(SCALE_BYTES);
    check(!madvise(area, SCALE_BYTES, MADV_NOHUGEPAGE), "keep the 40 MiB base-page region native order zero");
    for (unsigned long i = 0; i < SCALE_BASE_BYTES; i++)
        area[i] = pattern(i, 250);
    printf("R1_CHUNK offset=0 bytes=%lu native_order=0\n", SCALE_BASE_BYTES);
    for (unsigned order = 2; order <= 9; order++) {
        unsigned long offset = SCALE_BASE_BYTES + (order - 2) * PMD;
        policy(order);
        check(!madvise(area + offset, PMD, MADV_HUGEPAGE), "enable native mTHP for this order-specific chunk");
        for (unsigned long i = 0; i < PMD; i++)
            area[offset + i] = pattern(offset + i, 250);
        /* Enabling another global order starts/wakes khugepaged. A long
         * scan sleep alone does not prevent it from collapsing previously
         * faulted smaller folios after order 9 is enabled. Freeze only this
         * populated chunk's future allocation/collapse policy; Linux keeps
         * its existing native compound folios and all original PFNs. */
        check(!madvise(area + offset, PMD, MADV_NOHUGEPAGE),
              "preserve the populated native folios against later background collapse");
        printf("R1_CHUNK offset=%lu bytes=%lu native_order=%u\n", offset, PMD, order);
        for (unsigned long page = offset / PAGE; page < (offset + PMD) / PAGE;
             page += 1UL << order)
            scale_folio(area, page, order, "chunk_initialized");
    }
    policy(0);
    command(TRACK "control", "drain\n");
    snapshot(area, pages, frames);
    for (unsigned long page = 0; page < pages; page += 1UL << scale_order(page))
        scale_folio(area, page, scale_order(page), "complete_initial_layout");
    uint64_t floor = test_ram_bytes() - SCALE_REMOTE_BYTES;
    printf("R1_LAYOUT address=0x%lx bytes=%lu retired_bytes=%lu floor_bytes=%" PRIu64
           " expected_base_ranges=%lu expected_ranges=%lu\n", (unsigned long)area, SCALE_BYTES,
           SCALE_REMOTE_BYTES, floor, SCALE_BASE_RANGES, SCALE_RANGES);
    scale_checkpoint("initial");
    phase = "retain 48 MiB remotely while reusing transient Shadow credit";
    command(POLICY "control", "set epoch_us 1000\n");
    command(POLICY "control", "set threshold_ppm 1000000\n");
    command(POLICY "control", "set psi_full 0\n");
    command(POLICY "control", "set free_pages 0\n");
    command(POLICY "control", "set cold_folios 64\n");
    command(POLICY "control", "set minimum_local_bytes %" PRIu64 "\n", floor);
    command(POLICY "control", "set discard_test 0\n");
    command(POLICY "control", "target %d 0x%lx %lu\n", getpid(), (unsigned long)area, SCALE_BYTES);
    uint64_t prepared_before = scale_value(POLICY "stats", "shadow_prepared_objects");
    uint64_t errors = scale_value(POLICY "stats", "action_errors");
    uint64_t limit = c4_counter("pool_limit_pages");
    check(SCALE_REMOTE_BYTES / PAGE > limit, "target remote capacity exceeds the actual 0.5 percent transient quota");
    command(POLICY "control", "enable\n");
    uint64_t deadline = scale_time() + 180ULL * 1000000000;
    uint64_t peak_pending = 0, retired = 0;
    while (scale_time() < deadline) {
        char *policy_state = read_text(POLICY "stats");
        uint64_t local = field(policy_state, "local_bytes");
        retired = field(policy_state, "retired_bytes");
        free(policy_state);
        uint64_t pending = c4_counter("shadow_pages");
        if (pending > peak_pending) peak_pending = pending;
        check(local >= floor, "asynchronous retirement never crosses the configured local-capacity floor");
        check(pending <= limit, "simultaneous pending Shadow credit remains within its original quota");
        if (retired == SCALE_REMOTE_BYTES && !pending && !hvalue("inflight"))
            break;
        usleep(20000);
    }
    check(retired == SCALE_REMOTE_BYTES, "new batches continue until 48 MiB remain concurrently remote");
    usleep(100000); /* More epochs must not reclaim the remaining eligible 8 MiB. */
    check(scale_value(POLICY "stats", "local_bytes") == floor &&
          scale_value(POLICY "stats", "retired_bytes") == SCALE_REMOTE_BYTES,
          "capacity stays exactly at the floor with more cold candidates still present");
    check(scale_value(POLICY "stats", "action_errors") == errors,
          "scale and transient backpressure do not become policy action errors");
    check(c4_counter("shadow_pages") == 0 && c4_counter("owned_pages") == SCALE_REMOTE_BYTES / PAGE &&
          c4_counter("reservation_pages") == SCALE_REMOTE_BYTES / PAGE &&
          c4_counter("host_reclaimed_pages") == SCALE_REMOTE_BYTES / PAGE,
          "retired originals retain ownership and reservations without consuming pending Shadow credit");
    check(hvalue("allocated_pages") == SCALE_REMOTE_BYTES / PAGE && hvalue("live_slots") == SCALE_RANGES,
          "remote storage keeps every independent saved object simultaneously live");
    check(scale_value(POLICY "stats", "shadow_prepared_objects") >= prepared_before + SCALE_RANGES,
          "automatic policy submitted all scale objects across successive batches");
    uint64_t by_order[10] = {0}, retired_pages = 0, resident_pages = 0;
    unsigned long first_remote_base = pages;
    for (unsigned long page = 0; page < pages;) {
        unsigned order = scale_order(page), length = 1U << order;
        uint64_t first = pagemap(pagemap_fd, area + page * PAGE);
        bool remote = !(first >> 63) && (first & (1ULL << 62));
        if (remote) {
            by_order[order]++;
            if (!order && first_remote_base == pages) first_remote_base = page;
        }
        for (unsigned i = 0; i < length; i++) {
            uint64_t pte = pagemap(pagemap_fd, area + (page + i) * PAGE);
            if (remote) {
                check(!(pte >> 63) && (pte & (1ULL << 62)) &&
                      ((pte & PFN_MASK) >> 5) == ((first & PFN_MASK) >> 5),
                      "every retired folio page references the same real saved token");
                retired_pages++;
            } else {
                check((pte >> 63) && (pte & PFN_MASK) == frames[page + i],
                      "unreclaimed eligible memory remains present at its original PFN");
                resident_pages++;
            }
        }
        page += length;
    }
    check(by_order[0] == SCALE_BASE_RANGES && by_order[0] > 4096,
          "more than 4096 independent 4 KiB ranges coexist remotely");
    for (unsigned order = 2; order <= 9; order++) {
        check(by_order[order] == 512UL >> order, "every supported mixed order remains simultaneously remote");
        printf("R1_ORDER order=%u objects=%" PRIu64 " pages=%" PRIu64 "\n",
               order, by_order[order], by_order[order] << order);
    }
    check(retired_pages == SCALE_REMOTE_BYTES / PAGE && resident_pages == (SCALE_BYTES-SCALE_REMOTE_BYTES) / PAGE,
          "real PTEs show 48 MiB remote and 8 MiB still local");
    printf("R1_SCALE remote_pages=%" PRIu64 " local_pages=%" PRIu64 " base_ranges=%" PRIu64
           " transient_limit=%" PRIu64 " observed_peak_pending=%" PRIu64 "\n",
           retired_pages, resident_pages, by_order[0], limit, peak_pending);
    scale_objects(area, frames, pages);
    scale_checkpoint("retired_scale");
    phase = "demand and proactive readback preserve every original byte and PFN";
    uint64_t loads = hvalue("load_success");
    check(first_remote_base < pages, "an actual remote base page is selected for the demand read");
    content(area + first_remote_base * PAGE, PAGE, first_remote_base * PAGE, 250);
    check(hvalue("load_success") >= loads + 1, "ordinary userspace access reads a saved remote object");
    command(POLICY "control", "disable\n");
    command(POLICY "control", "clear_target\n");
    content(area, SCALE_BYTES, 0, 250);
    same_pfns(area, pages, frames);
    for (unsigned long page = 0; page < pages; page += 1UL << scale_order(page))
        scale_folio(area, page, scale_order(page), "restored_layout");
    check(scale_value(POLICY "stats", "local_bytes") == test_ram_bytes(),
          "all completed installations restore original local capacity");
    release(area, SCALE_BYTES, isolated);
    free(frames);
    scale_idle(isolated);
    scale_checkpoint("restored_scale");
    scale_checkpoint("finished");
    close(flags_fd);
    close(pagemap_fd);
    printf("PASS CHAMELEON_R1 checks=%u\n", checks);
    return 0;
}
