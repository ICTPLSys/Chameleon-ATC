/* SPDX-License-Identifier: GPL-2.0-or-later
 * Private Chameleon control transport. Included by virtio-llfree-balloon.c
 * after RAM translation helpers, sharing that device's discard manager.
 */
#include "qemu/interval-tree.h"

#define CH_MAX LL_CHAMELEON_MAX_RANGES

typedef struct ChRange {
    uint64_t token, gpa, transaction, batch;
    uint32_t pages, state, status;
    uint16_t order, flags;
    bool charged, no_huge, dma_unmapped, host_installed;
    IntervalTreeNode live;
    QTAILQ_ENTRY(ChRange) ready_link;
} ChRange;

typedef struct ChEvent {
    struct ll_chameleon_header header;
    struct ll_chameleon_range ranges[CH_MAX];
    QTAILQ_ENTRY(ChEvent) next;
} ChEvent;

struct LLChameleon {
    VirtIOLLFreeBalloon *dev;
    VirtQueue *ctrl, *events;
    QEMUTimer *timer;
    QemuMutex lock;
    Error *migration_blocker;
    bool broken;
    GHashTable *ranges;
    IntervalTreeRoot live, forgotten;
    uint64_t record_limit, forgotten_intervals, forgotten_tokens, ready_pages;
    QTAILQ_HEAD(, ChRange) ready;
    uint64_t lease, next_lease, last_epoch, lease_initial_hard;
    uint64_t batch_lease;
    uint64_t free_reclaim_ops, free_return_ops, free_reclaimed_bytes, free_returned_bytes;
    uint64_t policy_denials, gate_cancellations, auto_skips;
    bool reclaim_allowed, ept_immediate, batch_immediate;
    bool host_tuning, tuning_failed;
    QTAILQ_HEAD(, ChEvent) pending;
    uint64_t session, last_request, next_batch, current_batch;
    uint64_t pool_pages, charged_pages, retired_pages, error_pages;
    uint64_t batch_triggers, watermark_triggers, finalize_acks;
    uint64_t discarded_pages, installed_pages, install_failures;
    ChRange *batch[CH_MAX];
    unsigned nr_batch;
};

QEMU_BUILD_BUG_ON(sizeof(struct ll_chameleon_header) != 48);
QEMU_BUILD_BUG_ON(sizeof(struct ll_chameleon_range) != 32);
QEMU_BUILD_BUG_ON(sizeof(struct ll_chameleon_policy) != 80);
QEMU_BUILD_BUG_ON(sizeof(struct kvm_chameleon_batch) != 32);
QEMU_BUILD_BUG_ON(sizeof(struct kvm_chameleon_range) != 32);
QEMU_BUILD_BUG_ON(sizeof(struct kvm_chameleon_tuning) != 40);

static bool ch_live(const ChRange *r)
{
    return r->state != LL_CH_STATE_INSTALLED && r->state != LL_CH_STATE_CANCELED;
}

/* Both the GPA index and the ready queue contain only live records. Terminal
 * records remain in the token hash for QUERY/replay until explicit FORGET. */
static void ch_set_state(LLChameleon *ch, ChRange *r, uint32_t state)
{
    bool was_live = ch_live(r);
    if (r->state == LL_CH_STATE_READY) {
        QTAILQ_REMOVE(&ch->ready, r, ready_link);
        g_assert(ch->ready_pages >= r->pages);
        ch->ready_pages -= r->pages;
    }
    r->state = state;
    if (state == LL_CH_STATE_READY) {
        QTAILQ_INSERT_TAIL(&ch->ready, r, ready_link);
        ch->ready_pages += r->pages;
    }
    if (was_live && !ch_live(r)) {
        interval_tree_remove(&r->live, &ch->live);
    }
}

static bool ch_intersects(uint64_t a, uint64_t bytes, uint64_t b, uint64_t other)
{
    return a < b + other && b < a + bytes;
}

static bool ch_overlap_locked(LLChameleon *ch, hwaddr gpa, uint64_t bytes,
                              bool discarded_only)
{
    IntervalTreeNode *node;
    if (!bytes || gpa + bytes < gpa) {
        return false;
    }
    for (node = interval_tree_iter_first(&ch->live, gpa, gpa + bytes - 1);
         node; node = interval_tree_iter_next(node, gpa, gpa + bytes - 1)) {
        ChRange *r = container_of(node, ChRange, live);
        bool relevant = discarded_only ?
            (r->state == LL_CH_STATE_RETIRED || r->state == LL_CH_STATE_ERROR ||
             r->state == LL_CH_STATE_BLOCKED) : ch_live(r);
        if (relevant && ch_intersects(gpa, bytes, r->gpa, (uint64_t)r->pages * LL_PAGE_SIZE)) {
            return true;
        }
    }
    return false;
}

static bool ch_overlap(const VirtIOLLFreeBalloon *dev, hwaddr gpa, uint64_t bytes,
                       bool discarded_only)
{
    LLChameleon *ch = dev->chameleon;
    bool found;
    if (!ch) {
        return false;
    }
    qemu_mutex_lock(&ch->lock);
    found = ch_overlap_locked(ch, gpa, bytes, discarded_only);
    qemu_mutex_unlock(&ch->lock);
    return found;
}

/* Caller holds ch->lock. This only translates; mincore never faults memory in. */
static int ch_ram(ChRange *r, MemoryRegionSection *section, RAMBlock **rb,
                  ram_addr_t *offset, void **host)
{
    uint64_t bytes = (uint64_t)r->pages * LL_PAGE_SIZE;
    *section = memory_region_find(get_system_memory(), r->gpa, bytes);
    if (!section->mr) {
        return -EINVAL;
    }
    if (!memory_region_is_ram(section->mr) || memory_region_is_rom(section->mr) ||
        memory_region_is_romd(section->mr) || int128_get64(section->size) != bytes) {
        goto unsupported;
    }
    *host = memory_region_get_ram_ptr(section->mr) + section->offset_within_region;
    *rb = qemu_ram_block_from_host(*host, false, offset);
    if (!*rb || memory_region_get_fd(section->mr) != -1 || qemu_ram_is_shared(*rb) ||
        qemu_ram_pagesize(*rb) != LL_PAGE_SIZE ||
        !QEMU_PTR_IS_ALIGNED(*host, LL_PAGE_SIZE)) {
        goto unsupported;
    }
    return 0;
unsupported:
    memory_region_unref(section->mr);
    section->mr = NULL;
    return -EOPNOTSUPP;
}

static int ch_resident(ChRange *r, uint64_t *resident)
{
    MemoryRegionSection s;
    RAMBlock *rb;
    ram_addr_t offset;
    void *host;
    unsigned char vector[512];
    int ret = ch_ram(r, &s, &rb, &offset, &host);

    *resident = 0;
    if (ret) {
        return ret;
    }
    if (mincore(host, (size_t)r->pages * LL_PAGE_SIZE, vector)) {
        ret = -errno;
    } else {
        for (unsigned i = 0; i < r->pages; i++) {
            *resident += !!(vector[i] & 1);
        }
    }
    memory_region_unref(s.mr);
    return ret;
}

static void ch_wire(const ChRange *r, struct ll_chameleon_range *wire)
{
    *wire = (struct ll_chameleon_range) {
        .token = cpu_to_le64(r->token), .gpa = cpu_to_le64(r->gpa),
        .nr_pages = cpu_to_le32(r->pages), .order = cpu_to_le16(r->order),
        .flags = cpu_to_le16(r->flags), .status = cpu_to_le32(r->status),
        .state = cpu_to_le32(r->state),
    };
}

static bool ch_identity(const ChRange *r, const struct ll_chameleon_range *wire)
{
    return r->token == le64_to_cpu(wire->token) && r->gpa == le64_to_cpu(wire->gpa) &&
           r->pages == le32_to_cpu(wire->nr_pages) && r->order == le16_to_cpu(wire->order) &&
           r->flags == le16_to_cpu(wire->flags);
}

static ChRange *ch_find(LLChameleon *ch, uint64_t token)
{
    return g_hash_table_lookup(ch->ranges, &token);
}

/* Exact forgotten-token intervals preserve late registration of an older,
 * still-live token. Contiguous policy lifetimes coalesce into one interval. */
static bool ch_was_forgotten(LLChameleon *ch, uint64_t token)
{
    return interval_tree_iter_first(&ch->forgotten, token, token) != NULL;
}

