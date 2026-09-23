#pragma once

#include <cstdint>
#include <iosfwd>
#include <memory>
#include <optional>
#include <string>
#include <vector>

struct hdr_histogram;

namespace mcgen {

struct Counters {
  std::uint64_t scheduled = 0;
  // Arrivals materialized after their send-lag budget had already expired.
  // These never enter a worker queue and are distinct from worker-side late.
  std::uint64_t producer_late = 0;
  std::uint64_t sent = 0;
  std::uint64_t completed = 0;
  std::uint64_t get_hit = 0;
  std::uint64_t get_miss = 0;
  std::uint64_t set_success = 0;
  std::uint64_t delete_success = 0;
  std::uint64_t delete_miss = 0;
  std::uint64_t server_error = 0;
  std::uint64_t late = 0;
  std::uint64_t queue_full = 0;
  std::uint64_t no_slot = 0;
  std::uint64_t send_error = 0;
  std::uint64_t stopped_at_deadline = 0;
  std::uint64_t timeout = 0;
  std::uint64_t outstanding = 0;
  std::uint64_t malformed_fragment = 0;
  std::uint64_t duplicate_fragment = 0;
  std::uint64_t stale_fragment = 0;
  std::uint64_t multi_fragment_response = 0;
  std::uint64_t latency_out_of_range = 0;

  Counters &operator+=(const Counters &rhs);
};

class Histogram {
public:
  Histogram();
  ~Histogram();
  Histogram(Histogram &&other) noexcept;
  Histogram &operator=(Histogram &&other) noexcept;
  Histogram(const Histogram &) = delete;
  Histogram &operator=(const Histogram &) = delete;

  bool record(std::int64_t nanoseconds);
  std::uint64_t add(const Histogram &other);
  void reset();
  std::int64_t count() const;
  double mean_ns() const;
  std::int64_t max_ns() const;
  std::int64_t percentile_ns(double percentile) const;
  hdr_histogram *raw() { return histogram_; }
  const hdr_histogram *raw() const { return histogram_; }

private:
  hdr_histogram *histogram_ = nullptr;
};

struct SampleResult {
  std::size_t sample = 0;
  double target_mpps = 0.0;
  std::uint32_t workers = 0;
  std::uint32_t producer_shards = 0;
  double measured_seconds = 0.0;
  double producer_wall_seconds = 0.0;
  double sample_wall_seconds = 0.0;
  bool schedule_complete = true;
  bool load_valid = false;
  std::string invalid_reason;
  std::int64_t maximum_producer_lag_ns = 0;
  std::uint64_t socket_receive_drops = 0;
  std::uint64_t receive_handoff_drops = 0;
  std::uint64_t affinity_repairs = 0;
  std::optional<std::uint64_t> host_udp_receive_buffer_errors;
  std::size_t maximum_queue_depth = 0;
  std::size_t maximum_receive_queue_depth = 0;
  int minimum_socket_send_buffer_bytes = 0;
  int minimum_socket_receive_buffer_bytes = 0;
  Counters counters;
  Histogram histogram;
};

void PrintCsvHeader(std::ostream &out);
void PrintCsvRow(std::ostream &out, const SampleResult &result);
std::string CounterInvariantError(const Counters &counters);
std::string LoadValidityError(const SampleResult &result);
std::optional<std::int64_t> DropAwarePercentileNs(const SampleResult &result,
                                                  double percentile);

} // namespace mcgen
