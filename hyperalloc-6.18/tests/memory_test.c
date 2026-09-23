#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define PAGE 4096UL
#define MIB (1024UL * 1024UL)
#define KPF_COMPOUND_HEAD 15
#define KPF_COMPOUND_TAIL 16
#define KPF_THP 22
#define PMD_BYTES (2 * MIB)

static void fatal(const char *what)
{
    perror(what);
    exit(1);
}

static void require(int condition, const char *what)
{
    if (!condition) {
        fprintf(stderr, "FAIL %s\n", what);
        exit(1);
    }
}

static void write_text(const char *path, const char *value)
{
    int fd = open(path, O_WRONLY);
    if (fd < 0)
        fatal(path);
    if (write(fd, value, strlen(value)) != (ssize_t)strlen(value))
        fatal("write sysfs");
    close(fd);
}

static void select_thp(unsigned int size_kb)
{
    const char *base = "/sys/kernel/mm/transparent_hugepage";
    DIR *dir = opendir(base);
    struct dirent *entry;
    char path[512];
    require(dir != NULL, "transparent_hugepage sysfs missing");
    snprintf(path, sizeof(path), "%s/enabled", base);
    write_text(path, "madvise\n");
    while ((entry = readdir(dir))) {
        unsigned int size;
        char trailing;
        if (sscanf(entry->d_name, "hugepages-%ukB%c", &size, &trailing) == 1) {
            snprintf(path, sizeof(path), "%s/%s/enabled", base, entry->d_name);
            if (access(path, F_OK) == 0)
                write_text(path, size == size_kb ? "always\n" : "never\n");
        }
    }
    closedir(dir);
    snprintf(path, sizeof(path), "%s/hugepages-%ukB/enabled", base, size_kb);
    require(access(path, F_OK) == 0, "requested THP size is unsupported");
}

