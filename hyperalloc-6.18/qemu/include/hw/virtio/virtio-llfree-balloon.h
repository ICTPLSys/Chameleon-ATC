/*
 * Virtio Support
 *
 * Copyright IBM, Corp. 2007-2008
 *
 * Authors:
 *  Anthony Liguori   <aliguori@us.ibm.com>
 *  Rusty Russell     <rusty@rustcorp.com.au>
 *
 * This work is licensed under the terms of the GNU GPL, version 2.  See
 * the COPYING file in the top-level directory.
 *
 */

#ifndef QEMU_VIRTIO_LLFREE_BALLOON_H
#define QEMU_VIRTIO_LLFREE_BALLOON_H

#include <stdint.h>

#include "qemu/typedefs.h"
#include "standard-headers/linux/virtio_llfree_balloon.h"
#include "hw/virtio/virtio.h"
#include "qapi-builtin-types.h"
#include "qemu/queue.h"
#include "sysemu/iothread.h"
#include "qom/object.h"
#include "qapi/qapi-types-virtio.h"
#include "llfree_zone.h"

#define TYPE_VIRTIO_LLFREE_BALLOON "virtio-llfree-balloon-device"
OBJECT_DECLARE_SIMPLE_TYPE(VirtIOLLFreeBalloon, VIRTIO_LLFREE_BALLOON)
typedef struct LLChameleon LLChameleon;

/*-----------------------------------------------------------------------------------------------
| Structs
-------------------------------------------------------------------------------------------------*/
struct VirtIOLLFreeBalloon {
    VirtIODevice parent_obj;
    VirtQueue *llfree_info_vq;
    VirtQueue *llfree_request_vq;
    VirtQueue **llfree_auto_deflate_vqs;
    uint32_t num_queues;
    IOThreadVirtQueueMappingList *iothread_vq_mapping_list;
    AioContext **vq_aio_context;
    IOThread *auto_mode_iothread;
    QEMUTimer *retry_llfree_timer;
    QEMUTimer *auto_mode_timer;
    ll_nodes_t *nodes;

    /* listeners to notify on plug/unplug activity. */
    QLIST_HEAD(, RamDiscardListener) rdl_list;

    // Size of the balloon, higher means less memory for the guest
    uint64_t target_huge;
    uint64_t actual_huge;

    uint32_t shrink_pagecache;
    uint32_t pagecache_retries;
    uint32_t host_features;
    /* Explicit fault injection for install error-path regression tests. */
    uint32_t test_install_fail_count;
    bool chameleon_enabled;
    bool chameleon_vfio;
    bool chameleon_policy_enabled;
    char *chameleon_ept_mode;
    QemuMutex legacy_lock;
    uint64_t chameleon_batch_pages;
    uint64_t chameleon_watermark_bytes;
    uint32_t chameleon_install_fail_count;
    uint32_t chameleon_begin_fail_count;
    uint32_t chameleon_dma_map_fail_count;
    int32_t chameleon_discard_fail_index;
    LLChameleon *chameleon;
    bool qemu_4_0_config_size;
};

#endif
