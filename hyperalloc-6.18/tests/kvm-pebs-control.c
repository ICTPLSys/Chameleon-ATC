#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kvm.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

/* Private HyperAlloc ABI; this is not an upstream KVM capability number. */
#ifndef KVM_CAP_HYPERALLOC_PEBS_MEMINFO
#define KVM_CAP_HYPERALLOC_PEBS_MEMINFO 0x48410001
#endif
#define MSR_HYPERALLOC_PEBS_MEMINFO 0x4b564d10
#define MSR_PERF_CAPABILITIES 0x345
#define MSR_PEBS_ENABLE 0x3f1
#define MSR_PEBS_DATA_CFG 0x3f2
#define MSR_PERFEVTSEL0 0x186
#define MSR_FIXED_CTR_CTRL 0x38d
#define PERF_CAP_PEBS_BASELINE (1ULL << 14)
#define PERF_CAP_PEBS_FORMAT 0xf00ULL
#define EVENTSEL_ADAPTIVE (1ULL << 34)
#define FIXED_0_ADAPTIVE (1ULL << 32)

static unsigned checks;
static int kvm_fd;
static struct kvm_cpuid2 *supported_cpuid;
static uint64_t perf_caps;

__attribute__((format(printf, 1, 2), noreturn))
static void fail(const char *fmt, ...)
{
    va_list args;
    fprintf(stderr, "not ok %u - ", checks + 1);
    va_start(args, fmt);
    vfprintf(stderr, fmt, args);
    va_end(args);
    fputc('\n', stderr);
    exit(1);
}

static void pass(const char *name)
{
    printf("ok %u - %s\n", ++checks, name);
}

static void expect(bool condition, const char *name)
{
    if (!condition)
        fail("%s (errno=%d: %s)", name, errno, strerror(errno));
    pass(name);
}

static int vm_new(void)
{
    int fd = ioctl(kvm_fd, KVM_CREATE_VM, 0);
    if (fd < 0)
        fail("KVM_CREATE_VM errno=%d: %s", errno, strerror(errno));
    return fd;
}

static struct kvm_enable_cap meminfo_request(void)
{
    struct kvm_enable_cap cap = {
        .cap = KVM_CAP_HYPERALLOC_PEBS_MEMINFO,
        .args = {1, 0, 0, 0},
    };
    return cap;
}

static void enable_ok(int vm, const char *name)
{
    struct kvm_enable_cap cap = meminfo_request();
    expect(ioctl(vm, KVM_ENABLE_CAP, &cap) == 0, name);
}

static void enable_rejected(int vm, struct kvm_enable_cap *cap,
                            int error, const char *name)
{
    errno = 0;
    int ret = ioctl(vm, KVM_ENABLE_CAP, cap);
    int saved = errno;
    if (ret != -1 || saved != error)
        fail("%s: ret=%d errno=%d expected=-1/%d", name, ret, saved, error);
    pass(name);
}

static int msr_access(int fd, uint32_t index, uint64_t *value, bool write)
{
    struct kvm_msrs *msrs = calloc(1, sizeof(*msrs) + sizeof(msrs->entries[0]));
    if (!msrs)
        fail("allocate MSR request");
    msrs->nmsrs = 1;
    msrs->entries[0].index = index;
    if (write)
        msrs->entries[0].data = *value;
    int ret = ioctl(fd, write ? KVM_SET_MSRS : KVM_GET_MSRS, msrs);
    if (!write && ret == 1)
        *value = msrs->entries[0].data;
    free(msrs);
    return ret;
}

static void msr_read_eq(int fd, uint32_t index, uint64_t value, const char *name)
{
    uint64_t actual = UINT64_MAX;
    int ret = msr_access(fd, index, &actual, false);
    if (ret != 1 || actual != value)
        fail("%s: MSR=0x%x ret=%d value=0x%" PRIx64 " expected=0x%" PRIx64,
             name, index, ret, actual, value);
    pass(name);
}

static void msr_read_rejected(int fd, uint32_t index, const char *name)
{
    uint64_t value = UINT64_MAX;
    int ret = msr_access(fd, index, &value, false);
    if (ret != 0)
        fail("%s: MSR=0x%x GET_MSRS returned %d, expected short return 0", name, index, ret);
    pass(name);
}

static void msr_write_result(int fd, uint32_t index, uint64_t value,
                             bool allowed, const char *name)
{
    int ret = msr_access(fd, index, &value, true);
    if (ret != (allowed ? 1 : 0))
        fail("%s: MSR=0x%x value=0x%" PRIx64 " SET_MSRS returned %d expected=%d",
             name, index, value, ret, allowed ? 1 : 0);
    pass(name);
}

