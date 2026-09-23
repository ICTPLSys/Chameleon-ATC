#include "memcached_protocol.h"

#include <cstdint>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

int failures = 0;

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      std::cerr << __FILE__ << ':' << __LINE__                                 \
                << ": CHECK failed: " << #condition << '\n';                   \
      ++failures;                                                              \
    }                                                                          \
  } while (false)

void put_u16(std::vector<std::uint8_t> &bytes, std::size_t offset,
             std::uint16_t value) {
  bytes[offset] = static_cast<std::uint8_t>(value >> 8);
  bytes[offset + 1] = static_cast<std::uint8_t>(value);
}

void put_u32(std::vector<std::uint8_t> &bytes, std::size_t offset,
             std::uint32_t value) {
  bytes[offset] = static_cast<std::uint8_t>(value >> 24);
  bytes[offset + 1] = static_cast<std::uint8_t>(value >> 16);
  bytes[offset + 2] = static_cast<std::uint8_t>(value >> 8);
  bytes[offset + 3] = static_cast<std::uint8_t>(value);
}

void put_u64(std::vector<std::uint8_t> &bytes, std::size_t offset,
             std::uint64_t value) {
  put_u32(bytes, offset, static_cast<std::uint32_t>(value >> 32));
  put_u32(bytes, offset + 4, static_cast<std::uint32_t>(value));
}

std::vector<std::uint8_t> make_binary_response(std::uint8_t opcode,
                                               std::uint16_t status,
                                               std::uint32_t opaque,
                                               std::size_t body_size,
                                               std::uint64_t cas = 0) {
  std::vector<std::uint8_t> response(mcgen::kBinaryHeaderSize + body_size, 0);
  response[0] = 0x81;
  response[1] = opcode;
  response[4] = body_size == 0 ? 0 : 4;
  put_u16(response, 6, status);
  put_u32(response, 8, static_cast<std::uint32_t>(body_size));
  put_u32(response, 12, opaque);
  put_u64(response, 16, cas);
  for (std::size_t i = mcgen::kBinaryHeaderSize; i < response.size(); ++i) {
    response[i] = static_cast<std::uint8_t>(i);
  }
  return response;
}

std::vector<std::uint8_t>
make_udp_fragment(std::uint16_t request_id, std::uint16_t sequence,
                  std::uint16_t total, const std::vector<std::uint8_t> &payload,
                  std::size_t begin, std::size_t end,
                  std::uint16_t reserved = 0) {
  std::vector<std::uint8_t> datagram(mcgen::kUdpHeaderSize + end - begin, 0);
  put_u16(datagram, 0, request_id);
  put_u16(datagram, 2, sequence);
  put_u16(datagram, 4, total);
  put_u16(datagram, 6, reserved);
  for (std::size_t i = begin; i < end; ++i) {
    datagram[mcgen::kUdpHeaderSize + i - begin] = payload[i];
  }
  return datagram;
}

void test_get_and_delete_wire_bytes() {
  mcgen::MemcachedProtocol protocol(4, 0xab);
  std::vector<std::uint8_t> output;

  CHECK(protocol.encode_get("abc", 0x1234, 0x01020304, output) ==
        mcgen::EncodeError::None);
  const std::vector<std::uint8_t> expected_get = {
      0x12, 0x34, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x80, 0x00, 0x00, 0x03,
      0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0x01, 0x02, 0x03, 0x04,
      0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 'a',  'b',  'c'};
  CHECK(output == expected_get);

  CHECK(protocol.encode_delete("z", 0xabcd, 0x10203040, output) ==
        mcgen::EncodeError::None);
  CHECK(output.size() == mcgen::kUdpHeaderSize + mcgen::kBinaryHeaderSize + 1);
  CHECK(output[0] == 0xab && output[1] == 0xcd);
  CHECK(output[8] == 0x80);
  CHECK(output[9] == 0x04);
  CHECK(output[10] == 0x00 && output[11] == 0x01);
  CHECK(output[19] == 0x01); // Binary body length is one byte.
  CHECK(output[20] == 0x10 && output[21] == 0x20 && output[22] == 0x30 &&
        output[23] == 0x40);
  CHECK(output.back() == 'z');
}