static int ch_remember_forgotten(LLChameleon *ch, uint64_t token)
{
    IntervalTreeNode *previous, *next, *item;

    if (ch_was_forgotten(ch, token)) {
        return -EALREADY;
    }
    previous = token ? interval_tree_iter_first(&ch->forgotten, token - 1, token - 1) : NULL;
    next = token < UINT64_MAX ? interval_tree_iter_first(&ch->forgotten, token + 1, token + 1) : NULL;
    /* A new interval replaces the terminal record's metadata budget unit.
     * Commit replay protection before deleting that record. Coalescing keeps
     * sequential lifetimes constant-space; sparse histories share the same
     * RAM-derived metadata budget as records, independently of Shadow pages. */
    if (previous) {
        interval_tree_remove(previous, &ch->forgotten);
        previous->last = next ? next->last : token;
        if (next) {
            interval_tree_remove(next, &ch->forgotten);
            g_free(next);
            ch->forgotten_intervals--;
        }
        interval_tree_insert(previous, &ch->forgotten);
    } else if (next) {
        interval_tree_remove(next, &ch->forgotten);
        next->start = token;
        interval_tree_insert(next, &ch->forgotten);
    } else {
        item = g_try_new0(IntervalTreeNode, 1);
        if (!item) {
            return -ENOMEM;
        }
        item->start = item->last = token;
        interval_tree_insert(item, &ch->forgotten);
        ch->forgotten_intervals++;
    }
    ch->forgotten_tokens++;
    return 0;
}

static void ch_clear_forgotten(LLChameleon *ch)
{
    IntervalTreeNode *node;
    while ((node = interval_tree_iter_first(&ch->forgotten, 0, UINT64_MAX))) {
        interval_tree_remove(node, &ch->forgotten);
        g_free(node);
    }
    ch->forgotten_intervals = 0;
}

static void ch_uncharge(LLChameleon *ch, ChRange *r)
{
    if (r->charged) {
        g_assert(ch->charged_pages >= r->pages);
        ch->charged_pages -= r->pages;
        r->charged = false;
    }
}

static void ch_send_events(LLChameleon *ch)
{
    VirtIODevice *vdev = VIRTIO_DEVICE(ch->dev);
    while (!QTAILQ_EMPTY(&ch->pending) && virtio_queue_ready(ch->events)) {
        ChEvent *event = QTAILQ_FIRST(&ch->pending);
        unsigned nr = le32_to_cpu(event->header.nr_ranges);
        size_t length = sizeof(event->header) + nr * sizeof(event->ranges[0]);
        VirtQueueElement *elem = virtqueue_pop(ch->events, sizeof(*elem));
        if (!elem) {
            break;
        }
        if (elem->out_num || iov_size(elem->in_sg, elem->in_num) < length) {
            virtio_error(vdev, "chameleon: event buffer is too short or has output segments");
            virtqueue_push(ch->events, elem, 0);
            g_free(elem);
            return;
        }
        iov_from_buf(elem->in_sg, elem->in_num, 0, &event->header, sizeof(event->header));
        iov_from_buf(elem->in_sg, elem->in_num, sizeof(event->header), event->ranges,
                     nr * sizeof(event->ranges[0]));
        virtqueue_push(ch->events, elem, length);
        virtio_notify(vdev, ch->events);
        g_free(elem);
        QTAILQ_REMOVE(&ch->pending, event, next);
        g_free(event);
    }
}

static void ch_event(LLChameleon *ch, unsigned op, uint64_t batch,
                     ChRange **ranges, unsigned nr)
{
    ChEvent *event = g_new0(ChEvent, 1);
    event->header = (struct ll_chameleon_header) {
        .version = cpu_to_le16(LL_CHAMELEON_VERSION), .op = cpu_to_le16(op),
        .flags = cpu_to_le32(LL_CH_MSG_EVENT), .session = cpu_to_le64(ch->session),
        .batch_id = cpu_to_le64(batch), .nr_ranges = cpu_to_le32(nr),
    };
    for (unsigned i = 0; i < nr; i++) {
        ch_wire(ranges[i], &event->ranges[i]);
    }
    QTAILQ_INSERT_TAIL(&ch->pending, event, next);
    ch_send_events(ch);
}

static int ch_host_stats(struct kvm_chameleon_stats *stats)
{
    *stats = (struct kvm_chameleon_stats){ .version = KVM_CHAMELEON_VERSION };
    return kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_STATS, stats);
}

static void ch_apply_tuning(LLChameleon *ch,
                            const struct kvm_chameleon_tuning *tuning)
{
    ch->ept_immediate = tuning->ept_mode == KVM_CHAMELEON_EPT_IMMEDIATE;
    ch->dev->chameleon_batch_pages = tuning->batch_pages;
    ch->dev->chameleon_watermark_bytes = tuning->watermark_bytes;
}

/* KVM owns the requested configuration when the optional bridge is present.
 * Read it at batch decisions and on the existing 100ms timer, even when idle.
 * An in-flight FINALIZE request uses batch_immediate, never this live value. */
static int ch_sync_tuning(LLChameleon *ch)
{
    struct kvm_chameleon_tuning tuning = {
        .version = KVM_CHAMELEON_VERSION,
    };
    int ret;

    if (!ch->host_tuning) {
        return 0;
    }
    ret = kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_TUNING, &tuning);
    if (ret) {
        if (!ch->tuning_failed) {
            error_report("Chameleon cannot read Host tuning: %s; new batches paused",
                         strerror(-ret));
        }
        ch->tuning_failed = true;
        return ret;
    }
    ch->tuning_failed = false;
    ch_apply_tuning(ch, &tuning);
    return 0;
}

static uint64_t ch_ready_pages(LLChameleon *ch)
{
    return ch->ready_pages;
}

static void ch_maybe_finalize(LLChameleon *ch)
{
    struct kvm_chameleon_stats stats;
    uint64_t ready = ch_ready_pages(ch);
    bool threshold, watermark;

    if (ch_sync_tuning(ch)) {
        return;
    }
    /* An undelivered result must drain before issuing another batch. This
     * bounds queued event storage even if a Guest stops supplying buffers. */
    if (ch->broken || ch->current_batch || !ready || !QTAILQ_EMPTY(&ch->pending) ||
        (ch->lease && !ch->reclaim_allowed) || !virtio_queue_ready(ch->events)) {
        return;
    }
    threshold = ready >= ch->dev->chameleon_batch_pages;
    watermark = ch->dev->chameleon_watermark_bytes && !ch_host_stats(&stats) &&
        stats.host_available_pages < ch->dev->chameleon_watermark_bytes / LL_PAGE_SIZE;
    if (!threshold && !watermark) {
        return;
    }
    ch->current_batch = ++ch->next_batch;
    ch->batch_immediate = ch->ept_immediate;
    ch->batch_lease = ch->lease;
    ch->nr_batch = 0;
    while (ch->nr_batch < CH_MAX && !QTAILQ_EMPTY(&ch->ready)) {
        ChRange *r = QTAILQ_FIRST(&ch->ready);
        ch_set_state(ch, r, LL_CH_STATE_FINALIZING);
        r->batch = ch->current_batch;
        ch->batch[ch->nr_batch++] = r;
    }
    ch->batch_triggers += threshold;
    ch->watermark_triggers += !threshold && watermark;
    info_report("chameleon finalize-request batch=%" PRIu64 " ranges=%u trigger=%s",
                ch->current_batch, ch->nr_batch, threshold ? "batch" : "host-watermark");
    ch_event(ch, LL_CH_OP_FINALIZE_REQUEST, ch->current_batch, ch->batch, ch->nr_batch);
}

static bool ch_record_available(LLChameleon *ch, unsigned nr)
{
    return nr && (uint64_t)g_hash_table_size(ch->ranges) +
                 ch->forgotten_intervals + nr <= ch->record_limit;
}

