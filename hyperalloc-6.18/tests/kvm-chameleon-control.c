// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <linux/kvm.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include "../linux/mm/gup_test.h"

#ifndef MADV_COLLAPSE
#define MADV_COLLAPSE 25
#endif
#define PAGE 4096UL
#define HUGE (512 * PAGE)
#define RAM (16 * HUGE)
#define CPUS 4

static unsigned checks;
static int kvm_fd;
static size_t run_size;
static struct kvm_cpuid2 *cpuid;
static const char *phase = "setup";

static void check(bool good, const char *description)
{
    if (!good) {
        fprintf(stderr, "FAIL KVM_CHAMELEON phase=%s check=%u %s errno=%d (%s)\n",
                phase, checks + 1, description, errno, strerror(errno));
        exit(1);
    }
    checks++;
}

struct machine {
    int fd, cpu[CPUS];
    struct kvm_run *run[CPUS];
    unsigned char *ram;
    unsigned ncpus;
};

static struct kvm_chameleon_stats stats(struct machine *vm)
{
    struct kvm_chameleon_stats s = {.version = KVM_CHAMELEON_VERSION};
    check(!ioctl(vm->fd, KVM_CHAMELEON_STATS, &s), "read actual KVM counters");
    return s;
}

static void expect_errno(int ret, int expected, const char *description)
{
    int saved = errno;
    if (ret != -1 || saved != expected)
        fprintf(stderr, "errno mismatch ret=%d actual=%d expected=%d\n", ret, saved, expected);
    check(ret == -1 && saved == expected, description);
}

