"""Subprocess fault injector for BIMRI crash and post-commit tests.

This helper imports the engine as a module, patches one narrow durability
boundary, and then runs a normal engine command.  The abrupt modes deliberately
use os._exit so neither BIMRI nor Python gets a chance to unwind or flush state.
"""

import builtins
import errno
import importlib.util
import io
import json
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


SAVE_CRASH_POINT = ".test-save-crash-point"

# Clock modes (build step 11, DESIGN section 02 2.10 and 10.4 gate 2): pin
# only engine.today, the engine's one date seam; engine.now_iso stays real
# time. A clock mode runs alone (the command then runs unpatched otherwise)
# or combines with one other mode through "+" or ",", for example
# "clock_plus_days_8+save_crash_after_write_3".
#   clock_plus_days_<n>    today's UTC date plus n days
#   clock_plus_months_<n>  today's UTC date plus n calendar months (the day
#                          clamped to the target month's length)
#   clock_date_<YYYY-MM-DD> that exact date
# The base is the UTC date, the date a ticket's finished_at (now_iso, UTC)
# carries, so a ticket finished just before the pinned command is exactly
# n days old in every time zone (the engine's today() is a local date).
# The names the step-11 design and tests spell out (any <n> works).
NAMED_CLOCK_MODES = (
    "clock_plus_days_7",
    "clock_plus_days_8",
    "clock_plus_months_1",
    "clock_plus_months_2",
    "clock_plus_months_3",
)
CLOCK_MODE_RE = re.compile(
    r"clock_(?:plus_days_(?P<days>\d{1,5})|plus_months_(?P<months>\d{1,3})"
    r"|date_(?P<date>\d{4}-\d{2}-\d{2}))"
)


def pinned_clock_date(match):
    import calendar
    import datetime

    real = datetime.datetime.now(datetime.timezone.utc).date()
    if match.group("days") is not None:
        return (real + datetime.timedelta(days=int(match.group("days")))).isoformat()
    if match.group("months") is not None:
        total = real.year * 12 + (real.month - 1) + int(match.group("months"))
        year, month = divmod(total, 12)
        month += 1
        day = min(real.day, calendar.monthrange(year, month)[1])
        return datetime.date(year, month, day).isoformat()
    return datetime.date.fromisoformat(match.group("date")).isoformat()


def split_clock_mode(raw_mode):
    """(remaining mode, pinned date or None); at most one clock part."""
    parts = [part for part in re.split(r"[+,]", raw_mode) if part]
    clock = [part for part in parts if CLOCK_MODE_RE.fullmatch(part)]
    rest = [part for part in parts if not CLOCK_MODE_RE.fullmatch(part)]
    if len(clock) > 1 or len(rest) > 1 or not parts:
        raise SystemExit("unknown fault mode: " + raw_mode)
    if not clock:
        return raw_mode, None
    pinned = pinned_clock_date(CLOCK_MODE_RE.fullmatch(clock[0]))
    return (rest[0] if rest else clock[0]), pinned


