#ifndef CHAMELEON_BENCHMARKS_APPS_PVC_FORMAT_HH_
#define CHAMELEON_BENCHMARKS_APPS_PVC_FORMAT_HH_

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <limits>
#include <string>

namespace pvc {

// PVC files intentionally use fixed-width, naturally aligned records so that
// the benchmark can mmap the input and hand record fields to Metis without
// parsing or copying them.  Files are little-endian; the marker makes an
// accidental cross-endian run fail loudly instead of producing bad results.
static const unsigned char kMagic[8] = {'P', 'V', 'C', 'B', 'I', 'N', '1', '\0'};
static const uint32_t kVersion = 1;
static const uint32_t kEndianMarker = 0x01020304U;

struct FileHeader {
    unsigned char magic[8];
    uint32_t version;
    uint32_t header_bytes;
    uint32_t record_bytes;
    uint32_t endian_marker;
    uint64_t record_count;
    uint64_t url_count;
    uint64_t ip_count;
    uint64_t cookie_count;
    uint64_t seed;
};

struct Record {
    uint64_t url_id;
    uint64_t ip_id;
    uint64_t cookie_id;
};

static_assert(sizeof(FileHeader) == 64, "PVC header must be exactly 64 bytes");
static_assert(sizeof(Record) == 24, "PVC record must be exactly 24 bytes");

inline bool host_is_little_endian() {
    const uint32_t value = 1;
    return *reinterpret_cast<const unsigned char *>(&value) == 1;
}

inline FileHeader make_header(uint64_t record_count,
                              uint64_t url_count,
                              uint64_t ip_count,
                              uint64_t cookie_count,
                              uint64_t seed) {
    FileHeader header;
    memset(&header, 0, sizeof(header));
    memcpy(header.magic, kMagic, sizeof(kMagic));
    header.version = kVersion;
    header.header_bytes = sizeof(FileHeader);
    header.record_bytes = sizeof(Record);
    header.endian_marker = kEndianMarker;
    header.record_count = record_count;
    header.url_count = url_count;
    header.ip_count = ip_count;
    header.cookie_count = cookie_count;
    header.seed = seed;
    return header;
}

inline bool expected_file_size(const FileHeader &header, uint64_t *size_out) {
    if (!size_out || header.record_bytes == 0)
        return false;
    const uint64_t max = std::numeric_limits<uint64_t>::max();
    if (header.record_count >
        (max - static_cast<uint64_t>(header.header_bytes)) /
            static_cast<uint64_t>(header.record_bytes)) {
        return false;
    }
    *size_out = static_cast<uint64_t>(header.header_bytes) +
                header.record_count * static_cast<uint64_t>(header.record_bytes);
    return true;
}

inline bool validate_header(const FileHeader &header,
                            uint64_t file_bytes,
                            std::string *error) {
    const char *message = NULL;
    if (!host_is_little_endian()) {
        message = "PVC files are little-endian but this host is not";
    } else if (memcmp(header.magic, kMagic, sizeof(kMagic)) != 0) {
        message = "bad PVC file magic";
    } else if (header.version != kVersion) {
        message = "unsupported PVC file version";
    } else if (header.header_bytes != sizeof(FileHeader)) {
        message = "unexpected PVC header size";
    } else if (header.record_bytes != sizeof(Record)) {
        message = "unexpected PVC record size";
    } else if (header.endian_marker != kEndianMarker) {
        message = "PVC file has the wrong byte order";
    }

    uint64_t expected = 0;
    if (!message && !expected_file_size(header, &expected)) {
        message = "PVC record count overflows the file size";
    } else if (!message && expected != file_bytes) {
        message = "PVC file size does not match its record count";
    }

    if (message && error)
        *error = message;
    return message == NULL;
}

inline bool write_header(FILE *file,
                         const FileHeader &header,
                         std::string *error) {
    if (!host_is_little_endian()) {
        if (error)
            *error = "PVC files can only be written on a little-endian host";
        return false;
    }
    if (fwrite(&header, sizeof(header), 1, file) != 1) {
        if (error)
            *error = std::string("failed to write PVC header: ") + strerror(errno);
        return false;
    }
    return true;
}

}  // namespace pvc

#endif  // CHAMELEON_BENCHMARKS_APPS_PVC_FORMAT_HH_