static struct machine machine_new(bool enable, bool cpus)
{
    struct machine vm = {.fd = ioctl(kvm_fd, KVM_CREATE_VM, 0)};
    check(vm.fd >= 0, "create a real KVM VM");
    if (enable) {
        struct kvm_enable_cap cap = {.cap = KVM_CAP_CHAMELEON_RECLAIM, .args = {1}};
        check(!ioctl(vm.fd, KVM_ENABLE_CAP, &cap), "enable private control capability");
    }
    void *raw = mmap(NULL, RAM + HUGE, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    check(raw != MAP_FAILED, "allocate anonymous test RAM");
    uintptr_t aligned = ((uintptr_t)raw + HUGE - 1) & ~(HUGE - 1);
    size_t prefix = aligned - (uintptr_t)raw;
    if (prefix)
        check(!munmap(raw, prefix), "trim RAM alignment prefix");
    size_t suffix = RAM + HUGE - prefix - RAM;
    if (suffix)
        check(!munmap((void *)(aligned + RAM), suffix), "trim RAM alignment suffix");
    vm.ram = (void *)aligned;
    check(!madvise(vm.ram, RAM, MADV_HUGEPAGE), "request actual host huge pages");
    memset(vm.ram, 0x5a, RAM);
    check(!madvise(vm.ram, RAM, MADV_COLLAPSE), "collapse anonymous RAM into real host THPs");
    struct kvm_userspace_memory_region region = {
        .slot = 0, .guest_phys_addr = 0, .memory_size = RAM,
        .userspace_addr = aligned,
    };
    check(!ioctl(vm.fd, KVM_SET_USER_MEMORY_REGION, &region), "register actual anonymous RAM memslot");
    if (!cpus)
        return vm;
    uint64_t *pml4 = (void *)(vm.ram + 0x10000);
    uint64_t *pdpt = (void *)(vm.ram + 0x11000);
    uint64_t *pd = (void *)(vm.ram + 0x12000);
    memset(pml4, 0, PAGE);
    memset(pdpt, 0, PAGE);
    memset(pd, 0, PAGE);
    pml4[0] = 0x11003;
    pdpt[0] = 0x12003;
    for (unsigned i = 0; i < RAM / HUGE; i++)
        pd[i] = (uint64_t)i * HUGE | 0x83;
    /* Read [RBX] into [RCX], then exit through the actual guest HLT. */
    const unsigned char read_code[] = {0x8a, 0x03, 0x88, 0x01, 0xf4};
    memcpy(vm.ram + PAGE, read_code, sizeof(read_code));
    for (unsigned i = 0; i < CPUS; i++) {
        vm.cpu[i] = ioctl(vm.fd, KVM_CREATE_VCPU, i);
        check(vm.cpu[i] >= 0, "create actual vCPU");
        check(!ioctl(vm.cpu[i], KVM_SET_CPUID2, cpuid), "install supported CPU features");
        vm.run[i] = mmap(NULL, run_size, PROT_READ | PROT_WRITE, MAP_SHARED, vm.cpu[i], 0);
        check(vm.run[i] != MAP_FAILED, "map vCPU run state");
        struct kvm_sregs sr;
        check(!ioctl(vm.cpu[i], KVM_GET_SREGS, &sr), "read initial vCPU control state");
        sr.cr3 = 0x10000;
        sr.cr4 = 1ULL << 5; /* PAE */
        sr.cr0 = (1ULL << 31) | (1ULL << 5) | 1; /* PG, NE, PE */
        sr.efer = (1ULL << 8) | (1ULL << 10); /* LME, LMA */
        sr.cs = (struct kvm_segment){.base = 0, .limit = ~0U, .selector = 8,
                                     .type = 11, .present = 1, .s = 1, .l = 1, .g = 1};
        sr.ds = (struct kvm_segment){.base = 0, .limit = ~0U, .selector = 16,
                                     .type = 3, .present = 1, .s = 1, .db = 1, .g = 1};
        sr.es = sr.fs = sr.gs = sr.ss = sr.ds;
        check(!ioctl(vm.cpu[i], KVM_SET_SREGS, &sr), "enter actual guest long mode");
        vm.ncpus++;
    }
    return vm;
}

static void machine_destroy(struct machine *vm)
{
    for (unsigned i = 0; i < vm->ncpus; i++) {
        munmap(vm->run[i], run_size);
        close(vm->cpu[i]);
    }
    close(vm->fd);
    munmap(vm->ram, RAM);
}

static int access_guest(struct machine *vm, unsigned cpu, uint64_t gpa)
{
    struct kvm_regs r = {.rip = PAGE, .rflags = 2, .rbx = gpa,
                         .rcx = 0x18000 + cpu * PAGE, .rsp = 0x1f000};
    vm->ram[r.rcx] = 0xcc;
    check(!ioctl(vm->cpu[cpu], KVM_SET_REGS, &r), "point guest at physical test range");
    int ret;
    do {
        ret = ioctl(vm->cpu[cpu], KVM_RUN, 0);
    } while (ret == -1 && errno == EINTR);
    return ret;
}

static void guest_reads(struct machine *vm, unsigned cpu, uint64_t gpa, unsigned char expected)
{
    int ret = access_guest(vm, cpu, gpa);
    if (ret || vm->run[cpu]->exit_reason != KVM_EXIT_HLT)
        fprintf(stderr, "vCPU read ret=%d exit=%u gpa=0x%" PRIx64 "\n",
                ret, vm->run[cpu]->exit_reason, gpa);
    check(!ret && vm->run[cpu]->exit_reason == KVM_EXIT_HLT, "actual guest instruction reads mapped target");
    check(vm->ram[0x18000 + cpu * PAGE] == expected, "actual guest read returns expected bytes");
}

static void resident(struct machine *vm, uint64_t gpa, size_t pages, bool expected)
{
    unsigned char *vec = malloc(pages);
    check(vec != NULL, "allocate residency observation");
    check(!mincore(vm->ram + gpa, pages * PAGE, vec), "observe real host backing without populating it");
    for (size_t i = 0; i < pages; i++)
        check(!!(vec[i] & 1) == expected, "every target page has expected physical residency");
    free(vec);
}

static struct kvm_chameleon_range range(uint64_t gpa, unsigned order, uint64_t cookie)
{
    return (struct kvm_chameleon_range){.gpa = gpa, .order = order,
                                       .nr_pages = 1U << order, .cookie = cookie};
}

static struct kvm_chameleon_batch batch(struct kvm_chameleon_range *ranges,
                                        unsigned nr, uint64_t transaction)
{
    return (struct kvm_chameleon_batch){.version = KVM_CHAMELEON_VERSION,
        .transaction = transaction, .ranges = (uintptr_t)ranges, .nr_ranges = nr};
}


static void clear_results(struct kvm_chameleon_range *r, unsigned nr)
{
    for (unsigned i = 0; i < nr; i++) {
        r[i].status = 0;
        r[i].state = 0;
    }
}

static void controls(void)
{
    phase = "capability and input validation";
    struct machine vm = machine_new(false, false);
    struct kvm_chameleon_range r[2] = {range(2 * HUGE, 0, 1)};
    struct kvm_chameleon_batch b = batch(r, 1, 0);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b), EOPNOTSUPP,
                 "new ioctl is disabled unless VM opts in");
    struct kvm_enable_cap cap = {.cap = KVM_CAP_CHAMELEON_RECLAIM, .args = {1}};
    cap.flags = 1;
    expect_errno(ioctl(vm.fd, KVM_ENABLE_CAP, &cap), EINVAL, "unknown capability flags rejected");
    cap.flags = 0;
	cap.args[1] = KVM_CHAMELEON_ENABLE_VFIO << 1;
    expect_errno(ioctl(vm.fd, KVM_ENABLE_CAP, &cap), EINVAL, "unknown capability arguments rejected");
    cap.args[1] = 0;
    check(!ioctl(vm.fd, KVM_ENABLE_CAP, &cap), "enable after existing anonymous RAM registration");
    struct kvm_chameleon_stats before = stats(&vm);
    for (unsigned which = 0; which < 11; which++) {
        r[0] = range(2 * HUGE, 0, 1);
        b = batch(r, 1, 0);
        switch (which) {
        case 0: b.version++; break;
        case 1: b.flags = 1; break;
        case 2: b.reserved = 1; break;
        case 3: b.nr_ranges = 0; break;
        case 4: b.nr_ranges = KVM_CHAMELEON_MAX_RANGES + 1; break;
        case 5: r[0].gpa++; break;
        case 6: r[0].nr_pages = 0; break;
        case 7: r[0].order = 4; break;
        case 8: r[0].flags = 1; break;
        case 9: r[0].state = KVM_CHAMELEON_RANGE_PENDING; break;
        case 10: r[1] = r[0]; r[1].cookie++; b.nr_ranges = 2; break;
        }
        expect_errno(ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b), EINVAL,
                     "malformed vector rejected before any EPT change");
    }
    r[0] = range(2 * HUGE, 0, 1);
    b = batch(r, 1, 123456);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_QUERY, &b), ESTALE, "unknown transaction rejected");
    struct kvm_chameleon_stats after = stats(&vm);
    check(after.begin_batches == before.begin_batches && !after.blocked_pages && !after.guard_pages,
          "invalid requests leave no ownership or invalidation state");
    check(after.host_available_pages > 0 && after.max_ranges == KVM_CHAMELEON_MAX_RANGES,
          "watermark input is real Host available memory with bounded vector ABI");
    int pin_fd = open("/sys/kernel/debug/gup_test", O_RDWR);
    check(pin_fd >= 0, "open actual Host long-term pin interface");
    struct pin_longterm_test pin = {.addr = (uintptr_t)(vm.ram + 2 * HUGE), .size = PAGE,
        .flags = PIN_LONGTERM_TEST_FLAG_USE_FAST | PIN_LONGTERM_TEST_FLAG_USE_WRITE};
    check(!ioctl(pin_fd, PIN_LONGTERM_TEST_START, &pin), "acquire actual Host FOLL_PIN reference");
    r[0] = range(2 * HUGE, 0, 1);
    b = batch(r, 1, 0);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b), EBUSY, "pinned Host backing is rejected");
    check(!stats(&vm).blocked_pages, "pin rejection does not publish range ownership");
    check(!ioctl(pin_fd, PIN_LONGTERM_TEST_STOP), "release actual Host pin");
    close(pin_fd);
    machine_destroy(&vm);
    vm = machine_new(false, true);
    guest_reads(&vm, 0, 2 * HUGE, 0x5a);
    expect_errno(ioctl(vm.fd, KVM_ENABLE_CAP, &cap), EBUSY,
                 "enable after first guest execution is rejected to exclude old async GUP");
    machine_destroy(&vm);
    puts("PASS KVM_CHAMELEON controls opt_in bounds stale no_side_effects");
}

