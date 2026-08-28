"""Reproducible latency benchmark for the v5.1.1 read/write paths.

Not collected by unittest discovery: run it directly.

    <verified-python> tests/benchmark_v511.py --store <path-to-store-root>
    <verified-python> tests/benchmark_v511.py --store <path> --grow 200

The store at --store is never touched: the benchmark copies it into a
disposable directory and measures there. --grow N first enlarges the copy by
driving the engine itself (N extra runs, each with a journal line and one
accepted proposal), which keeps every hash chain valid — synthesizing files
by hand would only benchmark a broken store. Growing is slow by design;
expect minutes for hundreds of runs.

Reported per operation: p50 and p95 wall-clock over --samples CLI
invocations (interpreter startup included, matching what an agent pays).
The cold first operation that seeds the checkpoint is reported separately.
"""

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"

# Canonical release ceilings (--gate default). Deliberately generous: they
# catch an order-of-magnitude regression (the class this release fixed) on
# ordinary shared hardware, not millisecond noise.
CANONICAL_GATES = {
    "cold_first_read_ms": 90000.0,
    "warm_get_p50_ms": 2000.0,
    "start_p50_ms": 2500.0,
    "journal_p50_ms": 2500.0,
    "propose_p50_ms": 6000.0,
    "sync_p50_ms": 6000.0,
    "close_p50_ms": 6000.0,
}


