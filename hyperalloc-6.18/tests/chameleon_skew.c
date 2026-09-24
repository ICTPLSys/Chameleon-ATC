// SPDX-License-Identifier: GPL-2.0-only
/* Deterministic skewed anonymous-memory workload, independent of debugfs.
 *
 * After parallel initialization, wait for stdin commands:
 *   warmup [seconds] | run [seconds] | shift [percent] | verify | quit
 *   pattern contiguous|striped|uniform|sequential | hot-access-ppm VALUE
 * SKEW_READY / SKEW_DONE / SKEW_SHIFT / SKEW_VERIFIED delimit phases.
 * JSON progress records contain totals across all workers. --ops-per-thread
 * replaces timed stopping with a fixed sequence length for reproducibility.
 * EOF and quit perform a full verification if the last phase changed data.
 *
 * Each 64-byte line stores a version and seven position/version-specific
 * hash words. Workers own disjoint partitions. A pair of independent 64-bit
 * aggregate sums is updated on every write and checked by a final full read:
 * a stale, internally consistent old line cannot silently pass verification.
 * This is a corruption detector, not a cryptographic integrity mechanism.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#define PAGE_BYTES 4096ULL
#define LINE_BYTES 64ULL
#define WORDS_PER_LINE 8U
#define LINES_PER_PAGE (PAGE_BYTES / LINE_BYTES)
#define ALIGN_BYTES (2ULL << 20)
#define STRIPE_PAGES (ALIGN_BYTES / PAGE_BYTES)
#define PUBLISH_LINES 4096ULL
#define MAX_THREADS 256U

enum phase_kind { PH_INIT, PH_WARMUP, PH_RUN, PH_VERIFY, PH_QUIT };
enum access_pattern { PAT_CONTIGUOUS, PAT_STRIPED, PAT_UNIFORM, PAT_SEQUENTIAL };

struct metrics {
    uint64_t touches, reads, writes, init_bytes, verify_bytes, errors;
    uint64_t major_faults, minor_faults;
};

struct worker {
    pthread_t thread;
    unsigned id;
    uint64_t *memory;
    uint64_t first_line, pages, hot_pages, hot_start, random, sequential_page;
    uint64_t ledger_a, ledger_b;
    _Atomic uint64_t touches, reads, writes, init_bytes, verify_bytes, errors;
    _Atomic uint64_t major_faults, minor_faults;
};

static struct {
    uint64_t bytes, seed, ops_per_thread;
    unsigned threads, hot_access_ppm, write_percent, stripe_hot_pages;
    enum access_pattern pattern;
    double seconds, hot_percent, shift_percent;
    int advice;
    enum phase_kind phase;
    atomic_bool stop;
    atomic_uint done;
    pthread_barrier_t begin, end;
    struct worker *workers;
    uint64_t *memory;
    struct metrics total;
    unsigned command_errors;
} bench = {
    .bytes = 16ULL << 30, .seed = 1, .threads = 4, .seconds = 30,
    .hot_percent = 1, .hot_access_ppm = 990000, .write_percent = 10,
    .pattern = PAT_CONTIGUOUS, .stripe_hot_pages = 8,
    .advice = MADV_HUGEPAGE,
};

static void fatal(const char *what)
{
    fprintf(stderr, "SKEW_FATAL %s errno=%d (%s)\n", what, errno, strerror(errno));
    exit(1);
}

static double now_seconds(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) fatal("clock_gettime");
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static uint64_t mix64(uint64_t value)
{
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

static uint64_t random_next(struct worker *worker)
{
    worker->random += UINT64_C(0x9e3779b97f4a7c15);
    return mix64(worker->random);
}

static const char *pattern_name(enum access_pattern pattern)
{
    static const char *const names[] = {"contiguous", "striped", "uniform", "sequential"};
    return names[pattern];
}

static bool parse_pattern(const char *text, enum access_pattern *out)
{
    for (unsigned i = PAT_CONTIGUOUS; i <= PAT_SEQUENTIAL; i++) {
        if (!strcmp(text, pattern_name((enum access_pattern)i))) {
            *out = (enum access_pattern)i;
            return true;
        }
    }
    return false;
}

static bool pattern_supported(enum access_pattern pattern)
{
    return pattern != PAT_STRIPED || !(bench.bytes % (ALIGN_BYTES * bench.threads));
}

static uint64_t select_page(struct worker *worker)
{
    if (bench.pattern == PAT_SEQUENTIAL) {
        uint64_t page = worker->sequential_page++;
        if (worker->sequential_page == worker->pages) worker->sequential_page = 0;
        return page;
    }
    if (bench.pattern == PAT_UNIFORM)
        return random_next(worker) % worker->pages;
    if (random_next(worker) % 1000000 < bench.hot_access_ppm) {
        if (bench.pattern == PAT_STRIPED) {
            /* Uniformly choose from the hot pages in all 2 MiB stripes. */
            uint64_t hot = random_next(worker) % worker->hot_pages;
            uint64_t stripe = hot / bench.stripe_hot_pages;
            uint64_t within = (worker->hot_start + hot % bench.stripe_hot_pages) % STRIPE_PAGES;
            return stripe * STRIPE_PAGES + within;
        }
        uint64_t page = worker->hot_start + random_next(worker) % worker->hot_pages;
        if (page >= worker->pages) page -= worker->pages;
        return page;
    }
    return random_next(worker) % worker->pages;
}