def install_save_counter_seam(engine, root, crash, target):
    """Section 10 10.1.1 / 10.1.3: count durable writes under the store.

    The store's write primitives (and, for the crash seam, the one deletion
    primitive remove_protected_file) are wrapped. A nested call (for
    example atomic_write_json -> atomic_write_text) is part of its outer
    write and is never counted twice. The crash seam lets the n-th write
    complete, records it in .test-save-crash-point and exits 150; the
    full-disk seam raises ENOSPC instead of the n-th write. The full-disk
    seam never fails the generated view (bimri.md), whose write failure is
    a warning by design (write_generated_view(warn_only=True)).
    """
    bdir = Path(os.path.abspath(str(root / ".bimri")))
    hot = Path(os.path.abspath(str(root / "bimri.md")))
    counter = {"count": 0, "depth": 0}

    def relative(path):
        candidate = Path(os.path.abspath(os.fspath(path)))
        if candidate == hot:
            return "bimri.md"
        try:
            return candidate.relative_to(root).as_posix()
        except ValueError:
            return candidate.as_posix()

    def protected(path):
        try:
            candidate = Path(os.path.abspath(os.fspath(path)))
        except (TypeError, ValueError):
            return False
        if candidate == hot:
            return True
        return candidate == bdir or bdir in candidate.parents

    def wrap(name, removal=False):
        original = getattr(engine, name)

        def counted(path, *args, **kwargs):
            outer = counter["depth"] == 0 and protected(path)
            if outer and not crash:
                is_hot = Path(os.path.abspath(os.fspath(path))) == hot
                if not removal and not is_hot:
                    counter["count"] += 1
                    if counter["count"] == target:
                        raise OSError(errno.ENOSPC, "No space left on device")
            counter["depth"] += 1
            try:
                result = original(path, *args, **kwargs)
            finally:
                counter["depth"] -= 1
            if outer and crash:
                counter["count"] += 1
                if counter["count"] == target:
                    with open(
                        str(root / SAVE_CRASH_POINT), "w", encoding="utf-8"
                    ) as handle:
                        handle.write(f"{target}\t{relative(path)}\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os._exit(150)
            return result

        setattr(engine, name, counted)

    for name in (
        "exclusive_write_text",
        "exclusive_write_bytes",
        "atomic_write_json",
        "atomic_write_text",
        "atomic_write_bytes",
        "append_line",
    ):
        wrap(name)
    wrap("remove_protected_file", removal=True)


SAVE_FORBIDDEN_SCANS = (
    "audit_witness_manifest",
    "audit_witness_run_facts",
    "archive_records",
    "accepted_key_writer_after",
    "accepted_writers_for_key",
    "accepted_cold_current_at_revision",
    "uncommitted_owner_resolution",
    "next_proposal_id",
    "next_pattern_id",
    "exact_archive_effect",
    "referenced_revision_numbers",
    "recall_records",
    "held_recall_records",
    "classify_review_records",
    "build_index",
    "authority_has_unsettled_records",
    "authority_has_recoverable_applying",
)
# Directories under .bimri/ a warm save may list (D2.1, D2.7, D3.2 L6).
SAVE_LISTABLE_DIRECTORIES = (
    ("saves", "open"),
    ("saves", "done"),
    ("settlements",),
    ("audit-manifests",),
)
ONE_RUN_PREFIX_GLOB = re.compile(r"R\d{6}-Q\*\.json")
OPEN_COUNT_FILE = ".test-open-count"


def install_save_forbid_seam(engine, root):
    """Section 10 10.1.4 `save_forbid_history_scan` (gate 6).

    Every whole-history collector raises AssertionError (printed to stderr
    first, so a violation an engine handler swallows is still seen);
    authority_storage_issues and scan_open_conflicts pass only when bounded
    by `only=`, validate_cold_archive_bindings only with its
    checkpoint_months bypass. Any listing of a directory under .bimri/
    raises, except saves/open, saves/done, settlements, audit-manifests and
    one run's `<R>-Q*.json` prefix glob on proposals/ or decisions/. Every
    distinct .bimri/ file opened for reading is counted into
    <root>/.test-open-count at exit.
    """
    import atexit

    bdir = os.path.normcase(os.path.abspath(str(root / ".bimri")))
    opened = set()

    def violation(text):
        message = "save_forbid_history_scan: " + text
        print("AssertionError: " + message, file=sys.stderr, flush=True)
        raise AssertionError(message)

    def reject(name):
        def rejected(*_args, **_kwargs):
            violation("history scan " + name)
        return rejected

    for name in SAVE_FORBIDDEN_SCANS:
        if hasattr(engine, name):
            setattr(engine, name, reject(name))

    original_storage = engine.authority_storage_issues

    def bounded_storage(*args, **kwargs):
        if not kwargs.get("only"):
            violation("history scan authority_storage_issues without only=")
        return original_storage(*args, **kwargs)

    engine.authority_storage_issues = bounded_storage
    original_conflicts = engine.scan_open_conflicts

    def bounded_conflicts(*args, **kwargs):
        only = kwargs.get("only")
        if only is None and len(args) >= 3:
            only = args[2]
        if only is None:
            violation("history scan scan_open_conflicts without only=")
        return original_conflicts(*args, **kwargs)

    engine.scan_open_conflicts = bounded_conflicts
    original_bindings = engine.validate_cold_archive_bindings

    def checkpointed_bindings(*args, **kwargs):
        if kwargs.get("checkpoint_months") is None:
            violation(
                "history scan validate_cold_archive_bindings without "
                "checkpoint_months"
            )
        return original_bindings(*args, **kwargs)

    engine.validate_cold_archive_bindings = checkpointed_bindings

    def normalized(target):
        try:
            text = os.fspath(target)
        except TypeError:
            return None
        if isinstance(text, bytes):
            text = os.fsdecode(text)
        return os.path.normcase(os.path.abspath(text))

    def store_parts(target):
        """Path components below .bimri/, or None outside the store."""
        resolved = normalized(target)
        if resolved is None:
            return None
        if resolved == bdir:
            return ()
        if not resolved.startswith(bdir + os.sep):
            return None
        return tuple(resolved[len(bdir) + 1:].split(os.sep))

    def check_listing(operation, target, pattern=None):
        parts = store_parts(target)
        if parts is None:
            return
        if parts in SAVE_LISTABLE_DIRECTORIES:
            return
        if (
            operation == "glob"
            and parts in {("proposals",), ("decisions",)}
            and isinstance(pattern, str)
            and ONE_RUN_PREFIX_GLOB.fullmatch(pattern)
        ):
            return
        violation(
            "listed .bimri/" + "/".join(parts)
            + (" with " + pattern if pattern else "") + " (" + operation + ")"
        )

    path_type = type(Path())
    original_glob = path_type.glob
    original_rglob = path_type.rglob
    original_iterdir = path_type.iterdir
    original_listdir = os.listdir
    original_scandir = os.scandir

    def guarded_glob(self, pattern):
        check_listing("glob", self, pattern)
        return original_glob(self, pattern)

    def guarded_rglob(self, pattern):
        check_listing("rglob", self, pattern)
        return original_rglob(self, pattern)

    def guarded_iterdir(self):
        check_listing("iterdir", self)
        return original_iterdir(self)

    def guarded_listdir(path="."):
        check_listing("listdir", path)
        return original_listdir(path)

    def guarded_scandir(path="."):
        check_listing("scandir", path)
        return original_scandir(path)

    path_type.glob = guarded_glob
    path_type.rglob = guarded_rglob
    path_type.iterdir = guarded_iterdir
    os.listdir = guarded_listdir
    os.scandir = guarded_scandir

    def note_read(target):
        parts = store_parts(target)
        if parts:
            opened.add("/".join(parts))

    original_builtin_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open
    original_path_open = path_type.open

    def reading_mode(mode):
        return not any(flag in mode for flag in ("w", "a", "x", "+"))

    def counted_builtin_open(file, mode="r", *args, **kwargs):
        if not isinstance(file, int) and reading_mode(mode):
            note_read(file)
        return original_builtin_open(file, mode, *args, **kwargs)

    def counted_io_open(file, mode="r", *args, **kwargs):
        if not isinstance(file, int) and reading_mode(mode):
            note_read(file)
        return original_io_open(file, mode, *args, **kwargs)

    def counted_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR) == 0:
            note_read(path)
        return original_os_open(path, flags, *args, **kwargs)

    def counted_path_open(self, mode="r", *args, **kwargs):
        if reading_mode(mode):
            note_read(self)
        return original_path_open(self, mode, *args, **kwargs)

    builtins.open = counted_builtin_open
    io.open = counted_io_open
    os.open = counted_os_open
    path_type.open = counted_path_open

    def write_count():
        with original_builtin_open(
            str(root / OPEN_COUNT_FILE), "w", encoding="utf-8"
        ) as handle:
            handle.write(f"{len(opened)}\n")
            for name in sorted(opened):
                handle.write(name + "\n")

    atexit.register(write_count)


