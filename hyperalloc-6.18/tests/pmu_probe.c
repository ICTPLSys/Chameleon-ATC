#define _GNU_SOURCE
#include <cpuid.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/perf_event.h>
#include <linux/kernel-page-flags.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Capability validation, not a calibration or Chameleon implementation.
 * Ice Lake server encodings are from Linux 6.18 tools/perf/pmu-events/
 * arch/x86/icelakex/{cache,virtual-memory}.json. */
#define PAGE_BYTES 4096UL
#define WORK_PAGES 16384UL
#define PFN_MASK ((1ULL << 55) - 1)
#define MIN_VALID_SAMPLES 8

/* An explicit load instruction lets PEBS validate eventing IP, not just that
 * some userspace address was recorded. The miss workload flushes each tested
 * line: touching one byte in each 4KiB page otherwise fits easily in LLC. */
__asm__(
    ".text\n"
    ".globl pmu_memory_loop\n"
    ".type pmu_memory_loop,@function\n"
    "pmu_memory_loop:\n"
    "xor %r8d,%r8d\n"
    "xor %r9d,%r9d\n"
    "1:\n"
    "add $4093,%r8\n"
    "and %rsi,%r8\n"
    "mov %r8,%r10\n"
    "shl $12,%r10\n"
    "add %rdi,%r10\n"
    "test %ecx,%ecx\n"
    "jz 2f\n"
    "clflush (%r10)\n"
    "mfence\n"
    "2:\n"
    ".globl pmu_load_ip\n"
    "pmu_load_ip:\n"
    "movzbq (%r10),%rax\n"
    "add %rax,%r9\n"
    "dec %rdx\n"
    "jnz 1b\n"
    "mov %r9,%rax\n"
    "ret\n"
    ".globl pmu_memory_end\n"
    "pmu_memory_end:\n"
    ".size pmu_memory_loop,.-pmu_memory_loop\n");
extern unsigned long pmu_memory_loop(unsigned char *area, unsigned long mask,
                                     unsigned long iterations, int flush);
extern const char pmu_load_ip[], pmu_memory_end[];
static volatile unsigned long checksum;

struct workload {
    unsigned char *area;
    unsigned long pages;
    unsigned long offset;
    int pagemap;
};
struct counter_read { uint64_t value, enabled, running; };
struct sample_result {
    uint64_t samples, workload_ips, exact_ips, exact_workload_ips, workload_addresses;
    uint64_t matched, bad_address, physical, physical_matches;
    uint64_t physical_mismatch, pagemap_unavailable, lost, malformed;
    bool physical_requested, virtual_verified, physical_verified;
};

static int event_open(struct perf_event_attr *attr, int group)
{
    return syscall(__NR_perf_event_open, attr, 0, -1, group, 0);
}

static void show_file(const char *path)
{
    char line[256];
    FILE *f = fopen(path, "r");
    if (!f) {
        printf("SYSFS %s unavailable errno=%d\n", path, errno);
        return;
    }
    if (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\n")] = 0;
        printf("SYSFS %s = %s\n", path, line);
    }
    fclose(f);
}

static int load_encoding(uint64_t *config, uint64_t *latency)
{
    char line[256], *item;
    unsigned int event = 0, mask = 0, ldlat = 0;
    FILE *f = fopen("/sys/bus/event_source/devices/cpu/events/mem-loads", "r");
    if (!f)
        return -1;
    if (!fgets(line, sizeof(line), f)) {
        fclose(f);
        return -1;
    }
    fclose(f);
    for (item = strtok(line, ","); item; item = strtok(NULL, ",")) {
        if (!strncmp(item, "event=", 6))
            event = strtoul(item + 6, NULL, 0);
        if (!strncmp(item, "umask=", 6))
            mask = strtoul(item + 6, NULL, 0);
        if (!strncmp(item, "ldlat=", 6))
            ldlat = strtoul(item + 6, NULL, 0);
    }
    *config = event | ((uint64_t)mask << 8);
    *latency = ldlat;
    return event ? 0 : -1;
}

static void memory_work(struct workload *w, bool flush)
{
    checksum += pmu_memory_loop(w->area + w->offset, w->pages - 1,
                               w->pages * (flush ? 4 : 64), flush);
}