static uint64_t line_key(uint64_t line, uint64_t version)
{
    return mix64(bench.seed ^ mix64(line + UINT64_C(0xd1b54a32d192ed03)) ^
                 mix64(version + UINT64_C(0x8cb92baa3f3d8dd7)));
}

static uint64_t word_value(uint64_t key, unsigned word)
{
    return mix64(key + word * UINT64_C(0x9e3779b97f4a7c15));
}

static uint64_t digest_a(uint64_t line, uint64_t version)
{
    return mix64(line_key(line, version) ^ UINT64_C(0xa0761d6478bd642f));
}

static uint64_t digest_b(uint64_t line, uint64_t version)
{
    return mix64(bench.seed + line * UINT64_C(0xe7037ed1a0b428db) +
                 mix64(version ^ UINT64_C(0x589965cc75374cc3)));
}

static void fill_line(uint64_t *address, uint64_t line, uint64_t version)
{
    uint64_t key = line_key(line, version);
    address[0] = version;
    for (unsigned word = 1; word < WORDS_PER_LINE; word++)
        address[word] = word_value(key, word);
}

static bool check_line(struct worker *worker, uint64_t offset, uint64_t *version,
                       struct metrics *metrics)
{
    const volatile uint64_t *address = worker->memory + offset * WORDS_PER_LINE;
    uint64_t line = worker->first_line + offset;
    uint64_t values[WORDS_PER_LINE];
    for (unsigned word = 0; word < WORDS_PER_LINE; word++)
        values[word] = address[word];
    *version = values[0];
    uint64_t key = line_key(line, *version);
    for (unsigned word = 1; word < WORDS_PER_LINE; word++) {
        uint64_t expected = word_value(key, word);
        if (values[word] != expected) {
            if (metrics->errors < 8)
                fprintf(stderr, "SKEW_CORRUPTION thread=%u line=%" PRIu64
                        " address=%p version=%" PRIu64 " word=%u"
                        " expected=0x%016" PRIx64 " actual=0x%016" PRIx64 "\n",
                        worker->id, line, (const void *)address, *version,
                        word, expected, values[word]);
            metrics->errors++;
            return false;
        }
    }
    return true;
}

static void publish(struct worker *worker, struct metrics *metrics,
                    const struct rusage *baseline)
{
    struct rusage usage;
    if (getrusage(RUSAGE_THREAD, &usage)) fatal("worker getrusage");
    metrics->major_faults = (uint64_t)(usage.ru_majflt - baseline->ru_majflt);
    metrics->minor_faults = (uint64_t)(usage.ru_minflt - baseline->ru_minflt);
#define PUT(name) atomic_store_explicit(&worker->name, metrics->name, memory_order_relaxed)
    PUT(touches); PUT(reads); PUT(writes); PUT(init_bytes); PUT(verify_bytes);
    PUT(errors); PUT(major_faults); PUT(minor_faults);
#undef PUT
}

static void initialize_worker(struct worker *worker, struct metrics *metrics,
                              const struct rusage *baseline)
{
    uint64_t count = worker->pages * LINES_PER_PAGE;
    worker->ledger_a = worker->ledger_b = 0;
    for (uint64_t offset = 0; offset < count; offset++) {
        uint64_t line = worker->first_line + offset;
        fill_line(worker->memory + offset * WORDS_PER_LINE, line, 0);
        worker->ledger_a += digest_a(line, 0);
        worker->ledger_b += digest_b(line, 0);
        metrics->init_bytes += LINE_BYTES;
        if (!(offset % PUBLISH_LINES)) publish(worker, metrics, baseline);
    }
}

