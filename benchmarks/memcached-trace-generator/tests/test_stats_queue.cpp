#include "spsc_queue.h"
#include "stats.h"
#include "udp_receiver.h"

#include <algorithm>
#include <vector>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <string>

namespace {

int failures = 0;

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      std::cerr << __FILE__ << ':' << __LINE__                                 \
                << ": check failed: " << #condition << '\n';                   \
      ++failures;                                                              \
    }                                                                          \
  } while (false)

void TestQueue() {
  mcgen::SpscQueue<std::string> queue(2);
  CHECK(queue.capacity() == 2);
  CHECK(queue.empty());
  CHECK(queue.size_approx() == 0);
  CHECK(queue.try_push("one"));
  CHECK(queue.size_approx() == 1);
  CHECK(queue.try_push("two"));
  CHECK(queue.size_approx() == 2);
  CHECK(!queue.try_push("three"));
  CHECK(queue.front() != nullptr && *queue.front() == "one");
  std::string value;
  CHECK(queue.pop(value));
  CHECK(value == "one");
  CHECK(queue.try_push("three"));
  CHECK(queue.pop(value));
  CHECK(value == "two");
  CHECK(queue.pop(value));
  CHECK(value == "three");
  CHECK(queue.size_approx() == 0);
  CHECK(!queue.pop(value));

  CHECK(queue.try_write([](std::string &slot) { slot = "in-place"; }));
  CHECK(queue.front() != nullptr && *queue.front() == "in-place");
  CHECK(queue.pop(value));
  CHECK(value == "in-place");

  CHECK(queue.try_push("consume-in-place"));
  bool consumed = false;
  CHECK(queue.consume_front([&](const std::string &slot) {
    CHECK(slot == "consume-in-place");
    consumed = true;
  }));
  CHECK(consumed);
  CHECK(queue.empty());
}