static bool read_counter(int fd, const char *name)
{
    struct counter_read r = {0};
    ssize_t got = read(fd, &r, sizeof(r));
    if (got != sizeof(r)) {
        printf("COUNTER name=%s unavailable reason=read bytes=%zd errno=%d\n",
               name, got, errno);
        return false;
    }
    printf("COUNTER name=%s value=%" PRIu64 " enabled_ns=%" PRIu64
           " running_ns=%" PRIu64 " status=%s\n", name, r.value,
           r.enabled, r.running, r.value && r.running ? "observed" : "not_observed");
    return r.value && r.running;
}

/* Keep this fd alive through all probes: Intel allocates DS/PEBS buffers on
 * the first PMU event reservation, so max_precise read before open can be 0. */
static int probe_count(struct workload *w, const char *name, uint32_t type,
                       uint64_t config, bool flush)
{
    struct perf_event_attr attr = {
        .type = type, .size = sizeof(attr), .config = config,
        .disabled = 1, .exclude_kernel = 1, .exclude_hv = 1,
        .read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING,
    };
    int fd = event_open(&attr, -1);
    if (fd < 0) {
        printf("COUNTER name=%s unavailable reason=open config=0x%" PRIx64
               " errno=%d message=%s\n", name, config, errno, strerror(errno));
        return -1;
    }
    printf("COUNTER name=%s opened=1 config=0x%" PRIx64 "\n", name, config);
    if (type == PERF_TYPE_HARDWARE)
        show_file("/sys/bus/event_source/devices/cpu/caps/max_precise");
    if (ioctl(fd, PERF_EVENT_IOC_RESET, 0) || ioctl(fd, PERF_EVENT_IOC_ENABLE, 0)) {
        printf("COUNTER name=%s unavailable reason=enable errno=%d\n", name, errno);
        close(fd);
        return -1;
    }
    memory_work(w, flush);
    if (ioctl(fd, PERF_EVENT_IOC_DISABLE, 0))
        printf("COUNTER name=%s disable_failed errno=%d\n", name, errno);
    read_counter(fd, name);
    return fd;
}

static void ring_copy(void *dst, const unsigned char *data, size_t size,
                      uint64_t offset, size_t bytes)
{
    size_t pos = offset % size;
    size_t first = bytes < size - pos ? bytes : size - pos;
    memcpy(dst, data + pos, first);
    memcpy((unsigned char *)dst + first, data, bytes - first);
}

static bool physical_for_address(struct workload *w, uint64_t address, uint64_t *physical)
{
    uint64_t entry;
    if (w->pagemap < 0 ||
        pread(w->pagemap, &entry, sizeof(entry),
              (off_t)((address / PAGE_BYTES) * sizeof(entry))) != sizeof(entry) ||
        !(entry & (1ULL << 63)) || !(entry & PFN_MASK))
        return false;
    *physical = ((entry & PFN_MASK) * PAGE_BYTES) + address % PAGE_BYTES;
    return true;
}

