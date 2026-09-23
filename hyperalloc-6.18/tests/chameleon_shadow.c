// SPDX-License-Identifier: GPL-2.0-only
/* C3 integration tests: real C2 isolation, nonpresent PTEs and user faults. */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kernel-page-flags.h>
#include <linux/types.h>
#include <pthread.h>
#include <setjmp.h>
#include <signal.h>
#include <stdbool.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
#include "../linux/mm/gup_test.h"

#define TRACK "/sys/kernel/debug/chameleon/"
#define MANAGER "/sys/kernel/debug/chameleon_mm/"
#define SHADOW "/sys/kernel/debug/chameleon_shadow/"
#define TEST_BACKEND "/sys/kernel/debug/chameleon_shadow_backend/"
#define THP "/sys/kernel/mm/transparent_hugepage/"
#define PAGE 4096UL
#define PMD (512UL * PAGE)
#define PFN_MASK ((1ULL << 55) - 1)
#define TEXT_SIZE (256UL * 1024)
#define MAX_OBJECTS 128

#ifndef CHAMELEON_TEST_STAGE
#define CHAMELEON_TEST_STAGE "C3"
#endif

static unsigned checks;
static const char *phase = "initialization";
static int pagemap_fd, flags_fd;
static sigjmp_buf permission_jump;
static volatile sig_atomic_t permission_armed;

static void check(bool good, const char *reason)
{
    if (!good) {
        fprintf(stderr, "FAIL " CHAMELEON_TEST_STAGE " phase=%s check=%u %s errno=%d (%s)\n",
                phase, checks + 1, reason, errno, strerror(errno));
        exit(1);
    }
    checks++;
}

static int command_v(const char *path, const char *format, va_list ap)
{
    char text[256];
    int n = vsnprintf(text, sizeof(text), format, ap);
    check(n > 0 && n < (int)sizeof(text), "bounded control command");
    int fd = open(path, O_WRONLY);
    check(fd >= 0, path);
    ssize_t result = write(fd, text, n);
    int error = errno;
    close(fd);
    if (result < 0)
        return -error;
    check(result == n, "complete control command write");
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

static int shadow_try(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    int result = command_v(SHADOW "control", format, ap);
    va_end(ap);
    return result;
}

static int backend_try(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    int result = command_v(TEST_BACKEND "control", format, ap);
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
        check(n >= 0, "read kernel state");
        if (!n)
            break;
        used += n;
        check(used < TEXT_SIZE - 1, "kernel state fits buffer");
    }
    close(fd);
    return text;
}