void test_set_fixed_value_and_prefix() {
  // There is no trace-size argument on encode_set: every SET from this
  // protocol instance carries exactly the configured four-byte value.
  mcgen::MemcachedProtocol protocol(4, 0xab);
  std::vector<std::uint8_t> prefix;
  CHECK(protocol.encode_request_prefix(mcgen::Opcode::Set, "k", 7, 0x11223344,
                                       prefix) == mcgen::EncodeError::None);
  CHECK(prefix.size() == 8 + 24 + 8 + 1);
  CHECK(prefix[8] == 0x80 && prefix[9] == 0x01);
  CHECK(prefix[12] == 8);  // Extras length.
  CHECK(prefix[19] == 13); // 8 extras + 1 key + 4 value.
  CHECK(prefix[40] == 'k');
  CHECK(protocol.value_buffer() ==
        std::vector<std::uint8_t>({0xab, 0xab, 0xab, 0xab}));

  std::vector<std::uint8_t> complete;
  CHECK(protocol.encode_set("k", 7, 0x11223344, complete) ==
        mcgen::EncodeError::None);
  CHECK(complete.size() == prefix.size() + protocol.value_size());
  const auto prefix_end =
      static_cast<std::vector<std::uint8_t>::difference_type>(prefix.size());
  CHECK(std::vector<std::uint8_t>(complete.begin(),
                                  complete.begin() + prefix_end) == prefix);
  CHECK(std::vector<std::uint8_t>(complete.end() - 4, complete.end()) ==
        protocol.value_buffer());

  CHECK(protocol.encode_request_prefix(mcgen::Opcode::Get, "k", 8, 9, prefix) ==
        mcgen::EncodeError::None);
  CHECK(prefix.size() == 8 + 24 + 1); // GET prefix is the whole request.

  // The configured value is an exact override, not a cap derived from a
  // trace row. Exercise the important empty/default/large boundaries.
  for (const std::size_t value_size :
       {std::size_t{0}, std::size_t{1}, std::size_t{4096},
        std::size_t{65'217}}) {
    mcgen::MemcachedProtocol exact(value_size, 0x5a);
    CHECK(exact.encode_request_prefix(mcgen::Opcode::Set, "k", 1, 2, prefix) ==
          mcgen::EncodeError::None);
    CHECK(exact.value_buffer().size() == value_size);
    CHECK(prefix.size() + value_size == 8 + 24 + 8 + 1 + value_size);
    const std::uint32_t body_length =
        (static_cast<std::uint32_t>(prefix[16]) << 24) |
        (static_cast<std::uint32_t>(prefix[17]) << 16) |
        (static_cast<std::uint32_t>(prefix[18]) << 8) |
        static_cast<std::uint32_t>(prefix[19]);
    CHECK(body_length == 8 + 1 + value_size);
  }
}

void test_request_validation() {
  mcgen::MemcachedProtocol protocol(1);
  std::vector<std::uint8_t> output = {1, 2, 3};
  CHECK(protocol.encode_get("", 0, 0, output) == mcgen::EncodeError::EmptyKey);
  CHECK(output.empty());

  const std::string long_key(mcgen::kMaxMemcachedKeySize + 1, 'x');
  CHECK(protocol.encode_set(long_key, 0, 0, output) ==
        mcgen::EncodeError::KeyTooLong);

  mcgen::MemcachedProtocol maximum(mcgen::MemcachedProtocol::kMaximumValueSize);
  CHECK(maximum.encode_request_prefix(mcgen::Opcode::Set, "x", 0, 0, output) ==
        mcgen::EncodeError::None);
  CHECK(output.size() + maximum.value_buffer().size() ==
        mcgen::kMaxUdpPayloadSize);
  CHECK(maximum.encode_request_prefix(mcgen::Opcode::Set, "xx", 0, 0, output) ==
        mcgen::EncodeError::DatagramTooLarge);
  CHECK(output.empty());

  bool threw = false;
  try {
    mcgen::MemcachedProtocol too_large(
        mcgen::MemcachedProtocol::kMaximumValueSize + 1);
    (void)too_large;
  } catch (const std::length_error &) {
    threw = true;
  }
  CHECK(threw);
}

void test_single_fragment_response() {
  constexpr std::uint16_t request_id = 0x3456;
  constexpr std::uint32_t opaque = 0x89abcdef;
  constexpr std::uint64_t cas = 0x0102030405060708ULL;
  const auto binary = make_binary_response(0x00, 0x0001, opaque, 0, cas);
  const auto datagram =
      make_udp_fragment(request_id, 0, 1, binary, 0, binary.size());

  mcgen::ParsedResponseFragment parsed;
  CHECK(mcgen::parse_udp_response_fragment(datagram.data(), datagram.size(),
                                           parsed) == mcgen::ParseError::None);
  CHECK(parsed.udp.request_id == request_id);
  CHECK(parsed.udp.sequence == 0 && parsed.udp.total == 1);
  CHECK(parsed.has_binary_header);
  CHECK(parsed.binary.opcode == 0x00);
  CHECK(parsed.binary.status == 0x0001);
  CHECK(parsed.binary.opaque == opaque);
  CHECK(parsed.binary.cas == cas);

  mcgen::ResponseReassembler reassembler;
  CHECK(reassembler.accept(datagram.data(), datagram.size()).code ==
        mcgen::ReassemblyCode::Inactive);
  reassembler.reset(request_id, opaque);
  CHECK(reassembler.accept(datagram.data(), datagram.size()).code ==
        mcgen::ReassemblyCode::Complete);
  CHECK(reassembler.complete());
  CHECK(reassembler.received_fragments() == 1);
  CHECK(reassembler.response_header() != nullptr);
  CHECK(reassembler.response_header()->status == 0x0001);
  CHECK(reassembler.accept(datagram.data(), datagram.size()).code ==
        mcgen::ReassemblyCode::AlreadyComplete);
}

void test_multi_fragment_out_of_order_and_duplicate() {
  constexpr std::uint16_t request_id = 19;
  constexpr std::uint32_t opaque = 0x12345678;
  const auto binary = make_binary_response(0x00, 0, opaque, 20);
  CHECK(binary.size() == 44);

  const auto fragment0 = make_udp_fragment(request_id, 0, 3, binary, 0, 25);
  const auto fragment1 = make_udp_fragment(request_id, 1, 3, binary, 25, 35);
  const auto fragment2 = make_udp_fragment(request_id, 2, 3, binary, 35, 44);

  mcgen::ResponseReassembler reassembler;
  reassembler.reset(request_id, opaque);
  CHECK(reassembler.accept(fragment2.data(), fragment2.size()).code ==
        mcgen::ReassemblyCode::Accepted);
  CHECK(!reassembler.has_response_header());
  CHECK(reassembler.accept(fragment2.data(), fragment2.size()).code ==
        mcgen::ReassemblyCode::Duplicate);
  CHECK(reassembler.received_fragments() == 1);
  CHECK(reassembler.accept(fragment1.data(), fragment1.size()).code ==
        mcgen::ReassemblyCode::Accepted);
  CHECK(reassembler.accept(fragment0.data(), fragment0.size()).code ==
        mcgen::ReassemblyCode::Complete);
  CHECK(reassembler.received_fragments() == 3);
  CHECK(reassembler.response_header() != nullptr);
  CHECK(reassembler.response_header()->opaque == opaque);
  CHECK(reassembler.accept(fragment1.data(), fragment1.size()).code ==
        mcgen::ReassemblyCode::AlreadyComplete);
}

void test_invalid_fragments_and_mismatches() {
  constexpr std::uint16_t request_id = 5;
  constexpr std::uint32_t opaque = 9;
  const auto binary = make_binary_response(0x04, 0, opaque, 0);

  mcgen::ParsedResponseFragment parsed;
  CHECK(mcgen::parse_udp_response_fragment(nullptr, 0, parsed) ==
        mcgen::ParseError::NullData);

  auto invalid_total =
      make_udp_fragment(request_id, 0, 0, binary, 0, binary.size());
  CHECK(mcgen::parse_udp_response_fragment(invalid_total.data(),
                                           invalid_total.size(), parsed) ==
        mcgen::ParseError::InvalidTotal);

  auto invalid_sequence =
      make_udp_fragment(request_id, 2, 2, binary, 0, binary.size());
  CHECK(mcgen::parse_udp_response_fragment(invalid_sequence.data(),
                                           invalid_sequence.size(), parsed) ==
        mcgen::ParseError::InvalidSequence);

  auto invalid_reserved =
      make_udp_fragment(request_id, 0, 1, binary, 0, binary.size(), 1);
  CHECK(mcgen::parse_udp_response_fragment(invalid_reserved.data(),
                                           invalid_reserved.size(), parsed) ==
        mcgen::ParseError::ReservedNotZero);

  std::vector<std::uint8_t> tiny_payload(3, 0);
  auto short_first =
      make_udp_fragment(request_id, 0, 1, tiny_payload, 0, tiny_payload.size());
  CHECK(mcgen::parse_udp_response_fragment(short_first.data(),
                                           short_first.size(), parsed) ==
        mcgen::ParseError::FirstFragmentTooShort);

  auto bad_magic =
      make_udp_fragment(request_id, 0, 1, binary, 0, binary.size());
  bad_magic[8] = 0x80;
  CHECK(mcgen::parse_udp_response_fragment(bad_magic.data(), bad_magic.size(),
                                           parsed) ==
        mcgen::ParseError::InvalidResponseMagic);

  mcgen::ResponseReassembler reassembler;
  reassembler.reset(request_id, opaque);
  auto wrong_id =
      make_udp_fragment(request_id + 1, 0, 1, binary, 0, binary.size());
  CHECK(reassembler.accept(wrong_id.data(), wrong_id.size()).code ==
        mcgen::ReassemblyCode::RequestIdMismatch);

  const auto wrong_opaque_binary = make_binary_response(0x04, 0, opaque + 1, 0);
  auto wrong_opaque = make_udp_fragment(request_id, 0, 1, wrong_opaque_binary,
                                        0, wrong_opaque_binary.size());
  CHECK(reassembler.accept(wrong_opaque.data(), wrong_opaque.size()).code ==
        mcgen::ReassemblyCode::OpaqueMismatch);

  std::vector<std::uint8_t> continuation = {0xaa};
  auto first_seen = make_udp_fragment(request_id, 1, 3, continuation, 0, 1);
  CHECK(reassembler.accept(first_seen.data(), first_seen.size()).code ==
        mcgen::ReassemblyCode::Accepted);
  auto changed_total =
      make_udp_fragment(request_id, 0, 2, binary, 0, binary.size());
  CHECK(reassembler.accept(changed_total.data(), changed_total.size()).code ==
        mcgen::ReassemblyCode::TotalMismatch);

  reassembler.reset(request_id, opaque);
  auto excessive = make_udp_fragment(
      request_id, 1, mcgen::ResponseReassembler::kMaxFragments + 1,
      continuation, 0, 1);
  CHECK(reassembler.accept(excessive.data(), excessive.size()).code ==
        mcgen::ReassemblyCode::FragmentLimitExceeded);
}

void test_extended_fragment_bitmap() {
  constexpr std::uint16_t request_id = 77;
  constexpr std::uint32_t opaque = 88;
  constexpr std::uint16_t total = 65;
  const auto binary = make_binary_response(0x00, 0, opaque, 0);
  const std::vector<std::uint8_t> continuation = {0xcc};

  mcgen::ResponseReassembler reassembler;
  reassembler.reset(request_id, opaque);
  for (std::uint16_t sequence = 1; sequence < total; ++sequence) {
    const auto fragment =
        make_udp_fragment(request_id, sequence, total, continuation, 0, 1);
    CHECK(reassembler.accept(fragment.data(), fragment.size()).code ==
          mcgen::ReassemblyCode::Accepted);
  }
  CHECK(reassembler.received_fragments() == 64);
  const auto fragment0 =
      make_udp_fragment(request_id, 0, total, binary, 0, binary.size());
  CHECK(reassembler.accept(fragment0.data(), fragment0.size()).code ==
        mcgen::ReassemblyCode::Complete);
}

} // namespace

int main() {
  test_get_and_delete_wire_bytes();
  test_set_fixed_value_and_prefix();
  test_request_validation();
  test_single_fragment_response();
  test_multi_fragment_out_of_order_and_duplicate();
  test_invalid_fragments_and_mismatches();
  test_extended_fragment_bitmap();

  if (failures != 0) {
    std::cerr << failures << " protocol test(s) failed\n";
    return 1;
  }
  std::cout << "protocol tests passed\n";
  return 0;
}
