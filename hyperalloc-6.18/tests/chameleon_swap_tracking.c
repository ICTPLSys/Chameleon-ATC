// SPDX-License-Identifier: GPL-2.0-only
/* R3: hotness follows successful remote retirement/readback, not Shadow
 * preparation. Run with a registered Hermit backend and Host batch-pages=1,
 * watermark-bytes=0. The default run additionally requires genuine PEBS. */
#define CHAMELEON_TEST_STAGE "R3"
#define CHAMELEON_HERMIT_MAIN hermit_reference_main
#include "chameleon_hermit.c"
#include <stdatomic.h>

static void r3_dump_file(const char *path)
{
    char buffer[4096];
    int fd = open(path, O_RDONLY);
    fprintf(stderr, "R3_DIAGNOSTIC_BEGIN %s\n", path);
    if (fd < 0) {
        fprintf(stderr, "open errno=%d (%s)\n", errno, strerror(errno));
    } else {
        ssize_t n;
        while ((n = read(fd, buffer, sizeof(buffer))) > 0)
            (void)fwrite(buffer, 1, n, stderr);
        close(fd);
    }
    fprintf(stderr, "R3_DIAGNOSTIC_END %s\n", path);
}

static void r3_command(const char *path, const char *format, ...)
    __attribute__((format(printf, 2, 3)));

static void r3_command(const char *path, const char *format, ...)
{
    char text[256];
    va_list ap;
    va_start(ap, format);
    int n = vsnprintf(text, sizeof(text), format, ap);
    va_end(ap);
    check(n > 0 && n < (int)sizeof(text), "bounded expanded R3 control command");
    int fd = open(path, O_WRONLY);
    ssize_t result = fd < 0 ? -1 : write(fd, text, n);
    int error = errno;
    if (fd >= 0) close(fd);
    if (result != n) {
        fprintf(stderr, "R3_COMMAND_FAILURE phase=%s path=%s command=%s"
                "result=%zd expected=%d errno=%d (%s)\n",
                phase, path, text, result, n, error, strerror(error));
        const char *diagnostics[] = {TRACK "stats", MANAGER "stats", SHADOW "stats",
                                    SHADOW "states", HERMIT "stats", "/proc/self/maps"};
        for (unsigned i = 0; i < sizeof(diagnostics) / sizeof(diagnostics[0]); i++)
            r3_dump_file(diagnostics[i]);
        errno = error;
    }
    check(result == n, "expanded R3 debugfs/sysfs command succeeds");
}

#define command r3_command

struct r3_stats {
    uint64_t bins[16], managed, phi, out, in, hardware, synthetic, cooling;
};

static unsigned char *r3_reference;
static uint64_t r3_reference_pfns[64];

static struct r3_stats r3_stats(void)
{
    struct r3_stats s = {0};
    char *text = read_text(TRACK "stats");
    uint64_t sum = 0;
    for (unsigned i = 0; i < 16; i++) {
        char key[16];
        snprintf(key, sizeof(key), "bin%u", i);
        s.bins[i] = field(text, key);
        sum += s.bins[i];
    }
    s.managed = field(text, "managed_pages");
    check(sum == s.managed && sum == field(text, "histogram_sum"),
          "all sixteen histogram bins conserve every managed base page");
    s.phi = field(text, "phi");
    s.out = field(text, "swapout_pages");
    s.in = field(text, "swapin_pages");
    s.hardware = field(text, "hardware_samples");
    s.synthetic = field(text, "synthetic_samples");
    s.cooling = field(text, "cooling_epochs");
    free(text);
    return s;
}

static unsigned r3_bin(unsigned value)
{
    return value ? 31U - (unsigned)__builtin_clz(value) : 0;
}

/* Check the complete histogram transition, not only its conserved sum.
 * Newly managed pages can first enter bin 0 during unrelated kernel frees. */
