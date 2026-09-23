#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <chrono>
#include <exception>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "application.hh"
#include "pvc_format.hh"

namespace {

struct Options {
    Options()
        : processors(0), map_tasks(0), reduce_tasks(0), group_tasks(0),
          quiet(false) {}

    std::string input;
    std::string output;
    int processors;
    int map_tasks;
    int reduce_tasks;
    int group_tasks;
    bool quiet;
};

void usage(FILE *stream, const char *program) {
    fprintf(stream,
            "Usage: %s INPUT [OPTIONS]\n"
            "       %s --input INPUT [OPTIONS]\n\n"
            "Two-pass Page View Count over a PVC binary input.\n\n"
            "Options:\n"
            "  -p, --threads N       Metis worker threads (0: all online CPUs)\n"
            "  -m, --map-tasks N     map splits in each pass (0: Metis default)\n"
            "  -r, --reduce-tasks N  Stage-1 reduce tasks (0: sample and choose)\n"
            "  -g, --group-tasks N   Stage-2 group tasks (0: sample and choose)\n"
            "  -q, --quiet           suppress detailed Metis statistics\n"
            "  -o, --output FILE     write URL<TAB>view-count results\n"
            "  -i, --input FILE      input alternative to positional INPUT\n"
            "  -h, --help            show this help\n",
            program, program);
}

bool parse_nonnegative_int(const char *text, const char *name, int *result) {
    if (!text || !*text) {
        fprintf(stderr, "%s requires a non-negative integer\n", name);
        return false;
    }
    errno = 0;
    char *end = NULL;
    const long value = strtol(text, &end, 10);
    if (errno == ERANGE || end == text || *end != '\0' || value < 0 ||
        value > std::numeric_limits<int>::max()) {
        fprintf(stderr, "invalid %s value: %s\n", name, text);
        return false;
    }
    *result = static_cast<int>(value);
    return true;
}

bool parse_options(int argc, char **argv, Options *options) {
    static const struct option long_options[] = {
        {"input", required_argument, NULL, 'i'},
        {"threads", required_argument, NULL, 'p'},
        {"map-tasks", required_argument, NULL, 'm'},
        {"reduce-tasks", required_argument, NULL, 'r'},
        {"group-tasks", required_argument, NULL, 'g'},
        {"quiet", no_argument, NULL, 'q'},
        {"output", required_argument, NULL, 'o'},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0},
    };

    opterr = 0;
    int option = 0;
    while ((option = getopt_long(argc, argv, "i:p:m:r:g:qo:h",
                                 long_options, NULL)) != -1) {
        switch (option) {
        case 'i':
            if (!options->input.empty()) {
                fprintf(stderr, "input was specified more than once\n");
                return false;
            }
            options->input = optarg;
            break;
        case 'p':
            if (!parse_nonnegative_int(optarg, "thread count",
                                       &options->processors))
                return false;
            break;
        case 'm':
            if (!parse_nonnegative_int(optarg, "map-task count",
                                       &options->map_tasks))
                return false;
            break;
        case 'r':
            if (!parse_nonnegative_int(optarg, "reduce-task count",
                                       &options->reduce_tasks))
                return false;
            break;
        case 'g':
            if (!parse_nonnegative_int(optarg, "group-task count",
                                       &options->group_tasks))
                return false;
            break;
        case 'q':
            options->quiet = true;
            break;
        case 'o':
            if (!options->output.empty()) {
                fprintf(stderr, "output was specified more than once\n");
                return false;
            }
            options->output = optarg;
            break;
        case 'h':
            usage(stdout, argv[0]);
            exit(EXIT_SUCCESS);
        case '?':
        default:
            if (optopt)
                fprintf(stderr, "unknown option or missing argument: -%c\n", optopt);
            else
                fprintf(stderr, "unknown option: %s\n", argv[optind - 1]);
            return false;
        }
    }