static struct sample_result probe_sampling(struct workload *w, const char *name,
                                           uint32_t type, uint64_t config,
                                           uint64_t config1, unsigned int precise)
{
    struct sample_result result = {0};
    bool load = type == PERF_TYPE_RAW;
    struct perf_event_attr attr = {
        .type = type, .size = sizeof(attr), .config = config, .config1 = config1,
        .disabled = 1, .exclude_kernel = 1, .exclude_hv = 1,
        .sample_period = load ? 128 : 1000000,
        .precise_ip = precise, .sample_type = PERF_SAMPLE_IP,
        .read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING,
        .wakeup_events = 1,
    };
    if (load)
        attr.sample_type |= PERF_SAMPLE_ADDR;
    if (load && precise)
        attr.sample_type |= PERF_SAMPLE_PHYS_ADDR;
    result.physical_requested = !!(attr.sample_type & PERF_SAMPLE_PHYS_ADDR);
    int fd = event_open(&attr, -1);
    if (fd < 0 && result.physical_requested && (errno == EACCES || errno == EPERM)) {
        printf("SAMPLE name=%s physical_request_denied errno=%d retry_virtual=1\n", name, errno);
        attr.sample_type &= ~PERF_SAMPLE_PHYS_ADDR;
        result.physical_requested = false;
        fd = event_open(&attr, -1);
    }
    if (fd < 0) {
        printf("SAMPLE name=%s unavailable reason=open precise=%u config=0x%" PRIx64
               " config1=0x%" PRIx64 " errno=%d message=%s\n",
               name, precise, config, config1, errno, strerror(errno));
        return result;
    }
    printf("SAMPLE name=%s opened=1 precise=%u config=0x%" PRIx64
           " config1=0x%" PRIx64 " period=%" PRIu64 " physical_requested=%u\n",
           name, precise, config, config1, (uint64_t)attr.sample_period,
           result.physical_requested);
    size_t length = 65 * PAGE_BYTES;
    struct perf_event_mmap_page *meta = mmap(NULL, length, PROT_READ | PROT_WRITE,
                                            MAP_SHARED, fd, 0);
    if (meta == MAP_FAILED) {
        printf("SAMPLE name=%s unavailable reason=mmap errno=%d\n", name, errno);
        close(fd);
        return result;
    }
    if (ioctl(fd, PERF_EVENT_IOC_RESET, 0) || ioctl(fd, PERF_EVENT_IOC_ENABLE, 0)) {
        printf("SAMPLE name=%s unavailable reason=enable errno=%d\n", name, errno);
        munmap(meta, length);
        close(fd);
        return result;
    }
    memory_work(w, load);
    if (ioctl(fd, PERF_EVENT_IOC_DISABLE, 0)) {
        printf("SAMPLE name=%s unavailable reason=disable errno=%d\n", name, errno);
        munmap(meta, length);
        close(fd);
        return result;
    }
    read_counter(fd, name);
    uint64_t head = __atomic_load_n(&meta->data_head, __ATOMIC_ACQUIRE);
    uint64_t tail = meta->data_tail;
    size_t size = meta->data_size;
    const unsigned char *data = (unsigned char *)meta + meta->data_offset;
    if (!size || meta->data_offset > length || size > length - meta->data_offset ||
        head < tail || head - tail > size) {
        printf("SAMPLE name=%s unavailable reason=ring_overrun_or_invalid_metadata\n", name);
        result.malformed++;
    } else {
        while (tail < head) {
            struct perf_event_header record;
            if (head - tail < sizeof(record)) {
                result.malformed++;
                break;
            }
            ring_copy(&record, data, size, tail, sizeof(record));
            if (record.size < sizeof(record) || record.size > size ||
                record.size > head - tail) {
                result.malformed++;
                break;
            }
            if (record.type == PERF_RECORD_SAMPLE) {
                size_t words = 1 + load + result.physical_requested;
                uint64_t sample[3] = {0};
                if (record.size != sizeof(record) + words * sizeof(uint64_t)) {
                    result.malformed++;
                    break;
                }
                ring_copy(sample, data, size, tail + sizeof(record), words * sizeof(uint64_t));
                result.samples++;
                bool work_ip = sample[0] >= (uintptr_t)pmu_memory_loop &&
                               sample[0] < (uintptr_t)pmu_memory_end;
                bool exact_ip = !!(record.misc & PERF_RECORD_MISC_EXACT_IP);
                bool work_addr = sample[1] >= (uintptr_t)w->area &&
                                 sample[1] < (uintptr_t)w->area + w->pages * PAGE_BYTES;
                result.workload_ips += work_ip;
                result.exact_ips += exact_ip;
                result.exact_workload_ips += work_ip && exact_ip;
                result.workload_addresses += load && work_addr;
                if (load && precise && sample[0] == (uintptr_t)pmu_load_ip && exact_ip) {
                    if (!work_addr || (sample[1] - (uintptr_t)w->area) % PAGE_BYTES != w->offset) {
                        result.bad_address++;
                    } else {
                        result.matched++;
                        if (result.physical_requested && sample[2]) {
                            uint64_t expected = 0;
                            result.physical++;
                            if (!physical_for_address(w, sample[1], &expected))
                                result.pagemap_unavailable++;
                            else if (sample[2] == expected)
                                result.physical_matches++;
                            else
                                result.physical_mismatch++;
                            if (result.matched <= 4)
                                printf("SAMPLE_RECORD name=%s ip=0x%" PRIx64 " vaddr=0x%" PRIx64
                                       " physical=0x%" PRIx64 " pagemap_physical=0x%" PRIx64
                                       " exact_ip=1\n", name, sample[0], sample[1], sample[2], expected);
                        } else if (result.matched <= 4) {
                            printf("SAMPLE_RECORD name=%s ip=0x%" PRIx64 " vaddr=0x%" PRIx64
                                   " physical=0x%" PRIx64 " exact_ip=1\n",
                                   name, sample[0], sample[1], sample[2]);
                        }
                    }
                }
            } else if (record.type == PERF_RECORD_LOST && record.size >= 24) {
                uint64_t payload[2];
                ring_copy(payload, data, size, tail + sizeof(record), sizeof(payload));
                result.lost += payload[1];
            } else if (record.type == PERF_RECORD_LOST_SAMPLES && record.size >= 16) {
                uint64_t lost;
                ring_copy(&lost, data, size, tail + sizeof(record), sizeof(lost));
                result.lost += lost;
            }
            tail += record.size;
        }
        __atomic_store_n(&meta->data_tail, tail, __ATOMIC_RELEASE);
    }
    result.virtual_verified = load && precise && result.matched >= MIN_VALID_SAMPLES &&
                              !result.bad_address && !result.malformed;
    result.physical_verified = result.virtual_verified && result.physical_requested &&
                               result.physical_matches == result.matched &&
                               !result.physical_mismatch && !result.pagemap_unavailable;
    const char *status = precise && load ?
        (result.physical_verified ? "verified_guest_physical" :
         result.physical_mismatch ? "invalid_guest_physical" :
         result.virtual_verified ? "verified_virtual_only" : "not_observed") :
        (precise ?
         (result.exact_workload_ips && !result.malformed ? "exact_ip_samples_observed" : "not_observed") :
         (result.workload_ips && !result.malformed ? "pmi_samples_observed" : "not_observed"));
    printf("SAMPLE name=%s precise=%u samples=%" PRIu64 " workload_ips=%" PRIu64
           " exact_ips=%" PRIu64 " exact_workload_ips=%" PRIu64
           " workload_addresses=%" PRIu64 " matched_ip_address=%" PRIu64
           " bad_address=%" PRIu64 " physical_addresses=%" PRIu64 " physical_matches=%" PRIu64
           " physical_mismatch=%" PRIu64 " pagemap_unavailable=%" PRIu64
           " lost=%" PRIu64 " malformed=%" PRIu64 " physical_requested=%u status=%s\n",
           name, precise, result.samples, result.workload_ips, result.exact_ips, result.exact_workload_ips,
           result.workload_addresses, result.matched, result.bad_address, result.physical,
           result.physical_matches, result.physical_mismatch, result.pagemap_unavailable,
           result.lost, result.malformed, result.physical_requested, status);
    munmap(meta, length);
    close(fd);
    return result;
}

