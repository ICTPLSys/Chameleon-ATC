/* -*- mode: C; mode: folding; fill-column: 70; -*- */
/* Copyright 2010-2011,  Georgia Institute of Technology, USA. */
/* See COPYING for license. */
#include "compat.h"
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <string.h>
#include <math.h>

#include <assert.h>

#include <alloca.h> /* Portable enough... */
#include <fcntl.h>
/* getopt should be in unistd.h */
#include <unistd.h>

#if !defined(__MTA__)
#include <getopt.h>
#endif

#include "graph500.h"
#include "rmat.h"
#include "kronecker.h"
#include "verify.h"
#include "prng.h"
#include "timer.h"
#include "xalloc.h"
#include "options.h"
#include "generator/splittable_mrg.h"
#include "generator/graph_generator.h"
#include "generator/make_graph.h"
#ifdef GRAPH500_CSR_CACHE
#include "graph500-cache.h"
#endif

static int64_t nvtx_scale;

static int64_t bfs_root[NBFS_max];

static double generation_time;
static double construction_time;
static double bfs_time[NBFS_max];
static int64_t bfs_nedge[NBFS_max];

static packed_edge * restrict IJ;
static int64_t nedge;
static int graph_loaded;

#ifdef GRAPH500_CSR_CACHE
struct cache_header {
  char magic[8];
  uint64_t endian, version, scale, edgefactor, seed, nbfs, nedge, edge_bytes, rmat;
  double probabilities[4];
};

static int load_checkpoint(const char *name)
{
  struct cache_header h;
  int k, j, ret = -1;
  FILE *stream = fopen(name, "rb");
  if (!stream) return -1;
  if (cache_io(stream, &h, sizeof(h), 0)) goto out;
  if (memcmp(h.magic, "CHG5CSR1", 8) || h.version != 1 ||
      h.endian != UINT64_C(0x0102030405060708) || h.scale != SCALE ||
      h.edgefactor != edgefactor || h.seed != userseed || h.nbfs != NBFS ||
      h.nedge != nvtx_scale * edgefactor || h.edge_bytes != sizeof(*IJ) ||
      h.rmat != use_RMAT || h.probabilities[0] != A || h.probabilities[1] != B ||
      h.probabilities[2] != C || h.probabilities[3] != D) {
    errno = EINVAL; goto out;
  }
  if (cache_io(stream, bfs_root, NBFS*sizeof(*bfs_root), 0)) goto out;
  for (k = 0; k < NBFS; ++k) {
    if (bfs_root[k] < 0 || bfs_root[k] >= nvtx_scale) { errno = EINVAL; goto out; }
    for (j = 0; j < k; ++j)
      if (bfs_root[k] == bfs_root[j]) { errno = EINVAL; goto out; }
  }
  nedge = h.nedge;
  IJ = xmalloc_large_ext(nedge*sizeof(*IJ));
  if (!IJ || cache_io(stream, IJ, nedge*sizeof(*IJ), 0) ||
      load_csr_graph(stream, nvtx_scale, nedge)) goto out;
  if (fgetc(stream) != EOF || ferror(stream)) { errno = EINVAL; goto out; }
  graph_loaded = 1;
  ret = 0;
out:
  fclose(stream);
  return ret;
}

static int save_checkpoint(const char *name)
{
  struct cache_header h = {0};
  char *temporary = malloc(strlen(name)+40);
  FILE *stream;
  int fd, ret = -1;
  if (!temporary) return -1;
  snprintf(temporary, strlen(name)+40, "%s.tmp.%ld", name, (long)getpid());
  fd = open(temporary, O_WRONLY|O_CREAT|O_EXCL, 0600);
  if (fd < 0) { free(temporary); return -1; }
  stream = fdopen(fd, "wb");
  if (!stream) { close(fd); unlink(temporary); free(temporary); return -1; }
  memcpy(h.magic, "CHG5CSR1", 8);
  h.endian = UINT64_C(0x0102030405060708); h.version = 1;
  h.scale = SCALE; h.edgefactor = edgefactor; h.seed = userseed;
  h.nbfs = NBFS; h.nedge = nedge; h.edge_bytes = sizeof(*IJ); h.rmat = use_RMAT;
  h.probabilities[0] = A; h.probabilities[1] = B;
  h.probabilities[2] = C; h.probabilities[3] = D;
  if (!cache_io(stream, &h, sizeof(h), 1) &&
      !cache_io(stream, bfs_root, NBFS*sizeof(*bfs_root), 1) &&
      !cache_io(stream, IJ, nedge*sizeof(*IJ), 1) &&
      !save_csr_graph(stream) && !fflush(stream) && !fsync(fd)) ret = 0;
  if (fclose(stream)) ret = -1;
  if (!ret && rename(temporary, name)) ret = -1;
  if (ret) unlink(temporary);
  free(temporary);
  return ret;
}
#endif