static void r3_delta(struct r3_stats before, const unsigned *old,
                     const unsigned *new, unsigned nr, unsigned out, unsigned in)
{
    struct r3_stats after = r3_stats();
    int64_t delta[16] = {0};
    delta[0] = (int64_t)after.managed - (int64_t)before.managed;
    for (unsigned i = 0; i < nr; i++) {
        delta[r3_bin(old ? old[i] : 0)]--;
        delta[r3_bin(new ? new[i] : 0)]++;
    }
    for (unsigned i = 0; i < 16; i++)
        check((int64_t)after.bins[i] - (int64_t)before.bins[i] == delta[i],
              "exact bin population change agrees with every affected counter");
    check(after.out == before.out + out && after.in == before.in + in,
          "lifecycle counters count only successfully retired or published pages");
}

static void r3_range(uint64_t pfn, unsigned pages, unsigned *out)
{
    char request[80], result[32768];
    int fd = open(TRACK "range", O_RDWR);
    check(fd >= 0, "open PFN counters");
    int n = snprintf(request, sizeof(request), "%" PRIu64 " %u\n", pfn, pages);
    check(write(fd, request, n) == n, "select physical counter range");
    ssize_t got = pread(fd, result, sizeof(result) - 1, 0);
    check(got > 0, "read physical counters without touching application PTEs");
    close(fd);
    result[got] = 0;
    char *line = result;
    for (unsigned i = 0; i < pages; i++) {
        uint64_t frame;
        unsigned value;
        check(line && sscanf(line, "%" SCNu64 " %u", &frame, &value) == 2 &&
              frame == pfn + i && value <= 65535, "counter PFN and value match request");
        out[i] = value;
        line = strchr(line, '\n');
        if (line) line++;
    }
}

static void r3_equal(uint64_t pfn, unsigned pages, const unsigned *expected)
{
    unsigned values[512];
    r3_range(pfn, pages, values);
    for (unsigned i = 0; i < pages; i++)
        check(values[i] == (expected ? expected[i] : 0),
              "each physical counter has exactly its lifecycle value");
}

static void r3_reference_check(void)
{
    for (unsigned i = 0; i < 64; i++) {
        uint64_t frame = pfn(r3_reference + i * PAGE);
        unsigned value;
        r3_range(frame, 1, &value);
        if (frame != r3_reference_pfns[i] || value != 32)
            fprintf(stderr, "R3_REFERENCE page=%u original_pfn=%" PRIu64
                    " actual_pfn=%" PRIu64 " heat=%u expected_heat=32\n",
                    i, r3_reference_pfns[i], frame, value);
        check(frame == r3_reference_pfns[i] && value == 32,
              "reference remains on its original base PFNs with unchanged measured heat");
    }
}

/* The shared C3 idle helper deliberately rejects nonzero sample counts;
 * this test instead verifies cleanup while retaining real tracker evidence. */
static void r3_idle(uint64_t isolated)
{
    unsigned attempt;
    for (attempt = 0; attempt < 300; attempt++) {
        command(SHADOW "control", "drain\n");
        command(TRACK "control", "drain\n");
        if (!c4_counter("live_objects") && !c4_counter("live_slots") &&
            !c4_counter("metadata_pages") && !c4_counter("reservation_pages") &&
            !c4_counter("host_reclaimed_pages") && !hvalue("live_slots") &&
            !hvalue("allocated_pages") && !hvalue("inflight") &&
            isolated_anon() == isolated)
            break;
        usleep(10000);
    }
    check(attempt < 300, "remote objects, reservations, metadata and isolation all drain");
    r3_stats();
}

static void r3_seed(struct c4_object *obj, unsigned *values)
{
    r3_reference_check();
    for (unsigned i = 0; i < obj->pages; i++) {
        values[i] = 2U << (i % 4);
        command(TRACK "inject", "0x%lx %u\n",
                (unsigned long)(obj->area + i * PAGE), values[i]);
    }
    r3_equal(obj->frames[0], obj->pages, values);
}

static void r3_prepare(struct c4_object *obj, uint64_t isolated)
{
    /* Native selection permits hardware-heated sources while retaining all
     * normal ownership/pin checks. No synthetic tracker data is needed. */
    command(MANAGER "control", "lru_tail %d 0x%lx\n", getpid(),
            (unsigned long)obj->area);
    obj->entry = c4_prepare(getpid(), obj->area, obj->order, obj->frames[0], isolated);
}

