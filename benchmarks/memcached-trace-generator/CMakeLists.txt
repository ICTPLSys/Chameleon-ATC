cmake_minimum_required(VERSION 3.16)
project(memcached_trace_generator VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(HDR_HISTOGRAM_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/HdrHistogram_c" CACHE PATH
    "Path to the HdrHistogram_c source tree")
if(NOT EXISTS "${HDR_HISTOGRAM_ROOT}/include/hdr/hdr_histogram.h")
  message(FATAL_ERROR
          "HDR_HISTOGRAM_ROOT does not contain include/hdr/hdr_histogram.h: "
          "${HDR_HISTOGRAM_ROOT}")
endif()

set(HDR_HISTOGRAM_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
set(HDR_HISTOGRAM_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(HDR_HISTOGRAM_INSTALL_SHARED OFF CACHE BOOL "" FORCE)
set(HDR_HISTOGRAM_BUILD_STATIC ON CACHE BOOL "" FORCE)
set(HDR_HISTOGRAM_INSTALL_STATIC OFF CACHE BOOL "" FORCE)
set(HDR_LOG_REQUIRED DISABLED CACHE STRING "" FORCE)
add_subdirectory("${HDR_HISTOGRAM_ROOT}"
                 "${CMAKE_BINARY_DIR}/_deps/hdr_histogram"
                 EXCLUDE_FROM_ALL)

find_package(Threads REQUIRED)

add_library(mcgen_core
  src/memcached_protocol.cpp
  src/options.cpp
  src/poisson_scheduler.cpp
  src/stats.cpp
  src/thread_affinity.cpp
  src/trace_reader.cpp
  src/udp_receiver.cpp
  src/udp_worker.cpp)
target_include_directories(mcgen_core PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_link_libraries(mcgen_core PUBLIC hdr_histogram_static Threads::Threads)
target_compile_definitions(mcgen_core PRIVATE _GNU_SOURCE)
target_compile_options(mcgen_core PRIVATE
  -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)

add_executable(memcached-trace-generator src/main.cpp)
target_link_libraries(memcached-trace-generator PRIVATE mcgen_core)
target_compile_options(memcached-trace-generator PRIVATE
  -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)

include(CTest)
if(BUILD_TESTING)
  function(add_mcgen_test name source)
    add_executable(${name} ${source})
    target_link_libraries(${name} PRIVATE mcgen_core)
    target_compile_options(${name} PRIVATE
      -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)
    add_test(NAME ${name} COMMAND ${name})
  endfunction()

  add_mcgen_test(test_trace_reader tests/test_trace_reader.cpp)
  target_compile_definitions(test_trace_reader PRIVATE
    MCGEN_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data")
  add_mcgen_test(test_protocol tests/test_protocol.cpp)
  add_mcgen_test(test_options_scheduler tests/test_options_scheduler.cpp)
  add_mcgen_test(test_stats_queue tests/test_stats_queue.cpp)
  add_mcgen_test(test_udp_worker tests/test_udp_worker.cpp)

  find_program(MEMCACHED_EXECUTABLE memcached)
  if(MEMCACHED_EXECUTABLE)
    add_test(
      NAME test_memcached_integration
      COMMAND bash "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_memcached_integration.sh"
              "$<TARGET_FILE:memcached-trace-generator>"
              "${MEMCACHED_EXECUTABLE}"
              "${CMAKE_CURRENT_SOURCE_DIR}/tests/data/integration_trace.csv")
    add_test(
      NAME test_overload_hard_stop
      COMMAND bash "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_overload_hard_stop.sh"
              "$<TARGET_FILE:memcached-trace-generator>"
              "${MEMCACHED_EXECUTABLE}"
              "${CMAKE_CURRENT_SOURCE_DIR}/tests/data/integration_trace.csv")
    set_tests_properties(
      test_memcached_integration test_overload_hard_stop
      PROPERTIES TIMEOUT 20 RUN_SERIAL TRUE)
  endif()
endif()