static void get_cpu_configuration(void)
{
    for (unsigned count = 64; count <= 1024; count *= 2) {
        size_t bytes = sizeof(*supported_cpuid) + count * sizeof(supported_cpuid->entries[0]);
        supported_cpuid = calloc(1, bytes);
        if (!supported_cpuid)
            fail("allocate CPUID request");
        supported_cpuid->nent = count;
        if (ioctl(kvm_fd, KVM_GET_SUPPORTED_CPUID, supported_cpuid) == 0)
            break;
        if (errno != E2BIG)
            fail("KVM_GET_SUPPORTED_CPUID errno=%d", errno);
        free(supported_cpuid);
        supported_cpuid = NULL;
    }
    expect(supported_cpuid != NULL, "supported CPUID obtained");
    bool pdcm = false, pmu = false;
    for (unsigned i = 0; i < supported_cpuid->nent; i++) {
        struct kvm_cpuid_entry2 *entry = &supported_cpuid->entries[i];
        if (entry->function == 1)
            pdcm = !!(entry->ecx & (1U << 15));
        if (entry->function == 0xa)
            pmu = (entry->eax & 0xff) >= 2 && ((entry->eax >> 8) & 0xff) != 0;
    }
    expect(pdcm && pmu, "guest CPUID exposes PDCM and architectural PMU");
    expect(msr_access(kvm_fd, MSR_PERF_CAPABILITIES, &perf_caps, false) == 1,
           "system KVM_GET_MSRS reads PERF_CAPABILITIES feature MSR");
    printf("PERF_CAPABILITIES feature=0x%" PRIx64 "\n", perf_caps);
    expect(!(perf_caps & PERF_CAP_PEBS_BASELINE), "PEBS_BASELINE remains masked in feature MSR");
    expect(((perf_caps & PERF_CAP_PEBS_FORMAT) >> 8) == 4,
           "MEMINFO-capable host exposes PEBS format 4");
}

static int vcpu_new(int vm)
{
    int fd = ioctl(vm, KVM_CREATE_VCPU, 0);
    if (fd < 0)
        fail("KVM_CREATE_VCPU errno=%d: %s", errno, strerror(errno));
    expect(ioctl(fd, KVM_SET_CPUID2, supported_cpuid) == 0, "supported guest CPUID installed");
    msr_write_result(fd, MSR_PERF_CAPABILITIES, perf_caps, true,
                     "supported PERF_CAPABILITIES restore accepted");
    return fd;
}

static void test_default_off(void)
{
    int vm = vm_new();
    int vcpu = vcpu_new(vm);
    msr_read_rejected(vcpu, MSR_HYPERALLOC_PEBS_MEMINFO, "private MSR absent without VM opt-in");
    msr_write_result(vcpu, MSR_HYPERALLOC_PEBS_MEMINFO, 1, false,
                     "private MSR writes rejected without VM opt-in");
    msr_read_eq(vcpu, MSR_PERF_CAPABILITIES, perf_caps, "default guest preserves masked capabilities");
    struct kvm_enable_cap cap = meminfo_request();
    enable_rejected(vm, &cap, EBUSY, "enabling after vCPU creation rejected");
    close(vcpu);
    close(vm);
}

static void test_invalid_args(void)
{
    int vm = vm_new();
    struct kvm_enable_cap cap = meminfo_request();
    cap.flags = 1;
    enable_rejected(vm, &cap, EINVAL, "nonzero capability flags rejected");
    cap = meminfo_request();
    cap.args[0] = 0;
    enable_rejected(vm, &cap, EINVAL, "args[0]=0 rejected");
    cap.args[0] = 2;
    enable_rejected(vm, &cap, EINVAL, "args[0]=2 rejected");
    cap.args[0] = UINT64_MAX;
    enable_rejected(vm, &cap, EINVAL, "args[0]=UINT64_MAX rejected");
    for (unsigned i = 1; i < 4; i++) {
        char name[80];
        cap = meminfo_request();
        cap.args[i] = 1;
        snprintf(name, sizeof(name), "nonzero args[%u] rejected", i);
        enable_rejected(vm, &cap, EINVAL, name);
    }
    close(vm);
}

static int set_filter(int vm)
{
    /* A valid empty denylist still installs a filter object. */
    struct kvm_pmu_event_filter filter = { .action = KVM_PMU_EVENT_DENY };
    return ioctl(vm, KVM_SET_PMU_EVENT_FILTER, &filter);
}

