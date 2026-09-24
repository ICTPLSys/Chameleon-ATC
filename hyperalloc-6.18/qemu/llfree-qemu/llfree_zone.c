#include "llfree_zone.h"
#include "exec/hwaddr.h"
#include "llfree.h"
#include "llfree_inner.h"
#include "llfree_platform.h"
#include "qemu/error-report.h"
#include "qemu/thread.h"
#include "qemu/typedefs.h"
#include "qemu/error-report.h"
#include "hw/mem/pc-dimm.h"
#include "exec/address-spaces.h"

// shared with virtio-driver
typedef struct llfree_zone_info {
    llfree_meta_t llfree_meta;
    uint32_t start_pfn;
    uint32_t pages;
    uint32_t type;
    uint32_t node;
    _Atomic(int64_t) *free_pages;
    _Atomic(int64_t) *file_pages;
} llfree_zone_info_t;

// Struct used by qemu
struct ll_zone {
    llfree_t llfree;
    uint64_t start_pfn;
    ll_zone_type_t type;
    size_t node;
    _Atomic(int64_t) *free_pages;
    _Atomic(int64_t) *file_pages;
    ll_reclaim_states_t *states;
    QemuMutex lock;
};

typedef struct ll_node {
    ll_zone_t *zones[LL_ZONE_LEN];
} ll_node_t;

struct ll_nodes {
    ll_node_t nodes[LL_NODES_MAX];
    size_t nodes_len;
};

static void *gpa_to_hva(hwaddr gpa, uint64_t size)
{
    MemoryRegionSection section =
        memory_region_find(get_system_memory(), gpa, size);
    void *addr =
        memory_region_get_ram_ptr(section.mr) + section.offset_within_region;
    return addr;
}

static void writer(void *x, char *s) { info_report("%s", s); }

static ll_zone_t *ll_zone_create(void *buffer)
{
    ll_zone_t *zone = g_malloc(sizeof(ll_zone_t));
    g_assert(zone != NULL);

    llfree_zone_info_t *info = buffer;

    zone->start_pfn = info->start_pfn;
    zone->node = info->node;
    zone->type = info->type;

    zone->free_pages = (_Atomic(int64_t) *)gpa_to_hva((hwaddr)info->free_pages,
                                                      sizeof(_Atomic(int64_t)));
    g_assert(zone->free_pages != NULL);
    zone->file_pages = (_Atomic(int64_t) *)gpa_to_hva((hwaddr)info->file_pages,
                                                      sizeof(_Atomic(int64_t)));
    g_assert(zone->file_pages != NULL);

    llfree_meta_size_t sizes = llfree_metadata_size(1, info->pages);
    llfree_meta_t meta = {
        .local = aligned_alloc(LLFREE_CACHE_SIZE, sizeof(local_t)),
        .trees = gpa_to_hva((hwaddr)info->llfree_meta.trees, sizes.trees),
        .lower = gpa_to_hva((hwaddr)info->llfree_meta.lower, sizes.lower),
    };
    llfree_result_t res =
        llfree_init(&zone->llfree, 1, info->pages, LLFREE_INIT_NONE, meta);

    g_assert(llfree_is_ok(res));
    llfree_print_debug(&zone->llfree, writer, NULL);

    info_report("setup zone offset=%zu pages=%u", zone->start_pfn, info->pages);

    zone->states = ll_reclaim_states_create(llfree_frames(&zone->llfree));

    qemu_mutex_init(&zone->lock);
    return zone;
}

static void ll_zone_destroy(ll_zone_t *zone)
{
    g_assert(zone != NULL);

    ll_reclaim_states_destroy(zone->states);
    g_free(zone);
}

ll_nodes_t *ll_nodes_create(void)
{
    ll_nodes_t *self = g_malloc(sizeof(ll_nodes_t));
    self->nodes_len = 0;
    return self;
}

