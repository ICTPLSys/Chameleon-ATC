#include "pvc_format.hh"

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

namespace {

struct Options {
    std::string output;
    bool records_set;
    bool bytes_set;
    uint64_t records;
    uint64_t bytes;
    uint64_t urls;
    uint64_t ips;
    uint64_t cookies;
    uint64_t seed;
    double duplicate_rate;
    std::string distribution;
    double zipf_theta;

    Options()
        : records_set(false),
          bytes_set(false),
          records(0),
          bytes(0),
          urls(1000000),
          ips(10000000),
          cookies(100000000),
          seed(1),
          duplicate_rate(0.10),
          distribution("uniform"),
          zipf_theta(1.10) {}
};

void usage(FILE *stream, const char *program) {
    fprintf(stream,
            "Usage: %s --output FILE (--records N | --bytes SIZE) [OPTIONS]\n"
            "\n"
            "Generate deterministic synthetic <URL,IP,Cookie> records in the\n"
            "fixed-width PVC binary format.  --bytes is the requested record\n"
            "payload size (rounded down to a whole 24-byte record); the 64-byte\n"
            "file header is additional.\n"
            "\n"
            "Options:\n"
            "  -o, --output FILE           output file, or - for stdout\n"
            "      --records N             number of records\n"
            "      --bytes SIZE            payload bytes; K/M/G and KiB/MiB/GiB work\n"
            "      --urls N                URL ID domain (default 1000000)\n"
            "      --ips N                 IP ID domain (default 10000000)\n"
            "      --cookies N             cookie ID domain (default 100000000)\n"
            "      --duplicate-rate R      intentional full-record duplicate fraction\n"
            "                                in [0,1] (default 0.10)\n"
            "      --distribution NAME     URL distribution: uniform or zipf\n"
            "      --zipf-theta T           Zipf exponent, T > 0 (default 1.10)\n"
            "      --seed N                 deterministic 64-bit seed (default 1)\n"
            "  -h, --help                   show this help\n",
            program);
}

bool parse_u64(const char *text, uint64_t *value) {
    if (!text || !*text || *text == '-')
        return false;
    errno = 0;
    char *end = NULL;
    const unsigned long long parsed = strtoull(text, &end, 10);
    if (errno == ERANGE || end == text || *end != '\0')
        return false;
    *value = static_cast<uint64_t>(parsed);
    return true;
}

std::string lowercase(std::string value) {
    for (size_t i = 0; i < value.size(); ++i) {
        if (value[i] >= 'A' && value[i] <= 'Z')
            value[i] = static_cast<char>(value[i] - 'A' + 'a');
    }
    return value;
}

bool parse_size(const char *text, uint64_t *value) {
    if (!text || !*text || *text == '-')
        return false;
    errno = 0;
    char *end = NULL;
    const unsigned long long magnitude = strtoull(text, &end, 10);
    if (errno == ERANGE || end == text)
        return false;

    const std::string suffix = lowercase(std::string(end));
    uint64_t multiplier = 0;
    if (suffix.empty() || suffix == "b") {
        multiplier = 1;
    } else if (suffix == "k" || suffix == "kb" || suffix == "kib") {
        multiplier = UINT64_C(1) << 10;
    } else if (suffix == "m" || suffix == "mb" || suffix == "mib") {
        multiplier = UINT64_C(1) << 20;
    } else if (suffix == "g" || suffix == "gb" || suffix == "gib") {
        multiplier = UINT64_C(1) << 30;
    } else if (suffix == "t" || suffix == "tb" || suffix == "tib") {
        multiplier = UINT64_C(1) << 40;
    } else {
        return false;
    }

    if (magnitude > std::numeric_limits<uint64_t>::max() / multiplier)
        return false;
    *value = static_cast<uint64_t>(magnitude) * multiplier;
    return true;
}

bool parse_double(const char *text, double *value) {
    if (!text || !*text)
        return false;
    errno = 0;
    char *end = NULL;
    const double parsed = strtod(text, &end);
    if (errno == ERANGE || end == text || *end != '\0' || !std::isfinite(parsed))
        return false;
    *value = parsed;
    return true;
}

bool need_value(int argc, char **argv, int *index, const char **value) {
    if (*index + 1 >= argc) {
        fprintf(stderr, "missing value for %s\n", argv[*index]);
        return false;
    }
    *value = argv[++(*index)];
    return true;
}

bool parse_options(int argc, char **argv, Options *options) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg(argv[i]);
        const char *value = NULL;
        if (arg == "-h" || arg == "--help") {
            usage(stdout, argv[0]);
            exit(EXIT_SUCCESS);
        } else if (arg == "-o" || arg == "--output") {
            if (!need_value(argc, argv, &i, &value))
                return false;
            options->output = value;
        } else if (arg == "--records") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_u64(value, &options->records)) {
                fprintf(stderr, "invalid --records value\n");
                return false;
            }
            options->records_set = true;
        } else if (arg == "--bytes") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_size(value, &options->bytes)) {
                fprintf(stderr, "invalid --bytes value\n");
                return false;
            }
            options->bytes_set = true;
        } else if (arg == "--urls") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_u64(value, &options->urls)) {
                fprintf(stderr, "invalid --urls value\n");
                return false;
            }
        } else if (arg == "--ips") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_u64(value, &options->ips)) {
                fprintf(stderr, "invalid --ips value\n");
                return false;
            }
        } else if (arg == "--cookies") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_u64(value, &options->cookies)) {
                fprintf(stderr, "invalid --cookies value\n");
                return false;
            }
        } else if (arg == "--seed") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_u64(value, &options->seed)) {
                fprintf(stderr, "invalid --seed value\n");
                return false;
            }
        } else if (arg == "--duplicate-rate") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_double(value, &options->duplicate_rate)) {
                fprintf(stderr, "invalid --duplicate-rate value\n");
                return false;
            }
        } else if (arg == "--distribution") {
            if (!need_value(argc, argv, &i, &value))
                return false;
            options->distribution = lowercase(value);
        } else if (arg == "--zipf-theta") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_double(value, &options->zipf_theta)) {
                fprintf(stderr, "invalid --zipf-theta value\n");
                return false;
            }
        } else {
            fprintf(stderr, "unknown option: %s\n", arg.c_str());
            return false;
        }
    }

    if (options->output.empty()) {
        fprintf(stderr, "--output is required\n");
        return false;
    }
    if (options->records_set == options->bytes_set) {
        fprintf(stderr, "exactly one of --records and --bytes is required\n");
        return false;
    }
    if (!options->urls || !options->ips || !options->cookies) {
        fprintf(stderr, "--urls, --ips, and --cookies must all be nonzero\n");
        return false;
    }
    if (options->duplicate_rate < 0.0 || options->duplicate_rate > 1.0) {
        fprintf(stderr, "--duplicate-rate must be in [0,1]\n");
        return false;
    }
    if (options->distribution != "uniform" && options->distribution != "zipf") {
        fprintf(stderr, "--distribution must be uniform or zipf\n");
        return false;
    }
    if (!(options->zipf_theta > 0.0)) {
        fprintf(stderr, "--zipf-theta must be greater than zero\n");
        return false;
    }
    return true;
}

