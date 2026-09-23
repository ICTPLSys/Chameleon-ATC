#include "stats.h"

#include <hdr/hdr_histogram.h>

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <ostream>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace mcgen {

Counters &Counters::operator+=(const Counters &r) {
#define MCGEN_ADD_FIELD(field) field += r.field
  MCGEN_ADD_FIELD(scheduled);
  MCGEN_ADD_FIELD(producer_late);
  MCGEN_ADD_FIELD(sent);
  MCGEN_ADD_FIELD(completed);
  MCGEN_ADD_FIELD(get_hit);
  MCGEN_ADD_FIELD(get_miss);
  MCGEN_ADD_FIELD(set_success);
  MCGEN_ADD_FIELD(delete_success);
  MCGEN_ADD_FIELD(delete_miss);
  MCGEN_ADD_FIELD(server_error);
  MCGEN_ADD_FIELD(late);
  MCGEN_ADD_FIELD(queue_full);
  MCGEN_ADD_FIELD(no_slot);
  MCGEN_ADD_FIELD(send_error);
  MCGEN_ADD_FIELD(stopped_at_deadline);
  MCGEN_ADD_FIELD(timeout);
  MCGEN_ADD_FIELD(outstanding);
  MCGEN_ADD_FIELD(malformed_fragment);
  MCGEN_ADD_FIELD(duplicate_fragment);
  MCGEN_ADD_FIELD(stale_fragment);
  MCGEN_ADD_FIELD(multi_fragment_response);
  MCGEN_ADD_FIELD(latency_out_of_range);
#undef MCGEN_ADD_FIELD
  return *this;
}

Histogram::Histogram() {
  const int rc = hdr_init(1, 60'000'000'000LL, 3, &histogram_);
  if (rc != 0 || histogram_ == nullptr) {
    throw std::runtime_error("hdr_init failed with error " +
                             std::to_string(rc));
  }
}

Histogram::~Histogram() {
  if (histogram_ != nullptr) {
    hdr_close(histogram_);
  }
}

Histogram::Histogram(Histogram &&other) noexcept
    : histogram_(std::exchange(other.histogram_, nullptr)) {}

Histogram &Histogram::operator=(Histogram &&other) noexcept {
  if (this != &other) {
    if (histogram_ != nullptr) {
      hdr_close(histogram_);
    }
    histogram_ = std::exchange(other.histogram_, nullptr);
  }
  return *this;
}

bool Histogram::record(std::int64_t nanoseconds) {
  return hdr_record_value(histogram_, nanoseconds);
}

std::uint64_t Histogram::add(const Histogram &other) {
  return static_cast<std::uint64_t>(hdr_add(histogram_, other.histogram_));
}

void Histogram::reset() { hdr_reset(histogram_); }

std::int64_t Histogram::count() const { return histogram_->total_count; }

double Histogram::mean_ns() const {
  return histogram_->total_count == 0 ? 0.0 : hdr_mean(histogram_);
}

std::int64_t Histogram::max_ns() const { return hdr_max(histogram_); }

std::int64_t Histogram::percentile_ns(double percentile) const {
  return hdr_value_at_percentile(histogram_, percentile);
}

void PrintCsvHeader(std::ostream &out) {
  out << "sample,target_mpps,workers,producer_shards,load_valid,invalid_reason,"
         "schedule_complete,"
         "measured_seconds,producer_wall_seconds,sample_wall_seconds,"
         "actual_tx_mops,goodput_mops,admission_rate,response_rate,"
         "end_to_end_rate,scheduled,producer_late,sent,completed,get_hit,"
         "get_miss,set_success,delete_success,delete_miss,server_error,"
         "worker_late,queue_full,no_slot,send_error,stopped_at_deadline,"
         "timeout,outstanding,"
         "socket_receive_drops_total_sample,"
         "rx_handoff_drops_total_sample,"
         "affinity_repairs_total_sample,"
         "host_udp_rcvbuf_errors_delta_total_sample,"
         "maximum_queue_depth_total_sample,"
         "maximum_rx_queue_depth_total_sample,"
         "maximum_producer_lag_us_total_sample,"
         "malformed_fragment_total_sample,duplicate_fragment_total_sample,"
         "stale_fragment_total_sample,"
         "multi_fragment_response,latency_samples,latency_out_of_range,"
         "completed_p50_us,completed_p90_us,completed_p95_us,completed_p99_us,"
         "completed_p99.9_us,completed_p99.99_us,completed_mean_us,"
         "completed_max_us,offered_p50_us,offered_p90_us,offered_p95_us,offered_p99_us,"
         "offered_p99.9_us,offered_p99.99_us\n";
}

void PrintCsvRow(std::ostream &out, const SampleResult &r) {
  const double tx = r.measured_seconds > 0
                        ? static_cast<double>(r.counters.sent) /
                              r.measured_seconds / 1'000'000.0
                        : 0.0;
  const double goodput = r.measured_seconds > 0
                             ? static_cast<double>(r.counters.completed) /
                                   r.measured_seconds / 1'000'000.0
                             : 0.0;
  const double response_rate = r.counters.sent > 0
                                   ? static_cast<double>(r.counters.completed) /
                                         static_cast<double>(r.counters.sent)
                                   : 0.0;
  const double admission_rate =
      r.counters.scheduled > 0 ? static_cast<double>(r.counters.sent) /
                                     static_cast<double>(r.counters.scheduled)
                               : 0.0;
  const double end_to_end_rate =
      r.counters.scheduled > 0 ? static_cast<double>(r.counters.completed) /
                                     static_cast<double>(r.counters.scheduled)
                               : 0.0;
  const auto us = [](std::int64_t ns) {
    return static_cast<double>(ns) / 1000.0;
  };

  out << r.sample << ',' << std::fixed << std::setprecision(6) << r.target_mpps
      << ',' << r.workers << ',' << r.producer_shards << ','
      << (r.load_valid ? 1 : 0) << ','
      << (r.invalid_reason.empty() ? "none" : r.invalid_reason) << ','
      << (r.schedule_complete ? 1 : 0) << ',' << r.measured_seconds << ','
      << r.producer_wall_seconds << ',' << r.sample_wall_seconds << ',' << tx
      << ',' << goodput << ',' << admission_rate << ',' << response_rate << ','
      << end_to_end_rate << ',' << r.counters.scheduled << ','
      << r.counters.producer_late << ',' << r.counters.sent << ','
      << r.counters.completed << ',' << r.counters.get_hit << ','
      << r.counters.get_miss << ',' << r.counters.set_success << ','
      << r.counters.delete_success << ',' << r.counters.delete_miss << ','
      << r.counters.server_error << ',' << r.counters.late << ','
      << r.counters.queue_full << ',' << r.counters.no_slot << ','
      << r.counters.send_error << ',' << r.counters.stopped_at_deadline << ','
      << r.counters.timeout << ',' << r.counters.outstanding << ','
      << r.socket_receive_drops << ',' << r.receive_handoff_drops << ','
      << r.affinity_repairs << ','
      << (r.host_udp_receive_buffer_errors.has_value()
              ? std::to_string(*r.host_udp_receive_buffer_errors)
              : "NA")
      << ',' << r.maximum_queue_depth << ',' << r.maximum_receive_queue_depth
      << ',' << us(r.maximum_producer_lag_ns) << ','
      << r.counters.malformed_fragment << ',' << r.counters.duplicate_fragment
      << ',' << r.counters.stale_fragment << ','
      << r.counters.multi_fragment_response << ',' << r.histogram.count() << ','
      << r.counters.latency_out_of_range << ','
      << us(r.histogram.percentile_ns(50.0)) << ','
      << us(r.histogram.percentile_ns(90.0)) << ','
      << us(r.histogram.percentile_ns(95.0)) << ','
      << us(r.histogram.percentile_ns(99.0)) << ','
      << us(r.histogram.percentile_ns(99.9)) << ','
      << us(r.histogram.percentile_ns(99.99)) << ','
      << r.histogram.mean_ns() / 1000.0 << ',' << us(r.histogram.max_ns())
      << ',';

  const double offered_percentiles[] = {50.0, 90.0, 95.0, 99.0, 99.9, 99.99};
  for (std::size_t index = 0; index < 6; ++index) {
    const auto value = DropAwarePercentileNs(r, offered_percentiles[index]);
    if (value.has_value()) {
      out << us(*value);
    } else {
      out << "NA";
    }
    out << (index + 1 == 6 ? '\n' : ',');
  }
}

std::string CounterInvariantError(const Counters &c) {
  const auto never_sent = c.producer_late + c.late + c.queue_full + c.no_slot +
                          c.send_error + c.stopped_at_deadline;
  if (c.scheduled != c.sent + never_sent) {
    std::ostringstream out;
    out << "scheduled=" << c.scheduled
        << " but sent+never_sent=" << c.sent + never_sent;
    return out.str();
  }
  if (c.sent != c.completed + c.timeout + c.outstanding) {
    std::ostringstream out;
    out << "sent=" << c.sent << " but completed+timeout+outstanding="
        << c.completed + c.timeout + c.outstanding;
    return out.str();
  }
  const auto outcomes = c.get_hit + c.get_miss + c.set_success +
                        c.delete_success + c.delete_miss + c.server_error;
  if (c.completed != outcomes) {
    std::ostringstream out;
    out << "completed=" << c.completed
        << " but operation outcomes=" << outcomes;
    return out.str();
  }
  return {};
}

std::string LoadValidityError(const SampleResult &r) {
  std::string reason;
  const auto append = [&reason](const char *value) {
    if (!reason.empty()) {
      reason += ';';
    }
    reason += value;
  };
  if (!r.schedule_complete) {
    append("schedule_incomplete");
  }
  if (r.counters.scheduled == 0) {
    append("no_arrivals");
    return reason;
  }

  const double scheduled = static_cast<double>(r.counters.scheduled);
  const double producer_late =
      static_cast<double>(r.counters.producer_late) / scheduled;
  const double admission = static_cast<double>(r.counters.sent) / scheduled;
  const double response = r.counters.sent > 0
                              ? static_cast<double>(r.counters.completed) /
                                    static_cast<double>(r.counters.sent)
                              : 0.0;
  const double end_to_end =
      static_cast<double>(r.counters.completed) / scheduled;
  if (producer_late > 0.001) {
    append("producer_overload");
  }
  if (admission < 0.99) {
    append("admission_below_99pct");
  }
  if (response < 0.99) {
    append("response_below_99pct");
  }
  if (end_to_end < 0.99) {
    append("end_to_end_below_99pct");
  }
  if (r.receive_handoff_drops != 0) {
    append("rx_handoff_drops");
  }
  if (r.affinity_repairs != 0) {
    append("affinity_interference");
  }

  const double actual_tx = r.measured_seconds > 0.0
                               ? static_cast<double>(r.counters.sent) /
                                     r.measured_seconds / 1'000'000.0
                               : 0.0;
  const double poisson_tolerance = 5.0 / std::sqrt(scheduled);
  const double tolerance = std::max(0.01, poisson_tolerance);
  if (r.target_mpps > 0.0 &&
      std::abs(actual_tx - r.target_mpps) / r.target_mpps > tolerance) {
    append("tx_rate_outside_tolerance");
  }
  return reason;
}

std::optional<std::int64_t> DropAwarePercentileNs(const SampleResult &r,
                                                  double percentile) {
  if (!r.schedule_complete || r.counters.scheduled == 0 ||
      !std::isfinite(percentile) || percentile < 0.0 || percentile > 100.0) {
    return std::nullopt;
  }
  // Latencies rejected by the histogram are known to exceed its configured
  // range, but their exact values are unavailable. Treat them like other
  // non-finite outcomes rather than letting them make a high percentile look
  // deceptively finite.
  const double completion_fraction = static_cast<double>(r.histogram.count()) /
                                     static_cast<double>(r.counters.scheduled);
  const double quantile = percentile / 100.0;
  if (completion_fraction <= 0.0 || quantile > completion_fraction) {
    return std::nullopt;
  }
  const double completed_percentile =
      std::min(100.0, percentile / completion_fraction);
  return r.histogram.percentile_ns(completed_percentile);
}

} // namespace mcgen