static void probe_pagewalk(struct workload *w, unsigned model)
{
    if (model != 106 && model != 108) {
        puts("PTW unavailable reason=no_validated_raw_encoding_for_cpu_model");
        return;
    }
    struct perf_event_attr attr = {
        .type = PERF_TYPE_RAW, .size = sizeof(attr),
        .disabled = 1, .exclude_kernel = 1, .exclude_hv = 1,
        .read_format = PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED |
                       PERF_FORMAT_TOTAL_TIME_RUNNING,
        .config = 0x1008,
    };
    int pending = event_open(&attr, -1);
    if (pending < 0) {
        printf("PTW unavailable reason=pending_open errno=%d message=%s\n", errno, strerror(errno));
        return;
    }
    attr.config = 0x0e08;
    int completed = event_open(&attr, pending);
    if (completed < 0) {
        printf("PTW unavailable reason=completed_open errno=%d\n", errno);
        close(pending);
        return;
    }
    if (ioctl(pending, PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP) ||
        ioctl(pending, PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP)) {
        printf("PTW unavailable reason=enable errno=%d\n", errno);
    } else {
        memory_work(w, false);
        if (ioctl(pending, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP))
            printf("PTW disable_failed errno=%d\n", errno);
        struct { uint64_t nr, enabled, running, pending, completed; } result;
        ssize_t got = read(pending, &result, sizeof(result));
        if (got == sizeof(result) && result.nr == 2) {
            printf("PTW pending=%" PRIu64 " completed=%" PRIu64
                   " enabled_ns=%" PRIu64 " running_ns=%" PRIu64
                   " ratio=%.3f status=%s\n", result.pending, result.completed,
                   result.enabled, result.running,
                   result.completed ? (double)result.pending / result.completed : 0.0,
                   result.pending && result.completed && result.running ? "observed" : "not_observed");
        } else
            printf("PTW unavailable reason=read bytes=%zd errno=%d\n", got, errno);
    }
    close(completed);
    close(pending);
}