static void run_bfs (void);
static void output_results (const int64_t SCALE, int64_t nvtx_scale,
			    int64_t edgefactor,
			    const double A, const double B,
			    const double C, const double D,
			    const double generation_time,
			    const double construction_time,
			    const int NBFS,
			    const double *bfs_time, const int64_t *bfs_nedge);

int
main (int argc, char **argv)
{
  int64_t desired_nedge;
  if (sizeof (int64_t) < 8) {
    fprintf (stderr, "No 64-bit support.\n");
    return EXIT_FAILURE;
  }

  if (argc > 1)
    get_options (argc, argv);
#ifndef GRAPH500_CSR_CACHE
  if (cache_read_name || cache_write_name || prepare_only) {
    fprintf(stderr, "CSR checkpoints require the omp-csr implementation.\n");
    return EXIT_FAILURE;
  }
#endif
  OMP("omp parallel") {
    OMP("omp single")
      printf("OpenMP team size: %d\n", omp_get_num_threads());
  }

  nvtx_scale = ((int64_t)1)<<SCALE;

  init_random ();

  desired_nedge = nvtx_scale * edgefactor;
  /* Catch a few possible overflows. */
  assert (desired_nedge >= nvtx_scale);
  assert (desired_nedge >= edgefactor);

  /*
    If running the benchmark under an architecture simulator, replace
    the following if () {} else {} with a statement pointing IJ
    to wherever the edge list is mapped into the simulator's memory.
  */
#ifdef GRAPH500_CSR_CACHE
  if (cache_read_name) {
    int err;
    if (VERBOSE) fprintf(stderr, "Loading CSR checkpoint... ");
    TIME(construction_time, err = load_checkpoint(cache_read_name));
    if (err) { perror("Cannot load CSR checkpoint"); return EXIT_FAILURE; }
    if (VERBOSE) fprintf(stderr, "done.\n");
  } else
#endif
  if (!dumpname) {
    if (VERBOSE) fprintf (stderr, "Generating edge list...");
    if (use_RMAT) {
      nedge = desired_nedge;
      IJ = xmalloc_large_ext (nedge * sizeof (*IJ));
      TIME(generation_time, rmat_edgelist (IJ, nedge, SCALE, A, B, C));
    } else {
      TIME(generation_time, make_graph (SCALE, desired_nedge, userseed, userseed, &nedge, (packed_edge**)(&IJ)));
    }
    if (VERBOSE) fprintf (stderr, " done.\n");
  } else {
    int fd;
    ssize_t sz;
    if ((fd = open (dumpname, O_RDONLY)) < 0) {
      perror ("Cannot open input graph file");
      return EXIT_FAILURE;
    }
    sz = nedge * sizeof (*IJ);
    if (sz != read (fd, IJ, sz)) {
      perror ("Error reading input graph file");
      return EXIT_FAILURE;
    }
    close (fd);
  }

  run_bfs ();

  xfree_large (IJ);

  if (!prepare_only) output_results (SCALE, nvtx_scale, edgefactor, A, B, C, D,
		  generation_time, construction_time, NBFS, bfs_time, bfs_nedge);

  return EXIT_SUCCESS;
}

