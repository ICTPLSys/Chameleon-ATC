#!/usr/bin/env python3
"""End-to-end tests for the Metis Page View Count benchmark and data tools."""

import argparse
import collections
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile


HEADER = struct.Struct("<8sIIIIQQQQQ")
RECORD = struct.Struct("<QQQ")
MAGIC = b"PVCBIN1\0"
VERSION = 1
ENDIAN_MARKER = 0x01020304
ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
BENCHMARKS = ROOT.parent.parent
RUNNER = BENCHMARKS / "scripts" / "run-pvc.sh"


class TestFailure(RuntimeError):
    pass


def check(condition, message):
    if not condition:
        raise TestFailure(message)


def find_binary(explicit, name):
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend((ROOT / "build" / name, ROOT / name))
    for candidate in candidates:
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return candidate.resolve()
    raise TestFailure(
        "cannot find executable {} (tried {})".format(
            name, ", ".join(str(path) for path in candidates)
        )
    )


def metis_thread_counts():
    """Return useful thread counts that Metis can pin inside this cpuset."""
    if hasattr(os, "sched_getaffinity"):
        allowed = os.sched_getaffinity(0)
        contiguous = 0
        while contiguous in allowed:
            contiguous += 1
        check(
            contiguous > 0,
            "Metis pins logical worker 0 to CPU 0, but CPU 0 is absent from "
            "this process's affinity mask ({})".format(
                ",".join(str(cpu) for cpu in sorted(allowed))
            ),
        )
        upper = min(4, contiguous)
    else:
        # Metis requires CPU 0.  On platforms without sched_getaffinity, use
        # the least demanding configuration instead of guessing the cpuset.
        upper = 1
    return [1] if upper == 1 else [1, upper]