static int ch_register(LLChameleon *ch, struct ll_chameleon_range *wire, unsigned nr)
{
    ChRange incoming[CH_MAX];
    ChRange *allocated[CH_MAX];
    uint64_t pages = 0;

    if (!ch_record_available(ch, nr)) {
        return -ENOSPC;
    }
    for (unsigned i = 0; i < nr; i++) {
        ChRange *r = &incoming[i];
        MemoryRegionSection s;
        RAMBlock *rb;
        ram_addr_t offset;
        void *host;
        *r = (ChRange) {
            .token = le64_to_cpu(wire[i].token), .gpa = le64_to_cpu(wire[i].gpa),
            .pages = le32_to_cpu(wire[i].nr_pages), .order = le16_to_cpu(wire[i].order),
            .flags = le16_to_cpu(wire[i].flags), .state = LL_CH_STATE_REGISTERED,
            .charged = true,
        };
        if (!r->token || r->order > 9 || r->order == 1 || r->pages != (1U << r->order) ||
            r->gpa + (uint64_t)r->pages * LL_PAGE_SIZE < r->gpa ||
            !QEMU_IS_ALIGNED(r->gpa, (uint64_t)r->pages * LL_PAGE_SIZE) ||
            (r->flags != 0 && r->flags != LL_CH_RANGE_DISCARD_TEST &&
             r->flags != LL_CH_RANGE_DATA_SAVED) || wire[i].status || wire[i].state) {
            return -EINVAL;
        }
        if (r->flags == LL_CH_RANGE_DATA_SAVED &&
            !virtio_vdev_has_feature(VIRTIO_DEVICE(ch->dev), LL_BALLOON_F_CHAMELEON_DATA)) {
            return -EOPNOTSUPP;
        }
        if (ch_was_forgotten(ch, r->token)) {
            return -ESTALE;
        }
        if (ch_find(ch, r->token)) {
            return -EEXIST;
        }
        for (unsigned j = 0; j < i; j++) {
            if (r->token == incoming[j].token || ch_intersects(r->gpa,
                    (uint64_t)r->pages * LL_PAGE_SIZE, incoming[j].gpa,
                    (uint64_t)incoming[j].pages * LL_PAGE_SIZE)) {
                return -EINVAL;
            }
        }
        if (ch_overlap_locked(ch, r->gpa, (uint64_t)r->pages * LL_PAGE_SIZE, false)) {
            return -EBUSY;
        }
        int ret = ch_ram(r, &s, &rb, &offset, &host);
        if (ret) {
            return ret;
        }
        memory_region_unref(s.mr);
        pages += r->pages;
    }
    if (pages > ch->pool_pages - ch->charged_pages) {
        return -ENOSPC;
    }
    for (unsigned i = 0; i < nr; i++) {
        allocated[i] = g_try_new(ChRange, 1);
        if (!allocated[i]) {
            while (i) {
                g_free(allocated[--i]);
            }
            return -ENOMEM;
        }
        *allocated[i] = incoming[i];
    }
    for (unsigned i = 0; i < nr; i++) {
        ChRange *r = allocated[i];
        g_hash_table_insert(ch->ranges, &r->token, r);
        r->live.start = r->gpa;
        r->live.last = r->gpa + (uint64_t)r->pages * LL_PAGE_SIZE - 1;
        interval_tree_insert(&r->live, &ch->live);
        ch->charged_pages += r->pages;
        ch_wire(r, &wire[i]);
    }
    return 0;
}

static struct kvm_chameleon_range ch_host_range(ChRange *r)
{
    return (struct kvm_chameleon_range) {
        .gpa = r->gpa, .nr_pages = r->pages, .order = r->order, .cookie = r->token,
    };
}

/* ram_block_add() gives supported anonymous RAM MADV_HUGEPAGE. Suppress
 * khugepaged only for exact retained targets, then restore that policy after
 * KVM has accepted population. Never change policy on neighbouring pages. */
static void ch_restore_huge(LLChameleon *ch, ChRange *r)
{
    MemoryRegionSection section;
    RAMBlock *rb;
    ram_addr_t offset;
    void *host;
    int ret;
    if (!r->no_huge || ch->dev->chameleon_vfio) {
        return;
    }
    ret = ch_ram(r, &section, &rb, &offset, &host);
    if (!ret) {
        ret = qemu_madvise(host, (size_t)r->pages * LL_PAGE_SIZE, QEMU_MADV_HUGEPAGE);
        if (ret) {
            ret = -errno;
        }
        memory_region_unref(section.mr);
    }
    if (ret) {
        error_report("Chameleon populated token=%" PRIu64
                     " but could not restore THP policy: %s", r->token, strerror(-ret));
    } else {
        r->no_huge = false;
    }
}

/* These callbacks run on the control queue's main-loop thread, with ch->lock
 * held. VFIO listeners synchronously finish DMA unmap/map before returning;
 * they do not acquire Chameleon or LLFree zone locks. BEGIN's pin validation
 * remains authoritative if a void discard callback failed to release pins. */
static int ch_dma_discard(LLChameleon *ch, ChRange *r)
{
    MemoryRegionSection s;
    RAMBlock *rb;
    ram_addr_t offset;
    void *host;
    int ret;

    if (!ch->dev->chameleon_vfio || r->dma_unmapped) {
        return 0;
    }
    ret = ch_ram(r, &s, &rb, &offset, &host);
    if (ret) {
        return ret;
    }
    r->dma_unmapped = true;
    notify_discard(ch->dev, s.offset_within_region,
                   (uint64_t)r->pages * LL_PAGE_SIZE);
    memory_region_unref(s.mr);
    return 0;
}

static int ch_dma_populate(LLChameleon *ch, ChRange *r)
{
    MemoryRegionSection s;
    RAMBlock *rb;
    ram_addr_t offset;
    void *host;
    int ret;

    if (!r->dma_unmapped) {
        return 0;
    }
    ret = ch_ram(r, &s, &rb, &offset, &host);
    if (ret) {
        return ret;
    }
    /* Discard callbacks have no return value. Clear any residual mapping
     * from a failed unmap, or from a failed earlier populate, before retry. */
    notify_discard(ch->dev, s.offset_within_region,
                   (uint64_t)r->pages * LL_PAGE_SIZE);
    ret = notify_populate(ch->dev, s.offset_within_region,
                          (uint64_t)r->pages * LL_PAGE_SIZE);
    memory_region_unref(s.mr);
    if (!ret) {
        r->dma_unmapped = false;
    }
    return ret;
}

static int ch_install(LLChameleon *ch, ChRange *r)
{
    struct kvm_chameleon_range range = ch_host_range(r);
    struct kvm_chameleon_batch batch = {
        .version = KVM_CHAMELEON_VERSION, .transaction = r->transaction,
        .ranges = (uintptr_t)&range, .nr_ranges = 1,
    };
    MemoryRegionSection s;
    RAMBlock *rb;
    ram_addr_t offset;
    void *host;
    uint64_t resident;
    int ret;

    if (r->state == LL_CH_STATE_INSTALLED) {
        /* BEGIN refusal and NOT_ATTEMPTED retain populated backing. The
         * guest still needs this acknowledgement before zeroing/freeing its
         * finalized reservation. Repeated acknowledgements are harmless. */
        ret = ch_dma_populate(ch, r);
        if (!ret) {
            ch_restore_huge(ch, r);
        }
        return ret;
    }
    if (r->state != LL_CH_STATE_RETIRED && r->state != LL_CH_STATE_ERROR) {
        return -EBUSY;
    }
    if (ch->dev->chameleon_install_fail_count) {
        ch->dev->chameleon_install_fail_count--;
        ret = -ENOMEM;
        goto fail;
    }
    ret = ch_ram(r, &s, &rb, &offset, &host);
    if (ret) {
        goto fail;
    }
    if (qemu_madvise(host, (size_t)r->pages * LL_PAGE_SIZE, MADV_POPULATE_WRITE)) {
        ret = -errno;
    } else {
        ret = ch_resident(r, &resident);
        if (!ret && resident != r->pages) {
            ret = -EIO;
        }
    }
    memory_region_unref(s.mr);
    if (!ret && !r->host_installed) {
        ret = kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_INSTALL, &batch);
        if (ret) {
            /* INSTALL can succeed before a failed copyout. Never mistake
             * already-unblocked backing for a still-retired transaction. */
            range = ch_host_range(r);
            if (!kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_QUERY, &batch) &&
                range.state == KVM_CHAMELEON_RANGE_INSTALLED) {
                ret = 0;
            }
        }
        if (!ret && range.state != KVM_CHAMELEON_RANGE_INSTALLED) {
            ret = range.status ? range.status : -EIO;
        }
        if (!ret) {
            r->host_installed = true;
        }
    }
    if (ret) {
        goto fail;
    }
    /* Mapping before KVM INSTALL would pin the new backing and be rejected.
     * Keep the guest's non-present reservation on remap failure; retry only
     * the missing DMA mapping without reissuing a completed Host INSTALL. */
    ret = ch_dma_populate(ch, r);
    if (ret) {
        goto fail;
    }
    ch_restore_huge(ch, r);
    if (r->state == LL_CH_STATE_RETIRED) {
        ch->retired_pages -= r->pages;
    } else {
        ch->error_pages -= r->pages;
    }
    ch_uncharge(ch, r);
    ch_set_state(ch, r, LL_CH_STATE_INSTALLED);
    r->status = 0;
    ch->installed_pages += r->pages;
    info_report("chameleon installed token=%" PRIu64 " gpa=0x%" PRIx64 " pages=%u",
                r->token, r->gpa, r->pages);
    return 0;