void ll_nodes_destroy(ll_nodes_t *self)
{
    g_assert(self != NULL);
    for (size_t node = 0; node < self->nodes_len; node++) {
        for (ll_zone_type_t zone = 0; zone < LL_ZONE_LEN; zone++) {
            ll_zone_t *z = ll_nodes_get_zone(self, node, zone);
            if (z != NULL)
                ll_zone_destroy(z);
        }
    }
    g_free(self);
}

size_t ll_nodes_len(ll_nodes_t *self) { return self->nodes_len; }

void ll_nodes_add_zone(ll_nodes_t *self, void *buffer)
{
    ll_zone_t *zone = ll_zone_create(buffer);
    g_assert(zone != NULL);
    g_assert(0 <= zone->type && zone->type < LL_ZONE_LEN);

    if (zone->node >= self->nodes_len) {
        g_assert(zone->node < LL_NODES_MAX);
        // Initializing new nodes
        for (size_t i = self->nodes_len; i <= zone->node; i++) {
            for (ll_zone_type_t zid = 0; zid < LL_ZONE_LEN; zid++) {
                self->nodes[i].zones[zid] = NULL;
            }
        }
        self->nodes_len = zone->node + 1;
    }

    ll_zone_t **z = &(self->nodes[zone->node].zones[zone->type]);
    if (*z != NULL)
        error_report("init zone %zu, %d twice", zone->node, zone->type);
    *z = zone;
}

ll_zone_t *ll_nodes_get_zone(ll_nodes_t *self, size_t node, ll_zone_type_t zone)
{
    g_assert(zone < LL_ZONE_LEN);
    if (node < self->nodes_len) {
        return self->nodes[node].zones[zone];
    }
    return NULL;
}

llfree_result_t ll_zone_reclaim(ll_zone_t *zone, bool hard)
{
    g_assert(zone != NULL);
    llfree_result_t res = llfree_reclaim(&zone->llfree, 0, hard);
    if (hard && llfree_is_ok(res)) {
        atomic_fetch_sub(zone->free_pages, 1 << LLFREE_HUGE_ORDER);
    }
    return res;
}

llfree_result_t ll_zone_return(ll_zone_t *zone, uint64_t frame)
{
    g_assert(zone != NULL);
    llfree_result_t res = llfree_return(&zone->llfree, frame);
    if (llfree_is_ok(res)) {
        atomic_fetch_add(zone->free_pages, 1 << LLFREE_HUGE_ORDER);
    }
    return res;
}

llfree_result_t ll_zone_install(ll_zone_t *zone, uint64_t frame)
{
    g_assert(zone != NULL);
    return llfree_install(&zone->llfree, frame);
}

bool ll_zone_is_evicted(ll_zone_t *zone, uint64_t frame)
{
    g_assert(zone != NULL);
    return llfree_is_reclaimed(&zone->llfree, frame);
}

int64_t ll_zone_free_huge(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    return llfree_free_huge(&zone->llfree);
    // return *zone->info.free_pages >> HUGE_PAGE_ORDER;
}

uint64_t ll_zone_frames(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    return llfree_frames(&zone->llfree);
}

uint32_t ll_zone_node(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    return zone->node;
}

uint32_t ll_zone_type(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    return zone->type;
}

ll_reclaim_states_t *ll_zone_states(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    return zone->states;
}

uint32_t ll_zone_buffer_size(void) { return sizeof(llfree_zone_info_t); }

int64_t ll_zone_file_pages(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    return atomic_load(zone->file_pages);
}

uint64_t ll_zone_start_frame(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    // Align to the max allocation size
    return align_down(zone->start_pfn, 1 << LLFREE_MAX_ORDER);
}

void ll_zone_lock(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    qemu_mutex_lock(&zone->lock);
}

void ll_zone_unlock(ll_zone_t *zone)
{
    g_assert(zone != NULL);
    qemu_mutex_unlock(&zone->lock);
}