static void run_worker(struct worker *worker, struct metrics *metrics,
                       const struct rusage *baseline)
{
    while (!atomic_load_explicit(&bench.stop, memory_order_relaxed) &&
           (!bench.ops_per_thread || metrics->touches < bench.ops_per_thread)) {
        uint64_t page = select_page(worker);
        uint64_t offset = page * LINES_PER_PAGE + random_next(worker) % LINES_PER_PAGE;
        uint64_t version;
        bool writing = random_next(worker) % 100 < bench.write_percent;
        metrics->touches++;
        if (!check_line(worker, offset, &version, metrics)) {
            atomic_store_explicit(&bench.stop, true, memory_order_relaxed);
            break;
        }
        if (writing) {
            uint64_t line = worker->first_line + offset;
            uint64_t updated = version + 1;
            fill_line(worker->memory + offset * WORDS_PER_LINE, line, updated);
            uint64_t observed;
            if (!check_line(worker, offset, &observed, metrics) || observed != updated) {
                if (observed != updated) metrics->errors++;
                atomic_store_explicit(&bench.stop, true, memory_order_relaxed);
                break;
            }
            worker->ledger_a += digest_a(line, updated) - digest_a(line, version);
            worker->ledger_b += digest_b(line, updated) - digest_b(line, version);
            metrics->writes++;
        } else {
            metrics->reads++;
        }
        if (!(metrics->touches % PUBLISH_LINES)) publish(worker, metrics, baseline);
    }
}

static void verify_worker(struct worker *worker, struct metrics *metrics,
                          const struct rusage *baseline)
{
    uint64_t count = worker->pages * LINES_PER_PAGE, sum_a = 0, sum_b = 0;
    for (uint64_t offset = 0; offset < count; offset++) {
        uint64_t version;
        (void)check_line(worker, offset, &version, metrics);
        uint64_t line = worker->first_line + offset;
        sum_a += digest_a(line, version);
        sum_b += digest_b(line, version);
        metrics->verify_bytes += LINE_BYTES;
        if (!(offset % PUBLISH_LINES)) publish(worker, metrics, baseline);
    }
    if (sum_a != worker->ledger_a || sum_b != worker->ledger_b) {
        fprintf(stderr, "SKEW_LEDGER_MISMATCH thread=%u"
                " expected_a=0x%016" PRIx64 " actual_a=0x%016" PRIx64
                " expected_b=0x%016" PRIx64 " actual_b=0x%016" PRIx64 "\n",
                worker->id, worker->ledger_a, sum_a, worker->ledger_b, sum_b);
        metrics->errors++;
    }
}

static void *worker_main(void *argument)
{
    struct worker *worker = argument;
    for (;;) {
        pthread_barrier_wait(&bench.begin);
        if (bench.phase == PH_QUIT) return NULL;
        struct metrics metrics = {0};
        struct rusage baseline;
        if (getrusage(RUSAGE_THREAD, &baseline)) fatal("worker baseline getrusage");
        if (bench.phase == PH_INIT) initialize_worker(worker, &metrics, &baseline);
        else if (bench.phase == PH_VERIFY) verify_worker(worker, &metrics, &baseline);
        else run_worker(worker, &metrics, &baseline);
        publish(worker, &metrics, &baseline);
        atomic_fetch_add_explicit(&bench.done, 1, memory_order_release);
        pthread_barrier_wait(&bench.end);
    }
}

static struct metrics worker_metrics(const struct worker *worker)
{
    struct metrics metrics;
#define GET(name) metrics.name = atomic_load_explicit(&worker->name, memory_order_relaxed)
    GET(touches); GET(reads); GET(writes); GET(init_bytes); GET(verify_bytes);
    GET(errors); GET(major_faults); GET(minor_faults);
#undef GET
    return metrics;
}

static void metrics_add(struct metrics *to, const struct metrics *from)
{
#define ADD(name) to->name += from->name
    ADD(touches); ADD(reads); ADD(writes); ADD(init_bytes); ADD(verify_bytes);
    ADD(errors); ADD(major_faults); ADD(minor_faults);
#undef ADD
}

static struct metrics all_metrics(void)
{
    struct metrics total = {0};
    for (unsigned i = 0; i < bench.threads; i++) {
        struct metrics one = worker_metrics(&bench.workers[i]);
        metrics_add(&total, &one);
    }
    return total;
}

