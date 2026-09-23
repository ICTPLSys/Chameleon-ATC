#include "memcached_protocol.h"

#include <algorithm>
#include <stdexcept>

namespace mcgen {
namespace {

constexpr std::uint8_t kBinaryRequestMagic = 0x80;
constexpr std::uint8_t kBinaryResponseMagic = 0x81;

void write_u16_be(std::uint8_t *output, std::uint16_t value) noexcept {
  output[0] = static_cast<std::uint8_t>(value >> 8);
  output[1] = static_cast<std::uint8_t>(value);
}

void write_u32_be(std::uint8_t *output, std::uint32_t value) noexcept {
  output[0] = static_cast<std::uint8_t>(value >> 24);
  output[1] = static_cast<std::uint8_t>(value >> 16);
  output[2] = static_cast<std::uint8_t>(value >> 8);
  output[3] = static_cast<std::uint8_t>(value);
}

std::uint16_t read_u16_be(const std::uint8_t *input) noexcept {
  return static_cast<std::uint16_t>(
      (static_cast<std::uint16_t>(input[0]) << 8) |
      static_cast<std::uint16_t>(input[1]));
}

std::uint32_t read_u32_be(const std::uint8_t *input) noexcept {
  return (static_cast<std::uint32_t>(input[0]) << 24) |
         (static_cast<std::uint32_t>(input[1]) << 16) |
         (static_cast<std::uint32_t>(input[2]) << 8) |
         static_cast<std::uint32_t>(input[3]);
}

std::uint64_t read_u64_be(const std::uint8_t *input) noexcept {
  return (static_cast<std::uint64_t>(read_u32_be(input)) << 32) |
         static_cast<std::uint64_t>(read_u32_be(input + 4));
}

bool is_supported_opcode(Opcode opcode) noexcept {
  return opcode == Opcode::Get || opcode == Opcode::Set ||
         opcode == Opcode::Delete;
}

} // namespace

MemcachedProtocol::MemcachedProtocol(std::size_t value_size,
                                     std::uint8_t value_byte)
    : value_size_(value_size), value_byte_(value_byte) {
  if (value_size > kMaximumValueSize) {
    throw std::length_error(
        "Memcached UDP SET value cannot fit in one UDP datagram");
  }
  value_buffer_.assign(value_size_, value_byte_);
}

EncodeError MemcachedProtocol::encode_request_prefix(
    Opcode opcode, std::string_view key, std::uint16_t request_id,
    std::uint32_t opaque, std::vector<std::uint8_t> &output) const {
  output.clear();

  if (!is_supported_opcode(opcode)) {
    return EncodeError::UnsupportedOpcode;
  }
  if (key.empty()) {
    return EncodeError::EmptyKey;
  }
  if (key.size() > kMaxMemcachedKeySize) {
    return EncodeError::KeyTooLong;
  }

  const bool is_set = opcode == Opcode::Set;
  const std::size_t extras_size = is_set ? kSetExtrasSize : 0;
  const std::size_t prefix_size =
      kUdpHeaderSize + kBinaryHeaderSize + extras_size + key.size();
  const std::size_t wire_size = prefix_size + (is_set ? value_size_ : 0);
  if (wire_size > kMaxUdpPayloadSize) {
    return EncodeError::DatagramTooLarge;
  }

  output.assign(prefix_size, 0);

  // Memcached UDP framing: request id, sequence, total, reserved.
  write_u16_be(output.data(), request_id);
  write_u16_be(output.data() + 2, 0);
  write_u16_be(output.data() + 4, 1);
  write_u16_be(output.data() + 6, 0);

  std::uint8_t *binary = output.data() + kUdpHeaderSize;
  binary[0] = kBinaryRequestMagic;
  binary[1] = static_cast<std::uint8_t>(opcode);
  write_u16_be(binary + 2, static_cast<std::uint16_t>(key.size()));
  binary[4] = static_cast<std::uint8_t>(extras_size);
  binary[5] = 0;               // Raw bytes data type.
  write_u16_be(binary + 6, 0); // VBucket id.

  const std::size_t body_size =
      extras_size + key.size() + (is_set ? value_size_ : 0);
  write_u32_be(binary + 8, static_cast<std::uint32_t>(body_size));
  write_u32_be(binary + 12, opaque);
  // CAS (binary+16..23), SET flags, and SET expiration remain zero.

  const std::size_t key_offset =
      kUdpHeaderSize + kBinaryHeaderSize + extras_size;
  const auto key_position =
      static_cast<std::vector<std::uint8_t>::difference_type>(key_offset);
  std::copy(key.begin(), key.end(), output.begin() + key_position);
  return EncodeError::None;
}

EncodeError MemcachedProtocol::encode(Opcode opcode, std::string_view key,
                                      std::uint16_t request_id,
                                      std::uint32_t opaque,
                                      std::vector<std::uint8_t> &output) const {
  const EncodeError error =
      encode_request_prefix(opcode, key, request_id, opaque, output);
  if (error != EncodeError::None) {
    return error;
  }
  if (opcode == Opcode::Set) {
    output.insert(output.end(), value_buffer_.begin(), value_buffer_.end());
  }
  return EncodeError::None;
}

EncodeError
MemcachedProtocol::encode_get(std::string_view key, std::uint16_t request_id,
                              std::uint32_t opaque,
                              std::vector<std::uint8_t> &output) const {
  return encode(Opcode::Get, key, request_id, opaque, output);
}

EncodeError
MemcachedProtocol::encode_set(std::string_view key, std::uint16_t request_id,
                              std::uint32_t opaque,
                              std::vector<std::uint8_t> &output) const {
  return encode(Opcode::Set, key, request_id, opaque, output);
}

EncodeError
MemcachedProtocol::encode_delete(std::string_view key, std::uint16_t request_id,
                                 std::uint32_t opaque,
                                 std::vector<std::uint8_t> &output) const {
  return encode(Opcode::Delete, key, request_id, opaque, output);
}

ParseError
parse_udp_response_fragment(const std::uint8_t *data, std::size_t length,
                            ParsedResponseFragment &output) noexcept {
  output = ParsedResponseFragment{};
  if (data == nullptr) {
    return ParseError::NullData;
  }
  if (length < kUdpHeaderSize) {
    return ParseError::DatagramTooShort;
  }
  if (length > kMaxUdpPayloadSize) {
    return ParseError::DatagramTooLarge;
  }

  output.udp.request_id = read_u16_be(data);
  output.udp.sequence = read_u16_be(data + 2);
  output.udp.total = read_u16_be(data + 4);
  output.udp.reserved = read_u16_be(data + 6);
  output.protocol_payload_size = length - kUdpHeaderSize;

  if (output.udp.reserved != 0) {
    return ParseError::ReservedNotZero;
  }
  if (output.udp.total == 0) {
    return ParseError::InvalidTotal;
  }
  if (output.udp.sequence >= output.udp.total) {
    return ParseError::InvalidSequence;
  }

  if (output.udp.sequence != 0) {
    return ParseError::None;
  }
  if (output.protocol_payload_size < kBinaryHeaderSize) {
    return ParseError::FirstFragmentTooShort;
  }

  const std::uint8_t *binary = data + kUdpHeaderSize;
  if (binary[0] != kBinaryResponseMagic) {
    return ParseError::InvalidResponseMagic;
  }

  output.has_binary_header = true;
  output.binary.opcode = binary[1];
  output.binary.key_length = read_u16_be(binary + 2);
  output.binary.extras_length = binary[4];
  output.binary.data_type = binary[5];
  output.binary.status = read_u16_be(binary + 6);
  output.binary.total_body_length = read_u32_be(binary + 8);
  output.binary.opaque = read_u32_be(binary + 12);
  output.binary.cas = read_u64_be(binary + 16);
  return ParseError::None;
}

void ResponseReassembler::reset(std::uint16_t expected_request_id,
                                std::uint32_t expected_opaque) {
  active_ = true;
  header_seen_ = false;
  completion_reported_ = false;
  expected_request_id_ = expected_request_id;
  expected_opaque_ = expected_opaque;
  total_fragments_ = 0;
  received_fragments_ = 0;
  inline_bitmap_ = 0;
  extended_bitmap_.reset();
  response_header_ = BinaryResponseHeader{};
}

void ResponseReassembler::clear() noexcept {
  active_ = false;
  header_seen_ = false;
  completion_reported_ = false;
  expected_request_id_ = 0;
  expected_opaque_ = 0;
  total_fragments_ = 0;
  received_fragments_ = 0;
  inline_bitmap_ = 0;
  extended_bitmap_.reset();
  response_header_ = BinaryResponseHeader{};
}

void ResponseReassembler::initialize_bitmap(std::uint16_t total) {
  total_fragments_ = total;
  inline_bitmap_ = 0;
  extended_bitmap_.reset();
  if (total > 64) {
    const std::size_t words = (static_cast<std::size_t>(total) + 63) / 64;
    extended_bitmap_ = std::make_unique<std::uint64_t[]>(words);
    std::fill_n(extended_bitmap_.get(), words, std::uint64_t{0});
  }
}

bool ResponseReassembler::fragment_seen(std::uint16_t sequence) const noexcept {
  if (total_fragments_ <= 64) {
    return (inline_bitmap_ & (std::uint64_t{1} << sequence)) != 0;
  }
  const std::size_t word = sequence / 64;
  const std::size_t bit = sequence % 64;
  return (extended_bitmap_[word] & (std::uint64_t{1} << bit)) != 0;
}

void ResponseReassembler::mark_fragment_seen(std::uint16_t sequence) noexcept {
  if (total_fragments_ <= 64) {
    inline_bitmap_ |= std::uint64_t{1} << sequence;
    return;
  }
  const std::size_t word = sequence / 64;
  const std::size_t bit = sequence % 64;
  extended_bitmap_[word] |= std::uint64_t{1} << bit;
}

ReassemblyResult ResponseReassembler::accept(const std::uint8_t *data,
                                             std::size_t length) {
  if (!active_) {
    return {ReassemblyCode::Inactive, ParseError::None};
  }
  if (completion_reported_) {
    return {ReassemblyCode::AlreadyComplete, ParseError::None};
  }

  ParsedResponseFragment fragment;
  const ParseError parse_error =
      parse_udp_response_fragment(data, length, fragment);
  if (parse_error != ParseError::None) {
    return {ReassemblyCode::ParseError, parse_error};
  }
  if (fragment.udp.request_id != expected_request_id_) {
    return {ReassemblyCode::RequestIdMismatch, ParseError::None};
  }
  if (fragment.udp.total > kMaxFragments) {
    return {ReassemblyCode::FragmentLimitExceeded, ParseError::None};
  }

  // Reject a stale sequence-zero fragment before it can initialize or mutate
  // this request's reassembly state.
  if (fragment.udp.sequence == 0 &&
      fragment.binary.opaque != expected_opaque_) {
    return {ReassemblyCode::OpaqueMismatch, ParseError::None};
  }

  if (total_fragments_ == 0) {
    initialize_bitmap(fragment.udp.total);
  } else if (fragment.udp.total != total_fragments_) {
    return {ReassemblyCode::TotalMismatch, ParseError::None};
  }

  // Only fragment zero carries the binary header and therefore the opaque.
  // Nonzero fragments may arrive first and are retained in the bitmap.
  if (fragment_seen(fragment.udp.sequence)) {
    return {ReassemblyCode::Duplicate, ParseError::None};
  }

  if (fragment.udp.sequence == 0) {
    response_header_ = fragment.binary;
    header_seen_ = true;
  }
  mark_fragment_seen(fragment.udp.sequence);
  ++received_fragments_;

  if (header_seen_ && received_fragments_ == total_fragments_) {
    completion_reported_ = true;
    return {ReassemblyCode::Complete, ParseError::None};
  }
  return {ReassemblyCode::Accepted, ParseError::None};
}

} // namespace mcgen
