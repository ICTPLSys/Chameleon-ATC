#include "options.h"
#include "poisson_scheduler.h"
#include "stats.h"
#include "thread_affinity.h"
#include "trace_reader.h"
#include "udp_worker.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace mcgen {
namespace {

using Nanoseconds = std::chrono::nanoseconds;

constexpr std::uint64_t kSampleSeedStride = 0x9e3779b97f4a7c15ULL;
constexpr std::uint64_t kShardSeedStride = 0xbf58476d1ce4e5b9ULL;
constexpr std::uint64_t kPhaseSeedStride = 0x94d049bb133111ebULL;
constexpr std::uint64_t kWorkerSeedStride = 0xd6e8feb86659fd93ULL;
constexpr std::int64_t kAffinityCheckIntervalNs = 100'000'000;

std::int64_t CheckedAdd(std::int64_t lhs, std::int64_t rhs,
                        const char *description) {
  if (rhs > 0 && lhs > std::numeric_limits<std::int64_t>::max() - rhs) {
    throw std::overflow_error(std::string(description) +
                              " exceeds monotonic-clock range");
  }
  return lhs + rhs;
}

bool WaitForLookahead(std::int64_t deadline_ns, std::int64_t ahead_ns,
                      std::int64_t phase_end_ns,
                      const std::atomic<bool> &cancel) {
  for (;;) {
    if (cancel.load(std::memory_order_acquire)) {
      throw std::runtime_error("worker cancelled the sample");
    }
    const auto now_ns = MonotonicNowNs();
    if (now_ns >= phase_end_ns) {
      return false;
    }
    const auto sleep_ns = deadline_ns - ahead_ns - now_ns;
    if (sleep_ns <= 0) {
      return true;
    }
    const auto until_end_ns = phase_end_ns - now_ns;
    const auto chunk_ns =
        std::min({sleep_ns, until_end_ns, std::int64_t{10'000'000}});
    std::this_thread::sleep_for(Nanoseconds(chunk_ns));
  }
}

std::optional<std::uint64_t> ReadHostUdpReceiveBufferErrors() noexcept {
  try {
    std::ifstream input("/proc/net/snmp");
    std::string header;
    while (std::getline(input, header)) {
      if (header.rfind("Udp:", 0) != 0) {
        continue;
      }
      std::string values;
      if (!std::getline(input, values) || values.rfind("Udp:", 0) != 0) {
        return std::nullopt;
      }
      std::istringstream header_stream(header);
      std::istringstream value_stream(values);
      std::string header_label;
      std::string value_label;
      if (!(header_stream >> header_label) || !(value_stream >> value_label) ||
          header_label != "Udp:" || value_label != "Udp:") {
        return std::nullopt;
      }
      std::string name;
      std::uint64_t value = 0;
      while (header_stream >> name) {
        if (!(value_stream >> value)) {
          return std::nullopt;
        }
        if (name == "RcvbufErrors") {
          return value;
        }
      }
      return std::nullopt;
    }
  } catch (...) {
  }
  return std::nullopt;
}

struct ProducerStats {
  explicit ProducerStats(std::size_t workers)
      : enqueued_by_worker(workers, 0), queue_full_by_worker(workers, 0),
        queue_high_water_by_worker(workers, 0) {}