static const char *phase_name(enum phase_kind phase)
{
    static const char *const names[] = {"init", "warmup", "run", "verify", "quit"};
    return names[phase];
}

static void print_metrics(const char *event, enum phase_kind phase,
                          const struct metrics *metrics, double elapsed)
{
    uint64_t read_bytes = (metrics->reads + metrics->writes * 2) * LINE_BYTES;
    uint64_t write_bytes = metrics->writes * LINE_BYTES;
    double rate = elapsed > 0 ? (double)metrics->touches / elapsed : 0;
    double memory_rate = elapsed > 0 ?
        (double)(read_bytes + write_bytes + metrics->init_bytes + metrics->verify_bytes) /
        elapsed / (double)(1ULL << 30) : 0;
    printf("{\"event\":\"%s\",\"phase\":\"%s\",\"elapsed_seconds\":%.6f,"
           "\"touches\":%" PRIu64 ",\"reads\":%" PRIu64 ",\"writes\":%" PRIu64 ","
           "\"read_bytes\":%" PRIu64 ",\"write_bytes\":%" PRIu64 ","
           "\"init_bytes\":%" PRIu64 ",\"verify_bytes\":%" PRIu64 ","
           "\"verify_errors\":%" PRIu64 ",\"major_faults\":%" PRIu64 ","
           "\"minor_faults\":%" PRIu64 ",\"ops_per_second\":%.3f,"
           "\"logical_GiB_per_second\":%.6f,\"pattern\":\"%s\","
           "\"stripe_bytes\":%" PRIu64 ",\"stripe_hot_pages\":%u,"
           "\"hot_access_ppm\":%u,\"shift_percent\":%.6f}\n",
           event, phase_name(phase), elapsed, metrics->touches, metrics->reads,
           metrics->writes, read_bytes, write_bytes, metrics->init_bytes,
           metrics->verify_bytes, metrics->errors, metrics->major_faults,
           metrics->minor_faults, rate, memory_rate, pattern_name(bench.pattern),
           (uint64_t)ALIGN_BYTES, bench.stripe_hot_pages, bench.hot_access_ppm,
           bench.shift_percent);
}

static struct metrics run_phase(enum phase_kind phase, double seconds)
{
    for (unsigned i = 0; i < bench.threads; i++) {
        struct worker *worker = &bench.workers[i];
#define CLEAR(name) atomic_store_explicit(&worker->name, 0, memory_order_relaxed)
        CLEAR(touches); CLEAR(reads); CLEAR(writes); CLEAR(init_bytes); CLEAR(verify_bytes);
        CLEAR(errors); CLEAR(major_faults); CLEAR(minor_faults);
#undef CLEAR
    }
    bench.phase = phase;
    atomic_store_explicit(&bench.stop, false, memory_order_relaxed);
    atomic_store_explicit(&bench.done, 0, memory_order_relaxed);
    double started = now_seconds(), next_progress = started + 1;
    bool timed = (phase == PH_RUN || phase == PH_WARMUP) && !bench.ops_per_thread;
    printf("SKEW_PHASE phase=%s seconds=%.6f ops_per_thread=%" PRIu64
           " pattern=%s stripe_hot_pages=%u hot_access_ppm=%u shift_percent=%.6f\n",
           phase_name(phase), seconds, bench.ops_per_thread, pattern_name(bench.pattern),
           bench.stripe_hot_pages, bench.hot_access_ppm, bench.shift_percent);
    pthread_barrier_wait(&bench.begin);
    while (atomic_load_explicit(&bench.done, memory_order_acquire) < bench.threads) {
        double now = now_seconds();
        if (timed && now - started >= seconds)
            atomic_store_explicit(&bench.stop, true, memory_order_relaxed);
        if (now >= next_progress) {
            struct metrics current = all_metrics();
            print_metrics("progress", phase, &current, now - started);
            next_progress = now + 1;
        }
        struct timespec pause = {.tv_sec = 0, .tv_nsec = 10000000};
        while (nanosleep(&pause, &pause) && errno == EINTR) {}
    }
    pthread_barrier_wait(&bench.end);
    struct metrics final = all_metrics();
    double elapsed = now_seconds() - started;
    metrics_add(&bench.total, &final);
    print_metrics("complete", phase, &final, elapsed);
    for (unsigned i = 0; i < bench.threads; i++) {
        struct worker *worker = &bench.workers[i];
        struct metrics one = worker_metrics(worker);
        printf("SKEW_THREAD phase=%s thread=%u touches=%" PRIu64 " reads=%" PRIu64
               " writes=%" PRIu64 " init_bytes=%" PRIu64 " verify_bytes=%" PRIu64
               " verify_errors=%" PRIu64 " major_faults=%" PRIu64 " minor_faults=%" PRIu64
               " ledger_a=0x%016" PRIx64 " ledger_b=0x%016" PRIx64 "\n",
               phase_name(phase), i, one.touches, one.reads, one.writes,
               one.init_bytes, one.verify_bytes, one.errors, one.major_faults,
               one.minor_faults, worker->ledger_a, worker->ledger_b);
    }
    if (phase == PH_VERIFY)
        printf("SKEW_VERIFIED status=%s bytes=%" PRIu64 " errors=%" PRIu64
               " cumulative_errors=%" PRIu64 " major_faults=%" PRIu64 "\n",
               bench.total.errors ? "FAIL" : "PASS", final.verify_bytes,
               final.errors, bench.total.errors, final.major_faults);
    else
        printf("SKEW_DONE phase=%s status=%s touches=%" PRIu64 " errors=%" PRIu64
               " major_faults=%" PRIu64 " elapsed_seconds=%.6f pattern=%s\n",
               phase_name(phase), final.errors ? "FAIL" : "PASS", final.touches,
               final.errors, final.major_faults, elapsed, pattern_name(bench.pattern));
    return final;
}