def cli(root, *arguments, check=True):
    result = subprocess.run(
        [sys.executable, str(ENGINE), "--root", str(root), *map(str, arguments)],
        text=True,
        capture_output=True,
        timeout=600,
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"benchmark command failed: {arguments}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def timed(root, *arguments, check=True):
    began = time.perf_counter()
    cli(root, *arguments, check=check)
    return (time.perf_counter() - began) * 1000.0


RESULTS = {}


def summarize(label, samples, metric=None):
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95 = ordered[max(0, int(round(0.95 * len(ordered))) - 1)]
    print(f"{label:<28} p50 {p50:8.1f} ms   p95 {p95:8.1f} ms   n={len(ordered)}")
    if metric:
        RESULTS[f"{metric}_p50_ms"] = p50
        RESULTS[f"{metric}_p95_ms"] = p95


def apply_gates(specification):
    """Fail (exit 1) when any metric breaches its stated ceiling.

    Specification: "default" for the committed CANONICAL_GATES, or
    comma-separated metric=max_ms pairs, for example
    --gate "warm_get_p50_ms=1000,start_p50_ms=2000,propose_p50_ms=3000".
    Known metrics: cold_first_read_ms plus <op>_p50_ms/<op>_p95_ms for
    warm_get, start, journal, propose, sync, close. A non-finite measured
    value or ceiling is a breach, never a bypass.
    """
    if specification.strip() == "default":
        gates = dict(CANONICAL_GATES)
    else:
        gates = {}
        breaches = []
        for pair in filter(
            None, (part.strip() for part in specification.split(","))
        ):
            metric, _, ceiling = pair.partition("=")
            try:
                gates[metric.strip()] = float(ceiling)
            except ValueError:
                breaches.append(f"invalid gate ceiling: {pair}")
        if breaches:
            for breach in breaches:
                print(f"GATE FAILED: {breach}")
            raise SystemExit(1)
    breaches = []
    for metric, maximum in gates.items():
        if metric not in RESULTS:
            breaches.append(f"unknown gate metric: {metric}")
            continue
        value = RESULTS[metric]
        if not math.isfinite(maximum) or not math.isfinite(value):
            breaches.append(f"{metric} is not a finite measurement")
            continue
        if not value <= maximum:
            breaches.append(
                f"{metric} {value:.1f} ms exceeds {maximum:.1f} ms"
            )
    if breaches:
        for breach in breaches:
            print(f"GATE FAILED: {breach}")
        raise SystemExit(1)
    print("all gates passed")


def pick_hot_key(root):
    state = json.loads(
        (Path(root) / ".bimri" / "state.json").read_text("utf-8")
    )
    hot = (Path(root) / "bimri.md").read_text("utf-8")
    for line in hot.splitlines():
        if "[K:" in line:
            return line.split("[K:", 1)[1].split("]", 1)[0]
    cold = state.get("cold_current") or {}
    if cold:
        return sorted(cold)[0]
    raise SystemExit("store has no current keys to benchmark")


def start_run(root, actor):
    result = cli(root, "start", "--actor", actor)
    for token in result.stdout.split():
        if token.startswith("R") and token[1:7].isdigit():
            return token[:7]
    raise SystemExit(f"could not parse run handle from: {result.stdout!r}")


def grow(root, runs):
    for index in range(runs):
        run_id = start_run(root, f"bench-grow-{index:05d}")
        cli(
            root, "journal", "--run", run_id, "--importance", "3",
            "--text", f"Benchmark growth journal {index}.",
        )
        cli(
            root, "propose", "--run", run_id, "--operation", "set",
            "--tier", "2", "--key", f"bench.grow.{index:05d}",
            "--text", f"Benchmark growth subject {index}.",
            "--source", "agent", "--trust", "working", "--new-subject",
        )
        cli(root, "sync", "--run", run_id)
        cli(
            root, "close", "--run", run_id, "--outcome", "success",
            "--summary", "benchmark growth run",
        )
        if index and index % 25 == 0:
            print(f"  grew {index}/{runs} runs", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store", default="",
        help="store root to copy (omit with --init for a fresh store)",
    )
    parser.add_argument(
        "--init", action="store_true",
        help="build a small fresh store instead of copying --store",
    )
    parser.add_argument("--samples", type=int, default=9)
    parser.add_argument(
        "--grow", type=int, default=0,
        help="extra engine-driven runs to add to the copy before measuring",
    )
    parser.add_argument(
        "--gate", default="",
        help="comma-separated metric=max_ms ceilings; breach exits 1",
    )
    arguments = parser.parse_args()

    workspace = Path(tempfile.mkdtemp(prefix="bimri-bench-"))
    root = workspace / "store"
    root.mkdir()
    if arguments.init:
        print("building a fresh benchmark store...")
        grow(root, 3)
    else:
        if not arguments.store:
            raise SystemExit("provide --store, or --init for a fresh store")
        source = Path(arguments.store).resolve()
        if not (source / ".bimri").is_dir():
            raise SystemExit(f"{source} does not contain a .bimri store")
        if (source / "bimri.md").exists():
            shutil.copy2(source / "bimri.md", root / "bimri.md")
        shutil.copytree(source / ".bimri", root / ".bimri")
    print(f"benchmark copy: {root}")

    if arguments.grow:
        print(f"growing the copy by {arguments.grow} engine-driven runs...")
        grow(root, arguments.grow)

    (root / ".bimri" / "audit-witness.json").unlink(missing_ok=True)
    key = pick_hot_key(root)
    print(f"benchmark key: {key}")

    cold = timed(root, "get", "--key", key, check=False)
    print(f"{'cold first read (seeds)':<28} once {cold:8.1f} ms")
    RESULTS["cold_first_read_ms"] = cold

    reads = [
        timed(root, "get", "--key", key)
        for _ in range(arguments.samples)
    ]
    summarize("warm get --key", reads, metric="warm_get")

    starts, journals, proposes, syncs, closes = [], [], [], [], []
    for index in range(arguments.samples):
        began = time.perf_counter()
        run_id = start_run(root, f"bench-{index:03d}")
        starts.append((time.perf_counter() - began) * 1000.0)
        journals.append(timed(
            root, "journal", "--run", run_id, "--importance", "3",
            "--text", f"benchmark journal {index}",
        ))
        proposes.append(timed(
            root, "propose", "--run", run_id, "--operation", "set",
            "--tier", "2", "--key", f"bench.probe.{index:03d}",
            "--text", f"benchmark subject {index}",
            "--source", "agent", "--trust", "working", "--new-subject",
        ))
        syncs.append(timed(root, "sync", "--run", run_id))
        closes.append(timed(
            root, "close", "--run", run_id, "--outcome", "success",
            "--summary", "benchmark run",
        ))
    summarize("warm start", starts, metric="start")
    summarize("journal", journals, metric="journal")
    summarize("propose", proposes, metric="propose")
    summarize("sync", syncs, metric="sync")
    summarize("close (authority)", closes, metric="close")
    print(f"disposable copy retained at {workspace}")
    if arguments.gate:
        apply_gates(arguments.gate)


if __name__ == "__main__":
    main()
