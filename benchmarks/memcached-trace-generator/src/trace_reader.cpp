#include "trace_reader.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <utility>

namespace mcgen {
namespace {

constexpr std::string_view kExpectedHeader = "key,op,size,op_count,key_size";
constexpr std::string_view kTracePrefix = "kvcache_traces_";
constexpr std::string_view kTraceSuffix = ".csv";
constexpr std::size_t kAmplifiedMinKeyPrefix = 16;
constexpr std::uint32_t kMaxAmpFactor = 10000;

bool is_decimal(std::string_view value) {
  if (value.empty()) {
    return false;
  }
  return std::all_of(value.begin(), value.end(),
                     [](char ch) { return ch >= '0' && ch <= '9'; });
}

std::uint64_t parse_u64(std::string_view value, std::string_view field) {
  if (!is_decimal(value)) {
    throw std::invalid_argument(std::string(field) +
                                " must be an unsigned decimal integer");
  }

  std::uint64_t result = 0;
  for (char ch : value) {
    const auto digit = static_cast<std::uint64_t>(ch - '0');
    if (result > (std::numeric_limits<std::uint64_t>::max() - digit) / 10) {
      throw std::invalid_argument(std::string(field) + " is out of range");
    }
    result = result * 10 + digit;
  }
  return result;
}

std::array<std::string_view, 5> split_row(const std::string &line) {
  std::array<std::string_view, 5> fields;
  std::size_t field_begin = 0;

  for (std::size_t field = 0; field < fields.size(); ++field) {
    const std::size_t comma = line.find(',', field_begin);
    if (field + 1 == fields.size()) {
      if (comma != std::string::npos) {
        throw std::invalid_argument("expected exactly 5 CSV fields");
      }
      fields[field] = std::string_view(line).substr(field_begin);
      return fields;
    }
    if (comma == std::string::npos) {
      throw std::invalid_argument("expected exactly 5 CSV fields");
    }
    fields[field] =
        std::string_view(line).substr(field_begin, comma - field_begin);
    field_begin = comma + 1;
  }

  throw std::invalid_argument("expected exactly 5 CSV fields");
}

bool is_trace_filename(const std::filesystem::path &path) {
  const std::string name = path.filename().string();
  return name.size() >= kTracePrefix.size() + kTraceSuffix.size() &&
         name.compare(0, kTracePrefix.size(), kTracePrefix) == 0 &&
         name.compare(name.size() - kTraceSuffix.size(), kTraceSuffix.size(),
                      kTraceSuffix) == 0;
}

// Compare digit runs by numeric magnitude without converting them, so very
// large numeric filename components remain well-defined.
bool natural_less(const std::filesystem::path &lhs_path,
                  const std::filesystem::path &rhs_path) {
  const std::string lhs = lhs_path.filename().string();
  const std::string rhs = rhs_path.filename().string();
  std::size_t lhs_pos = 0;
  std::size_t rhs_pos = 0;

  while (lhs_pos < lhs.size() && rhs_pos < rhs.size()) {
    const bool lhs_digit =
        std::isdigit(static_cast<unsigned char>(lhs[lhs_pos])) != 0;
    const bool rhs_digit =
        std::isdigit(static_cast<unsigned char>(rhs[rhs_pos])) != 0;

    if (lhs_digit && rhs_digit) {
      const std::size_t lhs_run_begin = lhs_pos;
      const std::size_t rhs_run_begin = rhs_pos;
      while (lhs_pos < lhs.size() &&
             std::isdigit(static_cast<unsigned char>(lhs[lhs_pos])) != 0) {
        ++lhs_pos;
      }
      while (rhs_pos < rhs.size() &&
             std::isdigit(static_cast<unsigned char>(rhs[rhs_pos])) != 0) {
        ++rhs_pos;
      }

      std::size_t lhs_significant = lhs_run_begin;
      std::size_t rhs_significant = rhs_run_begin;
      while (lhs_significant < lhs_pos && lhs[lhs_significant] == '0') {
        ++lhs_significant;
      }
      while (rhs_significant < rhs_pos && rhs[rhs_significant] == '0') {
        ++rhs_significant;
      }

      const std::size_t lhs_digits = lhs_pos - lhs_significant;
      const std::size_t rhs_digits = rhs_pos - rhs_significant;
      if (lhs_digits != rhs_digits) {
        return lhs_digits < rhs_digits;
      }

      const int number_comparison = lhs.compare(
          lhs_significant, lhs_digits, rhs, rhs_significant, rhs_digits);
      if (number_comparison != 0) {
        return number_comparison < 0;
      }

      const std::size_t lhs_run_length = lhs_pos - lhs_run_begin;
      const std::size_t rhs_run_length = rhs_pos - rhs_run_begin;
      if (lhs_run_length != rhs_run_length) {
        return lhs_run_length < rhs_run_length;
      }
      continue;
    }

    if (lhs[lhs_pos] != rhs[rhs_pos]) {
      return static_cast<unsigned char>(lhs[lhs_pos]) <
             static_cast<unsigned char>(rhs[rhs_pos]);
    }
    ++lhs_pos;
    ++rhs_pos;
  }

  if (lhs.size() != rhs.size()) {
    return lhs.size() < rhs.size();
  }
  return lhs_path.string() < rhs_path.string();
}

std::string suffix_string(std::uint32_t suffix) {
  std::string result(4, '0');
  result[0] = static_cast<char>('0' + (suffix / 1000) % 10);
  result[1] = static_cast<char>('0' + (suffix / 100) % 10);
  result[2] = static_cast<char>('0' + (suffix / 10) % 10);
  result[3] = static_cast<char>('0' + suffix % 10);
  return result;
}

} // namespace

std::uint64_t StableKeyHash(std::string_view key) noexcept {
  constexpr std::uint64_t kFnv1a64OffsetBasis = 14695981039346656037ULL;
  constexpr std::uint64_t kFnv1a64Prime = 1099511628211ULL;
  std::uint64_t hash = kFnv1a64OffsetBasis;
  for (const char character : key) {
    hash ^= static_cast<unsigned char>(character);
    hash *= kFnv1a64Prime;
  }
  return hash;
}

TraceReader::TraceReader(std::filesystem::path trace_path,
                         std::uint32_t amp_factor)
    : amp_factor_(amp_factor) {
  validate_amp_factor();
  discover_files(trace_path);
  open_file(0);
}

TraceReader::TraceReader(std::vector<std::filesystem::path> trace_files,
                         std::uint32_t amp_factor)
    : files_(std::move(trace_files)), amp_factor_(amp_factor) {
  validate_amp_factor();
  validate_explicit_files();
  open_file(0);
}

void TraceReader::validate_amp_factor() const {
  if (amp_factor_ == 0 || amp_factor_ > kMaxAmpFactor) {
    throw std::invalid_argument("amp_factor must be in [1, 10000]");
  }
}

void TraceReader::validate_explicit_files() const {
  if (files_.empty()) {
    throw std::invalid_argument("trace file list must not be empty");
  }
  for (const auto &path : files_) {
    std::error_code error;
    const auto status = std::filesystem::status(path, error);
    if (error) {
      throw std::runtime_error("trace path '" + path.string() +
                               "': " + error.message());
    }
    if (!std::filesystem::is_regular_file(status)) {
      throw std::runtime_error("trace path '" + path.string() +
                               "' is not a regular file");
    }
  }
}

void TraceReader::discover_files(const std::filesystem::path &trace_path) {
  std::error_code error;
  const auto status = std::filesystem::status(trace_path, error);
  if (error) {
    throw std::runtime_error("trace path '" + trace_path.string() +
                             "': " + error.message());
  }

  if (std::filesystem::is_regular_file(status)) {
    files_.push_back(trace_path);
    return;
  }

  if (!std::filesystem::is_directory(status)) {
    throw std::runtime_error("trace path '" + trace_path.string() +
                             "' is neither a regular file nor a directory");
  }

  std::filesystem::directory_iterator iterator(trace_path, error);
  const std::filesystem::directory_iterator end;
  while (!error && iterator != end) {
    std::error_code entry_error;
    if (iterator->is_regular_file(entry_error) && !entry_error &&
        is_trace_filename(iterator->path())) {
      files_.push_back(iterator->path());
    }
    iterator.increment(error);
  }
  if (error) {
    throw std::runtime_error("failed to enumerate trace directory '" +
                             trace_path.string() + "': " + error.message());
  }

  std::sort(files_.begin(), files_.end(), natural_less);
  if (files_.empty()) {
    throw std::runtime_error("trace directory '" + trace_path.string() +
                             "' contains no kvcache_traces_*.csv files");
  }
}

void TraceReader::open_file(std::size_t index) {
  stream_.close();
  stream_.clear();
  file_index_ = index;
  line_number_ = 0;
  stream_.open(files_[file_index_]);
  if (!stream_) {
    throw std::runtime_error("failed to open trace file '" +
                             files_[file_index_].string() + "'");
  }

  std::string header;
  if (!std::getline(stream_, header)) {
    fail(1, "missing CSV header");
  }
  line_number_ = 1;
  if (!header.empty() && header.back() == '\r') {
    header.pop_back();
  }
  if (header != kExpectedHeader) {
    fail(1, "expected CSV header '" + std::string(kExpectedHeader) + "'");
  }
}

[[noreturn]] void TraceReader::fail(std::uint64_t line,
                                    const std::string &message) const {
  throw std::runtime_error(files_[file_index_].string() + ":" +
                           std::to_string(line) + ": " + message);
}

void TraceReader::load_next_row() {
  std::size_t files_exhausted_without_data = 0;
  // On calls after at least one row was emitted, the first EOF belongs to a
  // file known to contain data and must not be mistaken for an empty trace.
  bool current_file_had_data = have_row_;

  for (;;) {
    std::string line;
    if (!std::getline(stream_, line)) {
      if (!stream_.eof()) {
        fail(line_number_ + 1, "I/O error while reading trace");
      }
      if (current_file_had_data) {
        files_exhausted_without_data = 0;
      } else {
        ++files_exhausted_without_data;
      }
      if (files_exhausted_without_data >= files_.size()) {
        fail(line_number_, "trace contains no data rows");
      }
      open_file((file_index_ + 1) % files_.size());
      current_file_had_data = false;
      continue;
    }

    ++line_number_;
    if (!line.empty() && line.back() == '\r') {
      line.pop_back();
    }
    if (line.empty()) {
      continue;
    }

    try {
      const auto fields = split_row(line);
      if (!is_decimal(fields[2])) {
        throw std::invalid_argument("size must be an unsigned decimal integer");
      }

      const std::uint64_t op_count = parse_u64(fields[3], "op_count");
      if (op_count == 0) {
        throw std::invalid_argument("op_count must be greater than zero");
      }

      const std::uint64_t key_size_u64 = parse_u64(fields[4], "key_size");
      if (key_size_u64 > std::numeric_limits<std::size_t>::max()) {
        throw std::invalid_argument("key_size is out of range");
      }
      const auto key_size = static_cast<std::size_t>(key_size_u64);
      const std::size_t padded_size = std::max(fields[0].size(), key_size);
      if (padded_size == 0) {
        throw std::invalid_argument("key must not be empty");
      }
      if (padded_size > kMaximumTraceKeyLength) {
        throw std::invalid_argument("final key length " +
                                    std::to_string(padded_size) +
                                    " exceeds memcached limit 250");
      }

      if (fields[1] == "GET") {
        current_operation_ = Operation::Get;
      } else if (fields[1] == "SET") {
        current_operation_ = Operation::Set;
      } else if (fields[1] == "DELETE") {
        current_operation_ = Operation::Delete;
      } else {
        throw std::invalid_argument("unsupported operation '" +
                                    std::string(fields[1]) + "'");
      }

      base_key_.assign(fields[0]);
      base_key_.resize(padded_size, '0');
      current_op_count_ = op_count;
      current_suffix_ = 0;
      next_repeat_ = 0;
      have_row_ = true;
      invalidate_key();
      return;
    } catch (const std::invalid_argument &error) {
      fail(line_number_, error.what());
    }
  }
}

void TraceReader::invalidate_key() noexcept { current_key_.reset(); }

void TraceReader::materialize_key() {
  if (current_key_) {
    return;
  }

  std::size_t prefix_size = base_key_.size();
  if (amp_factor_ > 1) {
    if (prefix_size > kAmplifiedMinKeyPrefix) {
      prefix_size = std::max(prefix_size - 4, kAmplifiedMinKeyPrefix);
    }
  }

  const std::size_t suffix_size = amp_factor_ > 1 ? 4 : 0;
  const std::size_t key_size = prefix_size + suffix_size;
  if (key_size > kMaximumTraceKeyLength) {
    fail(line_number_, "final key length " + std::to_string(key_size) +
                           " exceeds memcached limit 250");
  }

  auto key = std::make_shared<TraceKey>();
  std::copy_n(base_key_.data(), prefix_size, key->bytes.data());
  if (suffix_size != 0) {
    const auto suffix = suffix_string(current_suffix_);
    std::copy(suffix.begin(), suffix.end(), key->bytes.data() + prefix_size);
  }
  key->length = static_cast<std::uint16_t>(key_size);
  key->hash = StableKeyHash(key->view());
  current_key_ = std::move(key);
}

void TraceReader::prepare_next_group() {
  if (!have_row_) {
    load_next_row();
  } else if (next_repeat_ == current_op_count_) {
    next_repeat_ = 0;
    ++current_suffix_;
    if (current_suffix_ == amp_factor_) {
      load_next_row();
    } else {
      invalidate_key();
    }
  }
}

SharedRequest TraceReader::next_shared() {
  prepare_next_group();
  materialize_key();

  ++next_repeat_;
  return SharedRequest{current_key_, current_operation_};
}

Request TraceReader::next() {
  const auto request = next_shared();
  return Request{std::string(request.key->view()), request.op};
}

void TraceReader::skip(std::uint64_t count) {
  while (count != 0) {
    prepare_next_group();
    const std::uint64_t remaining = current_op_count_ - next_repeat_;
    if (count <= remaining) {
      next_repeat_ += count;
      return;
    }

    count -= remaining;
    next_repeat_ = current_op_count_;

    // Skip complete amplification groups arithmetically. Leave the cursor at
    // the end of the last consumed group, which is the same state produced by
    // next() and lets prepare_next_group() perform the next transition.
    const std::uint64_t suffixes_remaining =
        static_cast<std::uint64_t>(amp_factor_ - current_suffix_ - 1);
    const std::uint64_t complete_groups =
        std::min(suffixes_remaining, count / current_op_count_);
    if (complete_groups != 0) {
      current_suffix_ += static_cast<std::uint32_t>(complete_groups);
      count -= complete_groups * current_op_count_;
      invalidate_key();
    }
  }
}

} // namespace mcgen
