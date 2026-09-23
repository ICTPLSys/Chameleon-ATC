#include "udp_receiver.h"

#include "thread_affinity.h"
#include "udp_worker.h"

#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <system_error>

namespace mcgen {
namespace {

constexpr std::size_t kReceiveBatch = 32;
constexpr std::size_t kBatchesPerReadySocket = 4;
constexpr std::int64_t kAffinityCheckIntervalNs = 100'000'000;

bool IsWouldBlock(int error) { return error == EAGAIN || error == EWOULDBLOCK; }

struct RecvScratch {
  std::array<std::uint8_t, kReceiveDatagramSize> buffer{};
  alignas(cmsghdr)
      std::array<unsigned char, CMSG_SPACE(sizeof(std::uint32_t))> control{};
  iovec iov{};
  mmsghdr message{};
};

struct ScopedFd {
  explicit ScopedFd(int value) : value(value) {}
  ~ScopedFd() {
    if (value >= 0) {
      ::close(value);
    }
  }
  ScopedFd(const ScopedFd &) = delete;
  ScopedFd &operator=(const ScopedFd &) = delete;
  int value = -1;
};

void PrepareReceiveMessages(std::array<RecvScratch, kReceiveBatch> &scratch,
                            std::array<mmsghdr, kReceiveBatch> &messages) {
  for (std::size_t i = 0; i < scratch.size(); ++i) {
    auto &entry = scratch[i];
    entry.iov = iovec{entry.buffer.data(), entry.buffer.size()};
    entry.message = mmsghdr{};
    entry.message.msg_hdr.msg_iov = &entry.iov;
    entry.message.msg_hdr.msg_iovlen = 1;
    entry.message.msg_hdr.msg_control = entry.control.data();
    entry.message.msg_hdr.msg_controllen = entry.control.size();
    messages[i] = entry.message;
  }
}

} // namespace

RxChannel::RxChannel(std::size_t queue_depth) : queue_(queue_depth) {
  notification_fd_ = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
  if (notification_fd_ < 0) {
    throw std::system_error(errno, std::generic_category(), "eventfd");
  }
}

RxChannel::~RxChannel() {
  if (notification_fd_ >= 0) {
    ::close(notification_fd_);
  }
}

bool RxChannel::Enqueue(const std::uint8_t *bytes, std::size_t length,
                        int flags, std::int64_t received_ns) {
  if (consumer_done()) {
    return false;
  }
  const auto copied = std::min(length, kReceiveDatagramSize);
  const bool pushed = queue_.try_write([&](ReceivedDatagram &slot) {
    std::memcpy(slot.bytes.data(), bytes, copied);
    slot.length = static_cast<std::uint32_t>(length);
    slot.flags = flags;
    slot.received_ns = received_ns;
  });
  if (!pushed) {
    if (!consumer_done()) {
      ++stats_.handoff_drops;
    }
    return false;
  }
  stats_.queue_high_water =
      std::max(stats_.queue_high_water, queue_.size_approx());
  return true;
}

bool RxChannel::Pop(ReceivedDatagram &datagram) { return queue_.pop(datagram); }

void RxChannel::Notify() {
  // A single unread eventfd token is sufficient. TX workers normally remain
  // active at high rates, so suppress notification writes until a worker
  // actually sleeps and acknowledges the pending token.
  if (notification_pending_.exchange(true, std::memory_order_acq_rel)) {
    return;
  }
  const std::uint64_t value = 1;
  for (;;) {
    const auto written = ::write(notification_fd_, &value, sizeof(value));
    if (written == static_cast<ssize_t>(sizeof(value)) ||
        (written < 0 && IsWouldBlock(errno))) {
      return;
    }
    if (written < 0 && errno == EINTR) {
      continue;
    }
    notification_pending_.store(false, std::memory_order_release);
    throw std::system_error(errno, std::generic_category(),
                            "RX notification eventfd write");
  }
}

void RxChannel::AcknowledgeNotifications() noexcept {
  std::uint64_t value = 0;
  while (::read(notification_fd_, &value, sizeof(value)) < 0 &&
         errno == EINTR) {
  }
  notification_pending_.store(false, std::memory_order_release);
}

void RxChannel::BeginReceiveBatch() noexcept {
  publication_epoch_.fetch_add(1, std::memory_order_acq_rel);
}

void RxChannel::EndReceiveBatch() noexcept {
  publication_epoch_.fetch_add(1, std::memory_order_release);
}

void RxChannel::RecordBatch(std::size_t datagrams) {
  ++stats_.receive_batches;
  stats_.receive_datagrams += datagrams;
}

void RxChannel::RecordSocketOverflow(const void *opaque_header) {
#ifdef SO_RXQ_OVFL
  auto &message = *static_cast<const msghdr *>(opaque_header);
  for (const cmsghdr *control = CMSG_FIRSTHDR(&message); control != nullptr;
       control = CMSG_NXTHDR(const_cast<msghdr *>(&message),
                             const_cast<cmsghdr *>(control))) {
    if (control->cmsg_level != SOL_SOCKET ||
        control->cmsg_type != SO_RXQ_OVFL ||
        control->cmsg_len < CMSG_LEN(sizeof(std::uint32_t))) {
      continue;
    }
    std::uint32_t cumulative = 0;
    std::memcpy(&cumulative, CMSG_DATA(const_cast<cmsghdr *>(control)),
                sizeof(cumulative));
    const auto delta =
        have_socket_drops_
            ? static_cast<std::uint32_t>(cumulative - last_socket_drops_)
            : cumulative;
    stats_.socket_receive_queue_drops += delta;
    last_socket_drops_ = cumulative;
    have_socket_drops_ = true;
  }
#else
  (void)opaque_header;
#endif
}

UdpReceiverPool::UdpReceiverPool(std::vector<RxEndpoint> endpoints,
                                 std::size_t thread_count,
                                 std::vector<std::uint32_t> cpus,
                                 std::atomic<bool> &cancel)
    : shards_(thread_count), cpus_(std::move(cpus)), cancel_(cancel),
      stop_fds_(thread_count, -1), results_(thread_count) {
  if (thread_count == 0 || thread_count > endpoints.size()) {
    throw std::invalid_argument(
        "RX thread count must be in [1, number of endpoints]");
  }
  if (!cpus_.empty() && cpus_.size() != thread_count) {
    throw std::invalid_argument("RX CPU count does not match RX threads");
  }
  for (std::size_t i = 0; i < endpoints.size(); ++i) {
    if (endpoints[i].socket < 0 || endpoints[i].channel == nullptr) {
      throw std::invalid_argument("invalid RX endpoint");
    }
    shards_[i % thread_count].push_back(endpoints[i]);
  }
  for (std::size_t i = 0; i < thread_count; ++i) {
    stop_fds_[i] = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (stop_fds_[i] < 0) {
      const int error = errno;
      for (auto fd : stop_fds_) {
        if (fd >= 0) {
          ::close(fd);
        }
      }
      throw std::system_error(error, std::generic_category(), "eventfd");
    }
    results_[i].requested_cpu = cpus_.empty() ? -1 : static_cast<int>(cpus_[i]);
  }
}

UdpReceiverPool::~UdpReceiverPool() {
  Stop();
  Join();
  for (const auto fd : stop_fds_) {
    if (fd >= 0) {
      ::close(fd);
    }
  }
}

void UdpReceiverPool::Start() {
  if (started_) {
    throw std::logic_error("RX pool already started");
  }
  started_ = true;
  threads_.reserve(shards_.size());
  try {
    for (std::size_t shard = 0; shard < shards_.size(); ++shard) {
      threads_.emplace_back(&UdpReceiverPool::Run, this, shard);
    }
  } catch (...) {
    Stop();
    Join();
    throw;
  }
}

void UdpReceiverPool::WakeStopFd(int fd) noexcept {
  const std::uint64_t value = 1;
  while (::write(fd, &value, sizeof(value)) < 0 && errno == EINTR) {
  }
}

void UdpReceiverPool::Stop() {
  if (!started_) {
    return;
  }
  stop_.store(true, std::memory_order_release);
  for (const auto fd : stop_fds_) {
    WakeStopFd(fd);
  }
}

void UdpReceiverPool::Join() {
  for (auto &thread : threads_) {
    if (thread.joinable()) {
      thread.join();
    }
  }
}

void UdpReceiverPool::Run(std::size_t shard) noexcept {
  try {
    NameCurrentThread("mc-rx-" + std::to_string(shard));
    PinCurrentThread(results_[shard].requested_cpu,
                     "RX thread " + std::to_string(shard));
    results_[shard].start_cpu = CurrentCpu();
    RunInner(shard);
    if (EnsureCurrentThreadPinned(results_[shard].requested_cpu,
                                  "RX thread " + std::to_string(shard))) {
      ++results_[shard].affinity_repairs;
    }
    results_[shard].end_cpu = CurrentCpu();
  } catch (const std::exception &error) {
    results_[shard].end_cpu = CurrentCpu();
    results_[shard].error = error.what();
    cancel_.store(true, std::memory_order_release);
    for (const auto &endpoint : shards_[shard]) {
      try {
        endpoint.channel->Notify();
      } catch (...) {
      }
    }
  } catch (...) {
    results_[shard].end_cpu = CurrentCpu();
    results_[shard].error = "unknown RX thread error";
    cancel_.store(true, std::memory_order_release);
  }
}

void UdpReceiverPool::RunInner(std::size_t shard) {
  const int epoll_fd = ::epoll_create1(EPOLL_CLOEXEC);
  if (epoll_fd < 0) {
    throw std::system_error(errno, std::generic_category(), "epoll_create1");
  }
  const ScopedFd epoll_guard(epoll_fd);

  epoll_event stop_event{};
  stop_event.events = EPOLLIN;
  stop_event.data.u32 = 0;
  if (::epoll_ctl(epoll_fd, EPOLL_CTL_ADD, stop_fds_[shard], &stop_event) !=
      0) {
    const int error = errno;
    throw std::system_error(error, std::generic_category(),
                            "epoll_ctl stop event");
  }
  for (std::size_t i = 0; i < shards_[shard].size(); ++i) {
    epoll_event event{};
    event.events = EPOLLIN;
    event.data.u32 = static_cast<std::uint32_t>(i + 1);
    if (::epoll_ctl(epoll_fd, EPOLL_CTL_ADD, shards_[shard][i].socket,
                    &event) != 0) {
      const int error = errno;
      throw std::system_error(error, std::generic_category(),
                              "epoll_ctl UDP socket");
    }
  }

  std::array<RecvScratch, kReceiveBatch> scratch{};
  std::array<mmsghdr, kReceiveBatch> messages{};
  std::array<epoll_event, 32> events{};
  std::int64_t next_affinity_check_ns =
      MonotonicNowNs() + kAffinityCheckIntervalNs;
  const auto maintain_affinity = [&](std::int64_t now_ns) {
    if (now_ns < next_affinity_check_ns) {
      return;
    }
    if (EnsureCurrentThreadPinned(results_[shard].requested_cpu,
                                  "RX thread " + std::to_string(shard))) {
      ++results_[shard].affinity_repairs;
    }
    next_affinity_check_ns = now_ns + kAffinityCheckIntervalNs;
  };
  while (!stop_.load(std::memory_order_acquire) &&
         !cancel_.load(std::memory_order_acquire)) {
    const int ready = ::epoll_wait(epoll_fd, events.data(), events.size(), 100);
    if (ready < 0) {
      if (errno == EINTR) {
        maintain_affinity(MonotonicNowNs());
        continue;
      }
      const int error = errno;
      throw std::system_error(error, std::generic_category(), "epoll_wait");
    }
    if (ready == 0) {
      maintain_affinity(MonotonicNowNs());
      continue;
    }
    bool observed_receive_time = false;
    ++results_[shard].epoll_wakeups;
    for (int event_index = 0; event_index < ready; ++event_index) {
      const auto tag = events[static_cast<std::size_t>(event_index)].data.u32;
      if (tag == 0) {
        return;
      }
      const auto endpoint_index = static_cast<std::size_t>(tag - 1);
      if (endpoint_index >= shards_[shard].size()) {
        throw std::runtime_error("invalid epoll endpoint tag");
      }
      auto &endpoint = shards_[shard][endpoint_index];
      if (endpoint.channel->consumer_done()) {
        (void)::epoll_ctl(epoll_fd, EPOLL_CTL_DEL, endpoint.socket, nullptr);
        continue;
      }
      for (std::size_t batch = 0; batch < kBatchesPerReadySocket; ++batch) {
        if (endpoint.channel->consumer_done()) {
          (void)::epoll_ctl(epoll_fd, EPOLL_CTL_DEL, endpoint.socket, nullptr);
          break;
        }
        PrepareReceiveMessages(scratch, messages);
        endpoint.channel->BeginReceiveBatch();
        ++results_[shard].receive_syscalls;
        const int received =
            ::recvmmsg(endpoint.socket, messages.data(), messages.size(),
                       MSG_DONTWAIT | MSG_TRUNC, nullptr);
        if (received < 0) {
          endpoint.channel->EndReceiveBatch();
          if (IsWouldBlock(errno)) {
            break;
          }
          if (errno == EINTR) {
            continue;
          }
          const int error = errno;
          throw std::system_error(error, std::generic_category(), "recvmmsg");
        }
        if (received == 0) {
          endpoint.channel->EndReceiveBatch();
          break;
        }
        if (endpoint.channel->consumer_done()) {
          endpoint.channel->EndReceiveBatch();
          (void)::epoll_ctl(epoll_fd, EPOLL_CTL_DEL, endpoint.socket, nullptr);
          break;
        }
        endpoint.channel->RecordBatch(static_cast<std::size_t>(received));
        const auto received_ns = MonotonicNowNs();
        observed_receive_time = true;
        maintain_affinity(received_ns);
        bool pushed_any = false;
        for (int i = 0; i < received; ++i) {
          const auto index = static_cast<std::size_t>(i);
          endpoint.channel->RecordSocketOverflow(&messages[index].msg_hdr);
          pushed_any =
              endpoint.channel->Enqueue(
                  scratch[index].buffer.data(), messages[index].msg_len,
                  messages[index].msg_hdr.msg_flags, received_ns) ||
              pushed_any;
        }
        endpoint.channel->EndReceiveBatch();
        if (pushed_any) {
          endpoint.channel->Notify();
        }
        if (static_cast<std::size_t>(received) < messages.size()) {
          break;
        }
      }
    }
    if (!observed_receive_time) {
      maintain_affinity(MonotonicNowNs());
    }
  }
}

} // namespace mcgen