static void retired_fault(struct machine *vm, uint64_t gpa, bool failed)
{
    int ret = access_guest(vm, 0, gpa);
    int saved = errno;
    check(ret == -1 && saved == EFAULT && vm->run[0]->exit_reason == KVM_EXIT_MEMORY_FAULT,
          "retired guest access exits explicitly instead of populating RAM");
    uint64_t flags = vm->run[0]->memory_fault.flags;
    check(flags & KVM_MEMORY_EXIT_FLAG_CHAMELEON, "retired fault is identified as private Chameleon state");
    check(!!(flags & KVM_MEMORY_EXIT_FLAG_CHAMELEON_ERROR) == failed,
          "uncertain backing failure remains distinguishable from successful discard");
}

/* A bridge device needs no physical RNIC. Real FOLL_PIN references exercise
 * the same Host refusal that protects against a failed VFIO DMA unmap. */
static void vfio_coordination(void)
{
    phase = "explicit VFIO coordination and retained pin validation";
    struct machine vm = machine_new(true, false);
    struct kvm_create_device dev = {.type = KVM_DEV_TYPE_VFIO};
    expect_errno(ioctl(vm.fd, KVM_CREATE_DEVICE, &dev), EOPNOTSUPP,
                 "default Chameleon still rejects uncoordinated VFIO bridge");
    struct kvm_enable_cap cap = {.cap = KVM_CAP_CHAMELEON_RECLAIM,
                                .args = {1, KVM_CHAMELEON_ENABLE_VFIO}};
    expect_errno(ioctl(vm.fd, KVM_ENABLE_CAP, &cap), EBUSY,
                 "an enabled VM cannot change its VFIO contract");
    machine_destroy(&vm);

    vm = machine_new(false, false);
    check(!ioctl(vm.fd, KVM_CREATE_DEVICE, &dev), "create actual KVM VFIO bridge");
    cap.args[1] = 0;
    expect_errno(ioctl(vm.fd, KVM_ENABLE_CAP, &cap), EOPNOTSUPP,
                 "existing VFIO bridge requires explicit coordination");
    cap.args[1] = KVM_CHAMELEON_ENABLE_VFIO;
    check(!ioctl(vm.fd, KVM_ENABLE_CAP, &cap), "enable coordinated VFIO with existing bridge");
    check(!ioctl(vm.fd, KVM_ENABLE_CAP, &cap), "repeated identical contract is idempotent");
    cap.args[1] = 0;
    expect_errno(ioctl(vm.fd, KVM_ENABLE_CAP, &cap), EBUSY,
                 "coordinated contract cannot be silently downgraded");
    close(dev.fd);
    machine_destroy(&vm);

    vm = machine_new(false, false);
    cap.args[1] = KVM_CHAMELEON_ENABLE_VFIO;
    check(!ioctl(vm.fd, KVM_ENABLE_CAP, &cap), "opt in before device creation");
    check(!ioctl(vm.fd, KVM_CREATE_DEVICE, &dev), "attach bridge after explicit opt in");
    struct kvm_chameleon_range r = range(2 * HUGE, 0, 9001);
    struct kvm_chameleon_batch b = batch(&r, 1, 0);
    int pin_fd = open("/sys/kernel/debug/gup_test", O_RDWR);
    check(pin_fd >= 0, "open actual Host pin interface for coordinated VM");
    struct pin_longterm_test pin = {.addr = (uintptr_t)(vm.ram + r.gpa), .size = PAGE,
        .flags = PIN_LONGTERM_TEST_FLAG_USE_FAST | PIN_LONGTERM_TEST_FLAG_USE_WRITE};
    check(!ioctl(pin_fd, PIN_LONGTERM_TEST_START, &pin), "pin source as a DMA mapping would");
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b), EBUSY,
                 "VFIO opt in never bypasses source DMA pin protection");
    check(!stats(&vm).blocked_pages, "failed unpin leaves no published transaction");
    check(!ioctl(pin_fd, PIN_LONGTERM_TEST_STOP), "DMA unmap releases source pin");
    check(!ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b), "BEGIN succeeds only after DMA unpin");
    check(!madvise(vm.ram + r.gpa, PAGE, MADV_NOHUGEPAGE), "keep restored backing at base-page granularity");
    check(!madvise(vm.ram + r.gpa, PAGE, MADV_DONTNEED), "discard coordinated source backing");
    resident(&vm, r.gpa, 1, false);
    r.state = KVM_CHAMELEON_RANGE_DISCARDED;
    check(!ioctl(vm.fd, KVM_CHAMELEON_REPORT, &b), "report real coordinated discard");
    clear_results(&r, 1);
    memset(vm.ram + r.gpa, 0xa5, PAGE);
    check(!ioctl(pin_fd, PIN_LONGTERM_TEST_START, &pin), "premature DMA remap pins new backing");
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), EBUSY,
                 "INSTALL rejects remap before Host accepts backing");
    check(stats(&vm).blocked_pages == 1, "premature pin preserves blocked ownership");
    check(!ioctl(pin_fd, PIN_LONGTERM_TEST_STOP), "undo premature DMA remap");
    check(!ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), "INSTALL accepts populated unpinned backing");
    check(!ioctl(pin_fd, PIN_LONGTERM_TEST_START, &pin), "DMA mapping can be restored after INSTALL");
    check(!stats(&vm).blocked_pages && vm.ram[r.gpa] == 0xa5,
          "accepted backing stays resident and valid after DMA remap");
    check(!ioctl(pin_fd, PIN_LONGTERM_TEST_STOP), "release restored DMA mapping");
    close(pin_fd);
    close(dev.fd);
    machine_destroy(&vm);
    puts("PASS KVM_CHAMELEON vfio_coordination explicit_opt_in pin_guard unpin_begin_install_remap");
}

