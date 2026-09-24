#pragma once

#include "memcached_protocol.h"
#include "spsc_queue.h"
#include "stats.h"
#include "trace_reader.h"
#include "udp_receiver.h"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <vector>

namespace mcgen {

struct ScheduledRequest {
  SharedRequest request;
  std::int64_t deadline_ns = 0;
  bool measured = false;
};

struct WorkerConfig {
  std::string server;
  std::uint32_t value_size = 4096;
  std::uint64_t request_timeout_ms = 200;
  std::uint64_t max_send_lag_us = 5;
  std::uint32_t max_inflight = 32768;
  std::uint32_t batch_size = 32;
  std::uint32_t socket_buffer_mb = 16;
  bool split_receive = false;
  std::uint32_t receive_queue_depth = 4096;
  int worker_cpu = -1;
  std::int64_t drain_deadline_ns = 0;
};

struct WorkerResult {
  Counters total;
  Counters measured;
  Histogram histogram;
  std::string error;

  // Per-worker scheduling diagnostics. "offered" is the number of requests
  // accepted by this worker (and therefore dequeued); producer-side queue-full
  // attempts are reported separately. queue_high_water is sampled from the
  // lock-free queue and is consequently an approximate high-water mark.
  std::uint64_t offered = 0;
  std::uint64_t dequeued = 0;
  std::uint64_t sent = 0;
  std::uint64_t late = 0;
  std::size_t queue_high_water = 0;

  // Receive-side diagnostics. Linux reports SO_RXQ_OVFL as a cumulative
  // 32-bit counter attached to received datagrams; the worker expands it to a
  // 64-bit delta count for the lifetime of its socket.
  std::uint64_t receive_batches = 0;
  std::uint64_t receive_datagrams = 0;
  std::uint64_t receive_deadline_yields = 0;
  std::uint64_t receive_slice_yields = 0;
  std::uint64_t socket_receive_queue_drops = 0;
  std::uint64_t receive_handoff_drops = 0;
  std::size_t receive_queue_high_water = 0;
  bool socket_receive_queue_overflow_enabled = false;
  int socket_receive_queue_overflow_error = 0;

  int requested_socket_buffer_bytes = 0;
  int socket_send_buffer_bytes = 0;
  int socket_receive_buffer_bytes = 0;
  int socket_send_buffer_set_error = 0;
  int socket_receive_buffer_set_error = 0;
  int socket_send_buffer_get_error = 0;
  int socket_receive_buffer_get_error = 0;

  std::uint64_t send_syscalls = 0;
  std::uint64_t sendmsg_calls = 0;
  std::uint64_t sendmmsg_calls = 0;
  std::uint64_t send_messages_attempted = 0;
  std::uint64_t send_messages_returned = 0;
  std::uint32_t maximum_send_batch = 0;
  std::uint64_t partial_send_calls = 0;
  std::int64_t maximum_actual_send_lag_ns = 0;
  std::uint32_t active_high_water = 0;
  std::size_t cooldown_high_water = 0;
  std::size_t free_ids_low_water = 65'536;

  int requested_worker_cpu = -1;
  int worker_start_cpu = -1;
  int worker_end_cpu = -1;
  std::uint64_t affinity_repairs = 0;
};

std::int64_t MonotonicNowNs();

class UdpWorker {
public:
  UdpWorker(std::size_t index, WorkerConfig config,
            SpscQueue<ScheduledRequest> &queue,
            std::atomic<bool> &producer_done, std::atomic<bool> &cancel);
  ~UdpWorker();

  UdpWorker(const UdpWorker &) = delete;
  UdpWorker &operator=(const UdpWorker &) = delete;

  void Start();
  // Open the socket and allocate the optional split-RX channel without
  // starting the TX worker. RunSample uses this to construct shared RX
  // pollers before any requests can be sent.
  void Prepare();
  void Join();
  void SetDrainDeadline(std::int64_t deadline_ns);
  RxEndpoint receive_endpoint();
  void CollectReceiverStats();
  const WorkerResult &result() const { return result_; }

private:
  struct Outstanding;
  struct Expiry;
  struct Cooldown;
  struct SendScratch;
  struct RecvScratch;

  void Run() noexcept;
  void RunInner();
  int OpenSocket();
  void ReceiveAvailable(std::int64_t now_ns);
  void ReceiveFromHandoff(std::int64_t now_ns);
  void ProcessReceivedDatagram(const std::uint8_t *buffer, std::size_t length,
                               int flags, std::int64_t received_ns);
  void ExpireRequests(std::int64_t cutoff_ns, std::int64_t now_ns);
  void ExpireSplitReceive(std::int64_t now_ns);
  void PruneInactiveExpiries();
  void RecycleIds(std::int64_t now_ns);
  void SendDue(std::int64_t now_ns);
  void ObserveQueueDepth();
  bool ShouldYieldReceive(std::int64_t now_ns, std::int64_t slice_start_ns);
  void RecordReceiveQueueDrops(msghdr &message);
  void Complete(std::uint16_t id, std::int64_t now_ns,
                const BinaryResponseHeader &response,
                std::uint16_t fragment_count);
  void ReleaseToCooldown(std::uint16_t id, std::int64_t now_ns);
  void Count(Counters &counters, bool measured, std::uint64_t Counters::*field,
             std::uint64_t amount = 1);
  void FinalizeQueued();
  void FinalizeOutstanding();

  std::size_t index_;
  WorkerConfig config_;
  SpscQueue<ScheduledRequest> &queue_;
  std::atomic<bool> &producer_done_;
  std::atomic<bool> &cancel_;
  std::atomic<std::int64_t> drain_deadline_ns_;
  std::int64_t request_timeout_ns_ = 0;
  std::int64_t max_send_lag_ns_ = 0;
  int socket_ = -1;
  MemcachedProtocol protocol_;
  std::thread thread_;
  WorkerResult result_;

  std::vector<Outstanding> outstanding_;
  std::vector<std::uint16_t> free_ids_;
  std::deque<Expiry> expiries_;
  std::deque<Cooldown> cooldowns_;
  std::vector<SendScratch> send_scratch_;
  std::vector<mmsghdr> send_messages_;
  std::vector<std::size_t> send_order_;
  std::vector<RecvScratch> recv_scratch_;
  std::unique_ptr<RxChannel> receive_channel_;
  std::uint32_t next_opaque_ = 1;
  std::uint32_t active_requests_ = 0;
  std::int64_t last_receive_attempt_ns_ = 0;
  std::int64_t next_affinity_check_ns_ = 0;
  std::uint32_t last_receive_queue_drops_ = 0;
  bool have_receive_queue_drops_ = false;
  bool started_ = false;
};

} // namespace mcgen
