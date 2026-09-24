#pragma once

#include <atomic>
#include <cstddef>
#include <stdexcept>
#include <type_traits>
#include <utility>
#include <vector>

namespace mcgen {

// A bounded single-producer/single-consumer queue. Capacity is fixed so a
// trace of arbitrary length cannot make the generator's memory usage grow.
template <typename T> class SpscQueue {
public:
  explicit SpscQueue(std::size_t capacity) : slots_(capacity + 1) {
    if (capacity == 0) {
      throw std::invalid_argument("SPSC queue capacity must be positive");
    }
  }

  SpscQueue(const SpscQueue &) = delete;
  SpscQueue &operator=(const SpscQueue &) = delete;

  bool try_push(T value) {
    const auto tail = tail_.load(std::memory_order_relaxed);
    const auto next = increment(tail);
    if (next == head_.load(std::memory_order_acquire)) {
      return false;
    }
    slots_[tail] = std::move(value);
    tail_.store(next, std::memory_order_release);
    return true;
  }

  // Populate the producer-owned slot in place and publish it only after the
  // callback returns. This avoids an additional large-object move for rings
  // whose entries contain receive buffers. The callback must not retain a
  // reference to the slot or throw.
  template <typename Writer> bool try_write(Writer &&writer) {
    static_assert(std::is_invocable_v<Writer, T &>,
                  "writer must accept a queue slot by reference");
    const auto tail = tail_.load(std::memory_order_relaxed);
    const auto next = increment(tail);
    if (next == head_.load(std::memory_order_acquire)) {
      return false;
    }
    std::forward<Writer>(writer)(slots_[tail]);
    tail_.store(next, std::memory_order_release);
    return true;
  }

  const T *front() const {
    const auto head = head_.load(std::memory_order_relaxed);
    if (head == tail_.load(std::memory_order_acquire)) {
      return nullptr;
    }
    return &slots_[head];
  }

  bool pop(T &value) {
    const auto head = head_.load(std::memory_order_relaxed);
    if (head == tail_.load(std::memory_order_acquire)) {
      return false;
    }
    value = std::move(slots_[head]);
    head_.store(increment(head), std::memory_order_release);
    return true;
  }

  // Invoke the consumer on the queue-owned slot and release it afterwards.
  // This avoids copying large entries such as received UDP datagrams. The
  // callback must not retain a reference to the slot.
  template <typename Consumer> bool consume_front(Consumer &&consumer) {
    static_assert(std::is_invocable_v<Consumer, const T &>,
                  "consumer must accept a const queue slot reference");
    const auto head = head_.load(std::memory_order_relaxed);
    if (head == tail_.load(std::memory_order_acquire)) {
      return false;
    }
    std::forward<Consumer>(consumer)(slots_[head]);
    head_.store(increment(head), std::memory_order_release);
    return true;
  }

  bool empty() const { return front() == nullptr; }
  std::size_t capacity() const { return slots_.size() - 1; }

  // A lock-free diagnostic snapshot. The producer and consumer may move the
  // indices while they are sampled, so callers must not use this value for
  // correctness decisions. It is bounded by capacity() and is suitable for
  // queue high-water reporting.
  std::size_t size_approx() const {
    const auto head = head_.load(std::memory_order_acquire);
    const auto tail = tail_.load(std::memory_order_acquire);
    return tail >= head ? tail - head : slots_.size() - head + tail;
  }

private:
  std::size_t increment(std::size_t value) const {
    ++value;
    return value == slots_.size() ? 0 : value;
  }

  std::vector<T> slots_;
  alignas(64) std::atomic<std::size_t> head_{0};
  alignas(64) std::atomic<std::size_t> tail_{0};
};

} // namespace mcgen
