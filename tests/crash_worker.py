"""Subprocess fault injector for BIMRI crash and post-commit tests.

This helper imports the engine as a module, patches one narrow durability
boundary, and then runs a normal engine command.  The abrupt modes deliberately
use os._exit so neither BIMRI nor Python gets a chance to unwind or flush state.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def load_engine(path):
    spec = importlib.util.spec_from_file_location("bimri_fault_target", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load BIMRI engine")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if len(sys.argv) < 5:
        raise SystemExit(
            "usage: crash_worker.py ENGINE MODE ROOT COMMAND [ARG ...]"
        )
    engine_path = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]
    root = Path(sys.argv[3]).resolve()
    command = sys.argv[4:]
    engine = load_engine(engine_path)

    if mode == "crash_after_revision":
        original = engine.exclusive_write_text

        def crash_after_revision(path, content):
            result = original(path, content)
            candidate = Path(path)
            if (
                candidate.parent == root / ".bimri" / "revisions"
                and candidate.name != "V000000.md"
            ):
                os._exit(91)
            return result

        engine.exclusive_write_text = crash_after_revision
    elif mode == "crash_after_view":
        original = engine.write_generated_view

        def crash_after_view(paths, content, warn_only=False):
            result = original(paths, content, warn_only=warn_only)
            if warn_only:
                os._exit(92)
            return result

        engine.write_generated_view = crash_after_view
    elif mode == "view_permission":
        original = engine.atomic_write_text
        hot = root / "bimri.md"

        def deny_hot_replace(path, content):
            if Path(path) == hot:
                raise PermissionError("forced test lock on bimri.md")
            return original(path, content)

        engine.atomic_write_text = deny_hot_replace
    elif mode == "view_oserror":
        original = engine.atomic_write_text
        hot = root / "bimri.md"

        def fail_hot_write(path, content):
            if Path(path) == hot:
                raise OSError("forced generic generated-view failure")
            return original(path, content)

        engine.atomic_write_text = fail_hot_write
    elif mode == "index_failure":

        def fail_index(_paths, _state):
            raise RuntimeError("forced index failure")

        engine.build_index = fail_index
    elif mode == "install_selfcheck":

        def fail_selfcheck(_paths, _state, governance_issues=None):
            return ["forced post-migration self-check failure"], []

        engine.doctor_errors = fail_selfcheck
    elif mode == "legacy_crash_before_state":
        original = engine.save_state

        def crash_before_legacy_state(paths, state):
            if state.get("legacy_migration") == "legacy-to-v5":
                os._exit(93)
            return original(paths, state)

        engine.save_state = crash_before_legacy_state
    elif mode == "legacy_crash_before_retire":

        def crash_before_legacy_retire(_paths, _marker, _revision_bytes):
            os._exit(94)

        engine._retire_legacy_sources = crash_before_legacy_retire
    elif mode == "v4_crash_before_state":
        original = engine.save_state

        def crash_before_v4_state(paths, state):
            if (
                state.get("bimri_version") == engine.VERSION
                and (paths.migrations / "v4-to-v5.json").exists()
            ):
                os._exit(95)
            return original(paths, state)

        engine.save_state = crash_before_v4_state
    elif mode in {"v4_hot_change_after_backup", "v4_state_change_after_backup"}:
        original = engine.backup_file

        def change_v4_source_after_backup(paths, path, label):
            result = original(paths, path, label)
            if mode == "v4_hot_change_after_backup" and label == "bimri-v4.md":
                paths.hot.write_bytes(paths.hot.read_bytes() + b"\nlate v4 hot writer\n")
            if mode == "v4_state_change_after_backup" and label == "state-v4.json":
                paths.state.write_bytes(paths.state.read_bytes() + b" \n")
            return result

        engine.backup_file = change_v4_source_after_backup
    elif mode == "legacy_source_change_before_retire":
        original = engine._retire_legacy_sources

        def change_source_before_retire(paths, marker, revision_bytes):
            source_record = next(
                asset for asset in marker["assets"] if asset["role"] == "active memory"
            )
            source = paths.root / source_record["source_path"]
            source.write_bytes(source.read_bytes() + b"\nlate legacy writer\n")
            return original(paths, marker, revision_bytes)

        engine._retire_legacy_sources = change_source_before_retire
    elif mode.startswith("python_verify_"):
        completed_process = subprocess.CompletedProcess
        timeout_expired = subprocess.TimeoutExpired

        def fake_python_check(arguments, **_kwargs):
            if mode == "python_verify_silent":
                return completed_process(arguments, 0, "", "")
            if mode == "python_verify_wrong":
                return completed_process(
                    arguments,
                    0,
                    '{"executable":"/wrong","sentinel":"wrong",'
                    '"version":[3,8]}\n',
                    "",
                )
            if mode == "python_verify_old":
                token = arguments[-1]
                return completed_process(
                    arguments,
                    0,
                    engine.json.dumps({
                        "executable": str(Path(sys.executable).resolve()),
                        "sentinel": token,
                        "version": [3, 7],
                    }) + "\n",
                    "",
                )
            if mode == "python_verify_timeout":
                raise timeout_expired(arguments, 5)
            raise RuntimeError("unknown Python verification fault")

        engine.subprocess.run = fake_python_check
    else:
        raise SystemExit("unknown fault mode: " + mode)

    return engine.main(["--root", str(root)] + command)


if __name__ == "__main__":
    sys.exit(main())
