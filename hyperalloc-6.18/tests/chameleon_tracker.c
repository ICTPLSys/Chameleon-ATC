// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kernel-page-flags.h>
#include <sched.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define BASE "/sys/kernel/debug/chameleon/"
#define PAGE 4096UL
#define MIB (1024UL * 1024)
#define PFN_MASK ((1ULL << 55) - 1)
static unsigned checks;
static volatile unsigned long sink;

static void check(int good, const char *why)
{
    if (!good) {
        fprintf(stderr, "FAIL C1 check=%u %s errno=%d (%s)\n", checks + 1, why, errno, strerror(errno));
        exit(1);
    }
    checks++;
}

static void write_file(const char *path, const char *fmt, ...)
{
    char buf[256];
    va_list args;
    va_start(args, fmt);
    int len = vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    check(len > 0 && len < (int)sizeof(buf), "control length");
    int fd = open(path, O_WRONLY);
    check(fd >= 0, path);
    check(write(fd, buf, len) == len, buf);
    close(fd);
}

struct stats {
    char text[8192];
};

static struct stats stats(void)
{
    struct stats s = {0};
    int fd = open(BASE "stats", O_RDONLY);
    check(fd >= 0, "open stats");
    ssize_t n = read(fd, s.text, sizeof(s.text) - 1);
    check(n > 0, "read stats");
    close(fd);
    return s;
}

static uint64_t field(const struct stats *s, const char *key)
{
    const char *line = s->text;
    size_t n = strlen(key);
    while (*line) {
        if (!strncmp(line, key, n) && (line[n] == ' ' || line[n] == '=')) {
            const char *start = line + n + 1;
            while (*start == ' ' || *start == '\t')
                start++;
            char *end;
            errno = 0;
            uint64_t value = strtoull(start, &end, 0);
            check(*start >= '0' && *start <= '9' && end != start && errno != ERANGE,
                  "stats value must be an unsigned integer");
            while (*end == ' ' || *end == '\t')
                end++;
            check(*end == '\n' || *end == '\0', "stats value has no unexpected suffix");
            return value;
        }
        line = strchr(line, '\n');
        if (!line)
            break;
        line++;
    }
    fprintf(stderr, "Missing stats field %s:\n%s", key, s->text);
    exit(1);
}

static void histogram(const struct stats *s)
{
    uint64_t total = 0;
    for (unsigned i = 0; i < 16; i++) {
        char key[20];
        snprintf(key, sizeof(key), "bin%u", i);
        total += field(s, key);
    }
    check(total == field(s, "managed_pages"), "histogram conservation");
}

static uint64_t pfn(void *address)
{
    uint64_t entry;
    int fd = open("/proc/self/pagemap", O_RDONLY);
    check(fd >= 0, "open pagemap");
    check(pread(fd, &entry, 8, (uintptr_t)address / PAGE * 8) == 8, "read pagemap");
    close(fd);
    check((entry >> 63) && (entry & PFN_MASK), "present visible PFN");
    return entry & PFN_MASK;
}