static void test_filter_exclusion(void)
{
    expect(ioctl(kvm_fd, KVM_CHECK_EXTENSION, KVM_CAP_PMU_EVENT_FILTER) > 0,
           "PMU event filtering available for exclusion test");
    int vm = vm_new();
    expect(set_filter(vm) == 0, "PMU filter accepted before opt-in");
    struct kvm_enable_cap cap = meminfo_request();
    enable_rejected(vm, &cap, EINVAL, "existing PMU filter blocks MEMINFO opt-in");
    close(vm);

    vm = vm_new();
    enable_ok(vm, "MEMINFO enabled before filter test");
    errno = 0;
    int ret = set_filter(vm);
    int saved = errno;
    if (ret != -1 || saved != EINVAL)
        fail("MEMINFO must block later PMU filter: ret=%d errno=%d", ret, saved);
    pass("MEMINFO opt-in blocks later PMU filter");
    close(vm);
}

static void test_pmu_disable_exclusion(void)
{
    expect(ioctl(kvm_fd, KVM_CHECK_EXTENSION, KVM_CAP_PMU_CAPABILITY) > 0,
           "PMU capability configuration available");
    struct kvm_enable_cap disable = { .cap = KVM_CAP_PMU_CAPABILITY,
                                     .args = {KVM_PMU_CAP_DISABLE, 0, 0, 0} };
    int vm = vm_new();
    expect(ioctl(vm, KVM_ENABLE_CAP, &disable) == 0, "PMU can be disabled before opt-in");
    struct kvm_enable_cap cap = meminfo_request();
    enable_rejected(vm, &cap, EINVAL, "disabled PMU blocks MEMINFO opt-in");
    close(vm);
    vm = vm_new();
    enable_ok(vm, "MEMINFO enabled before PMU-disable test");
    enable_rejected(vm, &disable, EINVAL, "MEMINFO opt-in blocks later PMU disable");
    close(vm);
}

static void test_optin_registers(void)
{
    int vm = vm_new();
    expect(ioctl(vm, KVM_CHECK_EXTENSION, KVM_CAP_HYPERALLOC_PEBS_MEMINFO) == 1,
           "private capability also advertised through VM fd");
    enable_ok(vm, "valid opt-in before vCPU succeeds");
    enable_ok(vm, "repeated opt-in before vCPU is idempotent");
    int vcpu = vcpu_new(vm);
    msr_read_eq(vcpu, MSR_HYPERALLOC_PEBS_MEMINFO, 1, "opted-in private MSR exposes ABI version 1");
    const uint64_t writes[] = {0, 1, UINT64_MAX};
    for (unsigned i = 0; i < sizeof(writes) / sizeof(writes[0]); i++) {
        msr_write_result(vcpu, MSR_HYPERALLOC_PEBS_MEMINFO, writes[i], false,
                         "private feature MSR is read-only, including userspace restore");
        msr_read_eq(vcpu, MSR_HYPERALLOC_PEBS_MEMINFO, 1,
                    "rejected private-MSR write did not change ABI value");
    }
    msr_read_eq(vcpu, MSR_PERF_CAPABILITIES, perf_caps, "opt-in does not advertise PEBS_BASELINE");
    msr_write_result(vcpu, MSR_PERF_CAPABILITIES, perf_caps | PERF_CAP_PEBS_BASELINE,
                     false, "userspace cannot restore PEBS_BASELINE into guest");
    msr_read_eq(vcpu, MSR_PERF_CAPABILITIES, perf_caps, "rejected baseline write preserves capabilities");
    /* KVM's advertised-MSR migration compatibility returns zero to userspace
     * for an unsupported MSR and accepts restoring zero as a no-op. These
     * ioctls do not execute guest RDMSR/WRMSR, which must still fault. */
    msr_read_eq(vcpu, MSR_PEBS_DATA_CFG, 0,
                "userspace DATA_CFG save returns compatibility zero");
    msr_write_result(vcpu, MSR_PEBS_DATA_CFG, 0, true,
                     "userspace DATA_CFG zero restore accepted as compatibility no-op");
    msr_read_eq(vcpu, MSR_PEBS_DATA_CFG, 0,
                "DATA_CFG zero restore leaves userspace-visible value zero");
    msr_read_eq(vcpu, MSR_HYPERALLOC_PEBS_MEMINFO, 1,
                "DATA_CFG zero restore preserves private MEMINFO ABI");
    const uint64_t configs[] = {1, 1ULL << 1, 1ULL << 2, 1ULL << 3,
                                (1ULL << 3) | (31ULL << 24), UINT64_MAX};
    const char *names[] = {"DATA_CFG MEMINFO restore rejected",
                          "DATA_CFG GP-register restore rejected", "DATA_CFG XMM restore rejected",
                          "DATA_CFG LBR restore rejected", "DATA_CFG LBR-count restore rejected",
                          "DATA_CFG all-bits restore rejected"};
    for (unsigned i = 0; i < sizeof(configs) / sizeof(configs[0]); i++)
        msr_write_result(vcpu, MSR_PEBS_DATA_CFG, configs[i], false, names[i]);

    msr_write_result(vcpu, MSR_PEBS_ENABLE, 1, true, "GP-counter PEBS enable remains available");
    msr_write_result(vcpu, MSR_PEBS_ENABLE, 1ULL << 32, false,
                     "fixed-counter PEBS enable rejected");
    msr_write_result(vcpu, MSR_PEBS_ENABLE, (1ULL << 32) | 1, false,
                     "mixed GP/fixed PEBS enable rejected atomically");
    msr_read_eq(vcpu, MSR_PEBS_ENABLE, 1, "rejected fixed enable preserves GP PEBS state");
    msr_write_result(vcpu, MSR_PEBS_ENABLE, 0, true, "GP PEBS state can be cleared");
    msr_write_result(vcpu, MSR_PERFEVTSEL0, 0x20d1, true, "paper raw event encoding restore accepted");
    msr_write_result(vcpu, MSR_PERFEVTSEL0, 0x20d1 | EVENTSEL_ADAPTIVE, false,
                     "GP adaptive PEBS event-select restore rejected");
    msr_read_eq(vcpu, MSR_PERFEVTSEL0, 0x20d1, "rejected adaptive event preserves event select");
    msr_write_result(vcpu, MSR_FIXED_CTR_CTRL, 0, true, "normal fixed-counter control restore accepted");
    msr_write_result(vcpu, MSR_FIXED_CTR_CTRL, FIXED_0_ADAPTIVE, false,
                     "fixed adaptive PEBS control restore rejected");
    msr_read_eq(vcpu, MSR_FIXED_CTR_CTRL, 0, "rejected adaptive fixed control preserves state");
    struct kvm_enable_cap cap = meminfo_request();
    enable_rejected(vm, &cap, EBUSY, "opt-in repeated after vCPU creation rejected");
    close(vcpu);
    close(vm);
}