static void round_trip(void)
{
    phase = "actual EPT batch, discard and reinstall";
    struct machine vm = machine_new(true, true);
    struct kvm_chameleon_range r[] = {
        range(2 * HUGE + PAGE, 0, 101),
        range(3 * HUGE + 16 * PAGE, 4, 102),
        range(4 * HUGE, 9, 103),
    };
    const unsigned nr = sizeof(r) / sizeof(r[0]);
    const uint64_t pages = 1 + 16 + 512;
    uint64_t neighbors[] = {2 * HUGE, 2 * HUGE + 2 * PAGE,
                           3 * HUGE + 15 * PAGE, 3 * HUGE + 32 * PAGE,
                           4 * HUGE - PAGE, 5 * HUGE};
    for (unsigned cpu = 0; cpu < CPUS; cpu++) {
        for (unsigned i = 0; i < nr; i++) {
            guest_reads(&vm, cpu, r[i].gpa, 0x5a);
            guest_reads(&vm, cpu, r[i].gpa + (r[i].nr_pages - 1) * PAGE, 0x5a);
        }
        for (unsigned i = 0; i < sizeof(neighbors) / sizeof(neighbors[0]); i++)
            guest_reads(&vm, cpu, neighbors[i], 0x5a);
    }
    struct kvm_chameleon_stats before = stats(&vm);
    struct kvm_chameleon_batch b = batch(r, nr, 0);
    check(!ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b) && b.transaction,
          "one vector withdraws EPT for all actual ranges");
    uint64_t token = b.transaction;
    struct kvm_chameleon_stats begun = stats(&vm);
    check(begun.begin_batches == before.begin_batches + 1 &&
          begun.begin_flushes == before.begin_flushes + 1 &&
          begun.remote_tlb_flush_requests == before.remote_tlb_flush_requests + 1,
          "exactly one real remote TLB flush request for the whole batch");
    check(begun.blocked_pages == pages && begun.guard_pages >= pages &&
          begun.pending_transaction == token, "range ownership persists across ioctl return");
    for (unsigned i = 0; i < nr; i++) {
        resident(&vm, r[i].gpa, r[i].nr_pages, true);
        check(!madvise(vm.ram + r[i].gpa, r[i].nr_pages * PAGE, MADV_NOHUGEPAGE),
              "prevent Host collapse from repopulating the exact retired hole");
        check(!madvise(vm.ram + r[i].gpa, r[i].nr_pages * PAGE, MADV_DONTNEED),
              "actually discard only the handed-off Host backing");
        resident(&vm, r[i].gpa, r[i].nr_pages, false);
        r[i].state = KVM_CHAMELEON_RANGE_DISCARDED;
        r[i].status = 0;
    }
    for (unsigned i = 0; i < sizeof(neighbors) / sizeof(neighbors[0]); i++)
        resident(&vm, neighbors[i], 1, true);
    struct kvm_chameleon_stats discarded = stats(&vm);
    check(discarded.remote_tlb_flush_requests == begun.remote_tlb_flush_requests,
          "partial host-THP discard causes no hidden second EPT flush");
    check(!ioctl(vm.fd, KVM_CHAMELEON_REPORT, &b), "report actual per-range discard results");
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_REPORT, &b), EALREADY,
                 "duplicate report cannot double-account retired memory");
    struct kvm_chameleon_stats reported = stats(&vm);
    check(reported.blocked_pages == pages && !reported.guard_pages &&
          !reported.pending_transaction && reported.retired_ranges == nr,
          "temporary THP guards end while exact retired holes remain protected");
    check(reported.remote_tlb_flush_requests == begun.remote_tlb_flush_requests,
          "report itself does not perform another remote flush");
    clear_results(r, nr);
    check(!ioctl(vm.fd, KVM_CHAMELEON_QUERY, &b), "query committed range states");
    for (unsigned i = 0; i < nr; i++) {
        check(r[i].state == KVM_CHAMELEON_RANGE_DISCARDED && !r[i].status,
              "query reports actual discarded state per range");
        retired_fault(&vm, r[i].gpa, false);
        resident(&vm, r[i].gpa, r[i].nr_pages, false);
    }
    for (unsigned cpu = 0; cpu < CPUS; cpu++)
        for (unsigned i = 0; i < sizeof(neighbors) / sizeof(neighbors[0]); i++)
            guest_reads(&vm, cpu, neighbors[i], 0x5a);
    for (unsigned i = 0; i < nr; i++)
        resident(&vm, r[i].gpa, r[i].nr_pages, false);
    clear_results(r, nr);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), EAGAIN,
                 "install refuses to silently populate missing backing");
    check(stats(&vm).blocked_pages == pages, "failed install retains ownership and accounting");
    for (unsigned i = 0; i < nr; i++) {
        memset(vm.ram + r[i].gpa, 0, r[i].nr_pages * PAGE);
        resident(&vm, r[i].gpa, r[i].nr_pages, true);
    }
    clear_results(r, nr);
    check(!ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), "install releases guards only after real population");
    for (unsigned i = 0; i < nr; i++)
        check(!madvise(vm.ram + r[i].gpa, r[i].nr_pages * PAGE, MADV_HUGEPAGE),
              "restore original hugepage policy only after installation");
    clear_results(r, nr);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), EALREADY,
                 "duplicate install cannot release or account the range twice");
    check(!stats(&vm).blocked_pages && stats(&vm).install_ranges == nr,
          "successful install restores all controlled capacity exactly once");
    for (unsigned cpu = 0; cpu < CPUS; cpu++)
        for (unsigned i = 0; i < nr; i++)
            guest_reads(&vm, cpu, r[i].gpa, 0);
    printf("EPT_BATCH token=%" PRIu64 " ranges=%u pages=%" PRIu64
           " vcpus=%u native_flush_requests_delta=%" PRIu64
           " discard_extra_flushes=%" PRIu64 " mincore_zero=1 neighbors_preserved=1\n",
           token, nr, pages, CPUS,
           (uint64_t)(begun.remote_tlb_flush_requests - before.remote_tlb_flush_requests),
           (uint64_t)(discarded.remote_tlb_flush_requests - begun.remote_tlb_flush_requests));
    machine_destroy(&vm);
    puts("PASS KVM_CHAMELEON actual_batch 4KiB_64KiB_2MiB retired_fault exact_backing install_retry");
}