static unsigned char *aligned_region(size_t size)
{
    unsigned char *raw = mmap(NULL, size + PMD_BYTES, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (raw == MAP_FAILED)
        fatal("mmap");
    uintptr_t aligned = ((uintptr_t)raw + PMD_BYTES - 1) & ~(PMD_BYTES - 1);
    size_t prefix = aligned - (uintptr_t)raw;
    if (prefix && munmap(raw, prefix))
        fatal("munmap prefix");
    if (PMD_BYTES > prefix && munmap((void *)(aligned + size), PMD_BYTES - prefix))
        fatal("munmap suffix");
    return (unsigned char *)aligned;
}

static uint64_t read_u64(int fd, uint64_t offset)
{
    uint64_t value;
    if (pread(fd, &value, sizeof(value), (off_t)offset) != sizeof(value))
        fatal("pread pagemap/kpageflags");
    return value;
}

static uint64_t verify_folio(unsigned char *area, size_t folio_size)
{
    int pagemap = open("/proc/self/pagemap", O_RDONLY);
    int flags = open("/proc/kpageflags", O_RDONLY);
    if (pagemap < 0 || flags < 0)
        fatal("open pagemap/kpageflags");
    uint64_t entry = read_u64(pagemap, ((uintptr_t)area / PAGE) * 8);
    uint64_t pfn = entry & ((1ULL << 55) - 1);
    require((entry >> 63) && pfn != 0, "PFN must be present and visible");
    require(pfn % (folio_size / PAGE) == 0, "folio PFN alignment");
    uint64_t head = read_u64(flags, pfn * 8);
    require(head & (1ULL << KPF_COMPOUND_HEAD), "mapping is not a compound head");
    require(head & (1ULL << KPF_THP), "mapping is not a THP folio");
    for (size_t i = 1; i < folio_size / PAGE; i++) {
        entry = read_u64(pagemap, (((uintptr_t)area / PAGE) + i) * 8);
        require(entry >> 63, "mTHP tail must be present");
        require((entry & ((1ULL << 55) - 1)) == pfn + i,
                "mTHP physical frames are not contiguous");
        uint64_t tail_mask = (1ULL << KPF_COMPOUND_TAIL) | (1ULL << KPF_THP);
        require((read_u64(flags, (pfn + i) * 8) & tail_mask) == tail_mask,
                "mTHP tail page or THP flag missing");
    }
    require(!(read_u64(flags, (pfn + folio_size / PAGE) * 8) &
              (1ULL << KPF_COMPOUND_TAIL)), "folio is larger than requested order");
    printf("FOLIO size_kb=%zu pfn=%" PRIu64 " head_flags=0x%" PRIx64 " verified\n",
           folio_size / 1024, pfn, head);
    close(flags);
    close(pagemap);
    return pfn;
}

static void verify_base_pages(unsigned char *area, size_t size,
                              uint64_t original_pfn)
{
    int pagemap = open("/proc/self/pagemap", O_RDONLY);
    int flags = open("/proc/kpageflags", O_RDONLY);
    if (pagemap < 0 || flags < 0)
        fatal("open pagemap/kpageflags after split");
    for (size_t i = 0; i < size / PAGE; i++) {
        uint64_t entry = read_u64(pagemap, (((uintptr_t)area / PAGE) + i) * 8);
        uint64_t pfn = entry & ((1ULL << 55) - 1);
        require((entry >> 63) && pfn == original_pfn + i,
                "native split changed or lost a mapped PFN");
        uint64_t state = read_u64(flags, pfn * 8);
        uint64_t compound = (1ULL << KPF_COMPOUND_HEAD) |
                            (1ULL << KPF_COMPOUND_TAIL) | (1ULL << KPF_THP);
        require(!(state & compound), "native split did not produce base pages");
    }
    close(flags);
    close(pagemap);
}

static unsigned char pattern(size_t page, unsigned int salt)
{
    return (unsigned char)((page * 71 + salt) % 251 + 1);
}

static void check_bytes(unsigned char *p, size_t size, unsigned int salt)
{
    for (size_t i = 0; i < size / PAGE; i++) {
        require(p[i * PAGE] == pattern(i, salt), "page first byte corrupted");
        require(p[(i + 1) * PAGE - 1] == pattern(i, salt), "page last byte corrupted");
    }
}

static void fill_bytes(unsigned char *p, size_t size, unsigned int salt)
{
    for (size_t i = 0; i < size / PAGE; i++)
        memset(p + i * PAGE, pattern(i, salt), PAGE);
}

static void check_all_bytes(unsigned char *p, size_t size, unsigned int salt)
{
    for (size_t page = 0; page < size / PAGE; page++) {
        unsigned char expected = pattern(page, salt);
        for (size_t byte = 0; byte < PAGE; byte++)
            require(p[page * PAGE + byte] == expected,
                    "native split data corruption");
    }
}

static void native_split_test(unsigned int source_kb, unsigned int target_kb)
{
    size_t size = source_kb * 1024UL;
    size_t target_size = target_kb * 1024UL;
    unsigned int target_order = 0;
    require(target_size >= PAGE && target_size < size &&
            (target_size & (target_size - 1)) == 0, "invalid split target");
    for (size_t bytes = PAGE; bytes < target_size; bytes <<= 1)
        target_order++;

    select_thp(source_kb);
    unsigned char *p = aligned_region(size);
    require(madvise(p, size, MADV_HUGEPAGE) == 0, "split MADV_HUGEPAGE");
    fill_bytes(p, size, 41);
    /* Prevent khugepaged from collapsing the split pages during inspection.
     * This advice changes VMA policy, not the physical folio: prove that the
     * source is still large immediately before invoking the split interface. */
    require(madvise(p, size, MADV_NOHUGEPAGE) == 0, "split MADV_NOHUGEPAGE");
    uint64_t original_pfn = verify_folio(p, size);
    char request[128];
    int length = snprintf(request, sizeof(request), "%d,0x%lx,0x%lx,%u\n",
                          getpid(), (unsigned long)p,
                          (unsigned long)(p + size), target_order);
    require(length > 0 && (size_t)length < sizeof(request), "split request size");
    write_text("/sys/kernel/debug/split_huge_pages", request);

    /* A successful debugfs write can skip a busy folio. Validate actual
     * physical metadata, every child, and stable PFNs rather than the write. */
    if (target_order == 0) {
        verify_base_pages(p, size, original_pfn);
    } else {
        for (size_t offset = 0; offset < size; offset += target_size) {
            uint64_t child_pfn = verify_folio(p + offset, target_size);
            require(child_pfn == original_pfn + offset / PAGE,
                    "native split child changed PFN");
        }
    }
    check_all_bytes(p, size, 41);
    fill_bytes(p, size, 73);
    check_all_bytes(p, size, 73);
    require(munmap(p, size) == 0, "native split munmap");
    printf("PASS split source_kb=%u target_kb=%u children=%zu preserved_pfns full_data\n",
           source_kb, target_kb, size / target_size);
}

static void mthp_test(unsigned int size_kb)
{
    size_t size = 8 * MIB;
    select_thp(size_kb);
    unsigned char *p = aligned_region(size);
    require(madvise(p, size, MADV_HUGEPAGE) == 0, "MADV_HUGEPAGE");
    fill_bytes(p, size, 13);
    verify_folio(p, size_kb * 1024UL);
    check_bytes(p, size, 13);

    /* Exercise write-protect/COW without changing the parent's contents. */
    pid_t child = fork();
    if (child < 0)
        fatal("fork");
    if (child == 0) {
        for (size_t i = 0; i < size / PAGE; i += 3)
            memset(p + i * PAGE, 0xa7, PAGE);
        for (size_t i = 0; i < size / PAGE; i++)
            require(p[i * PAGE] == (i % 3 == 0 ? 0xa7 : pattern(i, 13)),
                    "child COW contents");
        _exit(0);
    }
    int status;
    require(waitpid(child, &status, 0) == child && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0, "COW child failed");
    check_bytes(p, size, 13);

    require(mprotect(p + PAGE, PAGE, PROT_READ) == 0, "partial mprotect");
    require(mprotect(p + PAGE, PAGE, PROT_READ | PROT_WRITE) == 0,
            "restore mprotect");
    require(madvise(p + PAGE, PAGE, MADV_DONTNEED) == 0, "partial discard");
    for (size_t i = 0; i < PAGE; i++)
        require(p[PAGE + i] == 0, "discarded page was not zero-filled");
    memset(p + PAGE, pattern(1, 13), PAGE);
    check_bytes(p, size, 13);
    require(munmap(p + 3 * PAGE, PAGE) == 0, "partial unmap");
    require(mmap(p + 3 * PAGE, PAGE, PROT_READ | PROT_WRITE,
                 MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0) == p + 3 * PAGE,
            "remap hole");
    for (size_t i = 0; i < PAGE; i++)
        require(p[3 * PAGE + i] == 0, "remapped page not zero");
    memset(p + 3 * PAGE, pattern(3, 13), PAGE);
    check_bytes(p, size, 13);
    require(munmap(p, size) == 0, "mTHP munmap");

    /* Use fresh mappings: COW and partial unmap above may already have split
     * their folios, so they cannot prove that an explicit physical split ran. */
    native_split_test(size_kb, 4);
    if (size_kb == 2048)
        native_split_test(2048, 64);
    printf("PASS mthp size_kb=%u physical-folio COW mprotect discard unmap reuse\n",
           size_kb);
}

static double now_sec(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

struct worker_args { size_t size; unsigned int id; double deadline; };
static atomic_ulong iterations;
static pthread_barrier_t barrier;

static void *worker(void *arg)
{
    struct worker_args *a = arg;
    pthread_barrier_wait(&barrier);
    unsigned long iteration = 0;
    do {
        unsigned char *p = aligned_region(a->size);
        madvise(p, a->size, MADV_NOHUGEPAGE);
        unsigned int salt = a->id * 13 + (unsigned int)iteration;
        fill_bytes(p, a->size, salt);
        check_bytes(p, a->size, salt);
        /* Keep allocations live across host reclaim/return and other workers. */
        usleep(5000);
        check_bytes(p, a->size, salt);
        require(munmap(p, a->size) == 0, "worker munmap");
        iteration++;
        atomic_fetch_add(&iterations, 1);
    } while (now_sec() < a->deadline);
    return NULL;
}

static void stress_test(unsigned int mib, unsigned int threads, unsigned int seconds)
{
    require(threads > 0 && threads <= 32 && mib >= threads && seconds > 0,
            "invalid stress arguments");
    pthread_t ids[32];
    struct worker_args args[32];
    require(pthread_barrier_init(&barrier, NULL, threads) == 0, "barrier init");
    double deadline = now_sec() + seconds;
    for (unsigned int i = 0; i < threads; i++) {
        args[i] = (struct worker_args){ (mib / threads) * MIB, i, deadline };
        require(pthread_create(&ids[i], NULL, worker, &args[i]) == 0, "pthread_create");
    }
    for (unsigned int i = 0; i < threads; i++)
        require(pthread_join(ids[i], NULL) == 0, "pthread_join");
    printf("PASS stress mib=%u threads=%u seconds=%u iterations=%lu\n",
           mib, threads, seconds, atomic_load(&iterations));
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc >= 2 && !strcmp(argv[1], "mthp")) {
        if (argc > 2) {
            for (int i = 2; i < argc; i++)
                mthp_test((unsigned int)strtoul(argv[i], NULL, 10));
        } else {
            unsigned int sizes[] = {16, 32, 64, 128, 256, 512, 1024, 2048};
            for (size_t i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++)
                mthp_test(sizes[i]);
        }
        select_thp(2048);
        return 0;
    }
    if (argc == 5 && !strcmp(argv[1], "stress")) {
        stress_test(strtoul(argv[2], NULL, 10), strtoul(argv[3], NULL, 10),
                    strtoul(argv[4], NULL, 10));
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "touch")) {
        size_t size = strtoul(argv[2], NULL, 10) * MIB;
        unsigned char *p = aligned_region(size);
        madvise(p, size, MADV_NOHUGEPAGE);
        fill_bytes(p, size, 31);
        check_bytes(p, size, 31);
        require(munmap(p, size) == 0, "touch munmap");
        printf("PASS touch mib=%zu\n", size / MIB);
        return 0;
    }
    fprintf(stderr, "usage: %s mthp [sizes_kb...] | stress MiB threads seconds | touch MiB\n", argv[0]);
    return 2;
}
