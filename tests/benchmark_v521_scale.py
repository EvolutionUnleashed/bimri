"""Compare independent copies, alternating engine order for each matched save.

Each --store is a synthetic or copied store path. Pass 'empty' for a fresh one.
The path supplied is only read. All mutations use two TemporaryDirectory copies.
The JSON report contains aggregate bytes and timings, never record contents.
"""
import argparse
import contextlib
import io
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def checked_engine(engine, root, *args, misses=False):
    result = subprocess.run(
        [sys.executable, str(engine), "--root", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=2400,
    )
    if result.returncode not in ((0, 1) if misses else (0,)):
        raise RuntimeError("{} exited {}: {}".format(args[0], result.returncode, result.stderr[-500:]))
    return result.stdout


def save_args(engine, root, index):
    key = "bench.matched.%06d" % index
    result = json.loads(checked_engine(engine, root, "get", "--key", key, "--json", misses=True))
    return ["save", "--request", "matched-%06d" % index, "--observed", result["token"],
            "--key", key, "--tier", "2", "--new-subject", "--text", "Matched scale benchmark.", "--json"]


def measured(engine, root, index):
    args = save_args(engine, root, index)
    start = time.perf_counter()
    result = json.loads(checked_engine(engine, root, *args).splitlines()[0])
    if result["result"] != "accepted":
        raise RuntimeError("benchmark save did not commit")
    return [round(1000 * (time.perf_counter() - start), 2), result["lock_held_ms"]]


def instrument(engine_path, root, index):
    """Count durable write payloads, not temporary copies or nested helpers.

    Read volume is the sum of distinct checkpoint file sizes opened for
    reading. The value is a footprint, not a count of bytes read after seeks.
    """
    args = save_args(engine_path, root, index)
    spec = importlib.util.spec_from_file_location("bimri_volume", engine_path)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    volume = {"checkpoint": 0, "state_and_transition": 0, "other": 0}
    checkpoint_reads = {}
    active = [True]
    location = root / ".bimri"
    def read_event(event, arguments):
        if event != "open" or not active[0]:
            return
        path, mode, flags = arguments
        if not isinstance(path, str) or flags & (os.O_WRONLY | os.O_RDWR):
            return
        name = Path(path)
        if name.parent == location / "audit-manifests" or name == location / "audit-manifest.json":
            try:
                checkpoint_reads[path] = name.stat().st_size
            except FileNotFoundError:
                pass
    sys.addaudithook(read_event)
    depth = [0]
    def wrap(name):
        fn = getattr(engine, name)
        def write(path, value, *pos, **kw):
            outer = depth[0] == 0
            depth[0] += 1
            try:
                result = fn(path, value, *pos, **kw)
            finally:
                depth[0] -= 1
            if outer:
                dest = Path(path)
                size = len(value.encode("utf-8")) if isinstance(value, str) else len(value)
                key = ("checkpoint" if dest.parent.name == "audit-manifests" or dest.name == "audit-manifest.json"
                       else "state_and_transition" if dest.name in ("state.json", "audit-transition.json") else "other")
                volume[key] += size
            return result
        setattr(engine, name, write)
    for name in ("atomic_write_text", "atomic_write_bytes", "exclusive_write_text", "exclusive_write_bytes", "append_line"):
        wrap(name)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = engine.main(["--root", str(root)] + args)
        if status != 0 or json.loads(out.getvalue().splitlines()[0])["result"] != "accepted":
            raise RuntimeError("instrumented save failed")
    finally:
        active[0] = False
    volume["all_written"] = sum(volume.values())
    volume["checkpoint_unique_read_footprint"] = sum(checkpoint_reads.values())
    return volume


def score(samples):
    wall, lock = zip(*samples)
    def quantile(values, q):
        return sorted(values)[math.ceil(q * len(values)) - 1]
    return {"wall_p50_ms": statistics.median(wall), "wall_p95_ms": quantile(wall, .95),
            "lock_p50_ms": statistics.median(lock), "lock_p95_ms": quantile(lock, .95)}


def run(args, fixture):
    engines = {"baseline": args.baseline.resolve(), "candidate": args.engine.resolve()}
    with tempfile.TemporaryDirectory(prefix="bimri-matched-scale-") as temporary:
        roots = {}
        for label, engine in engines.items():
            root = Path(temporary) / label
            root.mkdir()
            if fixture != "empty":
                source = Path(fixture).resolve()
                shutil.copy2(source / "bimri.md", root / "bimri.md")
                shutil.copytree(source / ".bimri", root / ".bimri",
                                ignore=shutil.ignore_patterns("engine.lock", "runtime.local.json", "hooks.claude.local.json"))
                (root / ".bimri" / "engine.lock").touch()
            else:
                checked_engine(engine, root, "start")
            checked_engine(engine, root, "doctor", "--live")
            for i in range(2):
                measured(engine, root, i)
            roots[label] = root
        results = {key: [] for key in engines}
        start_state = json.loads((roots["baseline"] / ".bimri/state.json").read_bytes())
        for i in range(2, 2 + args.samples):
            order = list(engines) if i % 2 == 0 else list(reversed(engines))
            for name in order:
                results[name].append(measured(engines[name], roots[name], i))
            print("pair", Path(fixture).name, i-1, *(results[k][-1][0] for k in engines),flush=True)
        volumes = {k: instrument(engines[k], roots[k], 2 + args.samples) for k in engines}
        diffs = [b[0]-c[0] for b,c in zip(results["baseline"], results["candidate"])]
        rng = random.Random(481)
        bootstrap = sorted(statistics.median(rng.choices(diffs, k=len(diffs))) for _ in range(4000))
        return {"fixture": Path(fixture).name, "run_count_after_warmup": start_state["run_count"],
                "revisions_after_warmup": start_state["head_revision"], "samples": results,
                "summary": {k: score(v) for k,v in results.items()}, "bytes": volumes,
                "paired_wall_median_saved_ms": statistics.median(diffs),
                "bootstrap_95pct_paired_median_saved_ms": [bootstrap[99], bootstrap[3899]]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--engine", type=Path, default=Path(__file__).resolve().parents[1] / "bimri-engine.py")
    parser.add_argument("--store", action="append", required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.samples < 2:
        parser.error("at least two samples are required")
    report = []
    for item in options.store:
        report.append(run(options,item))
        options.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(report[-1]["summary"], report[-1]["bytes"], flush=True)