void
run_bfs (void)
{
  int * restrict has_adj;
  int m, err;
  int64_t k, nvtx_connected = 0;

  if (!graph_loaded) {
    if (VERBOSE) fprintf (stderr, "Creating graph...");
    TIME(construction_time, err = create_graph_from_edgelist (IJ, nedge));
    if (VERBOSE) fprintf (stderr, "done.\n");
    if (err) {
      fprintf (stderr, "Failure creating graph.\n");
      exit (EXIT_FAILURE);
    }
  }

  /*
    If running the benchmark under an architecture simulator, replace
    the following if () {} else {} with a statement pointing bfs_root
    to wherever the BFS roots are mapped into the simulator's memory.
  */
  if (!graph_loaded) {
  if (!rootname) {
    has_adj = xmalloc_large (nvtx_scale * sizeof (*has_adj));
    OMP("omp parallel") {
      OMP("omp for")
	for (k = 0; k < nvtx_scale; ++k)
	  has_adj[k] = 0;
      MTA("mta assert nodep") OMP("omp for")
	for (k = 0; k < nedge; ++k) {
	  const int64_t i = get_v0_from_edge(&IJ[k]);
	  const int64_t j = get_v1_from_edge(&IJ[k]);
	  if (i != j)
	    has_adj[i] = has_adj[j] = 1;
	}
      OMP("omp for reduction(+:nvtx_connected)")
	for (k = 0; k < nvtx_scale; ++k)
	  if (has_adj[k]) ++nvtx_connected;
    }

    /* Sample from {0, ..., nvtx_scale-1} without replacement, but
       only from vertices with degree > 0. */
    m = 0;
    for (k = 0; k < nvtx_scale && m < NBFS; ++k) {
      unsigned long challenge = (unsigned long)(mrg_get_double_orig(prng_state) * (double)(nvtx_scale-1));

      if (has_adj[challenge]) {
        size_t i=0;
        for (; i<m; i++) if (bfs_root[i] == challenge) break; // check for duplicates
        if (i == m) bfs_root[m++] = challenge; // if not duplicate, add to list
      }
    }
	  
    if (m < NBFS) {
      if (m > 0) {
	fprintf (stderr, "Cannot find %d sample roots of non-self degree > 0, using %d.\n",
		 NBFS, m);
	NBFS = m;
      } else {
	fprintf (stderr, "Cannot find any sample roots of non-self degree > 0.\n");
	exit (EXIT_FAILURE);
      }
    }

    xfree_large (has_adj);
  } else {
    int fd;
    ssize_t sz;
    if ((fd = open (rootname, O_RDONLY)) < 0) {
      perror ("Cannot open input BFS root file");
      exit (EXIT_FAILURE);
    }
    sz = NBFS * sizeof (*bfs_root);
    if (sz != read (fd, bfs_root, sz)) {
      perror ("Error reading input BFS root file");
      exit (EXIT_FAILURE);
    }
    close (fd);
  }
  }

  printf("bfs_roots:");
  for (m = 0; m < NBFS; ++m) printf(" %" PRId64, bfs_root[m]);
  printf("\n");
#ifdef GRAPH500_CSR_CACHE
  if (cache_write_name) {
    if (VERBOSE) fprintf(stderr, "Saving CSR checkpoint... ");
    if (save_checkpoint(cache_write_name)) {
      perror("Cannot save CSR checkpoint"); exit(EXIT_FAILURE);
    }
    if (VERBOSE) fprintf(stderr, "done.\n");
  }
#endif
  if (prepare_only) { destroy_graph(); return; }

  for (m = 0; m < NBFS; ++m) {
    int64_t *bfs_tree, max_bfsvtx;

    /* Re-allocate. Some systems may randomize the addres... */
    bfs_tree = xmalloc_large (nvtx_scale * sizeof (*bfs_tree));
    assert (bfs_root[m] < nvtx_scale);

    if (VERBOSE) fprintf (stderr, "Running bfs %d...", m);
    TIME(bfs_time[m], err = make_bfs_tree (bfs_tree, &max_bfsvtx, bfs_root[m]));
    if (VERBOSE) fprintf (stderr, "done\n");

    if (err) {
      perror ("make_bfs_tree failed");
      abort ();
    }

    if (!getenv("SKIP_VALIDATION")) {
      if (VERBOSE) fprintf (stderr, "Verifying bfs %d...", m);
      bfs_nedge[m] = verify_bfs_tree (bfs_tree, max_bfsvtx, bfs_root[m], IJ, nedge);
      if (VERBOSE) fprintf (stderr, "done\n");
      if (bfs_nedge[m] < 0) {
	fprintf (stderr, "bfs %d from %" PRId64 " failed verification (%" PRId64 ")\n",
		 m, bfs_root[m], bfs_nedge[m]);
	abort ();
      }
    }

    xfree_large (bfs_tree);
  }

  destroy_graph ();
}

#define NSTAT 9
#define PRINT_STATS(lbl, israte)					\
  do {									\
    printf ("min_%s: %20.17e\n", lbl, stats[0]);			\
    printf ("firstquartile_%s: %20.17e\n", lbl, stats[1]);		\
    printf ("median_%s: %20.17e\n", lbl, stats[2]);			\
    printf ("thirdquartile_%s: %20.17e\n", lbl, stats[3]);		\
    printf ("max_%s: %20.17e\n", lbl, stats[4]);			\
    if (!israte) {							\
      printf ("mean_%s: %20.17e\n", lbl, stats[5]);			\
      printf ("stddev_%s: %20.17e\n", lbl, stats[6]);			\
    } else {								\
      printf ("harmonic_mean_%s: %20.17e\n", lbl, stats[7]);		\
      printf ("harmonic_stddev_%s: %20.17e\n", lbl, stats[8]);	\
    }									\
  } while (0)