    if (optind < argc) {
        if (!options->input.empty()) {
            fprintf(stderr, "input was specified both positionally and with --input\n");
            return false;
        }
        options->input = argv[optind++];
    }
    if (optind != argc) {
        fprintf(stderr, "unexpected argument: %s\n", argv[optind]);
        return false;
    }
    if (options->input.empty()) {
        fprintf(stderr, "an input file is required\n");
        return false;
    }
    if (!options->output.empty() && options->output == options->input) {
        fprintf(stderr, "input and output must be different files\n");
        return false;
    }

    const long online = sysconf(_SC_NPROCESSORS_ONLN);
    if (options->processors > 0 && online > 0 && options->processors > online) {
        fprintf(stderr, "thread count %d exceeds the %ld online CPUs\n",
                options->processors, online);
        return false;
    }
    return true;
}

class MappedInput {
  public:
    explicit MappedInput(const std::string &path)
        : data_(MAP_FAILED), size_(0), header_(NULL), records_(NULL) {
        const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC);
        if (fd < 0)
            throw std::runtime_error("cannot open " + path + ": " + strerror(errno));

        struct stat status;
        if (fstat(fd, &status) != 0) {
            const int saved_errno = errno;
            close(fd);
            throw std::runtime_error("cannot stat " + path + ": " +
                                     strerror(saved_errno));
        }
        if (status.st_size < static_cast<off_t>(sizeof(pvc::FileHeader))) {
            close(fd);
            throw std::runtime_error("PVC input is smaller than its 64-byte header");
        }
        if (static_cast<uint64_t>(status.st_size) >
            static_cast<uint64_t>(std::numeric_limits<size_t>::max())) {
            close(fd);
            throw std::runtime_error("PVC input is too large for this process");
        }
        size_ = static_cast<size_t>(status.st_size);
        data_ = mmap(NULL, size_, PROT_READ, MAP_PRIVATE, fd, 0);
        const int mmap_errno = errno;
        close(fd);
        if (data_ == MAP_FAILED)
            throw std::runtime_error("cannot mmap " + path + ": " +
                                     strerror(mmap_errno));

        header_ = static_cast<const pvc::FileHeader *>(data_);
        std::string error;
        if (!pvc::validate_header(*header_, static_cast<uint64_t>(size_), &error)) {
            munmap(data_, size_);
            data_ = MAP_FAILED;
            throw std::runtime_error(error);
        }
        if (header_->record_count >
            static_cast<uint64_t>(std::numeric_limits<size_t>::max())) {
            munmap(data_, size_);
            data_ = MAP_FAILED;
            throw std::runtime_error("PVC record count is too large for this process");
        }
        records_ = reinterpret_cast<const pvc::Record *>(
            static_cast<const unsigned char *>(data_) + header_->header_bytes);
    }

    ~MappedInput() {
        if (data_ != MAP_FAILED)
            munmap(data_, size_);
    }

    const pvc::FileHeader &header() const { return *header_; }
    const pvc::Record *records() const { return records_; }
    const unsigned char *bytes() const {
        return static_cast<const unsigned char *>(data_);
    }

  private:
    MappedInput(const MappedInput &);
    MappedInput &operator=(const MappedInput &);

    void *data_;
    size_t size_;
    const pvc::FileHeader *header_;
    const pvc::Record *records_;
};

template <typename T>
class FixedSplitter {
  public:
    FixedSplitter(const T *items, size_t count, int requested_splits)
        : items_(items), count_(count), requested_splits_(requested_splits),
          next_(0), items_per_split_(0) {}

    bool split(split_t *result, int cores) {
        if (next_ >= count_)
            return false;
        if (items_per_split_ == 0) {
            size_t splits = requested_splits_ > 0
                                ? static_cast<size_t>(requested_splits_)
                                : static_cast<size_t>(cores) * def_nsplits_per_core;
            if (splits == 0)
                splits = 1;
            items_per_split_ = count_ / splits;
            if (count_ % splits)
                ++items_per_split_;
            if (items_per_split_ == 0)
                items_per_split_ = 1;
        }
        const size_t remaining = count_ - next_;
        const size_t number = remaining < items_per_split_ ? remaining : items_per_split_;
        result->data = const_cast<T *>(items_ + next_);
        result->length = number * sizeof(T);
        next_ += number;
        return true;
    }

