/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef CHAMELEON_RSWAP_TRANSPORT_H
#define CHAMELEON_RSWAP_TRANSPORT_H
#include <linux/types.h>
struct folio;
int rswap_rdma_init(const char *ip, unsigned int port, unsigned long *pages);
void rswap_rdma_exit(void);
int rswap_rdma_store(unsigned long slot, struct folio *folio);
int rswap_rdma_load(unsigned long slot, struct folio *folio);
#endif