static bool parse_u64(const char *text, uint64_t *out)
{
    char *end;
    if (!text[0] || text[0] == '-') return false;
    errno = 0;
    unsigned long long value = strtoull(text, &end, 0);
    if (errno || *end) return false;
    *out = value;
    return true;
}

static bool parse_double(const char *text, double *out)
{
    char *end;
    errno = 0;
    double value = strtod(text, &end);
    if (!text[0] || errno || *end || !isfinite(value)) return false;
    *out = value;
    return true;
}

static void usage(FILE *output)
{
    fprintf(output,
        "Usage: chameleon_skew [--gib 16 | --mib 64] [--threads 4] [--seconds 30]\n"
        "  [--seed 1] [--hot-percent 1] [--hot-access-ppm 990000] [--write-percent 10]\n"
        "  --hot-access-ppm accepts 0..1000000; 999900 means 99.99%% hot accesses.\n"
        "  --hot-access-percent 0..100 remains supported (percent * 10000 ppm).\n"
        "  If both hot-access options are given, the last one takes effect.\n"
        "  [--ops-per-thread N] [--madvise huge|nohuge|none]\n"
        "  [--pattern contiguous|striped|uniform|sequential] [--stripe-hot-pages 8]\n"
        "  contiguous (default) keeps a hot region per worker; striped places a hot window\n"
        "  in every aligned 2 MiB block (requires 2 MiB-aligned thread partitions).\n"
        "  --stripe-hot-pages accepts 1..512 and replaces --hot-percent for striped.\n"
        "  uniform samples all pages; sequential walks pages, choosing a random line per page.\n"
        "  uniform/sequential ignore hot-access probability.\n"
        "Commands after SKEW_READY: warmup [seconds], run [seconds], shift [percent], verify, quit.\n"
        "  pattern NAME and hot-access-ppm VALUE change the next run/warmup while idle.\n"
        "shift defaults to advancing 50%%; an explicit percentage is an absolute offset.\n"
        "For striped, shift moves the hot window within every 2 MiB block (with wraparound);\n"
        "for sequential, shift sets the next page within each worker's partition.\n"
        "--ops-per-thread runs exactly N operations per worker per run/warmup and ignores seconds.\n"
        "EOF/quit verifies all memory if modified since the last full verification.\n");
}

