#include "udp_worker.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <memory>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace {

using namespace std::chrono_literals;

int failures = 0;

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      std::cerr << __FILE__ << ':' << __LINE__                                 \
                << ": CHECK failed: " << #condition << '\n';                   \
      ++failures;                                                              \
    }                                                                          \
  } while (false)

std::uint16_t ReadU16(const std::uint8_t *input) {
  return static_cast<std::uint16_t>(
      (static_cast<std::uint16_t>(input[0]) << 8) |
      static_cast<std::uint16_t>(input[1]));
}

std::uint32_t ReadU32(const std::uint8_t *input) {
  return (static_cast<std::uint32_t>(input[0]) << 24) |
         (static_cast<std::uint32_t>(input[1]) << 16) |
         (static_cast<std::uint32_t>(input[2]) << 8) |
         static_cast<std::uint32_t>(input[3]);
}

mcgen::SharedRequest MakeSharedRequest(std::string_view key,
                                       mcgen::Operation operation) {
  auto trace_key = std::make_shared<mcgen::TraceKey>();
  std::copy(key.begin(), key.end(), trace_key->bytes.begin());
  trace_key->length = static_cast<std::uint16_t>(key.size());
  trace_key->hash = mcgen::StableKeyHash(key);
  return mcgen::SharedRequest{std::move(trace_key), operation};
}

void PutU16(std::uint8_t *output, std::uint16_t value) {
  output[0] = static_cast<std::uint8_t>(value >> 8);
  output[1] = static_cast<std::uint8_t>(value);
}

void PutU32(std::uint8_t *output, std::uint32_t value) {
  output[0] = static_cast<std::uint8_t>(value >> 24);
  output[1] = static_cast<std::uint8_t>(value >> 16);
  output[2] = static_cast<std::uint8_t>(value >> 8);
  output[3] = static_cast<std::uint8_t>(value);
}

struct FakeServerResult {
  std::string error;
  std::uint32_t requests = 0;
  std::uint32_t responses = 0;
};

void SetServerError(FakeServerResult &result, const std::string &message) {
  if (result.error.empty()) {
    result.error = message;
  }
}

std::vector<std::uint8_t> MakeGetHitResponse(std::uint32_t opaque) {
  constexpr std::size_t kBodySize = 2800;
  std::vector<std::uint8_t> response(mcgen::kBinaryHeaderSize + kBodySize, 0);
  response[0] = 0x81; // Binary response magic.
  response[1] = static_cast<std::uint8_t>(mcgen::Opcode::Get);
  response[4] = 4; // Four-byte flags extras in a successful GET response.
  PutU16(response.data() + 6, 0); // Success.
  PutU32(response.data() + 8, static_cast<std::uint32_t>(kBodySize));
  PutU32(response.data() + 12, opaque);
  for (std::size_t i = mcgen::kBinaryHeaderSize + 4; i < response.size(); ++i) {
    response[i] = static_cast<std::uint8_t>(i);
  }
  return response;
}

std::vector<std::uint8_t>
FrameResponse(std::uint16_t request_id, std::uint16_t sequence,
              std::uint16_t total, const std::vector<std::uint8_t> &response,
              std::size_t begin, std::size_t end) {
  std::vector<std::uint8_t> datagram(mcgen::kUdpHeaderSize + end - begin, 0);
  PutU16(datagram.data(), request_id);
  PutU16(datagram.data() + 2, sequence);
  PutU16(datagram.data() + 4, total);
  PutU16(datagram.data() + 6, 0);
  std::memcpy(datagram.data() + mcgen::kUdpHeaderSize, response.data() + begin,
              end - begin);
  return datagram;
}

bool SendDatagram(int socket, const sockaddr_storage &peer,
                  socklen_t peer_length,
                  const std::vector<std::uint8_t> &datagram,
                  FakeServerResult &result) {
  const ssize_t sent =
      ::sendto(socket, datagram.data(), datagram.size(), 0,
               reinterpret_cast<const sockaddr *>(&peer), peer_length);
  if (sent != static_cast<ssize_t>(datagram.size())) {
    SetServerError(result,
                   std::string("sendto failed: ") + std::strerror(errno));
    return false;
  }
  return true;
}