static int
dcmp (const void *a, const void *b)
{
  const double da = *(const double*)a;
  const double db = *(const double*)b;
  if (da > db) return 1;
  if (db > da) return -1;
  if (da == db) return 0;
  fprintf (stderr, "No NaNs permitted in output.\n");
  abort ();
  return 0;
}

void
statistics (double *out, double *data, int64_t n)
{
  long double s, mean;
  double t;
  int k;

  /* Quartiles */
  qsort (data, n, sizeof (*data), dcmp);
  out[0] = data[0];
  t = (n+1) / 4.0;
  k = (int) t;
  if (t == k)
    out[1] = data[k];
  else
    out[1] = 3*(data[k]/4.0) + data[k+1]/4.0;
  t = (n+1) / 2.0;
  k = (int) t;
  if (t == k)
    out[2] = data[k];
  else
    out[2] = data[k]/2.0 + data[k+1]/2.0;
  t = 3*((n+1) / 4.0);
  k = (int) t;
  if (t == k)
    out[3] = data[k];
  else
    out[3] = data[k]/4.0 + 3*(data[k+1]/4.0);
  out[4] = data[n-1];

  s = data[n-1];
  for (k = n-1; k > 0; --k)
    s += data[k-1];
  mean = s/n;
  out[5] = mean;
  s = data[n-1] - mean;
  s *= s;
  for (k = n-1; k > 0; --k) {
    long double tmp = data[k-1] - mean;
    s += tmp * tmp;
  }
  out[6] = sqrt (s/(n-1));

  s = (data[0]? 1.0L/data[0] : 0);
  for (k = 1; k < n; ++k)
    s += (data[k]? 1.0L/data[k] : 0);
  out[7] = n/s;
  mean = s/n;

  /*
    Nilan Norris, The Standard Errors of the Geometric and Harmonic
    Means and Their Application to Index Numbers, 1940.
    http://www.jstor.org/stable/2235723
  */
  s = (data[0]? 1.0L/data[0] : 0) - mean;
  s *= s;
  for (k = 1; k < n; ++k) {
    long double tmp = (data[k]? 1.0L/data[k] : 0) - mean;
    s += tmp * tmp;
  }
  s = (sqrt (s)/(n-1)) * out[7] * out[7];
  out[8] = s;
}

void
output_results (const int64_t SCALE, int64_t nvtx_scale, int64_t edgefactor,
		const double A, const double B, const double C, const double D,
		const double generation_time,
		const double construction_time,
		const int NBFS, const double *bfs_time, const int64_t *bfs_nedge)
{
  int k;
  int64_t sz;
  double *tm;
  double *stats;

  tm = alloca (NBFS * sizeof (*tm));
  stats = alloca (NSTAT * sizeof (*stats));
  if (!tm || !stats) {
    perror ("Error allocating within final statistics calculation.");
    abort ();
  }

  sz = (1L << SCALE) * edgefactor * 2 * sizeof (int64_t);
  printf ("SCALE: %" PRId64 "\nnvtx: %" PRId64 "\nedgefactor: %" PRId64 "\n"
	  "terasize: %20.17e\n",
	  SCALE, nvtx_scale, edgefactor, sz/1.0e12);
  printf ("A: %20.17e\nB: %20.17e\nC: %20.17e\nD: %20.17e\n", A, B, C, D);
  printf ("generation_time: %20.17e\n", generation_time);
  printf ("construction_time: %20.17e\n", construction_time);
  printf ("nbfs: %d\n", NBFS);

  memcpy (tm, bfs_time, NBFS*sizeof(tm[0]));
  statistics (stats, tm, NBFS);
  PRINT_STATS("time", 0);

  for (k = 0; k < NBFS; ++k)
    tm[k] = bfs_nedge[k];
  statistics (stats, tm, NBFS);
  PRINT_STATS("nedge", 0);

  for (k = 0; k < NBFS; ++k)
    tm[k] = bfs_nedge[k] / bfs_time[k];
  statistics (stats, tm, NBFS);
  PRINT_STATS("TEPS", 1);
}