fail:
    ch->install_failures++;
    return ret;
}

static void ch_host_commit(LLChameleon *ch, uint64_t batch_id,
                           ChRange **accepted, unsigned count, unsigned first_index)
{
    struct kvm_chameleon_range ranges[CH_MAX];
    struct kvm_chameleon_batch transaction = {
        .version = KVM_CHAMELEON_VERSION, .ranges = (uintptr_t)ranges,
    };
    int ret;
    for (unsigned i = 0; i < count; i++) {
        ranges[i] = ch_host_range(accepted[i]);
        ret = ch_dma_discard(ch, accepted[i]);
        if (ret) {
            goto begin_failed;
        }
    }
    transaction.nr_ranges = count;
    if (ch->dev->chameleon_begin_fail_count) {
        ch->dev->chameleon_begin_fail_count--;
        ret = -EAGAIN;
        goto begin_failed;
    }
    ret = kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_BEGIN, &transaction);
    if (ret) {
begin_failed:
        /* BEGIN validates before mutation. Nothing was discarded. */
        info_report("chameleon begin-refused batch=%" PRIu64 " ranges=%u status=%d",
                    batch_id, count, ret);
        for (unsigned i = 0; i < count; i++) {
            ChRange *r = accepted[i];
            int map_ret = ch_dma_populate(ch, r);
            r->host_installed = true;
            ch_set_state(ch, r, map_ret ? LL_CH_STATE_ERROR : LL_CH_STATE_INSTALLED);
            r->status = map_ret ? -map_ret : -ret;
            if (map_ret) {
                ch->error_pages += r->pages;
            }
            ch_uncharge(ch, r);
        }
        return;
    }
    info_report("chameleon begin-complete batch=%" PRIu64 " transaction=%" PRIu64 " ranges=%u",
                batch_id, (uint64_t)transaction.transaction, count);
    for (unsigned i = 0; i < count; i++) {
        ChRange *r = accepted[i];
        MemoryRegionSection s;
        RAMBlock *rb;
        ram_addr_t offset;
        void *host;
        r->transaction = transaction.transaction;
        ranges[i].state = KVM_CHAMELEON_RANGE_NOT_ATTEMPTED;
        ranges[i].status = 0;
        if (ch->dev->chameleon_discard_fail_index == (int)(first_index + i)) {
            ch->dev->chameleon_discard_fail_index = -1;
            r->status = EIO;
            continue;
        }
        ret = ch_ram(r, &s, &rb, &offset, &host);
        if (ret) {
            r->status = -ret;
            continue;
        }
        /* BEGIN's expanded guard and EPT flush already cover this change.
         * Without this exact-range policy, khugepaged could allocate backing
         * into a retired hole behind KVM's fault guards. */
        if (qemu_madvise(host, (size_t)r->pages * LL_PAGE_SIZE, QEMU_MADV_NOHUGEPAGE)) {
            r->status = errno;
            memory_region_unref(s.mr);
            continue; /* NOT_ATTEMPTED: no discard syscall has occurred. */
        }
        r->no_huge = true;
        ret = ram_block_discard_range(rb, offset, (size_t)r->pages * LL_PAGE_SIZE);
        memory_region_unref(s.mr);
        ranges[i].state = ret ? KVM_CHAMELEON_RANGE_ERROR_UNKNOWN : KVM_CHAMELEON_RANGE_DISCARDED;
        ranges[i].status = ret;
        r->status = ret ? -ret : 0;
        info_report("chameleon discard transaction=%" PRIu64 " token=%" PRIu64
                    " gpa=0x%" PRIx64 " pages=%u status=%d",
                    r->transaction, r->token, r->gpa, r->pages, ret);
    }
    ret = kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_REPORT, &transaction);
    if (ret) {
        /* REPORT copyout may fail after the atomic state transition. Query
         * authoritative Host state before exposing a result to the guest. */
        struct kvm_chameleon_range query[CH_MAX];
        struct kvm_chameleon_batch probe = transaction;
        bool pending = true;
        probe.ranges = (uintptr_t)query;
        for (unsigned i = 0; i < count; i++) {
            query[i] = ch_host_range(accepted[i]);
        }
        int query_ret = kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_QUERY, &probe);
        if (!query_ret) {
            for (unsigned i = 0; i < count; i++) {
                pending &= query[i].state == KVM_CHAMELEON_RANGE_PENDING;
            }
            if (pending) {
                ret = kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_REPORT, &transaction);
            } else {
                ret = 0;
                memcpy(ranges, query, count * sizeof(*ranges));
            }
        }
        if (ret) {
            /* An unresolved Host guard must never become reusable backing. */
            ch->broken = true;
            error_report("Chameleon REPORT/QUERY failed: transaction=%" PRIu64
                         " report=%d query=%d; stopping VM with ranges retained",
                         (uint64_t)transaction.transaction, ret, query_ret);
            qemu_system_vmstop_request_prepare();
            qemu_system_vmstop_request(RUN_STATE_INTERNAL_ERROR);
        }
    }
    for (unsigned i = 0; i < count; i++) {
        ChRange *r = accepted[i];
        if (ret || ranges[i].state == KVM_CHAMELEON_RANGE_ERROR_UNKNOWN) {
            ch_set_state(ch, r, LL_CH_STATE_ERROR);
            r->status = ret ? -ret : -ranges[i].status;
            ch->error_pages += r->pages;
        } else if (ranges[i].state == KVM_CHAMELEON_RANGE_DISCARDED) {
            uint64_t resident;
            ch_set_state(ch, r, LL_CH_STATE_RETIRED);
            ch->retired_pages += r->pages;
            ch->discarded_pages += r->pages;
            ch_uncharge(ch, r);
            int rc = ch_resident(r, &resident);
            info_report("chameleon retired token=%" PRIu64 " pages=%u mincore_resident=%" PRIu64 " rc=%d",
                        r->token, r->pages, resident, rc);
            if (rc || resident) {
                r->status = rc ? -rc : EIO;
            }
        } else {
            int map_ret = ch_dma_populate(ch, r);
            r->host_installed = true;
            ch_set_state(ch, r, map_ret ? LL_CH_STATE_ERROR : LL_CH_STATE_INSTALLED);
            if (map_ret) {
                r->status = -map_ret;
                ch->error_pages += r->pages;
            }
            ch_uncharge(ch, r);
        }
    }
}

static int ch_commit_ack(LLChameleon *ch, uint64_t batch_id,
                         struct ll_chameleon_range *wire, unsigned nr)
{
    ChRange *accepted[CH_MAX];
    unsigned count = 0;

    if (!batch_id || batch_id != ch->current_batch || nr != ch->nr_batch) {
        return -ESTALE;
    }
    for (unsigned i = 0; i < nr; i++) {
        ChRange *r = ch->batch[i];
        if (!ch_identity(r, &wire[i])) {
            return -EINVAL;
        }
    }
    ch->finalize_acks++;
    for (unsigned i = 0; i < nr; i++) {
        ChRange *r = ch->batch[i];
        if (r->state == LL_CH_STATE_FINALIZING && !wire[i].status) {
            if (ch->batch_lease &&
                (ch->lease != ch->batch_lease || !ch->reclaim_allowed)) {
                ch_set_state(ch, r, LL_CH_STATE_INSTALLED);
                r->status = ECANCELED;
                ch->gate_cancellations++;
                ch_uncharge(ch, r);
            } else {
                accepted[count++] = r;
                ch_set_state(ch, r, LL_CH_STATE_BLOCKED);
            }
        } else if (r->state == LL_CH_STATE_FINALIZING) {
            ch_set_state(ch, r, LL_CH_STATE_CANCELED);
            r->status = le32_to_cpu(wire[i].status);
            ch_uncharge(ch, r);
        }
    }
    info_report("chameleon finalize-ack batch=%" PRIu64 " accepted=%u ranges=%u",
                batch_id, count, nr);
    if (!count) {
        goto completed;
    }
    if (ch->batch_immediate) {
        for (unsigned i = 0; i < count && !ch->broken; i++) {
            ch_host_commit(ch, batch_id, &accepted[i], 1, i);
        }
    } else {
        ch_host_commit(ch, batch_id, accepted, count, 0);
    }
completed:
    for (unsigned i = 0; i < nr; i++) {
        ch_wire(ch->batch[i], &wire[i]);
    }
    ch_event(ch, LL_CH_OP_COMMIT_RESULT, batch_id, ch->batch, ch->nr_batch);
    ch->current_batch = 0;
    ch->nr_batch = 0;
    return 0;
}

