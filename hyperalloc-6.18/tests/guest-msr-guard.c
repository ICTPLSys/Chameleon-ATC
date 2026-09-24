#define _GNU_SOURCE
#include <cpuid.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MSR_PRIVATE_MEMINFO 0x4b564d10
#define MSR_PERF_CAPABILITIES 0x345
#define MSR_PEBS_ENABLE 0x3f1
#define MSR_PEBS_DATA_CFG 0x3f2
#define MSR_PERFEVTSEL0 0x186
#define MSR_FIXED_CTR_CTRL 0x38d

static unsigned checks;

static void failed(const char *name, uint32_t msr, ssize_t result, int error)
{
    fprintf(stderr, "FAIL GUEST_MSR_GUARD check=%s msr=0x%x result=%zd errno=%d (%s)\n",
            name, msr, result, error, strerror(error));
    exit(1);
}

static void passed(const char *name)
{
    printf("ok %u - %s\n", ++checks, name);
}

static uint64_t read_value(int fd, uint32_t msr, const char *name)
{
    uint64_t value;
    errno = 0;
    ssize_t result = pread(fd, &value, sizeof(value), msr);
    if (result != sizeof(value))
        failed(name, msr, result, errno);
    return value;
}

static void read_equal(int fd, uint32_t msr, uint64_t expected, const char *name)
{
    uint64_t value = read_value(fd, msr, name);
    if (value != expected) {
        fprintf(stderr, "VALUE expected=0x%" PRIx64 " actual=0x%" PRIx64 "\n",
                expected, value);
        failed(name, msr, sizeof(value), 0);
    }
    passed(name);
}

static void read_faults(int fd, uint32_t msr, const char *name)
{
    uint64_t value = UINT64_MAX;
    errno = 0;
    ssize_t result = pread(fd, &value, sizeof(value), msr);
    int error = errno;
    if (result != -1 || error != EIO)
        failed(name, msr, result, error);
    passed(name);
}

static void write_faults(int fd, uint32_t msr, uint64_t value, const char *name)
{
    errno = 0;
    ssize_t result = pwrite(fd, &value, sizeof(value), msr);
    int error = errno;
    /* Stop immediately if any forbidden write succeeds; do not issue a
     * follow-up write to repair a guest whose isolation failed this test. */
    if (result != -1 || error != EIO)
        failed(name, msr, result, error);
    passed(name);
}

int main(int argc, char **argv)
{
    (void)argv;
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc != 1) {
        fputs("usage: guest-msr-guard (inside the opted-in disposable guest only)\n", stderr);
        return 2;
    }
    unsigned a, b, c, d;
    __cpuid(1, a, b, c, d);
    if (!(c & (1U << 31))) {
        fputs("FAIL GUEST_MSR_GUARD requires_hypervisor_cpuid no_MSR_access_performed\n", stderr);
        return 1;
    }
    cpu_set_t allowed;
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) {
        perror("sched_getaffinity");
        return 1;
    }
    int cpu;
    for (cpu = 0; cpu < CPU_SETSIZE && !CPU_ISSET(cpu, &allowed); cpu++)
        ;
    if (cpu == CPU_SETSIZE)
        return 1;
    CPU_ZERO(&allowed);
    CPU_SET(cpu, &allowed);
    if (sched_setaffinity(0, sizeof(allowed), &allowed)) {
        perror("sched_setaffinity");
        return 1;
    }
    char path[80];
    snprintf(path, sizeof(path), "/dev/cpu/%d/msr", cpu);
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        perror(path);
        return 1;
    }
    /* No writes until the actual guest-only ABI and masked capability state
     * are verified via RDMSR. A bare-metal host fails before opening /dev/msr. */
    read_equal(fd, MSR_PRIVATE_MEMINFO, 1, "guest private MEMINFO ABI is 1");
    uint64_t caps = read_value(fd, MSR_PERF_CAPABILITIES, "guest capability read");
    if ((caps & 0xf00) != 0x400 || (caps & (1ULL << 14)))
        failed("requires format 4 with PEBS_BASELINE masked", MSR_PERF_CAPABILITIES, 8, 0);
    passed("guest PEBS format 4 with PEBS_BASELINE masked");
    printf("GUEST_MSR_GUARD cpu=%d device=%s mechanism=guest_rdmsr_safe_wrmsr_safe expected_fault=GP0_EIO\n",
           cpu, path);
    read_faults(fd, MSR_PEBS_DATA_CFG, "guest RDMSR DATA_CFG faults");
    const uint64_t configs[] = {0, 1, 1ULL << 1, 1ULL << 2, 1ULL << 3,
                                (1ULL << 3) | (31ULL << 24), UINT64_MAX};
    const char *names[] = {"guest WRMSR DATA_CFG zero faults", "guest WRMSR DATA_CFG MEMINFO faults",
                          "guest WRMSR DATA_CFG GP faults", "guest WRMSR DATA_CFG XMM faults",
                          "guest WRMSR DATA_CFG LBR faults", "guest WRMSR DATA_CFG LBR count faults",
                          "guest WRMSR DATA_CFG all bits faults"};
    for (unsigned i = 0; i < sizeof(configs) / sizeof(configs[0]); i++)
        write_faults(fd, MSR_PEBS_DATA_CFG, configs[i], names[i]);
    read_faults(fd, MSR_PEBS_DATA_CFG, "guest DATA_CFG read still faults after rejected writes");
    const uint64_t private_writes[] = {0, 1, UINT64_MAX};
    for (unsigned i = 0; i < sizeof(private_writes) / sizeof(private_writes[0]); i++)
        write_faults(fd, MSR_PRIVATE_MEMINFO, private_writes[i], "guest private ABI MSR write faults");
    read_equal(fd, MSR_PRIVATE_MEMINFO, 1, "rejected guest writes preserve private ABI");
    uint64_t previous = read_value(fd, MSR_PEBS_ENABLE, "read PEBS_ENABLE before rejection");
    write_faults(fd, MSR_PEBS_ENABLE, previous | (1ULL << 32), "guest fixed-counter PEBS enable faults");
    read_equal(fd, MSR_PEBS_ENABLE, previous, "guest fixed PEBS rejection preserves enable state");
    previous = read_value(fd, MSR_PERFEVTSEL0, "read event selector before rejection");
    write_faults(fd, MSR_PERFEVTSEL0, previous | (1ULL << 34), "guest GP adaptive event selector faults");
    read_equal(fd, MSR_PERFEVTSEL0, previous, "guest GP adaptive rejection preserves event selector");
    previous = read_value(fd, MSR_FIXED_CTR_CTRL, "read fixed control before rejection");
    write_faults(fd, MSR_FIXED_CTR_CTRL, previous | (1ULL << 32), "guest fixed adaptive control faults");
    read_equal(fd, MSR_FIXED_CTR_CTRL, previous, "guest fixed adaptive rejection preserves control");
    close(fd);
    printf("PASS GUEST_MSR_GUARD checks=%u actual_guest_instructions=1 unexpected_write_success=0\n", checks);
    return 0;
}