static uint64_t range(uint64_t start, unsigned n, unsigned *values)
{
    char request[80], result[32768];
    int fd = open(BASE "range", O_RDWR);
    check(fd >= 0, "open range");
    int len = snprintf(request, sizeof(request), "%" PRIu64 " %u\n", start, n);
    check(write(fd, request, len) == len, "select range");
    ssize_t got = pread(fd, result, sizeof(result) - 1, 0);
    check(got > 0, "read range");
    result[got] = 0;
    close(fd);
    char *line = result;
    uint64_t sum = 0;
    for (unsigned i = 0; i < n; i++) {
        uint64_t frame;
        unsigned value;
        check(line && sscanf(line, "%" SCNu64 " %u", &frame, &value) == 2, "parse counter");
        check(frame == start + i && value <= 65535, "counter PFN and bounds");
        if (values)
            values[i] = value;
        sum += value;
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    return sum;
}

static unsigned char *mapping(size_t size, int huge)
{
    size_t alignment = 2 * MIB;
    unsigned char *raw = mmap(NULL, size + alignment, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    check(raw != MAP_FAILED, "allocate mapping");
    size_t prefix = (-(uintptr_t)raw) & (alignment - 1);
    if (prefix)
        check(!munmap(raw, prefix), "trim prefix");
    raw += prefix;
    check(!munmap(raw + size, alignment - prefix), "trim suffix");
    check(!madvise(raw, size, huge ? MADV_HUGEPAGE : MADV_NOHUGEPAGE), "set THP policy");
    for (size_t i = 0; i < size / PAGE; i++)
        raw[i * PAGE + 173] = (i % 251) + 1;
    return raw;
}

static void inject(void *addr, uint64_t count)
{
    write_file(BASE "inject", "0x%lx %" PRIu64 "\n", (unsigned long)addr, count);
}

static void deterministic(void)
{
    write_file(BASE "control", "disable\n");
    write_file(BASE "control", "reset\n");
    write_file(BASE "control", "capacity %lu %lu\n", 2UL * 1024 * MIB, 2UL * 1024 * MIB);
    struct stats s = stats();
    histogram(&s);
    check(field(&s, "sample_period") == 4096, "full capacity Ts");
    check(field(&s, "cooling_samples") == 524288, "full capacity Nc");
    check(field(&s, "metadata_bytes") >= field(&s, "managed_pages") * 2, "u16 array covers RAM");
    check(field(&s, "metadata_phys") != 0, "physical counter allocation");
    unsigned char *p = mapping(PAGE * 2, 0);
    uint64_t frame = pfn(p);
    inject(p + 173, 1);
    check(range(frame, 1, NULL) == 1, "first sample");
    inject(p + 173, 2);
    check(range(frame, 1, NULL) == 3, "counter accumulate");
    s = stats(); histogram(&s);
    check(field(&s, "bin1") == 1, "3 in [2,3] bin");
    inject(p + 173, 65540);
    check(range(frame, 1, NULL) == 65535, "u16 saturation without wrap");
    s = stats(); histogram(&s);
    check(field(&s, "bin15") == 1, "saturated top bin");
    check(field(&s, "hardware_samples") == 0, "injection is not hardware evidence");
    check(field(&s, "synthetic_samples") >= 65543, "synthetic accounting");
    check(field(&s, "phi") == 1, "zero-heavy histogram threshold");
    write_file(BASE "control", "reset\n");
    unsigned previous = 0;
    for (unsigned bin = 0; bin < 16; bin++) {
        unsigned upper = (1U << (bin + 1)) - 1;
        inject(p + 173, upper - previous);
        previous = upper;
        check(range(frame, 1, NULL) == upper, "exponential bin upper boundary");
        s = stats();
        histogram(&s);
        for (unsigned other = 0; other < 16; other++) {
            char key[20];
            snprintf(key, sizeof(key), "bin%u", other);
            uint64_t expected = other == 0 ? field(&s, "managed_pages") - (bin != 0)
                                           : other == bin;
            check(field(&s, key) == expected, "counter crosses exactly one exponential histogram bin");
        }
    }
    write_file(BASE "control", "reset\n");
    write_file(BASE "control", "capacity %lu %lu\n", 16 * PAGE, 2UL * 1024 * MIB);
    inject(p + 173, 15);
    s = stats(); histogram(&s);
    check(field(&s, "sample_period") == 512, "Ts floor");
    check(field(&s, "cooling_samples") == 16, "capacity Nc");
    check(field(&s, "cooling_epochs") == 0, "no timer-driven early cooling");
    check(range(frame, 1, NULL) == 15, "counter before sample boundary");
    inject(p + 173, 1);
    check(range(frame, 1, NULL) == 8, "cooling exactly at Nc");
    s = stats(); histogram(&s);
    check(field(&s, "cooling_epochs") == 1, "one cooling epoch");
    check(!madvise(p, PAGE, MADV_DONTNEED), "discard counted page");
    /* Unmapping does not release references held by an LRU-add pagevec.
     * The tracker drain also drains guest LRU batches before testing free. */
    write_file(BASE "control", "drain\n");
    check(range(frame, 1, NULL) == 0, "free clears heat");
    p[173] = 7;
    check(range(pfn(p), 1, NULL) == 0, "allocation has no inherited heat");
    check(!munmap(p, 2 * PAGE), "free deterministic mapping");
    write_file(BASE "control", "capacity %lu %lu\n", 1024UL * MIB, 2UL * 1024 * MIB);
    s = stats();
    check(field(&s, "sample_period") == 2048, "half capacity Ts");
    check(field(&s, "cooling_samples") == 262144, "half capacity Nc");
    write_file(BASE "control", "reset\n");
    write_file(BASE "control", "capacity %lu %lu\n", 16 * PAGE, 2UL * 1024 * MIB);
    p = mapping(5 * PAGE, 0);
    for (unsigned i = 0; i < 5; i++)
        inject(p + i * PAGE + 173, 2);
    s = stats(); histogram(&s);
    check(field(&s, "bin1") == 5, "five warm base pages");
    check(field(&s, "phi") == 3, "30 percent of 16 resident pages selects bin1 upper bound");
    write_file(BASE "control", "capacity %lu %lu\n", 32 * PAGE, 2UL * 1024 * MIB);
    s = stats(); histogram(&s);
    check(field(&s, "phi") == 1, "larger resident target moves threshold into bin0");
    check(!munmap(p, 5 * PAGE), "free threshold test pages");
    write_file(BASE "control", "drain\n");
    puts("PASS C1 deterministic bins saturation capacity sample_count_cooling free_reuse");
}

static void select_64k(void)
{
    const char *base = "/sys/kernel/mm/transparent_hugepage";
    DIR *dir = opendir(base);
    struct dirent *de;
    check(dir != NULL, "open THP sysfs");
    while ((de = readdir(dir))) {
        unsigned size;
        char suffix, path[512];
        if (sscanf(de->d_name, "hugepages-%ukB%c", &size, &suffix) != 1)
            continue;
        snprintf(path, sizeof(path), "%s/%s/enabled", base, de->d_name);
        if (!access(path, F_OK))
            write_file(path, "%s\n", size == 64 ? "always" : "never");
    }
    closedir(dir);
}

static void verify_64k(unsigned char *area, size_t bytes)
{
    int fd = open("/proc/kpageflags", O_RDONLY);
    check(fd >= 0, "open kpageflags");
    for (size_t i = 0; i < bytes / PAGE; i++) {
        uint64_t flags, frame = pfn(area + i * PAGE);
        check(pread(fd, &flags, 8, frame * 8) == 8, "read kpageflags");
        check(flags & (1ULL << KPF_THP), "actual THP required");
        check(flags & (1ULL << (i % 16 ? KPF_COMPOUND_TAIL : KPF_COMPOUND_HEAD)), "actual 64KiB folio");
        if (i % 16)
            check(frame == pfn(area + (i - 1) * PAGE) + 1, "contiguous tail PFN");
    }
    close(fd);
}

static void verify_split_base_pages(unsigned char *area, uint64_t original_pfn)
{
    int fd = open("/proc/kpageflags", O_RDONLY);
    check(fd >= 0, "open split kpageflags");
    for (unsigned i = 0; i < 16; i++) {
        uint64_t flags, frame = pfn(area + i * PAGE);
        check(frame == original_pfn + i, "real split preserves every mapped PFN");
        check(pread(fd, &flags, sizeof(flags), frame * 8) == sizeof(flags),
              "read split kpageflags");
        check(!(flags & ((1ULL << KPF_COMPOUND_HEAD) |
                         (1ULL << KPF_COMPOUND_TAIL) | (1ULL << KPF_THP))),
              "split must produce 16 physical base pages, not only PTE mappings");
    }
    close(fd);
}

static uint64_t nanoseconds(void)
{
    struct timespec ts;
    check(!clock_gettime(CLOCK_MONOTONIC, &ts), "clock");
    return ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static uint64_t work(unsigned char *area, unsigned pages, unsigned long iterations)
{
    uint64_t start = nanoseconds();
    unsigned long total = 0;
    for (unsigned long i = 0, page = 0; i < iterations; i++) {
        page = (page + 4093) & (pages - 1);
        unsigned char *address = area + page * PAGE + 173;
        __asm__ volatile("clflush (%0); mfence" :: "r"(address) : "memory");
        total += *(volatile unsigned char *)address;
    }
    sink = total;
    return nanoseconds() - start;
}

static uint64_t heat(unsigned char *area, unsigned pages)
{
    uint64_t sum = 0;
    for (unsigned i = 0; i < pages; i += 16)
        sum += range(pfn(area + i * PAGE), 16, NULL);
    return sum;
}

static void hardware(void)
{
    const unsigned pages = 4096;
    const size_t bytes = pages * PAGE;
    select_64k();
    unsigned char *p = mapping(bytes * 2, 1);
    verify_64k(p, bytes * 2);
    cpu_set_t cpu;
    CPU_ZERO(&cpu); CPU_SET(0, &cpu);
    check(!sched_setaffinity(0, sizeof(cpu), &cpu), "bind workload to CPU0");
    write_file(BASE "control", "reset\n");
    write_file(BASE "control", "capacity %lu %lu\n", 64UL * MIB, 2UL * 1024 * MIB);
    uint64_t baseline = work(p, pages, 2UL << 20);
    write_file(BASE "control", "enable\n");
    uint64_t sampled = work(p, pages, 2UL << 20);
    work(p, pages, 14UL << 20);
    write_file(BASE "control", "disable\n");
    write_file(BASE "control", "drain\n");
    uint64_t a = heat(p, pages), b = heat(p + bytes, pages);
    struct stats first = stats(); histogram(&first);
    printf("HARDWARE phase=1 heat_a=%" PRIu64 " heat_b=%" PRIu64 "\n", a, b);
    check(a > b + 16, "PEBS identifies first hot region");
    check(field(&first, "hardware_samples") > 4096, "bulk PEBS records reach tracker, not only final record");
    check(field(&first, "synthetic_samples") == 0, "no injected hardware evidence");
    check(field(&first, "ptw_pending") > 0 && field(&first, "ptw_completed") > 0, "same-window PTW observed");
    check(field(&first, "ptw_read_errors") == 0 && field(&first, "ptw_multiplexed") == 0,
          "PTW window reads succeed without multiplexing");
    write_file(BASE "control", "enable\n");
    work(p + bytes, pages, 32UL << 20);
    write_file(BASE "control", "disable\n");
    write_file(BASE "control", "drain\n");
    uint64_t final_a = heat(p, pages), final_b = heat(p + bytes, pages);
    struct stats last = stats(); histogram(&last);
    printf("HARDWARE phase=2 heat_a=%" PRIu64 " heat_b=%" PRIu64 "\n", final_a, final_b);
    check(final_b > final_a + 16, "tracker follows migrated hot region");
    check(final_a < a, "old heat decays by sample-count cooling");
    check(field(&last, "cooling_epochs") > 0, "hardware-driven cooling");
    uint64_t frame = pfn(p + bytes);
    unsigned before[16], after[16];
    unsigned char saved[64 * 1024];
    /* Disable future collapse, but prove the source is still an actual
     * order-4 folio immediately before the split request. Debugfs may return
     * success while skipping a busy folio, so counter equality is insufficient. */
    check(!madvise(p + bytes, sizeof(saved), MADV_NOHUGEPAGE), "prevent split recollapse");
    verify_64k(p + bytes, sizeof(saved));
    memcpy(saved, p + bytes, sizeof(saved));
    range(frame, 16, before);
    write_file("/sys/kernel/debug/split_huge_pages", "%d,0x%lx,0x%lx,0\n", getpid(),
               (unsigned long)(p + bytes), (unsigned long)(p + bytes + 64 * 1024));
    verify_split_base_pages(p + bytes, frame);
    check(!memcmp(saved, p + bytes, sizeof(saved)), "real split preserves all 64KiB data");
    range(frame, 16, after);
    check(!memcmp(before, after, sizeof(before)), "native split preserves PFN heat");
    check(!munmap(p, bytes * 2), "release hardware workload");
    write_file(BASE "control", "drain\n");
    check(range(frame, 16, NULL) == 0, "release split pages clears counters");
    printf("OBSERVATION baseline_ns=%" PRIu64 " sampled_ns=%" PRIu64 " ratio=%.4f workload=2M_clflush_loads\n",
           baseline, sampled, (double)sampled / baseline);
    printf("FINAL_TRACKER_STATS\n%s", last.text);
    puts("PASS C1 hardware mthp64 nonzero_offset173 hot_region_move cooling PTW split_free");
}

static void lifecycle_child(unsigned char *inherited, uint64_t parent_pfn,
                            int start_fd)
{
    cpu_set_t cpu;
    CPU_ZERO(&cpu); CPU_SET(1, &cpu);
    check(!sched_setaffinity(0, sizeof(cpu), &cpu), "bind churn to CPU1");
    char start;
    check(read(start_fd, &start, 1) == 1 && start == 'G', "sampling enabled before churn");
    close(start_fd);
    for (unsigned i = 0; i < 64; i++)
        inherited[i * PAGE + 173] = 0xa5;
    check(pfn(inherited) != parent_pfn, "COW must allocate a different physical frame");
    for (unsigned i = 0; i < 64; i++)
        check(inherited[i * PAGE + 173] == 0xa5, "child COW data");

    for (unsigned round = 0; round < 32; round++) {
        const size_t bytes = 4 * MIB, half = bytes / 2;
        unsigned char *q = mapping(bytes, round & 1);
        for (unsigned i = 0; i < bytes / PAGE; i++)
            check(q[i * PAGE + 173] == (i % 251) + 1, "churn fault data");
        work(q, bytes / PAGE, 65536);
        check(!mprotect(q + PAGE, PAGE, PROT_READ), "churn read-only protection");
        check(!mprotect(q + PAGE, PAGE, PROT_READ | PROT_WRITE), "churn restore write permission");
        check(!madvise(q, 64 * 1024, MADV_DONTNEED), "churn discard");
        check(q[173] == 0 && q[64 * 1024 + 173] == 17, "discard zeroes only requested pages");
        q[173] = 0x63;
        check(!munmap(q + half, half), "churn partial unmap");
        unsigned char *replacement = mmap(q + half, half, PROT_READ | PROT_WRITE,
                                          MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE,
                                          -1, 0);
        check(replacement == q + half, "churn remap same virtual range");
        check(q[173] == 0x63 && replacement[173] == 0,
              "same-VA remap preserves neighbour and supplies fresh contents");
        replacement[173] = 0x59;
        work(q, bytes / PAGE, 65536);
        check(q[173] == 0x63 && replacement[173] == 0x59, "churn data survives sampling");
        check(!munmap(q, bytes), "churn release");
    }
    _exit(0);
}

static void lifecycle(void)
{
    const unsigned pages = 2048;
    const size_t bytes = pages * PAGE;
    write_file(BASE "control", "disable\n");
    write_file(BASE "control", "reset\n");
    unsigned char *p = mapping(bytes, 1);
    verify_64k(p, bytes);
    uint64_t original_pfns[64];
    for (unsigned i = 0; i < 64; i++)
        original_pfns[i] = pfn(p + i * PAGE);
    int start[2];
    check(!pipe(start), "create lifecycle start pipe");
    pid_t child = fork();
    check(child >= 0, "fork concurrent lifecycle process");
    if (!child) {
        close(start[1]);
        lifecycle_child(p, original_pfns[0], start[0]);
    }
    close(start[0]);
    write_file(BASE "control", "enable\n");
    check(write(start[1], "G", 1) == 1, "start lifecycle while sampling");
    close(start[1]);
    work(p, pages, 8UL << 20);
    int status;
    check(waitpid(child, &status, 0) == child && WIFEXITED(status) &&
          WEXITSTATUS(status) == 0, "concurrent lifecycle child passed");
    write_file(BASE "control", "disable\n");
    write_file(BASE "control", "drain\n");
    for (unsigned i = 0; i < pages; i++)
        check(p[i * PAGE + 173] == (i % 251) + 1, "parent data preserved through concurrent COW");
    for (unsigned i = 0; i < 64; i++)
        check(pfn(p + i * PAGE) == original_pfns[i], "COW preserves parent physical frames");
    struct stats s = stats();
    histogram(&s);
    check(field(&s, "hardware_samples") > 16, "real samples during concurrent page lifecycle");
    check(field(&s, "synthetic_samples") == 0, "lifecycle uses no injected samples");
    check(!munmap(p, bytes), "release lifecycle parent mapping");
    write_file(BASE "control", "drain\n");
    for (unsigned i = 0; i < 64; i++)
        check(range(original_pfns[i], 1, NULL) == 0, "lifecycle teardown clears parent heat");
    s = stats();
    histogram(&s);
    printf("LIFECYCLE_TRACKER_STATS\n%s", s.text);
    puts("PASS C1 lifecycle concurrent_sampling cpu0_hot cpu1_churn rounds=32 mmap_unmap_sameva COW discard mprotect histogram free");
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc == 2 && !strcmp(argv[1], "--expect-unavailable")) {
        int fd = open(BASE "control", O_WRONLY);
        check(fd >= 0, "open control for unavailable-capability test");
        errno = 0;
        ssize_t rc = write(fd, "enable\n", 7);
        check(rc == -1 && errno == EOPNOTSUPP, "tracker rejects missing PEBS MEMINFO capability");
        close(fd);
        struct stats s = stats();
        check(field(&s, "enabled") == 0 && field(&s, "hardware_samples") == 0,
              "failed enable leaves sampler disabled");
        puts("PASS CHAMELEON_C1_UNAVAILABLE errno=EOPNOTSUPP");
        return 0;
    }
    check(argc == 1, "supported test arguments");
    deterministic();
    hardware();
    lifecycle();
    printf("PASS CHAMELEON_C1 checks=%u\n", checks);
    return 0;
}