static void r3_fast_and_save_failure(uint64_t isolated)
{
    for (unsigned fail = 0; fail < 2; fail++) {
        phase = fail ? "failed save retains heat" : "resident Shadow restore retains heat";
        struct c4_object obj = c4_mapping(4, 220 + fail);
        unsigned values[512];
        r3_seed(&obj, values);
        struct r3_stats before = r3_stats();
        r3_prepare(&obj, isolated);
        if (fail) {
            uint64_t failures = hvalue("store_failures");
            command(HERMIT "control", "fail_store 1\n");
            command(SHADOW "control", "submit %" PRIu64 "\n", obj.entry.token);
            hwait("store_failures", failures + 1);
            unsigned attempt;
            for (attempt = 0; attempt < 1000 && c4_state(obj.entry.token).phase != 4; attempt++)
                usleep(1000);
            check(attempt < 1000 && !c4_state(obj.entry.token).registered,
                  "failed persistence cannot authorize source retirement");
        }
        r3_equal(obj.frames[0], obj.pages, values);
        r3_delta(before, NULL, NULL, 0, 0, 0);
        command(SHADOW "control", "restore %" PRIu64 "\n", obj.entry.token);
        content(obj.area, PMD, 0, obj.salt);
        same_pfns(obj.area, 512, obj.frames);
        r3_idle(isolated);
        r3_equal(obj.frames[0], obj.pages, values);
        r3_delta(before, NULL, NULL, 0, 0, 0);
        c4_unmap(&obj);
        r3_idle(isolated);
        r3_equal(obj.frames[0], obj.pages, NULL);
        r3_delta(before, values, NULL, obj.pages, 0, 0);
    }
    puts("PASS R3 resident_fast_restore failed_save unchanged_counters full_histogram");
}

static void r3_round_trip(uint64_t isolated, unsigned order, bool partial, bool fail_load)
{
    phase = partial ? "partial unmap initializes only surviving slots" :
            fail_load ? "failed read cannot initialize counters" : "successful retirement and readback";
    struct c4_object obj = c4_mapping(order, 230 + order + partial);
    unsigned old[512] = {0}, restored[512] = {0};
    r3_seed(&obj, old);
    struct r3_stats before = r3_stats();
    if (before.phi != 63)
        fprintf(stderr, "R3_THRESHOLD order=%u phi=%" PRIu64 " bin5=%" PRIu64 "\n",
                order, before.phi, before.bins[5]);
    check(before.phi == 63, "hot reference region gives a nontrivial initialization threshold");
    r3_prepare(&obj, isolated);
    hsubmit(&obj);
    r3_equal(obj.frames[0], obj.pages, NULL);
    r3_delta(before, old, NULL, obj.pages, obj.pages, 0);
    command(TRACK "control", "drain\n");
    r3_equal(obj.frames[0], obj.pages, NULL);
    struct r3_stats retired = r3_stats();
    if (fail_load) {
        command(HERMIT "control", "fail_load 1\n");
        check(shadow_try("restore %" PRIu64 "\n", obj.entry.token) == -EIO,
              "failed read returns its actual IO error");
        hnonpresent(&obj);
        r3_equal(obj.frames[0], obj.pages, NULL);
        r3_delta(retired, NULL, NULL, 0, 0, 0);
        command(HERMIT "control", "fail_load 1\n");
        int caught = sigsetjmp(permission_jump, 1);
        if (!caught) {
            permission_armed = 1;
            volatile unsigned char value = obj.area[173];
            (void)value;
            permission_armed = 0;
            check(false, "failed demand read cannot publish a source byte");
        }
        check(caught == SIGBUS, "failed demand read delivers the native SIGBUS result");
        hnonpresent(&obj);
        r3_equal(obj.frames[0], obj.pages, NULL);
        r3_delta(retired, NULL, NULL, 0, 0, 0);
    }
    unsigned holes = partial ? 4 : 0;
    if (partial)
        check(!munmap(obj.area + 4 * PAGE, holes * PAGE), "remove an interior subset of retired slots");
    for (unsigned i = 0; i < obj.pages; i++)
        restored[i] = partial && i >= 4 && i < 8 ? 0 : (unsigned)retired.phi;
    command(SHADOW "control", "restore %" PRIu64 "\n", obj.entry.token);
    r3_equal(obj.frames[0], obj.pages, restored);
    r3_delta(retired, NULL, restored, obj.pages, 0, obj.pages - holes);
    if (partial) {
        content(obj.area, 4 * PAGE, 0, obj.salt);
        content(obj.area + 8 * PAGE, PMD - 8 * PAGE, 8 * PAGE, obj.salt);
        unsigned char resident;
        errno = 0;
        check(mincore(obj.area + 4 * PAGE, PAGE, &resident) == -1 && errno == ENOMEM,
              "readback does not republish an unmapped hole");
    } else {
        content(obj.area, PMD, 0, obj.salt);
        same_pfns(obj.area, 512, obj.frames);
    }
    r3_idle(isolated);
    r3_equal(obj.frames[0], obj.pages, restored);
    before = r3_stats();
    c4_unmap(&obj);
    r3_idle(isolated);
    r3_equal(obj.frames[0], obj.pages, NULL);
    r3_delta(before, restored, NULL, obj.pages, 0, 0);
    printf("PASS R3 order=%u partial_unmapped=%u failed_read=%u clear_on_retire phi_on_readback full_histogram free_clear\n",
           order, holes, fail_load);
}