static void uncertain_result(void)
{
    phase = "uncertain result and huge EPT leaf protection";
    struct machine vm = machine_new(true, true);
    struct kvm_chameleon_range r = range(6 * HUGE + PAGE, 0, 201);
    uint64_t neighbor = 6 * HUGE + 2 * PAGE;
    for (unsigned cpu = 0; cpu < CPUS; cpu++) {
        guest_reads(&vm, cpu, r.gpa, 0x5a);
        guest_reads(&vm, cpu, neighbor, 0x5a);
    }
    struct kvm_chameleon_batch b = batch(&r, 1, 0);
    check(!ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b), "begin explicit error-recovery transaction");
    r.state = KVM_CHAMELEON_RANGE_ERROR_UNKNOWN;
    r.status = -EIO;
    /* Deliberately retain the actual huge Host mapping. This is an explicit
     * failure outcome, not a claim that any backing was discarded. */
    check(!ioctl(vm.fd, KVM_CHAMELEON_REPORT, &b), "uncertain failure stays blocked");
    struct kvm_chameleon_stats before = stats(&vm);
    for (unsigned cpu = 0; cpu < CPUS; cpu++)
        guest_reads(&vm, cpu, neighbor, 0x5a);
    struct kvm_chameleon_stats after = stats(&vm);
    check(after.hugepage_downgrades > before.hugepage_downgrades,
          "neighbor fault cannot install huge EPT leaf across a protected hole");
    retired_fault(&vm, r.gpa, true);
    resident(&vm, r.gpa, 1, true);
    memset(vm.ram + r.gpa, 0, PAGE);
    clear_results(&r, 1);
    check(!ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), "explicit repopulation resolves uncertain outcome");
    guest_reads(&vm, 0, r.gpa, 0);
    check(!stats(&vm).blocked_pages, "uncertain transaction leaves no leaked reservation after recovery");
    machine_destroy(&vm);
    puts("PASS KVM_CHAMELEON uncertain_failure huge_EPT_hole actual_fault explicit_recovery");
}

