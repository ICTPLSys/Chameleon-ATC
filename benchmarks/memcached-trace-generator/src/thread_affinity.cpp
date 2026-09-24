#include "thread_affinity.h"

#include <pthread.h>
#include <sched.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <stdexcept>
#include <string>
#include <system_error>

namespace mcgen {

namespace {

bool HasExactCpuAffinity(int cpu, std::string_view role) {
  cpu_set_t effective;
  CPU_ZERO(&effective);
  const int get_error =
      ::pthread_getaffinity_np(::pthread_self(), sizeof(effective), &effective);
  if (get_error != 0) {
    throw std::system_error(get_error, std::generic_category(),
                            std::string("cannot read ") + std::string(role) +
                                " affinity");
  }
  return CPU_COUNT(&effective) == 1 &&
         CPU_ISSET(static_cast<std::size_t>(cpu), &effective);
}

} // namespace

void PinCurrentThread(int cpu, std::string_view role) {
  if (cpu < 0) {
    return;
  }
  if (cpu >= CPU_SETSIZE) {
    throw std::invalid_argument(std::string(role) + " CPU " +
                                std::to_string(cpu) + " exceeds CPU_SETSIZE");
  }

  cpu_set_t requested;
  CPU_ZERO(&requested);
  CPU_SET(static_cast<std::size_t>(cpu), &requested);
  const int set_error =
      ::pthread_setaffinity_np(::pthread_self(), sizeof(requested), &requested);
  if (set_error != 0) {
    throw std::system_error(set_error, std::generic_category(),
                            std::string("cannot pin ") + std::string(role) +
                                " to CPU " + std::to_string(cpu));
  }

  if (!HasExactCpuAffinity(cpu, role)) {
    throw std::runtime_error(std::string(role) +
                             " affinity verification failed for CPU " +
                             std::to_string(cpu));
  }
}

bool EnsureCurrentThreadPinned(int cpu, std::string_view role) {
  if (cpu < 0) {
    return false;
  }
  if (cpu >= CPU_SETSIZE) {
    throw std::invalid_argument(std::string(role) + " CPU " +
                                std::to_string(cpu) + " exceeds CPU_SETSIZE");
  }
  if (HasExactCpuAffinity(cpu, role)) {
    return false;
  }
  PinCurrentThread(cpu, role);
  return true;
}

int CurrentCpu() noexcept { return ::sched_getcpu(); }

void NameCurrentThread(std::string_view name) noexcept {
  char truncated[16]{};
  const auto length = std::min<std::size_t>(name.size(), sizeof(truncated) - 1);
  std::memcpy(truncated, name.data(), length);
  (void)::pthread_setname_np(::pthread_self(), truncated);
}

} // namespace mcgen
