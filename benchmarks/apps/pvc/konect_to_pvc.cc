#include "pvc_format.hh"

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>

namespace {

struct Options {
    std::string input;
    std::string output;
    uint64_t seed;

    Options() : input("-"), seed(1) {}
};

void usage(FILE *stream, const char *program) {
    fprintf(stream,
            "Usage: %s [--input FILE|-] --output FILE [--seed N]\n"
            "\n"
            "Convert a KONECT edge list to the PVC binary format in one streaming\n"
            "pass.  For every `src dst` edge the proxy mapping is:\n"
            "\n"
            "    URL=dst, IP=src, Cookie=stable_hash(seed, src)\n"
            "\n"
            "This is a KONECT-derived proxy, not a real page-view log.  Blank lines\n"
            "and lines beginning with %% or # are ignored.  Additional columns are\n"
            "ignored.  Standard input is used when --input is omitted or is -.\n"
            "\n"
            "Options:\n"
            "  -i, --input FILE     KONECT text file (default -)\n"
            "  -o, --output FILE    output PVC file (required; must be seekable)\n"
            "      --seed N         deterministic cookie hash seed (default 1)\n"
            "  -h, --help           show this help\n",
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
        } else if (arg == "-i" || arg == "--input") {
            if (!need_value(argc, argv, &i, &value))
                return false;
            options->input = value;
        } else if (arg == "-o" || arg == "--output") {
            if (!need_value(argc, argv, &i, &value))
                return false;
            options->output = value;
        } else if (arg == "--seed") {
            if (!need_value(argc, argv, &i, &value) ||
                !parse_u64(value, &options->seed)) {
                fprintf(stderr, "invalid --seed value\n");
                return false;
            }
        } else {
            fprintf(stderr, "unknown option: %s\n", arg.c_str());
            return false;
        }
    }
    if (options->output.empty() || options->output == "-") {
        fprintf(stderr, "--output must name a seekable file\n");
        return false;
    }
    return true;
}

