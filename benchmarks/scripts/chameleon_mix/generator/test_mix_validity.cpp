#include "stats.h"
#include <iostream>

int main() {
  mcgen::SampleResult result;
  result.schedule_complete = true;
  result.target_mpps = 0.01;
  result.measured_seconds = 108.0;
  result.counters.scheduled = 1080774;
  result.counters.producer_late = 6827;
  result.counters.late = 2916;
  result.counters.sent = 1071031;
  result.counters.completed = 1071031;
  result.counters.get_hit = 1071031;
  if (!mcgen::LoadValidityError(result).empty()) return 1;
  result.counters.scheduled = 1000000;
  result.counters.producer_late = 10000;
  if (mcgen::LoadValidityError(result).find("producer_overload") != std::string::npos) return 2;
  result.counters.producer_late = 10001;
  if (mcgen::LoadValidityError(result).find("producer_overload") == std::string::npos) return 3;
  result.counters.producer_late = 0;
  result.receive_handoff_drops = 1;
  if (mcgen::LoadValidityError(result).find("rx_handoff_drops") == std::string::npos) return 4;
  std::cout << "PASS\n";
}