static int ch_request(LLChameleon *ch, struct ll_chameleon_header *header,
                      struct ll_chameleon_range *ranges, unsigned nr)
{
    unsigned op = le16_to_cpu(header->op);
    uint64_t request = le64_to_cpu(header->request_id);

    if (ch->broken) {
        return -EIO;
    }
    if (header->flags || header->status || header->reserved ||
        le16_to_cpu(header->version) != LL_CHAMELEON_VERSION) {
        return -EINVAL;
    }
    if (op == LL_CH_OP_HELLO) {
        return nr || header->session || header->batch_id ? -EINVAL : 0;
    }
    if (le64_to_cpu(header->session) != ch->session) {
        return -ESTALE;
    }
    if (!request || request <= ch->last_request) {
        return -EALREADY;
    }
    ch->last_request = request;
    if (op == LL_CH_OP_REGISTER) {
        if (header->batch_id) {
            return -EINVAL;
        }
        return ch_register(ch, ranges, nr);
    }
    if (op == LL_CH_OP_FINALIZE_ACK) {
        return ch_commit_ack(ch, le64_to_cpu(header->batch_id), ranges, nr);
    }
    if (!nr || header->batch_id || (op != LL_CH_OP_READY && op != LL_CH_OP_CANCEL &&
        op != LL_CH_OP_INSTALL && op != LL_CH_OP_QUERY && op != LL_CH_OP_FORGET)) {
        return -EINVAL;
    }
    for (unsigned i = 0; i < nr; i++) {
        ChRange *r = ch_find(ch, le64_to_cpu(ranges[i].token));
        int ret = 0;
        bool forget = false;
        if (!r || !ch_identity(r, &ranges[i])) {
            ranges[i].status = cpu_to_le32(ESTALE);
            ranges[i].state = cpu_to_le32(LL_CH_STATE_UNKNOWN);
            continue;
        }
        if (ranges[i].status || ranges[i].state) {
            ranges[i].status = cpu_to_le32(EINVAL);
            continue;
        }
        switch (op) {
        case LL_CH_OP_READY:
            if (r->flags != LL_CH_RANGE_DISCARD_TEST && r->flags != LL_CH_RANGE_DATA_SAVED) {
                ret = -EOPNOTSUPP;
            } else if (r->state != LL_CH_STATE_REGISTERED) {
                ret = -EBUSY;
            } else {
                ch_set_state(ch, r, LL_CH_STATE_READY);
            }
            break;
        case LL_CH_OP_CANCEL:
            if (r->state == LL_CH_STATE_REGISTERED || r->state == LL_CH_STATE_READY ||
                r->state == LL_CH_STATE_FINALIZING) {
                ch_set_state(ch, r, LL_CH_STATE_CANCELED);
                ch_uncharge(ch, r);
            } else {
                ret = r->state == LL_CH_STATE_CANCELED ? -EALREADY : -EBUSY;
            }
            break;
        case LL_CH_OP_INSTALL:
            ret = ch_install(ch, r);
            break;
        case LL_CH_OP_FORGET:
            if (!virtio_vdev_has_feature(VIRTIO_DEVICE(ch->dev), LL_BALLOON_F_CHAMELEON_POLICY) &&
                !virtio_vdev_has_feature(VIRTIO_DEVICE(ch->dev), LL_BALLOON_F_CHAMELEON_DATA)) {
                ret = -EOPNOTSUPP;
            } else if (ch_live(r) || (ch->current_batch && r->batch == ch->current_batch)) {
                ret = -EBUSY;
            } else {
                ret = ch_remember_forgotten(ch, r->token);
                forget = !ret;
            }
            break;
        case LL_CH_OP_QUERY:
            break;
        }
        if (op != LL_CH_OP_QUERY) {
            r->status = ret ? -ret : 0;
        }
        ch_wire(r, &ranges[i]);
        if (forget) {
            uint64_t token = r->token;
            g_hash_table_remove(ch->ranges, &token);
        }
    }
    return 0;
}

/* Legacy controller lock -> zone lock -> Chameleon lock. Never acquire a
 * zone while holding ch->lock; per-CPU installation has the opposite edge. */
static uint64_t ch_node_pages(VirtIOLLFreeBalloon *dev, size_t node, bool hard)
{
    uint64_t pages = 0;
    for (ll_zone_type_t zid = 0; zid < LL_ZONE_LEN; zid++) {
        ll_zone_t *zone = ll_nodes_get_zone(dev->nodes, node, zid);
        if (!zone) {
            continue;
        }
        ll_zone_lock(zone);
        for (uint64_t frame = 0; frame + LL_HUGE_COUNT <= ll_zone_frames(zone); frame += LL_HUGE_COUNT) {
            ll_reclaim_states_t *states = ll_zone_states(zone);
            if (hard ? ll_reclaim_states_is_hard(states, frame) :
                       ll_reclaim_states_is_soft(states, frame)) {
                pages += LL_HUGE_COUNT;
            }
        }
        ll_zone_unlock(zone);
    }
    return pages;
}

static uint64_t ch_soft_bytes(VirtIOLLFreeBalloon *dev)
{
    uint64_t pages = 0;
    for (size_t node = 0; node < ll_nodes_len(dev->nodes); node++) {
        pages += ch_node_pages(dev, node, false);
    }
    return pages * LL_PAGE_SIZE;
}

static uint64_t ch_return_free(VirtIOLLFreeBalloon *dev, uint64_t huge)
{
    uint64_t done = 0;
    for (size_t node = 0; node < ll_nodes_len(dev->nodes) && done < huge; node++) {
        uint64_t available = ch_node_pages(dev, node, true) / LL_HUGE_COUNT;
        uint64_t count = MIN(huge - done, available);
        if (count) {
            uint64_t returned = return_node(dev, node, count);
            qatomic_sub(&dev->actual_huge, returned);
            done += returned;
        }
    }
    return done;
}

/* legacy_lock held; read each zone before locking the Chameleon table. */
static void ch_policy_snapshot(LLChameleon *ch, struct ll_chameleon_policy *wire)
{
    VirtIOLLFreeBalloon *dev = ch->dev;
    uint64_t soft = ch_soft_bytes(dev);
    uint64_t total = get_current_ram_size();
    uint64_t hard = qatomic_read(&dev->actual_huge) * LL_HUGE_SIZE;
    qemu_mutex_lock(&ch->lock);
    uint64_t retired = ch->retired_pages * LL_PAGE_SIZE;
    g_assert(hard <= total && retired <= total - hard);
    wire->total_bytes = cpu_to_le64(total);
    wire->local_bytes = cpu_to_le64(total - hard - retired);
    wire->hard_reclaimed_bytes = cpu_to_le64(hard);
    wire->retired_bytes = cpu_to_le64(retired);
    wire->soft_reclaimed_bytes = cpu_to_le64(soft);
    wire->flags = cpu_to_le32((ch->lease ? LL_CH_POLICY_LEASED : 0) |
        ((!ch->lease || ch->reclaim_allowed) ? LL_CH_POLICY_ALLOW_RECLAIM : 0) |
        ((ch->error_pages || ch->broken) ? LL_CH_POLICY_UNCERTAIN : 0));
    wire->ept_mode = cpu_to_le32(ch->ept_immediate ? LL_CH_EPT_IMMEDIATE : LL_CH_EPT_DEFERRED);
    qemu_mutex_unlock(&ch->lock);
}

