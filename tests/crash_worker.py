"""Subprocess fault injector for BIMRI crash and post-commit tests.

This helper imports the engine as a module, patches one narrow durability
boundary, and then runs a normal engine command.  The abrupt modes deliberately
use os._exit so neither BIMRI nor Python gets a chance to unwind or flush state.
"""

import builtins
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
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

    no_memory_temp_modes = {
        "code_update_forbid_bdir_temp",
        "code_update_fail_before_engine_no_bdir_temp",
    }
    if mode in no_memory_temp_modes:
        original_mkstemp = engine.tempfile.mkstemp
        protected_temp_parent = root / ".bimri"

        def reject_memory_tempfile(*args, **kwargs):
            directory = kwargs.get("dir")
            if directory is None and len(args) >= 3:
                directory = args[2]
            if directory is not None and Path(directory) == protected_temp_parent:
                raise AssertionError(
                    "code-only updater attempted a temporary file in .bimri"
                )
            return original_mkstemp(*args, **kwargs)

        engine.tempfile.mkstemp = reject_memory_tempfile

    replace_fault = re.fullmatch(
        r"code_update_(caught|abrupt)_install_replace_(\d+)", mode
    )
    if replace_fault:
        try:
            target = Path(command[command.index("--target") + 1]).resolve()
        except (ValueError, IndexError) as exc:
            raise SystemExit("code-update fault requires --target") from exc
        fault_kind = replace_fault.group(1)
        fault_index = int(replace_fault.group(2))
        original_replace = engine.CodeUpdateDestinationPolicy.replace
        replacement_index = 0
        injected = False

        def fault_after_authorized_replace(policy, source, destination):
            nonlocal replacement_index, injected
            result = original_replace(policy, source, destination)
            try:
                relative = Path(destination).relative_to(target).as_posix()
            except ValueError:
                return result
            if relative not in engine.CODE_UPDATE_RELATIVE_TARGETS or injected:
                return result
            replacement_index += 1
            if replacement_index != fault_index:
                return result
            injected = True
            if fault_kind == "abrupt":
                os._exit(100)
            raise RuntimeError(
                "forced caught failure after authorized replacement "
                f"{fault_index}: {relative}"
            )

        engine.CodeUpdateDestinationPolicy.replace = (
            fault_after_authorized_replace
        )

    if mode == "protected_mutation_monitor":
        try:
            monitored_root = Path(
                command[command.index("--target") + 1]
            ).resolve()
        except (ValueError, IndexError):
            monitored_root = root
        monitored_bdir = monitored_root / ".bimri"
        exclusions = {
            monitored_bdir / "engine.lock",
            monitored_bdir / "runtime.local.json",
            monitored_bdir / "hooks.claude.local.json",
        }

        def normalized_path(value):
            if isinstance(value, int):
                return None
            try:
                return Path(os.path.abspath(os.fsdecode(value)))
            except (TypeError, ValueError):
                return None

        def assert_not_protected(operation, *values):
            for value in values:
                candidate = normalized_path(value)
                if candidate is None:
                    continue
                protected = candidate == monitored_root / "bimri.md"
                if (
                    candidate == monitored_bdir
                    or monitored_bdir in candidate.parents
                ):
                    protected = candidate not in exclusions
                if protected:
                    raise AssertionError(
                        "protected mutation monitor observed "
                        f"{operation}: {candidate}"
                    )

        original_builtin_open = builtins.open
        original_io_open = io.open
        original_os_open = engine.os.open
        original_mkstemp = engine.tempfile.mkstemp
        original_replace = engine.os.replace
        original_rename = engine.os.rename
        original_unlink = engine.os.unlink
        original_remove = engine.os.remove
        original_mkdir = engine.os.mkdir
        original_rmdir = engine.os.rmdir
        original_link = engine.os.link
        original_symlink = engine.os.symlink

        def guarded_builtin_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                assert_not_protected("open", file)
            return original_builtin_open(file, mode, *args, **kwargs)

        def guarded_io_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                assert_not_protected("io.open", file)
            return original_io_open(file, mode, *args, **kwargs)

        def guarded_os_open(path, flags, *args, **kwargs):
            writing = flags & (
                os.O_WRONLY
                | os.O_RDWR
                | os.O_APPEND
                | os.O_CREAT
                | os.O_TRUNC
            )
            if writing:
                assert_not_protected("os.open", path)
            return original_os_open(path, flags, *args, **kwargs)

        def guarded_mkstemp(*args, **kwargs):
            directory = kwargs.get("dir")
            if directory is None and len(args) >= 3:
                directory = args[2]
            if directory is not None:
                assert_not_protected("mkstemp", Path(directory) / "temporary")
            return original_mkstemp(*args, **kwargs)

        def guarded_replace(source, destination, *args, **kwargs):
            assert_not_protected("replace-source", source)
            assert_not_protected("replace-destination", destination)
            return original_replace(source, destination, *args, **kwargs)

        def guarded_rename(source, destination, *args, **kwargs):
            assert_not_protected("rename-source", source)
            assert_not_protected("rename-destination", destination)
            return original_rename(source, destination, *args, **kwargs)

        def guarded_unlink(path, *args, **kwargs):
            assert_not_protected("unlink", path)
            return original_unlink(path, *args, **kwargs)

        def guarded_remove(path, *args, **kwargs):
            assert_not_protected("remove", path)
            return original_remove(path, *args, **kwargs)

        def guarded_mkdir(path, *args, **kwargs):
            assert_not_protected("mkdir", path)
            return original_mkdir(path, *args, **kwargs)

        def guarded_rmdir(path, *args, **kwargs):
            assert_not_protected("rmdir", path)
            return original_rmdir(path, *args, **kwargs)

        def guarded_link(source, destination, *args, **kwargs):
            assert_not_protected("link-destination", destination)
            return original_link(source, destination, *args, **kwargs)

        def guarded_symlink(source, destination, *args, **kwargs):
            assert_not_protected("symlink-destination", destination)
            return original_symlink(source, destination, *args, **kwargs)

        builtins.open = guarded_builtin_open
        io.open = guarded_io_open
        engine.os.open = guarded_os_open
        engine.tempfile.mkstemp = guarded_mkstemp
        engine.os.replace = guarded_replace
        engine.os.rename = guarded_rename
        engine.os.unlink = guarded_unlink
        engine.os.remove = guarded_remove
        engine.os.mkdir = guarded_mkdir
        engine.os.rmdir = guarded_rmdir
        engine.os.link = guarded_link
        engine.os.symlink = guarded_symlink

    if replace_fault:
        pass
    elif mode == "protected_mutation_monitor":
        pass
    elif mode == "code_update_forbid_bdir_temp":
        pass
    elif mode == "crash_after_revision":
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
    elif mode == "resolution_crash_after_force_apply":
        original = engine.apply_proposal

        def crash_after_resolution_effect(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("force") and kwargs.get("human_confirmed"):
                os._exit(106)
            return result

        engine.apply_proposal = crash_after_resolution_effect
    elif mode == "resolution_crash_after_applying_record":
        original = engine.atomic_write_json
        resolution_dir = root / ".bimri" / "resolutions"

        def crash_after_applying_resolution(path, data):
            result = original(path, data)
            candidate = Path(path)
            if (
                candidate.parent == resolution_dir
                and isinstance(data, dict)
                and data.get("status") == "applying"
            ):
                os._exit(107)
            return result

        engine.atomic_write_json = crash_after_applying_resolution
    elif mode == "proposal_crash_after_applying_decision":
        original = engine.write_decision

        def crash_after_applying_decision(
            paths, proposal_id, outcome, *args, **kwargs
        ):
            result = original(
                paths, proposal_id, outcome, *args, **kwargs
            )
            if outcome == "applying":
                os._exit(108)
            return result

        engine.write_decision = crash_after_applying_decision
    elif mode == "resolution_crash_after_cooled_archive":
        original = engine.append_archive

        def crash_after_cooled_archive(
            paths, proposal_id, raw_line, reason
        ):
            result = original(paths, proposal_id, raw_line, reason)
            if reason == "cooled":
                os._exit(109)
            return result

        engine.append_archive = crash_after_cooled_archive
    elif mode == "cooling_crash_after_archive_august":
        engine.today = lambda: "2026-08-31"
        original = engine.append_archive

        def crash_after_august_cooled_archive(
            paths, proposal_id, raw_line, reason
        ):
            result = original(paths, proposal_id, raw_line, reason)
            if reason == "cooled":
                os._exit(109)
            return result

        engine.append_archive = crash_after_august_cooled_archive
    elif mode == "cooling_retry_september":
        engine.today = lambda: "2026-09-01"
    elif mode == "missing_head_before_state_save":
        original = engine.save_state
        removed = False

        def remove_head_then_save(paths, state):
            nonlocal removed
            if not removed and len(state.get("run_dates", {})) > 500:
                head = engine.revision_path(paths, state["head_revision"])
                head.unlink()
                removed = True
            return original(paths, state)

        engine.save_state = remove_head_then_save
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
    elif mode in {
        "code_update_fail_before_engine",
        "code_update_fail_before_engine_no_bdir_temp",
        "code_update_crash_after_engine",
    }:
        try:
            target = Path(command[command.index("--target") + 1]).resolve()
        except (ValueError, IndexError) as exc:
            raise SystemExit("code-update fault requires --target") from exc
        installed_engine = target / "bimri-engine.py"
        original = engine.CodeUpdateDestinationPolicy.replace
        injected = False

        def fault_at_engine(policy, source, destination):
            nonlocal injected
            destination = Path(destination)
            if (
                mode in {
                    "code_update_fail_before_engine",
                    "code_update_fail_before_engine_no_bdir_temp",
                }
                and destination == installed_engine
                and not injected
            ):
                injected = True
                raise RuntimeError("forced failure before engine replacement")
            result = original(policy, source, destination)
            if (
                mode == "code_update_crash_after_engine"
                and destination == installed_engine
            ):
                os._exit(96)
            return result

        engine.CodeUpdateDestinationPolicy.replace = fault_at_engine
    elif mode == "code_update_crash_after_preparing_mkdir":
        original = engine.CodeUpdateDestinationPolicy.mkdir

        def crash_after_preparing_mkdir(policy, path, *args, **kwargs):
            result = original(policy, path, *args, **kwargs)
            if Path(path).name.startswith(".preparing-"):
                os._exit(97)
            return result

        engine.CodeUpdateDestinationPolicy.mkdir = crash_after_preparing_mkdir
    elif mode == "code_update_crash_during_first_backup":
        original = engine.CodeUpdateDestinationPolicy.copy

        def crash_during_first_backup(policy, source, destination):
            result = original(policy, source, destination)
            destination = Path(destination)
            if any(
                part.startswith(".preparing-")
                for part in destination.parts
            ) and "files" in destination.parts:
                os._exit(98)
            return result

        engine.CodeUpdateDestinationPolicy.copy = crash_during_first_backup
    elif mode == "code_update_crash_before_prepared_publish":
        original = engine.CodeUpdateDestinationPolicy.rename

        def crash_before_prepared_publish(policy, source, destination):
            if Path(source).name.startswith(".preparing-"):
                os._exit(99)
            return original(policy, source, destination)

        engine.CodeUpdateDestinationPolicy.rename = crash_before_prepared_publish
    elif mode in {
        "code_update_crash_before_activation_state",
        "code_update_crash_after_activation_state",
    }:
        try:
            target = Path(command[command.index("--target") + 1]).resolve()
        except (ValueError, IndexError) as exc:
            raise SystemExit("code-update fault requires --target") from exc
        if mode == "code_update_crash_before_activation_state":
            original_write_json = engine.CodeUpdateDestinationPolicy.write_json

            def crash_after_activation_receipt(policy, destination, data):
                result = original_write_json(policy, destination, data)
                if (
                    Path(destination).name == "install-manifest.json"
                    and data.get("status") == "prepared-for-authority-activation"
                ):
                    os._exit(104)
                return result

            engine.CodeUpdateDestinationPolicy.write_json = (
                crash_after_activation_receipt
            )
        else:
            original_atomic_write_json = engine.atomic_write_json
            state_path = target / ".bimri" / "state.json"

            def crash_after_activation_state(path, data):
                result = original_atomic_write_json(path, data)
                if (
                    Path(path).resolve() == state_path
                    and isinstance(data, dict)
                    and data.get("bimri_version") == engine.MEMORY_FORMAT_VERSION
                ):
                    os._exit(105)
                return result

            engine.atomic_write_json = crash_after_activation_state
    elif mode == "code_update_fail_one_rollback_restore":
        try:
            target = Path(command[command.index("--target") + 1]).resolve()
        except (ValueError, IndexError) as exc:
            raise SystemExit("code-update fault requires --target") from exc
        original = engine.CodeUpdateDestinationPolicy.replace
        injected = False

        def fail_one_rollback_restore(policy, source, destination):
            nonlocal injected
            try:
                relative = Path(destination).relative_to(target).as_posix()
            except ValueError:
                relative = ""
            if (
                not injected
                and "rollback-stage" in Path(source).parts
                and relative == "BIMRI-PROTOCOL.md"
            ):
                injected = True
                raise OSError("forced transient rollback restore failure")
            return original(policy, source, destination)

        engine.CodeUpdateDestinationPolicy.replace = fail_one_rollback_restore
    elif mode == "code_update_crash_after_rollback_engine_replace":
        try:
            target = Path(command[command.index("--target") + 1]).resolve()
        except (ValueError, IndexError) as exc:
            raise SystemExit("code-update fault requires --target") from exc
        original_verify = engine.verify_python_relaunch
        original_replace = engine.CodeUpdateDestinationPolicy.replace

        def fail_after_candidate_verify(executable, program):
            result = original_verify(executable, program)
            if Path(program).resolve() == target / "bimri-engine.py":
                raise RuntimeError("forced failure to enter rollback")
            return result

        def crash_after_rollback_engine_replace(policy, source, destination):
            result = original_replace(policy, source, destination)
            if (
                Path(destination).resolve() == target / "bimri-engine.py"
                and "rollback-stage" in Path(source).parts
            ):
                os._exit(103)
            return result

        engine.verify_python_relaunch = fail_after_candidate_verify
        engine.CodeUpdateDestinationPolicy.replace = (
            crash_after_rollback_engine_replace
        )
    elif mode == "code_update_stop_after_recovery":

        def stop_before_new_backup(*_args, **_kwargs):
            raise RuntimeError("forced stop before fresh update transaction")

        engine._prepare_code_update_backup = stop_before_new_backup
    elif mode == "code_update_hold_lock":
        try:
            target = Path(command[command.index("--target") + 1]).resolve()
        except (ValueError, IndexError) as exc:
            raise SystemExit("code-update fault requires --target") from exc
        original = engine._prepare_code_update_backup
        signal = target / ".test-candidate-lock-held"
        release = target / ".test-release-candidate-lock"

        def hold_before_transaction(*args, **kwargs):
            signal.write_text("candidate lock held\n", encoding="utf-8")
            deadline = time.monotonic() + 10
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting to release candidate lock")
                time.sleep(0.01)
            return original(*args, **kwargs)

        engine._prepare_code_update_backup = hold_before_transaction
    elif mode in {
        "code_update_caught_installed_verify",
        "code_update_abrupt_installed_verify",
    }:
        try:
            target = Path(command[command.index("--target") + 1]).resolve()
        except (ValueError, IndexError) as exc:
            raise SystemExit("code-update fault requires --target") from exc
        original = engine.verify_python_relaunch

        def fault_after_installed_verify(executable, program):
            result = original(executable, program)
            if Path(program).resolve() == target / "bimri-engine.py":
                if mode == "code_update_abrupt_installed_verify":
                    os._exit(101)
                raise RuntimeError("forced failure after installed-engine verify")
            return result

        engine.verify_python_relaunch = fault_after_installed_verify
    elif mode in {
        "code_update_caught_final_receipt",
        "code_update_abrupt_final_receipt",
    }:
        original = engine.CodeUpdateDestinationPolicy.write_json

        def fault_after_final_receipt(policy, destination, data):
            result = original(policy, destination, data)
            if (
                Path(destination).name == "install-manifest.json"
                and data.get("status")
                in {"installed", "installed-recovery-required"}
            ):
                if mode == "code_update_abrupt_final_receipt":
                    os._exit(102)
                raise RuntimeError("forced failure after final update receipt")
            return result

        engine.CodeUpdateDestinationPolicy.write_json = (
            fault_after_final_receipt
        )
    elif mode in {
        "code_update_pre_audit_same_byte_attempt",
        "code_update_post_audit_same_byte_attempt",
    }:
        original = engine.read_only_store_audit
        audit_count = 0

        def attempt_same_byte_audit_write(paths, *args, **kwargs):
            nonlocal audit_count
            audit_count += 1
            selected = (
                audit_count == 1
                if mode == "code_update_pre_audit_same_byte_attempt"
                else audit_count == 2
            )
            if selected:
                engine.atomic_write_text(
                    paths.hot, paths.hot.read_text(encoding="utf-8")
                )
            return original(paths, *args, **kwargs)

        engine.read_only_store_audit = attempt_same_byte_audit_write
    elif mode == "code_update_protected_attempt":
        original = engine._install_staged_code_update

        def attempt_protected_write(
            source_paths,
            paths,
            policy,
            staged,
            python_executable,
        ):
            policy.write_text(paths.hot, paths.hot.read_text("utf-8"))
            return original(
                source_paths,
                paths,
                policy,
                staged,
                python_executable,
            )

        engine._install_staged_code_update = attempt_protected_write
    elif mode == "code_update_protected_source_attempt":
        original = engine._install_staged_code_update

        def attempt_protected_source(
            source_paths,
            paths,
            policy,
            staged,
            python_executable,
        ):
            policy.replace(paths.hot, staged["BIMRI-PROTOCOL.md"])
            return original(
                source_paths,
                paths,
                policy,
                staged,
                python_executable,
            )

        engine._install_staged_code_update = attempt_protected_source
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
                state.get("bimri_version") == engine.MEMORY_FORMAT_VERSION
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