bool RespondWithThreeFragments(int socket, const sockaddr_storage &peer,
                               socklen_t peer_length, std::uint16_t request_id,
                               std::uint32_t opaque, FakeServerResult &result) {
  constexpr std::size_t kMemcachedUdpDataSize = 1392;
  const auto response = MakeGetHitResponse(opaque);
  if (response.size() <= 2 * kMemcachedUdpDataSize ||
      response.size() > 3 * kMemcachedUdpDataSize) {
    SetServerError(result, "test response does not split into three packets");
    return false;
  }

  const auto fragment0 =
      FrameResponse(request_id, 0, 3, response, 0, kMemcachedUdpDataSize);
  const auto fragment1 =
      FrameResponse(request_id, 1, 3, response, kMemcachedUdpDataSize,
                    2 * kMemcachedUdpDataSize);
  const auto fragment2 = FrameResponse(
      request_id, 2, 3, response, 2 * kMemcachedUdpDataSize, response.size());

  // Deliver the last fragment twice before sequence zero. The short pause
  // ensures the worker observes the duplicate while the request is active,
  // rather than after completion when it would correctly be classified stale.
  if (!SendDatagram(socket, peer, peer_length, fragment2, result) ||
      !SendDatagram(socket, peer, peer_length, fragment2, result)) {
    return false;
  }
  std::this_thread::sleep_for(5ms);
  if (!SendDatagram(socket, peer, peer_length, fragment0, result) ||
      !SendDatagram(socket, peer, peer_length, fragment1, result)) {
    return false;
  }
  ++result.responses;
  return true;
}

void RunFakeServer(int socket, FakeServerResult &result) {
  constexpr std::size_t kMaxRequestSize = mcgen::kMaxUdpPayloadSize;
  std::vector<std::uint8_t> request(kMaxRequestSize);

  while (result.requests < 2 && result.error.empty()) {
    pollfd descriptor{socket, POLLIN, 0};
    int ready;
    do {
      ready = ::poll(&descriptor, 1, 2000);
    } while (ready < 0 && errno == EINTR);
    if (ready == 0) {
      SetServerError(result, "timed out waiting for worker request");
      break;
    }
    if (ready < 0) {
      SetServerError(result,
                     std::string("poll failed: ") + std::strerror(errno));
      break;
    }

    sockaddr_storage peer{};
    socklen_t peer_length = sizeof(peer);
    const ssize_t received =
        ::recvfrom(socket, request.data(), request.size(), 0,
                   reinterpret_cast<sockaddr *>(&peer), &peer_length);
    if (received < 0) {
      if (errno == EINTR) {
        continue;
      }
      SetServerError(result,
                     std::string("recvfrom failed: ") + std::strerror(errno));
      break;
    }
    ++result.requests;

    const auto length = static_cast<std::size_t>(received);
    if (length < mcgen::kUdpHeaderSize + mcgen::kBinaryHeaderSize) {
      SetServerError(result, "worker sent a truncated request");
      break;
    }
    const std::uint16_t request_id = ReadU16(request.data());
    const std::uint16_t sequence = ReadU16(request.data() + 2);
    const std::uint16_t total = ReadU16(request.data() + 4);
    const std::uint16_t reserved = ReadU16(request.data() + 6);
    const std::uint8_t *binary = request.data() + mcgen::kUdpHeaderSize;
    const std::uint16_t key_length = ReadU16(binary + 2);
    const std::uint8_t extras_length = binary[4];
    const std::uint32_t body_length = ReadU32(binary + 8);
    const std::uint32_t opaque = ReadU32(binary + 12);

    if (sequence != 0 || total != 1 || reserved != 0 || binary[0] != 0x80 ||
        binary[1] != static_cast<std::uint8_t>(mcgen::Opcode::Get) ||
        extras_length != 0 || body_length != key_length ||
        mcgen::kUdpHeaderSize + mcgen::kBinaryHeaderSize + key_length !=
            length) {
      SetServerError(result, "worker sent an invalid binary UDP GET");
      break;
    }
    const std::string key(
        reinterpret_cast<const char *>(binary + mcgen::kBinaryHeaderSize),
        key_length);
    if (key == "respond") {
      if (!RespondWithThreeFragments(socket, peer, peer_length, request_id,
                                     opaque, result)) {
        break;
      }
    } else if (key != "timeout") {
      SetServerError(result, "worker sent an unexpected key: " + key);
      break;
    }
    // The "timeout" request is deliberately left unanswered.
  }
  ::close(socket);
}