static int ch_policy_request(LLChameleon *ch, unsigned op,
                             struct ll_chameleon_policy *wire)
{
    VirtIOLLFreeBalloon *dev = ch->dev;
    uint64_t lease = le64_to_cpu(wire->lease), epoch = le64_to_cpu(wire->epoch);
    uint64_t requested = le64_to_cpu(wire->requested_bytes), done = 0;
    unsigned flags = le32_to_cpu(wire->flags);
    bool action = op == LL_CH_OP_POLICY_RECLAIM_FREE || op == LL_CH_OP_POLICY_RETURN_FREE;
    int ret = 0;

    qemu_mutex_lock(&dev->legacy_lock);
    if (wire->completed_bytes || wire->total_bytes || wire->local_bytes ||
        wire->hard_reclaimed_bytes || wire->retired_bytes || wire->soft_reclaimed_bytes ||
        wire->ept_mode || (flags & ~LL_CH_POLICY_ALLOW_RECLAIM) ||
        (op != LL_CH_OP_POLICY_GATE && flags) || (!action && (epoch || requested))) {
        ret = -EINVAL;
        goto out;
    }
    qemu_mutex_lock(&ch->lock);
    if (ch->broken) {
        ret = -EIO;
    } else if (op == LL_CH_OP_POLICY_ACQUIRE) {
        if (lease || ch->lease || ch->current_batch) {
            ret = -EBUSY;
        } else {
            ch->lease = ++ch->next_lease;
            ch->last_epoch = 0;
            ch->lease_initial_hard = qatomic_read(&dev->actual_huge);
            ch->reclaim_allowed = false;
            wire->lease = cpu_to_le64(ch->lease);
            qatomic_set_u64(&dev->target_huge, ch->lease_initial_hard);
            dev->shrink_pagecache = 0;
            timer_del(dev->retry_llfree_timer);
            info_report("chameleon policy-acquire lease=%" PRIu64, ch->lease);
        }
    } else if (op == LL_CH_OP_POLICY_SNAPSHOT && !lease) {
        /* A capability/monitor query does not acquire controller ownership. */
    } else if (!lease || lease != ch->lease) {
        ret = -ESTALE;
    } else if (op == LL_CH_OP_POLICY_GATE) {
        ch->reclaim_allowed = !!(flags & LL_CH_POLICY_ALLOW_RECLAIM);
    } else if (action && (!epoch || epoch <= ch->last_epoch)) {
        ret = -EALREADY;
    } else if (op == LL_CH_OP_POLICY_RECLAIM_FREE && !ch->reclaim_allowed) {
        ret = -EBUSY;
    } else if (action) {
        ch->last_epoch = epoch;
    } else if (op == LL_CH_OP_POLICY_RELEASE) {
        ch->reclaim_allowed = false;
    }
    if (ret) {
        ch->policy_denials++;
    }
    qemu_mutex_unlock(&ch->lock);
    if (ret) {
        goto out;
    }
    if (action) {
        uint64_t huge = MIN(requested / LL_HUGE_SIZE, (uint64_t)CH_MAX);
        if (op == LL_CH_OP_POLICY_RECLAIM_FREE) {
            for (size_t node = 0; node < ll_nodes_len(dev->nodes) && done < huge; node++) {
                uint64_t count = hard_reclaim_node(dev, node, huge - done);
                qatomic_add(&dev->actual_huge, count);
                done += count;
            }
            ch->free_reclaim_ops++;
            ch->free_reclaimed_bytes += done * LL_HUGE_SIZE;
        } else {
            done = ch_return_free(dev, MIN(huge, qatomic_read(&dev->actual_huge)));
            ch->free_return_ops++;
            ch->free_returned_bytes += done * LL_HUGE_SIZE;
        }
        info_report("chameleon policy-free lease=%" PRIu64 " epoch=%" PRIu64
                    " op=%s requested=%" PRIu64 " completed=%" PRIu64,
                    lease, epoch, op == LL_CH_OP_POLICY_RECLAIM_FREE ? "reclaim" : "return",
                    requested, done * LL_HUGE_SIZE);
    } else if (op == LL_CH_OP_POLICY_RELEASE) {
        uint64_t current = qatomic_read(&dev->actual_huge);
        if (current > ch->lease_initial_hard) {
            done = ch_return_free(dev, current - ch->lease_initial_hard);
            ch->free_returned_bytes += done * LL_HUGE_SIZE;
        }
        qemu_mutex_lock(&ch->lock);
        ch->lease = 0;
        ch->reclaim_allowed = false;
        qemu_mutex_unlock(&ch->lock);
        info_report("chameleon policy-release lease=%" PRIu64 " returned=%" PRIu64,
                    lease, done * LL_HUGE_SIZE);
    }
    if (action || op == LL_CH_OP_POLICY_RELEASE) {
        qatomic_set_u64(&dev->target_huge, qatomic_read(&dev->actual_huge));
    }
    wire->completed_bytes = cpu_to_le64(done * LL_HUGE_SIZE);
out:
    ch_policy_snapshot(ch, wire);
    qemu_mutex_unlock(&dev->legacy_lock);
    return ret;
}

static void handle_chameleon_control(VirtIODevice *vdev, VirtQueue *vq)
{
    LLChameleon *ch = VIRTIO_LLFREE_BALLOON(vdev)->chameleon;
    VirtQueueElement *elem;
    qemu_mutex_lock(&ch->lock);
    while ((elem = virtqueue_pop(vq, sizeof(*elem)))) {
        struct ll_chameleon_header header = {0};
        struct ll_chameleon_range ranges[CH_MAX];
        struct ll_chameleon_policy policy = {0};
        unsigned nr = 0;
        size_t length = sizeof(header), written;
        bool policy_op = false;
        int ret = -EINVAL;
        if (iov_size(elem->out_sg, elem->out_num) < sizeof(header) ||
            iov_size(elem->in_sg, elem->in_num) < sizeof(header)) {
            virtio_error(vdev, "chameleon: missing request/response header");
            virtqueue_push(vq, elem, 0);
            g_free(elem);
            break;
        }
        iov_to_buf(elem->out_sg, elem->out_num, 0, &header, sizeof(header));
        unsigned op = le16_to_cpu(header.op);
        nr = le32_to_cpu(header.nr_ranges);
        policy_op = op >= LL_CH_OP_POLICY_ACQUIRE && op <= LL_CH_OP_POLICY_GATE;
        if (policy_op) {
            length += sizeof(policy);
            if (!nr && iov_size(elem->out_sg, elem->out_num) == length &&
                iov_size(elem->in_sg, elem->in_num) >= length) {
                iov_to_buf(elem->out_sg, elem->out_num, sizeof(header), &policy, sizeof(policy));
                uint64_t request = le64_to_cpu(header.request_id);
                if (!virtio_vdev_has_feature(vdev, LL_BALLOON_F_CHAMELEON_POLICY)) {
                    ret = -EOPNOTSUPP;
                } else if (header.flags || header.status || header.reserved || header.batch_id ||
                           le16_to_cpu(header.version) != LL_CHAMELEON_VERSION) {
                    ret = -EINVAL;
                } else if (le64_to_cpu(header.session) != ch->session) {
                    ret = -ESTALE;
                } else if (!request || request <= ch->last_request) {
                    ret = -EALREADY;
                } else {
                    ch->last_request = request;
                    qemu_mutex_unlock(&ch->lock);
                    ret = ch_policy_request(ch, op, &policy);
                    qemu_mutex_lock(&ch->lock);
                }
            }
            nr = 0;
        } else if (nr <= CH_MAX) {
            length += nr * sizeof(ranges[0]);
            if (iov_size(elem->out_sg, elem->out_num) == length &&
                iov_size(elem->in_sg, elem->in_num) >= length &&
                virtio_vdev_has_feature(vdev, LL_BALLOON_F_CHAMELEON_RANGE)) {
                iov_to_buf(elem->out_sg, elem->out_num, sizeof(header), ranges,
                           nr * sizeof(ranges[0]));
                ret = ch_request(ch, &header, ranges, nr);
            } else {
                nr = 0;
            }
        } else {
            nr = 0;
        }
        header.flags = cpu_to_le32(LL_CH_MSG_RESPONSE);
        header.session = cpu_to_le64(ch->session);
        header.status = cpu_to_le32(ret ? -ret : 0);
        header.nr_ranges = cpu_to_le32(nr);
        written = iov_from_buf(elem->in_sg, elem->in_num, 0, &header, sizeof(header));
        if (policy_op) {
            written += iov_from_buf(elem->in_sg, elem->in_num, sizeof(header), &policy, sizeof(policy));
        } else {
            written += iov_from_buf(elem->in_sg, elem->in_num, sizeof(header), ranges,
                                    nr * sizeof(ranges[0]));
        }
        virtqueue_push(vq, elem, written);
        virtio_notify(vdev, vq);
        g_free(elem);
        ch_maybe_finalize(ch);
    }
    qemu_mutex_unlock(&ch->lock);
}