static void r3_unmap_remote(uint64_t isolated)
{
    phase = "fully unmapped remote object never initializes discarded counters";
    struct c4_object obj = c4_mapping(4, 248);
    unsigned values[512];
    r3_seed(&obj, values);
    r3_prepare(&obj, isolated);
    hsubmit(&obj);
    struct r3_stats retired = r3_stats();
    uint64_t loads = hvalue("load_success");
    c4_unmap(&obj);
    r3_idle(isolated);
    check(hvalue("load_success") == loads, "unmapped data needs no backend read");
    r3_equal(obj.frames[0], obj.pages, NULL);
    r3_delta(retired, NULL, NULL, 0, 0, 0);
    puts("PASS R3 retired_full_unmap no_read no_counter_resurrection full_histogram");
}

static void r3_split_collapse(uint64_t isolated)
{
    phase = "readback heat survives native split and real collapse";
    struct c4_object obj = c4_mapping(4, 249);
    r3_prepare(&obj, isolated);
    hsubmit(&obj);
    command(SHADOW "control", "restore %" PRIu64 "\n", obj.entry.token);
    r3_idle(isolated);
    unsigned wanted[16], actual[16];
    for (unsigned i = 0; i < 16; i++)
        command(TRACK "inject", "0x%lx %u\n", (unsigned long)(obj.area + i * PAGE), i + 1);
    r3_range(obj.frames[0], 16, wanted);
    struct r3_stats before = r3_stats();
    check(!madvise(obj.area, PMD, MADV_NOHUGEPAGE), "prevent spontaneous collapse during native split");
    command("/sys/kernel/debug/split_huge_pages", "%d,0x%lx,0x%lx,0\n", getpid(),
            (unsigned long)obj.area, (unsigned long)(obj.area + 16 * PAGE));
    for (unsigned i = 0; i < 16; i++) folio(obj.area + i * PAGE, 0);
    r3_equal(obj.frames[0], 16, wanted);
    r3_delta(before, NULL, NULL, 0, 0, 0);
    check(!madvise(obj.area, PMD, MADV_HUGEPAGE), "allow explicit C2 collapse after real split");
    /* C2 accepts base->order2, then adjacent equal-order source folios for
     * each larger order. A direct base->order4 request is correctly EINVAL. */
    for (unsigned order = 2; order <= 4; order++) {
        unsigned pages = 1U << order;
        for (unsigned offset = 0; offset < 16; offset += pages) {
            unsigned char *area = obj.area + offset * PAGE;
            uint64_t source[16], destination[16];
            snapshot(area, pages, source);
            command(MANAGER "control", "collapse %d 0x%lx %u\n", getpid(),
                    (unsigned long)area, order);
            folio(area, order);
            snapshot(area, pages, destination);
            for (unsigned i = 0; i < pages; i++) {
                for (unsigned j = 0; j < pages; j++)
                    check(destination[i] != source[j], "each collapse uses distinct replacement PFNs");
                r3_equal(source[i], 1, NULL);
            }
            r3_range(destination[0], pages, actual + offset);
            check(!memcmp(wanted + offset, actual + offset, pages * sizeof(*wanted)),
                  "every hierarchical collapse preserves each post-readback heat value");
            r3_delta(before, NULL, NULL, 0, 0, 0);
        }
    }
    folio(obj.area, 4);
    uint64_t destination = pfn(obj.area);
    r3_range(destination, 16, actual);
    check(!memcmp(wanted, actual, sizeof(wanted)), "collapse transfers each post-readback heat value");
    r3_delta(before, NULL, NULL, 0, 0, 0);
    content(obj.area, PMD, 0, obj.salt);
    c4_unmap(&obj);
    r3_idle(isolated);
    r3_equal(destination, 16, NULL);
    r3_delta(before, wanted, NULL, 16, 0, 0);
    puts("PASS R3 restored_heat native_split real_collapse exact_transfer old_PFN_clear full_histogram");
}