def run(command, *, input_bytes=None, expect_success=True):
    completed = subprocess.run(
        [str(item) for item in command],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if expect_success and completed.returncode != 0:
        raise TestFailure(
            "command failed ({}):\nstdout:\n{}\nstderr:\n{}".format(
                " ".join(str(item) for item in command),
                completed.stdout.decode("utf-8", "replace"),
                completed.stderr.decode("utf-8", "replace"),
            )
        )
    if not expect_success and completed.returncode == 0:
        raise TestFailure(
            "command unexpectedly succeeded: {}\nstdout:\n{}\nstderr:\n{}".format(
                " ".join(str(item) for item in command),
                completed.stdout.decode("utf-8", "replace"),
                completed.stderr.decode("utf-8", "replace"),
            )
        )
    return completed


def load_tsv(path):
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        check(len(fields) == 3, "{}:{} must have three fields".format(path, line_number))
        rows.append(tuple(int(field) for field in fields))
    return rows


def load_expected(path):
    result = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        check(len(fields) == 2, "{}:{} must have two fields".format(path, line_number))
        result[int(fields[0])] = int(fields[1])
    return result


def write_pvc(path, records, *, seed=0):
    records = list(records)
    url_count = max((row[0] for row in records), default=-1) + 1
    ip_count = max((row[1] for row in records), default=-1) + 1
    cookie_count = max((row[2] for row in records), default=-1) + 1
    with path.open("wb") as output:
        output.write(
            HEADER.pack(
                MAGIC,
                VERSION,
                HEADER.size,
                RECORD.size,
                ENDIAN_MARKER,
                len(records),
                url_count,
                ip_count,
                cookie_count,
                seed,
            )
        )
        for row in records:
            output.write(RECORD.pack(*row))


def read_pvc(path):
    data = path.read_bytes()
    check(len(data) >= HEADER.size, "{} is smaller than a PVC header".format(path))
    fields = HEADER.unpack_from(data)
    header = {
        "magic": fields[0],
        "version": fields[1],
        "header_bytes": fields[2],
        "record_bytes": fields[3],
        "endian_marker": fields[4],
        "record_count": fields[5],
        "url_count": fields[6],
        "ip_count": fields[7],
        "cookie_count": fields[8],
        "seed": fields[9],
    }
    check(header["magic"] == MAGIC, "bad PVC magic in {}".format(path))
    check(header["version"] == VERSION, "bad PVC version in {}".format(path))
    check(header["header_bytes"] == HEADER.size, "bad header size in {}".format(path))
    check(header["record_bytes"] == RECORD.size, "bad record size in {}".format(path))
    check(header["endian_marker"] == ENDIAN_MARKER, "bad endian marker in {}".format(path))
    expected_size = HEADER.size + header["record_count"] * RECORD.size
    check(len(data) == expected_size, "PVC size/count mismatch in {}".format(path))
    records = [
        RECORD.unpack_from(data, HEADER.size + index * RECORD.size)
        for index in range(header["record_count"])
    ]
    return header, records


def read_result_rows(path):
    rows = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split()
        check(len(fields) == 2, "{}:{} must have two columns".format(path, line_number))
        url, count = (int(field) for field in fields)
        check(url not in seen, "duplicate URL {} in {}".format(url, path))
        seen.add(url)
        rows.append((url, count))
    check(
        [row[0] for row in rows] == sorted(row[0] for row in rows),
        "PVC output is not sorted by URL in {}".format(path),
    )
    return rows


def read_result(path):
    return dict(read_result_rows(path))


def read_metrics(completed):
    text = completed.stdout.decode("utf-8", "replace")
    phases = re.findall(r"(?m)^phase=([^\r\n]+)$", text)
    check(
        phases[:3] == ["stage1", "interstage", "stage2"],
        "PVC phases are missing or out of order in:\n{}".format(text),
    )
    metrics = {}
    for name in ("stage1_unique", "stage2_urls", "total_views", "checksum"):
        match = re.search(r"(?m)^{}=([0-9]+)$".format(name), text)
        check(match is not None, "missing {} metric in:\n{}".format(name, text))
        metrics[name] = int(match.group(1))
    for name in ("stage1_ms", "interstage_ms", "stage2_ms", "total_ms"):
        match = re.search(
            r"(?m)^{}=([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)$".format(name),
            text,
        )
        check(match is not None, "missing {} metric in:\n{}".format(name, text))
        metrics[name] = float(match.group(1))
        check(
            math.isfinite(metrics[name]) and metrics[name] >= 0.0,
            "{} must be a finite non-negative duration".format(name),
        )
    return metrics


def run_pvc(binary, input_path, output_path, threads, tasks=(4, 4, 4)):
    command = [binary, "-p", str(threads)]
    if tasks is not None:
        command.extend(
            [
                "-m",
                str(tasks[0]),
                "-r",
                str(tasks[1]),
                "-g",
                str(tasks[2]),
            ]
        )
    command.extend(["-q", "-o", output_path, input_path])
    completed = run(command)
    return read_metrics(completed), read_result(output_path)


def test_tiny_application(pvc_binary, work, thread_counts):
    records = load_tsv(TESTS / "tiny.tsv")
    expected = load_expected(TESTS / "expected.tsv")
    input_path = work / "tiny.pvc"
    write_pvc(input_path, records, seed=7)

    first_threads = thread_counts[0]
    last_threads = thread_counts[-1]
    metrics_first, result_first = run_pvc(
        pvc_binary, input_path, work / "tiny-first.tsv", first_threads
    )
    metrics_last, result_last = run_pvc(
        pvc_binary, input_path, work / "tiny-last.tsv", last_threads
    )
    check(result_first == expected, "first PVC result differs from expected.tsv")
    check(result_last == expected, "last PVC result differs from expected.tsv")
    for name in ("stage1_unique", "stage2_urls", "total_views", "checksum"):
        check(
            metrics_first[name] == metrics_last[name],
            "PVC {} differs across thread counts".format(name),
        )
    check(metrics_first["stage1_unique"] == 4, "Stage 1 did not remove exact triples")
    check(metrics_first["stage2_urls"] == 2, "unexpected URL count")
    check(metrics_first["total_views"] == 4, "unexpected total page-view count")

    truncated = work / "truncated.pvc"
    truncated.write_bytes(input_path.read_bytes()[:-1])
    failed = run([pvc_binary, "-q", truncated], expect_success=False)
    check(
        b"size does not match" in failed.stderr,
        "truncated input did not report the expected size error",
    )


def test_application_edge_configs(pvc_binary, work, threads):
    records = [
        (99, 7, 700),
        (1, 2, 200),
        (50, 3, 300),
        (1, 2, 201),
        (99, 7, 700),
        (3, 5, 500),
    ]
    expected_rows = [(1, 2), (3, 1), (50, 1), (99, 1)]
    input_path = work / "configs.pvc"
    write_pvc(input_path, records)

    configurations = [
        ("tasks-above-record-count", (64, 64, 64)),
        ("explicit-zero-tasks", (0, 0, 0)),
        ("omitted-default-tasks", None),
    ]
    reference_metrics = None
    for name, tasks in configurations:
        output_path = work / (name + ".tsv")
        metrics, result = run_pvc(
            pvc_binary, input_path, output_path, threads, tasks=tasks
        )
        check(
            read_result_rows(output_path) == expected_rows,
            "{} produced incorrectly ordered URL counts".format(name),
        )
        check(result == dict(expected_rows), "{} produced wrong counts".format(name))
        if reference_metrics is None:
            reference_metrics = metrics
        else:
            for metric in ("stage1_unique", "stage2_urls", "total_views", "checksum"):
                check(
                    metrics[metric] == reference_metrics[metric],
                    "{} differs for {}".format(metric, name),
                )

    empty_input = work / "empty.pvc"
    empty_output = work / "empty.tsv"
    write_pvc(empty_input, [])
    empty_metrics, empty_result = run_pvc(
        pvc_binary,
        empty_input,
        empty_output,
        threads,
        tasks=(64, 64, 64),
    )
    check(empty_result == {}, "zero-record input produced result rows")
    check(empty_output.read_bytes() == b"", "zero-record output is not empty")
    check(empty_metrics["stage1_unique"] == 0, "zero-record Stage 1 count is wrong")
    check(empty_metrics["stage2_urls"] == 0, "zero-record URL count is wrong")
    check(empty_metrics["total_views"] == 0, "zero-record total count is wrong")
    check(
        empty_metrics["checksum"] == 14695981039346656037,
        "zero-record checksum is wrong",
    )


def test_generator(generator, work):
    common = [
        "--records",
        "256",
        "--urls",
        "10007",
        "--ips",
        "10009",
        "--cookies",
        "10037",
        "--duplicate-rate",
        "0.25",
        "--distribution",
        "uniform",
    ]
    first = work / "generated-a.pvc"
    second = work / "generated-b.pvc"
    third = work / "generated-c.pvc"
    completed = run([generator, "--output", first, "--seed", "42"] + common)
    run([generator, "--output", second, "--seed", "42"] + common)
    run([generator, "--output", third, "--seed", "43"] + common)
    check(first.read_bytes() == second.read_bytes(), "same seed is not byte deterministic")
    check(first.read_bytes() != third.read_bytes(), "different seeds produced identical files")
    check(
        b"intentional_duplicates=64" in completed.stderr,
        "generator did not report the requested duplicate count",
    )
    header, records = read_pvc(first)
    check(header["record_count"] == 256, "generator record count is wrong")
    check(header["url_count"] == 10007, "generator URL domain is wrong")
    check(header["ip_count"] == 10009, "generator IP domain is wrong")
    check(header["cookie_count"] == 10037, "generator cookie domain is wrong")
    check(header["seed"] == 42, "generator seed metadata is wrong")
    check(all(0 <= row[0] < 10007 for row in records), "generated URL out of range")
    check(all(0 <= row[1] < 10009 for row in records), "generated IP out of range")
    check(all(0 <= row[2] < 10037 for row in records), "generated cookie out of range")
    check(
        len(records) - len(set(records)) == 64,
        "generator did not create the exact intentional duplicate count",
    )

    sized = work / "generated-size.pvc"
    run([generator, "--output", sized, "--bytes", "1KiB", "--seed", "1"])
    sized_header, _ = read_pvc(sized)
    check(sized_header["record_count"] == 1024 // RECORD.size, "--bytes rounding is wrong")
    check(
        sized.stat().st_size == HEADER.size + (1024 // RECORD.size) * RECORD.size,
        "--bytes produced an unexpected file size",
    )

    zipf = work / "generated-zipf.pvc"
    run(
        [
            generator,
            "--output",
            zipf,
            "--records",
            "4096",
            "--urls",
            "128",
            "--ips",
            "257",
            "--cookies",
            "521",
            "--duplicate-rate",
            "0",
            "--distribution",
            "zipf",
            "--zipf-theta",
            "1.2",
            "--seed",
            "99",
        ]
    )
    _, zipf_records = read_pvc(zipf)
    histogram = collections.Counter(record[0] for record in zipf_records)
    check(all(0 <= record[0] < 128 for record in zipf_records), "Zipf URL out of range")
    check(
        sum(histogram[index] for index in range(16))
        > sum(histogram[index] for index in range(112, 128)),
        "Zipf generator did not concentrate records on low-rank URLs",
    )

    run(
        [generator, "--output", work / "bad-rate.pvc", "--records", "10",
         "--duplicate-rate", "1.1"],
        expect_success=False,
    )

    preserved = work / "generator-preserved.pvc"
    preserved.write_bytes(b"existing-output-must-survive")
    collision = run(
        [
            "/bin/sh",
            "-c",
            'printf "foreign-temporary" >"$1.tmp.$$"; '
            'exec "$2" --output "$1" --records 10',
            "generator-temporary-collision",
            preserved,
            generator,
        ],
        expect_success=False,
    )
    check(
        b"cannot create temporary output" in collision.stderr,
        "generator temporary collision did not report a creation error",
    )
    check(
        preserved.read_bytes() == b"existing-output-must-survive",
        "generator temporary collision damaged the existing output",
    )
    foreign_temporaries = list(work.glob(preserved.name + ".tmp.*"))
    check(
        len(foreign_temporaries) == 1
        and foreign_temporaries[0].read_bytes() == b"foreign-temporary",
        "generator removed or changed a temporary file that it did not create",
    )


def test_converter(converter, pvc_binary, work, threads):
    source = (TESTS / "konect-small.txt").read_bytes()
    first = work / "konect-a.pvc"
    second = work / "konect-b.pvc"
    other_seed = work / "konect-other-seed.pvc"
    completed = run(
        [converter, "--input", "-", "--output", first, "--seed", "42"],
        input_bytes=source,
    )
    run(
        [converter, "--input", TESTS / "konect-small.txt", "--output", second,
         "--seed", "42"]
    )
    run(
        [converter, "--input", "-", "--output", other_seed, "--seed", "43"],
        input_bytes=source,
    )
    check(b"proxy_records=6" in completed.stderr, "converter summary is missing")
    check(first.read_bytes() == second.read_bytes(), "file/stdin conversion differs")
    header, records = read_pvc(first)
    other_header, other_records = read_pvc(other_seed)
    check(header["record_count"] == 6, "converter record count is wrong")
    check(header["url_count"] == 21, "converter URL domain is wrong")
    check(header["ip_count"] == 10, "converter IP domain is wrong")
    check(
        header["cookie_count"] == 0,
        "hashed cookie domain must be marked non-compact/unknown",
    )
    check(header["seed"] == 42 and other_header["seed"] == 43, "converter seed metadata is wrong")
    check(
        [(row[0], row[1]) for row in records]
        == [(10, 1), (10, 1), (10, 2), (11, 1), (20, 9), (20, 9)],
        "converter did not map destination to URL and source to IP",
    )
    cookie_for_ip = {}
    for _, ip_id, cookie_id in records:
        check(
            ip_id not in cookie_for_ip or cookie_for_ip[ip_id] == cookie_id,
            "one source IP mapped to multiple cookies",
        )
        cookie_for_ip[ip_id] = cookie_id
    check(
        [row[2] for row in records] != [row[2] for row in other_records],
        "cookie mapping ignored --seed",
    )

    metrics, result = run_pvc(
        pvc_binary, first, work / "konect-result.tsv", threads
    )
    check(result == {10: 2, 11: 1, 20: 1}, "proxy PVC result is wrong")
    check(metrics["stage1_unique"] == 4, "proxy deduplication count is wrong")
    check(metrics["total_views"] == 4, "proxy total view count is wrong")

    preserved = work / "preserved.pvc"
    preserved.write_bytes(b"do-not-replace")
    run(
        [converter, "--input", "-", "--output", preserved],
        input_bytes=b"not-an-edge\n",
        expect_success=False,
    )
    check(preserved.read_bytes() == b"do-not-replace", "failed conversion replaced output")


def read_metadata(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    check(lines and lines[0] == "field\tvalue", "invalid metadata header in {}".format(path))
    result = {}
    for line_number, line in enumerate(lines[1:], 2):
        fields = line.split("\t", 1)
        check(len(fields) == 2, "{}:{} is not key/value metadata".format(path, line_number))
        check(fields[0] not in result, "duplicate metadata field {}".format(fields[0]))
        result[fields[0]] = fields[1]
    return result


def test_runner_smoke(pvc_binary, work, threads):
    check(RUNNER.is_file() and os.access(str(RUNNER), os.X_OK), "PVC runner is not executable")

    input_path = work / "runner-input.pvc"
    write_pvc(input_path, load_tsv(TESTS / "tiny.tsv"), seed=7)

    # The runner has a fixed overlay/build/page_view_count lookup.  A temporary
    # overlay lets this test exercise whichever binary --pvc selected without
    # modifying or rebuilding the source tree.
    overlay = work / "runner-overlay"
    (overlay / "build").mkdir(parents=True)
    (overlay / "metis-pvc.mk").symlink_to(ROOT / "metis-pvc.mk")
    (overlay / "build" / "page_view_count").symlink_to(pvc_binary)

    result_base = work / "runner-results"
    missing_metis = work / "deliberately-missing-metis"
    completed = run(
        [
            RUNNER,
            "--input",
            input_path,
            "--source-overlay",
            overlay,
            "--metis-source",
            missing_metis,
            "--output-base",
            result_base,
            "--threads",
            str(threads),
            "--map-tasks",
            "64",
            "--reduce-tasks",
            "64",
            "--group-tasks",
            "64",
            "--repetitions",
            "1",
            "--minimum-available-mib",
            "16",
            "--skip-build",
            "--allow-oversubscribe",
        ]
    )
    check(
        b"PVC completed successfully" in completed.stdout,
        "runner did not report successful completion",
    )

    result_dirs = [path for path in result_base.iterdir() if path.is_dir()]
    check(len(result_dirs) == 1, "runner did not create exactly one result directory")
    result_dir = result_dirs[0]
    metadata = read_metadata(result_dir / "metadata.tsv")
    expected_metadata = {
        "input": str(input_path.resolve()),
        "input_bytes": str(input_path.stat().st_size),
        "threads": str(threads),
        "map_tasks": "64",
        "reduce_tasks": "64",
        "group_tasks": "64",
        "repetitions": "1",
        "minimum_available_mib": "16",
        "skip_build": "1",
        "allow_oversubscribe": "1",
        "metis_source": str(missing_metis.resolve()),
        "metis_revision": "unknown",
        "full_counts_written": "no",
        "build_exit_status": "skipped",
        "build_log_exit_status": "skipped",
        "repetition_001_exit_status": "0",
        "repetition_001_command_exit_status": "0",
        "repetition_001_log_exit_status": "0",
    }
    for field, expected in expected_metadata.items():
        check(metadata.get(field) == expected, "runner metadata field {} is wrong".format(field))
    for field in ("input_sha256", "binary_sha256"):
        check(
            re.fullmatch(r"[0-9a-f]{64}", metadata.get(field, "")) is not None,
            "runner metadata is missing {}".format(field),
        )
    check(not (result_dir / "build.log").exists(), "--skip-build unexpectedly ran a build")

    required_root_files = [
        "cpuinfo.txt",
        "meminfo-before.txt",
        "meminfo-after.txt",
        "exit-status.tsv",
    ]
    for name in required_root_files:
        check((result_dir / name).is_file(), "runner result is missing {}".format(name))
    check(
        (result_dir / "exit-status.tsv").read_text(encoding="utf-8")
        == "repetition\texit_status\n1\t0\n",
        "runner exit-status summary is wrong",
    )

    repetition = result_dir / "repeat-001"
    for name in (
        "command.tsv",
        "stdout.log",
        "time.txt",
        "exit-status",
        "command-exit-status",
        "log-exit-status",
        "meminfo-before.txt",
        "meminfo-after.txt",
    ):
        check((repetition / name).is_file(), "runner repetition is missing {}".format(name))
    check(
        (repetition / "exit-status").read_text(encoding="utf-8") == "0\n",
        "runner repetition exit status is wrong",
    )
    check(
        (repetition / "command-exit-status").read_text(encoding="utf-8") == "0\n",
        "runner command exit status is wrong",
    )
    check(
        (repetition / "log-exit-status").read_text(encoding="utf-8") == "0\n",
        "runner log-writer exit status is wrong",
    )
    stdout_log = (repetition / "stdout.log").read_text(encoding="utf-8")
    for expected_line in (
        "phase=stage1",
        "phase=interstage",
        "phase=stage2",
        "stage1_unique=4",
        "stage2_urls=2",
        "total_views=4",
    ):
        check(expected_line in stdout_log.splitlines(), "runner stdout log lacks {}".format(expected_line))
    time_text = (repetition / "time.txt").read_text(encoding="utf-8")
    check("Maximum resident set size" in time_text, "runner time log lacks RSS")
    check("Exit status: 0" in time_text, "runner time log lacks successful exit status")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pvc", help="path to page_view_count")
    parser.add_argument("--generator", help="path to pvc_generate")
    parser.add_argument("--converter", help="path to konect_to_pvc")
    arguments = parser.parse_args()

    pvc_binary = find_binary(arguments.pvc, "page_view_count")
    generator = find_binary(arguments.generator, "pvc_generate")
    converter = find_binary(arguments.converter, "konect_to_pvc")
    thread_counts = metis_thread_counts()
    print(
        "INFO: Metis thread counts selected from current CPU affinity: {}".format(
            ", ".join(str(count) for count in thread_counts)
        )
    )

    tests = [
        ("tiny application semantics and malformed input", lambda work: test_tiny_application(pvc_binary, work, thread_counts)),
        ("application edge configurations and sorted output", lambda work: test_application_edge_configs(pvc_binary, work, thread_counts[-1])),
        ("deterministic uniform/Zipf generator", lambda work: test_generator(generator, work)),
        ("streaming KONECT proxy converter", lambda work: test_converter(converter, pvc_binary, work, thread_counts[-1])),
        ("single-repetition skip-build runner", lambda work: test_runner_smoke(pvc_binary, work, thread_counts[-1])),
    ]
    with tempfile.TemporaryDirectory(prefix="pvc-tests-") as temporary:
        base = Path(temporary)
        for index, (name, function) in enumerate(tests):
            work = base / str(index)
            work.mkdir()
            function(work)
            print("PASS: {}".format(name))
    print("PASS: all PVC tests")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TestFailure as error:
        print("FAIL: {}".format(error), file=sys.stderr)
        sys.exit(1)
