"""Exercise a 5.2 code-only upgrade twice on disposable copies of a v5 store."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


SOURCE = Path(__file__).resolve().parents[1]
ENGINE = SOURCE / "bimri-engine.py"


def protected_state(root):
    spec = importlib.util.spec_from_file_location("rehearsal_engine", ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.protected_store_manifest(module.Paths(root))
    return {key: result[key] for key in (
        "tree_digest", "protected_path_count", "bimri_md_sha256", "state_sha256",
        "accepted_head_revision", "accepted_head_hash",
    )}


def pending_legacy_work(root):
    """Snapshot each active legacy run, its log and the status of its proposals."""
    bdir = root / ".bimri"
    state = json.loads((bdir / "state.json").read_text(encoding="utf-8"))
    files = {}
    for rid in state["active_runs"]:
        paths = [bdir / "log" / (rid + ".md")]
        for proposal in (bdir / "proposals").glob(rid + "-Q*.json"):
            paths.extend([proposal, bdir / "decisions" / proposal.name])
        for path in paths:
            files[str(path.relative_to(bdir))] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return state["active_runs"], state["session_runs"], files


def timed_command(root, *arguments):
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(ENGINE), "--root", str(root), *arguments],
                            capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} exited {result.returncode}: "
                           f"{(result.stderr or result.stdout)[-2000:]}")
    return round(time.monotonic() - started, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--backups", type=Path, help="optional source .bimri-update-backups folder")
    parser.add_argument("--prior-engine", type=Path, help="optional v5.1.2 engine to verify writer refusal")
    args = parser.parse_args()
    source = args.store.resolve(strict=True)
    if not (source / ".bimri" / "state.json").is_file() or not (source / "bimri.md").is_file():
        parser.error("--store must be a populated, quiescent v5 snapshot")
    with tempfile.TemporaryDirectory(prefix="bimri-upgrade-rehearsal-") as temp:
        root = Path(temp) / "project"
        root.mkdir()
        backups = args.backups or source / ".bimri-update-backups"
        shutil.copytree(source / ".bimri", root / ".bimri", symlinks=True)
        shutil.copy2(source / "bimri.md", root / "bimri.md")
        if backups.is_dir():
            shutil.copytree(backups, root / ".bimri-update-backups", symlinks=True)
        for name in ("AGENTS.md", "CLAUDE.md", "bimri-engine.py"):
            if (source / name).is_file():
                shutil.copy2(source / name, root / name)
        original = protected_state(root)
        timings = {}
        receipts = []
        backup_root = root / ".bimri-update-backups"
        for attempt in (1, 2):
            preceding = set(backup_root.glob("*/install-manifest.json"))
            timings[f"install_{attempt}"] = timed_command(root, "install", "--target", str(root),
                                                         "--quiescent")
            if protected_state(root) != original:
                raise RuntimeError(f"protected memory changed on install {attempt}")
            if (root / "bimri-engine.py").read_bytes() != ENGINE.read_bytes():
                raise RuntimeError("installed engine differs from candidate")
            added = set(backup_root.glob("*/install-manifest.json")) - preceding
            if len(added) != 1:
                raise RuntimeError("expected one code-update receipt")
            receipt = json.loads(added.pop().read_text("utf-8"))
            if (receipt["status"] != "installed" or receipt["read_only_audit_errors"]
                    or receipt["protected_write_attempts"]
                    or receipt["before_tree_digest"] != original["tree_digest"]
                    or receipt["after_tree_digest"] != original["tree_digest"]
                    or receipt["accepted_head_hash_before"] != original["accepted_head_hash"]
                    or receipt["accepted_head_hash_after"] != original["accepted_head_hash"]):
                raise RuntimeError("update receipt disagrees with protected-tree verification")
            receipts.append({"status": receipt["status"], "release": receipt["engine_release"],
                             "protected_write_attempts": receipt["protected_write_attempts"]})
        timings["doctor"] = timed_command(root, "doctor", "--budget-ms", "600000")
        timings["status"] = timed_command(root, "status")
        refusal = None
        legacy = pending_legacy_work(root)
        key = "upgrade.rehearsal-probe"
        for index in range(10):
            got = subprocess.run([sys.executable, str(ENGINE), "--root", str(root),
                                  "get", "--key", key, "--json"], capture_output=True, text=True)
            payload = json.loads(got.stdout)
            if got.returncode != (1 if index == 0 else 0) or bool(payload["matches"]) != bool(index):
                raise RuntimeError("the rehearsal subject did not have its expected state")
            create = ["--new-subject"] if index == 0 else []
            timed_command(root, "save", "--request", f"rehearsal-write-{index}", "--observed",
                          payload["token"], "--tier", "2", "--key", key, *create,
                          "--text", f"Synthetic upgrade rehearsal {index}.")
            if pending_legacy_work(root) != legacy:
                raise RuntimeError(f"synthetic save {index} changed an existing legacy run")
        if args.prior_engine:
            before_refusal = protected_state(root)
            old = subprocess.run([sys.executable, str(args.prior_engine.resolve(strict=True)),
                                  "--root", str(root), "start"], capture_output=True, text=True)
            refusal = "unsupported BIMRI state version: 5.2.0"
            if old.returncode != 2 or refusal not in old.stderr or protected_state(root) != before_refusal:
                raise RuntimeError("the earlier engine did not refuse without changing memory")
        print(json.dumps({"engine_sha256": hashlib.sha256(ENGINE.read_bytes()).hexdigest(),
                          "unchanged_across_installs": original, "seconds": timings,
                          "legacy_runs_preserved_through_ten_saves": len(legacy[0]),
                          "receipts": receipts, "prior_engine_refusal": refusal}, indent=2))


if __name__ == "__main__":
    main()
