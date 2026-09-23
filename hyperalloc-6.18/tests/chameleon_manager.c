// SPDX-License-Identifier: GPL-2.0-only
/* Real C2 MM tests.  No simulated folios, PFNs, or manager completions. */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kernel-page-flags.h>
#include <linux/types.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#include "../linux/mm/gup_test.h"

#define TRACK "/sys/kernel/debug/chameleon/"
#define MANAGER "/sys/kernel/debug/chameleon_mm/"
#define THP "/sys/kernel/mm/transparent_hugepage/"
#define PAGE 4096UL
#define PMD (512UL * PAGE)
#define PFN_MASK ((1ULL << 55) - 1)
#define MAX_PAGES 1024
#define TEXT_SIZE (128UL * 1024)

#ifndef CHAMELEON_TEST_STAGE
#define CHAMELEON_TEST_STAGE "C2"
#endif

static unsigned checks;
static const char *phase = "initialization";
static int pagemap_fd, flags_fd;
static sigjmp_buf protection_jump;
static volatile sig_atomic_t protection_armed, protection_signal;

static void check(int good, const char *why)
{
    if (!good) {
        fprintf(stderr, "FAIL " CHAMELEON_TEST_STAGE " phase=%s check=%u %s errno=%d (%s)\n",
                phase, checks + 1, why, errno, strerror(errno));
        exit(1);
    }
    checks++;
}

static int command_v(const char *path, const char *format, va_list ap)
{
    char text[512];
    int n = vsnprintf(text, sizeof(text), format, ap);
    check(n > 0 && n < (int)sizeof(text), "control command length");
    int fd = open(path, O_WRONLY);
    check(fd >= 0, path);
    ssize_t wrote = write(fd, text, n);
    int error = errno;
    close(fd);
    if (wrote < 0)
        return -error;
    check(wrote == n, "complete debugfs command write");
    return 0;
}

static void command(const char *path, const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    int result = command_v(path, format, ap);
    va_end(ap);
    if (result) {
        fprintf(stderr, "Command failed path=%s format=%s result=%d\n", path, format, result);
        errno = -result;
    }
    check(!result, "debugfs/sysfs command succeeds");
}

static int manager_try(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    int result = command_v(MANAGER "control", format, ap);
    va_end(ap);
    return result;
}

static char *read_text(const char *path)
{
    char *text = calloc(1, TEXT_SIZE);
    check(text != NULL, "allocate text buffer");
    int fd = open(path, O_RDONLY);
    check(fd >= 0, path);
    size_t used = 0;
    for (;;) {
        ssize_t n = read(fd, text + used, TEXT_SIZE - 1 - used);
        check(n >= 0, "read debugfs text");
        if (!n)
            break;
        used += n;
        check(used < TEXT_SIZE - 1, "debugfs output fits buffer");
    }
    close(fd);
    return text;
}

