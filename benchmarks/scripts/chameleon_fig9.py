"""Fig9 configuration and arithmetic, independent of VM lifecycle operations.

The measured Chameleon bar uses the three saved high configurations unchanged.
The plot's 50%/75% entries are pre-measured baseline branches, not VM settings.
"""
import ast
import copy
import importlib.util
import json
import math
from pathlib import Path

MIXES = {
    "mix1": ["memcached", "graphchi", "xsbench"],
    "mix2": ["memcached", "graphchi", "spark-kmeans"],
    "mix3": ["memcached", "graph500", "liblinear"],
    "mix4": ["cassandra", "graph500", "xsbench"],
}
BASELINE_SYSTEMS = ("HyperAlloc", "HyperAlloc+Memtis", "Static")
HERE = Path(__file__).resolve().parent
DEFAULT_QUALIFIED = HERE.parent / "config/chameleon-qualified-curves.json"
KNOBS = ("vm_memory_mib", "minimum_local_mib", "psi_ppm", "epoch_us",
         "cold_folios", "sample_period", "cooling_samples", "hhh_interval_ms",
         "free_pages", "pre_reclaim_headroom_mib", "pre_reclaim_epoch_us")


def _finite(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Missing/nonfinite {label}: {value!r}")
    if positive and value <= 0:
        raise ValueError(f"Nonpositive {label}: {value!r}")
    return float(value)


def _absolute(path, parent):
    path = Path(path)
    return str((parent / path if not path.is_absolute() else path).resolve())


def _fixed_baseline(app, parent):
    local = app["all_local"]
    if local.get("report"):
        baseline = copy.deepcopy(local)
    else:
        observations = local.get("measurements", [])
        fixed = local.get("fixed_main_baseline_report")
        choices = [r for r in observations if r.get("report") == fixed] if fixed else observations[:1]
        if len(choices) != 1:
            raise ValueError("Missing/ambiguous fixed all-local observation")
        baseline = copy.deepcopy(choices[0])
    baseline["report"] = _absolute(baseline["report"], parent)
    _finite(baseline["performance"].get("cost"), "all-local cost", positive=True)
    tracking = baseline.get("tracking_profile") or {
        "sampling": local.get("sample_period"),
        "cooling": local.get("cooling_samples"),
        "hhh_interval_ms": local.get("hhh_interval_ms"),
    }
    for key in ("sampling", "cooling", "hhh_interval_ms"):
        _finite(tracking.get(key), "all-local " + key, positive=True)
    baseline["tracking_profile"] = tracking
    baseline["normalization_protocol"] = "fixed-saved-tracked-all-local-v1"
    # Keep the denominator identical across co-run repetitions; never average
    # the archived per-repeat all-local values.
    return baseline


def resolve_high(case, qualified_path=DEFAULT_QUALIFIED):
    """Return the saved high knobs and fixed tracked all-local denominator.

    User-selected files take precedence over accepted entries. The returned
    observations are historical single-VM evidence, never Fig9 measurements.
    """
    qualified_path = Path(qualified_path).resolve()
    index = json.loads(qualified_path.read_text())
    if index.get("schema") == "chameleon-ae-frozen-high-v1":
        high = copy.deepcopy(index["applications"][case])
        if high.get("application") != case:
            raise ValueError("Frozen high application mismatch")
        for key in KNOBS:
            value = _finite(high["configuration"].get(key), case + ": " + key)
            if value < 0:
                raise ValueError("Negative saved knob: " + key)
        if high["configuration"]["vm_memory_mib"] != high["vm_memory_mib"]:
            raise ValueError("Frozen high VM size mismatch")
        _finite(high["all_local"]["performance"].get("cost"), "all-local cost", positive=True)
        for key in ("sampling", "cooling", "hhh_interval_ms"):
            _finite(high["all_local"]["tracking_profile"].get(key), "all-local " + key, positive=True)
        return high
    selected = index.get("user_selected_configurations", {}).get(case)
    if selected:
        source = Path(_absolute(selected["configuration_file"], qualified_path.parent))
        app = json.loads(source.read_text())
        if app.get("application") != case:
            raise ValueError("Selected configuration application mismatch: " + case)
        selection = "user_selected"
    else:
        source = qualified_path
        app = index.get("applications", {}).get(case)
        if not app or app.get("status") != "ACCEPTED":
            raise ValueError("No accepted or user-selected configuration for " + case)
        selection = "accepted"
    highs = [p for p in app["points"] if p.get("role") == "high"]
    if len(highs) != 1:
        raise ValueError("Expected exactly one saved high point for " + case)
    high = copy.deepcopy(highs[0])
    knobs = high["configuration"]
    for key in KNOBS:
        value = _finite(knobs.get(key), case + ": " + key)
        if value < 0 or (key not in ("psi_ppm", "free_pages", "pre_reclaim_headroom_mib") and value == 0):
            raise ValueError("Invalid saved knob " + case + ": " + key)
    if knobs["vm_memory_mib"] != app["vm_memory_mib"]:
        raise ValueError("High point VM size differs from saved application VM")
    if knobs["minimum_local_mib"] > knobs["vm_memory_mib"]:
        raise ValueError("Local floor exceeds VM size")
    measurements = high.get("measurements") or [high.get("measurement")]
    if not measurements or any(not m or not m.get("report") for m in measurements):
        raise ValueError("High point has no original observation")
    for m in measurements:
        if m.get("status", "PASS") != "PASS":
            raise ValueError("Saved high contains failed observation")
        m["report"] = _absolute(m["report"], source.parent)
    observation_count = len({m["report"] for m in measurements})
    if observation_count != len(measurements):
        raise ValueError("Duplicate original high observations")
    baseline = _fixed_baseline(app, source.parent)
    source_search = app.get("source_search") or index.get("source_search")
    return {
        "application": case,
        "selection": selection,
        "selection_status": app["status"],
        "candidate_id": high.get("candidate_id", high.get("trial_id")),
        "vm_memory_mib": app["vm_memory_mib"],
        "remote_pool_mib": app.get("remote_pool_mib", index.get("remote_pool_mib")),
        "configuration": knobs,
        "cpu_affinity_profile": copy.deepcopy(app.get("cpu_affinity_profile")),
        "workload_configuration": copy.deepcopy(app["workload_configuration"]),
        "workload_identity": copy.deepcopy(app.get("workload_identity")),
        "all_local": baseline,
        "original_observation_count": observation_count,
        "original_measurements": measurements,
        "original_measurement_scope": "single-VM parameter-search evidence, not co-run observations",
        "replay_command": high.get("replay_command"),
        "provenance": {
            "qualified_config": str(qualified_path),
            "configuration_file": str(source),
            "source_search": _absolute(source_search, source.parent) if source_search else None,
            "all_local_report": baseline["report"],
            "original_reports": [m["report"] for m in measurements],
        },
    }


def reference_data(plot_path):
    """Read literal reference arrays without importing/executing the plot."""
    path = Path(plot_path).resolve()
    if path.suffix == '.json':
        reference = json.loads(path.read_text())
        if reference.get('kind') != 'pre-measured' or reference.get('figure') != 'fig9':
            raise ValueError('Expected packaged Figure 9 reference data')
        for mix in MIXES:
            for level in ('50%', '75%'):
                for system in BASELINE_SYSTEMS:
                    _finite(reference['mixes'][mix][level][system]['slowdown_percent'], 'reference slowdown')
        return reference
    wanted = {"DATA", "SYSTEMS", "ERROR_DATA"}
    literals = {}
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                if target.id in literals:
                    raise ValueError("Duplicate plot literal: " + target.id)
                literals[target.id] = ast.literal_eval(node.value)
    if literals.keys() != wanted:
        raise ValueError("Missing plot DATA, SYSTEMS or ERROR_DATA literal")
    systems = literals["SYSTEMS"]
    if not isinstance(systems, list) or len(set(systems)) != len(systems):
        raise ValueError("Invalid plot SYSTEMS")
    mixes = {}
    for slug in MIXES:
        label = "Mix " + slug[3:]
        branches = {}
        for load in ("50%", "75%"):
            values = literals["DATA"][label][load]
            errors = literals["ERROR_DATA"][label][load]
            if len(values) != len(systems) or len(errors) != len(systems):
                raise ValueError("Plot data/system length mismatch")
            branches[load] = {}
            for system in BASELINE_SYSTEMS:
                i = systems.index(system)
                error = _finite(errors[i], label + " " + system + " error")
                if error < 0:
                    raise ValueError("Negative plotting error")
                branches[load][system] = {
                    "slowdown_percent": _finite(values[i], label + " " + system),
                    "error_percent": error,
                    "kind": "pre-measured",
                    "error_kind": "legacy plotting-template error values; omitted from AE figures",
                }
        mixes[slug] = branches
    return {"source": str(path), "kind": "pre-measured", "mixes": mixes,
            "systems": list(BASELINE_SYSTEMS),
            "scope": "Author-provided pre-measured baseline values; 50%/75% are reference branches, not applied VM quotas"}


def performance_slowdown(performance, baseline):
    """Normalize existing per-app cost definitions, keeping P95 separate."""
    cost = _finite(performance.get("cost"), "application cost", positive=True)
    base = _finite(baseline.get("cost"), "all-local cost", positive=True)
    result = {"slowdown_percent": 100 * (cost / base - 1)}
    if performance.get("p95_us") is not None:
        p95 = _finite(performance["p95_us"], "application P95", positive=True)
        p95base = _finite(baseline.get("p95_us"), "all-local P95", positive=True)
        result["p95_slowdown_percent"] = 100 * (p95 / p95base - 1)
    return result


def evaluate_application(case, directory, high):
    """Extract a completed app's cost with the existing Fig7/8 definitions.

    directory is the per-application directory containing application/. Caller
    owns lifecycle/correctness validation and must attach its status to the row.
    """
    if high["application"] != case:
        raise ValueError("Application/configuration mismatch")
    spec = importlib.util.spec_from_file_location("fig9_metrics", HERE / "chameleon-tuning-metrics.py")
    metrics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(metrics)
    perf = metrics.performance(case, Path(directory))
    baseline = high["all_local"]["performance"]
    return {"application": case, "performance": perf,
            "baseline_performance": copy.deepcopy(baseline),
            "baseline_report": high["all_local"]["report"],
            **performance_slowdown(perf, baseline)}


def aggregate_mix(mix, rows):
    """Arithmetic mean of exactly three successful app slowdown percentages.

    Rows need application/case, status=PASS and either finite slowdown_percent
    or performance+baseline_performance. If costs are provided they determine
    slowdown; an inconsistent supplied percentage is rejected. P95 is retained
    per service and never mixed into the Fig9 throughput/runtime bar.
    """
    if mix not in MIXES:
        raise ValueError("Unsupported mix: " + mix)
    rows = list(rows)
    cases = [r.get("application", r.get("case")) for r in rows]
    if len(rows) != 3 or len(set(cases)) != 3 or set(cases) != set(MIXES[mix]):
        raise ValueError("Mix needs exactly its three distinct applications")
    result = []
    for case in MIXES[mix]:
        row = copy.deepcopy(rows[cases.index(case)])
        if row.get("status") != "PASS":
            raise ValueError("Incomplete/failed application: " + case)
        if "performance" in row or "baseline_performance" in row:
            computed = performance_slowdown(row.get("performance") or {}, row.get("baseline_performance") or {})
            for key, value in computed.items():
                if row.get(key) is not None and not math.isclose(_finite(row[key], key), value, rel_tol=1e-9, abs_tol=1e-8):
                    raise ValueError("Inconsistent " + key + " for " + case)
                row[key] = value
        _finite(row.get("slowdown_percent"), case + " slowdown")
        if row.get("p95_slowdown_percent") is not None:
            _finite(row["p95_slowdown_percent"], case + " P95 slowdown")
        row["application"] = case
        result.append(row)
    mean = math.fsum(r["slowdown_percent"] / 3 for r in result)
    _finite(mean, "mix mean slowdown")
    return {"mix": mix, "status": "PASS", "applications": result,
            "slowdown_percent": mean,
            "aggregation": "arithmetic mean of three per-application slowdown percentages",
            "p95_slowdown_by_application": {r["application"]: r["p95_slowdown_percent"] for r in result
                                             if r.get("p95_slowdown_percent") is not None}}