static void handle_chameleon_events(VirtIODevice *vdev, VirtQueue *vq)
{
    LLChameleon *ch = VIRTIO_LLFREE_BALLOON(vdev)->chameleon;
    qemu_mutex_lock(&ch->lock);
    ch_send_events(ch);
    ch_maybe_finalize(ch);
    qemu_mutex_unlock(&ch->lock);
}

static void ch_tick(void *opaque)
{
    LLChameleon *ch = opaque;
    qemu_mutex_lock(&ch->lock);
    ch_maybe_finalize(ch);
    ch_send_events(ch);
    qemu_mutex_unlock(&ch->lock);
    timer_mod(ch->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 100);
}

static bool ch_init(VirtIOLLFreeBalloon *dev, Error **errp)
{
    struct kvm_chameleon_tuning tuning = {
        .version = KVM_CHAMELEON_VERSION,
        .flags = KVM_CHAMELEON_TUNING_SET_ALL,
    };
    bool host_tuning;
    int ret;
    if (!dev->chameleon_enabled) {
        if (dev->chameleon_policy_enabled || dev->chameleon_ept_mode ||
            dev->chameleon_vfio) {
            error_setg(errp, "Chameleon policy/EPT/VFIO mode requires chameleon=on");
            return false;
        }
        return true;
    }
    if (dev->chameleon_ept_mode && strcmp(dev->chameleon_ept_mode, "deferred") &&
        strcmp(dev->chameleon_ept_mode, "immediate")) {
        error_setg(errp, "chameleon-ept-mode must be deferred or immediate");
        return false;
    }
    if (!kvm_enabled() || kvm_check_extension(kvm_state, KVM_CAP_CHAMELEON_RECLAIM) <= 0) {
        error_setg(errp, "chameleon=on requires KVM_CAP_CHAMELEON_RECLAIM (0x48410002)");
        return false;
    }
    MachineState *machine = MACHINE(qdev_get_machine());
    if (machine->mem_merge) {
        error_setg(errp, "chameleon=on requires -machine mem-merge=off (KSM is unsupported)");
        return false;
    }
    ram_addr_t ram_offset;
    RAMBlock *ram = qemu_ram_block_from_host(memory_region_get_ram_ptr(machine->ram),
                                            false, &ram_offset);
    if (!ram || memory_region_get_fd(machine->ram) != -1 ||
        qemu_ram_is_shared(ram) || qemu_ram_pagesize(ram) != LL_PAGE_SIZE) {
        error_setg(errp, "Chameleon requires private anonymous RAM with 4 KiB base pages");
        return false;
    }
    if (!dev->chameleon_batch_pages ||
        (!dev->chameleon_vfio && (dev->host_features & (1U << LL_BALLOON_F_VFIO)))) {
        error_setg(errp, "Chameleon requires nonzero batch-pages; VFIO requires chameleon-vfio=on");
        return false;
    }
    if (dev->chameleon_vfio) {
        bool ambiguous = false;
        if (!machine->memdev || machine->memdev->thp) {
            error_setg(errp, "chameleon-vfio=on requires memory-backend-ram with thp=off before first RAM allocation");
            return false;
        }
        if (object_resolve_path_type("", "vfio-pci", &ambiguous) || ambiguous) {
            error_setg(errp, "realize Chameleon balloon before any vfio-pci device so DMA mapping uses its discard manager");
            return false;
        }
    }
    ret = kvm_vm_enable_cap(kvm_state, KVM_CAP_CHAMELEON_RECLAIM, 0, 1,
                           dev->chameleon_vfio ? KVM_CHAMELEON_ENABLE_VFIO : 0);
    if (ret) {
        error_setg_errno(errp, -ret, "cannot enable Chameleon KVM transactions");
        return false;
    }
    tuning.ept_mode = dev->chameleon_ept_mode &&
        !strcmp(dev->chameleon_ept_mode, "immediate") ?
        KVM_CHAMELEON_EPT_IMMEDIATE : KVM_CHAMELEON_EPT_DEFERRED;
    tuning.batch_pages = dev->chameleon_batch_pages;
    tuning.watermark_bytes = dev->chameleon_watermark_bytes;
    ret = kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_TUNING, &tuning);
    host_tuning = !ret;
    if (ret == -ENOTTY) {
        warn_report("Host KVM lacks Chameleon debugfs tuning; using existing QMP controls");
    } else if (ret) {
        error_setg_errno(errp, -ret, "cannot register Chameleon Host tuning");
        return false;
    }
    LLChameleon *ch = g_new0(LLChameleon, 1);
    error_setg(&ch->migration_blocker, "Chameleon range ownership cannot be migrated");
    if (migrate_add_blocker(&ch->migration_blocker, errp)) {
        g_free(ch);
        return false;
    }
    ch->dev = dev;
    ch->host_tuning = host_tuning;
    ch->ept_immediate = dev->chameleon_ept_mode && !strcmp(dev->chameleon_ept_mode, "immediate");
    QTAILQ_INIT(&ch->ready);
    /* One metadata unit per nominal base page supports the most fragmented
     * active remote image. Terminal records and exact replay intervals share
     * this bound; they must be forgotten/coalesced to admit later lifetimes. */
    ch->record_limit = MIN((uint64_t)G_MAXUINT,
                          MAX((uint64_t)CH_MAX, get_current_ram_size() / LL_PAGE_SIZE));
    ch->session = qemu_clock_get_ns(QEMU_CLOCK_REALTIME) ?: 1;
    ch->pool_pages = get_current_ram_size() / (200ULL * LL_PAGE_SIZE);
    ch->ranges = g_hash_table_new_full(g_int64_hash, g_int64_equal, NULL, g_free);
    qemu_mutex_init(&ch->lock);
    QTAILQ_INIT(&ch->pending);
    dev->chameleon = ch;
    ch->timer = timer_new_ms(QEMU_CLOCK_REALTIME, ch_tick, ch);
    timer_mod(ch->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 100);
    return true;
}

static void ch_add_queues(VirtIOLLFreeBalloon *dev)
{
    if (dev->chameleon) {
        dev->chameleon->ctrl = virtio_add_queue(VIRTIO_DEVICE(dev), 128, handle_chameleon_control);
        dev->chameleon->events = virtio_add_queue(VIRTIO_DEVICE(dev), 128, handle_chameleon_events);
    }
}

static bool ch_reset(VirtIOLLFreeBalloon *dev)
{
    LLChameleon *ch = dev->chameleon;
    GHashTableIter iter;
    void *value;
    if (!ch) {
        return true;
    }
    qemu_mutex_lock(&ch->lock);
    if (ch->broken) {
        qemu_mutex_unlock(&ch->lock);
        return false;
    }
    g_hash_table_iter_init(&iter, ch->ranges);
    while (g_hash_table_iter_next(&iter, NULL, &value)) {
        ChRange *r = value;
        if ((r->state == LL_CH_STATE_RETIRED || r->state == LL_CH_STATE_ERROR) && ch_install(ch, r)) {
            virtio_error(VIRTIO_DEVICE(dev), "Chameleon reset cannot repopulate retained GPA");
            qemu_mutex_unlock(&ch->lock);
            return false;
        }
    }
    while (!QTAILQ_EMPTY(&ch->pending)) {
        ChEvent *event = QTAILQ_FIRST(&ch->pending);
        QTAILQ_REMOVE(&ch->pending, event, next);
        g_free(event);
    }
    ch->live = (IntervalTreeRoot){};
    QTAILQ_INIT(&ch->ready);
    ch->ready_pages = 0;
    g_hash_table_remove_all(ch->ranges);
    ch_clear_forgotten(ch);
    ch->forgotten_tokens = 0;
    ch->lease = ch->batch_lease = ch->last_epoch = 0;
    ch->reclaim_allowed = false;
    ch->charged_pages = ch->retired_pages = ch->error_pages = 0;
    ch->current_batch = ch->nr_batch = ch->last_request = 0;
    ch->session++;
    qemu_mutex_unlock(&ch->lock);
    return true;
}