static uint64_t field(const char *text, const char *key)
{
    size_t size = strlen(key);
    for (const char *line = text; line && *line; ) {
        if (!strncmp(line, key, size) && (line[size] == ' ' || line[size] == '=')) {
            const char *start = line + size + 1;
            char *end;
            errno = 0;
            uint64_t value = strtoull(start, &end, 0);
            check(end != start && errno != ERANGE, "numeric stats field");
            while (*end == ' ' || *end == '\t')
                end++;
            check(*end == '\n' || !*end, "stats field has no extra suffix");
            return value;
        }
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    fprintf(stderr, "Missing field %s:\n%s", key, text);
    exit(1);
}

struct heat_stats {
    uint64_t bins[16], hardware, synthetic, cooling;
};

static struct heat_stats heat_stats(void)
{
    char *text = read_text(TRACK "stats");
    struct heat_stats stats = {0};
    uint64_t sum = 0;
    for (unsigned i = 0; i < 16; i++) {
        char key[24];
        snprintf(key, sizeof(key), "bin%u", i);
        stats.bins[i] = field(text, key);
        sum += stats.bins[i];
    }
    check(sum == field(text, "managed_pages"), "C1 histogram conserves managed pages");
    check(sum == field(text, "histogram_sum"), "C1 exported histogram sum agrees");
    stats.hardware = field(text, "hardware_samples");
    stats.synthetic = field(text, "synthetic_samples");
    stats.cooling = field(text, "cooling_epochs");
    free(text);
    return stats;
}

static void heat_unchanged(const struct heat_stats *before)
{
    struct heat_stats after = heat_stats();
    check(!memcmp(before->bins, after.bins, sizeof(after.bins)), "MM conversion preserves all histogram bins");
    check(before->hardware == after.hardware && before->synthetic == after.synthetic &&
          before->cooling == after.cooling, "MM conversion does not manufacture samples or cooling");
}

static uint64_t pfn(unsigned char *address)
{
    uint64_t entry;
    check(pread(pagemap_fd, &entry, sizeof(entry), (uintptr_t)address / PAGE * 8) == 8,
          "read pagemap");
    check((entry >> 63) && !(entry & (1ULL << 62)) && (entry & PFN_MASK), "resident visible guest PFN");
    return entry & PFN_MASK;
}

static uint64_t page_flags(uint64_t frame)
{
    uint64_t flags;
    check(pread(flags_fd, &flags, sizeof(flags), frame * 8) == 8, "read physical page flags");
    return flags;
}

static void snapshot_pfns(unsigned char *area, unsigned nr, uint64_t *frames)
{
    check(nr <= MAX_PAGES, "PFN snapshot bounds");
    for (unsigned i = 0; i < nr; i++)
        frames[i] = pfn(area + i * PAGE);
}

static void same_pfns(unsigned char *area, unsigned first, unsigned end, const uint64_t *frames)
{
    for (unsigned i = first; i < end; i++)
        check(pfn(area + i * PAGE) == frames[i], "unchanged neighbour/source PFN");
}

/* Validate the physical compound head and every tail, not merely contiguity
 * or a PMD mapping. Base-page segments must have neither compound flag. */
static void folio(unsigned char *area, unsigned order)
{
    unsigned pages = 1U << order;
    uint64_t head = pfn(area);
    check(!(head & (pages - 1)), "real folio PFN alignment");
    for (unsigned i = 0; i < pages; i++) {
        uint64_t frame = pfn(area + i * PAGE), flags = page_flags(frame);
        check(frame == head + i, "real folio physical continuity");
        check(flags & (1ULL << KPF_ANON), "candidate is anonymous memory");
        if (!order) {
            check(!(flags & ((1ULL << KPF_COMPOUND_HEAD) | (1ULL << KPF_COMPOUND_TAIL) |
                             (1ULL << KPF_THP))), "actual base page, not a compound fragment");
        } else {
            check(flags & (1ULL << KPF_THP), "actual anonymous huge folio");
            check(!!(flags & (1ULL << KPF_COMPOUND_HEAD)) == !i, "exact physical folio head");
            check(!!(flags & (1ULL << KPF_COMPOUND_TAIL)) == !!i, "exact physical folio tails");
        }
    }
}

static unsigned char pattern(size_t offset, unsigned salt)
{
    return (unsigned char)((offset * 131U) ^ (offset >> 7) ^ (salt * 29U));
}

static void check_content(unsigned char *area, size_t bytes, unsigned salt)
{
    for (size_t i = 0; i < bytes; i++) {
        if (area[i] != pattern(i, salt)) {
            fprintf(stderr, "Data mismatch offset=%zu got=%u expected=%u\n", i, area[i], pattern(i, salt));
            check(0, "all bytes preserved");
        }
    }
    check(1, "all bytes preserved");
}

static void policy(unsigned order)
{
    DIR *dir = opendir(THP);
    check(dir != NULL, "open THP policy directory");
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        unsigned size;
        char extra, path[512];
        if (sscanf(entry->d_name, "hugepages-%ukB%c", &size, &extra) != 1)
            continue;
        snprintf(path, sizeof(path), THP "%s/enabled", entry->d_name);
        if (!access(path, F_OK))
            command(path, "%s\n", order && size == (4U << order) ? "always" : "never");
    }
    closedir(dir);
}

