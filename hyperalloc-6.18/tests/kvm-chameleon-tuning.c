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
#include <unistd.h>

static unsigned checks;

static void check(bool good, const char *what)
{
    if (!good) {
        fprintf(stderr, "FAIL KVM_CHAMELEON_TUNING check=%u %s errno=%d (%s)\n",
                checks + 1, what, errno, strerror(errno));
        exit(1);
    }
    checks++;
}

static void expect_errno(int ret, int expected, const char *what)
{
    int saved = errno;
    if (ret != -1 || saved != expected)
        fprintf(stderr, "errno mismatch ret=%d actual=%d expected=%d\n", ret, saved, expected);
    check(ret == -1 && saved == expected, what);
}

static int new_vm(int kvm)
{
    int vm = ioctl(kvm, KVM_CREATE_VM, 0);
    struct kvm_enable_cap cap = {.cap = KVM_CAP_CHAMELEON_RECLAIM, .args = {1}};
    check(vm >= 0, "create temporary VM");
    check(!ioctl(vm, KVM_ENABLE_CAP, &cap), "enable Chameleon before any vCPU");
    return vm;
}

static struct kvm_chameleon_tuning get_tuning(int vm)
{
    struct kvm_chameleon_tuning tuning = {.version = KVM_CHAMELEON_VERSION};
    check(!ioctl(vm, KVM_CHAMELEON_TUNING, &tuning), "GET canonical VM tuning");
    check(tuning.version == KVM_CHAMELEON_VERSION && !tuning.flags &&
          !tuning.reserved && tuning.generation, "well-formed tuning snapshot");
    return tuning;
}

static void defaults(int vm)
{
    struct kvm_chameleon_tuning tuning = {
        .version = KVM_CHAMELEON_VERSION,
        .flags = KVM_CHAMELEON_TUNING_SET_ALL,
        .ept_mode = KVM_CHAMELEON_EPT_DEFERRED,
        .batch_pages = 512,
    };
    check(!ioctl(vm, KVM_CHAMELEON_TUNING, &tuning), "register unchanged QEMU defaults");
    check(tuning.generation == 1 && tuning.batch_pages == 512 &&
          !tuning.ept_mode && !tuning.watermark_bytes, "defaults round-trip");
}

static void path_for(char *path, size_t size, int vm, const char *name)
{
    int n = snprintf(path, size, "/sys/kernel/debug/kvm/%d-%d/chameleon/%s",
                     getpid(), vm, name);
    check(n >= 0 && (size_t)n < size, "format exact per-VM debugfs path");
}

static int open_control(int vm, const char *name)
{
    char path[256];
    path_for(path, sizeof(path), vm, name);
    int fd = open(path, O_RDWR);
    check(fd >= 0, "open per-VM debugfs control");
    return fd;
}

static void read_is(int fd, const char *expected)
{
    char text[64] = {0};
    check(lseek(fd, 0, SEEK_SET) == 0, "rewind debugfs read");
    ssize_t n = read(fd, text, sizeof(text) - 1);
    check(n >= 0, "read debugfs control");
    check(!strcmp(text, expected), "debugfs shows canonical value");
}

static void write_is(int fd, const char *value)
{
    check(write(fd, value, strlen(value)) == (ssize_t)strlen(value),
          "update control with echo-compatible write");
}