  private:
    const T *items_;
    size_t count_;
    int requested_splits_;
    size_t next_;
    size_t items_per_split_;
};

int compare_u64(uint64_t lhs, uint64_t rhs) {
    return lhs < rhs ? -1 : (lhs > rhs ? 1 : 0);
}

uint64_t mix64(uint64_t value) {
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value *= UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

uint64_t record_order_hash(const pvc::Record &record) {
    uint64_t hash = mix64(record.url_id + UINT64_C(0x9e3779b97f4a7c15));
    hash ^= mix64(record.ip_id + UINT64_C(0x3c6ef372fe94f82a));
    hash ^= mix64(record.cookie_id + UINT64_C(0xdaa66d2c7ddef743));
    return mix64(hash);
}

class DeduplicatePass : public map_reduce {
  public:
    DeduplicatePass(const pvc::Record *records, size_t count, int map_tasks)
        : splitter_(records, count, map_tasks) {}

    bool split(split_t *result, int cores) {
        return splitter_.split(result, cores);
    }

    void map_function(split_t *input) {
        const pvc::Record *records = static_cast<const pvc::Record *>(input->data);
        const size_t count = input->length / sizeof(pvc::Record);
        for (size_t i = 0; i < count; ++i) {
            map_emit(const_cast<pvc::Record *>(&records[i]),
                     int2ptr(sizeof(pvc::Record)),
                     sizeof(pvc::Record));
        }
    }

    int key_compare(const void *lhs_key, const void *rhs_key) {
        const pvc::Record &lhs = *static_cast<const pvc::Record *>(lhs_key);
        const pvc::Record &rhs = *static_cast<const pvc::Record *>(rhs_key);
        int result = compare_u64(lhs.url_id, rhs.url_id);
        if (!result)
            result = compare_u64(lhs.ip_id, rhs.ip_id);
        if (!result)
            result = compare_u64(lhs.cookie_id, rhs.cookie_id);
        return result;
    }

    void reduce_function(void *key, void **values, size_t value_count) {
        (void)values;
        if (value_count == 0)
            return;
        // The Mars PVC description emits the original line size after
        // eliminating identical <URL, IP, Cookie> records.
        reduce_emit(key, int2ptr(sizeof(pvc::Record)));
    }

    int final_output_compare(const keyval_t *lhs, const keyval_t *rhs) {
        const pvc::Record &lhs_record = *static_cast<const pvc::Record *>(lhs->key_);
        const pvc::Record &rhs_record = *static_cast<const pvc::Record *>(rhs->key_);
        const uint64_t lhs_hash = record_order_hash(lhs_record);
        const uint64_t rhs_hash = record_order_hash(rhs_record);
        const int result = compare_u64(lhs_hash, rhs_hash);
        return result ? result : key_compare(lhs->key_, rhs->key_);
    }

  private:
    FixedSplitter<pvc::Record> splitter_;
};

class CountPass : public map_group {
  public:
    CountPass(const uint64_t *file_offsets,
              size_t count,
              const unsigned char *mapping,
              int map_tasks)
        : splitter_(file_offsets, count, map_tasks), mapping_(mapping) {}

    bool split(split_t *result, int cores) {
        return splitter_.split(result, cores);
    }

    void map_function(split_t *input) {
        const uint64_t *offsets = static_cast<const uint64_t *>(input->data);
        const size_t count = input->length / sizeof(uint64_t);
        for (size_t i = 0; i < count; ++i) {
            const pvc::Record *record = reinterpret_cast<const pvc::Record *>(
                mapping_ + offsets[i]);
            map_emit(const_cast<uint64_t *>(&record->url_id),
                     const_cast<uint64_t *>(&record->ip_id), sizeof(uint64_t));
        }
    }

    int key_compare(const void *lhs, const void *rhs) {
        return compare_u64(*static_cast<const uint64_t *>(lhs),
                           *static_cast<const uint64_t *>(rhs));
    }

  private:
    FixedSplitter<uint64_t> splitter_;
    const unsigned char *mapping_;
};

class MetisRuntime {
  public:
    MetisRuntime() { mapreduce_appbase::initialize(); }
    ~MetisRuntime() { mapreduce_appbase::deinitialize(); }

  private:
    MetisRuntime(const MetisRuntime &);
    MetisRuntime &operator=(const MetisRuntime &);
};

bool paths_refer_to_same_file(const std::string &lhs, const std::string &rhs) {
    struct stat lhs_status;
    struct stat rhs_status;
    if (stat(lhs.c_str(), &lhs_status) != 0 || stat(rhs.c_str(), &rhs_status) != 0)
        return false;
    return lhs_status.st_dev == rhs_status.st_dev &&
           lhs_status.st_ino == rhs_status.st_ino;
}

bool write_results(const std::string &path,
                   xarray<keyvals_len_t> *results,
                   std::string *error) {
    if (path.empty())
        return true;
    FILE *file = fopen(path.c_str(), "w");
    if (!file) {
        *error = "cannot open output " + path + ": " + strerror(errno);
        return false;
    }
    bool ok = true;
    for (size_t i = 0; i < results->size(); ++i) {
        const keyvals_len_t *entry = results->at(i);
        const uint64_t url = *static_cast<const uint64_t *>(entry->key_);
        if (fprintf(file, "%" PRIu64 "\t%" PRIu64 "\n", url, entry->len) < 0) {
            ok = false;
            break;
        }
    }
    if (ok && fflush(file) != 0)
        ok = false;
    const int saved_errno = errno;
    if (fclose(file) != 0 && ok) {
        ok = false;
        *error = "cannot close output " + path + ": " + strerror(errno);
    } else if (!ok) {
        *error = "cannot write output " + path + ": " + strerror(saved_errno);
    }
    return ok;
}

uint64_t update_checksum(uint64_t checksum, uint64_t value) {
    for (unsigned i = 0; i < sizeof(value); ++i) {
        checksum ^= static_cast<unsigned char>(value & 0xffU);
        checksum *= UINT64_C(1099511628211);
        value >>= 8;
    }
    return checksum;
}

struct Timings {
    Timings() : stage1_ms(0), interstage_ms(0), stage2_ms(0) {}

    double stage1_ms;
    double interstage_ms;
    double stage2_ms;
};

typedef std::chrono::steady_clock SteadyClock;

double elapsed_ms(const SteadyClock::time_point &start,
                  const SteadyClock::time_point &end) {
    return std::chrono::duration<double, std::milli>(end - start).count();
}

void print_summary(uint64_t unique_records,
                   uint64_t urls,
                   uint64_t total_views,
                   uint64_t checksum,
                   const Timings &timings) {
    std::cout << std::fixed << std::setprecision(3)
              << "stage1_ms=" << timings.stage1_ms << "\n"
              << "interstage_ms=" << timings.interstage_ms << "\n"
              << "stage2_ms=" << timings.stage2_ms << "\n"
              << "total_ms="
              << timings.stage1_ms + timings.interstage_ms + timings.stage2_ms << "\n"
              << "stage1_unique=" << unique_records << "\n"
              << "stage2_urls=" << urls << "\n"
              << "total_views=" << total_views << "\n"
              << "checksum=" << checksum << "\n";
}

int run(const Options &options) {
    MappedInput input(options.input);
    const uint64_t record_count_u64 = input.header().record_count;
    const size_t record_count = static_cast<size_t>(record_count_u64);

    if (!options.output.empty() &&
        paths_refer_to_same_file(options.input, options.output)) {
        throw std::runtime_error("input and output refer to the same file");
    }

    if (record_count == 0) {
        std::cout << "phase=stage1\nphase=interstage\nphase=stage2\n"
                  << std::flush;
        std::string error;
        xarray<keyvals_len_t> empty;
        if (!write_results(options.output, &empty, &error))
            throw std::runtime_error(error);
        print_summary(0, 0, 0, UINT64_C(14695981039346656037), Timings());
        return EXIT_SUCCESS;
    }

    MetisRuntime runtime;
    Timings timings;

    std::cout << "phase=stage1\n" << std::flush;
    DeduplicatePass deduplicate(input.records(), record_count, options.map_tasks);
    deduplicate.set_ncore(options.processors);
    deduplicate.set_reduce_task(options.reduce_tasks);
    const SteadyClock::time_point stage1_start = SteadyClock::now();
    deduplicate.sched_run();
    const SteadyClock::time_point stage1_end = SteadyClock::now();
    timings.stage1_ms = elapsed_ms(stage1_start, stage1_end);
    if (!options.quiet)
        deduplicate.print_stats();

    const uint64_t unique_records = deduplicate.results_.size();
    std::cout << "phase=interstage\n" << std::flush;
    const SteadyClock::time_point interstage_start = SteadyClock::now();
    std::vector<uint64_t> offsets;
    offsets.reserve(deduplicate.results_.size());
    const unsigned char *mapping = input.bytes();
    const unsigned char *first_record =
        reinterpret_cast<const unsigned char *>(input.records());
    const uintptr_t mapping_address = reinterpret_cast<uintptr_t>(mapping);
    const uintptr_t first_record_address =
        reinterpret_cast<uintptr_t>(first_record);
    const uintptr_t records_end_address =
        first_record_address + record_count * sizeof(pvc::Record);
    for (size_t i = 0; i < deduplicate.results_.size(); ++i) {
        const unsigned char *record = static_cast<const unsigned char *>(
            deduplicate.results_.at(i)->key_);
        const uintptr_t record_address = reinterpret_cast<uintptr_t>(record);
        if (record_address < first_record_address ||
            record_address >= records_end_address ||
            (record_address - first_record_address) % sizeof(pvc::Record) != 0) {
            deduplicate.free_results();
            throw std::runtime_error("Stage 1 returned a key outside the input mapping");
        }
        offsets.push_back(static_cast<uint64_t>(record_address - mapping_address));
    }
    // Stage 2 needs only one 64-bit mmap offset per unique record.  Release
    // the larger Metis result array before allocating Stage-2 intermediates.
    deduplicate.free_results();
    const SteadyClock::time_point interstage_end = SteadyClock::now();
    timings.interstage_ms = elapsed_ms(interstage_start, interstage_end);

    std::cout << "phase=stage2\n" << std::flush;
    CountPass count(offsets.data(), offsets.size(), mapping, options.map_tasks);
    count.set_ncore(options.processors);
    count.set_group_task(options.group_tasks);
    const SteadyClock::time_point stage2_start = SteadyClock::now();
    count.sched_run();
    const SteadyClock::time_point stage2_end = SteadyClock::now();
    timings.stage2_ms = elapsed_ms(stage2_start, stage2_end);
    if (!options.quiet)
        count.print_stats();

    uint64_t total_views = 0;
    uint64_t checksum = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < count.results_.size(); ++i) {
        const keyvals_len_t *entry = count.results_.at(i);
        const uint64_t url = *static_cast<const uint64_t *>(entry->key_);
        if (std::numeric_limits<uint64_t>::max() - total_views < entry->len) {
            count.free_results();
            throw std::runtime_error("total view count overflowed uint64_t");
        }
        total_views += entry->len;
        checksum = update_checksum(checksum, url);
        checksum = update_checksum(checksum, entry->len);
    }
    std::string error;
    if (!write_results(options.output, &count.results_, &error)) {
        count.free_results();
        throw std::runtime_error(error);
    }

    const uint64_t url_count = count.results_.size();
    count.free_results();
    print_summary(unique_records, url_count, total_views, checksum, timings);
    return EXIT_SUCCESS;
}

}  // namespace

int main(int argc, char **argv) {
    Options options;
    if (!parse_options(argc, argv, &options)) {
        usage(stderr, argv[0]);
        return EXIT_FAILURE;
    }
    try {
        return run(options);
    } catch (const std::exception &error) {
        fprintf(stderr, "page_view_count: %s\n", error.what());
        return EXIT_FAILURE;
    }
}