static void parse_args(int argc, char **argv)
{
    enum { OPT_GIB = 1000, OPT_MIB, OPT_THREADS, OPT_SECONDS, OPT_SEED,
           OPT_HOT, OPT_HOT_ACCESS, OPT_HOT_PPM, OPT_WRITE, OPT_OPS, OPT_ADVICE,
           OPT_PATTERN, OPT_STRIPE_HOT };
    const struct option options[] = {
        {"gib", required_argument, NULL, OPT_GIB},
        {"mib", required_argument, NULL, OPT_MIB},
        {"threads", required_argument, NULL, OPT_THREADS},
        {"seconds", required_argument, NULL, OPT_SECONDS},
        {"seed", required_argument, NULL, OPT_SEED},
        {"hot-percent", required_argument, NULL, OPT_HOT},
        {"hot-access-percent", required_argument, NULL, OPT_HOT_ACCESS},
        {"hot-access-ppm", required_argument, NULL, OPT_HOT_PPM},
        {"write-percent", required_argument, NULL, OPT_WRITE},
        {"ops-per-thread", required_argument, NULL, OPT_OPS},
        {"madvise", required_argument, NULL, OPT_ADVICE},
        {"pattern", required_argument, NULL, OPT_PATTERN},
        {"stripe-hot-pages", required_argument, NULL, OPT_STRIPE_HOT},
        {"help", no_argument, NULL, 'h'}, {NULL, 0, NULL, 0}
    };
    int option;
    while ((option = getopt_long(argc, argv, "h", options, NULL)) != -1) {
        uint64_t number = 0;
        double real = 0;
        if (option == 'h') { usage(stdout); exit(0); }
        bool numeric = option == OPT_SECONDS || option == OPT_HOT ?
                       parse_double(optarg ? optarg : "", &real) :
                       option == OPT_ADVICE || option == OPT_PATTERN ||
                       parse_u64(optarg ? optarg : "", &number);
        if (!numeric) goto invalid;
        switch (option) {
        case OPT_GIB:
        case OPT_MIB: {
            unsigned shift = option == OPT_GIB ? 30 : 20;
            if (!number || number > (uint64_t)(SIZE_MAX - ALIGN_BYTES) >> shift) goto invalid;
            bench.bytes = number << shift;
            break;
        }
        case OPT_THREADS:
            if (!number || number > MAX_THREADS) goto invalid;
            bench.threads = (unsigned)number;
            break;
        case OPT_SECONDS:
            if (real <= 0 || real > 86400) goto invalid;
            bench.seconds = real;
            break;
        case OPT_SEED: bench.seed = number; break;
        case OPT_HOT:
            if (real <= 0 || real > 100) goto invalid;
            bench.hot_percent = real;
            break;
        case OPT_HOT_ACCESS:
            if (number > 100) goto invalid;
            bench.hot_access_ppm = (unsigned)number * 10000;
            break;
        case OPT_HOT_PPM:
            if (number > 1000000) goto invalid;
            bench.hot_access_ppm = (unsigned)number;
            break;
        case OPT_WRITE:
            if (number > 100) goto invalid;
            bench.write_percent = (unsigned)number;
            break;
        case OPT_OPS:
            if (!number) goto invalid;
            bench.ops_per_thread = number;
            break;
        case OPT_ADVICE:
            if (!strcmp(optarg, "huge")) bench.advice = MADV_HUGEPAGE;
            else if (!strcmp(optarg, "nohuge")) bench.advice = MADV_NOHUGEPAGE;
            else if (!strcmp(optarg, "none")) bench.advice = -1;
            else goto invalid;
            break;
        case OPT_PATTERN:
            if (!parse_pattern(optarg, &bench.pattern)) goto invalid;
            break;
        case OPT_STRIPE_HOT:
            if (!number || number > STRIPE_PAGES) goto invalid;
            bench.stripe_hot_pages = (unsigned)number;
            break;
        default: goto invalid;
        }
        continue;
invalid:
        fprintf(stderr, "SKEW_ARGUMENT_ERROR option=%s\n", optind ? argv[optind - 1] : "unknown");
        usage(stderr);
        exit(2);
    }
    if (optind != argc || bench.bytes % (PAGE_BYTES * bench.threads) ||
        bench.bytes / bench.threads < PAGE_BYTES) {
        fprintf(stderr, "SKEW_ARGUMENT_ERROR bytes must divide into page-aligned thread partitions\n");
        exit(2);
    }
    if (!pattern_supported(bench.pattern)) {
        fprintf(stderr, "SKEW_ARGUMENT_ERROR striped requires 2 MiB-aligned thread partitions\n");
        exit(2);
    }
}