uint64_t splitmix64(uint64_t value) {
    value += UINT64_C(0x9e3779b97f4a7c15);
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

class Rng {
  public:
    explicit Rng(uint64_t state) : state_(state) {}

    uint64_t next() {
        state_ += UINT64_C(0x9e3779b97f4a7c15);
        uint64_t value = state_;
        value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
        value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
        return value ^ (value >> 31);
    }

    uint64_t bounded(uint64_t bound) {
        // Rejection avoids modulo bias without allocating a distribution table.
        const uint64_t threshold = static_cast<uint64_t>(-bound) % bound;
        uint64_t value;
        do {
            value = next();
        } while (value < threshold);
        return value % bound;
    }

    double open_unit() {
        // Exactly 53 random bits, strictly between zero and one.
        const uint64_t bits = next() >> 11;
        return (static_cast<double>(bits) + 0.5) /
               static_cast<double>(UINT64_C(1) << 53);
    }

  private:
    uint64_t state_;
};

// Constant-memory rejection-inversion sampler for a finite Zipf distribution.
// It follows the method used by common statistical libraries and is exact up
// to floating-point roundoff; unlike a CDF table it remains practical for tens
// of millions of URLs.
class ZipfSampler {
  public:
    ZipfSampler(uint64_t elements, double exponent)
        : elements_(elements),
          exponent_(exponent),
          h_integral_x1_(h_integral(1.5) - 1.0),
          h_integral_n_(h_integral(static_cast<double>(elements) + 0.5)),
          squeeze_(2.0 - h_integral_inverse(h_integral(2.5) - h(2.0))) {}

    uint64_t sample(Rng *rng) const {
        if (elements_ == 1)
            return 0;
        for (;;) {
            const double u = h_integral_n_ +
                             rng->open_unit() * (h_integral_x1_ - h_integral_n_);
            const double x = h_integral_inverse(u);
            uint64_t rank = static_cast<uint64_t>(x + 0.5);
            rank = std::max<uint64_t>(1, std::min<uint64_t>(elements_, rank));
            if (static_cast<double>(rank) - x <= squeeze_ ||
                u >= h_integral(static_cast<double>(rank) + 0.5) -
                         h(static_cast<double>(rank))) {
                return rank - 1;
            }
        }
    }

  private:
    double h(double x) const {
        return std::exp(-exponent_ * std::log(x));
    }

    double h_integral(double x) const {
        const double log_x = std::log(x);
        const double one_minus = 1.0 - exponent_;
        if (std::fabs(one_minus) < 1e-8)
            return log_x;
        return std::expm1(one_minus * log_x) / one_minus;
    }

    double h_integral_inverse(double x) const {
        const double one_minus = 1.0 - exponent_;
        if (std::fabs(one_minus) < 1e-8)
            return std::exp(x);
        return std::exp(std::log1p(one_minus * x) / one_minus);
    }

    uint64_t elements_;
    double exponent_;
    double h_integral_x1_;
    double h_integral_n_;
    double squeeze_;
};

uint64_t gcd_u64(uint64_t a, uint64_t b) {
    while (b) {
        const uint64_t remainder = a % b;
        a = b;
        b = remainder;
    }
    return a;
}

class Generator {
  public:
    Generator(const Options &options, uint64_t record_count)
        : options_(options),
          record_count_(record_count),
          duplicate_count_(0),
          unique_count_(record_count),
          multiplier_(1),
          increment_(0),
          zipf_(options.urls, options.zipf_theta) {
        if (record_count_) {
            const long double requested =
                static_cast<long double>(options_.duplicate_rate) *
                static_cast<long double>(record_count_);
            duplicate_count_ = static_cast<uint64_t>(std::floor(requested));
            duplicate_count_ = std::min<uint64_t>(duplicate_count_, record_count_ - 1);
            unique_count_ = record_count_ - duplicate_count_;

            multiplier_ = splitmix64(options_.seed ^ UINT64_C(0x551d69b247d1f269)) |
                          UINT64_C(1);
            multiplier_ %= record_count_;
            if (!multiplier_)
                multiplier_ = 1;
            while (gcd_u64(multiplier_, record_count_) != 1) {
                multiplier_ += 2;
                if (multiplier_ >= record_count_)
                    multiplier_ = (multiplier_ % record_count_) | UINT64_C(1);
            }
            increment_ = splitmix64(options_.seed ^ UINT64_C(0xe7037ed1a0b428db)) %
                         record_count_;
        }
    }

    pvc::Record record_at(uint64_t output_index) const {
        const uint64_t logical = permute(output_index);
        uint64_t base_index = logical;
        if (logical >= unique_count_) {
            base_index = splitmix64(options_.seed ^ logical ^
                                    UINT64_C(0x8ebc6af09c88c6e3)) %
                         unique_count_;
        }
        return make_base_record(base_index);
    }

    uint64_t duplicate_count() const { return duplicate_count_; }

  private:
    uint64_t permute(uint64_t index) const {
        if (record_count_ <= 1)
            return 0;
        const unsigned __int128 product =
            static_cast<unsigned __int128>(multiplier_) * index + increment_;
        return static_cast<uint64_t>(product % record_count_);
    }

    pvc::Record make_base_record(uint64_t index) const {
        Rng url_rng(splitmix64(options_.seed ^ index ^
                               UINT64_C(0xa0761d6478bd642f)));
        Rng ip_rng(splitmix64(options_.seed ^ index ^
                              UINT64_C(0xe7037ed1a0b428db)));
        Rng cookie_rng(splitmix64(options_.seed ^ index ^
                                  UINT64_C(0x8ebc6af09c88c6e3)));
        pvc::Record record;
        record.url_id = options_.distribution == "zipf"
                            ? zipf_.sample(&url_rng)
                            : url_rng.bounded(options_.urls);
        record.ip_id = ip_rng.bounded(options_.ips);
        record.cookie_id = cookie_rng.bounded(options_.cookies);
        return record;
    }

    const Options &options_;
    uint64_t record_count_;
    uint64_t duplicate_count_;
    uint64_t unique_count_;
    uint64_t multiplier_;
    uint64_t increment_;
    ZipfSampler zipf_;
};

bool write_records(FILE *file,
                   const Generator &generator,
                   uint64_t record_count,
                   std::string *error) {
    const size_t kBatchRecords = 4096;
    std::vector<pvc::Record> batch;
    batch.resize(kBatchRecords);
    uint64_t written = 0;
    while (written < record_count) {
        const size_t count = static_cast<size_t>(
            std::min<uint64_t>(kBatchRecords, record_count - written));
        for (size_t i = 0; i < count; ++i)
            batch[i] = generator.record_at(written + i);
        if (fwrite(&batch[0], sizeof(batch[0]), count, file) != count) {
            *error = std::string("failed to write PVC records: ") + strerror(errno);
            return false;
        }
        written += count;
    }
    return true;
}

bool make_temporary(const std::string &output,
                    std::string *temporary,
                    FILE **file,
                    std::string *error) {
    std::ostringstream path;
    path << output << ".tmp." << static_cast<unsigned long>(getpid());
    *temporary = path.str();
    const int descriptor = open(temporary->c_str(), O_WRONLY | O_CREAT | O_EXCL,
                                0666);
    if (descriptor < 0) {
        *error = std::string("cannot create temporary output ") + *temporary +
                 ": " + strerror(errno);
        return false;
    }
    *file = fdopen(descriptor, "wb");
    if (!*file) {
        const int saved_errno = errno;
        close(descriptor);
        unlink(temporary->c_str());
        *error = std::string("cannot open temporary output stream: ") +
                 strerror(saved_errno);
        return false;
    }
    return true;
}

}  // namespace

