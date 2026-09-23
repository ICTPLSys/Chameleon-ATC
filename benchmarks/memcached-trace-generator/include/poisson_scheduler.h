#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <random>
#include <vector>

namespace mcgen {

// Return samples at
//   start_mpps + (max_mpps - start_mpps) / samples * j,
// for j in [1, samples]. The final value is assigned max_mpps exactly to
// avoid a visible floating-point rounding error in reports.
std::vector<double> BuildRateSweep(double start_mpps, double max_mpps,
                                   std::size_t samples);

struct RampStep {
  double mpps;
  std::chrono::milliseconds duration;
};

// Build an exact rampup_seconds-long ramp consisting of 100 ms intervals.
// The last interval runs at target_mpps. A zero-second ramp is empty.
std::vector<RampStep> BuildRampSchedule(double target_mpps,
                                        std::uint64_t rampup_seconds);

// Generates an open-loop Poisson arrival schedule. Deadlines are relative to
// a caller-selected epoch, start strictly after initial_deadline, and are
// strictly increasing at nanosecond resolution. Consequently, representable
// rates are capped at 1,000 Mops/s (one arrival per nanosecond on average).
class PoissonScheduler {
public:
  using Duration = std::chrono::nanoseconds;

  PoissonScheduler(double rate_mpps, std::uint64_t seed,
                   Duration initial_deadline = Duration::zero());

  Duration NextDeadline();

  double rate_mpps() const noexcept { return rate_mpps_; }
  std::uint64_t emitted() const noexcept { return emitted_; }
  Duration last_deadline() const noexcept { return Duration(deadline_ns_); }

private:
  double rate_mpps_;
  std::mt19937_64 rng_;
  std::exponential_distribution<double> interval_ns_;
  std::int64_t deadline_ns_;
  std::uint64_t emitted_{0};
};

} // namespace mcgen