static void set_hotspots(double percent)
{
    bench.shift_percent = percent;
    for (unsigned i = 0; i < bench.threads; i++) {
        struct worker *worker = &bench.workers[i];
        uint64_t window = bench.pattern == PAT_STRIPED ? STRIPE_PAGES : worker->pages;
        worker->hot_start = (uint64_t)((long double)window * percent / 100.0L);
        if (worker->hot_start == window) worker->hot_start = 0;
        if (bench.pattern == PAT_STRIPED)
            worker->hot_pages = worker->pages / STRIPE_PAGES * bench.stripe_hot_pages;
        else if (bench.pattern == PAT_UNIFORM || bench.pattern == PAT_SEQUENTIAL)
            worker->hot_pages = worker->pages;
        else {
            worker->hot_pages = (uint64_t)((long double)worker->pages * bench.hot_percent / 100.0L);
            if (!worker->hot_pages) worker->hot_pages = 1;
        }
        worker->sequential_page = worker->hot_start;
        printf("SKEW_PARTITION thread=%u address=%p bytes=%" PRIu64
               " hot_address=%p hot_pages=%" PRIu64 " hot_start_page=%" PRIu64
               " pattern=%s stripe_hot_pages=%u stripe_start_page=%" PRIu64 "\n",
               i, (void *)worker->memory, (uint64_t)(worker->pages * PAGE_BYTES),
               (void *)((unsigned char *)worker->memory + worker->hot_start * PAGE_BYTES),
               worker->hot_pages, worker->hot_start, pattern_name(bench.pattern),
               bench.stripe_hot_pages, bench.pattern == PAT_STRIPED ? worker->hot_start : 0);
    }
}

