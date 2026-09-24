#pragma once

#include "spsc_queue.h"

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace mcgen {

constexpr std::size_t kReceiveDatagramSize = 2048;

struct ReceivedDatagram {
  std::array<std::uint8_t, kReceiveDatagramSize> bytes{};
  std::uint32_t length = 0;
  int flags = 0;
  std::int64_t received_ns = 0;
};

struct RxChannelStats {
  std::uint64_t receive_batches = 0;
  std::uint64_t receive_datagrams = 0;
  std::uint64_t socket_receive_queue_drops = 0;
  std::uint64_t handoff_drops = 0;
  std::size_t queue_high_water = 0;
};

// One channel connects one shared RX poller (producer) with one UDP worker
// (consumer). RX threads only copy raw datagrams; request IDs, reassembly,
// timeout state, counters, and histograms remain worker-owned.
class RxChannel {
public:
  explicit RxChannel(std::size_t queue_depth);
  ~RxChannel();

  RxChannel(const RxChannel &) = delete;
  RxChannel &operator=(const RxChannel &) = delete;

  bool Enqueue(const std::uint8_t *bytes, std::size_t length, int flags,
               std::int64_t received_ns);
  bool Pop(ReceivedDatagram &datagram);
  template <typename Consumer> bool Consume(Consumer &&consumer) {
    return queue_.consume_front(std::forward<Consumer>(consumer));
  }
  const ReceivedDatagram *front() const { return queue_.front(); }
  bool empty() const { return queue_.empty(); }
  std::size_t size_approx() const { return queue_.size_approx(); }

  int notification_fd() const { return notification_fd_; }
  void Notify();
  void AcknowledgeNotifications() noexcept;

  // Odd epochs mean recvmmsg has removed packets from the kernel socket but
  // the complete batch may not yet be visible in the SPSC ring. Sampling the
  // epoch on both sides of front() also detects a complete publication that
  // overlaps the consumer's empty check.
  void BeginReceiveBatch() noexcept;
  void EndReceiveBatch() noexcept;
  std::uint64_t publication_epoch() const noexcept {
    return publication_epoch_.load(std::memory_order_acquire);
  }
  void MarkConsumerDone() noexcept {
    consumer_done_.store(true, std::memory_order_release);
  }
  bool consumer_done() const noexcept {
    return consumer_done_.load(std::memory_order_acquire);
  }

  void RecordBatch(std::size_t datagrams);
  void RecordSocketOverflow(const void *message_header);
  const RxChannelStats &stats() const { return stats_; }

private:
  SpscQueue<ReceivedDatagram> queue_;
  int notification_fd_ = -1;
  std::atomic<std::uint64_t> publication_epoch_{0};
  std::atomic<bool> notification_pending_{false};
  std::atomic<bool> consumer_done_{false};
  RxChannelStats stats_;
  std::uint32_t last_socket_drops_ = 0;
  bool have_socket_drops_ = false;
};

struct RxEndpoint {
  std::size_t worker_index = 0;
  int socket = -1;
  RxChannel *channel = nullptr;
};

struct RxThreadResult {
  int requested_cpu = -1;
  int start_cpu = -1;
  int end_cpu = -1;
  std::uint64_t epoll_wakeups = 0;
  std::uint64_t receive_syscalls = 0;
  std::uint64_t affinity_repairs = 0;
  std::string error;
};

class UdpReceiverPool {
public:
  UdpReceiverPool(std::vector<RxEndpoint> endpoints, std::size_t thread_count,
                  std::vector<std::uint32_t> cpus, std::atomic<bool> &cancel);
  ~UdpReceiverPool();

  UdpReceiverPool(const UdpReceiverPool &) = delete;
  UdpReceiverPool &operator=(const UdpReceiverPool &) = delete;

  void Start();
  void Stop();
  void Join();
  const std::vector<RxThreadResult> &results() const { return results_; }

private:
  void Run(std::size_t shard) noexcept;
  void RunInner(std::size_t shard);
  void WakeStopFd(int fd) noexcept;

  std::vector<std::vector<RxEndpoint>> shards_;
  std::vector<std::uint32_t> cpus_;
  std::atomic<bool> &cancel_;
  std::atomic<bool> stop_{false};
  std::vector<int> stop_fds_;
  std::vector<std::thread> threads_;
  std::vector<RxThreadResult> results_;
  bool started_ = false;
};

} // namespace mcgen
