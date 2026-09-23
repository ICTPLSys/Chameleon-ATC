#!/usr/bin/env python3
"""Plot measured three-VM Mix1--4 slowdown and the supplied fixed references.

The reference level selects literal plotting data only. It does not change the
measured high configurations. Reference placeholder error bars are not plotted.
"""
import argparse
import copy
import json
import math
import statistics
from pathlib import Path

from chameleon_fig9 import BASELINE_SYSTEMS, MIXES, aggregate_mix

COLORS = {"Chameleon": "#5B7FA6", "HyperAlloc": "#B86A6A",
          "HyperAlloc+Memtis": "#6F9E72", "Static": "#E3A64D"}


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def plot_data(report, baseline_level="75"):
    """Preserve failed/incomplete groups as NA, not as zero or successful subsets."""
    level = str(baseline_level).rstrip("%") + "%"
    if level not in ("50%", "75%"):
        raise ValueError("Baseline level must be 50 or 75")
    reference = report["reference"]
    if reference.get("kind") != "pre-measured":
        raise ValueError("Expected pre-measured baselines from the supplied plotting script")
    result = {
        "schema_version": 1,
        "source_status": report.get("status"),
        "baseline_level": level,
        "reference": copy.deepcopy(reference),
        "reference_semantics": "Fixed pre-measured baseline branch; no change to measured VM settings",
        "baseline_errorbars": "omitted: legacy template ERROR_DATA are not used in AE figures",
        "chameleon_aggregation": "Arithmetic mean of three application slowdowns per repetition, then mean over repetitions",
        "chameleon_errorbars": "sample standard deviation across completed mix repetitions (n>=2); none for n=1",
        "configuration": copy.deepcopy(report.get("configuration")),
        "mixes": {},
    }
    for mix in MIXES:
        entry = report.get("mixes", {}).get(mix, {})
        reference_rows = copy.deepcopy(reference["mixes"][mix][level])
        for system in BASELINE_SYSTEMS:
            if not _number(reference_rows[system].get("slowdown_percent")):
                raise ValueError("Missing/nonfinite reference: " + mix + "/" + system)
        chameleon = {"status": "NA", "slowdown_percent": None, "sample_sd_percent": None,
                     "n": 0, "repetition_values": [], "source_status": entry.get("status", "MISSING")}
        repeats = entry.get("repetitions") or []
        try:
            if entry.get("status") != "PASS" or not repeats:
                raise ValueError("Mix is missing, incomplete, or failed")
            values = []
            for repetition in repeats:
                if repetition.get("status") != "PASS":
                    raise ValueError("Mix includes an incomplete/failed repetition")
                supplied = repetition.get("slowdown_percent")
                if not _number(supplied):
                    raise ValueError("Missing/nonfinite repetition slowdown")
                computed = aggregate_mix(mix, repetition.get("applications", []))["slowdown_percent"]
                if not math.isclose(computed, supplied, rel_tol=1e-9, abs_tol=1e-8):
                    raise ValueError("Repetition slowdown does not equal its three-application mean")
                values.append(float(supplied))
            chameleon.update(status="PASS", slowdown_percent=statistics.mean(values), n=len(values),
                             sample_sd_percent=statistics.stdev(values) if len(values) >= 2 else None,
                             repetition_values=values)
        except (ValueError, KeyError, TypeError) as error:
            chameleon["reason"] = str(error)
        result["mixes"][mix] = {"Chameleon": chameleon, "baselines": reference_rows,
                                "application_names": MIXES[mix],
                                "repetitions": copy.deepcopy(repeats)}
    return result


def render(data, directory, errorbars="sd"):
    """Render standalone vectors; source colors match the supplied Fig9 script."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from matplotlib.ticker import MaxNLocator

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    style = {
        "text.usetex": False,
        "font.family": "serif", "font.sans-serif": ["Times New Roman"],
        "font.size": 10, "axes.labelsize": 10,
        "xtick.labelsize": 9, "ytick.labelsize": 9, "legend.fontsize": 9,
        "savefig.bbox": None, "savefig.transparent": True,
        "figure.facecolor": "none", "axes.facecolor": "none",
        "axes.linewidth": .5, "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "pdf.fonttype": 42, "ps.fonttype": 42,
    }
    systems = ["Chameleon", *BASELINE_SYSTEMS]
    with plt.rc_context(style):
        fig, ax = plt.subplots(figsize=(7, 7 / 3))
        width = .18
        extents = [0.0]
        for i, mix in enumerate(MIXES):
            for j, system in enumerate(systems):
                x = i + (j - 1.5) * width
                item = data["mixes"][mix]["Chameleon"] if system == "Chameleon" else data["mixes"][mix]["baselines"][system]
                value = item["slowdown_percent"]
                if value is None:
                    ax.text(x, 0, "NA", ha="center", va="bottom", fontsize=8, color="#555555")
                    continue
                sd = item.get("sample_sd_percent") if system == "Chameleon" and errorbars == "sd" else None
                ax.bar(x, value, width=width, color=COLORS[system], edgecolor="#303030",
                       linewidth=.45, yerr=sd, capsize=2 if sd is not None else 0,
                       error_kw={"elinewidth": .6, "capthick": .6})
                extents.extend((value - (sd or 0), value + (sd or 0)))
                ax.annotate(f"{value:.1f}", (x, value + ((sd or 0) if value >= 0 else -(sd or 0))),
                            xytext=(0, 3 if value >= 0 else -3), textcoords="offset points",
                            ha="center", va="bottom" if value >= 0 else "top", fontsize=8)
        low, high = min(extents), max(extents)
        span = max(high - low, 1)
        ax.set_ylim(low - (.12 * span if low < 0 else 0), high + .28 * span)
        ax.axhline(0, color="#555555", linewidth=.5)
        ax.set_xticks(range(len(MIXES)), ["Mix " + mix[3:] for mix in MIXES])
        ax.set_ylabel("Slowdown (%)")
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
        ax.grid(axis="y", linestyle="--", linewidth=.4, alpha=.4)
        ax.set_axisbelow(True)
        handles = [Patch(facecolor=COLORS[s], edgecolor="#303030", linewidth=.45,
                         label=s) for s in systems]
        ax.legend(handles=handles, ncol=4, loc="upper left", columnspacing=1, handlelength=1.2,
                  handletextpad=.4, borderaxespad=.35)
        fig.subplots_adjust(left=.085, right=.99, bottom=.18, top=.98)
        # Canvas remains exactly seven inches wide; transparent vectors support
        # embedding without a raster background or tight-bbox dimension changes.
        for extension in ("svg", "pdf"):
            fig.savefig(directory / ("fig9-measured." + extension), bbox_inches=None, transparent=True)
        plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="Contains report.json and receives outputs")
    parser.add_argument("--baseline-level", choices=("50", "75"), default="75",
                        help="Select fixed reference numbers only; default75")
    parser.add_argument("--errorbars", choices=("sd", "none"), default="sd")
    args = parser.parse_args(argv)
    report_path = args.directory / "report.json"
    data = plot_data(json.loads(report_path.read_text()), args.baseline_level)
    data["source_report"] = str(report_path.resolve())
    data["displayed_chameleon_errorbars"] = args.errorbars
    render(data, args.directory, args.errorbars)
    (args.directory / "plot-data.json").write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    print("Wrote fig9-measured.svg, fig9-measured.pdf, and plot-data.json to " + str(args.directory))


if __name__ == "__main__":
    main()
