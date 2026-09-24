#include "options.h"
#include "poisson_scheduler.h"
#include "thread_affinity.h"

#include <chrono>
#include <cmath>
#include <cstdint>
#include <exception>
#include <functional>
#include <iostream>
#include <limits>
#include <mutex>
#include <sched.h>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

int failures = 0;

void Fail(const char *expression, const char *file, int line) {
  std::cerr << file << ':' << line << ": check failed: " << expression << '\n';
  ++failures;
}

#define CHECK(expression)                                                      \
  do {                                                                         \
    if (!(expression)) {                                                       \
      Fail(#expression, __FILE__, __LINE__);                                   \
    }                                                                          \
  } while (false)

bool NearlyEqual(double lhs, double rhs, double tolerance = 1e-12) {
  return std::abs(lhs - rhs) <= tolerance;
}

template <typename Exception, typename Function>
void CheckThrows(Function &&function, const char *expression, const char *file,
                 int line) {
  try {
    function();
  } catch (const Exception &) {
    return;
  } catch (const std::exception &error) {
    std::cerr << file << ':' << line << ": " << expression
              << " threw the wrong exception: " << error.what() << '\n';
    ++failures;
    return;
  }
  std::cerr << file << ':' << line << ": " << expression << " did not throw\n";
  ++failures;
}

#define CHECK_THROWS(exception, expression)                                    \
  CheckThrows<exception>([&] { (void)(expression); }, #expression, __FILE__,   \
                         __LINE__)

mcgen::Options Parse(std::vector<std::string> arguments) {
  std::vector<char *> argv;
  argv.reserve(arguments.size());
  for (std::string &argument : arguments) {
    argv.push_back(argument.data());
  }
  return mcgen::ParseOptions(static_cast<int>(argv.size()), argv.data());
}

std::vector<std::string> MinimalArgs() {
  return {"mcgen",   "--server",         "127.0.0.1:11211",
          "--trace", "/tmp/example.csv", "--mpps",
          "0.55"};
}

void TestOptionDefaults() {
  const mcgen::Options options = Parse(MinimalArgs());
  CHECK(options.server == "127.0.0.1:11211");
  CHECK(options.trace_path == "/tmp/example.csv");
  CHECK(NearlyEqual(options.start_mpps, 0.0));
  CHECK(NearlyEqual(options.max_mpps, 0.55));
  CHECK(options.samples == 1);
  CHECK(options.runtime_seconds == 10);
  CHECK(options.rampup_seconds == 4);
  CHECK(options.workers == 10);
  CHECK(options.rx_threads == 0);
  CHECK(options.producer_shards == 0);
  CHECK(options.amp_factor == 1);
  CHECK(options.value_size == 4096);
  CHECK(options.request_timeout_ms == 200);
  CHECK(options.drain_ms == 1000);
  CHECK(options.max_send_lag_us == 5);
  CHECK(options.max_inflight == 32768);
  CHECK(options.queue_depth == 8192);
  CHECK(options.schedule_ahead_ms == 20);
  CHECK(options.batch_size == 32);
  CHECK(options.socket_buffer_mb == 16);
  CHECK(options.rx_queue_depth == 4096);
  CHECK(options.worker_cpus.empty());
  CHECK(options.rx_cpus.empty());
  CHECK(options.producer_cpus.empty());
  CHECK(options.seed == 1);
  CHECK(!options.show_help);
}

void TestAllOptionsAndEqualsSyntax() {
  const mcgen::Options options = Parse({"mcgen",
                                        "--server=[::1]:22122",
                                        "--trace=/trace dir",
                                        "--start-mpps=0.1",
                                        "--mpps=0.9",
                                        "--samples=4",
                                        "--runtime=60",
                                        "--rampup=2",
                                        "--workers=12",
                                        "--rx-threads=2",
                                        "--producer-shards=6",
                                        "--amp-factor=100",
                                        "--value-size=0",
                                        "--request-timeout-ms=500",
                                        "--drain-ms=0",
                                        "--max-send-lag-us=0",
                                        "--max-inflight=65535",
                                        "--queue-depth=1024",
                                        "--schedule-ahead-ms=50",
                                        "--batch-size=64",
                                        "--socket-buffer-mb=32",
                                        "--rx-queue-depth=2048",
                                        "--worker-cpus=0-3,8-15",
                                        "--rx-cpus=16,17",
                                        "--producer-cpus=18-23",
                                        "--seed=0"});

  CHECK(options.server == "[::1]:22122");
  CHECK(options.trace_path == "/trace dir");
  CHECK(NearlyEqual(options.start_mpps, 0.1));
  CHECK(NearlyEqual(options.max_mpps, 0.9));
  CHECK(options.samples == 4);
  CHECK(options.runtime_seconds == 60);
  CHECK(options.rampup_seconds == 2);
  CHECK(options.workers == 12);
  CHECK(options.rx_threads == 2);
  CHECK(options.producer_shards == 6);
  CHECK(options.amp_factor == 100);
  CHECK(options.value_size == 0);
  CHECK(options.request_timeout_ms == 500);
  CHECK(options.drain_ms == 0);
  CHECK(options.max_send_lag_us == 0);
  CHECK(options.max_inflight == 65535);
  CHECK(options.queue_depth == 1024);
  CHECK(options.schedule_ahead_ms == 50);
  CHECK(options.batch_size == 64);
  CHECK(options.socket_buffer_mb == 32);
  CHECK(options.rx_queue_depth == 2048);
  CHECK(options.worker_cpus ==
        std::vector<std::uint32_t>({0, 1, 2, 3, 8, 9, 10, 11, 12, 13, 14, 15}));
  CHECK(options.rx_cpus == std::vector<std::uint32_t>({16, 17}));
  CHECK(options.producer_cpus ==
        std::vector<std::uint32_t>({18, 19, 20, 21, 22, 23}));
  CHECK(options.seed == 0);
}

void TestHelpAndUsage() {
  const mcgen::Options short_help = Parse({"mcgen", "-h"});
  const mcgen::Options long_help = Parse({"mcgen", "--help"});
  CHECK(short_help.show_help);
  CHECK(long_help.show_help);
  CHECK(mcgen::Usage("generator").find("Usage: generator") !=
        std::string::npos);
  CHECK(mcgen::Usage("").find("--mpps") != std::string::npos);
  CHECK_THROWS(mcgen::OptionError, Parse({"mcgen", "--help=yes"}));
  CHECK_THROWS(mcgen::OptionError,
               Parse({"mcgen", "--help", "--workers", "0"}));
}

void TestRequiredAndSyntaxErrors() {
  CHECK_THROWS(mcgen::OptionError,
               Parse({"mcgen", "--trace", "x", "--mpps", "0.1"}));
  CHECK_THROWS(mcgen::OptionError,
               Parse({"mcgen", "--server", "a:1", "--mpps", "0.1"}));
  CHECK_THROWS(mcgen::OptionError,
               Parse({"mcgen", "--server", "a:1", "--trace", "x"}));
  CHECK_THROWS(mcgen::OptionError, Parse({"mcgen", "positional"}));
  CHECK_THROWS(mcgen::OptionError, Parse({"mcgen", "--unknown", "1"}));
  CHECK_THROWS(mcgen::OptionError, Parse({"mcgen", "--help", "--help"}));
  CHECK_THROWS(mcgen::OptionError,
               Parse({"mcgen", "--mpps", "0.1", "--mpps", "0.2"}));
  CHECK_THROWS(mcgen::OptionError, Parse({"mcgen", "--server"}));
}

void TestInvalidValues() {
  const auto invalid = [](const std::string &option, const std::string &value) {
    auto args = MinimalArgs();
    for (std::size_t index = 0; index + 1 < args.size(); ++index) {
      if (args[index] == option) {
        args[index + 1] = value;
        return Parse(std::move(args));
      }
    }
    args.push_back(option);
    args.push_back(value);
    return Parse(std::move(args));
  };

  CHECK_THROWS(mcgen::OptionError, invalid("--server", "missing-port"));
  CHECK_THROWS(mcgen::OptionError, invalid("--server", "host:0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--server", "host:65536"));
  CHECK_THROWS(mcgen::OptionError, invalid("--server", "::1:11211"));
  CHECK_THROWS(mcgen::OptionError, invalid("--start-mpps", "-1"));
  CHECK_THROWS(mcgen::OptionError, invalid("--start-mpps", "0.6"));
  CHECK_THROWS(mcgen::OptionError, invalid("--mpps", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--mpps", "nan"));
  CHECK_THROWS(mcgen::OptionError, invalid("--mpps", "1001"));
  CHECK_THROWS(mcgen::OptionError, invalid("--samples", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--samples", "-1"));
  CHECK_THROWS(mcgen::OptionError, invalid("--runtime", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--rampup", "86401"));
  CHECK_THROWS(mcgen::OptionError, invalid("--workers", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--workers", "65536"));
  CHECK_THROWS(mcgen::OptionError, invalid("--rx-threads", "11"));
  CHECK_THROWS(mcgen::OptionError, invalid("--producer-shards", "11"));
  CHECK_THROWS(mcgen::OptionError, invalid("--amp-factor", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--amp-factor", "10001"));
  CHECK_THROWS(mcgen::OptionError, invalid("--value-size", "65218"));
  CHECK_THROWS(mcgen::OptionError, invalid("--request-timeout-ms", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--max-inflight", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--max-inflight", "65536"));
  CHECK_THROWS(mcgen::OptionError, invalid("--queue-depth", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--schedule-ahead-ms", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--batch-size", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--batch-size", "1025"));
  CHECK_THROWS(mcgen::OptionError,
               Parse({"mcgen", "--server", "a:1", "--trace", "x", "--mpps",
                      "0.1", "--queue-depth", "8", "--batch-size", "9"}));
  CHECK_THROWS(mcgen::OptionError, invalid("--socket-buffer-mb", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--socket-buffer-mb", "1025"));
  CHECK_THROWS(mcgen::OptionError, invalid("--rx-queue-depth", "0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--worker-cpus", "0-8"));
  CHECK_THROWS(mcgen::OptionError, invalid("--worker-cpus", "0-8,8"));
  CHECK_THROWS(mcgen::OptionError, invalid("--worker-cpus", "9-0"));
  CHECK_THROWS(mcgen::OptionError, invalid("--rx-cpus", "0"));
  CHECK_THROWS(
      mcgen::OptionError,
      Parse({"mcgen", "--server", "a:1", "--trace", "x", "--mpps", "0.1",
             "--workers", "2", "--rx-threads", "1", "--rx-cpus", "0,1"}));
  CHECK_THROWS(mcgen::OptionError,
               Parse({"mcgen", "--server", "a:1", "--trace", "x", "--mpps",
                      "0.1", "--workers", "2", "--producer-shards", "1",
                      "--producer-cpus", "0,1"}));
}

void TestDirectValidation() {
  mcgen::Options options;
  options.server = "localhost:11211";
  options.trace_path = "trace.csv";
  options.max_mpps = 0.55;
  mcgen::ValidateOptions(options);
  options.max_inflight = 65'536;
  CHECK_THROWS(mcgen::OptionError, mcgen::ValidateOptions(options));
}

void TestThreadPlacementValidation() {
  cpu_set_t allowed;
  CPU_ZERO(&allowed);
  CHECK(::sched_getaffinity(0, sizeof(allowed), &allowed) == 0);
  std::vector<std::uint32_t> available;
  for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
    if (CPU_ISSET(static_cast<std::size_t>(cpu), &allowed)) {
      available.push_back(static_cast<std::uint32_t>(cpu));
    }
  }
  CHECK(available.size() >= 3);
  if (available.size() < 3) {
    return;
  }

  mcgen::Options options;
  options.worker_cpus = {available[0]};
  options.rx_cpus = {available[1]};
  options.producer_cpus = {available[2]};
  mcgen::ValidateThreadPlacement(options, 1);

  options.producer_cpus = {available[1]};
  CHECK_THROWS(mcgen::OptionError, mcgen::ValidateThreadPlacement(options, 1));
  options.producer_cpus = {available[2], available[0]};
  CHECK_THROWS(mcgen::OptionError, mcgen::ValidateThreadPlacement(options, 1));

  int unavailable = -1;
  for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
    if (!CPU_ISSET(static_cast<std::size_t>(cpu), &allowed)) {
      unavailable = cpu;
      break;
    }
  }
  if (unavailable >= 0) {
    options.worker_cpus = {static_cast<std::uint32_t>(unavailable)};
    options.rx_cpus.clear();
    options.producer_cpus.clear();
    CHECK_THROWS(mcgen::OptionError,
                 mcgen::ValidateThreadPlacement(options, 0));
  }
}

void TestAffinityRepair() {
  cpu_set_t allowed;
  CPU_ZERO(&allowed);
  CHECK(::sched_getaffinity(0, sizeof(allowed), &allowed) == 0);
  int target_cpu = -1;
  for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
    if (CPU_ISSET(static_cast<std::size_t>(cpu), &allowed)) {
      target_cpu = cpu;
      break;
    }
  }
  CHECK(target_cpu >= 0);
  if (target_cpu < 0) {
    return;
  }

  bool repaired = false;
  bool repaired_again = true;
  int observed_cpu = -1;
  std::exception_ptr thread_error;
  std::thread thread([&] {
    try {
      repaired =
          mcgen::EnsureCurrentThreadPinned(target_cpu, "affinity repair test");
      repaired_again =
          mcgen::EnsureCurrentThreadPinned(target_cpu, "affinity repair test");
      observed_cpu = mcgen::CurrentCpu();
    } catch (...) {
      thread_error = std::current_exception();
    }
  });
  thread.join();
  CHECK(thread_error == nullptr);
  CHECK(repaired == (CPU_COUNT(&allowed) > 1));
  CHECK(!repaired_again);
  CHECK(observed_cpu == target_cpu);
}

void TestRateSweep() {
  const auto one = mcgen::BuildRateSweep(0.0, 0.55, 1);
  CHECK(one.size() == 1);
  CHECK(NearlyEqual(one[0], 0.55));

  const auto four = mcgen::BuildRateSweep(0.1, 0.5, 4);
  CHECK(four.size() == 4);
  CHECK(NearlyEqual(four[0], 0.2));
  CHECK(NearlyEqual(four[1], 0.3));
  CHECK(NearlyEqual(four[2], 0.4));
  CHECK(NearlyEqual(four[3], 0.5));

  const auto flat = mcgen::BuildRateSweep(0.5, 0.5, 3);
  CHECK(flat.size() == 3);
  CHECK(NearlyEqual(flat[0], 0.5));
  CHECK(NearlyEqual(flat[1], 0.5));
  CHECK(NearlyEqual(flat[2], 0.5));

  CHECK_THROWS(std::invalid_argument, mcgen::BuildRateSweep(0.0, 0.5, 0));
  CHECK_THROWS(std::invalid_argument, mcgen::BuildRateSweep(0.6, 0.5, 1));
  CHECK_THROWS(std::invalid_argument, mcgen::BuildRateSweep(-0.1, 0.5, 1));
}

void TestRampSchedule() {
  CHECK(mcgen::BuildRampSchedule(0.55, 0).empty());
  const auto ramp = mcgen::BuildRampSchedule(0.55, 1);
  CHECK(ramp.size() == 10);
  std::chrono::milliseconds total{0};
  double previous = 0.0;
  for (const auto &step : ramp) {
    CHECK(step.duration == std::chrono::milliseconds(100));
    CHECK(step.mpps > previous);
    previous = step.mpps;
    total += step.duration;
  }
  CHECK(NearlyEqual(ramp.front().mpps, 0.055));
  CHECK(NearlyEqual(ramp.back().mpps, 0.55));
  CHECK(total == std::chrono::seconds(1));
  CHECK_THROWS(std::invalid_argument, mcgen::BuildRampSchedule(0.0, 1));
}

void TestPoissonDeterminismAndOrdering() {
  mcgen::PoissonScheduler first(0.55, 1234);
  mcgen::PoissonScheduler second(0.55, 1234);
  mcgen::PoissonScheduler different(0.55, 4321);
  auto previous = std::chrono::nanoseconds::zero();
  bool saw_different_seed = false;
  for (std::size_t index = 0; index < 10'000; ++index) {
    const auto a = first.NextDeadline();
    const auto b = second.NextDeadline();
    const auto c = different.NextDeadline();
    CHECK(a == b);
    CHECK(a > previous);
    saw_different_seed = saw_different_seed || a != c;
    previous = a;
  }
  CHECK(saw_different_seed);
  CHECK(first.emitted() == 10'000);
  CHECK(first.last_deadline() == previous);
  CHECK(NearlyEqual(first.rate_mpps(), 0.55));

  mcgen::PoissonScheduler offset(0.55, 1, std::chrono::nanoseconds(1000));
  CHECK(offset.NextDeadline() > std::chrono::nanoseconds(1000));

  mcgen::PoissonScheduler maximum(1000.0, 1);
  previous = std::chrono::nanoseconds::zero();
  for (int index = 0; index < 1000; ++index) {
    const auto deadline = maximum.NextDeadline();
    CHECK(deadline > previous);
    previous = deadline;
  }
}

void TestPoissonMeanAndErrors() {
  constexpr std::size_t count = 200'000;
  mcgen::PoissonScheduler scheduler(0.55, 9);
  for (std::size_t index = 0; index < count; ++index) {
    scheduler.NextDeadline();
  }
  const double observed_mean_ns =
      static_cast<double>(scheduler.last_deadline().count()) /
      static_cast<double>(count);
  const double expected_mean_ns = 1000.0 / 0.55;
  CHECK(std::abs(observed_mean_ns - expected_mean_ns) / expected_mean_ns <
        0.02);

  CHECK_THROWS(std::invalid_argument, mcgen::PoissonScheduler(0.0, 1));
  CHECK_THROWS(std::invalid_argument, mcgen::PoissonScheduler(-1.0, 1));
  CHECK_THROWS(std::invalid_argument, mcgen::PoissonScheduler(1001.0, 1));
  CHECK_THROWS(std::invalid_argument,
               mcgen::PoissonScheduler(0.55, 1, std::chrono::nanoseconds(-1)));

  mcgen::PoissonScheduler overflow(
      0.55, 1,
      std::chrono::nanoseconds(std::numeric_limits<std::int64_t>::max()));
  CHECK_THROWS(std::overflow_error, overflow.NextDeadline());
}

} // namespace

int main() {
  TestOptionDefaults();
  TestAllOptionsAndEqualsSyntax();
  TestHelpAndUsage();
  TestRequiredAndSyntaxErrors();
  TestInvalidValues();
  TestDirectValidation();
  TestThreadPlacementValidation();
  TestAffinityRepair();
  TestRateSweep();
  TestRampSchedule();
  TestPoissonDeterminismAndOrdering();
  TestPoissonMeanAndErrors();

  if (failures != 0) {
    std::cerr << failures << " test(s) failed\n";
    return 1;
  }
  std::cout << "options/scheduler tests passed\n";
  return 0;
}