static uint64_t field(const char *text, const char *key)
{
    size_t length = strlen(key);
    for (const char *line = text; line && *line; ) {
        if (!strncmp(line, key, length) && (line[length] == ' ' || line[length] == '=')) {
            char *end;
            errno = 0;
            uint64_t value = strtoull(line + length + 1, &end, 0);
            check(end != line + length + 1 && errno != ERANGE, "numeric kernel field");
            while (*end == ' ' || *end == '\t')
                end++;
            check(!*end || *end == '\n', "complete numeric field");
            return value;
        }
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    fprintf(stderr, "Missing field %s:\n%s", key, text);
    exit(1);
}

static uint64_t isolated_anon(void)
{
    /* Isolation and putback may run on different vCPUs. Fold both per-CPU
     * buckets before comparing the exact charge, including at baseline. */
    char *refresh = read_text("/proc/sys/vm/stat_refresh");
    free(refresh);
    char *text = read_text("/proc/vmstat");
    uint64_t pages = field(text, "nr_isolated_anon");
    free(text);
    return pages;
}

static void histogram(void)
{
    char *text = read_text(TRACK "stats");
    uint64_t sum = 0;
    for (unsigned i = 0; i < 16; i++) {
        char key[24];
        snprintf(key, sizeof(key), "bin%u", i);
        sum += field(text, key);
    }
    check(sum == field(text, "managed_pages") && sum == field(text, "histogram_sum"),
          "C1 histogram conserves all managed base pages");
    check(!field(text, "hardware_samples") && !field(text, "synthetic_samples"),
          "Shadow deterministic fixture does not manufacture heat samples");
    free(text);
}

static uint64_t pagemap(int fd, const unsigned char *address)
{
    uint64_t entry;
    check(pread(fd, &entry, sizeof(entry), (uintptr_t)address / PAGE * 8) == 8,
          "read real pagemap entry");
    return entry;
}

static uint64_t pfn(unsigned char *address)
{
    uint64_t entry = pagemap(pagemap_fd, address);
    check((entry >> 63) && !(entry & (1ULL << 62)) && (entry & PFN_MASK),
          "real resident visible guest PFN");
    return entry & PFN_MASK;
}

static uint64_t page_flags(uint64_t frame)
{
    uint64_t flags;
    check(pread(flags_fd, &flags, sizeof(flags), frame * 8) == 8,
          "read physical page flags");
    return flags;
}

static void snapshot(unsigned char *area, unsigned pages, uint64_t *frames)
{
    for (unsigned i = 0; i < pages; i++)
        frames[i] = pfn(area + i * PAGE);
}

static void same_pfns(unsigned char *area, unsigned pages, const uint64_t *frames)
{
    for (unsigned i = 0; i < pages; i++)
        check(pfn(area + i * PAGE) == frames[i], "original guest PFN retained");
}

static void physical_folio(uint64_t first, unsigned order)
{
    unsigned pages = 1U << order;
    check(!(first & (pages - 1)), "physical folio alignment");
    for (unsigned i = 0; i < pages; i++) {
        uint64_t flags = page_flags(first + i);
        check(flags & (1ULL << KPF_ANON), "anonymous physical folio");
        if (!order)
            check(!(flags & ((1ULL << KPF_COMPOUND_HEAD) |
                            (1ULL << KPF_COMPOUND_TAIL) | (1ULL << KPF_THP))),
                  "physical base page is not a compound fragment");
        else {
            check(flags & (1ULL << KPF_THP), "physical huge folio retained");
            check(!!(flags & (1ULL << KPF_COMPOUND_HEAD)) == !i,
                  "exact physical compound head");
            check(!!(flags & (1ULL << KPF_COMPOUND_TAIL)) == !!i,
                  "exact physical compound tails");
        }
    }
}

static void folio(unsigned char *area, unsigned order)
{
    uint64_t first = pfn(area);
    physical_folio(first, order);
    for (unsigned i = 0; i < (1U << order); i++)
        check(pfn(area + i * PAGE) == first + i, "contiguous mapped physical folio");
}

static unsigned char pattern(size_t offset, unsigned salt)
{
    return (unsigned char)((offset * 131U) ^ (offset >> 7) ^ (salt * 29U));
}

static void content(unsigned char *area, size_t bytes, size_t original_offset, unsigned salt)
{
    for (size_t i = 0; i < bytes; i++) {
        if (area[i] != pattern(original_offset + i, salt)) {
            fprintf(stderr, "Data mismatch offset=%zu value=%u expected=%u\n",
                    original_offset + i, area[i], pattern(original_offset + i, salt));
            check(0, "every source byte survives");
        }
    }
    check(1, "every source byte survives");
}

static void policy(unsigned order)
{
    DIR *dir = opendir(THP);
    check(dir != NULL, "open native mTHP policies");
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

static unsigned char *aligned_map(size_t bytes)
{
    unsigned char *area = mmap(NULL, bytes + PMD, PROT_READ | PROT_WRITE,
                               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    check(area != MAP_FAILED, "allocate actual private anonymous VMA");
    size_t prefix = (-(uintptr_t)area) & (PMD - 1);
    if (prefix)
        check(!munmap(area, prefix), "trim alignment prefix");
    area += prefix;
    check(!munmap(area + bytes, PMD - prefix), "trim alignment suffix");
    return area;
}

static unsigned char *mapping(size_t bytes, unsigned order, unsigned salt)
{
    policy(order);
    unsigned char *area = aligned_map(bytes);
    check(!madvise(area, bytes, MADV_HUGEPAGE), "native anonymous huge-page eligibility");
    for (size_t i = 0; i < bytes; i++)
        area[i] = pattern(i, salt);
    command(TRACK "control", "drain\n");
    for (unsigned i = 0; i < bytes / PAGE; i += 1U << order)
        folio(area + i * PAGE, order);
    return area;
}

static void permission_handler(int signal)
{
    if (permission_armed) {
        permission_armed = 0;
        siglongjmp(permission_jump, signal);
    }
    _exit(128 + signal);
}

static void denied(unsigned char *address, int write_access)
{
    int signal = sigsetjmp(permission_jump, 1);
    if (!signal) {
        permission_armed = 1;
        if (write_access)
            *(volatile unsigned char *)address ^= 1;
        else {
            volatile unsigned char value = *(volatile unsigned char *)address;
            (void)value;
        }
        permission_armed = 0;
        check(0, "access forbidden by current VMA raises real SIGSEGV");
    }
    check(signal == SIGSEGV, "actual SIGSEGV enforces current VMA permission");
}

struct shadow_stats {
    uint64_t objects, slots, metadata, pages;
    uint64_t batches, groups, flushes, demotions, faults;
};

static struct shadow_stats stats(void)
{
    char *text = read_text(SHADOW "stats");
    struct shadow_stats s = {
        .objects = field(text, "live_objects"),
        .slots = field(text, "live_slots"),
        .metadata = field(text, "metadata_pages"),
        .pages = field(text, "shadow_pages"),
        .batches = field(text, "prepare_batches"),
        .groups = field(text, "mm_groups"),
        .flushes = field(text, "guest_flushes"),
        .demotions = field(text, "prepare_pmd_demotions"),
        .faults = field(text, "fault_restores"),
    };
    free(text);
    return s;
}

struct entry {
    uint64_t token, pfn, table_pfn;
    unsigned long address;
    unsigned order, slots, slot;
    int pid;
};

static unsigned entries(struct entry *out)
{
    char *text = read_text(SHADOW "entries");
    unsigned count = 0;
    for (char *line = text; line && *line; ) {
        struct entry e;
        if (sscanf(line, "entry token=%" SCNu64 " pid=%d address=0x%lx pfn=%" SCNu64
                        " order=%u live_slots=%u table_pfn=%" SCNu64 " slot=%u",
                   &e.token, &e.pid, &e.address, &e.pfn, &e.order, &e.slots,
                   &e.table_pfn, &e.slot) == 8) {
            check(count < MAX_OBJECTS, "bounded Shadow object list");
            out[count++] = e;
        }
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    free(text);
    return count;
}

static void idle(uint64_t isolated, int metadata_empty)
{
    struct shadow_stats s = {0};
    unsigned attempt;
    for (attempt = 0; attempt < 200; attempt++) {
        command(SHADOW "control", "drain\n");
        command(TRACK "control", "drain\n");
        s = stats();
        if (!s.objects && !s.slots && !s.pages && (!metadata_empty || !s.metadata) &&
            isolated_anon() == isolated)
            break;
        usleep(10000);
    }
    if (attempt == 200)
        fprintf(stderr, "Idle timeout objects=%" PRIu64 " slots=%" PRIu64 " pages=%" PRIu64
                " metadata=%" PRIu64 " isolated=%" PRIu64 " expected=%" PRIu64 "\n",
                s.objects, s.slots, s.pages, s.metadata, isolated_anon(), isolated);
    check(attempt < 200, "Shadow objects, references and isolation return to baseline");
    struct entry e[MAX_OBJECTS];
    check(!entries(e), "retired Shadow entries are absent");
    histogram();
}

static void release(unsigned char *area, size_t bytes, uint64_t isolated)
{
    command(MANAGER "control", "clear_target\n");
    check(!munmap(area, bytes), "unmap fixture and its page-table metadata");
    idle(isolated, 1);
}

/* Each range is one original, physically contiguous folio (or a surviving
 * subrange). Pagemap exposes PFN-special entries as type | (original_PFN<<5),
 * independently of the Shadow object's debugfs metadata. */
static void nonpresent(int fd, unsigned char *area, unsigned pages, uint64_t original_pfn)
{
    unsigned type = 0;
    for (unsigned i = 0; i < pages; i++) {
        uint64_t value = pagemap(fd, area + i * PAGE);
        check(!(value >> 63) && (value & (1ULL << 62)),
              "actual pagemap contains nonpresent special entry");
        unsigned current_type = value & 31;
        if (!i)
            type = current_type;
        check(type == current_type, "all Shadow slots use one dedicated swap type");
        check(((value & PFN_MASK) >> 5) == original_pfn + i,
              "actual nonpresent PTE encodes the original guest physical frame");
    }
}

static void set_target(pid_t pid, unsigned char *area, size_t bytes)
{
    command(MANAGER "control", "target %d 0x%lx %zu\n", pid, (unsigned long)area, bytes);
    command(MANAGER "control", "age\n");
}

static struct entry prepare(unsigned char *area, unsigned order, uint64_t isolated)
{
    unsigned pages = 1U << order;
    uint64_t original = pfn(area);
    set_target(getpid(), area, pages * PAGE);
    command(SHADOW "control", "prepare 1\n");
    command(MANAGER "control", "clear_target\n");
    struct entry list[MAX_OBJECTS];
    check(entries(list) == 1, "one actual Shadow object published");
    check(list[0].pid == getpid() && list[0].address == (uintptr_t)area &&
          list[0].pfn == original && list[0].order == order && list[0].slots == pages,
          "Shadow metadata identifies every page of the selected real folio");
    check(list[0].table_pfn && list[0].slot == (((uintptr_t)area / PAGE) & 511),
          "metadata belongs to an actual PTE table and the correct slot");
    struct shadow_stats s = stats();
    check(s.objects == 1 && s.slots == pages && s.pages == pages && s.metadata == 1,
          "real metadata and resident Shadow accounting");
    check(isolated_anon() == isolated, "C2 isolation charge transfers exactly once to private Shadow pool");
    check(!(page_flags(original) & (1ULL << KPF_LRU)), "Shadow stays outside native reclaim LRU");
    nonpresent(pagemap_fd, area, pages, original);
    physical_folio(original, order);
    return list[0];
}

static void core_orders(void)
{
    phase = "real prepare and fault restore";
    for (unsigned order = 0; order <= 9; order++) {
        if (order == 1)
            continue;
        unsigned salt = 20 + order, pages = 1U << order;
        uint64_t isolated = isolated_anon(), original[512];
        unsigned char *area = mapping(PMD, order, salt);
        snapshot(area, 512, original);
        struct shadow_stats before = stats();
        struct entry e = prepare(area, order, isolated);
        struct shadow_stats prepared = stats();
        check(prepared.groups == before.groups + 1 && prepared.flushes == before.flushes + 1,
              "one modified mm incurs one prepare guest flush");
        check(prepared.demotions == before.demotions + (order == 9),
              "PMD preparation is counted separately from the guest batch flush");
        check(*(volatile unsigned char *)(area + 173) == pattern(173, salt),
              "real user load faults and restores original data");
        idle(isolated, 0);
        struct shadow_stats after = stats();
        check(after.faults == prepared.faults + 1, "one real fault restores one complete object");
        same_pfns(area, 512, original);
        folio(area, order);
        content(area, PMD, 0, salt);
        check(page_flags(original[0]) & (1ULL << KPF_LRU), "restored folio returns to actual LRU");
        release(area, PMD, isolated);
        printf("RESTORE order=%u pages=%u token=%" PRIu64 " pfn=%" PRIu64
               " same_pfn full_content physical_folio\n", order, pages, e.token, e.pfn);
    }
    puts("PASS C3 prepare real_C2_selector nonpresent_PTE metadata isolation orders=0,2,3,4,5,6,7,8,9");
    puts("PASS C3 restore actual_user_fault original_PFN full_content physical_folio histogram");
}

static void backend(void)
{
    phase = "missing backend and stale identity";
    uint64_t isolated = isolated_anon(), original[512];
    unsigned char *area = mapping(PMD, 4, 40);
    snapshot(area, 512, original);
    struct entry e = prepare(area, 4, isolated);
    check(shadow_try("commit %" PRIu64 "\n", e.token) == -EOPNOTSUPP,
          "absent backend explicitly refuses commit");
    nonpresent(pagemap_fd, area, 16, original[0]);
    check(isolated_anon() == isolated && stats().pages == 16,
          "failed commit retains source ownership in the private Shadow pool");
    command(SHADOW "control", "restore %" PRIu64 "\n", e.token);
    idle(isolated, 0);
    int result = shadow_try("restore %" PRIu64 "\n", e.token);
    check(result == -ENOENT || result == -ESTALE, "retired token cannot restore unrelated memory");
    same_pfns(area, 512, original);
    content(area, PMD, 0, 40);
    release(area, PMD, isolated);
    puts("PASS C3 backend no_fake_ready source_retained explicit_restore stale_token_rejected");
}

static void permissions(void)
{
    phase = "Shadow and current VMA permissions";
    for (unsigned which = 0; which < 3; which++) {
        uint64_t isolated = isolated_anon(), original[512];
        unsigned salt = 50 + which;
        unsigned char *area = mapping(PMD, 4, salt);
        snapshot(area, 512, original);
        if (!which)
            check(!mprotect(area, PMD, PROT_READ), "readonly VMA before prepare");
        (void)prepare(area, 4, isolated);
        if (which == 1)
            check(!mprotect(area, PMD, PROT_READ), "mprotect restores Shadow before making VMA readonly");
        else if (which == 2) {
            check(!mprotect(area, PMD, PROT_NONE), "mprotect Shadow VMA to PROT_NONE");
            denied(area + 173, 0);
            check(!mprotect(area, PMD, PROT_READ), "permit reads after PROT_NONE");
        }
        check(*(volatile unsigned char *)(area + 173) == pattern(173, salt),
              "allowed load retains original contents");
        denied(area + 173, 1);
        idle(isolated, 1);
        same_pfns(area, 512, original);
        content(area, PMD, 0, salt);
        check(!mprotect(area, PMD, PROT_READ | PROT_WRITE), "restore writable permission");
        release(area, PMD, isolated);
    }
    puts("LIFECYCLE readonly_prepare mprotect_readonly PROT_NONE real_SIGSEGV");
}

struct resident_stats {
    unsigned long rss, pss, swap, locked;
};

static struct resident_stats residency(unsigned char *address)
{
    char *text = read_text("/proc/self/smaps");
    struct resident_stats state = {0};
    unsigned seen = 0;
    int active = 0;
    for (char *line = text; line && *line; ) {
        unsigned long start, end, value;
        if (sscanf(line, "%lx-%lx", &start, &end) == 2) {
            if (active)
                break;
            active = (uintptr_t)address >= start && (uintptr_t)address < end;
        } else if (active) {
            if (sscanf(line, "Rss: %lu kB", &value) == 1) {
                state.rss = value;
                seen |= 1;
            } else if (sscanf(line, "Pss: %lu kB", &value) == 1) {
                state.pss = value;
                seen |= 2;
            } else if (sscanf(line, "Swap: %lu kB", &value) == 1) {
                state.swap = value;
                seen |= 4;
            } else if (sscanf(line, "Locked: %lu kB", &value) == 1) {
                state.locked = value;
                seen |= 8;
            }
        }
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    free(text);
    check(seen == 15, "smaps reports target VMA RSS, PSS, swap and locked accounting");
    return state;
}

static void soft_dirty_probe(unsigned char *area, const char *label, uint64_t isolated)
{
    command("/proc/self/clear_refs", "4\n");
    idle(isolated, 1);
    uint64_t cleared = pagemap(pagemap_fd, area);
    check(!(cleared & (1ULL << 55)), "clear_refs clears the actual pagemap soft-dirty bit");
    struct rusage before, after;
    check(!getrusage(RUSAGE_SELF, &before), "read fault counters before actual write");
    *(volatile unsigned char *)(area + 173) ^= 1;
    check(!getrusage(RUSAGE_SELF, &after), "read fault counters after actual write");
    uint64_t written = pagemap(pagemap_fd, area);
    printf("SOFT_DIRTY path=%s cleared=0x%016" PRIx64 " written=0x%016" PRIx64
           " minor_fault_delta=%ld major_fault_delta=%ld\n", label, cleared, written,
           after.ru_minflt - before.ru_minflt, after.ru_majflt - before.ru_majflt);
    check((written & (1ULL << 55)) != 0, "real write fault sets the pagemap soft-dirty bit");
    *(volatile unsigned char *)(area + 173) ^= 1;
}

static void soft_dirty(void)
{
    phase = "resident RSS and clear_refs software permissions";
    uint64_t isolated = isolated_anon(), original[512];
    unsigned char *area = mapping(PMD, 4, 55);
    snapshot(area, 512, original);
    soft_dirty_probe(area, "native", isolated);
    struct resident_stats before = residency(area);
    check(before.rss == PMD / 1024 && before.pss == PMD / 1024 && !before.swap,
          "source is completely resident private memory without swap backing");
    (void)prepare(area, 4, isolated);
    struct resident_stats after = residency(area);
    check(after.rss == before.rss && after.pss == before.pss && !after.swap,
          "Shadow keeps resident RSS/PSS and is not counted as real swap");
    soft_dirty_probe(area, "shadow_pre_restore", isolated);
    same_pfns(area, 512, original);
    content(area, PMD, 0, 55);
    release(area, PMD, isolated);
    puts("LIFECYCLE resident_RSS_PSS no_fake_swap clear_refs actual_soft_dirty_write_fault");
}

static void locked_memory(void)
{
    phase = "mlock and MLOCK_ONFAULT restore real resident Shadow";
    for (unsigned onfault = 0; onfault < 2; onfault++) {
        uint64_t isolated = isolated_anon(), original[512];
        unsigned salt = 56 + onfault;
        unsigned char *area = mapping(PMD, 4, salt);
        snapshot(area, 512, original);
        (void)prepare(area, 4, isolated);
        int result = onfault ? mlock2(area, 16 * PAGE, MLOCK_ONFAULT) : mlock(area, 16 * PAGE);
        check(!result, "lock exactly one complete resident order-4 folio");
        idle(isolated, 1);
        same_pfns(area, 512, original);
        folio(area, 4);
        content(area, PMD, 0, salt);
        check(page_flags(original[0]) & (1ULL << KPF_UNEVICTABLE),
              "restored and locked physical folio is actually unevictable");
        check(residency(area).locked == 64, "smaps records the full 64KiB as locked resident memory");
        check(!munlock(area, 16 * PAGE), "unlock the complete physical folio");
        command(TRACK "control", "drain\n");
        check(!(page_flags(original[0]) & (1ULL << KPF_UNEVICTABLE)),
              "munlock and LRU drain clear physical unevictable state");
        check(!residency(area).locked, "smaps locked charge returns to zero");
        release(area, PMD, isolated);
        printf("LIFECYCLE mlock onfault=%u original_PFN physical_UNEVICTABLE locked_64KiB munlock_balanced\n",
               onfault);
    }
}

static void fork_cow(void)
{
    phase = "fork restores Shadow before native COW";
    uint64_t isolated = isolated_anon(), original[512];
    unsigned char *area = mapping(PMD, 4, 60);
    snapshot(area, 512, original);
    (void)prepare(area, 4, isolated);
    pid_t child = fork();
    check(child >= 0, "fork actual Shadow owner");
    if (!child) {
        content(area, PMD, 0, 60);
        area[173] ^= 0xff;
        int fd = open("/proc/self/pagemap", O_RDONLY);
        uint64_t entry;
        if (fd < 0 || pread(fd, &entry, 8, (uintptr_t)area / PAGE * 8) != 8 ||
            !(entry >> 63) || (entry & PFN_MASK) == original[0] ||
            area[173] != (unsigned char)(pattern(173, 60) ^ 0xff))
            _exit(1);
        close(fd);
        _exit(0);
    }
    int status;
    check(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status),
          "child read data and performed real COW to a distinct PFN");
    idle(isolated, 1);
    same_pfns(area, 512, original);
    content(area, PMD, 0, 60);
    release(area, PMD, isolated);
    puts("LIFECYCLE fork_prefault native_COW child_new_PFN parent_content");
}

static void partial_zap(void)
{
    phase = "partial munmap and MADV_DONTNEED";
    for (unsigned discard = 0; discard < 2; discard++) {
        uint64_t isolated = isolated_anon(), original[512];
        unsigned salt = 70 + discard;
        unsigned char *area = mapping(PMD, 4, salt);
        snapshot(area, 512, original);
        (void)prepare(area, 4, isolated);
        if (discard)
            check(!madvise(area + 4 * PAGE, 4 * PAGE, MADV_DONTNEED),
                  "discard actual middle Shadow entries");
        else
            check(!munmap(area + 4 * PAGE, 4 * PAGE), "unmap middle Shadow entries");
        struct shadow_stats s = stats();
        check(s.objects == 1 && s.slots == 12, "partial zap removes exactly four live Shadow slots");
        nonpresent(pagemap_fd, area, 4, original[0]);
        nonpresent(pagemap_fd, area + 8 * PAGE, 8, original[8]);
        for (unsigned i = 4; i < 8; i++)
            check(!(pagemap(pagemap_fd, area + i * PAGE) & ((1ULL << 63) | (1ULL << 62))),
                  "zapped entries are absent, not stale Shadow entries");
        check(*(volatile unsigned char *)(area + 173) == pattern(173, salt),
              "surviving page really faults back after partial zap");
        idle(isolated, 1);
        same_pfns(area, 4, original);
        same_pfns(area + 8 * PAGE, 504, original + 8);
        content(area, 4 * PAGE, 0, salt);
        content(area + 8 * PAGE, PMD - 8 * PAGE, 8 * PAGE, salt);
        if (discard) {
            /* Disable fresh huge faults before checking discarded pages:
             * their new zero contents must not be restored from the object. */
            policy(0);
            for (size_t i = 4 * PAGE; i < 8 * PAGE; i++)
                check(area[i] == 0, "MADV_DONTNEED refault has genuinely zero new contents");
        } else
            denied(area + 4 * PAGE + 173, 0);
        release(area, PMD, isolated);
    }
    puts("LIFECYCLE partial_munmap DONTNEED no_resurrection zero_refault");
}

static void remap(void)
{
    phase = "partial and whole-PMD mremap";
    for (unsigned full = 0; full < 2; full++) {
        uint64_t isolated = isolated_anon(), original[512];
        unsigned salt = 80 + full, order = full ? 9 : 4;
        unsigned char *area = mapping(PMD, order, salt);
        snapshot(area, 512, original);
        (void)prepare(area, order, isolated);
        size_t bytes = full ? PMD : 16 * PAGE;
        unsigned char *destination = aligned_map(PMD);
        check(mremap(area, bytes, bytes, MREMAP_MAYMOVE | MREMAP_FIXED, destination) == destination,
              "real fixed-address mremap moves Shadow source");
        idle(isolated, 1);
        same_pfns(destination, bytes / PAGE, original);
        content(destination, bytes, 0, salt);
        folio(destination, order);
        denied(area + 173, 0);
        if (!full) {
            same_pfns(area + bytes, (PMD - bytes) / PAGE, original + bytes / PAGE);
            content(area + bytes, PMD - bytes, bytes, salt);
        }
        check(!munmap(destination, PMD), "release destination including unused reservation");
        release(area, PMD, isolated);
    }
    puts("LIFECYCLE partial_mremap whole_PMD_mremap new_VA_same_PFN old_VA_absent");
}

struct fault_thread {
    pthread_barrier_t *barrier;
    unsigned char *address;
    unsigned char value;
};

static void *fault_reader(void *argument)
{
    struct fault_thread *state = argument;
    pthread_barrier_wait(state->barrier);
    state->value = *(volatile unsigned char *)state->address;
    return NULL;
}

static void competing_faults(void)
{
    phase = "two real faults arbitrate one Shadow object";
    uint64_t isolated = isolated_anon(), original[512];
    unsigned char *area = mapping(PMD, 4, 90);
    snapshot(area, 512, original);
    (void)prepare(area, 4, isolated);
    struct shadow_stats before = stats();
    pthread_barrier_t barrier;
    check(!pthread_barrier_init(&barrier, NULL, 3), "create simultaneous fault barrier");
    struct fault_thread args[2] = {
        {.barrier = &barrier, .address = area + 173},
        {.barrier = &barrier, .address = area + 15 * PAGE + 173},
    };
    pthread_t threads[2];
    for (unsigned i = 0; i < 2; i++)
        check(!pthread_create(&threads[i], NULL, fault_reader, &args[i]), "start faulting userspace thread");
    pthread_barrier_wait(&barrier);
    for (unsigned i = 0; i < 2; i++)
        check(!pthread_join(threads[i], NULL), "join simultaneous fault thread");
    check(!pthread_barrier_destroy(&barrier), "destroy fault barrier");
    check(args[0].value == pattern(173, 90) && args[1].value == pattern(15 * PAGE + 173, 90),
          "both competing user loads observe preserved bytes");
    idle(isolated, 1);
    struct shadow_stats after = stats();
    check(after.faults == before.faults + 1, "one winner restores object without duplicate accounting");
    same_pfns(area, 512, original);
    content(area, PMD, 0, 90);
    release(area, PMD, isolated);
    puts("LIFECYCLE concurrent_faults one_restore no_double_reference_release");
}

static void rejected_candidates(void)
{
    phase = "shared and real pinned candidates rejected";
    for (unsigned shared = 0; shared < 2; shared++) {
        uint64_t isolated = isolated_anon(), original[512];
        unsigned salt = 100 + shared;
        unsigned char *area = mapping(PMD, 4, salt);
        snapshot(area, 512, original);
        int pin_fd = -1, finish[2] = {-1, -1};
        pid_t child = -1;
        if (shared) {
            check(!pipe(finish), "create shared-source lifetime pipe");
            child = fork();
            check(child >= 0, "fork genuinely shared anonymous folio");
            if (!child) {
                char token;
                close(finish[1]);
                _exit(read(finish[0], &token, 1) == 1 ? 0 : 1);
            }
            close(finish[0]);
        } else {
            pin_fd = open("/sys/kernel/debug/gup_test", O_RDWR);
            check(pin_fd >= 0, "real long-term GUP pin interface");
            struct pin_longterm_test pin = {
                .addr = (uintptr_t)area, .size = 16 * PAGE,
                .flags = PIN_LONGTERM_TEST_FLAG_USE_WRITE | PIN_LONGTERM_TEST_FLAG_USE_FAST,
            };
            check(!ioctl(pin_fd, PIN_LONGTERM_TEST_START, &pin), "hold actual FOLL_PIN references");
        }
        set_target(getpid(), area, 16 * PAGE);
        int result = shadow_try("prepare 1\n");
        check(result == -ENOENT || result == -ENODATA || result == -EBUSY || result == -EAGAIN,
              "real C2 selection refuses shared or pinned source");
        command(MANAGER "control", "clear_target\n");
        idle(isolated, 1);
        same_pfns(area, 512, original);
        content(area, PMD, 0, salt);
        if (pin_fd >= 0) {
            check(!ioctl(pin_fd, PIN_LONGTERM_TEST_STOP), "release real pinned references");
            close(pin_fd);
        } else {
            check(write(finish[1], "X", 1) == 1, "release sharing child");
            close(finish[1]);
            int status;
            check(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status),
                  "sharing child exits cleanly");
        }
        release(area, PMD, isolated);
    }
    puts("LIFECYCLE shared_source_rejected real_longterm_pin_rejected unchanged_mapping");
}

struct pin_race_thread {
    pthread_barrier_t *barrier;
    struct pin_longterm_test request;
    int fd, result;
    unsigned delay_us;
};

static void *pin_race_start(void *argument)
{
    struct pin_race_thread *state = argument;
    pthread_barrier_wait(state->barrier);
    if (state->delay_us)
        usleep(state->delay_us);
    int result = ioctl(state->fd, PIN_LONGTERM_TEST_START, &state->request);
    state->result = result ? -errno : 0;
    /* The gup_test interface retains its real FOLL_PIN references after
     * this thread returns. Only main issues STOP after checking Shadow. */
    return NULL;
}

static void fast_gup_race(void)
{
    phase = "real fast-GUP pin versus Shadow preparation";
    const unsigned rounds = 32, salt = 142;
    uint64_t isolated = isolated_anon(), original[512];
    unsigned char *area = mapping(PMD, 4, salt);
    unsigned char *pinned_copy = malloc(16 * PAGE);
    check(pinned_copy != NULL, "allocate independent buffer for reading held pinned pages");
    snapshot(area, 512, original);
    int fd = open("/sys/kernel/debug/gup_test", O_RDWR);
    check(fd >= 0, "open actual fast long-term GUP pin interface");
    unsigned prepared = 0, rejected = 0;

    for (unsigned round = 0; round < rounds; round++) {
        set_target(getpid(), area, 16 * PAGE);
        pthread_barrier_t barrier;
        check(!pthread_barrier_init(&barrier, NULL, 2), "create prepare/pin start barrier");
        struct pin_race_thread state = {
            .barrier = &barrier,
            .request = {
                .addr = (uintptr_t)area, .size = 16 * PAGE,
                .flags = PIN_LONGTERM_TEST_FLAG_USE_FAST | PIN_LONGTERM_TEST_FLAG_USE_WRITE,
            },
            .fd = fd,
            /* Half the rounds have no delay; the remaining rounds bias
             * scheduling in opposite directions without assuming a winner. */
            .delay_us = round % 4 == 2 ? 500 : 0,
        };
        pthread_t thread;
        check(!pthread_create(&thread, NULL, pin_race_start, &state), "start real pinning competitor");
        pthread_barrier_wait(&barrier);
        if (round % 4 == 3)
            usleep(500);
        int result = shadow_try("prepare 1\n");
        check(!pthread_join(thread, NULL), "wait until real long-term pin acquisition completes");
        check(!pthread_barrier_destroy(&barrier), "destroy pin race barrier");
        check(!state.result, "fast-GUP succeeds through present mapping or actual Shadow fault");
        check(!result || result == -EBUSY || result == -EAGAIN ||
              result == -ENODATA || result == -ENOENT,
              "prepare only succeeds or rejects a known pin/selection race");
        if (result)
            rejected++;
        else
            prepared++;
        command(MANAGER "control", "clear_target\n");

        /* Do not read the source first: a test load could itself restore
         * buggy leftover Shadow and hide the pin/prepare race. */
        idle(isolated, 1);
        same_pfns(area, 512, original);
        uint64_t destination = (uintptr_t)pinned_copy;
        check(!ioctl(fd, PIN_LONGTERM_TEST_READ, &destination),
              "kernel reads through still-held real pinned-page references");
        content(pinned_copy, 16 * PAGE, 0, salt);
        content(area, PMD, 0, salt);
        check(!ioctl(fd, PIN_LONGTERM_TEST_STOP), "release pins only after ownership and content checks");

        /* The race must leave no poisoned refcount, slot, or selector
         * state: the same real folio must support a fresh round trip. */
        (void)prepare(area, 4, isolated);
        check(*(volatile unsigned char *)(area + 173) == pattern(173, salt),
              "unpin permits a fresh real Shadow prepare and user-fault restore");
        idle(isolated, 1);
        same_pfns(area, 512, original);
        folio(area, 4);
    }
    close(fd);
    free(pinned_copy);
    release(area, PMD, isolated);
    printf("LIFECYCLE fast_GUP_prepare_race rounds=%u prepare_success=%u prepare_rejected=%u"
           " pin_held_no_Shadow full_content unpin_retry isolation_balanced\n",
           rounds, prepared, rejected);
}

struct child_owner {
    pid_t pid;
    int finish;
    unsigned char *area;
    uint64_t first_pfn;
};

static struct child_owner child_owner(unsigned order, unsigned salt)
{
    int ready[2], finish[2];
    check(!pipe(ready) && !pipe(finish), "create independent child-owner pipes");
    pid_t child = fork();
    check(child >= 0, "fork before allocating each mm's independent source folio");
    if (!child) {
        close(ready[0]);
        close(finish[1]);
        close(pagemap_fd);
        pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
        check(pagemap_fd >= 0, "child uses its own pagemap, not inherited parent file");
        unsigned char *area = mapping(PMD, order, salt);
        uint64_t message[2] = {(uintptr_t)area, pfn(area)};
        if (write(ready[1], message, sizeof(message)) != sizeof(message))
            _exit(2);
        close(ready[1]);
        char action;
        if (read(finish[0], &action, 1) != 1)
            _exit(3);
        if (action == 'R') {
            content(area, PMD, 0, salt);
            if (pfn(area) != message[1])
                _exit(4);
            folio(area, order);
        } else if (action != 'X')
            _exit(5);
        _exit(0); /* X intentionally leaves live Shadow for exit_mmap. */
    }
    close(ready[1]);
    close(finish[0]);
    uint64_t message[2];
    check(read(ready[0], message, sizeof(message)) == sizeof(message),
          "receive actual child address and physical folio");
    close(ready[0]);
    return (struct child_owner){child, finish[1], (unsigned char *)(uintptr_t)message[0], message[1]};
}

static void child_finish(struct child_owner *child, char action)
{
    check(write(child->finish, &action, 1) == 1, "release child for actual fault or exit");
    close(child->finish);
    int status;
    check(waitpid(child->pid, &status, 0) == child->pid && WIFEXITED(status) && !WEXITSTATUS(status),
          "child completes without corruption, stale fault or blocked exit");
}

static int child_pagemap(pid_t pid)
{
    char path[80];
    snprintf(path, sizeof(path), "/proc/%d/pagemap", pid);
    int fd = open(path, O_RDONLY);
    check(fd >= 0, "inspect live child's actual page tables");
    return fd;
}

static void owner_exit(void)
{
    phase = "last mm_user exits with live Shadow";
    uint64_t isolated = isolated_anon();
    struct child_owner child = child_owner(4, 110);
    set_target(child.pid, child.area, 16 * PAGE);
    command(SHADOW "control", "prepare 1\n");
    struct entry e[MAX_OBJECTS];
    check(entries(e) == 1 && e[0].pid == child.pid && e[0].pfn == child.first_pfn,
          "Shadow holds the child's genuinely isolated source");
    int fd = child_pagemap(child.pid);
    nonpresent(fd, child.area, 16, child.first_pfn);
    close(fd);
    check(isolated_anon() == isolated && stats().pages == 16 &&
          !(page_flags(child.first_pfn) & (1ULL << KPF_LRU)),
          "child isolation reference is retained with the charge in Shadow pool");
    child_finish(&child, 'X');
    /* No explicit cancel or clear_target: both the persistent C2 observer
     * and the Shadow object must hold only mm_count after preparation.
     * Neither may prevent exit_mmap from zapping the remaining entries. */
    idle(isolated, 1);
    command(MANAGER "control", "clear_target\n");
    puts("LIFECYCLE owner_exit target_retained no_mm_users_self_hold zap_releases_metadata_and_folio");
}

static void same_mm_batch(void)
{
    phase = "512-slot metadata and cross-table single-mm batch";
    uint64_t isolated = isolated_anon(), original[1024];
    unsigned char *area = mapping(2 * PMD, 9, 120);
    snapshot(area, 1024, original);
    set_target(getpid(), area, 2 * PMD);
    struct shadow_stats before = stats();
    command(SHADOW "control", "prepare 2\n");
    command(MANAGER "control", "clear_target\n");
    struct entry e[MAX_OBJECTS];
    check(entries(e) == 2, "two selected PMD folios become real Shadow objects");
    struct shadow_stats after = stats();
    check(after.objects == 2 && after.slots == 1024 && after.metadata == 2 && after.pages == 1024,
          "two real PTE tables each own all 512 metadata slots");
    check(after.groups == before.groups + 1 && after.flushes == before.flushes + 1,
          "two folios in one mm share exactly one prepare guest flush");
    check(after.demotions == before.demotions + 2,
          "each original PMD demotion has separate preparation accounting");
    check(isolated_anon() == isolated, "batch transfers native isolation charges to private Shadow pool");
    check(e[0].table_pfn && e[1].table_pfn && e[0].table_pfn != e[1].table_pfn,
          "cross-table batch uses distinct real PTE tables");
    for (unsigned i = 0; i < 2; i++) {
        check(e[i].pid == getpid() && e[i].order == 9 && e[i].slots == 512 && !e[i].slot,
              "full metadata table covers a genuine order-9 object");
        check(e[i].address == (uintptr_t)area || e[i].address == (uintptr_t)area + PMD,
              "batch contains only explicitly scoped mappings");
        unsigned offset = (e[i].address - (uintptr_t)area) / PAGE;
        check(e[i].pfn == original[offset], "batch entry matches original physical folio");
        check(!(page_flags(e[i].pfn) & (1ULL << KPF_LRU)), "batch source remains physically isolated");
        physical_folio(e[i].pfn, 9);
    }
    check(e[0].address != e[1].address && e[0].token != e[1].token,
          "batch objects have distinct identity and address");
    nonpresent(pagemap_fd, area, 512, original[0]);
    nonpresent(pagemap_fd, area + PMD, 512, original[512]);
    check(*(volatile unsigned char *)(area + 511 * PAGE + 173) == pattern(511 * PAGE + 173, 120),
          "last metadata slot triggers a real first-table restore");
    command(SHADOW "control", "drain\n");
    after = stats();
    check(after.objects == 1 && after.slots == 512 && after.metadata == 1,
          "one restore retires only its own metadata table");
    nonpresent(pagemap_fd, area + PMD, 512, original[512]);
    check(*(volatile unsigned char *)(area + PMD + 173) == pattern(PMD + 173, 120),
          "first slot of next table independently restores");
    idle(isolated, 1);
    same_pfns(area, 1024, original);
    folio(area, 9);
    folio(area + PMD, 9);
    content(area, 2 * PMD, 0, 120);
    release(area, 2 * PMD, isolated);
    puts("BATCH one_mm two_tables 512_slots_per_table one_guest_flush two_PMD_demotions");
}

static void multiple_mm_batch(void)
{
    phase = "one batch spanning two real independent mms";
    uint64_t isolated = isolated_anon(), original[512];
    struct child_owner child = child_owner(9, 130);
    unsigned char *area = mapping(PMD, 9, 131);
    snapshot(area, 512, original);
    check(original[0] != child.first_pfn, "parent and child own distinct unshared physical folios");
    command(MANAGER "control", "clear_target\n");
    /* A disk Guest also has unrelated order-9 folios. Cost selection alone
     * cannot scope an untargeted cross-mm batch to these two sources, and
     * the single-mm target cannot express both owners. Position the actual
     * source folios at the native LRU tail using the existing test fixture;
     * native selection avoids PromoteAge reordering other cold folios ahead
     * of them. This case tests Shadow's cross-mm grouping, while core_orders
     * and same_mm_batch retain coverage of the mixed-cost selector. */
    command(MANAGER "control", "lru_tail %d 0x%lx\n", child.pid, (unsigned long)child.area);
    command(MANAGER "control", "lru_tail %d 0x%lx\n", getpid(), (unsigned long)area);
    command(MANAGER "control", "selector linux_lru\n");
    struct shadow_stats before = stats();
    int prepared = shadow_try("prepare 2\n");
    command(MANAGER "control", "selector mixed_cost\n");
    check(!prepared, "one native selection prepares the independent cross-mm sources");
    struct entry e[MAX_OBJECTS];
    check(entries(e) == 2, "cross-mm batch obtains both independent order-9 candidates");
    unsigned saw_parent = 0, saw_child = 0;
    for (unsigned i = 0; i < 2; i++) {
        check(e[i].order == 9 && e[i].slots == 512, "cross-mm batch consists only of complete huge folios");
        /* Untargeted C2 rmap ownership is an mm, not necessarily one PID.
         * Match the independently known physical/address identity first;
         * debug pid=0 is valid when no explicit target task was supplied. */
        if (e[i].address == (uintptr_t)area && e[i].pfn == original[0]) {
            check(!e[i].pid || e[i].pid == getpid(), "optional debug PID agrees with parent identity");
            saw_parent++;
        } else if (e[i].address == (uintptr_t)child.area && e[i].pfn == child.first_pfn) {
            check(!e[i].pid || e[i].pid == child.pid, "optional debug PID agrees with child identity");
            saw_child++;
        } else
            check(0, "batch must not reclaim an unrelated process");
    }
    check(saw_parent == 1 && saw_child == 1, "exactly one selected folio belongs to each mm");
    struct shadow_stats after = stats();
    check(after.batches == before.batches + 1 && after.groups == before.groups + 2 &&
          after.flushes == before.flushes + 2,
          "one cross-mm batch issues one guest flush per actually modified mm");
    check(after.demotions == before.demotions + 2 && after.metadata == 2 && after.slots == 1024,
          "cross-mm preparation counts PMD demotions and metadata independently");
    check(isolated_anon() == isolated && after.pages == 1024 &&
          !(page_flags(original[0]) & (1ULL << KPF_LRU)) &&
          !(page_flags(child.first_pfn) & (1ULL << KPF_LRU)),
          "cross-mm pool owns both isolated folios without duplicate native charges");
    nonpresent(pagemap_fd, area, 512, original[0]);
    int fd = child_pagemap(child.pid);
    nonpresent(fd, child.area, 512, child.first_pfn);
    close(fd);
    child_finish(&child, 'R');
    content(area, PMD, 0, 131);
    idle(isolated, 1);
    same_pfns(area, 512, original);
    release(area, PMD, isolated);
    puts("BATCH two_mms one_batch two_guest_flushes independent_PFNs real_child_fault");
    puts("PASS C3 batch 512_slots cross_table per_mm_flush independent_PMD_preparation");
}

static uint64_t backend_value(const char *key)
{
    char *text = read_text(TEST_BACKEND "stats");
    uint64_t value = field(text, key);
    free(text);
    return value;
}

static void backend_wait(uint64_t completions)
{
    unsigned attempt;
    for (attempt = 0; attempt < 500; attempt++) {
        if (backend_value("complete_count") >= completions)
            break;
        usleep(10000);
    }
    check(attempt < 500 && backend_value("complete_count") == completions,
          "one real asynchronous snapshot completion arrives");
}

static void snapshot_content(uint64_t token, unsigned pages, unsigned salt, int ready)
{
    command(TEST_BACKEND "control", "select %" PRIu64 "\n", token);
    int fd = open(TEST_BACKEND "snapshot", O_RDONLY);
    if (!ready) {
        check(fd < 0 && errno == EAGAIN, "uncopied backend snapshot cannot be read as completed data");
        return;
    }
    check(fd >= 0, "open actual copied backend bytes");
    size_t bytes = pages * PAGE;
    unsigned char *copy = malloc(bytes);
    check(copy != NULL, "allocate snapshot readback buffer");
    size_t used = 0;
    while (used < bytes) {
        ssize_t n = read(fd, copy + used, bytes - used);
        check(n > 0 && (size_t)n <= bytes - used, "read complete independently stored snapshot");
        used += n;
    }
    unsigned char extra;
    check(read(fd, &extra, 1) == 0, "snapshot has exactly the complete folio length");
    close(fd);
    content(copy, bytes, 0, salt);
    free(copy);
}

static void offsets(uint64_t token, unsigned char *area, unsigned pages, int saved)
{
    char *text = read_text(SHADOW "offsets");
    unsigned seen[512] = {0}, matched = 0;
    check(pages <= 512, "bounded metadata slot inspection");
    for (char *line = text; line && *line; ) {
        uint64_t found, value;
        unsigned long address;
        if (sscanf(line, "slot token=%" SCNu64 " address=0x%lx value=%" SCNu64,
                   &found, &address, &value) == 3 && found == token) {
            check(address >= (uintptr_t)area && address < (uintptr_t)area + pages * PAGE &&
                  !(address & (PAGE - 1)), "backend metadata belongs to the live source range");
            unsigned index = (address - (uintptr_t)area) / PAGE;
            check(!seen[index]++, "each backend metadata slot appears exactly once");
            check(value == (saved ? index * PAGE + 1 : UINT64_MAX),
                  saved ? "offset zero is represented by raw slot one, not pending/empty" :
                          "unfinished/failed save retains explicit pending slots");
            matched++;
        }
        line = strchr(line, '\n');
        if (line)
            line++;
    }
    free(text);
    check(matched == pages, "inspect all real metadata slots for this token");
}

static void asynchronous_backend(void)
{
    phase = "registered asynchronous snapshot backend interface";
    check(!backend_value("registered"), "test module starts without silently registering a backend");
    command(TEST_BACKEND "control", "register\n");
    check(backend_value("registered") == 1, "real backend registration succeeds");
    char *state = read_text(SHADOW "stats");
    check(field(state, "backend_present") == 1, "C3 core observes the registered callbacks");
    free(state);

    /* Complete a genuine copy at backend offset zero. The returned data
     * is an independent copy, not a direct read through the source VMA. */
    uint64_t isolated = isolated_anon(), original[512];
    unsigned char *area = mapping(PMD, 4, 150);
    snapshot(area, 512, original);
    struct entry e = prepare(area, 4, isolated);
    command(TEST_BACKEND "control", "delay 50\n");
    uint64_t completed = backend_value("complete_count"), success = backend_value("complete_success");
    uint64_t cancelled = backend_value("cancel_count");
    command(SHADOW "control", "submit %" PRIu64 "\n", e.token);
    check(shadow_try("submit %" PRIu64 "\n", e.token) == -EALREADY,
          "duplicate submit does not acquire a second backend reference");
    check(backend_try("unregister\n") == -EBUSY, "backend cannot unregister while a save owner remains live");
    backend_wait(completed + 1);
    check(backend_value("complete_success") == success + 1 && !backend_value("last_rc") &&
          !backend_value("last_status"), "real asynchronous copy completes successfully");
    snapshot_content(e.token, 16, 150, 1);
    offsets(e.token, area, 16, 1);
    nonpresent(pagemap_fd, area, 16, original[0]);
    check(backend_try("complete %" PRIu64 " 0 0\n", e.token) == -EALREADY,
          "second completion cannot overwrite successful metadata");
    offsets(e.token, area, 16, 1);
    check(shadow_try("commit %" PRIu64 "\n", e.token) == -EOPNOTSUPP,
          "snapshot interface success does not fabricate a C4 host commit");
    check(*(volatile unsigned char *)(area + 173) == pattern(173, 150),
          "saved Shadow still restores resident original content through a real fault");
    idle(isolated, 1);
    check(backend_value("cancel_count") == cancelled + 1 && !backend_value("active_jobs"),
          "restore drains backend activity before returning the isolation reference");
    snapshot_content(e.token, 16, 150, 1);
    check(backend_try("complete %" PRIu64 " 0 0\n", e.token) == -ESTALE,
          "completion after retirement is rejected by token identity");
    same_pfns(area, 512, original);
    content(area, PMD, 0, 150);
    release(area, PMD, isolated);
    command(TEST_BACKEND "control", "reset\n");

    /* A pending real save must be canceled, not allowed to read a folio
     * after fault/exit returned that folio to the ordinary MM lifecycle. */
    for (unsigned exiting = 0; exiting < 2; exiting++) {
        command(TEST_BACKEND "control", "delay 60000\n");
        cancelled = backend_value("cancel_count");
        completed = backend_value("complete_count");
        struct child_owner child = {0};
        unsigned salt = 151 + exiting;
        if (exiting) {
            child = child_owner(4, salt);
            set_target(child.pid, child.area, 16 * PAGE);
            command(SHADOW "control", "prepare 1\n");
            command(MANAGER "control", "clear_target\n");
            struct entry found[MAX_OBJECTS];
            check(entries(found) == 1 && found[0].pfn == child.first_pfn,
                  "pending-exit save belongs to an actual child folio");
            e = found[0];
        } else {
            area = mapping(PMD, 4, salt);
            snapshot(area, 512, original);
            e = prepare(area, 4, isolated);
        }
        command(SHADOW "control", "submit %" PRIu64 "\n", e.token);
        check(backend_value("active_jobs") == 1, "delayed save is genuinely pending");
        snapshot_content(e.token, 16, salt, 0);
        check(backend_try("unregister\n") == -EBUSY, "pending backend cannot unregister");
        if (exiting)
            child_finish(&child, 'X');
        else
            check(*(volatile unsigned char *)(area + 173) == pattern(173, salt),
                  "user fault cancels pending asynchronous save");
        idle(isolated, 1);
        check(backend_value("cancel_count") == cancelled + 1 &&
              backend_value("complete_count") == completed && !backend_value("active_jobs"),
              "cancellation drains delayed work without a late completion");
        check(backend_try("complete %" PRIu64 " 0 0\n", e.token) == -ESTALE,
              "pending cancellation rejects stale completion");
        if (!exiting) {
            same_pfns(area, 512, original);
            content(area, PMD, 0, salt);
            release(area, PMD, isolated);
        }
        command(TEST_BACKEND "control", "reset\n");
    }

    /* Error completion also has one winner. A later success must not
     * silently turn its metadata into a saved backing reference. */
    area = mapping(PMD, 4, 153);
    snapshot(area, 512, original);
    e = prepare(area, 4, isolated);
    command(TEST_BACKEND "control", "delay 20\n");
    command(TEST_BACKEND "control", "error 1\n");
    completed = backend_value("complete_count");
    uint64_t errors = backend_value("complete_errors");
    command(SHADOW "control", "submit %" PRIu64 "\n", e.token);
    backend_wait(completed + 1);
    check(backend_value("complete_errors") == errors + 1 &&
          (int64_t)backend_value("last_status") == -EIO && !backend_value("last_rc"),
          "asynchronous error completion is accepted once and recorded as failure");
    offsets(e.token, area, 16, 0);
    check(backend_try("complete %" PRIu64 " 0 0\n", e.token) == -EALREADY,
          "later success cannot overwrite the accepted failure");
    offsets(e.token, area, 16, 0);
    check(*(volatile unsigned char *)(area + 173) == pattern(173, 153),
          "failed save retains original resident bytes for actual fault restore");
    idle(isolated, 1);
    same_pfns(area, 512, original);
    content(area, PMD, 0, 153);
    release(area, PMD, isolated);
    command(TEST_BACKEND "control", "unregister\n");
    check(!backend_value("registered"), "unregister succeeds after every owner and callback drains");
    command(TEST_BACKEND "control", "reset\n");
    check(!backend_value("jobs") && !backend_value("active_jobs"), "test backend snapshots and work retire cleanly");
    state = read_text(SHADOW "stats");
    check(!field(state, "backend_present"), "C3 core no longer retains backend callbacks");
    free(state);
    puts("PASS C3 backend_async real_snapshot offset_zero duplicate_reject failure_once"
         " pending_fault_cancel pending_exit_cancel unregister_busy stale_reject no_host_commit");
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    check(sysconf(_SC_PAGESIZE) == PAGE, "x86 4KiB base-page platform");
    pagemap_fd = open("/proc/self/pagemap", O_RDONLY);
    flags_fd = open("/proc/kpageflags", O_RDONLY);
    check(pagemap_fd >= 0 && flags_fd >= 0, "privileged physical page inspection");
    struct sigaction action = {.sa_handler = permission_handler};
    sigemptyset(&action.sa_mask);
    check(!sigaction(SIGSEGV, &action, NULL), "install real permission-fault probe");
    command(TRACK "control", "disable\n");
    command(TRACK "control", "capacity %lu %lu\n", 2UL * 1024 * 1024 * 1024,
            2UL * 1024 * 1024 * 1024);
    command(TRACK "control", "reset\n");
    command(MANAGER "control", "disable\n");
    command(MANAGER "control", "putback\n");
    command(MANAGER "control", "clear_target\n");
    command(MANAGER "control", "batch 128 6400 128 4 10\n");
    for (unsigned order = 0; order <= 9; order++)
        if (order != 1)
            command(MANAGER "control", "cost %u %lu\n", order, 1UL << order);
    command(THP "khugepaged/scan_sleep_millisecs", "600000\n");
    uint64_t isolated = isolated_anon();
    idle(isolated, 1);
    core_orders();
    permissions();
    soft_dirty();
    locked_memory();
    fork_cow();
    partial_zap();
    remap();
    competing_faults();
    rejected_candidates();
    fast_gup_race();
    owner_exit();
    puts("PASS C3 lifecycle permissions fork_COW partial_zap mremap exit concurrent_faults pin_shared_reject fast_GUP_race");
    same_mm_batch();
    multiple_mm_batch();
    backend();
    asynchronous_backend();
    idle(isolated, 1);
    char *text = read_text(SHADOW "stats");
    printf("FINAL_SHADOW_STATS\n%s", text);
    free(text);
    close(flags_fd);
    close(pagemap_fd);
    printf("PASS CHAMELEON_C3 checks=%u\n", checks);
    return 0;
}
