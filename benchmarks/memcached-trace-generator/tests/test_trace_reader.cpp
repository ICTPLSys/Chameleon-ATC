#include "trace_reader.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {

using mcgen::Operation;
using mcgen::Request;
using mcgen::SharedRequest;
using mcgen::TraceReader;

void expect(bool condition, const std::string &message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

void expect_request(const Request &request, std::string key, Operation op) {
  expect(request.key == key,
         "expected key '" + key + "', got '" + request.key + "'");
  expect(request.op == op, "operation does not match");
}

void expect_request(const SharedRequest &request, std::string key,
                    Operation op) {
  expect(request.key != nullptr, "shared request has no key");
  expect(request.key->view() == key,
         "expected shared key '" + key + "', got '" +
             std::string(request.key->view()) + "'");
  expect(request.key->hash == mcgen::StableKeyHash(request.key->view()),
         "cached stable key hash does not match key bytes");
  expect(request.op == op, "shared operation does not match");
}

class TempDirectory {
public:
  TempDirectory() {
    const auto nonce =
        std::chrono::steady_clock::now().time_since_epoch().count();
    path_ = std::filesystem::temp_directory_path() /
            ("mcgen-trace-reader-" + std::to_string(nonce));
    std::filesystem::create_directory(path_);
  }

  ~TempDirectory() {
    std::error_code ignored;
    std::filesystem::remove_all(path_, ignored);
  }

  const std::filesystem::path &path() const { return path_; }

private:
  std::filesystem::path path_;
};

void write_trace(const std::filesystem::path &path, const std::string &rows) {
  std::ofstream output(path);
  expect(static_cast<bool>(output), "failed to create test trace");
  output << "key,op,size,op_count,key_size\n" << rows;
  expect(static_cast<bool>(output), "failed to write test trace");
}

std::filesystem::path tiny_trace_path() {
  return std::filesystem::path(__FILE__).parent_path() / "data" /
         "tiny_trace.csv";
}

void test_padding_op_count_and_rewind() {
  TraceReader reader(tiny_trace_path(), 1);

  expect_request(reader.next(), "abc00", Operation::Get);
  expect_request(reader.next(), "abc00", Operation::Get);
  expect_request(reader.next(), "12345678901234567", Operation::Set);
  expect_request(reader.next(), "z", Operation::Delete);
  expect_request(reader.next(), "abc00", Operation::Get);
}

void test_amplification_order_and_key_transform() {
  TraceReader reader(tiny_trace_path(), 2);

  // ampFactor is the outer expansion and op_count is the inner expansion.
  expect_request(reader.next(), "abc000000", Operation::Get);
  expect_request(reader.next(), "abc000000", Operation::Get);
  expect_request(reader.next(), "abc000001", Operation::Get);
  expect_request(reader.next(), "abc000001", Operation::Get);

  // A 17-byte key is truncated to max(17 - 4, 16), then gets a suffix.
  expect_request(reader.next(), "12345678901234560000", Operation::Set);
  expect_request(reader.next(), "12345678901234560001", Operation::Set);
}

void test_shared_key_reuse_and_lifetime() {
  expect(mcgen::StableKeyHash("") == 14695981039346656037ULL,
         "FNV-1a offset basis is incorrect");
  expect(mcgen::StableKeyHash("hello") == 0xa430d84680aabd0bULL,
         "64-bit FNV-1a known-answer test failed");

  TraceReader reader(tiny_trace_path(), 2);

  const auto first = reader.next_shared();
  const auto second = reader.next_shared();
  expect_request(first, "abc000000", Operation::Get);
  expect_request(second, "abc000000", Operation::Get);
  expect(first.key == second.key,
         "op_count repetitions did not reuse the immutable key");

  const auto next_suffix = reader.next_shared();
  expect_request(next_suffix, "abc000001", Operation::Get);
  expect(next_suffix.key != first.key,
         "different amplification suffixes unexpectedly share a key");

  // Advancing the reader must not invalidate keys already queued by a caller.
  for (int index = 0; index < 8; ++index) {
    (void)reader.next_shared();
  }
  expect_request(first, "abc000000", Operation::Get);
}

void test_bulk_skip_preserves_expansion_cursor() {
  for (const std::uint32_t amplification : {1U, 2U, 7U}) {
    for (std::uint64_t count = 0; count < 80; ++count) {
      TraceReader baseline(tiny_trace_path(), amplification);
      TraceReader skipped(tiny_trace_path(), amplification);
      for (std::uint64_t index = 0; index < count; ++index) {
        (void)baseline.next_shared();
      }
      skipped.skip(count);

      const auto expected = baseline.next_shared();
      const auto actual = skipped.next_shared();
      expect(expected.key->view() == actual.key->view(),
             "bulk skip produced the wrong key at count " +
                 std::to_string(count));
      expect(expected.op == actual.op,
             "bulk skip produced the wrong operation at count " +
                 std::to_string(count));
      expect(expected.key->hash == actual.key->hash,
             "bulk skip produced the wrong hash at count " +
                 std::to_string(count));
    }
  }
}

void test_directory_natural_order_and_rewind() {
  TempDirectory temp;
  write_trace(temp.path() / "kvcache_traces_10.csv", "ten,GET,0,1,3\n");
  write_trace(temp.path() / "kvcache_traces_2.csv", "two,SET,1,1,3\n");
  write_trace(temp.path() / "ignored.csv", "ignored,DELETE,0,1,7\n");

  TraceReader reader(temp.path(), 1);
  expect(reader.files().size() == 2, "directory filter did not select 2 files");
  expect(reader.files()[0].filename() == "kvcache_traces_2.csv",
         "natural order did not put trace 2 first");
  expect(reader.files()[1].filename() == "kvcache_traces_10.csv",
         "natural order did not put trace 10 second");

  expect_request(reader.next(), "two", Operation::Set);
  expect_request(reader.next(), "ten", Operation::Get);
  expect_request(reader.next(), "two", Operation::Set);
}

void test_explicit_file_order_and_validation() {
  TempDirectory temp;
  const auto first = temp.path() / "first.csv";
  const auto second = temp.path() / "second.csv";
  write_trace(first, "first,GET,0,1,5\n");
  write_trace(second, "second,SET,1,1,6\n");

  TraceReader reader(std::vector<std::filesystem::path>{second, first}, 1);
  expect(reader.files().size() == 2, "explicit file list changed size");
  expect(reader.files()[0] == second && reader.files()[1] == first,
         "explicit file order was not preserved");
  expect_request(reader.next_shared(), "second", Operation::Set);
  expect_request(reader.next_shared(), "first", Operation::Get);
  expect_request(reader.next_shared(), "second", Operation::Set);

  try {
    TraceReader empty(std::vector<std::filesystem::path>{}, 1);
    (void)empty;
    throw std::runtime_error("empty explicit trace file list was accepted");
  } catch (const std::invalid_argument &) {
  }

  try {
    TraceReader directory(std::vector<std::filesystem::path>{temp.path()}, 1);
    (void)directory;
    throw std::runtime_error("directory in explicit file list was accepted");
  } catch (const std::runtime_error &error) {
    expect(std::string(error.what()).find("not a regular file") !=
               std::string::npos,
           "explicit non-file error lacks reason");
  }

  const auto bad_header = temp.path() / "bad_header.csv";
  {
    std::ofstream output(bad_header);
    output << "key,op,op_count,size,key_size\n"
           << "bad,GET,1,0,3\n";
  }
  TraceReader validates_each_file(
      std::vector<std::filesystem::path>{first, bad_header}, 1);
  (void)validates_each_file.next_shared();
  try {
    (void)validates_each_file.next_shared();
    throw std::runtime_error("later explicit-file header was not validated");
  } catch (const std::runtime_error &error) {
    expect(std::string(error.what()).find("bad_header.csv:1:") !=
               std::string::npos,
           "later explicit-file header error lacks location");
  }
}

void test_size_is_validated_but_ignored() {
  TempDirectory temp;
  const auto valid = temp.path() / "valid.csv";
  const auto invalid = temp.path() / "invalid.csv";
  write_trace(valid, "key,GET,999999999999999999999999999999999999,1,3\n");
  write_trace(invalid, "key,GET,12x,1,3\n");

  TraceReader valid_reader(valid, 1);
  expect_request(valid_reader.next(), "key", Operation::Get);

  TraceReader invalid_reader(invalid, 1);
  try {
    (void)invalid_reader.next();
    throw std::runtime_error("invalid size was accepted");
  } catch (const std::runtime_error &error) {
    const std::string message = error.what();
    expect(message.find("invalid.csv:2:") != std::string::npos,
           "size error lacks file and line");
    expect(message.find("size") != std::string::npos,
           "size error lacks field name");
  }
}

void test_oversized_key_fails_with_location() {
  TempDirectory temp;
  const auto path = temp.path() / "too_long.csv";
  write_trace(path, "key,DELETE,0,1,251\n");

  TraceReader reader(path, 1);
  try {
    (void)reader.next();
    throw std::runtime_error("oversized key was accepted");
  } catch (const std::runtime_error &error) {
    const std::string message = error.what();
    expect(message.find("too_long.csv:2:") != std::string::npos,
           "key error lacks file and line");
    expect(message.find("250") != std::string::npos,
           "key error lacks memcached limit");
  }
}

void test_invalid_amp_factor() {
  try {
    TraceReader reader(tiny_trace_path(), 0);
    (void)reader;
    throw std::runtime_error("zero amp_factor was accepted");
  } catch (const std::invalid_argument &) {
  }

  try {
    TraceReader reader(tiny_trace_path(), 10001);
    (void)reader;
    throw std::runtime_error("five-digit amp_factor was accepted");
  } catch (const std::invalid_argument &) {
  }
}

void test_malformed_rows_and_empty_trace() {
  TempDirectory temp;

  const auto bad_header = temp.path() / "bad_header.csv";
  {
    std::ofstream output(bad_header);
    output << "key,op,size,key_size,op_count\n";
  }
  try {
    TraceReader reader(bad_header, 1);
    (void)reader;
    throw std::runtime_error("bad header was accepted");
  } catch (const std::runtime_error &error) {
    expect(std::string(error.what()).find("bad_header.csv:1:") !=
               std::string::npos,
           "header error lacks location");
  }

  const auto unknown_op = temp.path() / "unknown_op.csv";
  write_trace(unknown_op, "key,INCREMENT,1,1,3\n");
  try {
    TraceReader reader(unknown_op, 1);
    (void)reader.next();
    throw std::runtime_error("unknown operation was accepted");
  } catch (const std::runtime_error &error) {
    expect(std::string(error.what()).find("unsupported operation") !=
               std::string::npos,
           "operation error lacks reason");
  }

  const auto zero_count = temp.path() / "zero_count.csv";
  write_trace(zero_count, "key,GET,1,0,3\n");
  try {
    TraceReader reader(zero_count, 1);
    (void)reader.next();
    throw std::runtime_error("zero op_count was accepted");
  } catch (const std::runtime_error &error) {
    expect(std::string(error.what()).find("op_count") != std::string::npos,
           "op_count error lacks field name");
  }

  const auto empty_key = temp.path() / "empty_key.csv";
  write_trace(empty_key, ",GET,1,1,0\n");
  try {
    TraceReader reader(empty_key, 1);
    (void)reader.next();
    throw std::runtime_error("empty key was accepted");
  } catch (const std::runtime_error &error) {
    expect(std::string(error.what()).find("key must not be empty") !=
               std::string::npos,
           "empty-key error lacks reason");
  }

  const auto empty = temp.path() / "empty.csv";
  write_trace(empty, "");
  try {
    TraceReader reader(empty, 1);
    (void)reader.next();
    throw std::runtime_error("header-only trace was accepted");
  } catch (const std::runtime_error &error) {
    expect(std::string(error.what()).find("no data rows") != std::string::npos,
           "empty-trace error lacks reason");
  }
}

} // namespace

int main() {
  try {
    test_padding_op_count_and_rewind();
    test_amplification_order_and_key_transform();
    test_shared_key_reuse_and_lifetime();
    test_bulk_skip_preserves_expansion_cursor();
    test_directory_natural_order_and_rewind();
    test_explicit_file_order_and_validation();
    test_size_is_validated_but_ignored();
    test_oversized_key_fails_with_location();
    test_invalid_amp_factor();
    test_malformed_rows_and_empty_trace();
    std::cout << "trace_reader tests passed\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "trace_reader test failure: " << error.what() << '\n';
    return 1;
  }
}
