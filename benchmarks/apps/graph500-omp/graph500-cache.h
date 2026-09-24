#ifndef GRAPH500_CACHE_H
#define GRAPH500_CACHE_H
#include <stdio.h>
#include <errno.h>
#include <stdint.h>

/* Native-endian, versioned local checkpoint; no pointers are serialized. */
static inline int cache_io(FILE *stream, void *buffer, uint64_t bytes, int writing)
{
  unsigned char *p = buffer;
  while (bytes) {
    size_t chunk = bytes > 64 * 1024 * 1024 ? 64 * 1024 * 1024 : (size_t)bytes;
    size_t done = writing ? fwrite(p, 1, chunk, stream) : fread(p, 1, chunk, stream);
    if (done != chunk) { errno = EIO; return -1; }
    p += done;
    bytes -= done;
  }
  return 0;
}

int save_csr_graph(FILE *stream);
int load_csr_graph(FILE *stream, int64_t vertex_limit, int64_t edge_limit);
#endif