static void select_mthp(unsigned long kb)
{
    const char *base = "/sys/kernel/mm/transparent_hugepage";
    DIR *dir = opendir(base);
    struct dirent *de;
    bool found = false;
    if (!dir) { perror(base); exit(1); }
    while ((de = readdir(dir))) {
        unsigned long size;
        char extra, path[512];
        if (sscanf(de->d_name, "hugepages-%lukB%c", &size, &extra) != 1)
            continue;
        snprintf(path, sizeof(path), "%s/%s/enabled", base, de->d_name);
        if (access(path, F_OK))
            continue;
        FILE *file = fopen(path, "w");
        if (!file) { perror(path); exit(1); }
        if (fprintf(file, "%s\n", size == kb ? "always" : "never") < 0 || fclose(file))
            exit(1);
        found |= size == kb;
    }
    closedir(dir);
    if (!found) { fprintf(stderr, "unsupported mTHP size %lu\n", kb); exit(1); }
}

static void verify_mthp(struct workload *w, unsigned long kb)
{
    unsigned long pages = kb * 1024 / PAGE_BYTES;
    int flags = open("/proc/kpageflags", O_RDONLY);
    if (flags < 0) { perror("kpageflags"); exit(1); }
    for (unsigned long first = 0; first < w->pages; first += pages) {
        uint64_t start;
        if (!physical_for_address(w, (uintptr_t)w->area + first * PAGE_BYTES, &start) ||
            start % (kb * 1024))
            goto fail;
        for (unsigned long i = 0; i < pages; i++) {
            uint64_t physical, bits;
            if (!physical_for_address(w, (uintptr_t)w->area + (first + i) * PAGE_BYTES, &physical) ||
                physical != start + i * PAGE_BYTES ||
                pread(flags, &bits, sizeof(bits), physical / PAGE_BYTES * 8) != sizeof(bits) ||
                !(bits & (1ULL << KPF_THP)) ||
                !(bits & (1ULL << (i ? KPF_COMPOUND_TAIL : KPF_COMPOUND_HEAD))))
                goto fail;
        }
    }
    close(flags);
    printf("MTHP verified_kb=%lu folios=%lu all_pfns_and_head_tail_verified=1\n", kb, w->pages / pages);
    return;
fail:
    fprintf(stderr, "FAIL mTHP mapping/PFN/head-tail validation\n");
    exit(1);
}

