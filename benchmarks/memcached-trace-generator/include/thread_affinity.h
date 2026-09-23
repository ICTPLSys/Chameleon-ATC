#pragma once

#include <string_view>

namespace mcgen {

// A negative CPU leaves the current thread's affinity unchanged. Otherwise
// the thread is restricted to exactly one logical CPU and the effective mask
// is verified before returning. Throws std::system_error on Linux failures.
void PinCurrentThread(int cpu, std::string_view role);

// Re-assert a requested one-CPU mask if an external placement daemon changed
// it. Returns true when a repair was necessary.
bool EnsureCurrentThreadPinned(int cpu, std::string_view role);

// Returns sched_getcpu(), or -1 when the kernel cannot report a current CPU.
int CurrentCpu() noexcept;

// Give benchmark threads recognizable names in ps/pidstat output. Linux
// truncates names to 15 visible bytes; failure is non-fatal diagnostics only.
void NameCurrentThread(std::string_view name) noexcept;

} // namespace mcgen