  Counters total;
  Counters measured;
  bool schedule_complete = true;
  std::int64_t maximum_lag_ns = 0;
  std::vector<std::uint64_t> enqueued_by_worker;
  std::vector<std::uint64_t> queue_full_by_worker;
  std::vector<std::size_t> queue_high_water_by_worker;
  int requested_cpu = -1;
  int start_cpu = -1;
  int end_cpu = -1;
  std::uint64_t affinity_repairs = 0;
  std::int64_t next_affinity_check_ns = 0;
};

struct ProducerShard {
  TraceReader *trace = nullptr;
  std::size_t first_worker = 0;
  std::size_t worker_count = 0;
};

void CountProducer(ProducerStats &stats, bool measured,
                   std::uint64_t Counters::*field, std::uint64_t amount = 1) {
  stats.total.*field += amount;
  if (measured) {
    stats.measured.*field += amount;
  }
}

bool MaintainProducerAffinity(ProducerStats &stats, std::int64_t now_ns) {
  if (now_ns < stats.next_affinity_check_ns) {
    return false;
  }
  if (EnsureCurrentThreadPinned(stats.requested_cpu, "producer")) {
    ++stats.affinity_repairs;
  }
  stats.next_affinity_check_ns =
      CheckedAdd(now_ns, kAffinityCheckIntervalNs, "affinity check");
  return true;
}

bool SchedulePhase(
    double rate_mpps, std::int64_t phase_start_ns, std::int64_t phase_end_ns,
    std::int64_t measure_start_ns, std::int64_t measure_end_ns,
    std::uint64_t seed, std::int64_t ahead_ns, std::int64_t max_lag_ns,
    const ProducerShard &shard,
    std::vector<std::unique_ptr<SpscQueue<ScheduledRequest>>> &queues,
    ProducerStats &stats, const std::atomic<bool> &cancel) {
  if (phase_end_ns <= phase_start_ns) {
    return true;
  }
  if (shard.trace == nullptr || shard.worker_count == 0) {
    throw std::logic_error("producer shard has no trace or workers");
  }

  using Arrival = std::pair<std::int64_t, std::size_t>;
  std::priority_queue<Arrival, std::vector<Arrival>, std::greater<Arrival>>
      arrivals;
  std::vector<PoissonScheduler> schedulers;
  schedulers.reserve(shard.worker_count);
  const auto worker_rate_mpps = rate_mpps / static_cast<double>(queues.size());
  for (std::size_t local_worker = 0; local_worker < shard.worker_count;
       ++local_worker) {
    const auto worker = shard.first_worker + local_worker;
    schedulers.emplace_back(worker_rate_mpps,
                            seed + static_cast<std::uint64_t>(worker) *
                                       kWorkerSeedStride);
    arrivals.emplace(schedulers.back().NextDeadline().count(), local_worker);
  }

  for (;;) {
    if (cancel.load(std::memory_order_acquire)) {
      throw std::runtime_error("worker cancelled the sample");
    }
    if (MonotonicNowNs() >= phase_end_ns) {
      return false;
    }

    const auto [relative, local_worker] = arrivals.top();
    if (relative >= phase_end_ns - phase_start_ns) {
      return true;
    }
    arrivals.pop();
    arrivals.emplace(schedulers[local_worker].NextDeadline().count(),
                     local_worker);
    const auto deadline_ns =
        CheckedAdd(phase_start_ns, relative, "Poisson request deadline");
    if (!WaitForLookahead(deadline_ns, ahead_ns, phase_end_ns, cancel)) {
      return false;
    }
    auto now_ns = MonotonicNowNs();
    if (MaintainProducerAffinity(stats, now_ns)) {
      // Account for the occasional affinity syscall in the admission lag.
      now_ns = MonotonicNowNs();
    }

    const bool measured =
        deadline_ns >= measure_start_ns && deadline_ns < measure_end_ns;
    CountProducer(stats, measured, &Counters::scheduled);

    auto lag_ns = std::max<std::int64_t>(0, now_ns - deadline_ns);
    stats.maximum_lag_ns = std::max(stats.maximum_lag_ns, lag_ns);
    if (lag_ns > max_lag_ns) {
      shard.trace->skip(1);
      CountProducer(stats, measured, &Counters::producer_late);
      continue;
    }

    ScheduledRequest scheduled;
    scheduled.request = shard.trace->next_shared();
    scheduled.deadline_ns = deadline_ns;
    scheduled.measured = measured;

    now_ns = MonotonicNowNs();
    lag_ns = std::max<std::int64_t>(0, now_ns - deadline_ns);
    stats.maximum_lag_ns = std::max(stats.maximum_lag_ns, lag_ns);
    if (lag_ns > max_lag_ns) {
      CountProducer(stats, measured, &Counters::producer_late);
      continue;
    }

    const auto worker = shard.first_worker + local_worker;
    if (!queues[worker]->try_push(std::move(scheduled))) {
      CountProducer(stats, measured, &Counters::queue_full);
      ++stats.queue_full_by_worker[worker];
      continue;
    }
    ++stats.enqueued_by_worker[worker];
    stats.queue_high_water_by_worker[worker] =
        std::max(stats.queue_high_water_by_worker[worker],
                 queues[worker]->size_approx());
  }
}

void RunProducer(
    const Options &options, std::size_t sample_index, double target_mpps,
    std::size_t shard_index, const ProducerShard &shard, std::int64_t start_ns,
    std::int64_t stable_start_ns, std::int64_t measure_start_ns,
    std::int64_t stable_end_ns,
    std::vector<std::unique_ptr<SpscQueue<ScheduledRequest>>> &queues,
    ProducerStats &stats, const std::atomic<bool> &cancel) {
  const auto ramp = BuildRampSchedule(target_mpps, options.rampup_seconds);
  const auto ahead_ns =
      static_cast<std::int64_t>(options.schedule_ahead_ms) * 1'000'000LL;
  const auto max_lag_ns =
      static_cast<std::int64_t>(options.max_send_lag_us) * 1'000LL;
  std::int64_t phase_start_ns = start_ns;
  std::uint64_t phase_number = 0;

  const auto phase_seed = [&](std::uint64_t phase) {
    return options.seed +
           static_cast<std::uint64_t>(sample_index) * kSampleSeedStride +
           static_cast<std::uint64_t>(shard_index) * kShardSeedStride +
           phase * kPhaseSeedStride;
  };

  for (const auto &step : ramp) {
    const auto phase_end_ns = CheckedAdd(
        phase_start_ns,
        std::chrono::duration_cast<Nanoseconds>(step.duration).count(),
        "ramp phase end");
    const bool complete =
        SchedulePhase(step.mpps, phase_start_ns, phase_end_ns, measure_start_ns,
                      stable_end_ns, phase_seed(phase_number++), ahead_ns,
                      max_lag_ns, shard, queues, stats, cancel);
    stats.schedule_complete = stats.schedule_complete && complete;
    phase_start_ns = phase_end_ns;
  }

  const bool complete =
      SchedulePhase(target_mpps, stable_start_ns, stable_end_ns,
                    measure_start_ns, stable_end_ns, phase_seed(phase_number),
                    ahead_ns, max_lag_ns, shard, queues, stats, cancel);
  stats.schedule_complete = stats.schedule_complete && complete;
}

std::vector<std::unique_ptr<TraceReader>>
BuildTraceShards(const Options &options, std::size_t &trace_file_count) {
  TraceReader discovery(options.trace_path, options.amp_factor);
  const auto files = discovery.files();
  trace_file_count = files.size();
  const auto automatic = std::min<std::size_t>(files.size(), options.workers);
  const auto shard_count =
      options.producer_shards == 0
          ? automatic
          : static_cast<std::size_t>(options.producer_shards);
  if (shard_count == 0 || shard_count > files.size()) {
    throw OptionError("--producer-shards must be in [1, trace file count] "
                      "(or 0 for auto)");
  }

  std::vector<std::vector<std::filesystem::path>> partitions(shard_count);
  for (std::size_t index = 0; index < files.size(); ++index) {
    partitions[index % shard_count].push_back(files[index]);
  }

  std::vector<std::unique_ptr<TraceReader>> traces;
  traces.reserve(shard_count);
  for (auto &partition : partitions) {
    traces.push_back(std::make_unique<TraceReader>(std::move(partition),
                                                   options.amp_factor));
  }
  return traces;
}

SampleResult RunSample(const Options &options, std::size_t sample_index,
                       double target_mpps,
                       std::vector<std::unique_ptr<TraceReader>> &traces) {
  constexpr std::int64_t kStartDelayNs = 100'000'000;
  const auto host_udp_errors_before = ReadHostUdpReceiveBufferErrors();
  const auto ramp_ns =
      static_cast<std::int64_t>(options.rampup_seconds) * 1'000'000'000LL;
  const auto runtime_ns =
      static_cast<std::int64_t>(options.runtime_seconds) * 1'000'000'000LL;

  std::vector<std::unique_ptr<SpscQueue<ScheduledRequest>>> queues;
  queues.reserve(options.workers);
  for (std::uint32_t index = 0; index < options.workers; ++index) {
    queues.push_back(
        std::make_unique<SpscQueue<ScheduledRequest>>(options.queue_depth));
  }

  std::atomic<bool> producer_done{false};
  std::atomic<bool> cancel{false};
  WorkerConfig worker_config;
  worker_config.server = options.server;
  worker_config.value_size = options.value_size;
  worker_config.request_timeout_ms = options.request_timeout_ms;
  worker_config.max_send_lag_us = options.max_send_lag_us;
  worker_config.max_inflight = options.max_inflight;
  worker_config.batch_size = options.batch_size;
  worker_config.socket_buffer_mb = options.socket_buffer_mb;
  worker_config.split_receive = options.rx_threads > 0;
  worker_config.receive_queue_depth = options.rx_queue_depth;
  worker_config.drain_deadline_ns = std::numeric_limits<std::int64_t>::max();

  std::vector<std::unique_ptr<UdpWorker>> workers;
  workers.reserve(options.workers);
  std::unique_ptr<UdpReceiverPool> receiver_pool;
  try {
    for (std::uint32_t index = 0; index < options.workers; ++index) {
      worker_config.worker_cpu =
          options.worker_cpus.empty()
              ? -1
              : static_cast<int>(options.worker_cpus[index]);
      workers.push_back(std::make_unique<UdpWorker>(
          index, worker_config, *queues[index], producer_done, cancel));
      workers.back()->Prepare();
    }
    if (options.rx_threads > 0) {
      std::vector<RxEndpoint> endpoints;
      endpoints.reserve(workers.size());
      for (auto &worker : workers) {
        endpoints.push_back(worker->receive_endpoint());
      }
      receiver_pool = std::make_unique<UdpReceiverPool>(
          std::move(endpoints), options.rx_threads, options.rx_cpus, cancel);
      receiver_pool->Start();
    }
    for (auto &worker : workers) {
      worker->Start();
    }
  } catch (...) {
    cancel.store(true, std::memory_order_release);
    producer_done.store(true, std::memory_order_release);
    for (auto &worker : workers) {
      worker->Join();
    }
    if (receiver_pool) {
      receiver_pool->Stop();
      receiver_pool->Join();
    }
    throw;
  }

  std::int64_t start_ns = 0;
  std::int64_t stable_start_ns = 0;
  std::int64_t measure_start_ns = 0;
  std::int64_t stable_end_ns = 0;
  std::int64_t drain_end_ns = 0;
  try {
    start_ns = CheckedAdd(MonotonicNowNs(), kStartDelayNs, "sample start");
    stable_start_ns = CheckedAdd(start_ns, ramp_ns, "ramp end");
    measure_start_ns =
        CheckedAdd(stable_start_ns, runtime_ns / 10, "measurement start");
    stable_end_ns = CheckedAdd(stable_start_ns, runtime_ns, "steady-state end");
    drain_end_ns = CheckedAdd(
        stable_end_ns,
        static_cast<std::int64_t>(options.drain_ms) * 1'000'000LL, "drain end");
    for (auto &worker : workers) {
      worker->SetDrainDeadline(drain_end_ns);
    }
  } catch (...) {
    cancel.store(true, std::memory_order_release);
    producer_done.store(true, std::memory_order_release);
    for (auto &worker : workers) {
      worker->Join();
    }
    if (receiver_pool) {
      receiver_pool->Stop();
      receiver_pool->Join();
    }
    throw;
  }

  const std::size_t shard_count = traces.size();
  std::vector<ProducerShard> shards(shard_count);
  std::size_t next_worker = 0;
  for (std::size_t shard = 0; shard < shard_count; ++shard) {
    const std::size_t remaining_workers = options.workers - next_worker;
    const std::size_t remaining_shards = shard_count - shard;
    const std::size_t count =
        (remaining_workers + remaining_shards - 1) / remaining_shards;
    shards[shard] = ProducerShard{traces[shard].get(), next_worker, count};
    next_worker += count;
  }

  std::vector<ProducerStats> producer_stats;
  producer_stats.reserve(shard_count);
  for (std::size_t shard = 0; shard < shard_count; ++shard) {
    producer_stats.emplace_back(options.workers);
    producer_stats.back().requested_cpu =
        options.producer_cpus.empty()
            ? -1
            : static_cast<int>(options.producer_cpus[shard]);
  }

  std::mutex error_mutex;
  std::exception_ptr producer_error;
  std::vector<std::thread> producer_threads;
  producer_threads.reserve(shard_count);
  try {
    for (std::size_t shard = 0; shard < shard_count; ++shard) {
      producer_threads.emplace_back([&, shard] {
        try {
          NameCurrentThread("mc-prod-" + std::to_string(shard));
          PinCurrentThread(producer_stats[shard].requested_cpu,
                           "producer " + std::to_string(shard));
          producer_stats[shard].start_cpu = CurrentCpu();
          RunProducer(options, sample_index, target_mpps, shard, shards[shard],
                      start_ns, stable_start_ns, measure_start_ns,
                      stable_end_ns, queues, producer_stats[shard], cancel);
          if (EnsureCurrentThreadPinned(producer_stats[shard].requested_cpu,
                                        "producer " + std::to_string(shard))) {
            ++producer_stats[shard].affinity_repairs;
          }
          producer_stats[shard].end_cpu = CurrentCpu();
        } catch (...) {
          producer_stats[shard].end_cpu = CurrentCpu();
          {
            std::lock_guard<std::mutex> lock(error_mutex);
            if (producer_error == nullptr) {
              producer_error = std::current_exception();
            }
          }
          cancel.store(true, std::memory_order_release);
        }
      });
    }
  } catch (...) {
    cancel.store(true, std::memory_order_release);
    for (auto &thread : producer_threads) {
      thread.join();
    }
    producer_done.store(true, std::memory_order_release);
    for (auto &worker : workers) {
      worker->Join();
    }
    if (receiver_pool) {
      receiver_pool->Stop();
      receiver_pool->Join();
    }
    throw;
  }

  for (auto &thread : producer_threads) {
    thread.join();
  }
  const auto producer_finish_ns = MonotonicNowNs();
  producer_done.store(true, std::memory_order_release);
  for (auto &worker : workers) {
    worker->Join();
  }
  if (receiver_pool) {
    receiver_pool->Stop();
    receiver_pool->Join();
    for (auto &worker : workers) {
      worker->CollectReceiverStats();
    }
  }
  const auto sample_finish_ns = MonotonicNowNs();
  const auto host_udp_errors_after = ReadHostUdpReceiveBufferErrors();
  std::uint64_t affinity_repairs = 0;

  for (const auto &worker : workers) {
    if (!worker->result().error.empty()) {
      throw std::runtime_error(worker->result().error);
    }
  }
  if (receiver_pool) {
    for (std::size_t index = 0; index < receiver_pool->results().size();
         ++index) {
      const auto &receiver = receiver_pool->results()[index];
      if (!receiver.error.empty()) {
        throw std::runtime_error("RX thread " + std::to_string(index) + ": " +
                                 receiver.error);
      }
      affinity_repairs += receiver.affinity_repairs;
      std::cerr << "sample=" << sample_index << " rx_thread=" << index
                << " requested_cpu=" << receiver.requested_cpu
                << " start_cpu=" << receiver.start_cpu
                << " end_cpu=" << receiver.end_cpu
                << " affinity_repairs=" << receiver.affinity_repairs
                << " epoll_wakeups=" << receiver.epoll_wakeups
                << " receive_syscalls=" << receiver.receive_syscalls << '\n';
    }
  }
  if (producer_error != nullptr) {
    std::rethrow_exception(producer_error);
  }

  SampleResult result;
  result.sample = sample_index;
  result.target_mpps = target_mpps;
  result.workers = options.workers;
  result.producer_shards = static_cast<std::uint32_t>(shard_count);
  result.measured_seconds =
      static_cast<double>(stable_end_ns - measure_start_ns) / 1e9;
  result.producer_wall_seconds =
      static_cast<double>(producer_finish_ns - start_ns) / 1e9;
  result.sample_wall_seconds =
      static_cast<double>(sample_finish_ns - start_ns) / 1e9;
  result.schedule_complete = true;
  if (host_udp_errors_before.has_value() && host_udp_errors_after.has_value() &&
      *host_udp_errors_after >= *host_udp_errors_before) {
    result.host_udp_receive_buffer_errors =
        *host_udp_errors_after - *host_udp_errors_before;
  }
  result.minimum_socket_send_buffer_bytes = std::numeric_limits<int>::max();
  result.minimum_socket_receive_buffer_bytes = std::numeric_limits<int>::max();

  std::vector<std::uint64_t> enqueued_by_worker(options.workers, 0);
  std::vector<std::uint64_t> queue_full_by_worker(options.workers, 0);
  std::vector<std::size_t> producer_queue_high_water(options.workers, 0);
  for (const auto &producer : producer_stats) {
    affinity_repairs += producer.affinity_repairs;
    result.counters += producer.measured;
    result.schedule_complete =
        result.schedule_complete && producer.schedule_complete;
    result.maximum_producer_lag_ns =
        std::max(result.maximum_producer_lag_ns, producer.maximum_lag_ns);
    for (std::size_t worker = 0; worker < options.workers; ++worker) {
      enqueued_by_worker[worker] += producer.enqueued_by_worker[worker];
      queue_full_by_worker[worker] += producer.queue_full_by_worker[worker];
      producer_queue_high_water[worker] =
          std::max(producer_queue_high_water[worker],
                   producer.queue_high_water_by_worker[worker]);
    }
  }

  for (std::size_t index = 0; index < producer_stats.size(); ++index) {
    const auto &producer = producer_stats[index];
    std::cerr << "sample=" << sample_index << " producer=" << index
              << " requested_cpu=" << producer.requested_cpu
              << " start_cpu=" << producer.start_cpu
              << " end_cpu=" << producer.end_cpu
              << " affinity_repairs=" << producer.affinity_repairs
              << " maximum_lag_us="
              << static_cast<double>(producer.maximum_lag_ns) / 1000.0 << '\n';
  }

  Counters total_counters;
  for (const auto &producer : producer_stats) {
    total_counters += producer.total;
  }
  for (std::size_t index = 0; index < workers.size(); ++index) {
    const auto &worker_result = workers[index]->result();
    affinity_repairs += worker_result.affinity_repairs;
    result.counters += worker_result.measured;
    total_counters += worker_result.total;
    const auto dropped = result.histogram.add(worker_result.histogram);
    if (dropped != 0) {
      result.counters.latency_out_of_range += dropped;
    }
    result.minimum_socket_send_buffer_bytes =
        std::min(result.minimum_socket_send_buffer_bytes,
                 worker_result.socket_send_buffer_bytes);
    result.minimum_socket_receive_buffer_bytes =
        std::min(result.minimum_socket_receive_buffer_bytes,
                 worker_result.socket_receive_buffer_bytes);
    result.socket_receive_drops += worker_result.socket_receive_queue_drops;
    result.receive_handoff_drops += worker_result.receive_handoff_drops;
    result.maximum_receive_queue_depth =
        std::max(result.maximum_receive_queue_depth,
                 worker_result.receive_queue_high_water);
    result.maximum_queue_depth = std::max(
        result.maximum_queue_depth, std::max(producer_queue_high_water[index],
                                             worker_result.queue_high_water));

    if (enqueued_by_worker[index] != worker_result.offered ||
        worker_result.offered != worker_result.dequeued) {
      throw std::runtime_error(
          "per-worker ledger invariant failed for worker " +
          std::to_string(index));
    }
    const auto worker_terminal =
        worker_result.total.sent + worker_result.total.late +
        worker_result.total.no_slot + worker_result.total.send_error +
        worker_result.total.stopped_at_deadline;
    const auto worker_responses = worker_result.total.completed +
                                  worker_result.total.timeout +
                                  worker_result.total.outstanding;
    const auto worker_outcomes =
        worker_result.total.get_hit + worker_result.total.get_miss +
        worker_result.total.set_success + worker_result.total.delete_success +
        worker_result.total.delete_miss + worker_result.total.server_error;
    const auto worker_latency_samples =
        static_cast<std::uint64_t>(worker_result.histogram.count()) +
        worker_result.measured.latency_out_of_range;
    if (worker_result.offered != worker_terminal ||
        worker_result.total.sent != worker_responses ||
        worker_result.total.completed != worker_outcomes ||
        worker_result.measured.completed != worker_latency_samples ||
        worker_result.sent != worker_result.total.sent ||
        worker_result.late != worker_result.total.late) {
      throw std::runtime_error(
          "per-worker terminal invariant failed for worker " +
          std::to_string(index));
    }
    std::cerr
        << "sample=" << sample_index << " worker=" << index
        << " enqueued=" << enqueued_by_worker[index]
        << " queue_full=" << queue_full_by_worker[index]
        << " dequeued=" << worker_result.dequeued
        << " sent=" << worker_result.sent << " late=" << worker_result.late
        << " queue_high_water="
        << std::max(producer_queue_high_water[index],
                    worker_result.queue_high_water)
        << " rx_batches=" << worker_result.receive_batches
        << " rx_datagrams=" << worker_result.receive_datagrams
        << " rx_deadline_yields=" << worker_result.receive_deadline_yields
        << " rx_slice_yields=" << worker_result.receive_slice_yields
        << " rxq_drops=" << worker_result.socket_receive_queue_drops
        << " rx_handoff_drops=" << worker_result.receive_handoff_drops
        << " rx_queue_high_water=" << worker_result.receive_queue_high_water
        << " completed=" << worker_result.total.completed
        << " no_slot=" << worker_result.total.no_slot
        << " send_error=" << worker_result.total.send_error
        << " timeout=" << worker_result.total.timeout
        << " stopped_at_deadline=" << worker_result.total.stopped_at_deadline
        << " requested_sockbuf_bytes="
        << worker_result.requested_socket_buffer_bytes
        << " actual_sndbuf_bytes=" << worker_result.socket_send_buffer_bytes
        << " actual_rcvbuf_bytes=" << worker_result.socket_receive_buffer_bytes
        << " sndbuf_set_errno=" << worker_result.socket_send_buffer_set_error
        << " rcvbuf_set_errno=" << worker_result.socket_receive_buffer_set_error
        << " sndbuf_get_errno=" << worker_result.socket_send_buffer_get_error
        << " rcvbuf_get_errno=" << worker_result.socket_receive_buffer_get_error
        << " send_syscalls=" << worker_result.send_syscalls
        << " sendmsg_calls=" << worker_result.sendmsg_calls
        << " sendmmsg_calls=" << worker_result.sendmmsg_calls
        << " send_messages_attempted=" << worker_result.send_messages_attempted
        << " send_messages_returned=" << worker_result.send_messages_returned
        << " average_send_batch="
        << (worker_result.send_syscalls == 0
                ? 0.0
                : static_cast<double>(worker_result.send_messages_returned) /
                      static_cast<double>(worker_result.send_syscalls))
        << " maximum_send_batch=" << worker_result.maximum_send_batch
        << " partial_send_calls=" << worker_result.partial_send_calls
        << " maximum_actual_send_lag_us="
        << static_cast<double>(worker_result.maximum_actual_send_lag_ns) /
               1000.0
        << " active_high_water=" << worker_result.active_high_water
        << " cooldown_high_water=" << worker_result.cooldown_high_water
        << " free_ids_low_water=" << worker_result.free_ids_low_water
        << " requested_cpu=" << worker_result.requested_worker_cpu
        << " start_cpu=" << worker_result.worker_start_cpu
        << " end_cpu=" << worker_result.worker_end_cpu
        << " affinity_repairs=" << worker_result.affinity_repairs
        << " rxq_ovfl_enabled="
        << (worker_result.socket_receive_queue_overflow_enabled ? 1 : 0)
        << " rxq_ovfl_errno="
        << worker_result.socket_receive_queue_overflow_error << '\n';
  }

  const auto total_invariant_error = CounterInvariantError(total_counters);
  if (!total_invariant_error.empty()) {
    throw std::runtime_error("total counter invariant failed: " +
                             total_invariant_error);
  }

  result.counters.malformed_fragment = total_counters.malformed_fragment;
  result.counters.duplicate_fragment = total_counters.duplicate_fragment;
  result.counters.stale_fragment = total_counters.stale_fragment;
  result.affinity_repairs = affinity_repairs;

  result.invalid_reason = LoadValidityError(result);
  result.load_valid = result.invalid_reason.empty();
  return result;
}

} // namespace
} // namespace mcgen