int main(int argc, char **argv)
{
    bool require_pebs = false, check_fixed_precise = false;
    unsigned long mthp_kb = 0, offset = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--require-pebs"))
            require_pebs = true;
        else if (!strcmp(argv[i], "--check-fixed-precise"))
            check_fixed_precise = true;
        else if (!strcmp(argv[i], "--mthp-kb") && i + 1 < argc)
            mthp_kb = strtoul(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "--offset") && i + 1 < argc)
            offset = strtoul(argv[++i], NULL, 0);
        else {
            fprintf(stderr, "usage: %s [--require-pebs] [--check-fixed-precise] [--mthp-kb N] [--offset N]\n", argv[0]);
            return 2;
        }
    }
    if (offset >= PAGE_BYTES || (mthp_kb &&
        (mthp_kb < 16 || mthp_kb > 2048 || (mthp_kb & (mthp_kb - 1)))))
        return 2;
    setvbuf(stdout, NULL, _IOLBF, 0);
    unsigned a, b, c, d, model, family;
    char vendor[13] = {0};
    __cpuid(0, a, b, c, d);
    memcpy(vendor, &b, 4); memcpy(vendor + 4, &d, 4); memcpy(vendor + 8, &c, 4);
    __cpuid(1, a, b, c, d);
    family = (a >> 8) & 15;
    model = ((a >> 4) & 15) | ((a >> 12) & 0xf0);
    printf("CPU vendor=%s family=%u model=%u pdcm=%u hypervisor=%u\n",
           vendor, family, model, (c >> 15) & 1, c >> 31);
    if (__get_cpuid(0xa, &a, &b, &c, &d))
        printf("CPUID_PMU version=%u general_counters=%u counter_bits=%u fixed=%u\n",
               a & 255, (a >> 8) & 255, (a >> 16) & 255, d & 31);
    show_file("/proc/sys/kernel/perf_event_paranoid");
    show_file("/sys/bus/event_source/devices/cpu/caps/pmu_name");
    show_file("/sys/bus/event_source/devices/cpu/caps/max_precise");
    show_file("/sys/bus/event_source/devices/cpu/events/mem-loads");
    show_file("/sys/bus/event_source/devices/cpu/events/mem-stores");
    if (sysconf(_SC_PAGESIZE) != PAGE_BYTES) {
        puts("PMU unavailable reason=probe_requires_4KiB_base_pages");
        return 1;
    }
    const size_t alignment = 2UL << 20;
    struct workload w = { .pages = WORK_PAGES, .offset = offset,
        .area = mmap(NULL, WORK_PAGES * PAGE_BYTES + alignment, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
        .pagemap = open("/proc/self/pagemap", O_RDONLY),
    };
    if (w.area == MAP_FAILED)
        return 1;
    size_t prefix = (-(uintptr_t)w.area) & (alignment - 1);
    if (prefix)
        munmap(w.area, prefix);
    w.area += prefix;
    munmap(w.area + w.pages * PAGE_BYTES, alignment - prefix);
    if (mthp_kb)
        select_mthp(mthp_kb);
    if (madvise(w.area, w.pages * PAGE_BYTES, mthp_kb ? MADV_HUGEPAGE : MADV_NOHUGEPAGE)) {
        perror("madvise workload");
        return 1;
    }
    for (unsigned long i = 0; i < w.pages; i++)
        w.area[i * PAGE_BYTES + offset] = (i % 251) + 1;
    if (mthp_kb)
        verify_mthp(&w, mthp_kb);
    printf("WORKLOAD vaddr_start=0x%" PRIxPTR " vaddr_end=0x%" PRIxPTR
           " ip_start=0x%" PRIxPTR " ip_end=0x%" PRIxPTR " load_ip=0x%" PRIxPTR
           " pages=%lu offset=%lu mthp_kb=%lu raw_miss_workload=clflush_mfence_load\n",
           (uintptr_t)w.area, (uintptr_t)w.area + w.pages * PAGE_BYTES,
           (uintptr_t)pmu_memory_loop, (uintptr_t)pmu_memory_end,
           (uintptr_t)pmu_load_ip, w.pages, offset, mthp_kb);
    int cycles = probe_count(&w, "cycles_count", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES, false);
    probe_sampling(&w, "cycles_sample", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES, 0, 0);
    if (check_fixed_precise)
        probe_sampling(&w, "instructions_precise2", PERF_TYPE_HARDWARE,
                       PERF_COUNT_HW_INSTRUCTIONS, 0, 2);
    struct sample_result paper = {0};
    if (!strcmp(vendor, "GenuineIntel") && family == 6 && (model == 106 || model == 108)) {
        int raw = probe_count(&w, "l3_miss_count", PERF_TYPE_RAW, 0x20d1, true);
        if (raw >= 0)
            close(raw);
        probe_sampling(&w, "l3_miss_precise0", PERF_TYPE_RAW, 0x20d1, 0, 0);
        paper = probe_sampling(&w, "l3_miss_precise2", PERF_TYPE_RAW, 0x20d1, 0, 2);
        uint64_t config, latency;
        if (!load_encoding(&config, &latency))
            probe_sampling(&w, "mem_loads_ldlat_precise2", PERF_TYPE_RAW, config, latency, 2);
        else
            puts("SAMPLE name=mem_loads_ldlat_precise2 unavailable reason=no_cpu_mem_loads_event");
        probe_pagewalk(&w, model);
    } else {
        puts("PEBS unavailable reason=no_validated_paper_raw_encoding_for_cpu_model");
    }
    printf("PEBS_VALIDATION event=MEM_LOAD_RETIRED.L3_MISS config=0x20d1"
           " virtual_verified=%u guest_physical_verified=%u matched_samples=%" PRIu64
           " physical_matches=%" PRIu64 " physical_mismatch=%" PRIu64 " required_samples=%u\n",
           paper.virtual_verified, paper.physical_verified, paper.matched,
           paper.physical_matches, paper.physical_mismatch, MIN_VALID_SAMPLES);
    if (cycles >= 0)
        close(cycles);
    if (w.pagemap >= 0)
        close(w.pagemap);
    munmap(w.area, w.pages * PAGE_BYTES);
    puts("PMU_PROBE_COMPLETE (validated samples required; no calibration claim)");
    return require_pebs && !paper.physical_verified ? 1 : 0;
}