void RunDropServer(int socket, FakeServerResult &result) {
  std::vector<std::uint8_t> request(mcgen::kMaxUdpPayloadSize);
  pollfd descriptor{socket, POLLIN, 0};
  const int ready = ::poll(&descriptor, 1, 2000);
  if (ready <= 0) {
    SetServerError(result, "drop server did not receive a request");
    ::close(socket);
    return;
  }
  const ssize_t received = ::recv(socket, request.data(), request.size(), 0);
  if (received <= 0) {
    SetServerError(result, "drop server recv failed");
  } else {
    ++result.requests;
  }
  ::close(socket);
}

int BindLoopbackUdp(std::uint16_t &port) {
  const int socket = ::socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, IPPROTO_UDP);
  if (socket < 0) {
    return -1;
  }
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  address.sin_port = 0;
  if (::bind(socket, reinterpret_cast<const sockaddr *>(&address),
             sizeof(address)) != 0) {
    ::close(socket);
    return -1;
  }
  socklen_t length = sizeof(address);
  if (::getsockname(socket, reinterpret_cast<sockaddr *>(&address), &length) !=
      0) {
    ::close(socket);
    return -1;
  }
  port = ntohs(address.sin_port);
  return socket;
}

void TestUdpWorkerWithFakeServer() {
  std::uint16_t port = 0;
  const int server_socket = BindLoopbackUdp(port);
  CHECK(server_socket >= 0);
  if (server_socket < 0) {
    return;
  }

  FakeServerResult server_result;
  std::thread server(RunFakeServer, server_socket, std::ref(server_result));

  mcgen::SpscQueue<mcgen::ScheduledRequest> queue(4);
  std::atomic<bool> producer_done{false};
  std::atomic<bool> cancel{false};
  const std::int64_t now = mcgen::MonotonicNowNs();
  const std::int64_t send_deadline = now + 20'000'000LL;

  mcgen::ScheduledRequest answered;
  answered.request = MakeSharedRequest("respond", mcgen::Operation::Get);
  answered.deadline_ns = send_deadline;
  answered.measured = true;
  mcgen::ScheduledRequest unanswered;
  unanswered.request = MakeSharedRequest("timeout", mcgen::Operation::Get);
  unanswered.deadline_ns = send_deadline;
  unanswered.measured = true;
  CHECK(queue.try_push(std::move(answered)));
  CHECK(queue.try_push(std::move(unanswered)));
  producer_done.store(true, std::memory_order_release);

  mcgen::WorkerConfig config;
  config.server = "127.0.0.1:" + std::to_string(port);
  config.value_size = 4096;
  config.request_timeout_ms = 80;
  config.max_send_lag_us = 500'000;
  config.max_inflight = 8;
  config.batch_size = 2;
  config.socket_buffer_mb = 1;
  config.drain_deadline_ns = now + 400'000'000LL;

  mcgen::UdpWorker worker(0, config, queue, producer_done, cancel);
  worker.Start();
  worker.Join();
  server.join();

  CHECK(server_result.error.empty());
  CHECK(server_result.requests == 2);
  CHECK(server_result.responses == 1);

  const auto &result = worker.result();
  CHECK(result.error.empty());
  CHECK(result.total.sent == 2);
  CHECK(result.total.completed == 1);
  CHECK(result.total.get_hit == 1);
  CHECK(result.total.multi_fragment_response == 1);
  CHECK(result.total.duplicate_fragment == 1);
  CHECK(result.total.timeout == 1);
  CHECK(result.total.outstanding == 0);
  CHECK(result.total.malformed_fragment == 0);
  CHECK(result.total.server_error == 0);

  CHECK(result.measured.sent == 2);
  CHECK(result.measured.completed == 1);
  CHECK(result.measured.multi_fragment_response == 1);
  CHECK(result.measured.duplicate_fragment == 1);
  CHECK(result.measured.timeout == 1);
  CHECK(result.histogram.count() == 1);
  CHECK(result.histogram.percentile_ns(50.0) > 0);
}

