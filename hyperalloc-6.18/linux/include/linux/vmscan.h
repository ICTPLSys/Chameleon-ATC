#ifndef _LINUX_LLFREE_VMSCAN_H
#define _LINUX_LLFREE_VMSCAN_H
#include <linux/types.h>
unsigned long shrink_pagecache_for_reclaim(u32 node, u32 nr_to_reclaim);
#endif