int main(int argc, char **argv) {
  try {
    const auto options = mcgen::ParseOptions(argc, argv);
    if (options.show_help) {
      std::cout << mcgen::Usage(argc > 0 ? argv[0] : "");
      return 0;
    }

    std::size_t trace_file_count = 0;
    auto traces = mcgen::BuildTraceShards(options, trace_file_count);
    mcgen::ValidateThreadPlacement(options, traces.size());
    const auto rates = mcgen::BuildRateSweep(options.start_mpps,
                                             options.max_mpps, options.samples);

    std::cerr << "trace_files=" << trace_file_count
              << " producer_shards=" << traces.size()
              << " workers=" << options.workers
              << " rx_threads=" << options.rx_threads
              << " value_size=" << options.value_size
              << " amp_factor=" << options.amp_factor
              << " runtime_per_sample_s=" << options.runtime_seconds
              << " measured_fraction=0.9 transport=udp\n";
    mcgen::PrintCsvHeader(std::cout);
    for (std::size_t index = 0; index < rates.size(); ++index) {
      auto result = mcgen::RunSample(options, index + 1, rates[index], traces);
      std::cerr << "sample=" << result.sample
                << " load_valid=" << (result.load_valid ? 1 : 0)
                << " invalid_reason="
                << (result.invalid_reason.empty() ? "none"
                                                  : result.invalid_reason)
                << " schedule_complete=" << (result.schedule_complete ? 1 : 0)
                << " producer_wall_s=" << result.producer_wall_seconds
                << " sample_wall_s=" << result.sample_wall_seconds
                << " socket_sndbuf_min_bytes="
                << result.minimum_socket_send_buffer_bytes
                << " socket_rcvbuf_min_bytes="
                << result.minimum_socket_receive_buffer_bytes
                << " socket_rxq_drops=" << result.socket_receive_drops
                << " rx_handoff_drops=" << result.receive_handoff_drops
                << " affinity_repairs=" << result.affinity_repairs
                << " maximum_rx_queue_depth="
                << result.maximum_receive_queue_depth
                << " host_udp_rcvbuf_errors_delta=";
      if (result.host_udp_receive_buffer_errors.has_value()) {
        std::cerr << *result.host_udp_receive_buffer_errors;
      } else {
        std::cerr << "NA";
      }
      std::cerr << '\n';
      const auto invariant_error =
          mcgen::CounterInvariantError(result.counters);
      if (!invariant_error.empty()) {
        throw std::runtime_error("counter invariant failed: " +
                                 invariant_error);
      }
      const auto latency_accounted =
          static_cast<std::uint64_t>(result.histogram.count()) +
          result.counters.latency_out_of_range;
      if (latency_accounted != result.counters.completed) {
        throw std::runtime_error(
            "counter invariant failed: latency samples do not equal "
            "completed responses");
      }
      mcgen::PrintCsvRow(std::cout, result);
      std::cout.flush();
    }
    return 0;
  } catch (const mcgen::OptionError &error) {
    std::cerr << "error: " << error.what() << "\n\n"
              << mcgen::Usage(argc > 0 ? argv[0] : "");
    return 2;
  } catch (const std::exception &error) {
    std::cerr << "error: " << error.what() << '\n';
    return 1;
  }
}
