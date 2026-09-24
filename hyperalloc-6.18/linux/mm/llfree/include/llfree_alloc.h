#ifndef _LLFREE
#define _LLFREE

#ifdef CONFIG_LLFREE

#include <linux/init.h>
#include <llfree.h>
#ifndef LLFREE_MAX_ORDER
#define LLFREE_MAX_ORDER 10U
#endif

/// Create a new allocator instance for the given node
llfree_t *__init llfree_node_init(size_t node, size_t cores, size_t start_pfn,
			   size_t pages);

#endif // CONFIG_LLFREE
#endif // _LLFREE