int main(int argc, char **argv) {
    Options options;
    if (!parse_options(argc, argv, &options)) {
        usage(stderr, argv[0]);
        return EXIT_FAILURE;
    }

    const uint64_t record_count =
        options.records_set ? options.records : options.bytes / sizeof(pvc::Record);
    if (!record_count) {
        fprintf(stderr, "the requested workload contains no complete records\n");
        return EXIT_FAILURE;
    }
    const uint64_t max_records =
        (std::numeric_limits<uint64_t>::max() - sizeof(pvc::FileHeader)) /
        sizeof(pvc::Record);
    if (record_count > max_records) {
        fprintf(stderr, "record count is too large for the PVC file format\n");
        return EXIT_FAILURE;
    }

    const bool use_stdout = options.output == "-";
    std::string temporary;
    std::string error;
    FILE *output = stdout;
    if (!use_stdout &&
        !make_temporary(options.output, &temporary, &output, &error)) {
        fprintf(stderr, "%s\n", error.c_str());
        return EXIT_FAILURE;
    }

    const pvc::FileHeader header =
        pvc::make_header(record_count, options.urls, options.ips, options.cookies,
                         options.seed);
    Generator generator(options, record_count);
    bool ok = pvc::write_header(output, header, &error) &&
              write_records(output, generator, record_count, &error);
    if (ok && fflush(output) != 0) {
        error = std::string("failed to flush output: ") + strerror(errno);
        ok = false;
    }
    if (!use_stdout) {
        if (fclose(output) != 0 && ok) {
            error = std::string("failed to close output: ") + strerror(errno);
            ok = false;
        }
        if (ok && rename(temporary.c_str(), options.output.c_str()) != 0) {
            error = std::string("cannot install output file ") + options.output +
                    ": " + strerror(errno);
            ok = false;
        }
    }
    if (!ok) {
        fprintf(stderr, "%s\n", error.c_str());
        if (!use_stdout)
            unlink(temporary.c_str());
        return EXIT_FAILURE;
    }

    uint64_t file_bytes = 0;
    pvc::expected_file_size(header, &file_bytes);
    fprintf(stderr,
            "generated records=%" PRIu64 " intentional_duplicates=%" PRIu64
            " bytes=%" PRIu64 " distribution=%s seed=%" PRIu64 "\n",
            record_count, generator.duplicate_count(), file_bytes,
            options.distribution.c_str(), options.seed);
    return EXIT_SUCCESS;
}