def install_publish_timer(engine, root):
    """Section 10 10.4 `save_time_publish`: wall time of scoped publication
    (perf_counter around publish_scoped_checkpoint) into .test-publish-ms."""
    original = engine.publish_scoped_checkpoint
    total = {"ms": 0.0, "calls": 0}

    def timed(*args, **kwargs):
        began = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            total["ms"] += (time.perf_counter() - began) * 1000.0
            total["calls"] += 1
            with open(
                str(root / ".test-publish-ms"), "w", encoding="utf-8"
            ) as handle:
                handle.write(f"{total['ms']:.3f}\t{total['calls']}\n")

    engine.publish_scoped_checkpoint = timed


def _live_save_marker(root):
    marker = root / ".bimri" / "audit-transition.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("operation") != "save":
        return None
    return data


def install_named_save_seam(engine, root, mode):
    """Section 10 10.1.2: one save boundary by name. Returns False for an
    unknown save_* mode so the caller reports it."""
    bdir = root / ".bimri"

    def under(path, *parts):
        candidate = Path(os.path.abspath(os.fspath(path)))
        return candidate.parent == Path(os.path.abspath(str(bdir.joinpath(*parts))))

    if mode == "save_crash_after_ticket":
        original = engine.exclusive_write_text

        def crash_after_ticket(path, content):
            result = original(path, content)
            if under(path, "saves", "open"):
                os._exit(121)
            return result

        engine.exclusive_write_text = crash_after_ticket
    elif mode == "save_crash_after_marker_before_epoch":
        original = engine.atomic_write_json

        def crash_after_marker(path, data):
            result = original(path, data)
            if (
                Path(os.path.abspath(os.fspath(path)))
                == Path(os.path.abspath(str(bdir / "audit-transition.json")))
                and isinstance(data, dict)
                and data.get("operation") == "save"
                and (data.get("scope") or {}).get("completion") is None
            ):
                os._exit(122)
            return result

        engine.atomic_write_json = crash_after_marker
    elif mode == "save_crash_after_epoch":
        original = engine.atomic_write_json

        def crash_after_epoch(path, data):
            result = original(path, data)
            if Path(os.path.abspath(os.fspath(path))) == Path(
                os.path.abspath(str(bdir / "state.json"))
            ):
                marker = _live_save_marker(root)
                if marker is not None:
                    planned = (marker.get("scope") or {}).get("proposal_ids", [])
                    if planned and not (
                        bdir / "proposals" / f"{planned[0]}.json"
                    ).exists():
                        os._exit(123)
            return result

        engine.atomic_write_json = crash_after_epoch
    elif mode == "save_crash_after_proposal":
        original = engine.exclusive_write_text

        def crash_after_proposal(path, content):
            result = original(path, content)
            if under(path, "proposals") and '"request_id"' in content:
                os._exit(124)
            return result

        engine.exclusive_write_text = crash_after_proposal
    elif mode in {"save_crash_after_log", "save_hold_after_log"}:
        original = engine.exclusive_write_text
        signal = root / ".test-save-held"
        release = root / ".test-release-save"
        held = {"done": False}

        def after_save_log(path, content):
            result = original(path, content)
            if under(path, "log") and "| save:" in content:
                if mode == "save_crash_after_log":
                    os._exit(125)
                if not held["done"]:
                    held["done"] = True
                    signal.write_text("save writer lock held\n", encoding="utf-8")
                    deadline = time.monotonic() + 30
                    while not release.exists():
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "timed out waiting to release the save lock"
                            )
                        time.sleep(0.005)
            return result

        engine.exclusive_write_text = after_save_log
    elif mode in {
        "save_crash_after_applying",
        "save_crash_after_final_decision",
        "save_write_undeclared_path",
        "save_append_foreign_log",
    }:
        original = engine.write_decision
        finals = {"accepted", "held", "noop", "contested"}

        def after_decision(paths, proposal_id, outcome, *args, **kwargs):
            result = original(paths, proposal_id, outcome, *args, **kwargs)
            marker = _live_save_marker(root)
            if mode == "save_crash_after_applying":
                if outcome == "applying" and marker is not None:
                    os._exit(126)
            elif outcome in finals and marker is not None:
                if mode == "save_crash_after_final_decision":
                    os._exit(129)
                if mode == "save_write_undeclared_path":
                    engine.exclusive_write_bytes(
                        bdir / "recovery" / "x.bin", b"undeclared\n"
                    )
                else:
                    own = marker.get("run_id")
                    others = sorted(
                        path for path in (bdir / "log").glob("R*.md")
                        if path.stem != own
                    )
                    if others:
                        foreign = others[-1]
                        engine.append_line(
                            foreign,
                            f"[PROPOSE:{foreign.stem}-Q009] [set] "
                            "[K:foreign.append] [BASE:absent] foreign line",
                        )
            return result

        engine.write_decision = after_decision
    elif mode == "save_crash_after_state_commit":
        original_exclusive = engine.exclusive_write_text
        original_json = engine.atomic_write_json
        wrote_revision = {"done": False}

        def note_revision(path, content):
            result = original_exclusive(path, content)
            if under(path, "revisions"):
                wrote_revision["done"] = True
            return result

        def crash_after_state(path, data):
            result = original_json(path, data)
            if wrote_revision["done"] and Path(
                os.path.abspath(os.fspath(path))
            ) == Path(os.path.abspath(str(bdir / "state.json"))):
                os._exit(128)
            return result

        engine.exclusive_write_text = note_revision
        engine.atomic_write_json = crash_after_state
    elif mode == "save_crash_after_closed_line":
        original = engine.append_line

        def crash_after_closed(path, line, *args, **kwargs):
            result = original(path, line, *args, **kwargs)
            if str(line).startswith("[CLOSED:"):
                os._exit(132)
            return result

        engine.append_line = crash_after_closed
    elif mode == "save_crash_after_ticket_finished":
        original = engine.exclusive_write_text

        def crash_after_finished(path, content):
            result = original(path, content)
            if under(path, "saves", "done"):
                os._exit(133)
            return result

        engine.exclusive_write_text = crash_after_finished
    else:
        return False
    return True


