#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace mcgen {

enum class Operation {
  Get,
  Set,
  Delete,
};

struct Request {
  std::string key;
  Operation op;
};

constexpr std::size_t kMaximumTraceKeyLength = 250;

// Immutable compact key representation used by the high-rate replay path.
// Keeping the bytes inline means one allocation per distinct amplified trace
// key rather than one std::string allocation per expanded logical operation.
struct TraceKey {
  // Deliberately do not clear all 250 bytes on allocation: materialize_key()
  // initializes exactly [0, length), and view() never exposes the remainder.
  TraceKey() noexcept {}

  std::array<char, kMaximumTraceKeyLength> bytes;
  std::uint16_t length = 0;
  std::uint64_t hash = 0;

  std::string_view view() const noexcept {
    return std::string_view(bytes.data(), length);
  }
};

// All op_count repetitions of one amplified trace key share this immutable
// object. The cached hash is stable FNV-1a and is available without scanning
// the key again when callers need deterministic key affinity.
struct SharedRequest {
  std::shared_ptr<const TraceKey> key;
  Operation op;
};

std::uint64_t StableKeyHash(std::string_view key) noexcept;

// Streams CacheLib CSV traces and expands each input row into individual
// requests. Once the last input file reaches EOF, the reader automatically
// reopens the first file and continues.
class TraceReader {
public:
  explicit TraceReader(std::filesystem::path trace_path,
                       std::uint32_t amp_factor = 1);
  explicit TraceReader(std::vector<std::filesystem::path> trace_files,
                       std::uint32_t amp_factor = 1);

  // Returns the next expanded request. Format and key errors are reported as
  // std::runtime_error with the source file and line number.
  Request next();

  // Allocation-efficient replay interface. The returned key remains valid
  // after subsequent calls and is shared by all repetitions of the same
  // (row, amplification suffix) pair.
  SharedRequest next_shared();

  // Consume logical operations without constructing keys or hashing them.
  // Expansion order and the cursor observed by next()/next_shared() are
  // exactly the same as if next() had been called count times. The trace is
  // cyclic, so every requested operation is consumed unless parsing fails.
  void skip(std::uint64_t count);

  // Exposed for diagnostics and tests. Directory inputs are returned in
  // natural filename order.
  const std::vector<std::filesystem::path> &files() const noexcept {
    return files_;
  }

private:
  void validate_amp_factor() const;
  void validate_explicit_files() const;
  void discover_files(const std::filesystem::path &trace_path);
  void open_file(std::size_t index);
  void load_next_row();
  void prepare_next_group();
  void invalidate_key() noexcept;
  void materialize_key();

  [[noreturn]] void fail(std::uint64_t line, const std::string &message) const;

  std::vector<std::filesystem::path> files_;
  std::size_t file_index_{0};
  std::ifstream stream_;
  std::uint64_t line_number_{0};
  std::uint32_t amp_factor_{1};

  std::string base_key_;
  std::shared_ptr<const TraceKey> current_key_;
  Operation current_operation_{Operation::Get};
  std::uint64_t current_op_count_{0};
  std::uint64_t next_repeat_{0};
  std::uint32_t current_suffix_{0};
  bool have_row_{false};
};

} // namespace mcgen
