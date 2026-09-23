#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string_view>
#include <vector>

namespace mcgen {

constexpr std::size_t kUdpHeaderSize = 8;
constexpr std::size_t kBinaryHeaderSize = 24;
constexpr std::size_t kSetExtrasSize = 8;
constexpr std::size_t kMaxUdpPayloadSize = 65507;
constexpr std::size_t kMaxMemcachedKeySize = 250;

enum class Opcode : std::uint8_t {
  Get = 0x00,
  Set = 0x01,
  Delete = 0x04,
};

enum class EncodeError {
  None,
  EmptyKey,
  KeyTooLong,
  DatagramTooLarge,
  UnsupportedOpcode,
};

// Encodes one Memcached binary request in one Memcached UDP frame. The SET
// value length is configured once and is deliberately not supplied per call:
// trace object sizes must not change the generated value size.
class MemcachedProtocol {
public:
  static constexpr std::size_t kMaximumValueSize =
      kMaxUdpPayloadSize - kUdpHeaderSize - kBinaryHeaderSize - kSetExtrasSize -
      1; // A valid Memcached key has at least one byte.

  // Throws std::length_error if even a one-byte key plus this value cannot
  // fit in the maximum legal UDP payload.
  explicit MemcachedProtocol(std::size_t value_size,
                             std::uint8_t value_byte = 0);

  std::size_t value_size() const noexcept { return value_size_; }
  std::uint8_t value_byte() const noexcept { return value_byte_; }
  const std::vector<std::uint8_t> &value_buffer() const noexcept {
    return value_buffer_;
  }

  // For SET this writes the UDP header, binary header, extras, and key, but
  // not the value. The binary body length still includes value_size(). A
  // worker can submit the returned prefix and value_buffer() as two iovecs in
  // one sendmsg/sendmmsg call, avoiding a per-request value copy. GET and
  // DELETE prefixes are already complete requests.
  EncodeError encode_request_prefix(Opcode opcode, std::string_view key,
                                    std::uint16_t request_id,
                                    std::uint32_t opaque,
                                    std::vector<std::uint8_t> &output) const;

  EncodeError encode_get(std::string_view key, std::uint16_t request_id,
                         std::uint32_t opaque,
                         std::vector<std::uint8_t> &output) const;
  EncodeError encode_set(std::string_view key, std::uint16_t request_id,
                         std::uint32_t opaque,
                         std::vector<std::uint8_t> &output) const;
  EncodeError encode_delete(std::string_view key, std::uint16_t request_id,
                            std::uint32_t opaque,
                            std::vector<std::uint8_t> &output) const;

private:
  EncodeError encode(Opcode opcode, std::string_view key,
                     std::uint16_t request_id, std::uint32_t opaque,
                     std::vector<std::uint8_t> &output) const;

  std::size_t value_size_;
  std::uint8_t value_byte_;
  std::vector<std::uint8_t> value_buffer_;
};

struct UdpFragmentHeader {
  std::uint16_t request_id = 0;
  std::uint16_t sequence = 0;
  std::uint16_t total = 0;
  std::uint16_t reserved = 0;
};

struct BinaryResponseHeader {
  std::uint8_t opcode = 0;
  std::uint16_t key_length = 0;
  std::uint8_t extras_length = 0;
  std::uint8_t data_type = 0;
  std::uint16_t status = 0;
  std::uint32_t total_body_length = 0;
  std::uint32_t opaque = 0;
  std::uint64_t cas = 0;
};

enum class ParseError {
  None,
  NullData,
  DatagramTooShort,
  DatagramTooLarge,
  ReservedNotZero,
  InvalidTotal,
  InvalidSequence,
  FirstFragmentTooShort,
  InvalidResponseMagic,
};

struct ParsedResponseFragment {
  UdpFragmentHeader udp;
  std::size_t protocol_payload_size = 0;
  bool has_binary_header = false;
  BinaryResponseHeader binary;
};

// Only sequence zero contains the beginning of the binary response, so only
// that fragment has has_binary_header=true and a parsed BinaryResponseHeader.
ParseError parse_udp_response_fragment(const std::uint8_t *data,
                                       std::size_t length,
                                       ParsedResponseFragment &output) noexcept;

enum class ReassemblyCode {
  Accepted,
  Duplicate,
  Complete,
  AlreadyComplete,
  Inactive,
  ParseError,
  RequestIdMismatch,
  OpaqueMismatch,
  TotalMismatch,
  FragmentLimitExceeded,
};

struct ReassemblyResult {
  ReassemblyCode code = ReassemblyCode::Inactive;
  ParseError parse_error = ParseError::None;
};

// Tracks response fragments without retaining any response payload or value.
// Up to 64 fragments use the inline bitmap. Larger valid responses allocate
// only a bitmap. The cap prevents an untrusted datagram from causing a large
// allocation.
class ResponseReassembler {
public:
  static constexpr std::uint16_t kMaxFragments = 4096;

  ResponseReassembler() = default;
  ResponseReassembler(const ResponseReassembler &) = delete;
  ResponseReassembler &operator=(const ResponseReassembler &) = delete;
  ResponseReassembler(ResponseReassembler &&) noexcept = default;
  ResponseReassembler &operator=(ResponseReassembler &&) noexcept = default;

  void reset(std::uint16_t expected_request_id, std::uint32_t expected_opaque);
  void clear() noexcept;

  ReassemblyResult accept(const std::uint8_t *data, std::size_t length);

  bool active() const noexcept { return active_; }
  bool complete() const noexcept { return completion_reported_; }
  bool has_response_header() const noexcept { return header_seen_; }
  std::uint16_t expected_request_id() const noexcept {
    return expected_request_id_;
  }
  std::uint32_t expected_opaque() const noexcept { return expected_opaque_; }
  std::uint16_t total_fragments() const noexcept { return total_fragments_; }
  std::uint16_t received_fragments() const noexcept {
    return received_fragments_;
  }
  const BinaryResponseHeader *response_header() const noexcept {
    return header_seen_ ? &response_header_ : nullptr;
  }

private:
  void initialize_bitmap(std::uint16_t total);
  bool fragment_seen(std::uint16_t sequence) const noexcept;
  void mark_fragment_seen(std::uint16_t sequence) noexcept;

  bool active_ = false;
  bool header_seen_ = false;
  bool completion_reported_ = false;
  std::uint16_t expected_request_id_ = 0;
  std::uint32_t expected_opaque_ = 0;
  std::uint16_t total_fragments_ = 0;
  std::uint16_t received_fragments_ = 0;
  std::uint64_t inline_bitmap_ = 0;
  std::unique_ptr<std::uint64_t[]> extended_bitmap_;
  BinaryResponseHeader response_header_;
};

} // namespace mcgen