int main(void)
{
    int kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    check(kvm >= 0, "open KVM");
    int vm1 = new_vm(kvm), vm2 = new_vm(kvm);
    int mode = open_control(vm1, "ept_mode");
    int batch = open_control(vm1, "batch_pages");
    int water = open_control(vm1, "watermark_bytes");
    char value[32];
    struct kvm_chameleon_tuning tuning = {.version = KVM_CHAMELEON_VERSION};
    expect_errno(ioctl(vm1, KVM_CHAMELEON_TUNING, &tuning), ENOTCONN,
                 "unregistered old userspace cannot expose ineffective tuning");
    expect_errno(read(batch, value, sizeof(value)), ENOTCONN, "unregistered debugfs read rejected");
    expect_errno(write(batch, "256\n", 4), ENOTCONN, "unregistered debugfs write rejected");
    defaults(vm1);
    defaults(vm2);
    read_is(mode, "deferred\n");
    read_is(batch, "512\n");
    read_is(water, "0\n");

    write_is(mode, "immediate\n");
    write_is(batch, "256\n");
    write_is(water, "1048576\n");
    tuning = get_tuning(vm1);
    check(tuning.ept_mode == KVM_CHAMELEON_EPT_IMMEDIATE &&
          tuning.batch_pages == 256 && tuning.watermark_bytes == 1048576,
          "debugfs changes are observed by the QEMU ioctl");
    uint64_t generation = tuning.generation;
    tuning = get_tuning(vm2);
    check(!tuning.ept_mode && tuning.batch_pages == 512 && !tuning.watermark_bytes,
          "controls are isolated between VMs");

    /* Masked update models QMP: unspecified fields must not overwrite a
     * concurrent debugfs value with userspace's older snapshot. */
    tuning = (struct kvm_chameleon_tuning){
        .version = KVM_CHAMELEON_VERSION,
        .flags = KVM_CHAMELEON_TUNING_SET_BATCH,
        .batch_pages = 1024,
    };
    check(!ioctl(vm1, KVM_CHAMELEON_TUNING, &tuning), "masked QMP-style update");
    check(tuning.generation == generation + 1 &&
          tuning.ept_mode == KVM_CHAMELEON_EPT_IMMEDIATE &&
          tuning.watermark_bytes == 1048576, "masked update preserves other controls");
    read_is(batch, "1024\n");
    read_is(mode, "immediate\n");
    generation = tuning.generation;

    expect_errno(write(batch, "0\n", 2), EINVAL, "zero batch threshold rejected");
    expect_errno(write(batch, "-1\n", 3), EINVAL, "negative threshold rejected");
    expect_errno(write(mode, "2\n", 2), EINVAL, "invalid mode rejected");
    expect_errno(write(water, "garbage\n", 8), EINVAL, "malformed watermark rejected");
    expect_errno(write(water, "18446744073709551616\n", 21), ERANGE, "overflow rejected");
    tuning = (struct kvm_chameleon_tuning){
        .version = KVM_CHAMELEON_VERSION,
        .flags = KVM_CHAMELEON_TUNING_SET_ALL,
        .ept_mode = KVM_CHAMELEON_EPT_DEFERRED,
        .batch_pages = 0,
        .watermark_bytes = 0,
    };
    expect_errno(ioctl(vm1, KVM_CHAMELEON_TUNING, &tuning), EINVAL,
                 "multi-field invalid update rejected atomically");
    tuning = get_tuning(vm1);
    check(tuning.generation == generation && tuning.ept_mode == KVM_CHAMELEON_EPT_IMMEDIATE &&
          tuning.batch_pages == 1024 && tuning.watermark_bytes == 1048576,
          "all invalid updates left canonical state unchanged");
    tuning.version++;
    expect_errno(ioctl(vm1, KVM_CHAMELEON_TUNING, &tuning), EINVAL, "bad version rejected");
    tuning.version = KVM_CHAMELEON_VERSION;
    tuning.flags = 8;
    expect_errno(ioctl(vm1, KVM_CHAMELEON_TUNING, &tuning), EINVAL, "unknown update flag rejected");
    tuning.flags = 0;
    tuning.reserved = 1;
    expect_errno(ioctl(vm1, KVM_CHAMELEON_TUNING, &tuning), EINVAL, "reserved ABI bits rejected");

    write_is(mode, "0\n");
    read_is(mode, "deferred\n");
    write_is(mode, "1\n");
    read_is(mode, "immediate\n");
    write_is(water, "18446744073709551615\n");
    tuning = get_tuning(vm1);
    check(tuning.watermark_bytes == UINT64_MAX, "full u64 watermark preserved");

    /* An open debugfs FD holds a KVM reference. Closing the VM FD does not
     * invalidate it; the final debugfs close destroys the VM and directory. */
    char last_path[256];
    path_for(last_path, sizeof(last_path), vm1, "batch_pages");
    check(!close(vm1), "close VM FD while debugfs FDs remain open");
    read_is(batch, "1024\n");
    write_is(batch, "512\n");
    read_is(batch, "512\n");
    check(!close(mode) && !close(water) && !close(batch), "release last VM references");
    check(access(last_path, F_OK) == -1 && errno == ENOENT,
          "final debugfs FD release destroys per-VM directory");
    check(!close(vm2) && !close(kvm), "release second VM and KVM");
    printf("PASS KVM_CHAMELEON_TUNING checks=%u defaults=deferred,512,0 isolation=1 fd_lifetime=1\n", checks);
    return 0;
}