int main(int argc, char **argv)
{
    parse_args(argc, argv);
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (sysconf(_SC_PAGESIZE) != (long)PAGE_BYTES) {
        fprintf(stderr, "SKEW_FATAL requires 4096-byte native pages\n");
        return 2;
    }
    unsigned char *raw = mmap(NULL, bench.bytes + ALIGN_BYTES,
        PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (raw == MAP_FAILED) fatal("allocate workload");
    uint64_t prefix = (-(uintptr_t)raw) & (ALIGN_BYTES - 1);
    if (prefix && munmap(raw, prefix)) fatal("trim alignment prefix");
    raw += prefix;
    if (munmap(raw + bench.bytes, ALIGN_BYTES - prefix)) fatal("trim alignment suffix");
    bench.memory = (uint64_t *)raw;
    if (bench.advice >= 0 && madvise(raw, bench.bytes, bench.advice)) fatal("workload madvise");
    bench.workers = calloc(bench.threads, sizeof(*bench.workers));
    if (!bench.workers) fatal("allocate worker metadata");
    int error = pthread_barrier_init(&bench.begin, NULL, bench.threads + 1);
    if (!error) error = pthread_barrier_init(&bench.end, NULL, bench.threads + 1);
    if (error) { errno = error; fatal("initialize worker barriers"); }
    atomic_init(&bench.stop, false);
    atomic_init(&bench.done, 0);
    for (unsigned i = 0; i < bench.threads; i++) {
        struct worker *worker = &bench.workers[i];
        uint64_t partition_bytes = bench.bytes / bench.threads;
        worker->id = i;
        worker->memory = (uint64_t *)(raw + i * partition_bytes);
        worker->first_line = i * partition_bytes / LINE_BYTES;
        worker->pages = partition_bytes / PAGE_BYTES;
        worker->random = mix64(bench.seed ^ ((uint64_t)i + 1) * UINT64_C(0xd1342543de82ef95));
#define INIT(name) atomic_init(&worker->name, 0)
        INIT(touches); INIT(reads); INIT(writes); INIT(init_bytes); INIT(verify_bytes);
        INIT(errors); INIT(major_faults); INIT(minor_faults);
#undef INIT
        error = pthread_create(&worker->thread, NULL, worker_main, worker);
        if (error) { errno = error; fatal("start worker"); }
    }
    set_hotspots(0);
    printf("SKEW_START pid=%d address=%p bytes=%" PRIu64 " threads=%u seed=%" PRIu64 "\n",
           getpid(), (void *)bench.memory, bench.bytes, bench.threads, bench.seed);
    struct metrics initialized = run_phase(PH_INIT, 0);
    if (initialized.init_bytes != bench.bytes) fatal("incomplete physical initialization");
    printf("SKEW_READY pid=%d address=%p bytes=%" PRIu64 " threads=%u"
           " partition_bytes=%" PRIu64 " page_bytes=%" PRIu64
           " hot_pages_per_thread=%" PRIu64 " hot_percent=%.6f hot_access_ppm=%u hot_access_percent=%.4f"
           " write_percent=%u seed=%" PRIu64 " ops_per_thread=%" PRIu64
           " pattern=%s stripe_bytes=%" PRIu64 " stripe_hot_pages=%u effective_hot_percent=%.6f\n",
           getpid(), (void *)bench.memory, bench.bytes, bench.threads, bench.bytes / bench.threads,
           (uint64_t)PAGE_BYTES, bench.workers[0].hot_pages, bench.hot_percent,
           bench.hot_access_ppm, bench.hot_access_ppm / 10000.0,
           bench.write_percent, bench.seed, bench.ops_per_thread, pattern_name(bench.pattern),
           (uint64_t)ALIGN_BYTES, bench.stripe_hot_pages,
           (double)bench.workers[0].hot_pages * 100.0 / (double)bench.workers[0].pages);
    bool dirty = true;
    char input[256];
    while (fgets(input, sizeof(input), stdin)) {
        char *save, *op = strtok_r(input, " \t\r\n", &save);
        if (!op) continue;
        char *argument = strtok_r(NULL, " \t\r\n", &save);
        char *extra = strtok_r(NULL, " \t\r\n", &save);
        double number = 0;
        if (extra) goto bad_command;
        if (!strcmp(op, "warmup") || !strcmp(op, "run")) {
            number = bench.seconds;
            if (argument && (!parse_double(argument, &number) || number <= 0 || number > 86400))
                goto bad_command;
            run_phase(!strcmp(op, "run") ? PH_RUN : PH_WARMUP, number);
            dirty = true;
        } else if (!strcmp(op, "shift")) {
            number = bench.shift_percent + 50;
            if (number >= 100) number -= 100;
            if (argument && (!parse_double(argument, &number) || number < 0 || number >= 100))
                goto bad_command;
            set_hotspots(number);
            printf("SKEW_SHIFT percent=%.6f pattern=%s stripe_start_page=%" PRIu64 "\n",
                   number, pattern_name(bench.pattern),
                   bench.pattern == PAT_STRIPED ? bench.workers[0].hot_start : 0);
        } else if (!strcmp(op, "pattern")) {
            enum access_pattern pattern;
            if (!argument || !parse_pattern(argument, &pattern) || !pattern_supported(pattern))
                goto bad_command;
            bench.pattern = pattern;
            set_hotspots(bench.shift_percent);
            printf("SKEW_PATTERN pattern=%s stripe_hot_pages=%u hot_access_ppm=%u\n",
                   pattern_name(bench.pattern), bench.stripe_hot_pages, bench.hot_access_ppm);
        } else if (!strcmp(op, "hot-access-ppm")) {
            uint64_t ppm;
            if (!argument || !parse_u64(argument, &ppm) || ppm > 1000000)
                goto bad_command;
            bench.hot_access_ppm = (unsigned)ppm;
            printf("SKEW_HOT_ACCESS hot_access_ppm=%u pattern=%s\n",
                   bench.hot_access_ppm, pattern_name(bench.pattern));
        } else if (!strcmp(op, "verify") && !argument) {
            run_phase(PH_VERIFY, 0);
            dirty = false;
        } else if (!strcmp(op, "quit") && !argument) {
            break;
        } else {
bad_command:
            fprintf(stderr, "SKEW_COMMAND_ERROR command=%s\n", op);
            bench.command_errors++;
        }
    }
    if (ferror(stdin)) { bench.command_errors++; perror("SKEW_STDIN_ERROR"); }
    if (dirty) run_phase(PH_VERIFY, 0);
    bench.phase = PH_QUIT;
    pthread_barrier_wait(&bench.begin);
    for (unsigned i = 0; i < bench.threads; i++) {
        error = pthread_join(bench.workers[i].thread, NULL);
        if (error) { errno = error; fatal("join worker"); }
    }
    pthread_barrier_destroy(&bench.begin);
    pthread_barrier_destroy(&bench.end);
    struct rusage usage;
    if (getrusage(RUSAGE_SELF, &usage)) fatal("final getrusage");
    printf("SKEW_TOTAL status=%s bytes=%" PRIu64 " threads=%u touches=%" PRIu64
           " reads=%" PRIu64 " writes=%" PRIu64 " init_bytes=%" PRIu64
           " verify_bytes=%" PRIu64 " verify_errors=%" PRIu64
           " major_faults=%" PRIu64 " process_major_faults=%ld command_errors=%u\n",
           bench.total.errors || bench.command_errors ? "FAIL" : "PASS", bench.bytes,
           bench.threads, bench.total.touches, bench.total.reads, bench.total.writes,
           bench.total.init_bytes, bench.total.verify_bytes, bench.total.errors,
           bench.total.major_faults, usage.ru_majflt, bench.command_errors);
    if (munmap(bench.memory, bench.bytes)) fatal("release workload mapping");
    free(bench.workers);
    return bench.total.errors || bench.command_errors ? 1 : 0;
}