void TestNoSlotBudgetAndCancellation() {
  std::uint16_t port = 0;
  const int server_socket = BindLoopbackUdp(port);
  CHECK(server_socket >= 0);
  if (server_socket < 0) {
    return;
  }
  FakeServerResult server_result;
  std::thread server(RunDropServer, server_socket, std::ref(server_result));

  constexpr std::size_t kRequests = 64;
  mcgen::SpscQueue<mcgen::ScheduledRequest> queue(kRequests);
  std::atomic<bool> producer_done{false};
  std::atomic<bool> cancel{false};
  const auto now = mcgen::MonotonicNowNs();
  for (std::size_t i = 0; i < kRequests; ++i) {
    mcgen::ScheduledRequest request;
    request.request = MakeSharedRequest("drop", mcgen::Operation::Get);
    request.deadline_ns = now + 10'000'000LL;
    request.measured = true;
    CHECK(queue.try_push(std::move(request)));
  }
  producer_done.store(true, std::memory_order_release);

  mcgen::WorkerConfig config;
  config.server = "127.0.0.1:" + std::to_string(port);
  config.request_timeout_ms = 30;
  config.max_send_lag_us = 500'000;
  config.max_inflight = 1;
  config.batch_size = 4;
  config.socket_buffer_mb = 1;
  config.drain_deadline_ns = now + 120'000'000LL;

  mcgen::UdpWorker worker(1, config, queue, producer_done, cancel);
  worker.Start();
  worker.Join();
  server.join();
  CHECK(server_result.error.empty());
  CHECK(server_result.requests == 1);
  CHECK(worker.result().error.empty());
  CHECK(worker.result().measured.sent == 1);
  CHECK(worker.result().measured.no_slot == kRequests - 1);
  CHECK(worker.result().measured.timeout == 1);

  // An unstarted workload with an effectively infinite drain must still
  // terminate promptly when another component cancels the sample.
  std::uint16_t cancel_port = 0;
  const int cancel_socket = BindLoopbackUdp(cancel_port);
  CHECK(cancel_socket >= 0);
  if (cancel_socket < 0) {
    return;
  }
  mcgen::SpscQueue<mcgen::ScheduledRequest> empty_queue(1);
  std::atomic<bool> not_done{false};
  std::atomic<bool> cancel_now{false};
  config.server = "127.0.0.1:" + std::to_string(cancel_port);
  config.drain_deadline_ns = std::numeric_limits<std::int64_t>::max();
  mcgen::UdpWorker cancelled_worker(2, config, empty_queue, not_done,
                                    cancel_now);
  cancelled_worker.Start();
  cancel_now.store(true, std::memory_order_release);
  cancelled_worker.Join();
  CHECK(cancelled_worker.result().error.empty());
  ::close(cancel_socket);
}

void TestHardDrainDeadline() {
  std::uint16_t port = 0;
  const int server_socket = BindLoopbackUdp(port);
  CHECK(server_socket >= 0);
  if (server_socket < 0) {
    return;
  }

  mcgen::SpscQueue<mcgen::ScheduledRequest> queue(4);
  std::atomic<bool> producer_done{true};
  std::atomic<bool> cancel{false};
  const auto now = mcgen::MonotonicNowNs();
  mcgen::ScheduledRequest request;
  request.request = MakeSharedRequest("must-not-send", mcgen::Operation::Get);
  request.deadline_ns = now - 1'000'000LL;
  request.measured = true;
  CHECK(queue.try_push(std::move(request)));

  mcgen::WorkerConfig config;
  config.server = "127.0.0.1:" + std::to_string(port);
  config.max_send_lag_us = 500'000;
  config.max_inflight = 8;
  config.batch_size = 2;
  config.socket_buffer_mb = 1;
  config.drain_deadline_ns = now - 1;

  mcgen::UdpWorker worker(3, config, queue, producer_done, cancel);
  worker.Start();
  worker.Join();

  const auto &result = worker.result();
  CHECK(result.error.empty());
  CHECK(result.total.sent == 0);
  CHECK(result.total.stopped_at_deadline == 1);
  CHECK(result.offered == 1);
  CHECK(result.dequeued == 1);

  pollfd descriptor{server_socket, POLLIN, 0};
  CHECK(::poll(&descriptor, 1, 20) == 0);
  ::close(server_socket);
}

} // namespace

int main() {
  TestUdpWorkerWithFakeServer();
  TestNoSlotBudgetAndCancellation();
  TestHardDrainDeadline();
  if (failures != 0) {
    std::cerr << failures << " UDP worker integration test(s) failed\n";
    return 1;
  }
  std::cout << "UDP worker integration test passed\n";
  return 0;
}