struct r3_workload {
    unsigned char *area;
    atomic_bool stop;
};

static atomic_ulong r3_sink;

static void r3_work(unsigned char *area, unsigned pages, unsigned long iterations)
{
    unsigned long sum = 0;
    for (unsigned long i = 0, page = 0; i < iterations; i++) {
        page = (page + 4093) & (pages - 1);
        unsigned char *address = area + page * PAGE + 173;
        __asm__ volatile("clflush (%0); mfence" :: "r"(address) : "memory");
        sum += *(volatile unsigned char *)address;
    }
    atomic_store_explicit(&r3_sink, sum, memory_order_relaxed);
}

static void *r3_worker(void *argument)
{
    struct r3_workload *work = argument;
    while (!atomic_load_explicit(&work->stop, memory_order_relaxed))
        r3_work(work->area, 512, 32768);
    return NULL;
}

static void r3_hardware(uint64_t isolated)
{
    phase = "genuine PEBS continues through remote data lifecycle and cooling";
    command(TRACK "control", "reset\n");
    command(TRACK "control", "capacity %llu %llu\n", test_ram_bytes(), test_ram_bytes());
    command(TRACK "control", "sampling fixed 512\n");
    struct r3_workload work = {.area = mapping(PMD, 0, 251)};
    atomic_init(&work.stop, false);
    command(TRACK "control", "enable\n");
    pthread_t thread;
    check(!pthread_create(&thread, NULL, r3_worker, &work), "start concurrent real PEBS memory workload");
    for (unsigned round = 0; round < 6; round++) {
        struct c4_object obj = c4_mapping(4, 253 + round);
        if (round == 3)
            command(TRACK "control", "cooling fixed 1024\n");
        r3_work(obj.area, obj.pages, 1UL << 20);
        command(TRACK "control", "drain\n");
        unsigned values[512];
        r3_range(obj.frames[0], obj.pages, values);
        uint64_t heat = 0;
        for (unsigned i = 0; i < obj.pages; i++) heat += values[i];
        if (round < 3)
            check(heat > 16, "hardware samples give the source nonzero measured heat");
        struct r3_stats before = r3_stats();
        r3_prepare(&obj, isolated);
        hsubmit(&obj);
        command(TRACK "control", "drain\n");
        r3_equal(obj.frames[0], obj.pages, NULL);
        check(r3_stats().out == before.out + obj.pages,
              "sampling cannot reheat a successfully retired physical range");
        uint64_t loads = hvalue("load_success");
        if (round & 1) {
            volatile unsigned char value = obj.area[173];
            check(value == pattern(173, obj.salt), "a real demand fault reads the persisted source byte");
        } else {
            command(SHADOW "control", "restore %" PRIu64 "\n", obj.entry.token);
        }
        check(hvalue("load_success") == loads + 1, "each explicit or demand restore completes one backend read");
        r3_range(obj.frames[0], obj.pages, values);
        for (unsigned i = 0; i < obj.pages; i++) {
            /* Only the first page is accessed by the initiating demand
             * load. That new access may legitimately add a PEBS sample. */
            if ((round & 1) && !i)
                continue;
            check(round < 3 ? values[i] == 1 : values[i] <= 1,
                  "unaccessed readback starts at phi and only subsequent cooling may lower it");
        }
        check(r3_stats().in == before.in + obj.pages, "PEBS-enabled readback initializes every live page once");
        content(obj.area, PMD, 0, obj.salt);
        same_pfns(obj.area, 512, obj.frames);
        c4_unmap(&obj);
        r3_idle(isolated);
        r3_equal(obj.frames[0], obj.pages, NULL);
    }
    atomic_store_explicit(&work.stop, true, memory_order_relaxed);
    check(!pthread_join(thread, NULL), "stop independent sampling workload");
    command(TRACK "control", "disable\n");
    struct r3_stats final = r3_stats();
    check(final.hardware > 128 && !final.synthetic && final.cooling > 0,
          "real PEBS, no injected samples, and actual cooling occur during data retirement/readback");
    check(!munmap(work.area, PMD), "release PEBS workload");
    r3_idle(isolated);
    printf("PASS R3 genuine_PEBS hardware_samples=%" PRIu64 " cooling_epochs=%" PRIu64
           " rounds=6 explicit_and_demand_readback stale_sample_exclusion free_clear histogram\n",
           final.hardware, final.cooling);
}

