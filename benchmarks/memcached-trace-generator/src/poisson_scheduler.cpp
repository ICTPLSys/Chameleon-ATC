#include "poisson_scheduler.h"

#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>

namespace mcgen {
namespace {

constexpr double kMaxMpps = 1000.0;
constexpr std::size_t kMaxSamples = 1'000'000;
constexpr std::uint64_t kMaxRampupSeconds = 24 * 60 * 60;

void ValidateRate(double rate_mpps, const char *label, bool allow_zero) {
  const bool below_minimum = allow_zero ? rate_mpps < 0.0 : rate_mpps <= 0.0;
  if (!std::isfinite(rate_mpps) || below_minimum || rate_mpps > kMaxMpps) {
    throw std::invalid_argument(
        std::string(label) +
        (allow_zero ? " must be in [0, 1000]" : " must be in (0, 1000]"));
  }
}

double RatePerNanosecond(double rate_mpps) {
  ValidateRate(rate_mpps, "rate_mpps", false);
  // Mops/s * 1e6 ops/Mop / 1e9 ns/s.
  return rate_mpps / 1000.0;
}

} // namespace

std::vector<double> BuildRateSweep(double start_mpps, double max_mpps,
                                   std::size_t samples) {
  ValidateRate(start_mpps, "start_mpps", true);
  ValidateRate(max_mpps, "max_mpps", false);
  if (start_mpps > max_mpps) {
    throw std::invalid_argument("start_mpps must not exceed max_mpps");
  }
  if (samples == 0 || samples > kMaxSamples) {
    throw std::invalid_argument("samples must be in [1, 1000000]");
  }

  std::vector<double> rates;
  rates.reserve(samples);
  const double step = (max_mpps - start_mpps) / static_cast<double>(samples);
  for (std::size_t index = 1; index <= samples; ++index) {
    rates.push_back(index == samples
                        ? max_mpps
                        : start_mpps + step * static_cast<double>(index));
  }
  return rates;
}

std::vector<RampStep> BuildRampSchedule(double target_mpps,
                                        std::uint64_t rampup_seconds) {
  ValidateRate(target_mpps, "target_mpps", false);
  if (rampup_seconds > kMaxRampupSeconds) {
    throw std::invalid_argument("rampup_seconds must not exceed 86400");
  }
  if (rampup_seconds == 0) {
    return {};
  }

  const std::size_t steps = static_cast<std::size_t>(rampup_seconds * 10);
  std::vector<RampStep> schedule;
  schedule.reserve(steps);
  for (std::size_t index = 1; index <= steps; ++index) {
    schedule.push_back(RampStep{index == steps
                                    ? target_mpps
                                    : target_mpps * static_cast<double>(index) /
                                          static_cast<double>(steps),
                                std::chrono::milliseconds(100)});
  }
  return schedule;
}

PoissonScheduler::PoissonScheduler(double rate_mpps, std::uint64_t seed,
                                   Duration initial_deadline)
    : rate_mpps_(rate_mpps), rng_(seed),
      interval_ns_(RatePerNanosecond(rate_mpps)),
      deadline_ns_(initial_deadline.count()) {
  if (initial_deadline < Duration::zero()) {
    throw std::invalid_argument("initial_deadline must not be negative");
  }
}

PoissonScheduler::Duration PoissonScheduler::NextDeadline() {
  const double sampled_ns = interval_ns_(rng_);
  if (!std::isfinite(sampled_ns) || sampled_ns < 0.0) {
    throw std::overflow_error("Poisson interval is not representable");
  }

  // ceil preserves a positive interval at nanosecond resolution. A sampled
  // zero is also advanced by one nanosecond so callers can use a strict-order
  // queue without a secondary sequence number.
  const double rounded_ns = std::max(1.0, std::ceil(sampled_ns));
  const auto max_ns = std::numeric_limits<std::int64_t>::max();
  if (rounded_ns > static_cast<double>(max_ns)) {
    throw std::overflow_error("Poisson interval exceeds nanosecond range");
  }
  const auto delta_ns = static_cast<std::int64_t>(rounded_ns);
  if (deadline_ns_ > max_ns - delta_ns) {
    throw std::overflow_error("Poisson deadline exceeds nanosecond range");
  }
  if (emitted_ == std::numeric_limits<std::uint64_t>::max()) {
    throw std::overflow_error("Poisson arrival count overflow");
  }

  deadline_ns_ += delta_ns;
  ++emitted_;
  return Duration(deadline_ns_);
}

} // namespace mcgen
