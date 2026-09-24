#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace mcgen {

// Command-line configuration. Time values deliberately retain the unit used
// by the option name so that printing a configuration cannot silently change
// units.
struct Options {
  std::string server;
  std::string trace_path;

  double start_mpps{0.0};
  double max_mpps{0.0};
  std::uint32_t samples{1};

  std::uint64_t runtime_seconds{10};
  std::uint64_t rampup_seconds{4};
  std::uint32_t workers{10};
  // Zero keeps receive syscalls in each worker. A positive value enables
  // split RX I/O using this many shared receiver threads.
  std::uint32_t rx_threads{0};
  // Zero selects min(trace file count, workers). A positive value is checked
  // against both after trace discovery.
  std::uint32_t producer_shards{0};
  std::uint32_t amp_factor{1};
  std::uint32_t value_size{4096};

  std::uint64_t request_timeout_ms{200};
  std::uint64_t drain_ms{1000};
  std::uint64_t max_send_lag_us{5};
  std::uint32_t max_inflight{32768};
  std::uint32_t queue_depth{8192};
  std::uint64_t schedule_ahead_ms{20};
  std::uint32_t batch_size{32};
  std::uint32_t socket_buffer_mb{16};
  std::uint32_t rx_queue_depth{4096};
  std::uint64_t seed{1};

  // Optional one-logical-CPU-per-thread placement. Lists accept the Linux-like
  // syntax "0-3,8" and preserve order for worker/shard assignment.
  std::vector<std::uint32_t> worker_cpus;
  std::vector<std::uint32_t> rx_cpus;
  std::vector<std::uint32_t> producer_cpus;

  bool show_help{false};
};

class OptionError : public std::runtime_error {
public:
  explicit OptionError(const std::string &message)
      : std::runtime_error(message) {}
};

// Parse long options in either "--name value" or "--name=value" form.
// Unknown options, duplicate options, positional arguments, missing required
// options, and invalid values throw OptionError. --help/-h bypasses only the
// required-option checks; malformed options are still rejected.
Options ParseOptions(int argc, char *const argv[]);

// Validate an Options instance constructed by code rather than ParseOptions.
// This also throws OptionError on failure.
void ValidateOptions(const Options &options);

// Validate placement that depends on trace discovery (and therefore on the
// resolved producer-shard count), and ensure every requested logical CPU is in
// the affinity mask inherited by the process.
void ValidateThreadPlacement(const Options &options,
                             std::size_t resolved_producer_shards);

std::string Usage(const std::string &program_name);

} // namespace mcgen
