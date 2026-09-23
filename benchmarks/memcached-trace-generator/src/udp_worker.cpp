#include "udp_worker.h"

#include "thread_affinity.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <utility>

namespace mcgen {
namespace {

constexpr std::uint16_t kStatusSuccess = 0x0000;
constexpr std::uint16_t kStatusKeyNotFound = 0x0001;
constexpr std::uint32_t kWireRequestIds = 65'536;
constexpr std::int64_t kReceiveDeadlineGuardMaxNs = 5'000;
constexpr std::int64_t kReceiveSliceNs = 50'000;
constexpr std::int64_t kMaximumReceiveDeferralNs = 20'000;
constexpr std::size_t kMaxReceiveDatagramsPerCall = 32;
constexpr std::size_t kMaxHousekeepingPerPass = 64;
constexpr std::int64_t kAffinityCheckIntervalNs = 100'000'000;

std::pair<std::string, std::string> SplitHostPort(const std::string &server) {
  if (server.empty()) {
    throw std::invalid_argument("server address is empty");
  }
  if (server.front() == '[') {
    const auto close = server.find(']');
    if (close == std::string::npos || close + 2 > server.size() ||
        server[close + 1] != ':') {
      throw std::invalid_argument(
          "IPv6 server must use the form [address]:port");
    }
    return {server.substr(1, close - 1), server.substr(close + 2)};
  }
  const auto colon = server.rfind(':');
  if (colon == std::string::npos || colon == 0 || colon + 1 == server.size()) {
    throw std::invalid_argument("server must use the form host:port");
  }
  return {server.substr(0, colon), server.substr(colon + 1)};
}

Opcode ToOpcode(Operation operation) {
  switch (operation) {
  case Operation::Get:
    return Opcode::Get;
  case Operation::Set:
    return Opcode::Set;
  case Operation::Delete:
    return Opcode::Delete;
  }
  throw std::logic_error("unknown trace operation");
}

bool IsWouldBlock(int error) { return error == EAGAIN || error == EWOULDBLOCK; }

std::uint32_t CheckedMaxInflight(const WorkerConfig &config) {
  if (config.max_inflight == 0 || config.max_inflight >= kWireRequestIds) {
    throw std::invalid_argument("max_inflight must be in [1, 65535]");
  }
  return config.max_inflight;
}

std::uint32_t CheckedBatchSize(const WorkerConfig &config) {
  if (config.batch_size == 0 || config.batch_size > 1024) {
    throw std::invalid_argument("batch_size must be in [1, 1024]");
  }
  return config.batch_size;
}

std::int64_t CheckedDuration(std::uint64_t value, std::int64_t multiplier,
                             const char *name) {
  const auto limit = static_cast<std::uint64_t>(
      std::numeric_limits<std::int64_t>::max() / multiplier);
  if (value > limit) {
    throw std::invalid_argument(std::string(name) + " is too large");
  }
  return static_cast<std::int64_t>(value) * multiplier;
}

std::int64_t SaturatingAdd(std::int64_t base, std::int64_t delta) noexcept {
  if (delta > 0 && base > std::numeric_limits<std::int64_t>::max() - delta) {
    return std::numeric_limits<std::int64_t>::max();
  }
  return base + delta;
}

inline void CpuRelax() noexcept {
#if defined(__i386__) || defined(__x86_64__)
  __builtin_ia32_pause();
#else
  std::atomic_signal_fence(std::memory_order_seq_cst);
#endif
}

} // namespace

struct UdpWorker::Outstanding {
  bool active = false;
  bool measured = false;
  Operation operation = Operation::Get;
  std::uint32_t opaque = 0;
  std::int64_t sent_ns = 0;
  std::int64_t expiry_ns = 0;
  ResponseReassembler reassembler;
};

struct UdpWorker::Expiry {
  std::int64_t deadline_ns;
  std::uint16_t id;
  std::uint32_t opaque;
};

struct UdpWorker::Cooldown {
  std::int64_t deadline_ns;
  std::uint16_t id;
};

struct UdpWorker::SendScratch {
  std::vector<std::uint8_t> prefix;
  ScheduledRequest request;
  std::uint16_t id = 0;
  std::uint32_t opaque = 0;
  iovec iov[2]{};
  mmsghdr message{};
};

struct UdpWorker::RecvScratch {
  std::array<std::uint8_t, kReceiveDatagramSize> buffer;
  // SO_RXQ_OVFL is the only control message requested by this socket. Keep
  // enough aligned storage for its uint32_t cumulative drop counter.
  alignas(cmsghdr)
      std::array<unsigned char, CMSG_SPACE(sizeof(std::uint32_t))> control{};
  iovec iov{};
  mmsghdr message{};
};

std::int64_t MonotonicNowNs() {
  timespec now{};
  if (::clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
    throw std::system_error(errno, std::generic_category(), "clock_gettime");
  }
  return static_cast<std::int64_t>(now.tv_sec) * 1'000'000'000LL + now.tv_nsec;
}

UdpWorker::UdpWorker(std::size_t index, WorkerConfig config,
                     SpscQueue<ScheduledRequest> &queue,
                     std::atomic<bool> &producer_done,
                     std::atomic<bool> &cancel)
    : index_(index), config_(std::move(config)), queue_(queue),
      producer_done_(producer_done), cancel_(cancel),
      drain_deadline_ns_(config_.drain_deadline_ns),
      request_timeout_ns_(CheckedDuration(config_.request_timeout_ms, 1'000'000,
                                          "request_timeout_ms")),
      max_send_lag_ns_(
          CheckedDuration(config_.max_send_lag_us, 1'000, "max_send_lag_us")),
      protocol_(config_.value_size), outstanding_(kWireRequestIds),
      send_scratch_(CheckedBatchSize(config_)),
      send_messages_(CheckedBatchSize(config_)),
      send_order_(CheckedBatchSize(config_)),
      recv_scratch_(config_.split_receive ? 0 : CheckedBatchSize(config_)) {
  (void)CheckedMaxInflight(config_);
  if (config_.request_timeout_ms == 0) {
    throw std::invalid_argument("request_timeout_ms must be positive");
  }
  free_ids_.reserve(kWireRequestIds);
  for (std::uint32_t id = kWireRequestIds; id > 0; --id) {
    free_ids_.push_back(static_cast<std::uint16_t>(id - 1));
  }
  for (auto &scratch : send_scratch_) {
    scratch.prefix.reserve(kUdpHeaderSize + kBinaryHeaderSize + kSetExtrasSize +
                           kMaxMemcachedKeySize);
  }
  result_.requested_worker_cpu = config_.worker_cpu;
  result_.free_ids_low_water = free_ids_.size();
}

UdpWorker::~UdpWorker() {
  if (thread_.joinable()) {
    thread_.join();
  }
  if (socket_ >= 0) {
    ::close(socket_);
  }
}

void UdpWorker::Start() {
  if (started_) {
    throw std::logic_error("worker already started");
  }
  Prepare();
  started_ = true;
  thread_ = std::thread(&UdpWorker::Run, this);
}

void UdpWorker::Prepare() {
  if (socket_ >= 0) {
    return;
  }
  // Resolve and connect before starting any worker so configuration errors do
  // not leave a partially running benchmark.
  socket_ = OpenSocket();
  if (config_.split_receive) {
    receive_channel_ = std::make_unique<RxChannel>(config_.receive_queue_depth);
  }
}

RxEndpoint UdpWorker::receive_endpoint() {
  if (!config_.split_receive || socket_ < 0 || !receive_channel_) {
    throw std::logic_error("split-RX worker is not prepared");
  }
  return RxEndpoint{index_, socket_, receive_channel_.get()};
}

void UdpWorker::CollectReceiverStats() {
  if (!receive_channel_) {
    return;
  }
  const auto &stats = receive_channel_->stats();
  result_.receive_batches = stats.receive_batches;
  result_.receive_datagrams = stats.receive_datagrams;
  result_.socket_receive_queue_drops = stats.socket_receive_queue_drops;
  result_.receive_handoff_drops = stats.handoff_drops;
  result_.receive_queue_high_water = stats.queue_high_water;
}

void UdpWorker::Join() {
  if (thread_.joinable()) {
    thread_.join();
  }
}

void UdpWorker::SetDrainDeadline(std::int64_t deadline_ns) {
  drain_deadline_ns_.store(deadline_ns, std::memory_order_release);
}

int UdpWorker::OpenSocket() {
  const auto [host, port] = SplitHostPort(config_.server);
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_DGRAM;
  hints.ai_protocol = IPPROTO_UDP;
  addrinfo *addresses = nullptr;
  const int gai_error =
      ::getaddrinfo(host.c_str(), port.c_str(), &hints, &addresses);
  if (gai_error != 0) {
    throw std::runtime_error("cannot resolve server '" + config_.server +
                             "': " + gai_strerror(gai_error));
  }

  int fd = -1;
  int last_error = 0;
  const int requested_buffer_bytes = static_cast<int>(std::min<std::uint64_t>(
      static_cast<std::uint64_t>(config_.socket_buffer_mb) * 1024 * 1024,
      static_cast<std::uint64_t>(std::numeric_limits<int>::max())));
  for (addrinfo *address = addresses; address != nullptr;
       address = address->ai_next) {
    fd = ::socket(address->ai_family,
                  address->ai_socktype | SOCK_NONBLOCK | SOCK_CLOEXEC,
                  address->ai_protocol);
    if (fd < 0) {
      last_error = errno;
      continue;
    }
#ifdef IP_MTU_DISCOVER
    if (address->ai_family == AF_INET) {
      const int discovery = IP_PMTUDISC_DONT;
      if (::setsockopt(fd, IPPROTO_IP, IP_MTU_DISCOVER, &discovery,
                       sizeof(discovery)) != 0) {
        last_error = errno;
        ::close(fd);
        fd = -1;
        continue;
      }
    }
#endif
#ifdef IPV6_MTU_DISCOVER
    if (address->ai_family == AF_INET6) {
      const int discovery = IPV6_PMTUDISC_DONT;
      if (::setsockopt(fd, IPPROTO_IPV6, IPV6_MTU_DISCOVER, &discovery,
                       sizeof(discovery)) != 0) {
        last_error = errno;
        ::close(fd);
        fd = -1;
        continue;
      }
    }
#endif
    int send_buffer_set_error = 0;
    int receive_buffer_set_error = 0;
    if (requested_buffer_bytes > 0) {
      if (::setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &requested_buffer_bytes,
                       sizeof(requested_buffer_bytes)) != 0) {
        send_buffer_set_error = errno;
      }
      if (::setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &requested_buffer_bytes,
                       sizeof(requested_buffer_bytes)) != 0) {
        receive_buffer_set_error = errno;
      }
    }

    bool receive_queue_overflow_enabled = false;
    int receive_queue_overflow_error = 0;
#ifdef SO_RXQ_OVFL
    const int enable_receive_queue_overflow = 1;
    if (::setsockopt(fd, SOL_SOCKET, SO_RXQ_OVFL,
                     &enable_receive_queue_overflow,
                     sizeof(enable_receive_queue_overflow)) == 0) {
      receive_queue_overflow_enabled = true;
    } else {
      receive_queue_overflow_error = errno;
    }
#else
    receive_queue_overflow_error = ENOPROTOOPT;
#endif

    if (::connect(fd, address->ai_addr, address->ai_addrlen) == 0) {
      result_.requested_socket_buffer_bytes = requested_buffer_bytes;
      result_.socket_send_buffer_set_error = send_buffer_set_error;
      result_.socket_receive_buffer_set_error = receive_buffer_set_error;
      result_.socket_receive_queue_overflow_enabled =
          receive_queue_overflow_enabled;
      result_.socket_receive_queue_overflow_error =
          receive_queue_overflow_error;

      socklen_t option_length = sizeof(int);
      if (::getsockopt(fd, SOL_SOCKET, SO_SNDBUF,
                       &result_.socket_send_buffer_bytes,
                       &option_length) != 0) {
        result_.socket_send_buffer_get_error = errno;
        result_.socket_send_buffer_bytes = 0;
      }
      option_length = sizeof(int);
      if (::getsockopt(fd, SOL_SOCKET, SO_RCVBUF,
                       &result_.socket_receive_buffer_bytes,
                       &option_length) != 0) {
        result_.socket_receive_buffer_get_error = errno;
        result_.socket_receive_buffer_bytes = 0;
      }
      break;
    }
    last_error = errno;
    ::close(fd);
    fd = -1;
  }
  ::freeaddrinfo(addresses);
  if (fd < 0) {
    throw std::system_error(last_error, std::generic_category(),
                            "cannot connect UDP socket to " + config_.server);
  }
  return fd;
}

void UdpWorker::Run() noexcept {
  try {
    NameCurrentThread("mc-tx-" + std::to_string(index_));
    PinCurrentThread(config_.worker_cpu, "worker " + std::to_string(index_));
    result_.worker_start_cpu = CurrentCpu();
    RunInner();
    if (EnsureCurrentThreadPinned(config_.worker_cpu,
                                  "worker " + std::to_string(index_))) {
      ++result_.affinity_repairs;
    }
    result_.worker_end_cpu = CurrentCpu();
  } catch (const std::exception &error) {
    result_.worker_end_cpu = CurrentCpu();
    result_.error = "worker " + std::to_string(index_) + ": " + error.what();
    cancel_.store(true, std::memory_order_release);
  } catch (...) {
    result_.worker_end_cpu = CurrentCpu();
    result_.error = "worker " + std::to_string(index_) + ": unknown exception";
    cancel_.store(true, std::memory_order_release);
  }
  if (receive_channel_) {
    receive_channel_->MarkConsumerDone();
  }
  FinalizeQueued();
  FinalizeOutstanding();
}

void UdpWorker::RunInner() {
  while (true) {
    if (cancel_.load(std::memory_order_acquire)) {
      break;
    }
    ObserveQueueDepth();
    auto now_ns = MonotonicNowNs();
    if (now_ns >= next_affinity_check_ns_) {
      if (EnsureCurrentThreadPinned(config_.worker_cpu,
                                    "worker " + std::to_string(index_))) {
        ++result_.affinity_repairs;
      }
      next_affinity_check_ns_ = SaturatingAdd(now_ns, kAffinityCheckIntervalNs);
    }
    const auto initial_drain_deadline_ns =
        drain_deadline_ns_.load(std::memory_order_acquire);
    if (producer_done_.load(std::memory_order_acquire) &&
        now_ns >= initial_drain_deadline_ns) {
      break;
    }
    RecycleIds(now_ns);
    if (config_.split_receive) {
      // Preserve open-loop send deadlines before doing response work. The RX
      // publication barrier below still prevents a timely response waiting in
      // the handoff path from becoming an artificial timeout.
      SendDue(now_ns);
      now_ns = MonotonicNowNs();
      ReceiveFromHandoff(now_ns);
      ObserveQueueDepth();
      now_ns = MonotonicNowNs();
      ExpireSplitReceive(now_ns);
      SendDue(now_ns);
    } else {
      ExpireRequests(now_ns, now_ns);
      // Preserve the open-loop deadline first. Under a response burst,
      // draining the socket before transmitting can make otherwise on-time
      // requests miss the deliberately tight send-lag bound.
      SendDue(now_ns);
      now_ns = MonotonicNowNs();
      ReceiveAvailable(now_ns);
      ObserveQueueDepth();
      now_ns = MonotonicNowNs();
      ExpireRequests(now_ns, now_ns);
      // Receiving and histogram updates can span another arrival deadline.
      SendDue(now_ns);
    }

    now_ns = MonotonicNowNs();
    const auto drain_deadline_ns =
        drain_deadline_ns_.load(std::memory_order_acquire);
    const bool producer_done = producer_done_.load(std::memory_order_acquire);
    // The drain deadline is a hard wall-clock stop. Never extend a benchmark
    // by catching up queued arrivals after its configured drain window.
    if (producer_done && now_ns >= drain_deadline_ns) {
      break;
    }

    std::int64_t wake_ns = now_ns + 1'000'000; // Check at least every 1 ms.
    if (const auto *request = queue_.front()) {
      wake_ns = std::min(wake_ns, request->deadline_ns);
    }
    if (!expiries_.empty()) {
      wake_ns = std::min(wake_ns, expiries_.front().deadline_ns);
    }
    if (config_.split_receive && !receive_channel_->empty()) {
      wake_ns = now_ns;
    }
    // An expired drain deadline is only useful when it can make the worker
    // exit. Otherwise retaining it as the next wakeup creates a busy-spin if
    // the producer is delayed or a queue still has work.
    if (drain_deadline_ns > now_ns || (producer_done && queue_.empty())) {
      wake_ns = std::min(wake_ns, drain_deadline_ns);
    }
    const auto wait_ns = wake_ns - now_ns;
    constexpr std::int64_t kSpinWindowNs = 200'000;
    if (wait_ns > kSpinWindowNs) {
      // Leave a small spin margin because the original generator rejects a
      // request once it is more than 5 us late.
      const auto poll_ns = wait_ns - kSpinWindowNs;
      timespec timeout{poll_ns / 1'000'000'000LL, poll_ns % 1'000'000'000LL};
      const int wait_fd =
          config_.split_receive ? receive_channel_->notification_fd() : socket_;
      pollfd descriptor{wait_fd, POLLIN, 0};
      const int rc = ::ppoll(&descriptor, 1, &timeout, nullptr);
      if (rc < 0 && errno != EINTR) {
        throw std::system_error(errno, std::generic_category(), "ppoll");
      }
      if (config_.split_receive && rc > 0 &&
          (descriptor.revents & POLLIN) != 0) {
        receive_channel_->AcknowledgeNotifications();
      }
    } else if (wait_ns > 0) {
      // sched_yield() routinely resumes more than 5 us later under load.
      // Busy-wait only inside this short final window, matching the timing
      // behavior of the original generator without burning a core while the
      // next event is far away.
      while (MonotonicNowNs() < wake_ns) {
        CpuRelax();
      }
    }
  }
}

void UdpWorker::Count(Counters &counters, bool measured,
                      std::uint64_t Counters::*field, std::uint64_t amount) {
  counters.*field += amount;
  if (measured) {
    result_.measured.*field += amount;
  }
}

void UdpWorker::ObserveQueueDepth() {
  result_.queue_high_water =
      std::max(result_.queue_high_water, queue_.size_approx());
}

bool UdpWorker::ShouldYieldReceive(std::int64_t now_ns,
                                   std::int64_t slice_start_ns) {
  if (cancel_.load(std::memory_order_acquire)) {
    return true;
  }
  PruneInactiveExpiries();
  if (!config_.split_receive && !expiries_.empty() &&
      expiries_.front().deadline_ns <= now_ns) {
    return true;
  }

  if (const auto *request = queue_.front()) {
    const auto guard_ns =
        std::min(max_send_lag_ns_, kReceiveDeadlineGuardMaxNs);
    if (request->deadline_ns <= SaturatingAdd(now_ns, guard_ns)) {
      ++result_.receive_deadline_yields;
      return true;
    }
  }

  if (now_ns - slice_start_ns >= kReceiveSliceNs) {
    ++result_.receive_slice_yields;
    return true;
  }
  return false;
}

void UdpWorker::RecordReceiveQueueDrops(msghdr &message) {
#ifdef SO_RXQ_OVFL
  if (!result_.socket_receive_queue_overflow_enabled) {
    return;
  }
  for (cmsghdr *control = CMSG_FIRSTHDR(&message); control != nullptr;
       control = CMSG_NXTHDR(&message, control)) {
    if (control->cmsg_level != SOL_SOCKET ||
        control->cmsg_type != SO_RXQ_OVFL ||
        control->cmsg_len < CMSG_LEN(sizeof(std::uint32_t))) {
      continue;
    }

    std::uint32_t cumulative_drops = 0;
    std::memcpy(&cumulative_drops, CMSG_DATA(control),
                sizeof(cumulative_drops));
    const std::uint32_t delta =
        have_receive_queue_drops_
            ? static_cast<std::uint32_t>(cumulative_drops -
                                         last_receive_queue_drops_)
            : cumulative_drops;
    result_.socket_receive_queue_drops += delta;
    last_receive_queue_drops_ = cumulative_drops;
    have_receive_queue_drops_ = true;
  }
#else
  (void)message;
#endif
}

void UdpWorker::ReceiveAvailable(std::int64_t now_ns) {
  // Drain dynamically instead of assuming one response datagram per request:
  // a 4 KiB GET hit normally produces three datagrams. Stop before an
  // imminent send deadline and periodically return for expiry/cancellation
  // housekeeping, so a continuously readable socket cannot starve sends.
  const auto slice_start_ns = now_ns;
  bool attempted_receive = false;
  for (;;) {
    now_ns = MonotonicNowNs();
    if (!attempted_receive) {
      if (active_requests_ == 0) {
        return;
      }
      // Do not start a receive syscall immediately before a future transmit
      // deadline. At very high arrival rates there may never be a wide enough
      // gap, so force one attempt after a short bounded deferral to keep RX
      // making progress and prevent an artificial timeout storm.
      if (const auto *request = queue_.front();
          request != nullptr && request->deadline_ns > now_ns) {
        const auto guard_ns =
            std::min(max_send_lag_ns_, kReceiveDeadlineGuardMaxNs);
        const bool imminent =
            request->deadline_ns <= SaturatingAdd(now_ns, guard_ns);
        const bool recently_attempted =
            last_receive_attempt_ns_ != 0 &&
            now_ns - last_receive_attempt_ns_ < kMaximumReceiveDeferralNs;
        if (imminent && recently_attempted) {
          ++result_.receive_deadline_yields;
          return;
        }
      }
    }
    // Always make one nonblocking receive attempt between the two SendDue()
    // calls in RunInner. Otherwise a permanently due transmit backlog could
    // prevent all receive progress and manufacture avoidable timeouts. Once
    // one batch has had a chance to run, transmit deadlines take priority.
    if (attempted_receive && ShouldYieldReceive(now_ns, slice_start_ns)) {
      return;
    }
    attempted_receive = true;
    last_receive_attempt_ns_ = now_ns;

    for (auto &scratch : recv_scratch_) {
      scratch.iov = iovec{scratch.buffer.data(), scratch.buffer.size()};
      scratch.message = mmsghdr{};
      scratch.message.msg_hdr.msg_iov = &scratch.iov;
      scratch.message.msg_hdr.msg_iovlen = 1;
      scratch.message.msg_hdr.msg_control = scratch.control.data();
      scratch.message.msg_hdr.msg_controllen = scratch.control.size();
    }
    // mmsghdr must be contiguous, while the payload storage remains in the
    // reusable per-worker scratch array. A separate descriptor array avoids
    // clearing tens of kilobytes of receive buffers on every event-loop turn.
    for (std::size_t i = 0; i < recv_scratch_.size(); ++i) {
      send_messages_[i] = recv_scratch_[i].message;
    }
    const auto receive_capacity =
        std::min(recv_scratch_.size(), kMaxReceiveDatagramsPerCall);
    const int received = ::recvmmsg(socket_, send_messages_.data(),
                                    static_cast<unsigned int>(receive_capacity),
                                    MSG_DONTWAIT | MSG_TRUNC, nullptr);
    if (received < 0) {
      if (IsWouldBlock(errno) || errno == EINTR) {
        return;
      }
      throw std::system_error(errno, std::generic_category(), "recvmmsg");
    }
    if (received == 0) {
      return;
    }

    ++result_.receive_batches;
    result_.receive_datagrams += static_cast<std::uint64_t>(received);
    now_ns = MonotonicNowNs();
    for (int i = 0; i < received; ++i) {
      const auto index = static_cast<std::size_t>(i);
      RecordReceiveQueueDrops(send_messages_[index].msg_hdr);
      const auto length =
          static_cast<std::size_t>(send_messages_[index].msg_len);
      ProcessReceivedDatagram(recv_scratch_[index].buffer.data(), length,
                              send_messages_[index].msg_hdr.msg_flags, now_ns);
    }
  }
}

void UdpWorker::ReceiveFromHandoff(std::int64_t now_ns) {
  if (receive_channel_->empty()) {
    return;
  }
  const auto slice_start_ns = now_ns;
  while (receive_channel_->Consume([&](const ReceivedDatagram &datagram) {
    ProcessReceivedDatagram(datagram.bytes.data(), datagram.length,
                            datagram.flags, datagram.received_ns);
  })) {
    now_ns = MonotonicNowNs();
    if (ShouldYieldReceive(now_ns, slice_start_ns)) {
      return;
    }
  }
}

void UdpWorker::ProcessReceivedDatagram(const std::uint8_t *buffer,
                                        std::size_t length, int flags,
                                        std::int64_t received_ns) {
  if ((flags & MSG_TRUNC) != 0 || length < kUdpHeaderSize ||
      length > kReceiveDatagramSize) {
    ++result_.total.malformed_fragment;
    return;
  }
  const std::uint16_t id =
      static_cast<std::uint16_t>((static_cast<std::uint16_t>(buffer[0]) << 8) |
                                 static_cast<std::uint16_t>(buffer[1]));
  if (id >= outstanding_.size() || !outstanding_[id].active) {
    ++result_.total.stale_fragment;
    return;
  }

  auto &entry = outstanding_[id];
  // A split-RX ring may retain a delayed non-zero fragment across request-ID
  // cooldown and reuse. Such fragments do not carry the binary opaque value,
  // so reject them using their receive timestamp before touching reassembly.
  if (received_ns < entry.sent_ns) {
    ++result_.total.stale_fragment;
    return;
  }
  if (received_ns >= entry.expiry_ns) {
    Count(result_.total, entry.measured, &Counters::timeout);
    entry.active = false;
    entry.reassembler.clear();
    --active_requests_;
    // The handoff queue can delay processing, so its receive timestamps may
    // precede cooldown entries already appended by ExpireRequests(). Base the
    // quarantine on processing time to preserve the deque's deadline order.
    ReleaseToCooldown(id, MonotonicNowNs());
    ++result_.total.stale_fragment;
    return;
  }
  const auto accepted = entry.reassembler.accept(buffer, length);
  switch (accepted.code) {
  case ReassemblyCode::Accepted:
    break;
  case ReassemblyCode::Duplicate:
  case ReassemblyCode::AlreadyComplete:
    Count(result_.total, entry.measured, &Counters::duplicate_fragment);
    break;
  case ReassemblyCode::Complete: {
    const auto *header = entry.reassembler.response_header();
    if (header == nullptr) {
      Count(result_.total, entry.measured, &Counters::malformed_fragment);
    } else {
      Complete(id, received_ns, *header, entry.reassembler.total_fragments());
    }
    break;
  }
  case ReassemblyCode::Inactive:
  case ReassemblyCode::ParseError:
  case ReassemblyCode::RequestIdMismatch:
  case ReassemblyCode::TotalMismatch:
  case ReassemblyCode::FragmentLimitExceeded:
    Count(result_.total, entry.measured, &Counters::malformed_fragment);
    break;
  case ReassemblyCode::OpaqueMismatch:
    ++result_.total.stale_fragment;
    break;
  }
}

void UdpWorker::ExpireRequests(std::int64_t cutoff_ns, std::int64_t now_ns) {
  PruneInactiveExpiries();
  std::size_t processed = 0;
  while (processed < kMaxHousekeepingPerPass && !expiries_.empty() &&
         expiries_.front().deadline_ns <= cutoff_ns) {
    const auto expiry = expiries_.front();
    expiries_.pop_front();
    ++processed;
    auto &entry = outstanding_[expiry.id];
    if (!entry.active || entry.opaque != expiry.opaque) {
      continue;
    }
    Count(result_.total, entry.measured, &Counters::timeout);
    entry.active = false;
    entry.reassembler.clear();
    --active_requests_;
    ReleaseToCooldown(expiry.id, now_ns);
  }
}

void UdpWorker::PruneInactiveExpiries() {
  while (!expiries_.empty()) {
    const auto &expiry = expiries_.front();
    const auto &entry = outstanding_[expiry.id];
    if (entry.active && entry.opaque == expiry.opaque) {
      return;
    }
    expiries_.pop_front();
  }
}

void UdpWorker::ExpireSplitReceive(std::int64_t now_ns) {
  PruneInactiveExpiries();
  if (expiries_.empty()) {
    return;
  }
  const auto before = receive_channel_->publication_epoch();
  if ((before & 1U) != 0) {
    return;
  }
  const auto *packet = receive_channel_->front();
  const auto packet_ns = packet == nullptr ? now_ns : packet->received_ns;
  const auto after = receive_channel_->publication_epoch();
  if (before != after || (after & 1U) != 0) {
    return;
  }
  // Re-evaluate every expiry against the oldest published packet. A single
  // boolean check followed by a batch expiry could otherwise time out a later
  // request whose timely response is exactly that front packet.
  ExpireRequests(std::min(now_ns, packet_ns), now_ns);
}

void UdpWorker::RecycleIds(std::int64_t now_ns) {
  std::size_t processed = 0;
  while (processed < kMaxHousekeepingPerPass && !cooldowns_.empty() &&
         cooldowns_.front().deadline_ns <= now_ns) {
    free_ids_.push_back(cooldowns_.front().id);
    cooldowns_.pop_front();
    ++processed;
  }
}

void UdpWorker::ReleaseToCooldown(std::uint16_t id, std::int64_t now_ns) {
  // A non-zero UDP fragment has no opaque field. Quarantine a request id for
  // one timeout interval so a delayed fragment cannot contaminate a later
  // request that happens to reuse the same 16-bit id.
  cooldowns_.push_back(
      Cooldown{SaturatingAdd(now_ns, request_timeout_ns_), id});
  result_.cooldown_high_water =
      std::max(result_.cooldown_high_water, cooldowns_.size());
}

void UdpWorker::SendDue(std::int64_t now_ns) {
  ObserveQueueDepth();
  std::size_t count = 0;
  std::size_t processed = 0;
  while (count < send_scratch_.size() && processed < send_scratch_.size()) {
    const auto *next = queue_.front();
    if (next == nullptr || next->deadline_ns > now_ns) {
      break;
    }

    ScheduledRequest request;
    if (!queue_.pop(request)) {
      break;
    }
    ++result_.offered;
    ++result_.dequeued;
    ++processed;
    now_ns = MonotonicNowNs();
    const auto lag_ns = now_ns - request.deadline_ns;
    if (lag_ns > max_send_lag_ns_) {
      Count(result_.total, request.measured, &Counters::late);
      ++result_.late;
      continue;
    }
    RecycleIds(now_ns);
    if (active_requests_ + count >= config_.max_inflight || free_ids_.empty()) {
      Count(result_.total, request.measured, &Counters::no_slot);
      continue;
    }

    auto &scratch = send_scratch_[count];
    scratch.request = std::move(request);
    scratch.id = free_ids_.back();
    free_ids_.pop_back();
    result_.free_ids_low_water =
        std::min(result_.free_ids_low_water, free_ids_.size());
    scratch.opaque = next_opaque_++;
    if (next_opaque_ == 0) {
      next_opaque_ = 1;
    }
    const auto opcode = ToOpcode(scratch.request.request.op);
    if (!scratch.request.request.key) {
      free_ids_.push_back(scratch.id);
      Count(result_.total, scratch.request.measured, &Counters::send_error);
      continue;
    }
    const auto encode = protocol_.encode_request_prefix(
        opcode, scratch.request.request.key->view(), scratch.id, scratch.opaque,
        scratch.prefix);
    if (encode != EncodeError::None) {
      free_ids_.push_back(scratch.id);
      Count(result_.total, scratch.request.measured, &Counters::send_error);
      continue;
    }

    scratch.iov[0] = iovec{scratch.prefix.data(), scratch.prefix.size()};
    std::size_t iov_count = 1;
    if (opcode == Opcode::Set && !protocol_.value_buffer().empty()) {
      scratch.iov[1] =
          iovec{const_cast<std::uint8_t *>(protocol_.value_buffer().data()),
                protocol_.value_buffer().size()};
      iov_count = 2;
    }
    scratch.message = mmsghdr{};
    scratch.message.msg_hdr.msg_iov = scratch.iov;
    scratch.message.msg_hdr.msg_iovlen = iov_count;
    ++count;
  }

  if (count == 0) {
    return;
  }

  // Encoding a batch takes time. Recheck against the actual submission time
  // and compact only requests which still satisfy the send-lag contract.
  const auto sent_ns = MonotonicNowNs();
  std::size_t send_count = 0;
  for (std::size_t i = 0; i < count; ++i) {
    auto &scratch = send_scratch_[i];
    if (sent_ns - scratch.request.deadline_ns > max_send_lag_ns_) {
      free_ids_.push_back(scratch.id);
      Count(result_.total, scratch.request.measured, &Counters::late);
      ++result_.late;
      continue;
    }
    send_order_[send_count] = i;
    send_messages_[send_count] = scratch.message;
    ++send_count;
  }
  if (send_count == 0) {
    return;
  }

  // mmsghdr entries passed to sendmmsg are compact, while their iovecs point
  // at the stable scratch slots selected by send_order_.
  ++result_.send_syscalls;
  result_.send_messages_attempted += send_count;
  int sent = -1;
  if (send_count == 1) {
    ++result_.sendmsg_calls;
    const auto bytes = ::sendmsg(socket_, &send_messages_[0].msg_hdr,
                                 MSG_DONTWAIT | MSG_NOSIGNAL);
    sent = bytes < 0 ? -1 : 1;
  } else {
    ++result_.sendmmsg_calls;
    sent = ::sendmmsg(socket_, send_messages_.data(),
                      static_cast<unsigned int>(send_count),
                      MSG_DONTWAIT | MSG_NOSIGNAL);
  }
  const int send_errno = sent < 0 ? errno : 0;
  const std::size_t sent_count = sent < 0 ? 0 : static_cast<std::size_t>(sent);
  result_.send_messages_returned += sent_count;
  result_.maximum_send_batch = std::max(result_.maximum_send_batch,
                                        static_cast<std::uint32_t>(sent_count));
  if (sent_count < send_count) {
    ++result_.partial_send_calls;
  }
  for (std::size_t i = 0; i < send_count; ++i) {
    auto &scratch = send_scratch_[send_order_[i]];
    if (i >= sent_count) {
      free_ids_.push_back(scratch.id);
      Count(result_.total, scratch.request.measured, &Counters::send_error);
      continue;
    }
    auto &entry = outstanding_[scratch.id];
    entry.active = true;
    entry.measured = scratch.request.measured;
    entry.operation = scratch.request.request.op;
    entry.opaque = scratch.opaque;
    entry.sent_ns = sent_ns;
    entry.expiry_ns = SaturatingAdd(sent_ns, request_timeout_ns_);
    entry.reassembler.reset(scratch.id, scratch.opaque);
    ++active_requests_;
    result_.maximum_actual_send_lag_ns =
        std::max(result_.maximum_actual_send_lag_ns,
                 sent_ns - scratch.request.deadline_ns);
    result_.active_high_water =
        std::max(result_.active_high_water, active_requests_);
    Count(result_.total, entry.measured, &Counters::sent);
    ++result_.sent;
    expiries_.push_back(Expiry{entry.expiry_ns, scratch.id, scratch.opaque});
  }

  if (sent < 0 && !IsWouldBlock(send_errno) && send_errno != EINTR) {
    throw std::system_error(send_errno, std::generic_category(), "sendmmsg");
  }
}

void UdpWorker::Complete(std::uint16_t id, std::int64_t now_ns,
                         const BinaryResponseHeader &response,
                         std::uint16_t fragment_count) {
  auto &entry = outstanding_[id];
  const auto expected_opcode =
      static_cast<std::uint8_t>(ToOpcode(entry.operation));
  Count(result_.total, entry.measured, &Counters::completed);
  if (fragment_count > 1) {
    Count(result_.total, entry.measured, &Counters::multi_fragment_response);
  }

  if (response.opcode != expected_opcode) {
    Count(result_.total, entry.measured, &Counters::server_error);
  } else {
    switch (entry.operation) {
    case Operation::Get:
      if (response.status == kStatusSuccess) {
        Count(result_.total, entry.measured, &Counters::get_hit);
      } else if (response.status == kStatusKeyNotFound) {
        Count(result_.total, entry.measured, &Counters::get_miss);
      } else {
        Count(result_.total, entry.measured, &Counters::server_error);
      }
      break;
    case Operation::Set:
      if (response.status == kStatusSuccess) {
        Count(result_.total, entry.measured, &Counters::set_success);
      } else {
        Count(result_.total, entry.measured, &Counters::server_error);
      }
      break;
    case Operation::Delete:
      if (response.status == kStatusSuccess) {
        Count(result_.total, entry.measured, &Counters::delete_success);
      } else if (response.status == kStatusKeyNotFound) {
        Count(result_.total, entry.measured, &Counters::delete_miss);
      } else {
        Count(result_.total, entry.measured, &Counters::server_error);
      }
      break;
    }
  }

  if (entry.measured) {
    const auto latency_ns = std::max<std::int64_t>(1, now_ns - entry.sent_ns);
    if (!result_.histogram.record(latency_ns)) {
      ++result_.measured.latency_out_of_range;
      ++result_.total.latency_out_of_range;
    }
  }
  entry.active = false;
  entry.reassembler.clear();
  --active_requests_;
  // now_ns is the packet receive timestamp in split-RX mode and can lag the
  // processing clock. Cooldowns are consumed as an ordered deque, so append
  // them using the current processing time.
  ReleaseToCooldown(id, MonotonicNowNs());
}

void UdpWorker::FinalizeOutstanding() {
  for (auto &entry : outstanding_) {
    if (!entry.active) {
      continue;
    }
    Count(result_.total, entry.measured, &Counters::outstanding);
    entry.active = false;
    entry.reassembler.clear();
    --active_requests_;
  }
}

void UdpWorker::FinalizeQueued() {
  ScheduledRequest request;
  while (queue_.pop(request)) {
    ++result_.offered;
    ++result_.dequeued;
    Count(result_.total, request.measured, &Counters::stopped_at_deadline);
  }
}

} // namespace mcgen