static void ch_cleanup(VirtIOLLFreeBalloon *dev)
{
    LLChameleon *ch = dev->chameleon;
    if (!ch) {
        return;
    }
    timer_del(ch->timer);
    timer_free(ch->timer);
    ch_reset(dev);
    if (ch->ctrl) {
        virtio_delete_queue(ch->ctrl);
        virtio_delete_queue(ch->events);
    }
    migrate_del_blocker(&ch->migration_blocker);
    g_hash_table_destroy(ch->ranges);
    ch_clear_forgotten(ch);
    qemu_mutex_destroy(&ch->lock);
    g_free(ch);
    dev->chameleon = NULL;
}

static void ch_configure(void *opaque, ChameleonConfig *config, Error **errp)
{
    VirtIOLLFreeBalloon *dev = opaque;
    LLChameleon *ch = dev->chameleon;
    if (!ch) {
        error_setg(errp, "Chameleon is not enabled on this device");
        return;
    }
    if ((config->has_batch_pages && !config->batch_pages) ||
        (config->has_discard_fail_index && (config->discard_fail_index < -1 ||
                                          config->discard_fail_index >= CH_MAX))) {
        error_setg(errp, "invalid Chameleon threshold or fault-injection index");
        return;
    }
    qemu_mutex_lock(&ch->lock);
    if (config->has_ept_mode && ch->current_batch) {
        error_setg(errp, "cannot change EPT mode during a pending Chameleon batch");
        qemu_mutex_unlock(&ch->lock);
        return;
    }
    if (ch->host_tuning) {
        struct kvm_chameleon_tuning tuning = {
            .version = KVM_CHAMELEON_VERSION,
            .flags = (config->has_ept_mode ? KVM_CHAMELEON_TUNING_SET_MODE : 0) |
                (config->has_batch_pages ? KVM_CHAMELEON_TUNING_SET_BATCH : 0) |
                (config->has_watermark_bytes ? KVM_CHAMELEON_TUNING_SET_WATERMARK : 0),
            .ept_mode = config->ept_mode == CHAMELEONEPT_MODE_IMMEDIATE ?
                KVM_CHAMELEON_EPT_IMMEDIATE : KVM_CHAMELEON_EPT_DEFERRED,
            .batch_pages = config->batch_pages,
            .watermark_bytes = config->watermark_bytes,
        };
        int ret = kvm_vm_ioctl(kvm_state, KVM_CHAMELEON_TUNING, &tuning);

        if (ret) {
            error_setg_errno(errp, -ret, "cannot update Chameleon Host tuning");
            qemu_mutex_unlock(&ch->lock);
            return;
        }
        ch_apply_tuning(ch, &tuning);
    } else {
        if (config->has_ept_mode) {
            ch->ept_immediate = config->ept_mode == CHAMELEONEPT_MODE_IMMEDIATE;
        }
        if (config->has_batch_pages) {
            dev->chameleon_batch_pages = config->batch_pages;
        }
        if (config->has_watermark_bytes) {
            dev->chameleon_watermark_bytes = config->watermark_bytes;
        }
    }
    if (config->has_install_fail_count) {
        dev->chameleon_install_fail_count = config->install_fail_count;
    }
    if (config->has_begin_fail_count) {
        dev->chameleon_begin_fail_count = config->begin_fail_count;
    }
    if (config->has_dma_map_fail_count) {
        dev->chameleon_dma_map_fail_count = config->dma_map_fail_count;
    }
    if (config->has_discard_fail_index) {
        dev->chameleon_discard_fail_index = config->discard_fail_index;
    }
    ch_maybe_finalize(ch);
    qemu_mutex_unlock(&ch->lock);
    virtio_notify_config(VIRTIO_DEVICE(dev));
}

static void ch_stat(VirtIOLLFreeBalloon *dev, LLfreeBalloonInfo *info)
{
    LLChameleon *ch = dev->chameleon;
    struct kvm_chameleon_stats ks;
    GHashTableIter iter;
    void *value;
    if (!ch) {
        return;
    }
    ChameleonInfo *s = g_new0(ChameleonInfo, 1);
    info->chameleon = s;
    qemu_mutex_lock(&dev->legacy_lock);
    uint64_t soft_bytes = dev->chameleon_policy_enabled ? ch_soft_bytes(dev) : 0;
    qemu_mutex_lock(&ch->lock);
    ch_sync_tuning(ch);
    s->ept_mode = ch->ept_immediate ? CHAMELEONEPT_MODE_IMMEDIATE : CHAMELEONEPT_MODE_DEFERRED;
    s->range_records = g_hash_table_size(ch->ranges);
    s->forgotten_tokens = ch->forgotten_tokens;
    s->forgotten_intervals = ch->forgotten_intervals;
    s->gate_cancellations = ch->gate_cancellations;
    if (dev->chameleon_policy_enabled) {
        ChameleonPolicyInfo *p = g_new0(ChameleonPolicyInfo, 1);
        s->policy = p;
        p->lease = ch->lease;
        p->reclaim_allowed = !ch->lease || ch->reclaim_allowed;
        p->last_epoch = ch->last_epoch;
        p->total_bytes = get_current_ram_size();
        p->hard_reclaimed_bytes = qatomic_read(&dev->actual_huge) * LL_HUGE_SIZE;
        p->retired_bytes = ch->retired_pages * LL_PAGE_SIZE;
        p->local_bytes = p->total_bytes - p->hard_reclaimed_bytes - p->retired_bytes;
        p->soft_reclaimed_bytes = soft_bytes;
        p->free_reclaim_ops = ch->free_reclaim_ops;
        p->free_return_ops = ch->free_return_ops;
        p->free_reclaimed_bytes = ch->free_reclaimed_bytes;
        p->free_returned_bytes = ch->free_returned_bytes;
        p->denials = ch->policy_denials;
        p->auto_skips = ch->auto_skips;
    }
    s->session = ch->session;
    s->pool_pages = ch->pool_pages;
    s->registered_pages = ch->charged_pages;
    s->ready_pages = ch_ready_pages(ch);
    s->retired_pages = ch->retired_pages;
    s->error_pages = ch->error_pages;
    s->batch_pages = dev->chameleon_batch_pages;
    s->watermark_bytes = dev->chameleon_watermark_bytes;
    s->batch_triggers = ch->batch_triggers;
    s->watermark_triggers = ch->watermark_triggers;
    s->finalize_acks = ch->finalize_acks;
    s->discarded_pages = ch->discarded_pages;
    s->installed_pages = ch->installed_pages;
    s->install_failures = ch->install_failures;
    s->vfio_coordinated = dev->chameleon_vfio;
    if (!ch_host_stats(&ks)) {
        s->begin_batches = ks.begin_batches;
        s->begin_flushes = ks.begin_flushes;
        s->blocked_pages = ks.blocked_pages;
        s->retired_faults = ks.retired_faults;
        s->hugepage_downgrades = ks.hugepage_downgrades;
        s->remote_tlb_flush_requests = ks.remote_tlb_flush_requests;
        s->remote_tlb_flush = ks.remote_tlb_flush;
        s->host_available_pages = ks.host_available_pages;
    }
    g_hash_table_iter_init(&iter, ch->ranges);
    while (g_hash_table_iter_next(&iter, NULL, &value)) {
        ChRange *r = value;
        ChameleonRangeInfoList *node = g_new0(ChameleonRangeInfoList, 1);
        ChameleonRangeInfo *entry = g_new0(ChameleonRangeInfo, 1);
        node->value = entry;
        node->next = s->ranges;
        s->ranges = node;
        entry->token = r->token; entry->gpa = r->gpa; entry->pages = r->pages;
        entry->order = r->order; entry->state = r->state; entry->status = r->status;
        entry->flags = r->flags;
        entry->transaction = r->transaction;
        entry->no_huge = r->no_huge;
        entry->dma_unmapped = r->dma_unmapped;
        entry->host_installed = r->host_installed;
        entry->residency_status = ch_resident(r, &entry->resident_pages);
    }
    info->actual = get_current_ram_size() - qatomic_read(&dev->actual_huge) * LL_HUGE_SIZE -
                   ch->retired_pages * LL_PAGE_SIZE;
    qemu_mutex_unlock(&ch->lock);
    qemu_mutex_unlock(&dev->legacy_lock);
}