static unsigned char *mapping(size_t bytes, unsigned order, unsigned salt)
{
    policy(order);
    unsigned char *raw = mmap(NULL, bytes + PMD, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    check(raw != MAP_FAILED, "allocate aligned anonymous mapping");
    size_t prefix = (-(uintptr_t)raw) & (PMD - 1);
    if (prefix)
        check(!munmap(raw, prefix), "trim alignment prefix");
    raw += prefix;
    check(!munmap(raw + bytes, PMD - prefix), "trim alignment suffix");
    /* All mTHP fault orders are selected above. VM_HUGEPAGE keeps the VMA
     * eligible for the explicit C2 collapse helper even for base sources. */
    check(!madvise(raw, bytes, MADV_HUGEPAGE), "mark private VMA for huge pages");
    for (size_t i = 0; i < bytes; i++)
        raw[i] = pattern(i, salt);
    command(TRACK "control", "drain\n");
    for (unsigned i = 0; i < bytes / PAGE; i += 1U << order)
        folio(raw + i * PAGE, order);
    return raw;
}

static void reset_tracking(void)
{
    command(TRACK "control", "disable\n");
    command(TRACK "control", "capacity %lu %lu\n", 2UL * 1024 * 1024 * 1024,
            2UL * 1024 * 1024 * 1024);
    command(TRACK "control", "reset\n");
    (void)heat_stats();
}

static void inject(unsigned char *address, unsigned value)
{
    if (value)
        command(TRACK "inject", "0x%lx %u\n", (unsigned long)address + 173, value);
}

static void read_counters(uint64_t start, unsigned nr, unsigned *values)
{
    char request[80], *text = calloc(1, TEXT_SIZE);
    check(text != NULL, "allocate counter buffer");
    int fd = open(TRACK "range", O_RDWR);
    check(fd >= 0, "open real PFN counters");
    int length = snprintf(request, sizeof(request), "%" PRIu64 " %u\n", start, nr);
    check(write(fd, request, length) == length, "select counter range");
    ssize_t n = pread(fd, text, TEXT_SIZE - 1, 0);
    check(n > 0, "read counter range");
    close(fd);
    char *line = text;
    for (unsigned i = 0; i < nr; i++) {
        uint64_t frame;
        unsigned value;
        check(line && sscanf(line, "%" SCNu64 " %u", &frame, &value) == 2,
              "parse PFN counter");
        check(frame == start + i && value <= 65535, "counter identity and bounds");
        values[i] = value;
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    free(text);
}

static void mapped_counters(const uint64_t *frames, unsigned nr, unsigned *values)
{
    for (unsigned first = 0; first < nr; ) {
        unsigned end = first + 1;
        while (end < nr && frames[end] == frames[end - 1] + 1)
            end++;
        read_counters(frames[first], end - first, values + first);
        first = end;
    }
}

static void target(unsigned char *area, size_t bytes)
{
    command(MANAGER "control", "target %d 0x%lx %zu\n", getpid(), (unsigned long)area, bytes);
}

static void release_mapping(unsigned char *area, size_t bytes)
{
    command(MANAGER "control", "clear_target\n");
    check(!munmap(area, bytes), "release mapping");
    command(TRACK "control", "drain\n");
    (void)heat_stats();
}

static void verify_segments(unsigned char *area, unsigned pages, const unsigned *orders,
                            const uint64_t *original)
{
    unsigned next = 0;
    while (next < pages) {
        unsigned order = orders[next], count = 1U << order;
        check(order != 1 && order <= 9 && count <= pages - next && !(next & (count - 1)),
              "supported aligned complete mixed-order partition");
        for (unsigned i = next; i < next + count; i++)
            check(orders[i] == order, "one expected order throughout physical folio");
        folio(area + next * PAGE, order);
        next += count;
    }
    same_pfns(area, 0, pages, original);
}

static void verify_last(unsigned pages, const unsigned *orders, int changed, int allow_empty)
{
    char *text = read_text(MANAGER "last");
    check(field(text, "split_changed") == (uint64_t)changed, "manager reports actual split result");
    unsigned cover[MAX_PAGES] = {0}, segments = 0;
    for (char *line = text; line && *line; ) {
        unsigned offset, order, dominant;
        if (sscanf(line, "segment offset=%u order=%u dominant=%u", &offset, &order, &dominant) == 3) {
            check(order <= 9 && order != 1 && dominant <= 1 && offset < pages &&
                  (1U << order) <= pages - offset && !(offset & ((1U << order) - 1)),
                  "reported real HHH segment bounds");
            for (unsigned i = offset; i < offset + (1U << order); i++) {
                check(!cover[i]++, "reported HHH segments do not overlap");
                check(orders[i] == order, "reported HHH agrees with independent expected layout");
            }
            segments++;
        }
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    if (!allow_empty || segments)
        for (unsigned i = 0; i < pages; i++)
            check(cover[i] == 1, "reported HHH partition covers original folio");
    free(text);
}

/* Closed-form expected buddy siblings for a single hot base page. This
 * checks actual physical results, including order-1 normalization. */
static void hotspot_orders(unsigned pages, unsigned hot, unsigned *orders)
{
    for (unsigned i = 0; i < pages; i++) {
        unsigned difference = i ^ hot, order = 0;
        while (difference >>= 1)
            order++;
        orders[i] = order == 1 ? 0 : order;
    }
}

static void hhh_balanced(void)
{
    phase = "HHH balanced and delta boundary";
    for (unsigned fixture = 0; fixture < 4; fixture++) {
        reset_tracking();
        unsigned char *area = mapping(PMD, 9, fixture + 1);
        uint64_t original[512];
        unsigned expected[512], heat[512], after[512];
        snapshot_pfns(area, 512, original);
        for (unsigned i = 0; i < 512; i++) {
            unsigned count = fixture == 0 ? 0 : fixture == 1 ? 8 :
                             i < 256 ? (fixture == 2 ? 7 : 8) : 3;
            inject(area + i * PAGE, count);
            expected[i] = fixture == 3 ? 8 : 9;
        }
        mapped_counters(original, 512, heat);
        struct heat_stats before = heat_stats();
        target(area, PMD);
        command(MANAGER "control", "split %d 0x%lx\n", getpid(), (unsigned long)area);
        verify_segments(area, 512, expected, original);
        verify_last(512, expected, fixture == 3, fixture == 0);
        mapped_counters(original, 512, after);
        check(!memcmp(heat, after, sizeof(heat)), "HHH preserves per-base-page heat");
        check_content(area, PMD, fixture + 1);
        heat_unchanged(&before);
        printf("HHH fixture=%s actual_segments=%u\n",
               fixture == 0 ? "zero" : fixture == 1 ? "uniform" : fixture == 2 ? "exact_0.7" : "above_0.7",
               fixture == 3 ? 2 : 1);
        release_mapping(area, PMD);
    }
    puts("PASS C2 hhh zero uniform exact_delta_boundary above_delta_boundary");
}

static void split_orders(void)
{
    phase = "HHH real mixed folios";
    reset_tracking();
    unsigned char *base = mapping(PMD, 0, 19);
    uint64_t base_frames[512];
    unsigned base_order = 0;
    snapshot_pfns(base, 512, base_frames);
    inject(base, 8);
    target(base, PMD);
    command(MANAGER "control", "split %d 0x%lx\n", getpid(), (unsigned long)base);
    verify_segments(base, 1, &base_order, base_frames);
    verify_last(1, &base_order, 0, 0);
    same_pfns(base, 1, 512, base_frames);
    check_content(base, PMD, 19);
    release_mapping(base, PMD);
    for (unsigned order = 2; order <= 9; order++) {
        for (unsigned side = 0; side < 2; side++) {
            reset_tracking();
            unsigned salt = 20 + order * 2 + side, pages = 1U << order;
            unsigned char *area = mapping(PMD, order, salt);
            uint64_t original[512];
            unsigned expected[512], before_heat[512], after_heat[512];
            snapshot_pfns(area, 512, original);
            unsigned hot = side ? pages - 1 : 0;
            inject(area + hot * PAGE, pages * 2);
            hotspot_orders(pages, hot, expected);
            mapped_counters(original, pages, before_heat);
            struct heat_stats before = heat_stats();
            target(area, PMD);
            command(MANAGER "control", "split %d 0x%lx\n", getpid(), (unsigned long)area);
            verify_segments(area, pages, expected, original);
            verify_last(pages, expected, 1, 0);
            for (unsigned i = pages; i < 512; i += pages)
                folio(area + i * PAGE, order);
            same_pfns(area, pages, 512, original);
            mapped_counters(original, pages, after_heat);
            check(!memcmp(before_heat, after_heat, pages * sizeof(*before_heat)),
                  "mixed split keeps each base-page counter");
            check_content(area, PMD, salt);
            heat_unchanged(&before);
            printf("SPLIT source_order=%u dominant=%s pages=%u real_PFNs_preserved=1\n",
                   order, side ? "right" : "left", pages);
            release_mapping(area, PMD);
        }
    }
    puts("PASS C2 split orders=2,3,4,5,6,7,8,9 mixed_real_orders=0,2,3,4,5,6,7,8 full_data PFN heat");
}

static void protection_handler(int sig)
{
    if (!protection_armed)
        _exit(128 + sig);
    protection_signal = sig;
    siglongjmp(protection_jump, 1);
}

static void assert_read_only(unsigned char *address)
{
    protection_signal = 0;
    protection_armed = 1;
    if (!sigsetjmp(protection_jump, 1))
        *(volatile unsigned char *)address ^= 1;
    protection_armed = 0;
    check(protection_signal == SIGSEGV, "read-only mapping still rejects a real user write");
}

static void seed_heat(unsigned char *area, unsigned pages, unsigned *values)
{
    for (unsigned i = 0; i < pages; i++) {
        values[i] = 7 + i % 3;
        inject(area + i * PAGE, values[i]);
    }
}

static void *fragment_base_sources(unsigned char *area, unsigned offset, unsigned salt)
{
    const size_t bytes = 256 * PAGE;
    unsigned char *other = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
                                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    check(other != MAP_FAILED, "allocate interleaved fragmentation mapping");
    check(!madvise(other, bytes, MADV_NOHUGEPAGE), "fragment using real base-page faults");
    int fragmented = 0;
    for (unsigned attempt = 0; attempt < 8 && !fragmented; attempt++) {
        check(!madvise(area + offset * PAGE, 4 * PAGE, MADV_DONTNEED), "refault source base pages");
        command(TRACK "control", "drain\n");
        for (unsigned page = 0; page < 4; page++) {
            for (size_t byte = 0; byte < PAGE; byte++) {
                size_t at = (offset + page) * PAGE + byte;
                area[at] = pattern(at, salt);
            }
            /* Hold intervening physical allocations until collapse ends. */
            memset(other + (attempt * 16 + page) * PAGE, 0x5a, PAGE);
        }
        for (unsigned page = 1; page < 4; page++)
            fragmented |= pfn(area + (offset + page) * PAGE) !=
                          pfn(area + (offset + page - 1) * PAGE) + 1;
    }
    check(fragmented, "collapse fixture has genuinely noncontiguous source PFNs");
    return other;
}

static void collapse_orders(void)
{
    phase = "real collapse and heat transfer";
    for (unsigned order = 2; order <= 9; order++) {
        reset_tracking();
        unsigned source_order = order == 2 ? 0 : order - 1;
        unsigned pages = 1U << order, salt = 60 + order;
        unsigned char *area = mapping(PMD, source_order, salt);
        /* Put sub-PMD destinations away from the first page so both sides
         * have real neighbour mappings under the same temporarily removed PMD. */
        unsigned offset = order == 9 ? 0 : pages;
        unsigned char *destination = area + offset * PAGE;
        void *fragment = order == 2 ? fragment_base_sources(area, offset, salt) : NULL;
        uint64_t original[512], new_frames[512];
        unsigned wanted[512], actual[512], cleared[512];
        snapshot_pfns(area, 512, original);
        seed_heat(destination, pages, wanted);
        struct heat_stats before = heat_stats();
        int readonly = order == 4 || order == 9;
        if (readonly) {
            check(!mprotect(area, PMD, PROT_READ), "make entire VMA read-only");
            assert_read_only(destination + 173);
        }
        target(area, PMD);
        command(MANAGER "control", "collapse %d 0x%lx %u\n", getpid(),
                (unsigned long)destination, order);
        folio(destination, order);
        snapshot_pfns(destination, pages, new_frames);
        for (unsigned i = 0; i < pages; i++)
            for (unsigned j = 0; j < pages; j++)
                check(new_frames[i] != original[offset + j], "collapse allocates a distinct destination folio");
        same_pfns(area, 0, offset, original);
        same_pfns(area, offset + pages, 512, original);
        mapped_counters(new_frames, pages, actual);
        check(!memcmp(wanted, actual, pages * sizeof(*wanted)), "collapse transfers every base-page heat value");
        mapped_counters(original + offset, pages, cleared);
        for (unsigned i = 0; i < pages; i++)
            check(!cleared[i], "collapse clears old physical frame counters");
        check_content(area, PMD, salt);
        if (readonly) {
            assert_read_only(destination + 173);
            check(!mprotect(area, PMD, PROT_READ | PROT_WRITE), "restore writable VMA for teardown");
        }
        heat_unchanged(&before);
        printf("COLLAPSE source_order=%u target_order=%u pages=%u readonly=%d destination_pfn=%" PRIu64 "\n",
               source_order, order, pages, readonly, new_frames[0]);
        if (fragment)
            check(!munmap(fragment, 256 * PAGE), "release fragmentation allocation");
        release_mapping(area, PMD);
    }
    puts("PASS C2 collapse targets=2,3,4,5,6,7,8,9 physical_folios full_data neighbours readonly per_page_heat");
}

static void expect_reject(unsigned char *address, unsigned order, const char *reason, int exact_errno)
{
    int result = manager_try("collapse %d 0x%lx %u\n", getpid(), (unsigned long)address, order);
    if (exact_errno)
        check(result == -exact_errno, reason);
    else
        check(result == -EINVAL || result == -EBUSY || result == -EAGAIN ||
              result == -EFAULT || result == -EOPNOTSUPP || result == -EPERM, reason);
    printf("REJECT case=%s errno=%d\n", reason, -result);
}

static void collapse_failures(void)
{
    phase = "collapse failure rollback";
    for (unsigned mode = 1; mode <= 2; mode++) {
        reset_tracking();
        unsigned char *area = mapping(PMD, 3, 90 + mode);
        uint64_t original[512];
        unsigned heat[16], observed[16];
        snapshot_pfns(area, 512, original);
        seed_heat(area, 16, heat);
        struct heat_stats before = heat_stats();
        target(area, PMD);
        command(MANAGER "control", "fail_collapse %u\n", mode);
        expect_reject(area, 4, mode == 1 ? "allocation_failure" : "copy_after_4KiB_failure",
                      mode == 1 ? ENOMEM : EIO);
        same_pfns(area, 0, 512, original);
        for (unsigned i = 0; i < 512; i += 8)
            folio(area + i * PAGE, 3);
        mapped_counters(original, 16, observed);
        check(!memcmp(heat, observed, sizeof(heat)), "failed collapse keeps source heat");
        check_content(area, PMD, 90 + mode);
        heat_unchanged(&before);
        /* Both faults are one-shot; successful retry proves the restored
         * page table and folio references are usable for another conversion. */
        command(MANAGER "control", "collapse %d 0x%lx 4\n", getpid(), (unsigned long)area);
        folio(area, 4);
        check_content(area, PMD, 90 + mode);
        release_mapping(area, PMD);
    }

    for (unsigned which = 0; which < 4; which++) {
        reset_tracking();
        unsigned salt = 100 + which, source_order = which == 2 ? 5 : 3;
        unsigned char *area = mapping(PMD, source_order, salt);
        uint64_t original[512];
        snapshot_pfns(area, 512, original);
        unsigned heat[16];
        seed_heat(area, 16, heat);
        target(area, PMD);
        int pin_fd = -1, child_pipe[2] = {-1, -1};
        pid_t child = -1;
        if (which == 0) {
            pin_fd = open("/sys/kernel/debug/gup_test", O_RDWR);
            check(pin_fd >= 0, "CONFIG_GUP_TEST real long-term pin interface");
            struct pin_longterm_test pin = {
                .addr = (uintptr_t)area, .size = 16 * PAGE,
                .flags = PIN_LONGTERM_TEST_FLAG_USE_WRITE | PIN_LONGTERM_TEST_FLAG_USE_FAST,
            };
            check(!ioctl(pin_fd, PIN_LONGTERM_TEST_START, &pin), "hold real FOLL_PIN references");
            expect_reject(area, 4, "longterm_pin", 0);
        } else if (which == 1) {
            check(!pipe(child_pipe), "create shared-folio child pipe");
            child = fork();
            check(child >= 0, "fork shared private-anonymous source");
            if (!child) {
                char token;
                close(child_pipe[1]);
                _exit(read(child_pipe[0], &token, 1) == 1 ? 0 : 1);
            }
            close(child_pipe[0]);
            expect_reject(area, 4, "fork_shared_source", 0);
        } else if (which == 2) {
            expect_reject(area, 4, "source_folio_crosses_target_range", 0);
        } else {
            check(!munmap(area + 8 * PAGE, 8 * PAGE), "unmap half of requested collapse range");
            expect_reject(area, 4, "partially_unmapped_range", 0);
        }
        if (which == 3) {
            same_pfns(area, 0, 8, original);
            same_pfns(area, 16, 512, original);
            for (size_t i = 0; i < PMD; i++)
                if (i < 8 * PAGE || i >= 16 * PAGE)
                    check(area[i] == pattern(i, salt), "partial-unmap rejection preserves surviving bytes");
        } else {
            same_pfns(area, 0, 512, original);
            check_content(area, PMD, salt);
        }
        if (pin_fd >= 0) {
            check(!ioctl(pin_fd, PIN_LONGTERM_TEST_STOP), "release real long-term pins");
            close(pin_fd);
        }
        if (child > 0) {
            check(write(child_pipe[1], "G", 1) == 1, "release sharing child");
            close(child_pipe[1]);
            int status;
            check(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status),
                  "sharing child exits cleanly");
        }
        release_mapping(area, PMD);
    }
    puts("PASS C2 lifecycle real_pin fork_shared partial_folio unmap alloc_failure copy_failure rollback_retry");
}

static void background(void)
{
    phase = "asynchronous manager worker";
    reset_tracking();
    unsigned char *area = mapping(PMD, 4, 115);
    uint64_t original[512];
    unsigned expected[16];
    snapshot_pfns(area, 512, original);
    inject(area, 32);
    hotspot_orders(16, 0, expected);
    target(area, 16 * PAGE);
    command(MANAGER "control", "age\n");
    command(MANAGER "control", "enable\n");
    unsigned polls;
    for (polls = 0; polls < 150; polls++) {
        uint64_t entry, flags;
        ssize_t n = pread(pagemap_fd, &entry, 8, (uintptr_t)area / PAGE * 8);
        if (n == 8 && (entry >> 63) && (entry & PFN_MASK) &&
            pread(flags_fd, &flags, 8, (entry & PFN_MASK) * 8) == 8 &&
            !(flags & ((1ULL << KPF_COMPOUND_HEAD) | (1ULL << KPF_COMPOUND_TAIL))))
            break;
        usleep(100000);
    }
    command(MANAGER "control", "disable\n");
    check(polls < 150, "five-second background worker performs a real split");
    verify_segments(area, 16, expected, original);
    same_pfns(area, 16, 512, original);
    check_content(area, PMD, 115);
    release_mapping(area, PMD);
    printf("BACKGROUND split_polls=%u\n", polls);

    reset_tracking();
    area = mapping(PMD, 0, 116);
    snapshot_pfns(area, 512, original);
    unsigned wanted[4], observed[4];
    seed_heat(area, 4, wanted);
    struct heat_stats before = heat_stats();
    target(area, 4 * PAGE);
    command(MANAGER "control", "age\n");
    command(MANAGER "control", "enable\n");
    for (polls = 0; polls < 150; polls++) {
        uint64_t entry, flags;
        ssize_t n = pread(pagemap_fd, &entry, 8, (uintptr_t)area / PAGE * 8);
        if (n == 8 && (entry >> 63) && (entry & PFN_MASK) &&
            pread(flags_fd, &flags, 8, (entry & PFN_MASK) * 8) == 8 &&
            (flags & (1ULL << KPF_COMPOUND_HEAD)) && (flags & (1ULL << KPF_THP)))
            break;
        usleep(100000);
    }
    command(MANAGER "control", "disable\n");
    check(polls < 150, "five-second worker performs real uniform-hot collapse");
    folio(area, 2);
    read_counters(pfn(area), 4, observed);
    check(!memcmp(wanted, observed, sizeof(wanted)), "background collapse transfers per-page heat");
    same_pfns(area, 4, 512, original);
    check_content(area, PMD, 116);
    heat_unchanged(&before);
    release_mapping(area, PMD);
    printf("PASS C2 background asynchronous_real_split uniform_hot_real_collapse collapse_polls=%u\n", polls);
}

struct candidate {
    uint64_t token, pfn;
    unsigned long address;
    unsigned order;
    int pid;
};

static unsigned candidates(struct candidate *out)
{
    char *text = read_text(MANAGER "candidates");
    unsigned nr = 0;
    for (char *line = text; line && *line; ) {
        struct candidate entry;
        if (sscanf(line, "candidate token=%" SCNu64 " pid=%d address=0x%lx pfn=%" SCNu64 " order=%u",
                   &entry.token, &entry.pid, &entry.address, &entry.pfn, &entry.order) == 5) {
            check(nr < 128, "bounded candidate count");
            out[nr++] = entry;
        }
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    free(text);
    return nr;
}

static uint64_t isolated_anon(void)
{
    char *text = read_text("/proc/vmstat");
    uint64_t pages = field(text, "nr_isolated_anon");
    free(text);
    return pages;
}

static void selector(void)
{
    phase = "real order queues and cost selector";
    reset_tracking();
    unsigned char *area = mapping(2 * PMD, 9, 120);
    uint64_t original[1024];
    snapshot_pfns(area, 1024, original);
    inject(area, 1024);
    target(area, 2 * PMD);
    command(MANAGER "control", "split %d 0x%lx\n", getpid(), (unsigned long)area);
    unsigned orders[512];
    hotspot_orders(512, 0, orders);
    verify_segments(area, 512, orders, original);
    folio(area + PMD, 9);
    reset_tracking();
    command(MANAGER "control", "age\n");
    for (unsigned i = 0; i < 1024; ) {
        unsigned order = i < 512 ? orders[i] : 9;
        check(page_flags(original[i]) & (1ULL << KPF_LRU), "cold fixture is on real LRU");
        check(!(page_flags(original[i]) & (1ULL << KPF_ACTIVE)), "age moves zero-heat folio to inactive");
        i += 1U << order;
    }
    /* Common cost is (6400 + 128*4*10)/K = 2880. With these
     * reclaim costs the first four nonempty orders are 2,9,8,7. */
    for (unsigned order = 0; order <= 9; order++) {
        if (order == 1)
            continue;
        uint64_t cost = order == 2 ? 0 : order == 9 ? 512000 : 1000000ULL << order;
        command(MANAGER "control", "cost %u %" PRIu64 "\n", order, cost);
    }
    command(MANAGER "control", "batch 4 6400 128 4 10\n");
    uint64_t isolated_before = isolated_anon(), selected_pages = 0;
    command(MANAGER "control", "select 4\n");
    struct candidate held[128], empty[128];
    unsigned nr = candidates(held);
    check(nr == 4, "batch K counts isolated folios, not base pages");
    const unsigned expected[] = {2, 9, 8, 7};
    for (unsigned i = 0; i < nr; i++) {
        selected_pages += 1UL << held[i].order;
        check(held[i].order == expected[i], "known cost model chooses expected nonempty order");
        check(held[i].pid == getpid() && held[i].address >= (uintptr_t)area &&
              held[i].address + (PAGE << held[i].order) <= (uintptr_t)area + 2 * PMD,
              "candidate owns an actual mapping in target mm");
        check(held[i].pfn == pfn((unsigned char *)held[i].address), "candidate GPA matches live pagemap");
        folio((unsigned char *)held[i].address, held[i].order);
        check(!(page_flags(held[i].pfn) & (1ULL << KPF_LRU)), "candidate genuinely isolated from Linux LRU");
        for (unsigned j = 0; j < i; j++)
            check(held[j].pfn != held[i].pfn && held[j].token != held[i].token,
                  "candidate identity is unique");
        printf("SELECT token=%" PRIu64 " order=%u pfn=%" PRIu64 " address=0x%lx\n",
               held[i].token, held[i].order, held[i].pfn, held[i].address);
    }
    check(isolated_anon() == isolated_before + selected_pages,
          "NR_ISOLATED_ANON counts every genuinely isolated base page");
    command(MANAGER "control", "putback\n");
    command(TRACK "control", "drain\n");
    check(isolated_anon() == isolated_before, "putback restores original NR_ISOLATED_ANON");
    check(!candidates(empty), "putback releases every held candidate descriptor");
    for (unsigned i = 0; i < nr; i++)
        check(page_flags(held[i].pfn) & (1ULL << KPF_LRU), "putback restores actual Linux LRU membership");
    same_pfns(area, 0, 1024, original);
    check_content(area, 2 * PMD, 120);

    /* Reheat the order-8 sibling and verify Promote removes it from the
     * cold selection set. The next cheapest remaining order is 6. */
    inject(area + 256 * PAGE, 1024);
    command(MANAGER "control", "age\n");
    check(page_flags(original[256]) & (1ULL << KPF_ACTIVE), "Promote moves newly hot folio to active");
    command(MANAGER "control", "select 4\n");
    nr = candidates(held);
    check(nr == 4, "second real selection supplies requested folio count");
    const unsigned promoted_expected[] = {2, 9, 7, 6};
    for (unsigned i = 0; i < nr; i++)
        check(held[i].order == promoted_expected[i], "cost selector skips hot promoted order");
    command(MANAGER "control", "putback\n");
    command(TRACK "control", "drain\n");
    (void)heat_stats();
    release_mapping(area, 2 * PMD);
    puts("PASS C2 selector known_cost_order queue_exhaustion true_LRU_isolation putback Promote_Age");
}

static void exit_with_candidate(void)
{
    phase = "candidate mm lifetime across process exit";
    reset_tracking();
    int ready[2], finish[2];
    check(!pipe(ready) && !pipe(finish), "create candidate lifetime pipes");
    pid_t child = fork();
    check(child >= 0, "fork candidate owner");
    if (!child) {
        close(ready[0]);
        close(finish[1]);
        unsigned char *area = mmap(NULL, PMD, PROT_READ | PROT_WRITE,
                                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (area == MAP_FAILED || madvise(area, PMD, MADV_NOHUGEPAGE))
            _exit(1);
        memset(area, 0xa3, PMD);
        uintptr_t address = (uintptr_t)area;
        char token;
        if (write(ready[1], &address, sizeof(address)) != sizeof(address) ||
            read(finish[0], &token, 1) != 1)
            _exit(2);
        _exit(0);
    }
    close(ready[1]);
    close(finish[0]);
    uintptr_t address;
    check(read(ready[0], &address, sizeof(address)) == sizeof(address), "receive real child mapping");
    close(ready[0]);
    command(TRACK "control", "drain\n");
    command(MANAGER "control", "target %d 0x%lx %lu\n", child, (unsigned long)address, PMD);
    command(MANAGER "control", "age\n");
    command(MANAGER "control", "select 1\n");
    struct candidate held[128];
    check(candidates(held) == 1 && held[0].pid == child && held[0].order == 0,
          "selector holds an actual child base-page candidate");
    char path[80];
    snprintf(path, sizeof(path), "/proc/%d/pagemap", child);
    int fd = open(path, O_RDONLY);
    check(fd >= 0, "open live candidate owner's pagemap");
    uint64_t entry;
    check(pread(fd, &entry, 8, held[0].address / PAGE * 8) == 8 &&
          (entry >> 63) && (entry & PFN_MASK) == held[0].pfn,
          "child candidate corresponds to its real physical page");
    close(fd);
    check(write(finish[1], "G", 1) == 1, "let candidate owner exit while isolation is held");
    close(finish[1]);
    int status;
    check(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status),
          "owner exit is not blocked by candidate hold");
    check(candidates(held) == 1, "candidate descriptor survives owner exit until explicit putback");
    command(MANAGER "control", "putback\n");
    check(!candidates(held), "putback releases isolation and mm reference after owner exit");
    command(MANAGER "control", "clear_target\n");
    command(TRACK "control", "drain\n");
    (void)heat_stats();
    puts("PASS C2 lifecycle candidate_mm_reference owner_exit putback_cleanup");
}

struct concurrent_work {
    unsigned char *area;
    atomic_bool stop;
    atomic_uint hot_page;
    atomic_ulong loads;
    unsigned long checksum;
};

static void *concurrent_reader(void *argument)
{
    struct concurrent_work *work = argument;
    unsigned long checksum = 0;

    while (!atomic_load_explicit(&work->stop, memory_order_relaxed)) {
        unsigned hot = atomic_load_explicit(&work->hot_page, memory_order_relaxed);
        volatile unsigned char *address = work->area + hot * PAGE + 173;
        for (unsigned i = 0; i < 4096; i++) {
            __asm__ volatile("clflush (%0); mfence" :: "r"(address) : "memory");
            checksum += *address;
        }
        atomic_fetch_add_explicit(&work->loads, 4096, memory_order_relaxed);
        sched_yield();
    }
    work->checksum = checksum;
    return NULL;
}

/* This phase deliberately follows every deterministic fixture. Real PEBS
 * callbacks, the C1 sampling worker, and real C2 conversions run together.
 * Heat is not expected to remain equal across these concurrent operations. */
static void concurrent_pebs(void)
{
    phase = "real PEBS concurrent with manager conversions";
    reset_tracking();
    unsigned char *area = mapping(PMD, 4, 131);
    uint64_t original[512];
    snapshot_pfns(area, 512, original);
    target(area, 16 * PAGE);
    command(TRACK "control", "capacity %lu %lu\n", 64UL * 1024 * 1024,
            2UL * 1024 * 1024 * 1024);
    command(TRACK "control", "reset\n");
    struct concurrent_work work = {.area = area};
    atomic_init(&work.stop, 0);
    atomic_init(&work.hot_page, 0);
    atomic_init(&work.loads, 0);
    pthread_t reader;
    command(TRACK "control", "enable\n");
    check(!pthread_create(&reader, NULL, concurrent_reader, &work), "start real concurrent load thread");

    unsigned conversions = 0, busy = 0, attempts;
    for (attempts = 0; attempts < 300 && conversions < 2; attempts++) {
        usleep(100000);
        /* After the first left-hot split, the right half remains one
         * order-3 folio. Move the hardware workload there and split it. */
        unsigned first = conversions ? 8 : 0;
        int result = manager_try("split %d 0x%lx\n", getpid(),
                                 (unsigned long)(area + first * PAGE));
        if (result == -EBUSY || result == -EAGAIN) {
            busy++;
            continue;
        }
        check(!result, "concurrent conversion only retries transient PEBS references");
        char *last = read_text(MANAGER "last");
        int changed = field(last, "split_changed") != 0;
        free(last);
        if (changed) {
            /* A changed report must correspond to changed physical folio
             * head/tail flags, not merely a PMD-to-PTE mapping split. */
            unsigned hot = conversions ? 15 : 0;
            uint64_t flags = page_flags(pfn(area + hot * PAGE));
            check(!(flags & ((1ULL << KPF_COMPOUND_HEAD) |
                            (1ULL << KPF_COMPOUND_TAIL) | (1ULL << KPF_THP))),
                  "hardware-hot page actually becomes a physical base folio");
            conversions++;
            atomic_store_explicit(&work.hot_page, 15, memory_order_relaxed);
        }
    }
    /* Keep generating samples after a completed conversion as well, so
     * the tracker consumes references to the resulting smaller folios. */
    usleep(250000);
    atomic_store_explicit(&work.stop, 1, memory_order_relaxed);
    check(!pthread_join(reader, NULL), "join load thread before unmapping");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "drain\n");
    check(conversions > 0, "at least one real conversion completes during hardware sampling");
    struct heat_stats after = heat_stats();
    check(after.hardware > 0 && !after.synthetic, "concurrent evidence consists of real PEBS samples");
    unsigned counts[16];
    mapped_counters(original, 16, counts);
    uint64_t target_heat = 0;
    for (unsigned i = 0; i < 16; i++)
        target_heat += counts[i];
    check(target_heat > 0, "hardware samples were attributed to the converted mapping");
    check(atomic_load_explicit(&work.loads, memory_order_relaxed) > 0 && work.checksum,
          "reader completed actual data loads");
    same_pfns(area, 0, 512, original);
    check_content(area, PMD, 131);
    unsigned orders[16];
    hotspot_orders(16, 0, orders);
    if (conversions == 2)
        hotspot_orders(8, 7, orders + 8);
    verify_segments(area, 16, orders, original);
    release_mapping(area, PMD);
    mapped_counters(original, 16, counts);
    for (unsigned i = 0; i < 16; i++)
        check(!counts[i], "free clears counters after concurrent conversion and drain");
    printf("PASS C2 concurrent real_pebs conversions=%u attempts=%u busy=%u hardware_samples=%" PRIu64
           " loads=%lu target_heat=%" PRIu64 " cooling=%" PRIu64 " full_content histogram_conserved free_clear\n",
           conversions, attempts, busy, after.hardware,
           atomic_load_explicit(&work.loads, memory_order_relaxed), target_heat, after.cooling);
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    check(sysconf(_SC_PAGESIZE) == PAGE, "x86 4KiB base-page platform");
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "privileged physical folio inspection");
    struct sigaction action = {.sa_handler = protection_handler};
    sigemptyset(&action.sa_mask);
    check(!sigaction(SIGSEGV, &action, NULL), "install real permission fault probe");
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    /* Avoid unrelated khugepaged scans racing explicitly constructed
     * source orders. C2's own background path is controlled separately. */
    command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
    hhh_balanced();
    split_orders();
    collapse_orders();
    collapse_failures();
    background();
    selector();
    exit_with_candidate();
    concurrent_pebs();
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    char *state = read_text(MANAGER "stats");
    printf("FINAL_MANAGER_STATS\n%s", state);
    free(state);
    (void)heat_stats();
    close(pagemap_fd);
    close(flags_fd);
    printf("PASS CHAMELEON_C2 checks=%u\n", checks);
    return 0;
}