void TestHistogramAndOutput() {
  mcgen::Histogram first;
  mcgen::Histogram second;
  CHECK(first.record(1'000));
  CHECK(first.record(2'000));
  CHECK(second.record(3'000));
  CHECK(first.add(second) == 0);
  CHECK(first.count() == 3);
  CHECK(first.percentile_ns(50.0) >= 1'000);
  CHECK(first.percentile_ns(99.0) >= first.percentile_ns(50.0));
  CHECK(first.max_ns() >= 3'000);

  mcgen::SampleResult result;
  result.sample = 1;
  result.target_mpps = 0.1;
  result.measured_seconds = 1.0;
  result.schedule_complete = true;
  result.counters.scheduled = 3;
  result.counters.sent = 3;
  result.counters.completed = 3;
  result.counters.get_hit = 3;
  CHECK(result.histogram.record(1'000));
  CHECK(result.histogram.record(2'000));
  CHECK(result.histogram.record(3'000));
  std::ostringstream output;
  mcgen::PrintCsvHeader(output);
  mcgen::PrintCsvRow(output, result);
  CHECK(output.str().find("p99.99_us") != std::string::npos);
  // CSV schema and percentile values must stay aligned when adding P95.
  std::istringstream csv(output.str());
  std::string header_line, value_line, extra_line;
  CHECK(static_cast<bool>(std::getline(csv, header_line)));
  CHECK(static_cast<bool>(std::getline(csv, value_line)));
  CHECK(!std::getline(csv, extra_line));
  const auto split = [](const std::string &line) {
    std::vector<std::string> fields;
    std::istringstream input(line);
    std::string field;
    while (std::getline(input, field, ',')) fields.push_back(field);
    return fields;
  };
  const auto headers = split(header_line);
  const auto values = split(value_line);
  CHECK(headers.size() == values.size());
  for (const auto *name : {"completed_p95_us", "offered_p95_us"}) {
    const auto it = std::find(headers.begin(), headers.end(), name);
    CHECK(it != headers.end());
    if (it != headers.end() && headers.size() == values.size()) {
      const auto index = static_cast<std::size_t>(it - headers.begin());
      CHECK(std::stod(values[index]) >= 3.0);
      CHECK(std::stod(values[index]) < 3.01);
    }
  }

  CHECK(output.str().find("host_udp_rcvbuf_errors_delta_total_sample") !=
        std::string::npos);
  CHECK(output.str().find("rx_handoff_drops_total_sample") !=
        std::string::npos);
  CHECK(output.str().find("affinity_repairs_total_sample") !=
        std::string::npos);
  CHECK(output.str().find("0.100000") != std::string::npos);
  CHECK(mcgen::DropAwarePercentileNs(result, 99.0).has_value());
  CHECK(mcgen::CounterInvariantError(result.counters).empty());
  ++result.counters.late;
  CHECK(!mcgen::CounterInvariantError(result.counters).empty());

  mcgen::SampleResult dropped;
  dropped.schedule_complete = true;
  dropped.counters.scheduled = 4;
  dropped.counters.sent = 3;
  dropped.counters.completed = 3;
  dropped.counters.get_hit = 3;
  dropped.counters.queue_full = 1;
  CHECK(dropped.histogram.record(1'000));
  CHECK(dropped.histogram.record(2'000));
  CHECK(dropped.histogram.record(3'000));
  CHECK(mcgen::DropAwarePercentileNs(dropped, 50.0).has_value());
  CHECK(!mcgen::DropAwarePercentileNs(dropped, 90.0).has_value());
  CHECK(mcgen::CounterInvariantError(dropped.counters).empty());

  dropped.schedule_complete = false;
  CHECK(!mcgen::DropAwarePercentileNs(dropped, 50.0).has_value());
  CHECK(mcgen::LoadValidityError(dropped).find("schedule_incomplete") !=
        std::string::npos);

  mcgen::SampleResult invalid_rate;
  invalid_rate.schedule_complete = true;
  invalid_rate.target_mpps = 0.1;
  invalid_rate.measured_seconds = 1.0;
  invalid_rate.counters.scheduled = 10'000;
  invalid_rate.counters.sent = 10'000;
  invalid_rate.counters.completed = 10'000;
  invalid_rate.counters.get_hit = 10'000;
  CHECK(mcgen::LoadValidityError(invalid_rate)
            .find("tx_rate_outside_tolerance") != std::string::npos);

  invalid_rate.receive_handoff_drops = 1;
  CHECK(mcgen::LoadValidityError(invalid_rate).find("rx_handoff_drops") !=
        std::string::npos);
  invalid_rate.affinity_repairs = 1;
  CHECK(mcgen::LoadValidityError(invalid_rate).find("affinity_interference") !=
        std::string::npos);
}

void TestReceiveChannel() {
  mcgen::RxChannel channel(1);
  CHECK(channel.publication_epoch() == 0);
  channel.BeginReceiveBatch();
  CHECK((channel.publication_epoch() & 1U) != 0);
  channel.EndReceiveBatch();
  CHECK(channel.publication_epoch() == 2);

  const std::uint8_t bytes[] = {1, 2, 3};
  CHECK(channel.Enqueue(bytes, sizeof(bytes), 0, 123));
  CHECK(!channel.Enqueue(bytes, sizeof(bytes), 0, 456));
  CHECK(channel.stats().handoff_drops == 1);
  bool consumed = false;
  CHECK(channel.Consume([&](const mcgen::ReceivedDatagram &datagram) {
    CHECK(datagram.length == sizeof(bytes));
    CHECK(datagram.bytes[0] == 1);
    CHECK(datagram.received_ns == 123);
    consumed = true;
  }));
  CHECK(consumed);

  channel.MarkConsumerDone();
  CHECK(channel.consumer_done());
  CHECK(!channel.Enqueue(bytes, sizeof(bytes), 0, 789));
  CHECK(channel.stats().handoff_drops == 1);
}

} // namespace

int main() {
  TestQueue();
  TestHistogramAndOutput();
  TestReceiveChannel();
  if (failures != 0) {
    std::cerr << failures << " test(s) failed\n";
    return 1;
  }
  std::cout << "stats/queue tests passed\n";
  return 0;
}