int main(int argc, char **argv)
{
    bool pebs = argc == 1;
    check(pebs || (argc == 2 && !strcmp(argv[1], "--without-pebs")), "supported test arguments");
    setvbuf(stdout, NULL, _IOLBF, 0);
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "open real PFN diagnostics");
    struct sigaction action = {.sa_handler = permission_handler};
    sigemptyset(&action.sa_mask);
    check(!sigaction(SIGSEGV, &action, NULL) && !sigaction(SIGBUS, &action, NULL),
          "install native failed-read signal probes");
    command(POLICY "control", "disable\n");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "reset\n");
    command(TRACK "control", "cooling fixed 1099511627776\n");
    command(TRACK "control", "capacity 65536 %llu\n", test_ram_bytes());
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    command(MANAGER "control", "selector linux_lru\n");
    command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
    check(hvalue("registered") == 1, "real Hermit persistence backend is registered");
    uint64_t isolated = isolated_anon();
    r3_idle(isolated);
    r3_reference = mapping(PMD, 0, 219);
    /* mapping() deliberately requests MADV_HUGEPAGE even for order 0.
     * Later order-9 policy changes must not let native khugepaged replace
     * these independent reference PFNs and clear their seeded heat. */
    check(!madvise(r3_reference, PMD, MADV_NOHUGEPAGE),
          "reference base pages remain ineligible for native huge-page collapse");
    snapshot(r3_reference, 64, r3_reference_pfns);
    for (unsigned i = 0; i < 64; i++)
        command(TRACK "inject", "0x%lx 32\n", (unsigned long)(r3_reference + i * PAGE));
    r3_reference_check();
    check(r3_stats().phi == 63, "reference region fixes phi independently of the lifecycle target");
    r3_fast_and_save_failure(isolated);
    r3_round_trip(isolated, 0, false, false);
    r3_round_trip(isolated, 4, false, true);
    r3_round_trip(isolated, 4, true, false);
    r3_round_trip(isolated, 9, false, false);
    r3_unmap_remote(isolated);
    r3_split_collapse(isolated);
    check(!munmap(r3_reference, PMD), "release deterministic reference region");
    r3_idle(isolated);
    if (pebs) r3_hardware(isolated);
    else puts("SKIP R3 genuine_PEBS explicitly_disabled_by_without_pebs");
    command(MANAGER "control", "clear_target\n");
    close(flags_fd);
    close(pagemap_fd);
    printf("PASS CHAMELEON_R3 checks=%u mode=%s\n", checks, pebs ? "PEBS_AND_DATA" : "DETERMINISTIC_ONLY");
    return 0;
}
