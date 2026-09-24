#include "options.h"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <sched.h>
#include <sstream>
#include <string_view>
#include <system_error>
#include <type_traits>
#include <unordered_set>

namespace mcgen {
namespace {

constexpr double kMaxMpps = 1000.0;
constexpr std::uint32_t kMaxSamples = 1'000'000;
constexpr std::uint64_t kMaxRampupSeconds = 24 * 60 * 60;
constexpr std::uint32_t kMaxWorkers = 65'535;
constexpr std::uint32_t kMaxAmpFactor = 10'000;
// Maximum SET value which still leaves room in a legal 65,507-byte UDP
// payload for the 8-byte UDP frame, 24-byte binary header, 8-byte SET extras,
// and a maximum-length (250-byte) Memcached key.
constexpr std::uint32_t kMaxValueSize = 65'217;
constexpr std::uint32_t kMaxBatchSize = 1024;
constexpr std::uint32_t kMaxSocketBufferMb = 1024;

bool IsKnownOption(std::string_view name) {
  static const std::unordered_set<std::string> names = {"server",
                                                        "trace",
                                                        "start-mpps",
                                                        "mpps",
                                                        "samples",
                                                        "runtime",
                                                        "rampup",
                                                        "workers",
                                                        "rx-threads",
                                                        "producer-shards",
                                                        "amp-factor",
                                                        "value-size",
                                                        "request-timeout-ms",
                                                        "drain-ms",
                                                        "max-send-lag-us",
                                                        "max-inflight",
                                                        "queue-depth",
                                                        "schedule-ahead-ms",
                                                        "batch-size",
                                                        "socket-buffer-mb",
                                                        "rx-queue-depth",
                                                        "worker-cpus",
                                                        "rx-cpus",
                                                        "producer-cpus",
                                                        "seed",
                                                        "help"};
  return names.find(std::string(name)) != names.end();
}

template <typename T>
T ParseUnsigned(const std::string &text, std::string_view option) {
  static_assert(std::is_unsigned<T>::value, "T must be unsigned");
  if (text.empty()) {
    throw OptionError("--" + std::string(option) + " requires a value");
  }

  T result{};
  const char *first = text.data();
  const char *last = first + text.size();
  const auto parsed = std::from_chars(first, last, result, 10);
  if (parsed.ec != std::errc{} || parsed.ptr != last) {
    throw OptionError("invalid unsigned integer for --" + std::string(option) +
                      ": " + text);
  }
  return result;
}

double ParseDouble(const std::string &text, std::string_view option) {
  if (text.empty() ||
      std::any_of(text.begin(), text.end(), [](unsigned char character) {
        return std::isspace(character) != 0;
      })) {
    throw OptionError("invalid number for --" + std::string(option) + ": " +
                      text);
  }

  errno = 0;
  char *end = nullptr;
  const double result = std::strtod(text.c_str(), &end);
  if (errno == ERANGE || end != text.c_str() + text.size() ||
      !std::isfinite(result)) {
    throw OptionError("invalid finite number for --" + std::string(option) +
                      ": " + text);
  }
  return result;
}

std::uint32_t ParseU32(const std::string &text, std::string_view option) {
  const std::uint64_t value = ParseUnsigned<std::uint64_t>(text, option);
  if (value > std::numeric_limits<std::uint32_t>::max()) {
    throw OptionError("value for --" + std::string(option) +
                      " exceeds uint32 range: " + text);
  }
  return static_cast<std::uint32_t>(value);
}

std::vector<std::uint32_t> ParseCpuList(const std::string &text,
                                        std::string_view option) {
  if (text.empty() ||
      std::any_of(text.begin(), text.end(), [](unsigned char character) {
        return std::isspace(character) != 0;
      })) {
    throw OptionError("invalid CPU list for --" + std::string(option) + ": " +
                      text);
  }

  std::vector<std::uint32_t> cpus;
  std::unordered_set<std::uint32_t> seen;
  std::size_t begin = 0;
  while (begin < text.size()) {
    const auto comma = text.find(',', begin);
    const auto end = comma == std::string::npos ? text.size() : comma;
    if (end == begin) {
      throw OptionError("empty element in --" + std::string(option));
    }
    const auto token = text.substr(begin, end - begin);
    const auto dash = token.find('-');
    if (dash != std::string::npos &&
        token.find('-', dash + 1) != std::string::npos) {
      throw OptionError("invalid range in --" + std::string(option) + ": " +
                        token);
    }
    const auto first = ParseU32(
        dash == std::string::npos ? token : token.substr(0, dash), option);
    const auto last = ParseU32(
        dash == std::string::npos ? token : token.substr(dash + 1), option);
    if (first > last) {
      throw OptionError("descending range in --" + std::string(option) + ": " +
                        token);
    }
    if (last >= CPU_SETSIZE) {
      throw OptionError("CPU in --" + std::string(option) +
                        " exceeds CPU_SETSIZE: " + std::to_string(last));
    }
    for (std::uint32_t cpu = first; cpu <= last; ++cpu) {
      if (!seen.insert(cpu).second) {
        throw OptionError("duplicate CPU " + std::to_string(cpu) + " in --" +
                          std::string(option));
      }
      cpus.push_back(cpu);
    }
    if (comma == std::string::npos) {
      break;
    }
    begin = comma + 1;
    if (begin == text.size()) {
      throw OptionError("empty element in --" + std::string(option));
    }
  }
  return cpus;
}

void ValidateServer(const std::string &server) {
  std::string host;
  std::string port_text;

  if (!server.empty() && server.front() == '[') {
    const std::size_t close = server.find(']');
    if (close == std::string::npos || close == 1 ||
        close + 1 >= server.size() || server[close + 1] != ':') {
      throw OptionError(
          "--server must be HOST:PORT or bracketed [IPv6]:PORT: " + server);
    }
    host = server.substr(1, close - 1);
    port_text = server.substr(close + 2);
  } else {
    const std::size_t colon = server.rfind(':');
    if (colon == std::string::npos || colon == 0 ||
        colon + 1 >= server.size() || server.find(':') != colon) {
      throw OptionError(
          "--server must be HOST:PORT or bracketed [IPv6]:PORT: " + server);
    }
    host = server.substr(0, colon);
    port_text = server.substr(colon + 1);
  }

  if (host.empty() ||
      std::any_of(host.begin(), host.end(), [](unsigned char character) {
        return std::isspace(character) != 0;
      })) {
    throw OptionError("invalid host in --server: " + server);
  }
  const std::uint64_t port = ParseUnsigned<std::uint64_t>(port_text, "server");
  if (port == 0 || port > 65'535) {
    throw OptionError("port in --server must be in [1, 65535]: " + server);
  }
}

void RequireDurationConvertible(std::uint64_t value,
                                std::uint64_t units_per_second,
                                std::string_view option) {
  const auto max =
      static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
  if (value > max / units_per_second) {
    throw OptionError("--" + std::string(option) +
                      " is too large for nanosecond timekeeping");
  }
}

void ValidateOptionsImpl(const Options &options, bool require_required,
                         bool mpps_was_set) {
  if (require_required && options.server.empty()) {
    throw OptionError("missing required option --server");
  }
  if (!options.server.empty()) {
    ValidateServer(options.server);
  }
  if (require_required && options.trace_path.empty()) {
    throw OptionError("missing required option --trace");
  }

  if (!std::isfinite(options.start_mpps) || options.start_mpps < 0.0 ||
      options.start_mpps > kMaxMpps) {
    throw OptionError("--start-mpps must be finite and in [0, 1000]");
  }
  if (require_required && !mpps_was_set) {
    throw OptionError("missing required option --mpps");
  }
  if (mpps_was_set &&
      (!std::isfinite(options.max_mpps) || options.max_mpps <= 0.0 ||
       options.max_mpps > kMaxMpps)) {
    throw OptionError("--mpps must be finite and in (0, 1000]");
  }
  if (mpps_was_set && options.start_mpps > options.max_mpps) {
    throw OptionError("--start-mpps must not exceed --mpps");
  }
  if (options.samples == 0 || options.samples > kMaxSamples) {
    throw OptionError("--samples must be in [1, 1000000]");
  }
  if (options.runtime_seconds == 0) {
    throw OptionError("--runtime must be greater than zero seconds");
  }
  RequireDurationConvertible(options.runtime_seconds, 1'000'000'000ULL,
                             "runtime");
  if (options.rampup_seconds > kMaxRampupSeconds) {
    throw OptionError("--rampup must not exceed 86400 seconds");
  }
  if (options.workers == 0 || options.workers > kMaxWorkers) {
    throw OptionError("--workers must be in [1, 65535]");
  }
  if (options.rx_threads > options.workers) {
    throw OptionError("--rx-threads must not exceed --workers");
  }
  if (options.producer_shards > options.workers) {
    throw OptionError("--producer-shards must not exceed --workers");
  }
  if (options.amp_factor == 0 || options.amp_factor > kMaxAmpFactor) {
    throw OptionError("--amp-factor must be in [1, 10000]");
  }
  if (options.value_size > kMaxValueSize) {
    throw OptionError("--value-size must be in [0, 65217]");
  }
  if (options.request_timeout_ms == 0) {
    throw OptionError("--request-timeout-ms must be greater than zero");
  }
  RequireDurationConvertible(options.request_timeout_ms, 1'000'000ULL,
                             "request-timeout-ms");
  RequireDurationConvertible(options.drain_ms, 1'000'000ULL, "drain-ms");
  RequireDurationConvertible(options.max_send_lag_us, 1'000ULL,
                             "max-send-lag-us");
  if (options.max_inflight == 0 || options.max_inflight >= 65'536) {
    throw OptionError("--max-inflight must be in [1, 65535]");
  }
  if (options.queue_depth == 0) {
    throw OptionError("--queue-depth must be greater than zero");
  }
  if (options.schedule_ahead_ms == 0) {
    throw OptionError("--schedule-ahead-ms must be greater than zero");
  }
  RequireDurationConvertible(options.schedule_ahead_ms, 1'000'000ULL,
                             "schedule-ahead-ms");
  if (options.batch_size == 0 || options.batch_size > kMaxBatchSize) {
    throw OptionError("--batch-size must be in [1, 1024]");
  }
  if (options.batch_size > options.queue_depth) {
    throw OptionError("--batch-size must not exceed --queue-depth");
  }
  if (options.socket_buffer_mb == 0 ||
      options.socket_buffer_mb > kMaxSocketBufferMb) {
    throw OptionError("--socket-buffer-mb must be in [1, 1024]");
  }
  if (options.rx_queue_depth == 0) {
    throw OptionError("--rx-queue-depth must be greater than zero");
  }
  if (!options.worker_cpus.empty() &&
      options.worker_cpus.size() != options.workers) {
    throw OptionError("--worker-cpus must contain exactly --workers CPUs");
  }
  if (options.rx_threads == 0 && !options.rx_cpus.empty()) {
    throw OptionError("--rx-cpus requires --rx-threads greater than zero");
  }
  if (!options.rx_cpus.empty() &&
      options.rx_cpus.size() != options.rx_threads) {
    throw OptionError("--rx-cpus must contain exactly --rx-threads CPUs");
  }
  if (!options.producer_cpus.empty() && options.producer_shards != 0 &&
      options.producer_cpus.size() != options.producer_shards) {
    throw OptionError(
        "--producer-cpus must contain exactly --producer-shards CPUs");
  }
}

} // namespace

Options ParseOptions(int argc, char *const argv[]) {
  if (argc < 0 || (argc > 0 && argv == nullptr)) {
    throw OptionError("invalid argc/argv");
  }

  Options options;
  std::unordered_set<std::string> seen;
  bool mpps_was_set = false;

  for (int index = 1; index < argc; ++index) {
    const std::string argument(argv[index] == nullptr ? "" : argv[index]);
    if (argument == "-h") {
      if (!seen.insert("help").second) {
        throw OptionError("duplicate option --help");
      }
      options.show_help = true;
      continue;
    }
    if (argument.rfind("--", 0) != 0 || argument.size() == 2) {
      throw OptionError("unexpected positional argument: " + argument);
    }

    const std::size_t equals = argument.find('=');
    const std::string name = argument.substr(
        2, equals == std::string::npos ? std::string::npos : equals - 2);
    if (!IsKnownOption(name)) {
      throw OptionError("unknown option --" + name);
    }
    if (!seen.insert(name).second) {
      throw OptionError("duplicate option --" + name);
    }

    if (name == "help") {
      if (equals != std::string::npos) {
        throw OptionError("--help does not take a value");
      }
      options.show_help = true;
      continue;
    }

    std::string value;
    if (equals != std::string::npos) {
      value = argument.substr(equals + 1);
    } else {
      if (++index >= argc || argv[index] == nullptr) {
        throw OptionError("--" + name + " requires a value");
      }
      value = argv[index];
    }

    if (name == "server") {
      options.server = value;
    } else if (name == "trace") {
      options.trace_path = value;
    } else if (name == "start-mpps") {
      options.start_mpps = ParseDouble(value, name);
    } else if (name == "mpps") {
      options.max_mpps = ParseDouble(value, name);
      mpps_was_set = true;
    } else if (name == "samples") {
      options.samples = ParseU32(value, name);
    } else if (name == "runtime") {
      options.runtime_seconds = ParseUnsigned<std::uint64_t>(value, name);
    } else if (name == "rampup") {
      options.rampup_seconds = ParseUnsigned<std::uint64_t>(value, name);
    } else if (name == "workers") {
      options.workers = ParseU32(value, name);
    } else if (name == "rx-threads") {
      options.rx_threads = ParseU32(value, name);
    } else if (name == "producer-shards") {
      options.producer_shards = ParseU32(value, name);
    } else if (name == "amp-factor") {
      options.amp_factor = ParseU32(value, name);
    } else if (name == "value-size") {
      options.value_size = ParseU32(value, name);
    } else if (name == "request-timeout-ms") {
      options.request_timeout_ms = ParseUnsigned<std::uint64_t>(value, name);
    } else if (name == "drain-ms") {
      options.drain_ms = ParseUnsigned<std::uint64_t>(value, name);
    } else if (name == "max-send-lag-us") {
      options.max_send_lag_us = ParseUnsigned<std::uint64_t>(value, name);
    } else if (name == "max-inflight") {
      options.max_inflight = ParseU32(value, name);
    } else if (name == "queue-depth") {
      options.queue_depth = ParseU32(value, name);
    } else if (name == "schedule-ahead-ms") {
      options.schedule_ahead_ms = ParseUnsigned<std::uint64_t>(value, name);
    } else if (name == "batch-size") {
      options.batch_size = ParseU32(value, name);
    } else if (name == "socket-buffer-mb") {
      options.socket_buffer_mb = ParseU32(value, name);
    } else if (name == "rx-queue-depth") {
      options.rx_queue_depth = ParseU32(value, name);
    } else if (name == "worker-cpus") {
      options.worker_cpus = ParseCpuList(value, name);
    } else if (name == "rx-cpus") {
      options.rx_cpus = ParseCpuList(value, name);
    } else if (name == "producer-cpus") {
      options.producer_cpus = ParseCpuList(value, name);
    } else if (name == "seed") {
      options.seed = ParseUnsigned<std::uint64_t>(value, name);
    }
  }

  ValidateOptionsImpl(options, !options.show_help, mpps_was_set);
  return options;
}

void ValidateOptions(const Options &options) {
  ValidateOptionsImpl(options, true, true);
}

void ValidateThreadPlacement(const Options &options,
                             std::size_t resolved_producer_shards) {
  if (!options.producer_cpus.empty() &&
      options.producer_cpus.size() != resolved_producer_shards) {
    throw OptionError("--producer-cpus must contain exactly the resolved "
                      "producer-shard count (" +
                      std::to_string(resolved_producer_shards) + ")");
  }

  cpu_set_t allowed;
  CPU_ZERO(&allowed);
  if (::sched_getaffinity(0, sizeof(allowed), &allowed) != 0) {
    throw OptionError("cannot read process CPU affinity: " +
                      std::string(std::strerror(errno)));
  }

  std::unordered_set<std::uint32_t> assigned;
  const auto validate = [&](const std::vector<std::uint32_t> &cpus,
                            std::string_view option) {
    for (const auto cpu : cpus) {
      if (!CPU_ISSET(static_cast<std::size_t>(cpu), &allowed)) {
        throw OptionError("CPU " + std::to_string(cpu) + " from --" +
                          std::string(option) +
                          " is outside the process affinity mask");
      }
      if (!assigned.insert(cpu).second) {
        throw OptionError("logical CPU " + std::to_string(cpu) +
                          " is assigned to more than one benchmark thread");
      }
    }
  };
  validate(options.worker_cpus, "worker-cpus");
  validate(options.rx_cpus, "rx-cpus");
  validate(options.producer_cpus, "producer-cpus");
}

std::string Usage(const std::string &program_name) {
  const std::string program =
      program_name.empty() ? "memcached-trace-generator" : program_name;
  std::ostringstream out;
  out << "Usage: " << program << " --server HOST:PORT --trace PATH --mpps N "
      << "[options]\n\n"
      << "Required:\n"
      << "  --server HOST:PORT          Memcached UDP endpoint\n"
      << "  --trace PATH                Trace CSV file or directory\n"
      << "  --mpps N                    Maximum aggregate Mops/s (0 < N <= "
         "1000)\n\n"
      << "Rate schedule:\n"
      << "  --start-mpps N              Sweep start (default: 0)\n"
      << "  --samples N                 Number of sweep points (default: 1)\n"
      << "  --runtime SEC               Steady seconds per point (default: "
         "10)\n"
      << "  --rampup SEC                100 ms ramp duration per point "
         "(default: 4)\n"
      << "  --seed N                    Poisson RNG seed (default: 1)\n\n"
      << "Replay and worker options:\n"
      << "  --workers N                 UDP workers (default: 10)\n"
      << "  --rx-threads N              Split RX pollers; 0=integrated "
         "(default: 0)\n"
      << "  --producer-shards N         Parallel file shards; 0=auto (default: "
         "0)\n"
      << "  --amp-factor N              Key-space amplification (default: 1)\n"
      << "  --value-size BYTES          Fixed SET value size (default: 4096)\n"
      << "  --request-timeout-ms MS     Response timeout (default: 200)\n"
      << "  --drain-ms MS               Final response drain (default: 1000)\n"
      << "  --max-send-lag-us US        Drop-after deadline lag (default: 5)\n"
      << "  --max-inflight N            Per-worker slots, <65536 (default: "
         "32768)\n"
      << "  --queue-depth N             Per-worker queue depth (default: "
         "8192)\n"
      << "  --schedule-ahead-ms MS      Producer lookahead (default: 20)\n"
      << "  --batch-size N              sendmmsg/recvmmsg batch (default: 32)\n"
      << "  --socket-buffer-mb MB       Requested socket buffer (default: 16)\n"
      << "  --rx-queue-depth N          Per-worker split-RX ring (default: "
         "4096)\n"
      << "  --worker-cpus LIST          Ordered worker CPU list, e.g. 4-23\n"
      << "  --rx-cpus LIST              Ordered split-RX CPU list\n"
      << "  --producer-cpus LIST        Ordered producer-shard CPU list\n"
      << "  -h, --help                  Show this help\n";
  return out.str();
}

} // namespace mcgen