static void partial_result(void)
{
    phase = "partial results and memslot ownership";
    struct machine vm = machine_new(true, true);
    struct kvm_chameleon_range r[2] = {range(2 * HUGE + PAGE, 0, 301),
                                        range(3 * HUGE + PAGE, 0, 302)};
    guest_reads(&vm, 0, r[0].gpa, 0x5a);
    guest_reads(&vm, 1, r[1].gpa, 0x5a);
    struct kvm_chameleon_batch b = batch(r, 2, 0);
    check(!ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b), "begin partial outcome batch");
    struct kvm_chameleon_stats before = stats(&vm);
    struct kvm_userspace_memory_region deletion = {.slot = 0};
    expect_errno(ioctl(vm.fd, KVM_SET_USER_MEMORY_REGION, &deletion), EBUSY,
                 "in-use RAM memslot cannot be deleted during ownership transfer");
    check(stats(&vm).remote_tlb_flush_requests == before.remote_tlb_flush_requests,
          "rejected memslot change does not invalidate the old slot first");
    uint64_t token = b.transaction;
    clear_results(r, 2);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), EBUSY,
                 "pending transaction cannot be installed before discard result");
    check(!madvise(vm.ram + r[0].gpa, PAGE, MADV_DONTNEED), "actually discard only first partial-result range");
    resident(&vm, r[0].gpa, 1, false);
    resident(&vm, r[1].gpa, 1, true);
    r[0].state = KVM_CHAMELEON_RANGE_DISCARDED;
    r[1].state = KVM_CHAMELEON_RANGE_NOT_ATTEMPTED;
    r[1].cookie++;
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_REPORT, &b), EINVAL,
                 "report with changed identity cannot alter any range");
    r[1].cookie--;
    check(stats(&vm).pending_transaction == token && stats(&vm).blocked_pages == 2,
          "bad partial report keeps complete pending ownership");
    check(!ioctl(vm.fd, KVM_CHAMELEON_REPORT, &b), "record actual partial completion honestly");
    check(stats(&vm).blocked_pages == 1 && stats(&vm).retired_ranges == 1,
          "only actually discarded range remains retired");
    guest_reads(&vm, 1, r[1].gpa, 0x5a);
    retired_fault(&vm, r[0].gpa, false);
    clear_results(r, 2);
    b = batch(r + 1, 1, token);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), EALREADY,
                 "not-attempted backing needs no fabricated install");
    memset(vm.ram + r[0].gpa, 0, PAGE);
    b = batch(r, 1, token);
    check(!ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b), "install subset completes remaining reservation");
    check(!stats(&vm).blocked_pages, "partial outcome eventually releases all controlled ownership");
    machine_destroy(&vm);
    puts("PASS KVM_CHAMELEON partial_results immutable_identity memslot_guard no_false_rollback");
}