def main():
    if len(sys.argv) < 5:
        raise SystemExit(
            "usage: crash_worker.py ENGINE MODE ROOT COMMAND [ARG ...]"
        )
    engine_path = Path(sys.argv[1]).resolve()
    mode, pinned_today = split_clock_mode(sys.argv[2])
    root = Path(sys.argv[3]).resolve()
    command = sys.argv[4:]
    engine = load_engine(engine_path)
    if pinned_today is not None:
        engine.today = lambda: pinned_today
    graph_replay_observed = None

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
    save_counter = re.fullmatch(
        r"save_(crash_after_write|fail_enospc_at)_(\d+)", mode
    )
    # Gate 1 (section 10 10.4): run the command n times in one process.
    loop_start = re.fullmatch(r"loop_start_(\d+)", mode)
    if save_counter:
        install_save_counter_seam(
            engine,
            root,
            crash=save_counter.group(1) == "crash_after_write",
            target=int(save_counter.group(2)),
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
    elif save_counter:
        pass
    elif CLOCK_MODE_RE.fullmatch(mode):
        pass
    elif mode == "save_forbid_history_scan":
        install_save_forbid_seam(engine, root)
    elif mode == "save_time_publish":
        install_publish_timer(engine, root)
    elif loop_start:
        pass
    elif mode == "exit_after_first_stdout_line":
        # Gate 1 (abandoned chats): the process dies right after the first
        # line reaches stdout, with no unwinding, as a killed hook would.
        original_print = builtins.print

        def print_then_exit(*args, **kwargs):
            target = kwargs.get("file")
            original_print(*args, **kwargs)
            if target is None or target is sys.stdout:
                try:
                    sys.stdout.flush()
                finally:
                    os._exit(0)

        engine.print = print_then_exit
    elif mode.startswith("save_") and install_named_save_seam(
        engine, root, mode
    ):
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
    elif mode == "resolution_fail_during_force_apply":
        original = engine.apply_proposal

        def fail_during_resolution_effect(*args, **kwargs):
            if kwargs.get("force") and kwargs.get("human_confirmed"):
                raise engine.BimriError("forced resolution effect failure")
            return original(*args, **kwargs)

        engine.apply_proposal = fail_during_resolution_effect
    elif mode == "resolution_fail_before_archive_append":
        original = engine.append_line
        conflict_id = command[1]
        choice = command[command.index("--choose") + 1]
        resolution_path = root / ".bimri" / "resolutions" / f"{conflict_id}.json"
        closed_record = re.compile(
            r"\[ARCHIVED:\d{4}-\d{2}-\d{2}\] \[BY:"
            + re.escape(choice) + r"\] \[closed\] .+"
        )

        def fail_before_selected_archive_append(path, line, *args, **kwargs):
            if (
                Path(path).parent == root / ".bimri" / "archive"
                and closed_record.fullmatch(line)
            ):
                resolution = json.loads(resolution_path.read_text("utf-8"))
                if (
                    resolution["status"] != "applying"
                    or resolution["conflict_id"] != conflict_id
                    or resolution["choice"] != choice
                ):
                    raise AssertionError("archive fault reached without matching intent")
                print("FORCED_ARCHIVE_APPEND_FAILURE:" + choice, file=sys.stderr)
                raise OSError(errno.EIO, "forced archive append failure")
            return original(path, line, *args, **kwargs)

        engine.append_line = fail_before_selected_archive_append
    elif mode == "resolution_crash_after_resolved_record":
        original = engine.atomic_write_json
        conflict_id = command[1]
        choice = command[command.index("--choose") + 1]
        resolution_path = root / ".bimri" / "resolutions" / f"{conflict_id}.json"

        def crash_after_selected_resolved_record(path, data):
            result = original(path, data)
            if (
                Path(path) == resolution_path
                and isinstance(data, dict)
                and data.get("status") == "resolved"
                and data.get("choice") == choice
            ):
                os._exit(120)
            return result

        engine.atomic_write_json = crash_after_selected_resolved_record
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
            paths, proposal_id, raw_line, reason, *args, **kwargs
        ):
            result = original(
                paths, proposal_id, raw_line, reason, *args, **kwargs
            )
            if reason == "cooled":
                os._exit(109)
            return result

        engine.append_archive = crash_after_cooled_archive
    elif mode == "cooling_crash_after_archive_august":
        engine.today = lambda: "2026-08-31"
        original = engine.append_archive

        def crash_after_august_cooled_archive(
            paths, proposal_id, raw_line, reason, *args, **kwargs
        ):
            result = original(
                paths, proposal_id, raw_line, reason, *args, **kwargs
            )
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
    elif mode == "lifecycle_crash_before_log":
        original = engine.exclusive_write_text

        def crash_before_run_log(path, content):
            if Path(path).parent == root / ".bimri" / "log":
                os._exit(110)
            return original(path, content)

        engine.exclusive_write_text = crash_before_run_log
    elif mode == "witness_crash_before_replace":
        original = engine.os.replace
        witness = (root / ".bimri" / "audit-witness.json").resolve()

        def crash_before_witness_replace(source, destination, *args, **kwargs):
            if Path(destination).resolve() == witness:
                os._exit(110)
            return original(source, destination, *args, **kwargs)

        engine.os.replace = crash_before_witness_replace
    elif mode == "blocked_restore_crash_between_manifest_and_witness":
        original_replace = engine.os.replace
        manifest = (root / ".bimri" / "audit-manifest.json").resolve()
        witness = (root / ".bimri" / "audit-witness.json").resolve()
        publication_marker = root / ".test-restore-manifest-published"

        def crash_after_manifest_before_witness(
            source, destination, *args, **kwargs
        ):
            resolved = Path(destination).resolve()
            if resolved == witness and publication_marker.exists():
                os._exit(113)
            result = original_replace(source, destination, *args, **kwargs)
            if resolved == manifest:
                publication_marker.write_text(
                    "manifest published\n", encoding="utf-8"
                )
            return result

        engine.os.replace = crash_after_manifest_before_witness
    elif mode == "exact_recall_forbid_global_collection":

        def reject_global_collection(*_args, **_kwargs):
            raise AssertionError(
                "exact-current recall attempted global history collection"
            )

        engine.recall_records = reject_global_collection
    elif mode == "valid_witness_forbid_full_audit":

        def reject_full_authority_audit(*_args, **_kwargs):
            raise AssertionError(
                "valid audit witness fell back to a full governance audit"
            )

        engine.authority_storage_issues = reject_full_authority_audit
        engine.audit_witness_manifest = reject_full_authority_audit
        engine.audit_witness_run_facts = reject_full_authority_audit
        engine.load_audit_manifest_evidence = reject_full_authority_audit
        engine.scan_open_conflicts = reject_full_authority_audit
        engine.held_recall_records = reject_full_authority_audit
    elif mode == "status_forbid_history_scan":
        # D5.1 / section 05 1.7 / section 10.1.4: status is a short report
        # from state and a few small files. Every whole-history collector
        # (the D5.1 list plus the step-3 save list's scans), any open of a
        # run log, and any listing of log/ or archive/ fails the command.

        def reject_history_scan(name):
            def reject(*_args, **_kwargs):
                raise AssertionError("status attempted a history scan: " + name)
            return reject

        for name in (
            "audit_witness_manifest",
            "audit_witness_run_facts",
            "authority_storage_issues",
            "scan_open_conflicts",
            "referenced_revision_numbers",
            "archive_records",
            "classify_review_records",
            "governance_snapshot",
            "cold_entry",
            "recall_records",
            "accepted_key_writer_after",
            "held_recall_records",
            "uncommitted_owner_resolution",
            "authority_has_unsettled_records",
            "authority_has_recoverable_applying",
            "next_proposal_id",
            "next_pattern_id",
            "validate_current_residency",
        ):
            if hasattr(engine, name):
                setattr(engine, name, reject_history_scan(name))
        log_root = os.path.normcase(str((root / ".bimri" / "log").resolve()))
        unlisted_roots = {
            log_root,
            os.path.normcase(str((root / ".bimri" / "archive").resolve())),
        }

        def normalized(target):
            try:
                text = os.fspath(target)
            except TypeError:
                return None
            if isinstance(text, bytes):
                text = os.fsdecode(text)
            return os.path.normcase(str(Path(text).resolve()))

        def under_log(target):
            resolved = normalized(target)
            if resolved is None:
                return False
            return resolved == log_root or resolved.startswith(log_root + os.sep)

        def listed_history(target):
            return normalized(target) in unlisted_roots

        original_scandir = os.scandir
        original_listdir = os.listdir
        original_os_open = os.open
        original_glob = Path.glob
        original_iterdir = Path.iterdir

        def guarded_scandir(path="."):
            if listed_history(path):
                raise AssertionError("status listed history: " + str(path))
            return original_scandir(path)

        def guarded_listdir(path="."):
            if listed_history(path):
                raise AssertionError("status listed history: " + str(path))
            return original_listdir(path)

        def guarded_os_open(path, flags, *args, **kwargs):
            if (
                flags & (os.O_WRONLY | os.O_RDWR)
            ) == 0 and under_log(path):
                raise AssertionError("status opened a run log: " + str(path))
            return original_os_open(path, flags, *args, **kwargs)

        def guarded_glob(self, pattern):
            if listed_history(self):
                raise AssertionError("status listed history: " + str(self))
            return original_glob(self, pattern)

        def guarded_iterdir(self):
            if listed_history(self):
                raise AssertionError("status listed history: " + str(self))
            return original_iterdir(self)

        os.scandir = guarded_scandir
        os.listdir = guarded_listdir
        os.open = guarded_os_open
        Path.glob = guarded_glob
        Path.iterdir = guarded_iterdir

        original_open = builtins.open
        original_io_open = io.open
        path_type = type(Path())
        original_read_text = path_type.read_text
        original_read_bytes = path_type.read_bytes
        original_path_open = path_type.open

        def guarded_open(file, *args, **kwargs):
            if not isinstance(file, int) and under_log(file):
                raise AssertionError("status opened a run log: " + str(file))
            return original_open(file, *args, **kwargs)

        def guarded_io_open(file, *args, **kwargs):
            if not isinstance(file, int) and under_log(file):
                raise AssertionError("status opened a run log: " + str(file))
            return original_io_open(file, *args, **kwargs)

        def guarded_read_text(self, *args, **kwargs):
            if under_log(self):
                raise AssertionError("status read a run log: " + str(self))
            return original_read_text(self, *args, **kwargs)

        def guarded_read_bytes(self, *args, **kwargs):
            if under_log(self):
                raise AssertionError("status read a run log: " + str(self))
            return original_read_bytes(self, *args, **kwargs)

        def guarded_path_open(self, *args, **kwargs):
            if under_log(self):
                raise AssertionError("status opened a run log: " + str(self))
            return original_path_open(self, *args, **kwargs)

        builtins.open = guarded_open
        io.open = guarded_io_open
        path_type.read_text = guarded_read_text
        path_type.read_bytes = guarded_read_bytes
        path_type.open = guarded_path_open
    elif mode == "cold_forbid_archive_reads":
        # valid_witness_forbid_full_audit plus every archive reader: a warm
        # `cold` listing iterates state.cold_current and nothing else.

        def reject_cold_archive_read(*_args, **_kwargs):
            raise AssertionError(
                "warm cold listing read the archive or a full audit"
            )

        for name in (
            "authority_storage_issues",
            "audit_witness_manifest",
            "audit_witness_run_facts",
            "load_audit_manifest_evidence",
            "scan_open_conflicts",
            "held_recall_records",
            "archive_records",
            "parse_archive_month",
            "cold_entry",
            "validate_cold_archive_bindings",
            "validate_selected_cold_archive_binding",
            "validate_cold_archive_snapshot",
            "recall_records",
            "governance_snapshot",
        ):
            setattr(engine, name, reject_cold_archive_read)
    elif mode == "warm_search_forbid_full_audit":
        # Section 06 4.5: the warm search may read M0 (the month-hash check
        # needs it) but never a whole-history scan or the full path.

        def reject_warm_search_full_audit(*_args, **_kwargs):
            raise AssertionError(
                "warm search fell back to a full audit or history scan"
            )

        for name in (
            "recall_records",
            "held_recall_records",
            "archive_records",
            "authority_proposal",
            "validate_proposal_base_snapshot",
            "authority_storage_issues",
            "audit_witness_manifest",
            "audit_witness_run_facts",
            "scan_open_conflicts",
            "governance_snapshot",
            "capture_audit_copy",
        ):
            # capture_audit_copy arrives with the copy path (step 8); the
            # seam guards it whenever it exists.
            setattr(engine, name, reject_warm_search_full_audit)
    elif mode == "valid_checkpoint_forbid_graph_replay":

        def reject_graph_replay(*_args, **_kwargs):
            raise AssertionError(
                "valid authority checkpoint triggered global graph replay"
            )

        engine.authority_storage_issues = reject_graph_replay
    elif mode == "missing_checkpoint_require_graph_replay":
        original = engine.authority_storage_issues
        graph_replay_observed = {"called": False}

        def observe_graph_replay(*args, **kwargs):
            graph_replay_observed["called"] = True
            return original(*args, **kwargs)

        engine.authority_storage_issues = observe_graph_replay
    elif mode == "journal_hold_lock":
        original = engine.append_line
        signal = root / ".test-writer-lock-held"
        release = root / ".test-release-writer-lock"
        held = False

        def hold_journal_write(path, line, *args, **kwargs):
            nonlocal held
            candidate = Path(path)
            if (
                not held
                and candidate.parent == root / ".bimri" / "log"
                and str(line).startswith("[ID:")
            ):
                held = True
                signal.write_text("writer lock held\n", encoding="utf-8")
                deadline = time.monotonic() + 20
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "timed out waiting to release journal writer lock"
                        )
                    time.sleep(0.005)
            return original(path, line, *args, **kwargs)

        engine.append_line = hold_journal_write
    elif mode == "audit_copy_hold":
        # Gate 7 (DESIGN section 05 6): once a copy capture has finished
        # (Phase A under the lock, Phase B with it released), signal and hold
        # before the copy is audited, so tests can run commands against the
        # free live lock while the audit is pending.
        original = engine.complete_audit_copy
        signal = root / ".test-copy-captured"
        release = root / ".test-release-audit"

        def hold_after_capture(paths, copy_paths, pending):
            receipt = original(paths, copy_paths, pending)
            signal.write_text(
                json.dumps({
                    "copy": str(copy_paths.root),
                    "head_revision": receipt.get("head_revision"),
                    "audit_epoch": receipt.get("audit_epoch"),
                }),
                encoding="utf-8",
            )
            deadline = time.monotonic() + 120
            while not release.exists():
                if time.monotonic() >= deadline:
                    os._exit(9)
                time.sleep(0.05)
            return receipt

        engine.complete_audit_copy = hold_after_capture
    elif mode == "audit_copy_between_phases":
        # DESIGN section 05 6: hold after Phase A released the lock and
        # before Phase B, so a test can replace or shorten an inventoried
        # file between the phases.
        original = engine.complete_audit_copy
        signal = root / ".test-phase-a-done"
        release = root / ".test-phase-b-go"

        def hold_between_phases(paths, copy_paths, pending):
            signal.write_text("phase A done\n", encoding="utf-8")
            deadline = time.monotonic() + 120
            while not release.exists():
                if time.monotonic() >= deadline:
                    os._exit(9)
                time.sleep(0.05)
            return original(paths, copy_paths, pending)

        engine.complete_audit_copy = hold_between_phases
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

        # A v5.2.0 engine creates the migrated store at its state format;
        # older engines at the memory format.
        migrated_versions = getattr(
            engine, "READABLE_STATE_VERSIONS", {engine.MEMORY_FORMAT_VERSION}
        )

        def crash_before_v4_state(paths, state):
            if (
                state.get("bimri_version") in migrated_versions
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

    repeat = int(loop_start.group(1)) if loop_start else 1
    result = 0
    for iteration in range(repeat):
        result = engine.main(["--root", str(root)] + command)
        if loop_start and result:
            print(
                f"loop iteration {iteration + 1} of {repeat} returned {result}",
                file=sys.stderr,
            )
            return result
    if graph_replay_observed is not None and not graph_replay_observed["called"]:
        print(
            "missing or invalid checkpoint bypassed global graph replay",
            file=sys.stderr,
        )
        return 119
    return result


if __name__ == "__main__":
    sys.exit(main())