uint64_t stable_cookie(uint64_t seed, uint64_t source) {
    uint64_t value = source ^ (seed + UINT64_C(0x9e3779b97f4a7c15));
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

bool parse_id(const std::string &token, uint64_t *value) {
    return parse_u64(token.c_str(), value);
}

bool parse_edge(const std::string &line,
                uint64_t line_number,
                bool *has_edge,
                uint64_t *source,
                uint64_t *destination,
                std::string *error) {
    *has_edge = false;
    const std::string::size_type first = line.find_first_not_of(" \t\r\n");
    if (first == std::string::npos || line[first] == '%' || line[first] == '#')
        return true;

    std::istringstream fields(line.substr(first));
    std::string source_text;
    std::string destination_text;
    if (!(fields >> source_text >> destination_text) ||
        !parse_id(source_text, source) || !parse_id(destination_text, destination)) {
        std::ostringstream message;
        message << "invalid KONECT edge at line " << line_number
                << ": expected two unsigned integer IDs";
        *error = message.str();
        return false;
    }
    *has_edge = true;
    return true;
}

bool domain_size(uint64_t maximum,
                 bool saw_value,
                 const char *name,
                 uint64_t *size,
                 std::string *error) {
    if (!saw_value) {
        *size = 0;
        return true;
    }
    if (maximum == std::numeric_limits<uint64_t>::max()) {
        *error = std::string(name) + " ID is too large to describe its domain";
        return false;
    }
    // KONECT IDs are commonly one-based and can be sparse.  max+1 is an upper
    // bound that keeps every preserved ID inside [0, domain_size).
    *size = maximum + 1;
    return true;
}

bool make_temporary(const std::string &output,
                    std::string *temporary,
                    FILE **file,
                    std::string *error) {
    std::ostringstream path;
    path << output << ".tmp." << static_cast<unsigned long>(getpid());
    *temporary = path.str();
    const int descriptor = open(temporary->c_str(), O_RDWR | O_CREAT | O_EXCL, 0666);
    if (descriptor < 0) {
        *error = std::string("cannot create temporary output ") + *temporary +
                 ": " + strerror(errno);
        return false;
    }
    *file = fdopen(descriptor, "w+b");
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

    std::ifstream input_file;
    std::istream *input = &std::cin;
    if (options.input != "-") {
        input_file.open(options.input.c_str());
        if (!input_file) {
            fprintf(stderr, "cannot open %s: %s\n", options.input.c_str(),
                    strerror(errno));
            return EXIT_FAILURE;
        }
        input = &input_file;
    }

    std::string temporary;
    std::string error;
    FILE *output = NULL;
    if (!make_temporary(options.output, &temporary, &output, &error)) {
        fprintf(stderr, "%s\n", error.c_str());
        return EXIT_FAILURE;
    }

    const pvc::FileHeader placeholder = pvc::make_header(0, 0, 0, 0, options.seed);
    bool ok = pvc::write_header(output, placeholder, &error);
    uint64_t record_count = 0;
    uint64_t max_url = 0;
    uint64_t max_ip = 0;
    bool saw_record = false;
    uint64_t line_number = 0;
    std::string line;
    while (ok && std::getline(*input, line)) {
        ++line_number;
        bool has_edge = false;
        uint64_t source = 0;
        uint64_t destination = 0;
        if (!parse_edge(line, line_number, &has_edge, &source, &destination, &error)) {
            ok = false;
            break;
        }
        if (!has_edge)
            continue;
        if (record_count == std::numeric_limits<uint64_t>::max()) {
            error = "too many KONECT edges for the PVC format";
            ok = false;
            break;
        }
        pvc::Record record;
        record.url_id = destination;
        record.ip_id = source;
        record.cookie_id = stable_cookie(options.seed, source);
        if (fwrite(&record, sizeof(record), 1, output) != 1) {
            error = std::string("failed to write PVC record: ") + strerror(errno);
            ok = false;
            break;
        }
        ++record_count;
        max_url = saw_record ? std::max(max_url, destination) : destination;
        max_ip = saw_record ? std::max(max_ip, source) : source;
        saw_record = true;
    }
    if (ok && input->bad()) {
        error = std::string("failed while reading ") + options.input;
        ok = false;
    }

    uint64_t url_count = 0;
    uint64_t ip_count = 0;
    if (ok)
        ok = domain_size(max_url, saw_record, "URL", &url_count, &error) &&
             domain_size(max_ip, saw_record, "IP", &ip_count, &error);
    if (ok) {
        // The stable hash deliberately preserves no compact numeric range.
        // cookie_count=0 records that the cookie domain is non-compact/unknown;
        // consumers must treat cookie_id as an opaque 64-bit identifier.
        const pvc::FileHeader header = pvc::make_header(
            record_count, url_count, ip_count, 0, options.seed);
        if (fseek(output, 0, SEEK_SET) != 0) {
            error = std::string("cannot seek to the PVC header: ") + strerror(errno);
            ok = false;
        } else {
            ok = pvc::write_header(output, header, &error);
        }
    }
    if (ok && fflush(output) != 0) {
        error = std::string("failed to flush output: ") + strerror(errno);
        ok = false;
    }
    if (fclose(output) != 0 && ok) {
        error = std::string("failed to close output: ") + strerror(errno);
        ok = false;
    }

    if (ok && rename(temporary.c_str(), options.output.c_str()) != 0) {
        error = std::string("cannot install output file ") + options.output +
                ": " + strerror(errno);
        ok = false;
    }
    if (!ok) {
        unlink(temporary.c_str());
        fprintf(stderr, "%s\n", error.c_str());
        return EXIT_FAILURE;
    }

    fprintf(stderr,
            "converted proxy_records=%" PRIu64 " url_domain=%" PRIu64
            " ip_domain=%" PRIu64 " seed=%" PRIu64 " output=%s\n",
            record_count, url_count, ip_count, options.seed, options.output.c_str());
    return EXIT_SUCCESS;
}