int main(int argc, char **argv)
{
    bool unavailable = argc == 2 && !strcmp(argv[1], "--expect-unavailable");
    if (argc > 1 && !unavailable) {
        fprintf(stderr, "usage: %s [--expect-unavailable]\n", argv[0]);
        return 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    kvm_fd = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    if (kvm_fd < 0) {
        fprintf(stderr, "SKIP KVM_PEBS_CONTROL /dev/kvm unavailable errno=%d\n", errno);
        return unavailable ? 1 : 4;
    }
    expect(ioctl(kvm_fd, KVM_GET_API_VERSION, 0) == KVM_API_VERSION, "KVM API version is supported");
    int supported = ioctl(kvm_fd, KVM_CHECK_EXTENSION, KVM_CAP_HYPERALLOC_PEBS_MEMINFO);
    printf("CAPABILITY id=0x%x supported=%d\n", KVM_CAP_HYPERALLOC_PEBS_MEMINFO, supported);
    if (unavailable) {
        expect(supported == 0, "private capability reports unavailable");
        int vm = vm_new();
        struct kvm_enable_cap cap = meminfo_request();
        errno = 0;
        int ret = ioctl(vm, KVM_ENABLE_CAP, &cap);
        int saved = errno;
        if (ret != -1 || (saved != EINVAL && saved != EOPNOTSUPP))
            fail("unavailable capability enable ret=%d errno=%d, expected EINVAL or EOPNOTSUPP", ret, saved);
        pass("unavailable private capability cannot be enabled");
        printf("ENABLE_REJECT errno=%d path=%s\n", saved,
               saved == EOPNOTSUPP ? "hardware_gated_extension" : "unknown_capability");
        close(vm);
        close(kvm_fd);
        printf("PASS KVM_PEBS_CONTROL mode=unavailable checks=%u supported=0 control_execution=negative_only\n", checks);
        return 0;
    }
    if (supported == 0) {
        puts("SKIP KVM_PEBS_CONTROL required_private_capability_unavailable; supported-host controls were not executed");
        close(kvm_fd);
        return 4;
    }
    expect(supported == 1, "private MEMINFO capability reports ABI support");
    get_cpu_configuration();
    test_default_off();
    test_invalid_args();
    test_filter_exclusion();
    test_pmu_disable_exclusion();
    test_optin_registers();
    free(supported_cpuid);
    close(kvm_fd);
    printf("PASS KVM_PEBS_CONTROL mode=supported checks=%u no_KVM_RUN=1\n", checks);
    return 0;
}