static void scalable_retirement(void)
{
    enum { COUNT = 6144, BATCHED = 4096 };
    struct saved_batch { uint64_t id; unsigned first, count; };
    phase = "scalable persistent retirement indexes";
    struct machine vm = machine_new(true, true);
    struct kvm_chameleon_range *ranges = calloc(COUNT, sizeof(*ranges));
    struct saved_batch *batches = calloc(COUNT, sizeof(*batches));
    unsigned transactions = 0;
    const uint64_t base = 2 * HUGE;
    check(ranges && batches, "allocate scalable real-VM fixtures");
    check(base + COUNT * PAGE <= RAM, "scaled ranges fit registered RAM");
    check(!madvise(vm.ram + base, COUNT * PAGE, MADV_NOHUGEPAGE),
          "prevent Host collapse into independently retired base-page holes");
    for (unsigned i = 0; i < COUNT; i++)
        ranges[i] = range(base + i * PAGE, 0, 10000 + i);
    struct kvm_chameleon_stats before = stats(&vm);
    for (unsigned first = 0; first < COUNT;) {
        unsigned count = first < BATCHED ? KVM_CHAMELEON_MAX_RANGES : 1;
        struct kvm_chameleon_batch b = batch(ranges + first, count, 0);
        check(!ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &b),
              "retire additional ranges after crossing 4096 live guards");
        batches[transactions++] = (struct saved_batch){b.transaction, first, count};
        check(!madvise(vm.ram + ranges[first].gpa, count * PAGE, MADV_DONTNEED),
              "actually discard every scalable batch");
        for (unsigned j = first; j < first + count; j++) {
            ranges[j].state = KVM_CHAMELEON_RANGE_DISCARDED;
            ranges[j].status = 0;
        }
        check(!ioctl(vm.fd, KVM_CHAMELEON_REPORT, &b),
              "publish exact scalable persistent retirement");
        first += count;
    }
    struct kvm_chameleon_stats retired = stats(&vm);
    check(retired.blocked_pages == COUNT && retired.retired_ranges == COUNT &&
          !retired.pending_transaction && !retired.guard_pages,
          "6144 cold base ranges survive while transient PMD guards are released");
    check(retired.begin_batches == before.begin_batches + transactions &&
          retired.begin_flushes == before.begin_flushes + transactions,
          "scalable retirements preserve one actual flush per transaction");
    resident(&vm, base, COUNT, false);
    for (unsigned i = 0; i < CPUS; i++)
        retired_fault(&vm, base + (i * (COUNT - 1) / (CPUS - 1)) * PAGE, false);
    guest_reads(&vm, 0, RAM - PAGE, 0x5a);
    struct kvm_chameleon_range duplicate = range(base + (COUNT - 1) * PAGE, 0, 90000);
    struct kvm_chameleon_batch repeat = batch(&duplicate, 1, 0);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_BEGIN, &repeat), EBUSY,
                 "interval index rejects a duplicate retired HVA beyond old limit");
    check(stats(&vm).blocked_pages == COUNT, "duplicate does not change persistent ownership");
    for (unsigned i = transactions; i--;) {
        struct saved_batch *saved = &batches[i];
        struct kvm_chameleon_range *r = ranges + saved->first;
        clear_results(r, saved->count);
        struct kvm_chameleon_batch b = batch(r, saved->count, saved->id);
        check(!ioctl(vm.fd, KVM_CHAMELEON_QUERY, &b),
              "transaction index finds older transactions in reverse completion order");
        for (unsigned j = 0; j < saved->count; j++)
            check(r[j].state == KVM_CHAMELEON_RANGE_DISCARDED,
                  "every indexed identity still reports its real retired state");
        memset(vm.ram + r->gpa, 0x69, saved->count * PAGE);
        clear_results(r, saved->count);
        check(!ioctl(vm.fd, KVM_CHAMELEON_INSTALL, &b),
              "reverse-order INSTALL releases exact indexed ranges");
    }
    struct kvm_chameleon_stats after = stats(&vm);
    check(!after.blocked_pages && !after.retired_ranges && !after.guard_pages &&
          !after.pending_transaction && after.install_ranges == before.install_ranges + COUNT,
          "all 6144 cold ranges and guards return to zero after install");
    for (unsigned i = 0; i < CPUS; i++)
        guest_reads(&vm, i, base + (i * (COUNT - 1) / (CPUS - 1)) * PAGE, 0x69);
    /* Completed history is bounded to one transaction, not all past IDs. */
    struct saved_batch *stale = &batches[transactions - 1];
    clear_results(ranges + stale->first, stale->count);
    repeat = batch(ranges + stale->first, stale->count, stale->id);
    expect_errno(ioctl(vm.fd, KVM_CHAMELEON_QUERY, &repeat), ESTALE,
                 "completed older transaction storage is released");
    free(batches);
    free(ranges);
    machine_destroy(&vm);
    printf("PASS KVM_CHAMELEON scalable_retirement ranges=%u transactions=%u exact_holes reverse_install zero_leaks\n",
           COUNT, transactions);
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    check(sysconf(_SC_PAGESIZE) == PAGE, "x86 4KiB base pages");
    kvm_fd = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    check(kvm_fd >= 0, "open real KVM device");
    check(ioctl(kvm_fd, KVM_GET_API_VERSION, 0) == 12, "supported KVM API");
    check(ioctl(kvm_fd, KVM_CHECK_EXTENSION, KVM_CAP_CHAMELEON_RECLAIM) == 1,
          "actual modified Host advertises private Chameleon capability");
    run_size = ioctl(kvm_fd, KVM_GET_VCPU_MMAP_SIZE, 0);
    check(run_size >= sizeof(struct kvm_run), "vCPU run mapping size");
    unsigned capacity = 256;
    cpuid = calloc(1, sizeof(*cpuid) + capacity * sizeof(cpuid->entries[0]));
    check(cpuid != NULL, "allocate supported CPUID array");
    cpuid->nent = capacity;
    check(!ioctl(kvm_fd, KVM_GET_SUPPORTED_CPUID, cpuid), "obtain actual Host CPU capabilities");
    controls();
    vfio_coordination();
    round_trip();
    uncertain_result();
    partial_result();
    scalable_retirement();
    free(cpuid);
    close(kvm_fd);
    printf("PASS KVM_CHAMELEON checks=%u\n", checks);
    return 0;
}
