PVC_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
METIS_ROOT ?= $(abspath $(PVC_DIR)/../metis)
PVC_BUILD ?= $(PVC_DIR)/build
PVC_BUILD_MARKER := $(PVC_BUILD)/.pvc-build-dir

CXX ?= g++
MAXCPUS ?= $(shell grep -c '^processor' /proc/cpuinfo)
PVC_OPTFLAGS ?= -O3 -g -fno-omit-frame-pointer
PVC_CXXFLAGS := -std=gnu++11 -Wall -Wextra $(PVC_OPTFLAGS)
PVC_METIS_FLAGS := -D_GNU_SOURCE -include $(METIS_ROOT)/config.h \
	-I$(METIS_ROOT) -I$(METIS_ROOT)/lib -I$(PVC_DIR) \
	-DJTLS=__thread -DJSHARED_ATTR= -DJOS_CLINE=64 \
	-DCACHE_LINE_SIZE=64 -DJOS_NCPU=$(MAXCPUS) -D__STDC_FORMAT_MACROS
METIS_LIBS ?= -ldl -lc -lm -lpthread -ldl

PVC_BIN := $(PVC_BUILD)/page_view_count
PVC_TOOLS := $(PVC_BUILD)/pvc_generate $(PVC_BUILD)/konect_to_pvc

.PHONY: all page_view_count tools metis test clean

all: page_view_count tools

page_view_count: $(PVC_BIN)

tools: $(PVC_TOOLS)

test: all
	python3 "$(PVC_DIR)/tests/test-pvc.py" \
		--pvc "$(PVC_BIN)" \
		--generator "$(PVC_BUILD)/pvc_generate" \
		--converter "$(PVC_BUILD)/konect_to_pvc"

metis:
	@test -x "$(METIS_ROOT)/configure" || \
		{ echo "METIS_ROOT does not contain Metis: $(METIS_ROOT)" >&2; exit 1; }
	@if test ! -f "$(METIS_ROOT)/GNUmakefile" || \
		   test ! -f "$(METIS_ROOT)/config.h"; then \
		cd "$(METIS_ROOT)" && ./configure $(METIS_CONFIGURE_FLAGS); \
	fi
	$(MAKE) -C "$(METIS_ROOT)" obj/libmetis.a

$(PVC_BUILD_MARKER):
	mkdir -p "$(PVC_BUILD)"
	touch "$@"

$(PVC_BIN): $(PVC_DIR)/page_view_count.cc $(PVC_DIR)/pvc_format.hh | $(PVC_BUILD_MARKER) metis
	$(CXX) $(CPPFLAGS) $(PVC_CXXFLAGS) $(CXXFLAGS) $(PVC_METIS_FLAGS) \
		-o "$@" $(PVC_DIR)/page_view_count.cc \
		$(METIS_ROOT)/obj/libmetis.a $(LDFLAGS) $(METIS_LIBS) $(LDLIBS)

$(PVC_BUILD)/pvc_generate: $(PVC_DIR)/pvc_generate.cc $(PVC_DIR)/pvc_format.hh | $(PVC_BUILD_MARKER)
	$(CXX) $(CPPFLAGS) -std=gnu++11 -Wall -Wextra $(PVC_OPTFLAGS) $(CXXFLAGS) \
		-I$(PVC_DIR) -o "$@" $(PVC_DIR)/pvc_generate.cc $(LDFLAGS) $(LDLIBS)

$(PVC_BUILD)/konect_to_pvc: $(PVC_DIR)/konect_to_pvc.cc $(PVC_DIR)/pvc_format.hh | $(PVC_BUILD_MARKER)
	$(CXX) $(CPPFLAGS) -std=gnu++11 -Wall -Wextra $(PVC_OPTFLAGS) $(CXXFLAGS) \
		-I$(PVC_DIR) -o "$@" $(PVC_DIR)/konect_to_pvc.cc $(LDFLAGS) $(LDLIBS)

clean:
	@build_path=$$(realpath -m -- "$(PVC_BUILD)"); \
	pvc_path=$$(realpath -m -- "$(PVC_DIR)"); \
	metis_path=$$(realpath -m -- "$(METIS_ROOT)"); \
	if test -z "$$build_path" || test "$$build_path" = / || \
	   test "$$build_path" = "$$pvc_path" || test "$$build_path" = "$$metis_path"; then \
		echo "refusing unsafe PVC_BUILD clean target: $$build_path" >&2; exit 2; \
	fi; \
	case "$$pvc_path/" in "$$build_path"/*) \
		echo "refusing PVC_BUILD ancestor of PVC_DIR: $$build_path" >&2; exit 2;; \
	esac; \
	case "$$metis_path/" in "$$build_path"/*) \
		echo "refusing PVC_BUILD ancestor of METIS_ROOT: $$build_path" >&2; exit 2;; \
	esac; \
	if test ! -e "$$build_path"; then exit 0; fi; \
	if test ! -f "$$build_path/.pvc-build-dir"; then \
		echo "refusing unmarked PVC_BUILD clean target: $$build_path" >&2; exit 2; \
	fi; \
	rm -rf -- "$$build_path"
