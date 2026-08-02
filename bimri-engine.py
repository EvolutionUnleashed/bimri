#!/usr/bin/env python3
"""
BIMRI Engine v5
Portable, human-governed memory for local agents.

The shared memory is a generated Markdown view. Agents work in independent
append-only run logs and submit structured proposals. The engine serializes
only short commits to shared state, detects structural conflicts, and raises
judgment to the human.

Stdlib only. Python 3.8+.

Common commands:
  start --actor codex
  journal --run R000001 --text "Decision detail"
  propose --run R000001 --tier 2 --key launch.next-step --text "..."
  close --run R000001 --outcome success --summary "..."
  resolve C000001 --choose R000002-Q001
  status
  doctor

Claude Code hooks use hook-start and hook-close. Protocol-only agents use the
same start/close commands explicitly.
"""

import argparse
import copy
import contextlib
import datetime as dt
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

try:  # pragma: no cover - platform-specific import
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - platform-specific import
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


VERSION = "5.0.1"
PREVIOUS_V5_VERSION = "5.0"
COMPATIBLE_ARTIFACT_VERSIONS = {PREVIOUS_V5_VERSION, VERSION}
RUN_RE = re.compile(r"^R\d{6}$")
ENTRY_RE = re.compile(r"^R\d{6}-E\d{3}$")
LEGACY_ENTRY_RE = re.compile(r"^R\d+-E\d+$")
MEMORY_ID_RE = re.compile(r"^(?:R\d+-E\d+|P\d+)$")
PROPOSAL_RE = re.compile(r"^R\d{6}-Q\d{3}$")
CONFLICT_RE = re.compile(r"^C\d{6}$")
PATTERN_ID_RE = re.compile(r"^P\d+$")
ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
KEY_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
LEGACY_VERSION_RE = re.compile(
    r"^<!--\s*BIMRI\s+v(?P<version>[123])(?:\.\d+)?\b[^>]*-->$",
    re.IGNORECASE,
)
LEGACY_V3_T2_RE = re.compile(
    r"^\[ID:(?P<id>T2-\d{8}-\d+)\]\s+"
    r"\[IMP:(?P<imp>[1-5])\]\s+"
    r"\[CREATED:(?P<created>\d{4}-\d{2}-\d{2})\]\s+"
    r"\[SESSION:(?P<session>\d+)\]\s+"
    r"\[LAST_USED:(?P<last_used>\d{4}-\d{2}-\d{2})\]\s+"
    r"\[LAST_USED_SESSION:(?P<last_session>\d+)\]\s+"
    r"\[TAGS:(?P<tags>[^\]]*)\]\s+"
    r"\[W:(?P<weight>\d+(?:\.\d+)?)\]$"
)
LEGACY_V1_T2_RE = re.compile(
    r"^\[IMPORTANCE:(?P<imp>[1-5])\]\s+"
    r"\[TIMESTAMP:(?P<created>\d{4}-\d{2}-\d{2})\]\s+"
    r"\[TAGS:(?P<tags>[^\]]*)\]\s+"
    r"\[WEIGHT:(?P<weight>\d+(?:\.\d+)?)\]$"
)
LEGACY_PATTERN_RE = re.compile(
    r"^\[PATTERN\]\s+"
    r"\[CONFIDENCE:(?P<confidence>EMERGING|DEVELOPING|ESTABLISHED)\]\s+"
    r"\[OBSERVATIONS:(?P<observations>\d+)\]\s+"
    r"\[TAGS:(?P<tags>[^\]]*)\]$",
    re.IGNORECASE,
)

LEGACY_ACTIVE_NAMES = ("BIMRI.md", "bimri.md")
LEGACY_BACKUP_NAMES = ("BIMRI-backup.md", "bimri-backup.md")

TRUSTS = {"working", "confirmed", "contested"}
SOURCES = {"user", "agent", "external", "system", "legacy"}
OUTCOMES = {"success", "partial", "overflow", "fail"}
OPERATIONS = {"set", "touch", "close"}
TIER1_KINDS = {"decision", "fact", "pref", "rule"}
TIER2_STATUSES = {"active", "watch", "closed"}
PATTERN_CONFIDENCE = {"emerging", "developing", "established"}
CONFLICT_TYPES = {
    "agent-declared",
    "approval",
    "capacity-or-validation",
    "confirmed-change",
    "manual-edit",
    "stale-base",
}
MAX_PATTERN_EVIDENCE = 24
MAX_SERIALIZED_ENTRY_CHARS = 4096

LIMIT_FIELDS = (
    "tier1_max",
    "tier2_max",
    "tier3_max",
    "entry_max_chars",
    "hot_max_bytes",
)
V5_0_DEFAULT_LIMITS = {
    "tier1_max": 12,
    "tier2_max": 20,
    "tier3_max": 8,
    "entry_max_chars": 500,
    "hot_max_bytes": 16384,
}
V5_0_HOT_TEMPLATE = """# BIMRI Memory

<!-- BIMRI v5 | Generated view. Do not edit directly. -->
<!-- Full history: .bimri/log/ | Revisions: .bimri/revisions/ -->

## Tier 1: Core Intelligence

<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->

## Tier 2: Active Context

<!-- Current work, risks and next actions. Cap: 20. -->

## Tier 3: Pattern Recognition

<!-- Evidence-backed patterns. Cap: 8. -->

<!-- END BIMRI -->
"""

DEFAULT_STATE = {
    "bimri_version": VERSION,
    "project_id": "unset",
    "cadence_class": "interactive",
    "run_count": 0,
    "conflict_count": 0,
    "pattern_count": 0,
    "head_revision": 0,
    "head_hash": None,
    "active_runs": {},
    "session_runs": {},
    "run_dates": {},
    "last_started_at": None,
    "last_closed_at": None,
    "prune_policy": "archive_only",
    "tier1_max": 20,
    "tier2_max": 40,
    "tier3_max": 12,
    "entry_max_chars": 500,
    "hot_max_bytes": 49152,
    "flag_threshold": 0.5,
}

DECAY_DAYS = [
    (1, 1.0), (3, 0.8), (5, 0.5), (10, 0.35),
    (15, 0.2), (20, 0.15), (10**9, 0.1),
]
DECAY_RUNS = {
    "interactive": [(1, 1.0), (3, 0.8), (5, 0.5), (10, 0.35), (10**9, 0.2)],
    "daily_cron": [(2, 1.0), (6, 0.8), (12, 0.5), (24, 0.35), (10**9, 0.2)],
    "hourly_cron": [(24, 1.0), (72, 0.8), (168, 0.5), (336, 0.35), (10**9, 0.2)],
}

HOT_TEMPLATE = """# BIMRI Memory

<!-- BIMRI v5.0.1 | Generated view. Do not edit directly. -->
<!-- Full history: .bimri/log/ | Revisions: .bimri/revisions/ -->

## Tier 1: Core Intelligence

<!-- Confirmed facts, decisions, preferences and rules. Capacity: state.json. -->

## Tier 2: Active Context

<!-- Current work, risks and next actions. Capacity: state.json. -->

## Tier 3: Pattern Recognition

<!-- Evidence-backed patterns. Capacity: state.json. -->

<!-- END BIMRI -->
"""

V5_T1_RE = re.compile(
    r"^\[(?P<id>R\d{6}-E\d{3}|R\d+-E\d+)\]\s+"
    r"\[K:(?P<key>[a-z0-9][a-z0-9.-]*)\]\s+"
    r"\[(?P<kind>decision|fact|pref|rule)\]\s+"
    r"\[T:(?P<trust>working|confirmed|contested)\]\s+"
    r"\[SRC:(?P<source>user|agent|external|system|legacy)\]\s+"
    r"\[(?P<tags>[^\]]*)\]\s+(?P<text>.+?)"
    r"(?:\s+->\s+(?P<ptr>\S+))?$"
)
V4_T1_RE = re.compile(
    r"^\[(?P<id>R\d+-E\d+)\]\s+"
    r"\[(?P<kind>decision|fact|pref|rule)\]\s+"
    r"\[(?P<tags>[^\]]*)\]\s+(?P<text>.+?)"
    r"(?:\s+->\s+(?P<ptr>\S+))?$"
)
V5_T2_RE = re.compile(
    r"^\[(?P<id>R\d{6}-E\d{3}|R\d+-E\d+)\]\s+"
    r"\[K:(?P<key>[a-z0-9][a-z0-9.-]*)\]\s+"
    r"\[I:(?P<imp>[1-5])\]\s+"
    r"\[(?P<status>active|watch|closed)\]\s+"
    r"\[T:(?P<trust>working|confirmed|contested)\]\s+"
    r"\[SRC:(?P<source>user|agent|external|system|legacy)\]\s+"
    r"\[F:(?P<first>R\d{6}|R\d+)\]\s+"
    r"\[L:(?P<last>R\d{6}|R\d+)\]\s+"
    r"\[(?P<tags>[^\]]*)\]\s+(?P<text>.+?)"
    r"(?:\s+->\s+(?P<ptr>\S+))?$"
)
V4_T2_RE = re.compile(
    r"^\[(?P<id>R\d+-E\d+)\]\s+\[I:(?P<imp>[1-5])\]\s+"
    r"\[(?P<status>active|watch|closed|decision)\]\s+"
    r"\[F:(?P<first>R\d+)\]\s+\[L:(?P<last>R\d+)\]\s+"
    r"\[(?P<tags>[^\]]*)\]\s+(?P<text>.+?)"
    r"(?:\s+->\s+(?P<ptr>\S+))?$"
)
V5_PATTERN_RE = re.compile(
    r"^\[(?P<id>P\d{4}|P\d+)\]\s+"
    r"\[K:(?P<key>[a-z0-9][a-z0-9.-]*)\]\s+"
    r"\[(?P<conf>emerging|developing|established)\]\s+"
    r"\[obs:(?P<obs>\d+)\]\s+\[ev:(?P<ev>[^\]]*)\]\s+"
    r"(?P<text>.+?)\s+\|\s+Falsify:\s+(?P<falsifier>.+)$"
)
V4_PATTERN_RE = re.compile(
    r"^\[(?P<id>P\d+)\]\s+"
    r"\[(?P<conf>emerging|developing|established)\]\s+"
    r"\[obs:(?P<obs>\d+)\]\s+\[ev:(?P<ev>[^\]]*)\]\s+"
    r"(?P<text>.+?)\s+\|\s+Falsify:\s+(?P<falsifier>.+)$"
)


class BimriError(RuntimeError):
    pass


class Paths:
    def __init__(self, root):
        self.root = Path(root).resolve()
        # Per-invocation facts for callers such as the installer. This is
        # deliberately ephemeral so an old migration is never reported as if
        # it happened during the current command.
        self.migration_receipt = None
        self.bdir = self.root / ".bimri"
        self.hot = self.root / "bimri.md"
        self.state = self.bdir / "state.json"
        self.index = self.bdir / "index.tsv"
        self.lock = self.bdir / "engine.lock"
        self.logs = self.bdir / "log"
        self.proposals = self.bdir / "proposals"
        self.decisions = self.bdir / "decisions"
        self.revisions = self.bdir / "revisions"
        self.conflicts = self.bdir / "conflicts"
        self.resolutions = self.bdir / "resolutions"
        self.archive = self.bdir / "archive"
        self.inbox = self.bdir / "inbox"
        self.backups = self.bdir / "backups"
        self.recovery = self.bdir / "recovery"
        self.migrations = self.bdir / "migrations"

    @property
    def legacy_active(self):
        return tuple(self.root / name for name in LEGACY_ACTIVE_NAMES)

    @property
    def legacy_backups(self):
        return tuple(self.root / name for name in LEGACY_BACKUP_NAMES)

    @property
    def dirs(self):
        return (
            self.bdir, self.logs, self.proposals, self.decisions,
            self.revisions, self.conflicts, self.resolutions, self.archive,
            self.inbox, self.backups, self.recovery, self.migrations,
        )


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return dt.date.today().isoformat()


def parse_timestamp(value, name):
    normalized = clean_scalar(value, name, 30)
    if normalized != value:
        raise BimriError(f"{name} must already be normalized.")
    try:
        parsed = dt.datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise BimriError(f"{name} is invalid.") from exc
    return parsed.replace(tzinfo=dt.timezone.utc)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(content):
    return hashlib.sha256(content).hexdigest()


def line_hash(text):
    return sha256_text(text.strip())


def clean_scalar(value, name, max_chars=500, allow_empty=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BimriError(f"{name} must be text.")
    if not allow_empty and not value.strip():
        raise BimriError(f"{name} cannot be empty.")
    if len(value) > max_chars:
        raise BimriError(f"{name} exceeds {max_chars} characters.")
    if any(ch in value for ch in ("\n", "\r", "\t")):
        raise BimriError(f"{name} must be one line without tabs.")
    if any(ord(ch) < 32 for ch in value):
        raise BimriError(f"{name} contains control characters.")
    return value.strip()


def clean_actor(value):
    value = clean_scalar(value or "agent", "actor", 64).lower()
    if not ACTOR_RE.fullmatch(value):
        raise BimriError("actor must be a lowercase slug.")
    return value


def clean_key(value):
    value = clean_scalar(value, "key", 80).lower()
    if not KEY_RE.fullmatch(value):
        raise BimriError(
            "key must use lowercase dotted or hyphenated words, "
            "for example launch.price."
        )
    return value


def clean_tags(value):
    if not value:
        return []
    raw = value if isinstance(value, list) else value.split(",")
    tags = []
    for tag in raw:
        tag = clean_scalar(str(tag), "tag", 40).lower()
        if not TAG_RE.fullmatch(tag):
            raise BimriError(f"invalid tag: {tag}")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > 12:
        raise BimriError("a memory entry may have at most 12 unique tags.")
    return tags


def validate_fixed_id(value, regex, name):
    value = clean_scalar(value, name, 80)
    if not regex.fullmatch(value):
        raise BimriError(f"invalid {name}: {value}")
    return value


def ensure_layout(paths):
    if paths.bdir.exists() and paths.bdir.is_symlink():
        raise BimriError(".bimri cannot be a symbolic link.")
    paths.root.mkdir(parents=True, exist_ok=True)
    for directory in paths.dirs:
        if directory.exists() and directory.is_symlink():
            raise BimriError(f"{directory.relative_to(paths.root)} cannot be a symbolic link.")
        existed = directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if not existed:
            fsync_directory(directory.parent)
    if paths.hot.is_symlink():
        raise BimriError("bimri.md cannot be a symbolic link.")
    if paths.state.is_symlink():
        raise BimriError(".bimri/state.json cannot be a symbolic link.")
    if paths.index.is_symlink():
        raise BimriError(".bimri/index.tsv cannot be a symbolic link.")
    if paths.lock.is_symlink():
        raise BimriError(".bimri/engine.lock cannot be a symbolic link.")
    if not paths.lock.exists():
        try:
            fd = os.open(
                str(paths.lock),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(paths.lock.parent)
        except FileExistsError:
            pass


def fsync_directory(directory):
    if os.name != "posix":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def ensure_directory_durable(directory):
    directory = Path(directory)
    missing = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        fsync_directory(created.parent)


def atomic_write_text(path, content):
    path = Path(path)
    ensure_directory_durable(path.parent)
    if path.exists() and path.is_symlink():
        raise BimriError(f"refusing to replace symbolic link: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=".bimri-tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        fsync_directory(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def atomic_write_json(path, data):
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def atomic_copy_file(source, destination):
    source = Path(source)
    destination = Path(destination)
    ensure_directory_durable(destination.parent)
    if destination.exists() and destination.is_symlink():
        raise BimriError(f"refusing to restore through symbolic link: {destination}")
    fd, temp_name = tempfile.mkstemp(
        prefix=".bimri-tmp-", dir=str(destination.parent)
    )
    try:
        with source.open("rb") as source_handle, os.fdopen(
            fd, "wb"
        ) as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        os.replace(temp_name, destination)
        with contextlib.suppress(OSError):
            os.chmod(destination, source.stat().st_mode & 0o777)
        fsync_directory(destination.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def write_generated_view(paths, content, warn_only=False):
    last_error = None
    for attempt in range(5):
        try:
            atomic_write_text(paths.hot, content)
            return True
        except PermissionError as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(0.05 * (2 ** attempt))
        except Exception as exc:
            if not warn_only:
                raise
            last_error = exc
            break
    detail = (
        "another program may be holding the file open"
        if isinstance(last_error, PermissionError)
        else "the generated-view write failed"
    )
    message = (
        f"bimri.md could not be refreshed because {detail}. The accepted "
        "revision and state are durable; the next engine command will retry "
        f"the generated view. Last error: {last_error}"
    )
    if warn_only:
        print(f"BIMRI WARNING: {message}", file=sys.stderr)
        return False
    raise BimriError(message)


def exclusive_write_bytes(path, content):
    path = Path(path)
    ensure_directory_durable(path.parent)
    if path.exists() or path.is_symlink():
        raise FileExistsError(str(path))
    fd, temp_name = tempfile.mkstemp(
        prefix=".bimri-new-", dir=str(path.parent)
    )
    installed = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            raise
        except (AttributeError, NotImplementedError, OSError):
            if path.exists() or path.is_symlink():
                raise FileExistsError(str(path))
            os.replace(temp_name, path)
            installed = True
        else:
            installed = True
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
        fsync_directory(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        if installed:
            with contextlib.suppress(OSError):
                path.unlink()
        raise


def exclusive_write_text(path, content):
    return exclusive_write_bytes(path, content.encode("utf-8"))


def append_line(path, line):
    clean_scalar(line, "journal line", 12000)
    path = Path(path)
    if path.is_symlink():
        raise BimriError(f"refusing to append through symbolic link: {path}")
    created = not path.exists()
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line.rstrip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        fsync_directory(path.parent)


@contextlib.contextmanager
def engine_lock(paths, timeout=10.0):
    preflight_legacy_source(paths)
    ensure_layout(paths)
    handle = paths.lock.open("r+b")
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while time.monotonic() < deadline:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover
                    raise BimriError("this platform has no supported file lock.")
                locked = True
                break
            except (BlockingIOError, OSError):
                time.sleep(0.02 + random.random() * 0.03)
        if not locked:
            raise BimriError("BIMRI is busy. Retry the command.")
        yield
    finally:
        if locked:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def read_json_strict(path, label):
    path = Path(path)
    if path.is_symlink():
        raise BimriError(f"{label} cannot be a symbolic link.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise BimriError(f"{label} is unreadable; BIMRI stopped without resetting it: {exc}")
    if not isinstance(data, dict):
        raise BimriError(f"{label} must contain a JSON object.")
    return data


def limits_profile(state):
    return {key: state[key] for key in LIMIT_FIELDS}


def record_migration_receipt(paths, action, source_version=None, **details):
    receipt = {
        "action": action,
        "source_version": source_version,
        "target_version": VERSION,
    }
    receipt.update(details)
    paths.migration_receipt = receipt
    return receipt


def validate_state(state, accepted_versions=None):
    required_ints = (
        "run_count", "conflict_count", "pattern_count", "head_revision",
        "tier1_max", "tier2_max", "tier3_max",
        "entry_max_chars", "hot_max_bytes",
    )
    for key in required_ints:
        if (
            isinstance(state.get(key), bool)
            or not isinstance(state.get(key), int)
            or state[key] < 0
        ):
            raise BimriError(f"state field {key} must be a non-negative integer.")
    for key in ("run_count", "conflict_count", "head_revision"):
        if state[key] > 999999:
            raise BimriError(f"state field {key} exceeds its six-digit ID space.")
    accepted_versions = accepted_versions or {VERSION}
    if state.get("bimri_version") not in accepted_versions:
        raise BimriError(f"unsupported BIMRI state version: {state.get('bimri_version')}")
    if state.get("legacy_migration") not in (None, "legacy-to-v5"):
        raise BimriError("state legacy_migration has an unsupported value.")
    if not isinstance(state.get("active_runs"), dict):
        raise BimriError("state active_runs must be an object.")
    if not isinstance(state.get("session_runs"), dict):
        raise BimriError("state session_runs must be an object.")
    if not isinstance(state.get("run_dates"), dict):
        raise BimriError("state run_dates must be an object.")
    if state.get("cadence_class") not in DECAY_RUNS:
        raise BimriError("state cadence_class is invalid.")
    if state.get("prune_policy") != "archive_only":
        raise BimriError("state prune_policy must be archive_only.")
    for key in ("flag_threshold",):
        if not isinstance(state.get(key), (int, float)) or state[key] < 0:
            raise BimriError(f"state field {key} must be a non-negative number.")
    if state["entry_max_chars"] < 1 or state["hot_max_bytes"] < 1:
        raise BimriError("state text and byte caps must be positive.")
    for rid, meta in state["active_runs"].items():
        validate_fixed_id(rid, RUN_RE, "run ID")
        if not isinstance(meta, dict):
            raise BimriError(f"active run {rid} metadata is invalid.")
        clean_actor(meta.get("actor"))
        if (
            isinstance(meta.get("base_revision"), bool)
            or not isinstance(meta.get("base_revision"), int)
            or meta["base_revision"] < 0
        ):
            raise BimriError(f"active run {rid} base revision is invalid.")
    for skey, rid in state["session_runs"].items():
        clean_scalar(skey, "session key", 100)
        validate_fixed_id(rid, RUN_RE, "session run ID")
    for rid, date_value in state["run_dates"].items():
        validate_fixed_id(rid, re.compile(r"^R\d+$"), "dated run ID")
        try:
            dt.date.fromisoformat(clean_scalar(date_value, "run date", 10))
        except ValueError as exc:
            raise BimriError(f"run date for {rid} is invalid.") from exc
    head_hash = state.get("head_hash")
    if head_hash is not None and not HASH_RE.fullmatch(str(head_hash)):
        raise BimriError("state head_hash is invalid.")
    return state


def save_state(paths, state):
    validate_state(state)
    if len(state.get("run_dates", {})) > 500:
        keys = sorted(state["run_dates"], key=lambda value: int(re.search(r"\d+", value).group()))
        state["run_dates"] = {key: state["run_dates"][key] for key in keys[-500:]}
    atomic_write_json(paths.state, state)


def backup_file(paths, path, label):
    if not Path(path).exists():
        return None
    name = f"{label}-{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    destination = paths.backups / name
    atomic_copy_file(path, destination)
    return destination


def _legacy_version_from_text(text):
    versions = []
    for line in text.splitlines():
        match = LEGACY_VERSION_RE.fullmatch(line.strip())
        if match:
            versions.append(int(match.group("version")))
    if len(versions) != 1:
        return None
    return versions[0]


def _legacy_date(value, label):
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise BimriError(f"{label} is invalid: {value}") from exc


def _legacy_claim_text(line, label):
    stripped = line.strip()
    bullet = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)(?P<text>.+)$", stripped)
    if bullet:
        stripped = bullet.group("text").strip()
    if (
        not stripped
        or stripped.startswith(("#", "<!--", "```", "|", ">"))
        or (stripped.startswith("[") and "]" in stripped)
    ):
        raise BimriError(f"{label} is not a parseable one-line memory claim.")
    if re.search(r"\s+->\s+", stripped):
        raise BimriError(
            f"{label} contains the reserved v5 pointer delimiter ' -> '; "
            "rewrite that claim explicitly before migration."
        )
    # Preserve inherited claims above the v5 authoring cap. The converted
    # serialized entry remains subject to MAX_SERIALIZED_ENTRY_CHARS.
    return clean_scalar(stripped, label, MAX_SERIALIZED_ENTRY_CHARS)


def _legacy_tags(value, label):
    try:
        return clean_tags(value)
    except BimriError as exc:
        raise BimriError(f"{label} has invalid tags: {exc}") from exc


def parse_legacy_hot(content, label="legacy BIMRI memory"):
    """Parse the explicitly versioned v1-v3 flat-file grammar without inference."""
    version = _legacy_version_from_text(content)
    if version is None:
        raise BimriError(
            f"{label} must contain exactly one BIMRI v1, v2, or v3 header."
        )
    lines = content.splitlines()
    headings = []
    tier = 0
    sections = {1: [], 2: [], 3: []}
    header_line = next(
        line.strip() for line in lines
        if LEGACY_VERSION_RE.fullmatch(line.strip())
    )
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        low = stripped.lower()
        if low == "## tier 1: core intelligence":
            tier = 1
            headings.append(1)
            continue
        if low == "## tier 2: active context":
            tier = 2
            headings.append(2)
            continue
        if low == "## tier 3: pattern recognition":
            tier = 3
            headings.append(3)
            continue
        if not stripped or stripped.startswith("<!--"):
            continue
        if tier == 0 and low == "# bimri: memory file":
            continue
        if tier == 0:
            raise BimriError(
                f"{label} line {number} contains unparseable content outside a tier."
            )
        if stripped.startswith("## "):
            raise BimriError(
                f"{label} line {number} contains an unknown tier heading."
            )
        sections[tier].append((number, stripped))
    if headings != [1, 2, 3]:
        raise BimriError(
            f"{label} must contain exactly one ordered Tier 1, Tier 2 and Tier 3 heading."
        )

    claims = []
    seen_source_ids = set()

    def add_claim(source_id, source_tier, text, importance, tags, status, metadata):
        if source_id in seen_source_ids:
            raise BimriError(f"{label} contains duplicate source ID {source_id}.")
        seen_source_ids.add(source_id)
        claims.append({
            "source_id": source_id,
            "source_tier": source_tier,
            "text": text,
            "importance": importance,
            "tags": tags,
            "status": status,
            "metadata": metadata,
        })

    tier1_index = 0
    cursor = 0
    tier1 = sections[1]
    while cursor < len(tier1):
        number, line = tier1[cursor]
        tier1_index += 1
        meta = LEGACY_V3_T2_RE.fullmatch(line) or LEGACY_V1_T2_RE.fullmatch(line)
        if meta:
            if cursor + 1 >= len(tier1):
                raise BimriError(f"{label} line {number} has no following memory claim.")
            text_number, text_line = tier1[cursor + 1]
            text = _legacy_claim_text(text_line, f"{label} line {text_number}")
            data = meta.groupdict()
            source_id = data.get("id") or f"T1-{tier1_index:04d}"
            _legacy_date(data["created"], f"{label} line {number} date")
            if data.get("last_used"):
                _legacy_date(data["last_used"], f"{label} line {number} last-used date")
            tags = _legacy_tags(data.get("tags", ""), f"{label} line {number}")
            add_claim(source_id, 1, text, int(data["imp"]), tags, "active", data)
            cursor += 2
        else:
            text = _legacy_claim_text(line, f"{label} line {number}")
            add_claim(
                f"T1-{tier1_index:04d}", 1, text, 5, [], "active",
                {"line": number},
            )
            cursor += 1

    cursor = 0
    tier2 = sections[2]
    tier2_ordinal = 0
    while cursor < len(tier2):
        number, line = tier2[cursor]
        meta = LEGACY_V3_T2_RE.fullmatch(line) or LEGACY_V1_T2_RE.fullmatch(line)
        if not meta:
            raise BimriError(
                f"{label} line {number} is not a recognized v1-v3 Tier 2 metadata line."
            )
        if cursor + 1 >= len(tier2):
            raise BimriError(f"{label} line {number} has no following memory claim.")
        text_number, text_line = tier2[cursor + 1]
        text = _legacy_claim_text(text_line, f"{label} line {text_number}")
        data = meta.groupdict()
        _legacy_date(data["created"], f"{label} line {number} date")
        if data.get("last_used"):
            _legacy_date(data["last_used"], f"{label} line {number} last-used date")
        tags = _legacy_tags(data.get("tags", ""), f"{label} line {number}")
        tier2_ordinal += 1
        source_id = data.get("id") or f"T2-LEGACY-{tier2_ordinal:04d}"
        add_claim(source_id, 2, text, int(data["imp"]), tags, "active", data)
        cursor += 2

    cursor = 0
    tier3 = sections[3]
    pattern_ordinal = 0
    while cursor < len(tier3):
        number, line = tier3[cursor]
        meta = LEGACY_PATTERN_RE.fullmatch(line)
        if not meta:
            raise BimriError(
                f"{label} line {number} is not a recognized v1-v3 Tier 3 pattern line."
            )
        if cursor + 1 >= len(tier3):
            raise BimriError(f"{label} line {number} has no following pattern claim.")
        text_number, text_line = tier3[cursor + 1]
        text = _legacy_claim_text(text_line, f"{label} line {text_number}")
        data = meta.groupdict()
        observations = int(data["observations"])
        if observations < 1:
            raise BimriError(f"{label} line {number} observations must be positive.")
        tags = _legacy_tags(data.get("tags", ""), f"{label} line {number}")
        pattern_ordinal += 1
        add_claim(
            f"PATTERN-{pattern_ordinal:04d}", 3, text, 3, tags, "watch", data
        )
        cursor += 2

    sessions_match = re.search(r"\bSessions:\s*(\d+)\b", header_line, re.IGNORECASE)
    sessions = int(sessions_match.group(1)) if sessions_match else 0
    if sessions > 999999:
        raise BimriError(f"{label} session count exceeds the v5 run ID space.")
    return {
        "version": version,
        "sessions": sessions,
        "claims": claims,
    }


def _read_legacy_file(path, role):
    if path.is_symlink():
        raise BimriError(f"{path.name} cannot be a symbolic link during migration.")
    if not path.is_file():
        raise BimriError(f"{path.name} must be a regular file during migration.")
    content_bytes = path.read_bytes()
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BimriError(
            f"{path.name} is not valid UTF-8; BIMRI stopped without mutation."
        ) from exc
    parsed = parse_legacy_hot(content, f"{role} {path.name}")
    return {
        "path": path,
        "relative": path.name,
        "bytes": content_bytes,
        "sha256": sha256_bytes(content_bytes),
        "parsed": parsed,
        "role": role,
    }


def _lower_hot_is_v4_or_v5(paths, path):
    if path.name != "bimri.md":
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    if "BIMRI v5" in content:
        _, _, errors, _ = validate_hot_content(
            content, fresh_state(), allow_legacy_overflow=True
        )
        return not errors
    return looks_like_v4_hot(content)


def _actual_legacy_root_paths(paths, names):
    if not paths.root.exists() or not paths.root.is_dir():
        return []
    wanted = {name.casefold() for name in names}
    return sorted(
        (entry for entry in paths.root.iterdir() if entry.name.casefold() in wanted),
        key=lambda entry: entry.name,
    )


def _legacy_root_role(name):
    folded = name.casefold()
    if folded == "bimri.md":
        return "active memory"
    if folded == "bimri-backup.md":
        return "rolling backup"
    return None


def discover_legacy_migration(paths):
    """Return a pure, validated v1-v3 migration plan or None."""
    # v1-v3 never owned structured state.  Any existing state authority must
    # route through the normal strict v4/v5 loader and can never authorize a
    # flat-file reset, even when it is malformed or declares an unknown schema.
    if paths.state.exists() or paths.state.is_symlink():
        return None

    active_paths = _actual_legacy_root_paths(paths, LEGACY_ACTIVE_NAMES)
    backup_paths = _actual_legacy_root_paths(paths, LEGACY_BACKUP_NAMES)
    if not active_paths:
        if backup_paths:
            names = ", ".join(path.name for path in backup_paths)
            raise BimriError(
                "legacy BIMRI rolling backup exists without active memory "
                f"({names}); BIMRI stopped without mutation because recovery is ambiguous."
            )
        return None
    if len(active_paths) == 1 and active_paths[0].name == "bimri.md":
        if _lower_hot_is_v4_or_v5(paths, active_paths[0]):
            return None
        try:
            lower_text = active_paths[0].read_text(encoding="utf-8")
        except UnicodeDecodeError:
            lower_text = None
        except OSError as exc:
            raise BimriError(f"could not inspect bimri.md safely: {exc}") from exc
        if (
            lower_text is not None
            and _legacy_version_from_text(lower_text) is None
            and not backup_paths
        ):
            return None

    active = [_read_legacy_file(path, "active memory") for path in active_paths]
    if len(active) > 1 and any(
        item["bytes"] != active[0]["bytes"] for item in active[1:]
    ):
        raise BimriError(
            "multiple case variants of BIMRI.md differ; BIMRI stopped without mutation. "
            "Choose the authoritative legacy memory before retrying migration."
        )
    if len(active) > 1 and any(
        item["parsed"]["version"] != active[0]["parsed"]["version"]
        for item in active[1:]
    ):
        raise BimriError("dual-case legacy active files declare different versions.")

    if len(active) == 1:
        matching_name = active[0]["path"].stem + "-backup.md"
        mismatched = [path for path in backup_paths if path.name != matching_name]
        if mismatched:
            raise BimriError(
                f"{mismatched[0].name} does not match active {active[0]['relative']}; "
                "BIMRI stopped without mutation because backup provenance is ambiguous."
            )
    backups = [_read_legacy_file(path, "rolling backup") for path in backup_paths]
    primary = next(
        (item for item in active if item["relative"] == "bimri.md"), active[0]
    )
    return {
        "primary": primary,
        "active": active,
        "backups": backups,
        "parsed": primary["parsed"],
    }


def preflight_legacy_source(paths):
    """Reject ambiguous or malformed legacy files before creating runtime files."""
    discover_legacy_migration(paths)


def _legacy_key(version, source_id, ordinal):
    slug = source_id.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if not slug:
        slug = f"entry-{ordinal:04d}"
    key = f"legacy.v{version}.{slug}"
    if len(key) > 80:
        key = f"legacy.v{version}.entry-{ordinal:04d}-{sha256_text(source_id)[:12]}"
    return clean_key(key)


def legacy_import_summary(parsed):
    claims = parsed["claims"]
    return {
        "claims_imported": len(claims),
        "tier1_imported": sum(
            claim["source_tier"] == 1 for claim in claims
        ),
        "tier2_imported": sum(
            claim["source_tier"] in {2, 3} for claim in claims
        ),
        "patterns_converted_to_watches": sum(
            claim["source_tier"] == 3 for claim in claims
        ),
        "inherited_overlength_claims": sum(
            len(claim["text"]) > DEFAULT_STATE["entry_max_chars"]
            for claim in claims
        ),
        "longest_claim_chars": max(
            (len(claim["text"]) for claim in claims), default=0
        ),
    }


def convert_legacy_hot(plan, template=None, historical_spacing=False):
    parsed = plan["parsed"]
    tier1_lines = []
    tier2_lines = []
    id_map = []
    seen_keys = set()
    for ordinal, claim in enumerate(parsed["claims"], 1):
        target_id = f"R0-E{ordinal}"
        key = _legacy_key(parsed["version"], claim["source_id"], ordinal)
        if key in seen_keys:
            key = f"legacy.v{parsed['version']}.entry-{ordinal:04d}-{sha256_text(claim['source_id'])[:8]}"
            key = clean_key(key)
        seen_keys.add(key)
        tags = ",".join(claim["tags"])
        if claim["source_tier"] == 1:
            line = (
                f"[{target_id}] [K:{key}] [fact] [T:working] "
                f"[SRC:legacy] [{tags}] {claim['text']}"
            )
            tier1_lines.append(line)
            target_tier = 1
        else:
            status = "watch" if claim["source_tier"] == 3 else "active"
            line = (
                f"[{target_id}] [K:{key}] [I:{claim['importance']}] [{status}] "
                f"[T:working] [SRC:legacy] [F:R0] [L:R0] [{tags}] {claim['text']}"
            )
            tier2_lines.append(line)
            target_tier = 2
        id_map.append({
            "source_id": claim["source_id"],
            "source_tier": claim["source_tier"],
            "target_id": target_id,
            "target_key": key,
            "target_tier": target_tier,
            "source_text_sha256": sha256_text(claim["text"]),
        })
    lines = (template or HOT_TEMPLATE).splitlines()
    insert_lines_in_tier(
        lines, 1, tier1_lines, leading_blank=historical_spacing
    )
    insert_lines_in_tier(
        lines, 2, tier2_lines, leading_blank=historical_spacing
    )
    return render_content(lines), id_map


def _legacy_backup_destination(paths, asset):
    safe_name = asset["relative"].replace("/", "-")
    return paths.backups / f"legacy-{safe_name}-{asset['sha256']}.bin"


def _preserve_legacy_asset(paths, asset):
    current = asset["path"].read_bytes()
    if sha256_bytes(current) != asset["sha256"]:
        raise BimriError(
            f"{asset['relative']} changed while migration was preparing; retry when quiescent."
        )
    destination = _legacy_backup_destination(paths, asset)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != asset["bytes"]:
            raise BimriError(
                f"legacy backup destination conflicts with {asset['relative']}."
            )
    else:
        exclusive_write_bytes(destination, asset["bytes"])
    return destination


def _validate_legacy_marker(paths, marker, state=None):
    if marker.get("migration") != "legacy-to-v5":
        raise BimriError("legacy migration marker has an invalid migration type.")
    if marker.get("source_version") not in {1, 2, 3} or not HASH_RE.fullmatch(
        str(marker.get("source_hot_hash", ""))
    ):
        raise BimriError("legacy migration marker has invalid source authority.")
    if marker.get("revision") != "V000000" or not HASH_RE.fullmatch(
        str(marker.get("revision_sha256", ""))
    ):
        raise BimriError("legacy migration marker has invalid revision authority.")
    try:
        parse_timestamp(marker.get("completed_at"), "legacy migration completion time")
    except BimriError as exc:
        raise BimriError("legacy migration marker has an invalid completion time.") from exc
    revision = revision_path(paths, 0)
    if not revision.is_file() or revision.is_symlink():
        raise BimriError("legacy migration marker exists but V000000 is missing or unsafe.")
    revision_bytes = revision.read_bytes()
    if sha256_bytes(revision_bytes) != marker["revision_sha256"]:
        raise BimriError("legacy migration V000000 does not match its durable marker.")
    assets = marker.get("assets")
    if not isinstance(assets, list) or not assets:
        raise BimriError("legacy migration marker must contain preserved assets.")
    seen_sources = set()
    active_hashes = set()
    validated_assets = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise BimriError("legacy migration marker contains an invalid asset record.")
        backup_rel = asset.get("backup_path")
        source_path = asset.get("source_path")
        digest = str(asset.get("sha256", ""))
        byte_length = asset.get("byte_length")
        if (
            not isinstance(backup_rel, str)
            or not isinstance(source_path, str)
            or Path(source_path).name != source_path
            or _legacy_root_role(source_path) is None
            or not HASH_RE.fullmatch(digest)
            or isinstance(byte_length, bool)
            or not isinstance(byte_length, int)
            or byte_length < 0
            or asset.get("source_version") not in {1, 2, 3}
        ):
            raise BimriError("legacy migration marker contains invalid backup authority.")
        if source_path in seen_sources:
            raise BimriError("legacy migration marker contains duplicate source paths.")
        seen_sources.add(source_path)
        if asset.get("role") != _legacy_root_role(source_path):
            raise BimriError(
                "legacy migration marker asset role does not match its source path."
            )
        if asset.get("role") == "active memory":
            active_hashes.add(digest)
        elif asset.get("role") != "rolling backup":
            raise BimriError("legacy migration marker contains an invalid asset role.")
        expected_name = f"legacy-{source_path}-{digest}.bin"
        expected_rel = (Path(".bimri") / "backups" / expected_name).as_posix()
        if backup_rel != expected_rel:
            raise BimriError(
                "legacy migration backup path is not the deterministic direct "
                ".bimri/backups authority."
            )
        backup = paths.backups / expected_name
        if not backup.is_file() or backup.is_symlink():
            raise BimriError(f"legacy migration backup is missing or damaged: {backup_rel}")
        backup_bytes = backup.read_bytes()
        if len(backup_bytes) != byte_length or sha256_bytes(backup_bytes) != digest:
            raise BimriError(f"legacy migration backup is missing or damaged: {backup_rel}")
        try:
            backup_text = backup_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BimriError(
                f"legacy migration backup is not valid UTF-8: {backup_rel}"
            ) from exc
        parsed = parse_legacy_hot(
            backup_text, f"preserved {asset['role']} {source_path}"
        )
        if parsed["version"] != asset["source_version"]:
            raise BimriError(
                "legacy migration marker asset version does not match its backup."
            )
        validated_assets.append({
            "role": asset["role"],
            "source_path": source_path,
            "sha256": digest,
            "parsed": parsed,
        })
    if not active_hashes or active_hashes != {marker["source_hot_hash"]}:
        raise BimriError("legacy migration marker source hash has no active asset.")
    authoritative = next(
        asset for asset in validated_assets if asset["role"] == "active memory"
    )
    if marker["source_version"] != authoritative["parsed"]["version"]:
        raise BimriError(
            "legacy migration marker source version does not match its active backup."
        )
    id_map = marker.get("id_map")
    if not isinstance(id_map, list):
        raise BimriError("legacy migration marker ID map must be a list.")
    seen_target_ids = set()
    seen_target_keys = set()
    for mapping in id_map:
        if not isinstance(mapping, dict):
            raise BimriError("legacy migration marker contains an invalid ID mapping.")
        target_id = mapping.get("target_id")
        target_key = mapping.get("target_key")
        if (
            not isinstance(target_id, str)
            or not LEGACY_ENTRY_RE.fullmatch(target_id)
            or target_id in seen_target_ids
            or not isinstance(target_key, str)
            or target_key in seen_target_keys
            or mapping.get("source_tier") not in {1, 2, 3}
            or mapping.get("target_tier") not in {1, 2}
            or not HASH_RE.fullmatch(str(mapping.get("source_text_sha256", "")))
        ):
            raise BimriError("legacy migration marker contains an invalid ID mapping.")
        try:
            clean_key(target_key)
        except BimriError as exc:
            raise BimriError("legacy migration marker contains an invalid ID mapping.") from exc
        seen_target_ids.add(target_id)
        seen_target_keys.add(target_key)
    conversion_version = marker.get("converter_version")
    if conversion_version not in {None, PREVIOUS_V5_VERSION, VERSION}:
        raise BimriError(
            "legacy migration marker has an unsupported converter version."
        )
    templates = {
        PREVIOUS_V5_VERSION: V5_0_HOT_TEMPLATE,
        VERSION: HOT_TEMPLATE,
    }
    candidate_versions = (
        (PREVIOUS_V5_VERSION, VERSION)
        if conversion_version is None
        else (conversion_version,)
    )
    expected_id_map = None
    matching_conversion = None
    for candidate_version in candidate_versions:
        expected_revision, candidate_id_map = convert_legacy_hot(
            {"parsed": authoritative["parsed"]},
            template=templates[candidate_version],
            historical_spacing=(candidate_version == PREVIOUS_V5_VERSION),
        )
        expected_id_map = candidate_id_map
        if revision_bytes == expected_revision.encode("utf-8"):
            matching_conversion = candidate_version
            break
    if matching_conversion is None:
        raise BimriError(
            "legacy migration revision is not the deterministic conversion of "
            "its preserved active source."
        )
    if id_map != expected_id_map:
        raise BimriError(
            "legacy migration ID map is not the deterministic source-to-v5 mapping."
        )
    receipt = marker.get("receipt")
    if receipt is not None:
        expected_receipt = legacy_import_summary(authoritative["parsed"])
        if not isinstance(receipt, dict):
            raise BimriError("legacy migration receipt must be an object.")
        active_sources = {
            asset["source_path"] for asset in validated_assets
            if asset["role"] == "active memory"
        }
        if receipt.get("source_file") not in active_sources:
            raise BimriError("legacy migration receipt source file is invalid.")
        if receipt.get("source_version") != authoritative["parsed"]["version"]:
            raise BimriError("legacy migration receipt source version is invalid.")
        for key, value in expected_receipt.items():
            if receipt.get(key) != value:
                raise BimriError("legacy migration receipt counts are invalid.")
    if state is not None and state.get("legacy_migration") != "legacy-to-v5":
        raise BimriError("v5 state does not claim its legacy migration authority.")
    if state is not None and state.get("head_revision") == 0:
        if state.get("head_hash") != marker["revision_sha256"]:
            raise BimriError("v5 state does not match the legacy migration revision.")
    return revision_bytes


def _validate_legacy_runtime_namespace(paths, plan):
    """Allow only deterministic retry artifacts when legacy state is absent."""
    expected_backups = {
        _legacy_backup_destination(paths, asset).relative_to(paths.root).as_posix()
        for asset in plan["active"] + plan["backups"]
    }
    allowed = {
        ".bimri/engine.lock",
        ".bimri/revisions/V000000.md",
        ".bimri/migrations/legacy-to-v5.json",
        *expected_backups,
    }
    unexpected = sorted(collect_bimri_files(paths) - allowed)
    if unexpected:
        raise BimriError(
            "state.json is missing while unrelated BIMRI history exists: "
            + ", ".join(unexpected)
            + ". BIMRI stopped to avoid orphaning or resetting history."
        )
    marker = paths.migrations / "legacy-to-v5.json"
    revision = revision_path(paths, 0)
    if marker.exists() and not revision.is_file():
        raise BimriError(
            "legacy migration marker exists without V000000; restore the "
            "matching revision or recovery copy before retrying."
        )


def _retire_legacy_sources(paths, marker, revision_bytes):
    revision_hash = sha256_bytes(revision_bytes)

    def aliases_hot(source):
        if source.name.lower() != "bimri.md" or not source.exists() or not paths.hot.exists():
            return source.name == "bimri.md"
        try:
            return os.path.samefile(str(source), str(paths.hot))
        except OSError:
            return source.name == "bimri.md"

    def verify_all(after_view):
        for asset in marker.get("assets", []):
            relative = asset["source_path"]
            if _legacy_root_role(relative) is None or Path(relative).name != relative:
                raise BimriError("legacy migration source path is invalid.")
            source = paths.root / relative
            if not (source.exists() or source.is_symlink()):
                continue
            if source.is_symlink() or not source.is_file():
                raise BimriError(f"cannot retire unsafe legacy source {relative}.")
            digest = sha256_bytes(source.read_bytes())
            expected = {asset["sha256"]}
            if aliases_hot(source):
                expected.add(revision_hash)
                if after_view:
                    expected = {revision_hash}
            if digest not in expected:
                raise BimriError(
                    f"legacy source {relative} changed before retirement; "
                    "preserved backup remains authoritative."
                )

    # Verify the entire legacy source set before replacing even the generated
    # view.  This avoids a half-retired workspace if an old writer was active.
    verify_all(after_view=False)
    write_generated_view(paths, revision_bytes.decode("utf-8"))
    verify_all(after_view=True)

    for asset in marker.get("assets", []):
        relative = asset["source_path"]
        source = paths.root / relative
        if not (source.exists() or source.is_symlink()):
            continue
        if aliases_hot(source):
            # On a case-insensitive filesystem an uppercase source and the v5
            # lowercase generated view are the same physical file.  Force the
            # directory entry to canonical lowercase with a two-step rename.
            actual = next(
                (
                    item for item in _actual_legacy_root_paths(paths, LEGACY_ACTIVE_NAMES)
                    if os.path.samefile(str(item), str(paths.hot))
                ),
                paths.hot,
            )
            if actual.name != "bimri.md":
                temporary = paths.root / f".bimri-case-{uuid.uuid4().hex}"
                os.replace(str(actual), str(temporary))
                os.replace(str(temporary), str(paths.hot))
                fsync_directory(paths.root)
            continue
        if sha256_bytes(source.read_bytes()) != asset["sha256"]:
            raise BimriError(
                f"legacy source {relative} changed during retirement; retry safely."
            )
        source.unlink()
        fsync_directory(source.parent)


def finalize_legacy_migration(paths, state):
    marker_path = paths.migrations / "legacy-to-v5.json"
    if not marker_path.exists():
        if state.get("legacy_migration") == "legacy-to-v5":
            raise BimriError(
                "v5 state requires legacy-to-v5.json, but the durable migration "
                "marker is missing. Restore it from recovery before continuing."
            )
        return
    if state.get("legacy_migration") != "legacy-to-v5":
        raise BimriError(
            "legacy-to-v5.json exists, but v5 state does not claim that migration."
        )
    marker = read_json_strict(marker_path, marker_path.name)
    revision_bytes = _validate_legacy_marker(paths, marker, state)
    if state["head_revision"] != 0:
        return
    actual = {
        path.name: path
        for path in _actual_legacy_root_paths(
            paths, LEGACY_ACTIVE_NAMES + LEGACY_BACKUP_NAMES
        )
    }
    retirement_needed = False
    for asset in marker["assets"]:
        source = actual.get(asset["source_path"])
        if source is None:
            continue
        if asset["source_path"] == "bimri.md":
            if sha256_bytes(source.read_bytes()) == asset["sha256"]:
                retirement_needed = True
        else:
            retirement_needed = True
    if retirement_needed:
        _retire_legacy_sources(paths, marker, revision_bytes)


def migrate_legacy(paths, plan):
    converted, id_map = convert_legacy_hot(plan)
    conversion_version = VERSION
    _validate_legacy_runtime_namespace(paths, plan)
    revision = revision_path(paths, 0)
    if revision.exists() and not revision.is_symlink():
        existing_revision = revision.read_bytes()
        if existing_revision != converted.encode("utf-8"):
            old_converted, old_id_map = convert_legacy_hot(
                plan,
                template=V5_0_HOT_TEMPLATE,
                historical_spacing=True,
            )
            if existing_revision == old_converted.encode("utf-8"):
                converted, id_map = old_converted, old_id_map
                conversion_version = PREVIOUS_V5_VERSION
    state = fresh_state()
    state["run_count"] = plan["parsed"]["sessions"]
    _, _, errors, _ = validate_hot_content(
        converted, state, allow_legacy_overflow=True
    )
    if errors:
        raise BimriError(
            "legacy BIMRI memory could not be converted safely: " + "; ".join(errors)
        )
    assets = []
    for asset in plan["active"] + plan["backups"]:
        destination = _preserve_legacy_asset(paths, asset)
        assets.append({
            "role": asset["role"],
            "source_path": asset["relative"],
            "source_version": asset["parsed"]["version"],
            "sha256": asset["sha256"],
            "byte_length": len(asset["bytes"]),
            "backup_path": destination.relative_to(paths.root).as_posix(),
        })
    revision_bytes = converted.encode("utf-8")
    if revision.exists():
        if revision.is_symlink() or revision.read_bytes() != revision_bytes:
            raise BimriError(
                "revision V000000 conflicts with the deterministic legacy migration."
            )
    else:
        exclusive_write_bytes(revision, revision_bytes)
    revision_hash = sha256_bytes(revision_bytes)
    marker_path = paths.migrations / "legacy-to-v5.json"
    marker_core = {
        "migration": "legacy-to-v5",
        "source_version": plan["parsed"]["version"],
        "source_hot_hash": plan["primary"]["sha256"],
        "revision": "V000000",
        "revision_sha256": revision_hash,
        "assets": assets,
        "id_map": id_map,
    }
    if marker_path.exists():
        marker = read_json_strict(marker_path, marker_path.name)
        for key, value in marker_core.items():
            if marker.get(key) != value:
                raise BimriError(
                    "legacy migration marker conflicts with deterministic retry data."
                )
    else:
        marker = dict(marker_core)
        marker["converter_version"] = conversion_version
        marker["receipt"] = {
            "source_file": plan["primary"]["relative"],
            "source_version": plan["parsed"]["version"],
            **legacy_import_summary(plan["parsed"]),
        }
        marker["completed_at"] = now_iso()
        atomic_write_json(marker_path, marker)
    # A crash retry may arrive with durable marker/revision artifacts but no
    # state.  Revalidate the complete marker before it can authorize source
    # retirement or a new state authority.
    _validate_legacy_marker(paths, marker)
    state["head_hash"] = revision_hash
    state["legacy_migration"] = "legacy-to-v5"
    save_state(paths, state)
    finalize_legacy_migration(paths, state)
    metadata_revision = finalize_current_v5_metadata(paths, state)
    summary = legacy_import_summary(plan["parsed"])
    record_migration_receipt(
        paths,
        "migrated",
        source_version=str(plan["parsed"]["version"]),
        source_file=plan["primary"]["relative"],
        imported=summary,
        backups=[asset["backup_path"] for asset in assets],
        limits=limits_profile(state),
        metadata_revision=metadata_revision,
    )
    return state


def revision_path(paths, number):
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise BimriError("revision number is invalid.")
    return paths.revisions / f"V{number:06d}.md"


def fresh_state():
    return copy.deepcopy(DEFAULT_STATE)


def initialize_v5(paths):
    if not paths.hot.exists():
        atomic_write_text(paths.hot, HOT_TEMPLATE)
    content = paths.hot.read_text(encoding="utf-8")
    state = fresh_state()
    rev = revision_path(paths, 0)
    existing_revisions = sorted(paths.revisions.glob("V*.md"))
    if existing_revisions:
        raise BimriError(
            "state.json is missing while memory revisions already exist. "
            "BIMRI stopped to avoid resetting history; restore state from backup "
            "or ask an agent to recover it."
        )
    _, _, errors, _ = validate_hot_content(content, state)
    if errors:
        raise BimriError(
            "initial hot memory is invalid: " + "; ".join(errors)
        )
    exclusive_write_text(rev, content)
    state["head_hash"] = sha256_text(content)
    save_state(paths, state)
    atomic_write_text(paths.hot, content)
    record_migration_receipt(
        paths, "initialized", limits=limits_profile(state)
    )
    return state


def convert_v4_hot(content, generated_header=None):
    output = []
    tier = 0
    generated_header = generated_header or (
        "<!-- BIMRI v5.0.1 | Generated view. Do not edit directly. -->"
    )
    for line in content.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if stripped.startswith("<!-- BIMRI v4"):
            line = generated_header
        elif stripped.startswith("<!-- Engine:"):
            line = (
                "<!-- Full history: .bimri/log/ | "
                "Revisions: .bimri/revisions/ -->"
            )
        if low.startswith("## tier 1"):
            tier = 1
        elif low.startswith("## tier 2"):
            tier = 2
        elif low.startswith("## tier 3"):
            tier = 3
        if tier == 1:
            match = V4_T1_RE.fullmatch(stripped)
            if match and not V5_T1_RE.fullmatch(stripped):
                data = match.groupdict()
                key = f"legacy.{data['id'].lower()}"
                ptr = f" -> {data['ptr']}" if data.get("ptr") else ""
                tags = ",".join(clean_tags(data["tags"]))
                line = (
                    f"[{data['id']}] [K:{key}] [{data['kind']}] [T:working] "
                    f"[SRC:legacy] [{tags}] {data['text']}{ptr}"
                )
        elif tier == 2:
            match = V4_T2_RE.fullmatch(stripped)
            if match and not V5_T2_RE.fullmatch(stripped):
                data = match.groupdict()
                key = f"legacy.{data['id'].lower()}"
                status = "watch" if data["status"] == "decision" else data["status"]
                ptr = f" -> {data['ptr']}" if data.get("ptr") else ""
                tags = ",".join(clean_tags(data["tags"]))
                line = (
                    f"[{data['id']}] [K:{key}] [I:{data['imp']}] [{status}] "
                    f"[T:working] [SRC:legacy] [F:{data['first']}] [L:{data['last']}] "
                    f"[{tags}] {data['text']}{ptr}"
                )
        elif tier == 3:
            match = V4_PATTERN_RE.fullmatch(stripped)
            if match and not V5_PATTERN_RE.fullmatch(stripped):
                data = match.groupdict()
                key = f"legacy.pattern-{data['id'].lower()}"
                line = (
                    f"[{data['id']}] [K:{key}] [{data['conf']}] [obs:{data['obs']}] "
                    f"[ev:{data['ev']}] {data['text']} | Falsify: {data['falsifier']}"
                )
        output.append(line)
    return "\n".join(output) + "\n"


def _v4_marker_backup(paths, marker, field, required):
    value = marker.get(field)
    if value is None:
        if required:
            raise BimriError(f"v4 migration marker is missing {field}.")
        return None, None
    if not isinstance(value, str) or not value:
        raise BimriError(f"v4 migration marker has an invalid {field} path.")
    portable = PurePosixPath(value.replace("\\", "/"))
    if (
        portable.is_absolute()
        or len(portable.parts) != 3
        or portable.parts[:2] != (".bimri", "backups")
        or portable.parts[2] in {"", ".", ".."}
    ):
        raise BimriError(
            f"v4 migration marker {field} must name a direct .bimri/backups file."
        )
    candidate = paths.root.joinpath(*portable.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise BimriError(f"v4 migration {field} is missing or unsafe: {value}")
    try:
        resolved = candidate.resolve(strict=True)
        backup_root = paths.backups.resolve(strict=True)
        content = candidate.read_bytes()
    except OSError as exc:
        raise BimriError(f"v4 migration {field} is unreadable: {exc}") from exc
    if resolved.parent != backup_root:
        raise BimriError(f"v4 migration {field} escaped .bimri/backups.")
    return candidate, content


def _validate_v4_marker(paths, marker, original_hot_bytes):
    if marker.get("migration") != "v4-to-v5":
        raise BimriError("v4 migration marker has an invalid migration type.")
    try:
        parse_timestamp(marker.get("completed_at"), "v4 migration completion time")
    except BimriError as exc:
        raise BimriError(f"v4 migration marker has an invalid completion time: {exc}") from exc
    source_hot_hash = str(marker.get("source_hot_hash", ""))
    if (
        not HASH_RE.fullmatch(source_hot_hash)
        or source_hot_hash != sha256_bytes(original_hot_bytes)
    ):
        raise BimriError(
            "v4 migration marker source hash does not match the current v4 memory."
        )
    backup_hot, backup_hot_bytes = _v4_marker_backup(
        paths, marker, "backup_hot", required=True
    )
    if backup_hot_bytes != original_hot_bytes:
        raise BimriError(
            "v4 migration hot backup does not match the current v4 memory."
        )
    recorded_hot_backup_hash = marker.get("backup_hot_sha256")
    if recorded_hot_backup_hash is not None and (
        not isinstance(recorded_hot_backup_hash, str)
        or recorded_hot_backup_hash != sha256_bytes(backup_hot_bytes)
    ):
        raise BimriError("v4 migration marker hot-backup hash is invalid.")

    state_exists = paths.state.exists() or paths.state.is_symlink()
    backup_state, backup_state_bytes = _v4_marker_backup(
        paths, marker, "backup_state", required=state_exists
    )
    if state_exists:
        if paths.state.is_symlink() or not paths.state.is_file():
            raise BimriError("v4 state is missing or unsafe during migration resume.")
        current_state_bytes = paths.state.read_bytes()
        if backup_state_bytes != current_state_bytes:
            raise BimriError(
                "v4 migration state backup does not match the current v4 state."
            )
        source_state_hash = marker.get("source_state_hash")
        if source_state_hash is not None and (
            not isinstance(source_state_hash, str)
            or source_state_hash != sha256_bytes(current_state_bytes)
        ):
            raise BimriError("v4 migration marker source-state hash is invalid.")
        backup_state_hash = marker.get("backup_state_sha256")
        if backup_state_hash is not None and (
            not isinstance(backup_state_hash, str)
            or backup_state_hash != sha256_bytes(backup_state_bytes)
        ):
            raise BimriError("v4 migration marker state-backup hash is invalid.")
    elif marker.get("backup_state") is not None:
        raise BimriError(
            "v4 migration marker claims a state backup but no v4 state exists."
        )
    return backup_state, backup_hot


def _verify_v4_sources_unchanged(paths, hot_bytes, state_bytes):
    if paths.hot.is_symlink() or not paths.hot.is_file():
        raise BimriError("v4 bimri.md became missing or unsafe during migration.")
    if paths.hot.read_bytes() != hot_bytes:
        raise BimriError(
            "v4 bimri.md changed while migration was preparing; retry when quiescent."
        )
    if state_bytes is None:
        if paths.state.exists() or paths.state.is_symlink():
            raise BimriError(
                "v4 state appeared while migration was preparing; retry when quiescent."
            )
        return
    if paths.state.is_symlink() or not paths.state.is_file():
        raise BimriError("v4 state became missing or unsafe during migration.")
    if paths.state.read_bytes() != state_bytes:
        raise BimriError(
            "v4 state changed while migration was preparing; retry when quiescent."
        )


def migrate_v4(paths, old_state):
    if not paths.hot.exists():
        raise BimriError(
            "v4 state exists but bimri.md is missing. BIMRI stopped without "
            "creating replacement memory; restore the file from backup first."
        )
    if paths.hot.is_symlink() or not paths.hot.is_file():
        raise BimriError("v4 bimri.md must be a regular non-symbolic file.")
    marker = paths.migrations / "v4-to-v5.json"
    marker_data = read_json_strict(marker, marker.name) if marker.exists() else None
    original_bytes = paths.hot.read_bytes()
    original_state_bytes = paths.state.read_bytes() if paths.state.exists() else None
    if marker_data is not None:
        backup_state, backup_hot = _validate_v4_marker(
            paths, marker_data, original_bytes
        )
    else:
        backup_state = backup_file(paths, paths.state, "state-v4.json")
        backup_hot = backup_file(paths, paths.hot, "bimri-v4.md")
        if backup_hot is None or backup_hot.read_bytes() != original_bytes:
            raise BimriError("v4 hot-memory backup did not preserve the selected source.")
        if original_state_bytes is not None and (
            backup_state is None or backup_state.read_bytes() != original_state_bytes
        ):
            raise BimriError("v4 state backup did not preserve the selected source.")
    _verify_v4_sources_unchanged(paths, original_bytes, original_state_bytes)
    try:
        original = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BimriError("v4 bimri.md is not valid UTF-8.") from exc
    converted = convert_v4_hot(original)
    rev = revision_path(paths, 0)
    if rev.is_symlink():
        raise BimriError("v4 migration revision V000000 cannot be a symbolic link.")
    if marker_data is not None and not rev.is_file():
        raise BimriError(
            "v4 migration marker exists but revision V000000 is missing."
        )
    if rev.exists() and not rev.is_symlink():
        existing_revision = rev.read_bytes()
        if existing_revision != converted.encode("utf-8"):
            old_converted = convert_v4_hot(
                original,
                generated_header=(
                    "<!-- BIMRI v5 | Generated view. Do not edit directly. -->"
                ),
            )
            if existing_revision == old_converted.encode("utf-8"):
                converted = old_converted
    run_numbers = [int(path.stem[1:]) for path in paths.logs.glob("R*.md")
                   if re.fullmatch(r"R\d+", path.stem)]
    state = fresh_state()
    for key in (
        "project_id", "cadence_class", "prune_policy",
        "flag_threshold", "run_dates",
    ):
        if key in old_state:
            state[key] = old_state[key]
    # v4 has no declaration that distinguishes a stock value from an owner's
    # configured value. Preserve every limit it actually records; only fields
    # absent from v4 adopt the v5.0.1 defaults already present in fresh state.
    state.update({
        key: old_state[key] for key in LIMIT_FIELDS if key in old_state
    })
    old_run_count = old_state.get("run_count", 0)
    if (
        isinstance(old_run_count, bool)
        or not isinstance(old_run_count, int)
        or old_run_count < 0
    ):
        raise BimriError("v4 state run_count must be a non-negative integer.")
    state["run_count"] = max([old_run_count] + run_numbers + [0])
    # Reject malformed inherited limits and state fields before creating a
    # revision or migration marker. Exact source backups may already exist,
    # but no new authority has been committed at this point.
    validate_state(state)
    _, converted_entries, migration_errors, counts = validate_hot_content(
        converted, state, allow_legacy_overflow=True
    )
    migration_errors.extend(
        error for error in (
            pointer_validation_error(paths, entry)
            for entry in converted_entries
        ) if error
    )
    if migration_errors:
        raise BimriError(
            "v4 memory could not be converted safely. The original and backups "
            "were preserved: " + "; ".join(migration_errors)
        )
    _verify_v4_sources_unchanged(paths, original_bytes, original_state_bytes)
    if rev.exists():
        existing = rev.read_bytes()
        if existing != converted.encode("utf-8"):
            raise BimriError(
                "revision V000000 conflicts with the deterministic v4 migration; "
                "BIMRI stopped without overwriting either version."
            )
    else:
        exclusive_write_text(rev, converted)
    state["head_hash"] = sha256_text(converted)
    if not marker.exists():
        atomic_write_json(marker, {
            "migration": "v4-to-v5",
            "completed_at": now_iso(),
            "source_hot_hash": sha256_bytes(original_bytes),
            "source_state_hash": (
                sha256_bytes(original_state_bytes)
                if original_state_bytes is not None else None
            ),
            "backup_state": (
                backup_state.relative_to(paths.root).as_posix()
                if backup_state else None
            ),
            "backup_state_sha256": (
                sha256_bytes(backup_state.read_bytes()) if backup_state else None
            ),
            "backup_hot": (
                backup_hot.relative_to(paths.root).as_posix()
                if backup_hot else None
            ),
            "backup_hot_sha256": sha256_bytes(backup_hot.read_bytes()),
        })
    metadata_revision, generated_view = stage_v5_0_metadata_revision(
        paths, state, converted
    )
    _verify_v4_sources_unchanged(paths, original_bytes, original_state_bytes)
    save_state(paths, state)
    atomic_write_text(paths.hot, generated_view)
    record_migration_receipt(
        paths,
        "migrated",
        source_version=str(old_state.get("bimri_version") or "4.0"),
        source_file="bimri.md",
        imported={
            "tier1": counts[1],
            "tier2": counts[2],
            "tier3": counts[3],
            "total": sum(counts.values()),
            "inherited_overlength_claims": sum(
                len(entry.get("text", "")) > state["entry_max_chars"]
                and (
                    entry.get("source") == "legacy" or entry["tier"] == 3
                )
                for entry in converted_entries
            ),
        },
        backups=[
            path.relative_to(paths.root).as_posix()
            for path in (backup_state, backup_hot) if path is not None
        ],
        limits=limits_profile(state),
        metadata_revision=metadata_revision,
    )
    return state


def looks_like_v4_hot(content):
    if "BIMRI v4" in content:
        return True
    return any(
        regex.fullmatch(line.strip())
        for line in content.splitlines()
        for regex in (V4_T1_RE, V4_T2_RE, V4_PATTERN_RE)
    )


def reject_unclaimed_legacy_roots(paths):
    """Reject legacy root files that are distinct from the generated view."""
    unclaimed = []
    for path in _actual_legacy_root_paths(
        paths, LEGACY_ACTIVE_NAMES + LEGACY_BACKUP_NAMES
    ):
        is_generated_view = False
        if (
            path.name.casefold() == "bimri.md"
            and not path.is_symlink()
            and paths.hot.exists()
            and not paths.hot.is_symlink()
        ):
            try:
                is_generated_view = os.path.samefile(str(path), str(paths.hot))
            except OSError:
                is_generated_view = path == paths.hot
        if not is_generated_view:
            unclaimed.append(path.name)
    if unclaimed:
        raise BimriError(
            "selected structured BIMRI authority coexists with unclaimed legacy root "
            "file(s): " + ", ".join(sorted(unclaimed)) + ". BIMRI stopped "
            "without importing or deleting them. Ask the owner which memory "
            "is authoritative, then move or remove the unclaimed files."
        )


V5_0_METADATA_UPDATES = {
    "<!-- BIMRI v5 | Generated view. Do not edit directly. -->":
        "<!-- BIMRI v5.0.1 | Generated view. Do not edit directly. -->",
    "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->":
        "<!-- Confirmed facts, decisions, preferences and rules. Capacity: state.json. -->",
    "<!-- Current work, risks and next actions. Cap: 20. -->":
        "<!-- Current work, risks and next actions. Capacity: state.json. -->",
    "<!-- Evidence-backed patterns. Cap: 8. -->":
        "<!-- Evidence-backed patterns. Capacity: state.json. -->",
}
V5_0_COMPACT_METADATA_UPDATES = {
    "<!-- BIMRI v5 | Generated view. Do not edit directly. -->":
        "<!-- BIMRI v5.0.1 | Generated. -->",
    "<!-- Confirmed facts, decisions, preferences and rules. Cap: 12. -->":
        "<!-- Tier 1 capacity: state.json. -->",
    "<!-- Current work, risks and next actions. Cap: 20. -->":
        "<!-- Tier 2 capacity: state.json. -->",
    "<!-- Evidence-backed patterns. Cap: 8. -->":
        "<!-- Tier 3 capacity: state.json. -->",
}


def normalize_v5_0_hot_metadata(content, compact=False):
    """Update only exact historical metadata lines, preserving entry bytes."""
    updates = (
        V5_0_COMPACT_METADATA_UPDATES if compact else V5_0_METADATA_UPDATES
    )
    rendered = []
    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        rendered.append(updates.get(body, body) + ending)
    return "".join(rendered)


def select_v5_0_metadata_normalization(content, state):
    """Choose rich or compact truthful metadata without worsening overflow."""
    normalized = normalize_v5_0_hot_metadata(content)
    if normalized == content:
        return content
    old_overflow = overflow_vector(content, state)
    normalized_overflow = overflow_vector(normalized, state)
    if (
        old_overflow is not None
        and normalized_overflow is not None
        and any(
            new_value > old_value
            for new_value, old_value in zip(normalized_overflow, old_overflow)
        )
    ):
        normalized = normalize_v5_0_hot_metadata(content, compact=True)
        normalized_overflow = overflow_vector(normalized, state)
    if (
        old_overflow is None
        or normalized_overflow is None
        or any(
            new_value > old_value
            for new_value, old_value in zip(normalized_overflow, old_overflow)
        )
    ):
        raise BimriError(
            "cannot normalize v5.0 metadata without increasing bounded-memory "
            "overflow; BIMRI stopped before writing new authority."
        )
    return normalized


def stage_v5_0_metadata_revision(paths, state, content, normalized=None):
    """Create/reuse one deterministic metadata-only revision and update state."""
    normalized = (
        select_v5_0_metadata_normalization(content, state)
        if normalized is None else normalized
    )
    if normalized == content:
        return None, content
    _, entries, errors, _ = validate_hot_content(
        normalized, state, allow_legacy_overflow=True
    )
    errors.extend(
        error for error in (
            pointer_validation_error(paths, entry) for entry in entries
        ) if error
    )
    if errors:
        raise BimriError(
            "cannot normalize v5.0 metadata safely: " + "; ".join(errors)
        )
    number = state["head_revision"] + 1
    if number > 999999:
        raise BimriError("BIMRI has exhausted its six-digit revision ID space.")
    revision = revision_path(paths, number)
    normalized_bytes = normalized.encode("utf-8")
    if revision.exists() or revision.is_symlink():
        if (
            revision.is_symlink()
            or not revision.is_file()
            or revision.read_bytes() != normalized_bytes
        ):
            raise BimriError(
                "v5.0.1 metadata revision conflicts with an existing "
                f"{revision.name}; BIMRI stopped without overwriting it."
            )
    else:
        exclusive_write_bytes(revision, normalized_bytes)
    state["head_revision"] = number
    state["head_hash"] = sha256_bytes(normalized_bytes)
    state["last_revision_reason"] = "v5.0.1 metadata normalization"
    return revision.name, normalized


def finalize_current_v5_metadata(paths, state):
    """Normalize stale v5.0 view metadata in current-version authority."""
    if state.get("bimri_version") != VERSION:
        return None
    head = revision_path(paths, state["head_revision"])
    if not head.is_file() or head.is_symlink():
        raise BimriError(
            "cannot normalize current metadata: head revision is missing or unsafe."
        )
    head_bytes = head.read_bytes()
    if sha256_bytes(head_bytes) != state["head_hash"]:
        raise BimriError(
            "cannot normalize current metadata: state head hash does not match "
            "the head revision."
        )
    try:
        content = head_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BimriError(
            "cannot normalize current metadata: head revision is not UTF-8."
        ) from exc
    normalized = select_v5_0_metadata_normalization(content, state)
    if normalized == content:
        return None

    # Validate and preflight the deterministic next path before sync can write
    # a manual-edit recovery/conflict. A conflicting path must fail without
    # changing any current authority.
    _, entries, errors, _ = validate_hot_content(
        normalized, state, allow_legacy_overflow=True
    )
    errors.extend(
        error for error in (
            pointer_validation_error(paths, entry) for entry in entries
        ) if error
    )
    if errors:
        raise BimriError(
            "cannot normalize current metadata safely: " + "; ".join(errors)
        )
    number = state["head_revision"] + 1
    if number > 999999:
        raise BimriError("BIMRI has exhausted its six-digit revision ID space.")
    candidate = revision_path(paths, number)
    normalized_bytes = normalized.encode("utf-8")
    if candidate.exists() or candidate.is_symlink():
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.read_bytes() != normalized_bytes
        ):
            raise BimriError(
                "v5.0.1 metadata revision conflicts with an existing "
                f"{candidate.name}; BIMRI stopped without overwriting it."
            )

    # Run recovery while the old head is still authoritative. This preserves
    # unknown hot bytes byte-for-byte and restores the old generated view.
    sync_generated_view(paths, state)
    authority_state_bytes = paths.state.read_bytes()
    if head.read_bytes() != head_bytes:
        raise BimriError(
            "current head changed while metadata normalization was preparing; "
            "retry when BIMRI is quiescent."
        )
    metadata_revision, normalized = stage_v5_0_metadata_revision(
        paths, state, content, normalized=normalized
    )
    if (
        paths.state.read_bytes() != authority_state_bytes
        or head.read_bytes() != head_bytes
    ):
        raise BimriError(
            "current state or head changed while metadata normalization was "
            "committing; BIMRI stopped before replacing state."
        )
    save_state(paths, state)
    write_generated_view(paths, normalized, warn_only=True)
    return metadata_revision


def upgrade_v5_0_state(paths, state):
    old_limits = limits_profile(state)
    expanded_default_limits = old_limits == V5_0_DEFAULT_LIMITS
    upgraded = copy.deepcopy(state)
    upgraded["bimri_version"] = VERSION
    if expanded_default_limits:
        upgraded.update(limits_profile(DEFAULT_STATE))
    # Validate both authorities before any upgrade write. A syntactically valid
    # state file must never be enough to bless a missing, corrupt or mismatched
    # head revision.
    validate_state(upgraded)
    head = revision_path(paths, state["head_revision"])
    if not head.is_file() or head.is_symlink():
        raise BimriError(
            f"cannot upgrade v5.0: head revision is missing or unsafe: {head.name}"
        )
    try:
        head_bytes = head.read_bytes()
        head_content = head_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BimriError(f"cannot upgrade v5.0: head revision is unreadable: {exc}") from exc
    if sha256_bytes(head_bytes) != state["head_hash"]:
        raise BimriError(
            "cannot upgrade v5.0: state head hash does not match the head revision."
        )
    _, head_entries, head_errors, _ = validate_hot_content(
        head_content, state, allow_legacy_overflow=True
    )
    head_errors.extend(
        error for error in (
            pointer_validation_error(paths, entry)
            for entry in head_entries
        ) if error
    )
    if head_errors:
        raise BimriError(
            "cannot upgrade v5.0: head revision is invalid: "
            + "; ".join(head_errors)
        )

    normalized_content = select_v5_0_metadata_normalization(
        head_content, upgraded
    )
    metadata_revision = None

    source_bytes = paths.state.read_bytes()
    source_hash = sha256_bytes(source_bytes)
    backup = paths.backups / f"state-v5.0-{source_hash}.json"
    if backup.exists():
        if backup.is_symlink() or backup.read_bytes() != source_bytes:
            raise BimriError("v5.0 state upgrade backup conflicts with source state.")
    else:
        exclusive_write_bytes(backup, source_bytes)

    if paths.state.read_bytes() != source_bytes or head.read_bytes() != head_bytes:
        raise BimriError(
            "v5.0 state or head revision changed while the upgrade was "
            "preparing; retry when BIMRI is quiescent."
        )
    metadata_revision, normalized_content = stage_v5_0_metadata_revision(
        paths, upgraded, head_content, normalized=normalized_content
    )

    if paths.state.read_bytes() != source_bytes or head.read_bytes() != head_bytes:
        raise BimriError(
            "v5.0 state or head revision changed while the upgrade was "
            "committing; BIMRI stopped before replacing state."
        )
    save_state(paths, upgraded)
    if metadata_revision is not None:
        # Preserve the normal manual-edit recovery path. The old generated
        # head can be replaced directly, but any other current hot bytes must
        # pass through sync_generated_view so they are recovered byte-for-byte
        # and raised as a human-visible conflict before the normalized view is
        # restored.
        current_hot = paths.hot.read_bytes() if paths.hot.exists() else None
        if current_hot == head_bytes:
            write_generated_view(paths, normalized_content, warn_only=True)
        else:
            sync_generated_view(paths, upgraded)
    record_migration_receipt(
        paths,
        "upgraded",
        source_version=PREVIOUS_V5_VERSION,
        expanded_default_limits=expanded_default_limits,
        old_limits=old_limits,
        limits=limits_profile(upgraded),
        backups=[backup.relative_to(paths.root).as_posix()],
        metadata_revision=metadata_revision,
    )
    return upgraded


def require_complete_v5_state(raw):
    """Reject partial structured state instead of guessing authoritative data."""
    missing = sorted(key for key in DEFAULT_STATE if key not in raw)
    if missing:
        raise BimriError(
            "state.json is missing required v5 field(s): "
            + ", ".join(missing)
            + ". BIMRI stopped without filling them from defaults."
        )


def load_or_initialize(paths):
    ensure_layout(paths)
    legacy_plan = discover_legacy_migration(paths)
    if legacy_plan is not None:
        return migrate_legacy(paths, legacy_plan)
    if not paths.state.exists():
        if not paths.hot.exists():
            atomic_write_text(paths.hot, HOT_TEMPLATE)
        content = paths.hot.read_text(encoding="utf-8")
        if looks_like_v4_hot(content):
            ensure_v4_install_is_quiescent(
                paths, {"bimri_version": "4.0"}
            )
            reject_unclaimed_legacy_roots(paths)
            return migrate_v4(paths, {})
        return initialize_v5(paths)
    raw = read_json_strict(paths.state, "state.json")
    version = str(raw.get("bimri_version", ""))
    if version.startswith("4") or (not version and "current_run_id" in raw):
        ensure_v4_install_is_quiescent(paths, raw)
        reject_unclaimed_legacy_roots(paths)
        return migrate_v4(paths, raw)
    if raw.get("bimri_version") == PREVIOUS_V5_VERSION:
        require_complete_v5_state(raw)
        merged = fresh_state()
        merged.update(V5_0_DEFAULT_LIMITS)
        merged["bimri_version"] = PREVIOUS_V5_VERSION
        merged.update(raw)
        state = validate_state(
            merged, accepted_versions={PREVIOUS_V5_VERSION}
        )
        finalize_legacy_migration(paths, state)
        reject_unclaimed_legacy_roots(paths)
        return upgrade_v5_0_state(paths, state)
    if raw.get("bimri_version") != VERSION:
        raise BimriError(
            f"unsupported BIMRI state version: {raw.get('bimri_version')}"
        )
    require_complete_v5_state(raw)
    merged = fresh_state()
    merged.update(raw)
    state = validate_state(merged)
    finalize_legacy_migration(paths, state)
    reject_unclaimed_legacy_roots(paths)
    metadata_revision = finalize_current_v5_metadata(paths, state)
    record_migration_receipt(
        paths,
        "verified",
        source_version=VERSION,
        limits=limits_profile(state),
        metadata_revision=metadata_revision,
    )
    return state


def parse_hot(content):
    lines = content.splitlines()
    tier = 0
    entries = []
    errors = []
    headings = []
    end_markers = 0
    ended = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        low = stripped.lower()
        if low == "<!-- end bimri -->":
            end_markers += 1
            ended = True
            tier = 0
            continue
        if ended:
            if stripped and not stripped.startswith("<!--"):
                errors.append(
                    f"line {index + 1}: content appears after the BIMRI end marker."
                )
            continue
        if low == "## tier 1: core intelligence":
            tier = 1
            headings.append(1)
            continue
        if low == "## tier 2: active context":
            tier = 2
            headings.append(2)
            continue
        if low == "## tier 3: pattern recognition":
            tier = 3
            headings.append(3)
            continue
        if not stripped or stripped.startswith("<!--"):
            continue
        if stripped.startswith("#"):
            continue
        match = None
        if tier == 1:
            match = V5_T1_RE.fullmatch(stripped)
        elif tier == 2:
            match = V5_T2_RE.fullmatch(stripped)
        elif tier == 3:
            match = V5_PATTERN_RE.fullmatch(stripped)
        if match:
            data = match.groupdict()
            data.update({"tier": tier, "line": index, "raw": stripped})
            entries.append(data)
        elif tier:
            errors.append(f"line {index + 1}: malformed Tier {tier} entry: {stripped[:120]}")
        else:
            errors.append(
                f"line {index + 1}: content appears outside a BIMRI tier: "
                f"{stripped[:120]}"
            )
    if headings != [1, 2, 3]:
        errors.append(
            "hot memory must contain exactly one ordered Tier 1, Tier 2 and "
            "Tier 3 heading."
        )
    if end_markers != 1:
        errors.append("hot memory must contain exactly one END BIMRI marker.")
    return lines, entries, errors


def validate_hot_content(content, state, allow_legacy_overflow=False):
    lines, entries, errors = parse_hot(content)
    seen_ids = set()
    seen_keys = set()
    counts = {1: 0, 2: 0, 3: 0}
    for entry in entries:
        counts[entry["tier"]] += 1
        if len(entry["raw"]) > MAX_SERIALIZED_ENTRY_CHARS:
            errors.append(
                f"{entry['id']} serialized entry exceeds "
                f"{MAX_SERIALIZED_ENTRY_CHARS} characters."
            )
        if entry["id"] in seen_ids:
            errors.append(f"duplicate memory ID: {entry['id']}")
        seen_ids.add(entry["id"])
        key = entry.get("key")
        if key:
            if key in seen_keys:
                errors.append(f"duplicate memory key: {key}")
            seen_keys.add(key)
            try:
                if clean_key(key) != key:
                    errors.append(f"{entry['id']} key is not normalized.")
            except BimriError as exc:
                errors.append(str(exc))
        text = entry.get("text", "")
        inherited_overlength = (
            (
                entry.get("source") == "legacy"
                # Tier 3 has no source field. New v5 proposals cannot author
                # overlength patterns, so an overlength pattern that survives
                # structural validation is inherited v4 content.
                or entry["tier"] == 3
            )
            and len(text) > state["entry_max_chars"]
        )
        try:
            clean_scalar(
                text,
                f"memory text {entry['id']}",
                (
                    MAX_SERIALIZED_ENTRY_CHARS
                    if inherited_overlength
                    else state["entry_max_chars"]
                ),
            )
        except BimriError as exc:
            errors.append(str(exc))
        if inherited_overlength and not allow_legacy_overflow:
            inheritance = (
                "legacy" if entry.get("source") == "legacy" else "v4 pattern"
            )
            errors.append(
                f"{entry['id']} inherited {inheritance} text exceeds active entry "
                f"cap: {len(text)}/{state['entry_max_chars']} characters"
            )
        for field in ("tags", "ev"):
            if entry.get(field) and ("\t" in entry[field] or "\n" in entry[field]):
                errors.append(f"{entry['id']} has unsafe {field}.")
        if entry["tier"] in {1, 2}:
            try:
                normalized_tags = ",".join(clean_tags(entry.get("tags", "")))
                if normalized_tags != entry.get("tags", ""):
                    errors.append(f"{entry['id']} tags are not normalized.")
            except BimriError as exc:
                errors.append(str(exc))
        if entry["tier"] == 3:
            evidence = [
                item.strip() for item in entry.get("ev", "").split(",")
                if item.strip()
            ]
            if not evidence:
                errors.append(f"{entry['id']} pattern has no evidence.")
            if len(evidence) > MAX_PATTERN_EVIDENCE:
                errors.append(
                    f"{entry['id']} pattern exceeds "
                    f"{MAX_PATTERN_EVIDENCE} evidence IDs."
                )
            for evidence_id in evidence:
                try:
                    validate_fixed_id(
                        evidence_id, LEGACY_ENTRY_RE, "pattern evidence ID"
                    )
                except BimriError as exc:
                    errors.append(str(exc))
            if (
                " | Falsify: " in text
                or " | Falsify: " in entry.get("falsifier", "")
            ):
                errors.append(
                    f"{entry['id']} text contains the reserved pattern delimiter."
                )
    if not allow_legacy_overflow:
        limits = {1: state["tier1_max"], 2: state["tier2_max"], 3: state["tier3_max"]}
        for tier, count in counts.items():
            if count > limits[tier]:
                errors.append(f"Tier {tier} exceeds cap: {count}/{limits[tier]}")
        if len(content.encode("utf-8")) > state["hot_max_bytes"]:
            errors.append(
                f"hot memory exceeds byte cap: "
                f"{len(content.encode('utf-8'))}/{state['hot_max_bytes']}"
            )
    return lines, entries, errors, counts


def overflow_vector(content, state):
    _, entries, structural_errors, counts = validate_hot_content(
        content, state, allow_legacy_overflow=True
    )
    if structural_errors:
        return None
    limits = {
        1: state["tier1_max"],
        2: state["tier2_max"],
        3: state["tier3_max"],
    }
    inherited_excess = [
        len(entry.get("text", "")) - state["entry_max_chars"]
        for entry in entries
        if (
            (entry.get("source") == "legacy" or entry["tier"] == 3)
            and len(entry.get("text", "")) > state["entry_max_chars"]
        )
    ]
    return (
        max(0, counts[1] - limits[1]),
        max(0, counts[2] - limits[2]),
        max(0, counts[3] - limits[3]),
        max(0, len(content.encode("utf-8")) - state["hot_max_bytes"]),
        len(inherited_excess),
        sum(inherited_excess),
    )


def strictly_reduces_overflow(before, after, state):
    old = overflow_vector(before, state)
    new = overflow_vector(after, state)
    if old is None or new is None or not any(old):
        return False
    return all(new_value <= old_value for new_value, old_value in zip(new, old)) and any(
        new_value < old_value for new_value, old_value in zip(new, old)
    )


def pointer_validation_error(paths, entry):
    pointer = entry.get("ptr")
    if not pointer:
        return None
    try:
        candidate = (paths.root / pointer).resolve()
    except OSError as exc:
        return f"{entry['id']} pointer cannot be resolved safely: {exc}"
    if paths.root not in candidate.parents and candidate != paths.root:
        return f"{entry['id']} pointer escapes the BIMRI project."
    return None


def find_entry(entries, key=None, target_id=None):
    for entry in entries:
        if target_id and entry["id"] == target_id:
            return entry
        if key and entry.get("key") == key:
            return entry
    return None


def resolve_entry(entries, key, target_id=None, require_target=False):
    by_key = next(
        (entry for entry in entries if entry.get("key") == key),
        None,
    )
    by_target = next(
        (entry for entry in entries if target_id and entry["id"] == target_id),
        None,
    )
    if target_id and require_target and not by_target:
        raise BimriError(f"target {target_id} does not exist in the run's base memory.")
    if by_target and by_target.get("key") != key:
        raise BimriError(
            f"target {target_id} belongs to key {by_target.get('key')}, not {key}."
        )
    if by_key and by_target and by_key["id"] != by_target["id"]:
        raise BimriError("proposal key and target identify different memory entries.")
    return by_key or by_target


def insert_lines_in_tier(lines, tier, new_lines, leading_blank=False):
    heading = f"## tier {tier}"
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        low = line.strip().lower()
        if low.startswith(heading):
            start = index
            continue
        if start is not None and low.startswith("## tier "):
            end = index
            break
        if start is not None and low.startswith("<!-- end bimri"):
            end = index
            break
    if start is None:
        raise BimriError(f"hot memory is missing Tier {tier} heading.")
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    block = list(new_lines)
    if block and leading_blank:
        block.insert(0, "")
    lines[insert_at:insert_at] = block
    return lines


def insert_in_tier(lines, tier, new_line):
    return insert_lines_in_tier(lines, tier, [new_line])


def render_content(lines):
    return "\n".join(lines).rstrip() + "\n"


def referenced_revision_numbers(paths, state):
    numbers = {0, state["head_revision"]}
    for path in paths.decisions.glob("*.json"):
        if not PROPOSAL_RE.fullmatch(path.stem) or path.is_symlink():
            continue
        try:
            decision = validate_decision(
                read_json_strict(path, path.name), path.stem
            )
            validate_decision_effect(paths, state, decision)
        except (BimriError, OSError):
            continue
        if decision["outcome"] == "accepted":
            numbers.add(decision["revision"])
    for path in paths.resolutions.glob("*.json"):
        if not CONFLICT_RE.fullmatch(path.stem) or path.is_symlink():
            continue
        try:
            cpath = conflict_path(paths, path.stem)
            if not cpath.exists():
                continue
            conflict = validate_conflict_record(
                paths,
                read_json_strict(cpath, cpath.name),
                expected_conflict_id=path.stem,
            )
            resolution = validate_resolution_record(
                read_json_strict(path, path.name),
                conflict=conflict,
                expected_conflict_id=path.stem,
            )
            if resolution["status"] == "resolved":
                validate_resolution_effect(
                    paths, state, conflict, resolution
                )
        except (BimriError, OSError):
            continue
        if resolution["status"] == "resolved":
            numbers.add(resolution["revision_after"])
    return {number for number in numbers if 0 <= number <= 999999}


def sync_generated_view(paths, state):
    rev = revision_path(paths, state["head_revision"])
    if rev.is_symlink():
        raise BimriError(f"head revision cannot be a symbolic link: {rev.name}")
    if not rev.exists():
        raise BimriError(f"head revision is missing: {rev.name}")
    expected_bytes = rev.read_bytes()
    try:
        expected = expected_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BimriError(f"head revision is not valid UTF-8: {rev.name}") from exc
    expected_hash = sha256_bytes(expected_bytes)
    if state["head_hash"] != expected_hash:
        raise BimriError("state head hash does not match the head revision.")
    hot_exists = paths.hot.exists()
    current_bytes = paths.hot.read_bytes() if hot_exists else b""
    current_hash = sha256_bytes(current_bytes)
    if current_hash == expected_hash:
        return None
    known_hashes = set()
    for number in referenced_revision_numbers(paths, state):
        path = revision_path(paths, number)
        if not path.exists() or path.is_symlink():
            continue
        try:
            known_hashes.add(sha256_bytes(path.read_bytes()))
        except OSError:
            continue
    if current_hash not in known_hashes and hot_exists:
        try:
            current_bytes.decode("utf-8")
            suffix = ".md"
        except UnicodeDecodeError:
            suffix = ".bin"
        recovery = paths.recovery / (
            f"manual-hot-{dt.datetime.now():%Y%m%d-%H%M%S}-"
            f"{uuid.uuid4().hex[:8]}{suffix}"
        )
        exclusive_write_bytes(recovery, current_bytes)
        relative_recovery = recovery.relative_to(paths.root).as_posix()
        conflict = create_system_conflict(
            paths, state, "manual-edit", "manual.bimri",
            "BIMRI found a direct edit to generated hot memory. The edit was "
            f"preserved at {relative_recovery}. Ask the owner "
            "whether the agent should review and re-submit it as proposals.",
            {
                "recovery_file": relative_recovery,
                "recovery_files": [relative_recovery],
            },
        )
        print(
            "BIMRI NOTICE: a direct edit to bimri.md was preserved at "
            f"{relative_recovery} and the generated view was "
            f"restored. Human decision: {conflict}.",
            file=sys.stderr,
        )
    else:
        conflict = None
    write_generated_view(paths, expected)
    return conflict


def commit_revision(paths, state, content, reason, allow_legacy_overflow=False):
    _, _, errors, _ = validate_hot_content(
        content, state, allow_legacy_overflow=allow_legacy_overflow
    )
    if errors:
        raise BimriError("cannot commit invalid hot memory: " + "; ".join(errors))
    number = state["head_revision"] + 1
    while revision_path(paths, number).exists():
        number += 1
    if number > 999999:
        raise BimriError("BIMRI has exhausted its six-digit revision ID space.")
    exclusive_write_text(revision_path(paths, number), content)
    state["head_revision"] = number
    state["head_hash"] = sha256_text(content)
    state["last_revision_reason"] = clean_scalar(reason, "revision reason", 300)
    save_state(paths, state)
    write_generated_view(paths, content, warn_only=True)
    return number


def session_key(actor, session):
    if not session:
        return None
    session = clean_scalar(session, "session", 500)
    return f"{actor}:{sha256_text(session)[:20]}"


def run_log_path(paths, run_id):
    run_id = validate_fixed_id(run_id, RUN_RE, "run ID")
    path = paths.logs / f"{run_id}.md"
    if path.parent.resolve() != paths.logs.resolve():
        raise BimriError("run path escaped the log directory.")
    if path.is_symlink():
        raise BimriError(f"run log cannot be a symbolic link: {path.name}")
    return path


def proposal_path(paths, proposal_id):
    proposal_id = validate_fixed_id(proposal_id, PROPOSAL_RE, "proposal ID")
    return paths.proposals / f"{proposal_id}.json"


def decision_path(paths, proposal_id):
    proposal_id = validate_fixed_id(
        proposal_id, PROPOSAL_RE, "decision proposal ID"
    )
    return paths.decisions / f"{proposal_id}.json"


def conflict_path(paths, conflict_id):
    conflict_id = validate_fixed_id(conflict_id, CONFLICT_RE, "conflict ID")
    return paths.conflicts / f"{conflict_id}.json"


def resolution_file_path(paths, conflict_id):
    conflict_id = validate_fixed_id(
        conflict_id, CONFLICT_RE, "resolution conflict ID"
    )
    return paths.resolutions / f"{conflict_id}.json"


def open_conflicts(paths, state):
    items = []
    for path in sorted(paths.conflicts.glob("C*.json")):
        data = validate_conflict_record(
            paths, read_json_strict(path, path.name), path.stem
        )
        conflict_id = data["conflict_id"]
        resolution_path = resolution_file_path(paths, conflict_id)
        resolved = False
        resolution = None
        if resolution_path.exists():
            resolution = validate_resolution_record(
                read_json_strict(resolution_path, resolution_path.name),
                conflict=data,
                expected_conflict_id=conflict_id,
            )
            if resolution["status"] == "resolved":
                validate_resolution_effect(paths, state, data, resolution)
                validate_conflict_candidate_decisions(
                    paths, data, resolution
                )
            resolved = resolution["status"] == "resolved"
        if not resolved:
            items.append(data)
    return items


def allocate_conflict_id(paths, state):
    existing = [
        int(path.stem[1:])
        for path in paths.conflicts.glob("C*.json")
        if CONFLICT_RE.fullmatch(path.stem)
    ]
    number = max([state["conflict_count"]] + existing + [0]) + 1
    if number > 999999:
        raise BimriError("BIMRI has exhausted its six-digit conflict ID space.")
    while conflict_path(paths, f"C{number:06d}").exists():
        number += 1
        if number > 999999:
            raise BimriError("BIMRI has exhausted its six-digit conflict ID space.")
    state["conflict_count"] = number
    return f"C{number:06d}"


def proposal_file_hash(paths, proposal_id):
    path = proposal_path(paths, proposal_id)
    if not path.exists() or path.is_symlink():
        raise BimriError(f"proposal file is missing or unsafe: {proposal_id}")
    return sha256_bytes(path.read_bytes())


def validate_proposal_id_list(value, name):
    if not isinstance(value, list):
        raise BimriError(f"{name} must be a list.")
    proposal_ids = []
    for item in value:
        proposal_id = validate_fixed_id(
            item, PROPOSAL_RE, f"{name} entry"
        )
        if proposal_id != item:
            raise BimriError(f"{name} entries must already be normalized.")
        proposal_ids.append(proposal_id)
    if len(proposal_ids) != len(set(proposal_ids)):
        raise BimriError(f"{name} contains duplicate proposal IDs.")
    return proposal_ids


def validate_revision_number(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 999999
    ):
        raise BimriError(f"{name} must be a valid revision number.")
    return value


def validate_conflict_record(
    paths, conflict, expected_conflict_id=None, verify_candidates=True
):
    if not isinstance(conflict, dict):
        raise BimriError("conflict must be a JSON object.")
    if conflict.get("bimri_version") not in COMPATIBLE_ARTIFACT_VERSIONS:
        raise BimriError("conflict BIMRI version is invalid.")
    conflict_id = validate_fixed_id(
        conflict.get("conflict_id"), CONFLICT_RE, "conflict ID"
    )
    if expected_conflict_id and conflict_id != expected_conflict_id:
        raise BimriError("conflict filename does not match its ID.")
    conflict_type = conflict.get("type")
    if conflict_type not in CONFLICT_TYPES:
        raise BimriError("conflict type is invalid.")
    key = clean_key(conflict.get("key"))
    if key != conflict.get("key"):
        raise BimriError("conflict key must already be normalized.")
    parse_timestamp(conflict.get("created_at"), "conflict timestamp")
    proposal_ids = validate_proposal_id_list(
        conflict.get("proposal_ids"), "conflict proposal_ids"
    )
    if conflict_type == "manual-edit":
        if proposal_ids:
            raise BimriError("manual-edit conflicts cannot contain proposals.")
    elif not proposal_ids:
        raise BimriError("proposal conflicts require at least one candidate.")
    hashes = conflict.get("proposal_hashes")
    if not isinstance(hashes, dict) or set(hashes) != set(proposal_ids):
        raise BimriError(
            "conflict proposal hashes must exactly match its candidate IDs."
        )
    for proposal_id in proposal_ids:
        expected = hashes.get(proposal_id)
        if not isinstance(expected, str) or not HASH_RE.fullmatch(expected):
            raise BimriError(
                f"conflict is missing a valid hash for {proposal_id}."
            )
        if verify_candidates and proposal_file_hash(paths, proposal_id) != expected:
            raise BimriError(
                f"proposal {proposal_id} changed after the conflict was raised. "
                "BIMRI stopped so the owner can review it again."
            )
    current_line = conflict.get("current_line")
    current_hash = conflict.get("current_hash")
    if current_line is None:
        if current_hash != "absent":
            raise BimriError(
                "conflict current hash must be absent when no current line exists."
            )
    else:
        current_line = clean_scalar(
            current_line,
            "conflict current line",
            MAX_SERIALIZED_ENTRY_CHARS,
        )
        if not isinstance(current_hash, str) or not HASH_RE.fullmatch(current_hash):
            raise BimriError("conflict current hash is invalid.")
        if line_hash(current_line) != current_hash:
            raise BimriError("conflict current line does not match its hash.")
    clean_scalar(conflict.get("question"), "conflict question", 1000)
    extra = conflict.get("extra", {})
    if not isinstance(extra, dict):
        raise BimriError("conflict extra metadata must be an object.")
    if conflict_type == "manual-edit":
        recovery_files = extra.get("recovery_files", [])
        if not isinstance(recovery_files, list):
            raise BimriError("manual-edit recovery_files must be a list.")
        for recovery_file in recovery_files:
            clean_scalar(recovery_file, "manual recovery path", 500)
    return conflict


def validate_resolution_record(
    resolution, conflict=None, expected_conflict_id=None
):
    if not isinstance(resolution, dict):
        raise BimriError("resolution must be a JSON object.")
    if resolution.get("bimri_version") not in COMPATIBLE_ARTIFACT_VERSIONS:
        raise BimriError("resolution BIMRI version is invalid.")
    conflict_id = validate_fixed_id(
        resolution.get("conflict_id"), CONFLICT_RE, "resolution conflict ID"
    )
    if expected_conflict_id and conflict_id != expected_conflict_id:
        raise BimriError("resolution filename does not match its conflict ID.")
    status = resolution.get("status")
    if status not in {"applying", "failed", "resolved"}:
        raise BimriError("resolution status is invalid.")
    proposal_ids = validate_proposal_id_list(
        resolution.get("proposal_ids"), "resolution proposal_ids"
    )
    choice = clean_scalar(resolution.get("choice"), "resolution choice", 80)
    if choice != resolution.get("choice"):
        raise BimriError("resolution choice must already be normalized.")
    if choice not in {"current", "dismiss", *proposal_ids}:
        raise BimriError("resolution choice is not one of its recorded candidates.")
    if resolution.get("by") != "user":
        raise BimriError("resolution authority must be user.")
    parse_timestamp(resolution.get("started_at"), "resolution start timestamp")
    revision_before = validate_revision_number(
        resolution.get("revision_before"), "resolution revision_before"
    )
    if status == "failed":
        parse_timestamp(resolution.get("failed_at"), "resolution failure timestamp")
        clean_scalar(resolution.get("error"), "resolution error", 1000)
    if status == "resolved":
        parse_timestamp(
            resolution.get("resolved_at"), "resolution completion timestamp"
        )
        revision_after = validate_revision_number(
            resolution.get("revision_after"), "resolution revision_after"
        )
        if revision_after < revision_before:
            raise BimriError(
                "resolution revision_after cannot precede revision_before."
            )
    if "archived_raw" in resolution:
        clean_scalar(
            resolution.get("archived_raw"),
            "resolution archived line",
            MAX_SERIALIZED_ENTRY_CHARS,
        )
    if conflict is not None:
        if conflict_id != conflict["conflict_id"]:
            raise BimriError("resolution refers to the wrong conflict.")
        if proposal_ids != conflict["proposal_ids"]:
            raise BimriError(
                "resolution candidates do not match the conflict snapshot."
            )
        if choice not in {"current", "dismiss", *conflict["proposal_ids"]}:
            raise BimriError("resolution choice is invalid for this conflict.")
    return resolution


def create_system_conflict(paths, state, conflict_type, key, question, extra=None):
    for existing in open_conflicts(paths, state):
        if existing.get("type") == conflict_type and existing.get("key") == key:
            if extra:
                current_extra = existing.setdefault("extra", {})
                recovery_files = current_extra.setdefault(
                    "recovery_files", []
                )
                first = current_extra.get("recovery_file")
                if first and first not in recovery_files:
                    recovery_files.append(first)
                for recovery_file in extra.get("recovery_files", []):
                    if recovery_file not in recovery_files:
                        recovery_files.append(recovery_file)
                if not current_extra.get("recovery_file") and recovery_files:
                    current_extra["recovery_file"] = recovery_files[0]
                atomic_write_json(
                    conflict_path(paths, existing["conflict_id"]),
                    existing,
                )
            return existing["conflict_id"]
    conflict_id = allocate_conflict_id(paths, state)
    data = {
        "bimri_version": VERSION,
        "conflict_id": conflict_id,
        "type": conflict_type,
        "key": key,
        "created_at": now_iso(),
        "proposal_ids": [],
        "proposal_hashes": {},
        "current_line": None,
        "current_hash": "absent",
        "question": clean_scalar(question, "conflict question", 1000),
        "extra": extra or {},
    }
    exclusive_write_text(
        conflict_path(paths, conflict_id),
        json.dumps(data, indent=2, sort_keys=True) + "\n",
    )
    save_state(paths, state)
    return conflict_id


def create_proposal_conflict(paths, state, conflict_type, proposal, current, question):
    current_hash = line_hash(current["raw"]) if current else "absent"
    for existing in open_conflicts(paths, state):
        if (
            existing.get("type") == conflict_type
            and existing.get("key") == proposal["key"]
            and existing.get("current_hash") == current_hash
        ):
            hashes = existing.setdefault("proposal_hashes", {})
            for existing_id in existing.get("proposal_ids", []):
                hashes.setdefault(
                    existing_id, proposal_file_hash(paths, existing_id)
                )
            if proposal["proposal_id"] not in existing["proposal_ids"]:
                existing["proposal_ids"].append(proposal["proposal_id"])
                hashes[proposal["proposal_id"]] = proposal_file_hash(
                    paths, proposal["proposal_id"]
                )
                existing["question"] = clean_scalar(question, "conflict question", 1000)
            atomic_write_json(
                conflict_path(paths, existing["conflict_id"]), existing
            )
            return existing["conflict_id"]
    conflict_id = allocate_conflict_id(paths, state)
    data = {
        "bimri_version": VERSION,
        "conflict_id": conflict_id,
        "type": conflict_type,
        "key": proposal["key"],
        "created_at": now_iso(),
        "proposal_ids": [proposal["proposal_id"]],
        "proposal_hashes": {
            proposal["proposal_id"]: proposal_file_hash(
                paths, proposal["proposal_id"]
            )
        },
        "current_line": current["raw"] if current else None,
        "current_hash": current_hash,
        "question": clean_scalar(question, "conflict question", 1000),
    }
    exclusive_write_text(
        conflict_path(paths, conflict_id),
        json.dumps(data, indent=2, sort_keys=True) + "\n",
    )
    save_state(paths, state)
    return conflict_id


def write_decision(
    paths, proposal_id, outcome, replace_applying=False, **extra
):
    decision = {
        "bimri_version": VERSION,
        "proposal_id": proposal_id,
        "outcome": outcome,
        "recorded_at": now_iso(),
    }
    decision.update(extra)
    path = decision_path(paths, proposal_id)
    if not path.exists():
        exclusive_write_text(path, json.dumps(decision, indent=2, sort_keys=True) + "\n")
        return decision
    existing = validate_decision(
        read_json_strict(path, path.name), proposal_id
    )
    if replace_applying and existing["outcome"] == "applying":
        atomic_write_json(path, decision)
        return decision
    return existing


def next_entry_id(paths, run_id):
    log = run_log_path(paths, run_id)
    text = log.read_text(encoding="utf-8")
    numbers = [
        int(match.group(1))
        for match in re.finditer(
            rf"^\[ID:{re.escape(run_id)}-E(\d{{3}})\](?:\s|$)",
            text,
            re.MULTILINE,
        )
    ]
    number = max(numbers + [0]) + 1
    if number > 999:
        raise BimriError(
            f"{run_id} has exhausted its 999 journal entry IDs; close it and "
            "start a new run."
        )
    return f"{run_id}-E{number:03d}"


def next_proposal_id(paths, run_id):
    numbers = []
    for path in paths.proposals.glob(f"{run_id}-Q*.json"):
        match = re.fullmatch(rf"{re.escape(run_id)}-Q(\d{{3}})\.json", path.name)
        if match:
            numbers.append(int(match.group(1)))
    number = max(numbers + [0]) + 1
    if number > 999:
        raise BimriError(
            f"{run_id} has exhausted its 999 proposal IDs; close it and "
            "start a new run."
        )
    return f"{run_id}-Q{number:03d}"


def next_pattern_id(paths, state, entries):
    numbers = [state["pattern_count"]]
    for entry in entries:
        if entry["tier"] == 3 and re.fullmatch(r"P\d+", entry["id"]):
            numbers.append(int(entry["id"][1:]))
    for path in paths.proposals.glob("R*-Q*.json"):
        proposal = read_json_strict(path, path.name)
        pattern_id = proposal.get("pattern_id")
        if isinstance(pattern_id, str) and re.fullmatch(r"P\d+", pattern_id):
            numbers.append(int(pattern_id[1:]))
    number = max(numbers + [0]) + 1
    state["pattern_count"] = number
    return f"P{number:04d}"


def render_proposed_line(proposal, state):
    entry_id = proposal["entry_id"]
    pointer = f".bimri/log/{proposal['run_id']}.md"
    tags = ",".join(proposal.get("tags", []))
    if proposal["tier"] == 1:
        return (
            f"[{entry_id}] [K:{proposal['key']}] [{proposal['kind']}] "
            f"[T:{proposal['trust']}] [SRC:{proposal['source']}] "
            f"[{tags}] {proposal['text']} -> {pointer}"
        )
    if proposal["tier"] == 2:
        first = proposal.get("first_run") or proposal["run_id"]
        return (
            f"[{entry_id}] [K:{proposal['key']}] [I:{proposal['importance']}] "
            f"[{proposal['status']}] [T:{proposal['trust']}] "
            f"[SRC:{proposal['source']}] [F:{first}] [L:{proposal['run_id']}] "
            f"[{tags}] {proposal['text']} -> {pointer}"
        )
    return (
        f"[{proposal['pattern_id']}] [K:{proposal['key']}] "
        f"[{proposal['confidence']}] [obs:{proposal['observations']}] "
        f"[ev:{','.join(proposal['evidence'])}] {proposal['text']} "
        f"| Falsify: {proposal['falsifier']}"
    )


def validate_proposal(proposal, state=None):
    if not isinstance(proposal, dict):
        raise BimriError("proposal must be a JSON object.")
    if proposal.get("bimri_version") not in COMPATIBLE_ARTIFACT_VERSIONS:
        raise BimriError("proposal BIMRI version is invalid.")
    proposal_id = validate_fixed_id(
        proposal.get("proposal_id"), PROPOSAL_RE, "proposal ID"
    )
    run_id = validate_fixed_id(proposal.get("run_id"), RUN_RE, "proposal run ID")
    if not proposal_id.startswith(f"{run_id}-Q"):
        raise BimriError("proposal ID does not belong to its run.")
    clean_actor(proposal.get("actor"))
    clean_scalar(proposal.get("created_at"), "proposal timestamp", 30)
    try:
        dt.datetime.strptime(proposal["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise BimriError("proposal timestamp is invalid.") from exc
    if (
        isinstance(proposal.get("base_revision"), bool)
        or not isinstance(proposal.get("base_revision"), int)
        or proposal["base_revision"] < 0
    ):
        raise BimriError("proposal base revision is invalid.")
    base_hash = proposal.get("base_hash")
    if base_hash != "absent" and not (
        isinstance(base_hash, str) and HASH_RE.fullmatch(base_hash)
    ):
        raise BimriError("proposal base hash is invalid.")
    if proposal.get("operation") not in OPERATIONS:
        raise BimriError("proposal operation is invalid.")
    if proposal.get("tier") not in {1, 2, 3}:
        raise BimriError("proposal tier is invalid.")
    key = clean_key(proposal.get("key"))
    if key != proposal.get("key"):
        raise BimriError("proposal key must already be normalized.")
    target_id = proposal.get("target_id")
    if target_id is not None:
        validate_fixed_id(target_id, MEMORY_ID_RE, "proposal target ID")
    validate_fixed_id(proposal.get("entry_id"), ENTRY_RE, "proposal entry ID")
    if proposal.get("kind") not in TIER1_KINDS:
        raise BimriError("proposal kind is invalid.")
    importance = proposal.get("importance")
    if (
        isinstance(importance, bool)
        or not isinstance(importance, int)
        or importance not in range(1, 6)
    ):
        raise BimriError("proposal importance is invalid.")
    if proposal.get("status") not in TIER2_STATUSES:
        raise BimriError("proposal status is invalid.")
    trust = proposal.get("trust")
    source = proposal.get("source")
    if trust not in TRUSTS or source not in SOURCES:
        raise BimriError("proposal trust or source is invalid.")
    if trust == "confirmed" and source not in {"user", "system"}:
        raise BimriError(
            "only directly human-stated or system memory may be confirmed."
        )
    tags = proposal.get("tags")
    if not isinstance(tags, list) or clean_tags(tags) != tags:
        raise BimriError("proposal tags must be a normalized list.")
    max_chars = state["entry_max_chars"] if state else 500
    clean_scalar(proposal.get("text"), "proposal memory text", max_chars)
    if (
        proposal.get("tier") == 3
        and " | Falsify: " in proposal.get("text", "")
    ):
        raise BimriError(
            "Tier 3 memory text cannot contain the reserved delimiter "
            "' | Falsify: '."
        )
    clean_scalar(proposal.get("rationale"), "proposal rationale", 4000)
    if not isinstance(proposal.get("needs_human"), bool):
        raise BimriError("proposal needs_human must be true or false.")
    clean_scalar(
        proposal.get("question", ""), "proposal question", 1000, allow_empty=True
    )
    if proposal.get("confidence") not in PATTERN_CONFIDENCE:
        raise BimriError("proposal confidence is invalid.")
    observations = proposal.get("observations")
    if (
        isinstance(observations, bool)
        or not isinstance(observations, int)
        or observations < 1
    ):
        raise BimriError("proposal observations must be a positive integer.")
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list):
        raise BimriError("proposal evidence must be a list.")
    if len(evidence) > MAX_PATTERN_EVIDENCE:
        raise BimriError(
            f"proposal evidence exceeds {MAX_PATTERN_EVIDENCE} IDs."
        )
    for entry_id in evidence:
        validate_fixed_id(entry_id, LEGACY_ENTRY_RE, "proposal evidence ID")
    falsifier = clean_scalar(
        proposal.get("falsifier", ""),
        "proposal falsifier",
        500,
        allow_empty=True,
    )
    if proposal.get("tier") == 3 and " | Falsify: " in falsifier:
        raise BimriError(
            "Tier 3 falsifiers cannot contain the reserved delimiter "
            "' | Falsify: '."
        )
    if proposal["tier"] == 3:
        validate_fixed_id(
            proposal.get("pattern_id"), PATTERN_ID_RE, "proposal pattern ID"
        )
        if not evidence or not proposal.get("falsifier"):
            raise BimriError("Tier 3 proposals require evidence and a falsifier.")
        if proposal["operation"] != "set":
            raise BimriError("Tier 3 supports set proposals only.")
    first_run = proposal.get("first_run")
    if first_run is not None:
        validate_fixed_id(first_run, re.compile(r"^R\d+$"), "proposal first run ID")
    return proposal


def validate_decision(decision, proposal_id):
    if not isinstance(decision, dict):
        raise BimriError("decision must be a JSON object.")
    if decision.get("bimri_version") not in COMPATIBLE_ARTIFACT_VERSIONS:
        raise BimriError("decision BIMRI version is invalid.")
    if decision.get("proposal_id") != proposal_id:
        raise BimriError("decision proposal ID mismatch.")
    if decision.get("outcome") not in {
        "applying", "accepted", "noop", "contested"
    }:
        raise BimriError("decision outcome is invalid.")
    parse_timestamp(decision.get("recorded_at"), "decision timestamp")
    outcome = decision["outcome"]
    if outcome == "applying":
        base_hash = decision.get("base_hash")
        if base_hash != "absent" and not (
            isinstance(base_hash, str) and HASH_RE.fullmatch(base_hash)
        ):
            raise BimriError("applying decision base hash is invalid.")
        validate_revision_number(
            decision.get("revision_before"),
            "applying decision revision_before",
        )
    else:
        validate_revision_number(
            decision.get("revision"), f"{outcome} decision revision"
        )
    if outcome == "contested":
        validate_fixed_id(
            decision.get("conflict_id"),
            CONFLICT_RE,
            "decision conflict ID",
        )
    if outcome == "noop" and "resolution_id" not in decision:
        reason = clean_scalar(
            decision.get("reason"), "noop decision reason", 300
        )
        if reason != decision.get("reason"):
            raise BimriError("noop decision reason must already be normalized.")
    if "resolution_id" in decision:
        validate_fixed_id(
            decision.get("resolution_id"),
            CONFLICT_RE,
            "decision resolution ID",
        )
        parse_timestamp(
            decision.get("resolved_at"), "decision resolution timestamp"
        )
        clean_scalar(
            decision.get("resolution_choice"),
            "decision resolution choice",
            80,
        )
    if "initial_outcome" in decision and decision["initial_outcome"] not in {
        "applying", "accepted", "noop", "contested"
    }:
        raise BimriError("decision initial outcome is invalid.")
    return decision


def proposal_equivalent(proposal, current):
    if not current:
        return False
    if proposal["operation"] == "close":
        return False
    if proposal["operation"] == "touch":
        return False
    fields = ("key", "text")
    if any(str(proposal.get(field, "")) != str(current.get(field, "")) for field in fields):
        return False
    if proposal["tier"] != current["tier"]:
        return False
    if proposal["tier"] in {1, 2}:
        for field in ("trust", "source"):
            if str(proposal.get(field, "")) != str(current.get(field, "")):
                return False
        proposed_tags = ",".join(clean_tags(proposal.get("tags", [])))
        current_tags = ",".join(clean_tags(current.get("tags", "")))
        if proposed_tags != current_tags:
            return False
    if proposal["tier"] == 1:
        return proposal["kind"] == current.get("kind")
    if proposal["tier"] == 2:
        return (
            str(proposal["importance"]) == str(current.get("imp"))
            and proposal["status"] == current.get("status")
        )
    return (
        proposal["confidence"] == current.get("conf")
        and str(proposal["observations"]) == str(current.get("obs"))
        and ",".join(proposal["evidence"]) == current.get("ev", "")
        and proposal["falsifier"] == current.get("falsifier")
    )


def proposal_effect_reflected(proposal, current):
    if proposal["operation"] == "close":
        return current is None
    if proposal["operation"] == "touch":
        return bool(
            current
            and current["tier"] == 2
            and current.get("key") == proposal["key"]
            and current.get("last") == proposal["run_id"]
        )
    return proposal_equivalent(proposal, current)


def authority_proposal(paths, state, proposal_id):
    path = proposal_path(paths, proposal_id)
    if not path.exists() or path.is_symlink():
        raise BimriError(
            f"authority record refers to a missing or unsafe proposal: "
            f"{proposal_id}."
        )
    proposal = validate_proposal(
        read_json_strict(path, path.name), state
    )
    if proposal["proposal_id"] != proposal_id:
        raise BimriError("proposal filename does not match its ID.")
    return proposal


def authority_revision_entries(paths, state, number, label):
    validate_revision_number(number, f"{label} revision")
    if number > state["head_revision"]:
        raise BimriError(
            f"{label} refers to revision V{number:06d} beyond the current "
            f"canonical head V{state['head_revision']:06d}."
        )
    path = revision_path(paths, number)
    if not path.exists() or path.is_symlink():
        raise BimriError(
            f"{label} refers to a missing or unsafe revision "
            f"V{number:06d}."
        )
    try:
        content = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BimriError(
            f"{label} revision V{number:06d} is not valid UTF-8."
        ) from exc
    _, entries, errors, _ = validate_hot_content(
        content, state, allow_legacy_overflow=True
    )
    if errors:
        raise BimriError(
            f"{label} revision V{number:06d} is malformed: "
            + "; ".join(errors)
        )
    return entries


def validate_resolution_effect(paths, state, conflict, resolution):
    if resolution["status"] != "resolved":
        return resolution
    entries = authority_revision_entries(
        paths,
        state,
        resolution["revision_after"],
        f"resolution {resolution['conflict_id']}",
    )
    choice = resolution["choice"]
    current = find_entry(entries, conflict["key"])
    if choice in conflict["proposal_ids"]:
        proposal = human_approved_proposal(
            authority_proposal(paths, state, choice)
        )
        if not proposal_effect_reflected(proposal, current):
            raise BimriError(
                f"resolved conflict {conflict['conflict_id']} names revision "
                f"V{resolution['revision_after']:06d}, but that revision does "
                f"not contain the chosen proposal's effect."
            )
    else:
        actual_hash = line_hash(current["raw"]) if current else "absent"
        if actual_hash != conflict["current_hash"]:
            raise BimriError(
                f"resolved conflict {conflict['conflict_id']} names revision "
                f"V{resolution['revision_after']:06d}, but that revision does "
                "not preserve the recorded current value."
            )
    return resolution


def validate_decision_effect(paths, state, decision):
    outcome = decision["outcome"]
    proposal_id = decision["proposal_id"]
    proposal = authority_proposal(paths, state, proposal_id)
    if outcome == "applying":
        return decision
    if outcome == "contested":
        conflict_id = decision["conflict_id"]
        cpath = conflict_path(paths, conflict_id)
        if not cpath.exists():
            raise BimriError(
                f"contested decision refers to missing conflict {conflict_id}."
            )
        conflict = validate_conflict_record(
            paths,
            read_json_strict(cpath, cpath.name),
            expected_conflict_id=conflict_id,
        )
        if proposal_id not in conflict["proposal_ids"]:
            raise BimriError(
                "contested decision is not a candidate in its conflict."
            )
        return decision

    resolution_id = decision.get("resolution_id")
    if resolution_id:
        cpath = conflict_path(paths, resolution_id)
        rpath = resolution_file_path(paths, resolution_id)
        if not cpath.exists() or not rpath.exists():
            raise BimriError(
                "final decision refers to missing conflict resolution records."
            )
        conflict = validate_conflict_record(
            paths,
            read_json_strict(cpath, cpath.name),
            expected_conflict_id=resolution_id,
        )
        resolution = validate_resolution_record(
            read_json_strict(rpath, rpath.name),
            conflict=conflict,
            expected_conflict_id=resolution_id,
        )
        if resolution["status"] != "resolved":
            raise BimriError("final decision refers to an unfinished resolution.")
        validate_resolution_effect(paths, state, conflict, resolution)
        expected_outcome = (
            "accepted"
            if resolution["choice"] == proposal_id
            else "noop"
        )
        if outcome != expected_outcome:
            raise BimriError(
                "final decision outcome disagrees with its human resolution."
            )
        if decision["revision"] != resolution["revision_after"]:
            raise BimriError(
                "final decision revision disagrees with its human resolution."
            )
        if decision.get("resolution_choice") != resolution["choice"]:
            raise BimriError(
                "final decision choice disagrees with its human resolution."
            )
        if decision.get("resolved_at") != resolution["resolved_at"]:
            raise BimriError(
                "final decision timestamp disagrees with its human resolution."
            )
        return decision

    if outcome == "noop" and decision.get("reason") not in {
        "current memory already matches",
        "memory already absent",
        "proposal produced no memory change",
    }:
        raise BimriError("noop decision has an unknown deterministic reason.")
    entries = authority_revision_entries(
        paths,
        state,
        decision["revision"],
        f"decision {proposal_id}",
    )
    current = find_entry(entries, proposal["key"])
    if not proposal_effect_reflected(proposal, current):
        raise BimriError(
            f"{outcome} decision {proposal_id} names revision "
            f"V{decision['revision']:06d}, but that revision does not contain "
            "the proposal's recorded effect."
        )
    return decision


def validate_conflict_candidate_decisions(paths, conflict, resolution=None):
    for proposal_id in conflict["proposal_ids"]:
        path = decision_path(paths, proposal_id)
        if not path.exists() or path.is_symlink():
            raise BimriError(
                f"conflict {conflict['conflict_id']} candidate {proposal_id} "
                "has no safe decision record; retry that run's sync first."
            )
        decision = validate_decision(
            read_json_strict(path, path.name), proposal_id
        )
        if decision["outcome"] == "contested":
            if decision.get("conflict_id") != conflict["conflict_id"]:
                raise BimriError(
                    f"candidate {proposal_id} points to the wrong conflict."
                )
            continue
        if not (
            resolution
            and resolution["status"] == "resolved"
            and decision.get("resolution_id") == conflict["conflict_id"]
            ):
            raise BimriError(
                f"candidate {proposal_id} has an invalid decision state for "
                f"conflict {conflict['conflict_id']}."
            )
        expected_outcome = (
            "accepted"
            if resolution["choice"] == proposal_id
            else "noop"
        )
        if (
            decision["outcome"] != expected_outcome
            or decision.get("resolution_choice") != resolution["choice"]
            or decision.get("resolved_at") != resolution["resolved_at"]
            or decision.get("revision") != resolution["revision_after"]
        ):
            raise BimriError(
                f"candidate {proposal_id} final decision disagrees with "
                f"resolution {conflict['conflict_id']}."
            )


def proposal_base_entry(paths, proposal):
    path = revision_path(paths, proposal["base_revision"])
    if not path.exists() or path.is_symlink():
        raise BimriError("proposal base revision is missing or unsafe.")
    content = path.read_text(encoding="utf-8")
    _, entries, errors = parse_hot(content)
    if errors:
        raise BimriError("proposal base revision is malformed.")
    return resolve_entry(
        entries, proposal["key"], proposal.get("target_id")
    )


def append_archive(paths, proposal_id, raw_line, reason):
    target = paths.archive / f"{dt.date.today():%Y-%m}.md"
    if target.is_symlink():
        raise BimriError("monthly archive file cannot be a symbolic link.")
    proposal_id = validate_fixed_id(
        proposal_id, PROPOSAL_RE, "archive proposal ID"
    )
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            match = re.match(
                r"^\[ARCHIVED:\d{4}-\d{2}-\d{2}\] "
                r"\[BY:(R\d{6}-Q\d{3})\](?:\s|$)",
                line,
            )
            if match and match.group(1) == proposal_id:
                return
    append_line(
        target,
        f"[ARCHIVED:{today()}] [BY:{proposal_id}] "
        f"[{clean_scalar(reason, 'archive reason', 100)}] "
        f"{clean_scalar(raw_line, 'archived line', MAX_SERIALIZED_ENTRY_CHARS)}",
    )


def apply_proposal(paths, state, proposal, force=False):
    validate_proposal(proposal, state)
    proposal_decision_path = decision_path(paths, proposal["proposal_id"])
    existing_decision = None
    if proposal_decision_path.exists() and not force:
        existing_decision = validate_decision(
            read_json_strict(
                proposal_decision_path, proposal_decision_path.name
            ),
            proposal["proposal_id"],
        )
        if existing_decision["outcome"] != "applying":
            return effective_decision(paths, state, existing_decision)
    content = revision_path(paths, state["head_revision"]).read_text(encoding="utf-8")
    lines, entries, parse_errors, _ = validate_hot_content(
        content, state, allow_legacy_overflow=True
    )
    if parse_errors:
        raise BimriError("hot memory must be repaired before applying proposals: "
                         + "; ".join(parse_errors))
    current = resolve_entry(
        entries, proposal.get("key"), proposal.get("target_id")
    )

    if existing_decision and proposal_effect_reflected(proposal, current):
        if proposal["operation"] == "close":
            base_entry = proposal_base_entry(paths, proposal)
            if base_entry:
                append_archive(
                    paths, proposal["proposal_id"], base_entry["raw"], "closed"
                )
        return write_decision(
            paths, proposal["proposal_id"], "accepted",
            replace_applying=True, revision=state["head_revision"],
            recovered_from_intent=True,
        )

    if proposal_equivalent(proposal, current):
        return write_decision(
            paths, proposal["proposal_id"], "noop",
            replace_applying=True,
            reason="current memory already matches", revision=state["head_revision"],
        )

    conflict_type = None
    question = None
    if proposal.get("needs_human") and not force:
        conflict_type = "agent-declared"
        question = proposal.get("question") or (
            f"An agent asked you to decide what BIMRI should remember for "
            f"{proposal['key']}."
        )
    elif (
        proposal["tier"] == 1
        and proposal["source"] not in {"user", "system"}
        and not force
    ):
        conflict_type = "approval"
        question = (
            f"Should BIMRI promote this {proposal['kind']} to core memory? "
            f"{proposal['text']}"
        )
    elif current and current.get("trust") == "confirmed" and proposal["source"] != "user" and not force:
        conflict_type = "confirmed-change"
        question = (
            f"An agent proposed changing confirmed memory for {proposal['key']}. "
            f"Keep the current value or accept the proposal?"
        )
    elif proposal["operation"] in {"set", "touch", "close"}:
        expected = proposal.get("base_hash", "absent")
        actual = line_hash(current["raw"]) if current else "absent"
        if expected != actual and not force:
            conflict_type = "stale-base"
            question = (
                f"Memory changed while agents were working on {proposal['key']}. "
                "Which version should BIMRI remember?"
            )

    if conflict_type:
        conflict_id = create_proposal_conflict(
            paths, state, conflict_type, proposal, current, question
        )
        return write_decision(
            paths, proposal["proposal_id"], "contested",
            replace_applying=True,
            conflict_id=conflict_id, revision=state["head_revision"],
        )

    new_lines = list(lines)
    archived_raw = None
    if proposal["operation"] == "close":
        if not current:
            return write_decision(
                paths, proposal["proposal_id"], "noop",
                replace_applying=True,
                reason="memory already absent", revision=state["head_revision"],
            )
        archived_raw = current["raw"]
        del new_lines[current["line"]]
    elif proposal["operation"] == "touch":
        if not current or current["tier"] != 2:
            raise BimriError("touch requires an existing Tier 2 entry.")
        data = dict(proposal)
        data.update({
            "tier": 2,
            "entry_id": current["id"],
            "key": current["key"],
            "importance": int(current["imp"]),
            "status": current["status"],
            "trust": current["trust"],
            "source": current["source"],
            "tags": clean_tags(current.get("tags", "")),
            "text": current["text"],
            "first_run": current["first"],
        })
        new_lines[current["line"]] = render_proposed_line(data, state)
    else:
        new_line = render_proposed_line(proposal, state)
        if current:
            if current["tier"] == proposal["tier"]:
                new_lines[current["line"]] = new_line
            else:
                del new_lines[current["line"]]
                new_lines = insert_in_tier(new_lines, proposal["tier"], new_line)
        else:
            new_lines = insert_in_tier(new_lines, proposal["tier"], new_line)

    new_content = render_content(new_lines)
    if new_content == content:
        return write_decision(
            paths, proposal["proposal_id"], "noop",
            replace_applying=True,
            reason="proposal produced no memory change",
            revision=state["head_revision"],
        )
    _, _, validation_errors, _ = validate_hot_content(new_content, state)
    legacy_reduction = strictly_reduces_overflow(content, new_content, state)
    if validation_errors and not legacy_reduction and not force:
        conflict_id = create_proposal_conflict(
            paths, state, "capacity-or-validation", proposal, current,
            "This memory change needs your decision because it would exceed a "
            "memory cap or create invalid state.",
        )
        return write_decision(
            paths, proposal["proposal_id"], "contested",
            replace_applying=True,
            conflict_id=conflict_id, errors=validation_errors,
            revision=state["head_revision"],
        )
    if validation_errors and not legacy_reduction:
        raise BimriError("resolved proposal is still invalid: " + "; ".join(validation_errors))

    if not force:
        write_decision(
            paths, proposal["proposal_id"], "applying",
            base_hash=proposal["base_hash"],
            revision_before=state["head_revision"],
        )
    if archived_raw:
        append_archive(
            paths, proposal["proposal_id"], archived_raw, "closed"
        )
    revision = commit_revision(
        paths, state, new_content, f"accepted {proposal['proposal_id']}",
        allow_legacy_overflow=legacy_reduction,
    )
    if force:
        return {
            "bimri_version": VERSION,
            "proposal_id": proposal["proposal_id"],
            "outcome": "accepted",
            "recorded_at": now_iso(),
            "revision": revision,
        }
    return write_decision(
        paths, proposal["proposal_id"], "accepted",
        replace_applying=True, revision=revision,
    )


def process_run_proposals(paths, state, run_id):
    results = []
    for path in sorted(paths.proposals.glob(f"{run_id}-Q*.json")):
        proposal = read_json_strict(path, path.name)
        if proposal.get("proposal_id") != path.stem:
            raise BimriError(f"proposal ID mismatch in {path.name}")
        results.append(apply_proposal(paths, state, proposal))
    return results


def build_index(paths, state):
    content = revision_path(paths, state["head_revision"]).read_text(encoding="utf-8")
    _, entries, errors = parse_hot(content)
    if errors:
        raise BimriError("cannot index malformed hot memory: " + "; ".join(errors))
    rows = []
    for entry in entries:
        rows.append([
            entry["id"], entry.get("key", ""), f"T{entry['tier']}",
            entry.get("trust", ""), entry.get("source", ""),
            entry.get("status") or entry.get("kind") or entry.get("conf", ""),
            "bimri.md", entry.get("text", "")[:160],
        ])
    for log in sorted(paths.logs.glob("R*.md")):
        if log.is_symlink():
            raise BimriError(f"run log cannot be a symbolic link: {log.name}")
        for line in log.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\[ID:(R\d+-E\d+)\]", line.strip())
            if match:
                rows.append([
                    match.group(1), "", "log", "", "", "detail",
                    log.relative_to(paths.root).as_posix(), line.strip()[:160],
                ])
    for archive in sorted(paths.archive.glob("*.md")):
        if archive.is_symlink():
            raise BimriError(
                f"archive file cannot be a symbolic link: {archive.name}"
            )
        for line in archive.read_text(encoding="utf-8").splitlines():
            match = re.search(r"\[(R\d+-E\d+|P\d+)\]", line)
            if match:
                rows.append([
                    match.group(1), "", "archive", "", "", "archived",
                    archive.relative_to(paths.root).as_posix(), line[:160],
                ])
    safe_rows = []
    for row in rows:
        safe_rows.append([
            clean_scalar(str(cell), "index field", 500, allow_empty=True)
            for cell in row
        ])
    safe_rows.sort(key=lambda row: (row[0], row[2], row[6]))
    header = "id\tkey\tloc\ttrust\tsource\tstatus\tfile\theadline\n"
    text = header + "\n".join("\t".join(row) for row in safe_rows)
    atomic_write_text(paths.index, text.rstrip() + "\n")
    return len(safe_rows)


def rebuild_index_best_effort(paths, state):
    try:
        return build_index(paths, state)
    except Exception as exc:
        print(
            "BIMRI WARNING: the durable operation succeeded, but the derived "
            f"index could not be rebuilt: {exc}. Run `doctor` after repairing "
            "the reported file.",
            file=sys.stderr,
        )
        return None


def print_brief(paths, state, run_id, reused=False):
    content = revision_path(paths, state["head_revision"]).read_text(encoding="utf-8")
    _, _, errors, counts = validate_hot_content(content, state, allow_legacy_overflow=True)
    print(
        f"=== BIMRI BRIEF {run_id} | {today()} | "
        f"{state['active_runs'][run_id]['actor']} ==="
    )
    print(
        f"Memory revision V{state['head_revision']:06d} | "
        f"~{len(content) // 4} tokens | "
        f"T1 {counts[1]}/{state['tier1_max']} "
        f"T2 {counts[2]}/{state['tier2_max']} "
        f"T3 {counts[3]}/{state['tier3_max']}"
    )
    if reused:
        print("Resumed existing run handle for this harness session.")
    conflicts = open_conflicts(paths, state)
    if conflicts:
        print("HUMAN DECISION NEEDED:")
        for conflict in conflicts[:5]:
            print(f"  - {conflict['conflict_id']} [{conflict['key']}]: "
                  f"{conflict['question']}")
            for proposal_id in conflict.get("proposal_ids", []):
                proposal = read_json_strict(proposal_path(paths, proposal_id), proposal_id)
                print(f"      {proposal_id}: {proposal.get('text', '')}")
            if conflict.get("current_line"):
                print(f"      current: {conflict['current_line']}")
    stale = []
    now = dt.datetime.now(dt.timezone.utc)
    for rid, meta in state["active_runs"].items():
        if rid == run_id:
            continue
        try:
            started = dt.datetime.strptime(meta["started_at"], "%Y-%m-%dT%H:%M:%SZ")
            started = started.replace(tzinfo=dt.timezone.utc)
            if (now - started).total_seconds() > 86400:
                stale.append(rid)
        except (KeyError, ValueError):
            stale.append(rid)
    for log in sorted(paths.logs.glob("R*.md")):
        rid = log.stem
        if rid in state["active_runs"] or not re.fullmatch(r"R\d+", rid):
            continue
        if log.is_symlink():
            stale.append(rid)
            continue
        try:
            log_text = log.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            stale.append(rid)
            continue
        if not any(
            line.strip().startswith("[CLOSED:")
            for line in log_text.splitlines()
        ):
            stale.append(rid)
    stale = sorted(set(stale))
    if stale:
        print("ORPHAN CANDIDATES (never auto-closed): " + ", ".join(stale))
    if errors:
        print("VALIDATION NEEDED:")
        for error in errors[:10]:
            print(f"  - {error}")
    print(
        f"Run handle: {run_id}. Journal and propose through bimri-engine.py; "
        f"close explicitly with --run {run_id}."
    )
    print("=== END BRIEF ===")


def cmd_start(paths, actor, session=None):
    actor = clean_actor(actor)
    with engine_lock(paths):
        state = load_or_initialize(paths)
        sync_generated_view(paths, state)
        skey = session_key(actor, session)
        if skey and skey in state["session_runs"]:
            existing = state["session_runs"][skey]
            if existing in state["active_runs"]:
                print_brief(paths, state, existing, reused=True)
                return existing
        existing_numbers = [
            int(path.stem[1:]) for path in paths.logs.glob("R*.md")
            if RUN_RE.fullmatch(path.stem)
        ]
        number = max([state["run_count"]] + existing_numbers + [0]) + 1
        while True:
            if number > 999999:
                raise BimriError("BIMRI has exhausted its six-digit run ID space.")
            run_id = f"R{number:06d}"
            log = run_log_path(paths, run_id)
            try:
                exclusive_write_text(
                    log,
                    f"# Run {run_id} | {now_iso()} | actor:{actor} | "
                    f"base:V{state['head_revision']:06d}\n\n"
                    "## Journal\n\n"
                    "<!-- Use the host-only argv_prefix recorded in "
                    ".bimri/runtime.local.json with "
                    f"`journal --run {run_id} --text \"...\"` "
                    "for durable detail. -->\n\n"
                    "## Proposals\n\n"
                    "## Outcome\n\n",
                )
                break
            except FileExistsError:
                number += 1
        state["run_count"] = number
        state["run_dates"][run_id] = today()
        state["active_runs"][run_id] = {
            "actor": actor,
            "session_key": skey,
            "started_at": now_iso(),
            "last_activity_at": now_iso(),
            "base_revision": state["head_revision"],
        }
        if skey:
            state["session_runs"][skey] = run_id
        state["last_started_at"] = now_iso()
        save_state(paths, state)
        print(f"BIMRI RUN HANDLE: {run_id}", flush=True)
        print_brief(paths, state, run_id)
        rebuild_index_best_effort(paths, state)
        return run_id


def require_active_run(paths, state, run_id):
    validate_fixed_id(run_id, RUN_RE, "run ID")
    if run_id not in state["active_runs"]:
        raise BimriError(f"{run_id} is not an active run.")
    log = run_log_path(paths, run_id)
    if not log.exists():
        raise BimriError(f"{run_id} log is missing.")
    return log


def cmd_journal(paths, run_id, text, importance=3):
    text = clean_scalar(text, "journal text", 8000)
    if importance not in range(1, 6):
        raise BimriError("importance must be 1 through 5.")
    with engine_lock(paths):
        state = load_or_initialize(paths)
        log = require_active_run(paths, state, run_id)
        entry_id = next_entry_id(paths, run_id)
        append_line(log, f"[ID:{entry_id}] [I:{importance}] {text}")
        state["active_runs"][run_id]["last_activity_at"] = now_iso()
        save_state(paths, state)
    print(entry_id)
    return entry_id


def cmd_propose(paths, args):
    run_id = validate_fixed_id(args.run, RUN_RE, "run ID")
    operation = args.operation
    if operation not in OPERATIONS:
        raise BimriError(f"unsupported operation: {operation}")
    key = clean_key(args.key)
    source = args.source
    trust = args.trust
    if source not in SOURCES:
        raise BimriError(f"unsupported source: {source}")
    if trust not in TRUSTS:
        raise BimriError(f"unsupported trust: {trust}")
    if trust == "confirmed" and source not in {"user", "system"}:
        raise BimriError("only directly human-stated or system memory may be confirmed.")
    if args.target:
        validate_fixed_id(args.target, MEMORY_ID_RE, "target ID")
    with engine_lock(paths):
        state = load_or_initialize(paths)
        sync_generated_view(paths, state)
        log = require_active_run(paths, state, run_id)
        run_meta = state["active_runs"][run_id]
        base_revision = run_meta["base_revision"]
        base_path = revision_path(paths, base_revision)
        if not base_path.exists():
            raise BimriError(
                f"{run_id} base revision V{base_revision:06d} is missing."
            )
        base_content = base_path.read_text(encoding="utf-8")
        _, base_entries, errors = parse_hot(base_content)
        if errors:
            raise BimriError(
                "repair the run's base memory before proposing: "
                + "; ".join(errors)
            )
        base_entry = resolve_entry(
            base_entries, key, args.target, require_target=bool(args.target)
        )
        if operation in {"touch", "close"} and not base_entry:
            raise BimriError(f"{operation} requires an existing key or target.")
        if operation == "touch" and base_entry["tier"] != 2:
            raise BimriError("only Tier 2 entries can be touched.")

        proposal_id = next_proposal_id(paths, run_id)
        entry_id = next_entry_id(paths, run_id)
        tier = args.tier or (base_entry["tier"] if base_entry else None)
        if tier not in {1, 2, 3}:
            raise BimriError("tier must be 1, 2 or 3.")
        if tier == 1 and args.kind not in TIER1_KINDS:
            raise BimriError("Tier 1 requires a valid --kind.")
        if tier == 2 and args.status not in TIER2_STATUSES:
            raise BimriError("Tier 2 requires a valid --status.")
        if operation in {"touch", "close"}:
            text = base_entry["text"]
            if len(text) > state["entry_max_chars"]:
                # Touch and close act on the immutable base snapshot and do not
                # author text. Keep the proposal itself within the current
                # authoring cap while the base hash retains exact authority.
                descriptor = (
                    f"{operation} inherited {base_entry['id']} "
                    f"sha256:{sha256_text(text)}"
                )
                text = descriptor[:state["entry_max_chars"]]
        else:
            text = clean_scalar(
                args.text, "memory text", state["entry_max_chars"]
            )
        rationale = clean_scalar(
            args.rationale or text, "proposal rationale", 4000
        )
        if tier == 3:
            if operation != "set":
                raise BimriError("Tier 3 currently supports set proposals only.")
            if args.confidence not in PATTERN_CONFIDENCE:
                raise BimriError("Tier 3 requires a valid --confidence.")
            if not isinstance(args.observations, int) or args.observations < 1:
                raise BimriError("--observations must be a positive integer.")
            if not args.falsifier:
                raise BimriError("Tier 3 requires --falsifier.")
            evidence = [
                validate_fixed_id(item.strip(), LEGACY_ENTRY_RE, "evidence ID")
                for item in (args.evidence or entry_id).split(",")
            ]
            pattern_id = (
                base_entry["id"]
                if base_entry and base_entry["tier"] == 3
                else next_pattern_id(paths, state, base_entries)
            )
        else:
            evidence = []
            pattern_id = None

        proposal = {
            "bimri_version": VERSION,
            "proposal_id": proposal_id,
            "run_id": run_id,
            "actor": state["active_runs"][run_id]["actor"],
            "created_at": now_iso(),
            "base_revision": base_revision,
            "base_hash": line_hash(base_entry["raw"]) if base_entry else "absent",
            "operation": operation,
            "tier": tier,
            "key": key,
            "target_id": base_entry["id"] if base_entry else args.target,
            "entry_id": entry_id,
            "kind": args.kind,
            "importance": args.importance,
            "status": args.status,
            "trust": trust,
            "source": source,
            "tags": clean_tags(args.tags),
            "text": text,
            "rationale": rationale,
            "needs_human": bool(args.needs_human),
            "question": clean_scalar(
                args.question or "", "question", 1000, allow_empty=True
            ),
            "confidence": args.confidence,
            "observations": args.observations,
            "evidence": evidence,
            "falsifier": clean_scalar(
                args.falsifier or "", "falsifier", 500, allow_empty=True
            ),
            "pattern_id": pattern_id,
            "first_run": (
                base_entry.get("first")
                if base_entry and base_entry["tier"] == 2
                else run_id
            ),
        }
        validate_proposal(proposal, state)
        append_line(log, f"[ID:{entry_id}] [I:{args.importance}] {rationale}")
        exclusive_write_text(
            proposal_path(paths, proposal_id),
            json.dumps(proposal, indent=2, sort_keys=True) + "\n",
        )
        append_line(
            log,
            f"[PROPOSE:{proposal_id}] [{operation}] [K:{key}] "
            f"[BASE:{proposal['base_hash']}] {text}",
        )
        state["active_runs"][run_id]["last_activity_at"] = now_iso()
        save_state(paths, state)
    print(proposal_id)
    return proposal_id


def resolve_run_from_args(state, run_id=None, actor=None, session=None):
    if run_id:
        return validate_fixed_id(run_id, RUN_RE, "run ID")
    if actor or session:
        if not actor or not session:
            raise BimriError(
                "--actor and --session must be provided together, or use --run."
            )
        skey = session_key(clean_actor(actor), session)
        candidate = state["session_runs"].get(skey)
        if candidate:
            return candidate
        raise BimriError(
            "no active BIMRI run is mapped to that actor and session."
        )
    active = sorted(state["active_runs"])
    if len(active) == 1:
        return active[0]
    if not active:
        raise BimriError("there are no active runs to close.")
    raise BimriError(
        "multiple runs are active; close explicitly with --run. Active: "
        + ", ".join(active)
    )


def cmd_sync(paths, run_id):
    with engine_lock(paths):
        state = load_or_initialize(paths)
        sync_generated_view(paths, state)
        require_active_run(paths, state, run_id)
        results = process_run_proposals(paths, state, run_id)
        state["active_runs"][run_id]["base_revision"] = state["head_revision"]
        state["active_runs"][run_id]["last_activity_at"] = now_iso()
        save_state(paths, state)
        rebuild_index_best_effort(paths, state)
    accepted = sum(item["outcome"] in {"accepted", "noop"} for item in results)
    contested = sum(item["outcome"] == "contested" for item in results)
    print(f"BIMRI: synced {run_id}; accepted {accepted}, contested {contested}.")
    return results


def cmd_close(paths, run_id=None, actor=None, session=None,
              outcome="partial", summary=None):
    if outcome not in OUTCOMES:
        raise BimriError("outcome must be success, partial, overflow or fail.")
    summary = clean_scalar(
        summary or "session ended without a detailed outcome",
        "outcome summary", 1000,
    )
    with engine_lock(paths):
        state = load_or_initialize(paths)
        sync_generated_view(paths, state)
        rid = resolve_run_from_args(state, run_id, actor, session)
        log = require_active_run(paths, state, rid)
        results = process_run_proposals(paths, state, rid)
        text = log.read_text(encoding="utf-8")
        if not any(
            line.strip().startswith("[CLOSED:")
            for line in text.splitlines()
        ):
            append_line(log, f"[OUTCOME:{outcome}] {summary}")
            append_line(log, f"[CLOSED:{rid} {now_iso()}]")
        meta = state["active_runs"].pop(rid)
        skey = meta.get("session_key")
        if skey and state["session_runs"].get(skey) == rid:
            del state["session_runs"][skey]
        state["last_closed_at"] = now_iso()
        save_state(paths, state)
        rebuild_index_best_effort(paths, state)
    accepted = sum(item["outcome"] in {"accepted", "noop"} for item in results)
    contested = sum(item["outcome"] == "contested" for item in results)
    print(
        f"BIMRI: run {rid} closed. Memory proposals accepted {accepted}; "
        f"human decisions needed {contested}."
    )
    return rid


def cmd_recover_run(paths, run_id, outcome, summary):
    return cmd_close(
        paths,
        run_id=validate_fixed_id(run_id, RUN_RE, "run ID"),
        outcome=outcome,
        summary=clean_scalar(summary, "recovery summary", 1000),
    )


def human_approved_proposal(proposal):
    approved = dict(proposal)
    approved["source"] = "user"
    if approved["tier"] in {1, 2}:
        approved["trust"] = "confirmed"
    return approved


def verify_conflict_candidates(paths, conflict):
    validate_conflict_record(paths, conflict, verify_candidates=True)


def finalize_conflict_decisions(paths, resolution):
    resolution = validate_resolution_record(resolution)
    if resolution["status"] != "resolved":
        raise BimriError("cannot finalize decisions from an unfinished resolution.")
    chosen = resolution["choice"]
    planned = []
    for proposal_id in resolution.get("proposal_ids", []):
        path = decision_path(paths, proposal_id)
        if not path.exists() or path.is_symlink():
            raise BimriError(
                f"cannot finalize {resolution['conflict_id']}: candidate "
                f"decision {proposal_id} is missing or unsafe."
            )
        decision = validate_decision(
            read_json_strict(path, path.name), proposal_id
        )
        final_outcome = "accepted" if chosen == proposal_id else "noop"
        already_final = (
            decision["outcome"] == final_outcome
            and decision.get("resolution_id") == resolution["conflict_id"]
            and decision.get("resolution_choice") == chosen
            and decision.get("resolved_at") == resolution["resolved_at"]
            and decision.get("revision") == resolution["revision_after"]
        )
        if already_final:
            continue
        if not (
            decision["outcome"] == "contested"
            and decision.get("conflict_id") == resolution["conflict_id"]
        ):
            raise BimriError(
                f"cannot finalize {proposal_id}: its decision is neither the "
                "recorded contested candidate nor the exact resolved outcome."
            )
        updated = dict(decision)
        updated.update({
            "initial_outcome": decision.get(
                "initial_outcome", decision["outcome"]
            ),
            "outcome": final_outcome,
            "resolution_id": resolution["conflict_id"],
            "resolution_choice": chosen,
            "resolved_at": resolution["resolved_at"],
            "revision": resolution["revision_after"],
        })
        planned.append((path, updated))
    for path, updated in planned:
        atomic_write_json(path, updated)


def effective_decision(paths, state, decision):
    validate_decision_effect(paths, state, decision)
    if decision["outcome"] != "contested":
        return decision
    conflict_id = validate_fixed_id(
        decision.get("conflict_id"), CONFLICT_RE, "decision conflict ID"
    )
    cpath = conflict_path(paths, conflict_id)
    if not cpath.exists():
        raise BimriError(
            f"decision refers to missing conflict {conflict_id}."
        )
    conflict = validate_conflict_record(
        paths,
        read_json_strict(cpath, cpath.name),
        expected_conflict_id=conflict_id,
    )
    if decision["proposal_id"] not in conflict["proposal_ids"]:
        raise BimriError(
            "contested decision is not a candidate in its conflict."
        )
    path = resolution_file_path(paths, conflict_id)
    if not path.exists():
        return decision
    resolution = validate_resolution_record(
        read_json_strict(path, path.name),
        conflict=conflict,
        expected_conflict_id=conflict_id,
    )
    if resolution["status"] != "resolved":
        return decision
    validate_resolution_effect(paths, state, conflict, resolution)
    effective = dict(decision)
    effective["outcome"] = (
        "accepted"
        if resolution.get("choice") == decision["proposal_id"]
        else "noop"
    )
    effective["resolution_id"] = conflict_id
    effective["revision"] = resolution.get("revision_after")
    return effective


def cmd_resolve(paths, conflict_id, choice):
    conflict_id = validate_fixed_id(conflict_id, CONFLICT_RE, "conflict ID")
    choice = clean_scalar(choice, "resolution choice", 80)
    with engine_lock(paths):
        state = load_or_initialize(paths)
        sync_generated_view(paths, state)
        cpath = conflict_path(paths, conflict_id)
        if not cpath.exists():
            raise BimriError(f"unknown conflict: {conflict_id}")
        conflict = validate_conflict_record(
            paths,
            read_json_strict(cpath, cpath.name),
            expected_conflict_id=conflict_id,
        )
        proposal_ids = conflict["proposal_ids"]
        allowed = {"current", "dismiss", *proposal_ids}
        if choice not in allowed:
            raise BimriError(
                "choose current, dismiss, or one of: "
                + ", ".join(proposal_ids)
            )
        verify_conflict_candidates(paths, conflict)

        resolution_path = resolution_file_path(paths, conflict_id)
        existing = (
            validate_resolution_record(
                read_json_strict(resolution_path, resolution_path.name),
                conflict=conflict,
                expected_conflict_id=conflict_id,
            )
            if resolution_path.exists()
            else None
        )
        validate_conflict_candidate_decisions(paths, conflict, existing)
        if existing and existing["status"] == "resolved":
            validate_resolution_effect(paths, state, conflict, existing)
            finalize_conflict_decisions(paths, existing)
            rebuild_index_best_effort(paths, state)
            print(
                f"BIMRI: {conflict_id} already resolved as "
                f"{existing['choice']}."
            )
            return existing
        if (
            existing
            and existing["status"] == "applying"
            and existing.get("choice") != choice
        ):
            raise BimriError(
                f"{conflict_id} is already applying choice "
                f"{existing.get('choice')}; retry that choice."
            )

        content = revision_path(
            paths, state["head_revision"]
        ).read_text(encoding="utf-8")
        _, entries, errors = parse_hot(content)
        if errors:
            raise BimriError("repair hot memory before resolving conflicts.")
        current = find_entry(entries, conflict["key"])
        actual_hash = line_hash(current["raw"]) if current else "absent"

        proposal = None
        if choice in proposal_ids:
            proposal = validate_proposal(
                read_json_strict(
                    proposal_path(paths, choice), f"proposal {choice}"
                ),
                state,
            )
            proposal = human_approved_proposal(proposal)
            if existing and proposal_effect_reflected(proposal, current):
                if proposal["operation"] == "close":
                    archived_raw = (
                        existing.get("archived_raw")
                        or conflict.get("current_line")
                    )
                    if archived_raw:
                        if line_hash(archived_raw) != conflict.get(
                            "current_hash"
                        ):
                            raise BimriError(
                                "resolution archive provenance no longer "
                                "matches the conflict."
                            )
                        append_archive(
                            paths, proposal["proposal_id"],
                            clean_scalar(
                                archived_raw,
                                "resolution archived line",
                                MAX_SERIALIZED_ENTRY_CHARS,
                            ),
                            "closed",
                        )
                resolution = dict(existing)
                resolution.update({
                    "status": "resolved",
                    "resolved_at": now_iso(),
                    "revision_after": state["head_revision"],
                })
                validate_resolution_effect(
                    paths, state, conflict, resolution
                )
                atomic_write_json(resolution_path, resolution)
                finalize_conflict_decisions(paths, resolution)
                rebuild_index_best_effort(paths, state)
                print(f"BIMRI: {conflict_id} resolved with {choice}.")
                return resolution

        if actual_hash != conflict.get("current_hash"):
            raise BimriError(
                "memory changed after this question was raised. Ask the agent "
                "to review the latest state before resolving it."
            )

        resolution = {
            "bimri_version": VERSION,
            "conflict_id": conflict_id,
            "choice": choice,
            "status": "applying",
            "started_at": (
                existing.get("started_at") if existing else now_iso()
            ),
            "by": "user",
            "proposal_ids": proposal_ids,
            "revision_before": (
                existing.get("revision_before")
                if existing else state["head_revision"]
            ),
        }
        if (
            proposal
            and proposal["operation"] == "close"
            and current is not None
        ):
            resolution["archived_raw"] = clean_scalar(
                current["raw"],
                "resolution archived line",
                MAX_SERIALIZED_ENTRY_CHARS,
            )
        atomic_write_json(resolution_path, resolution)
        try:
            if choice in {"current", "dismiss"}:
                revision_after = state["head_revision"]
            else:
                result = apply_proposal(paths, state, proposal, force=True)
                revision_after = result.get(
                    "revision", state["head_revision"]
                )
        except (BimriError, OSError, UnicodeError) as exc:
            resolution.update({
                "status": "failed",
                "failed_at": now_iso(),
                "error": clean_scalar(
                    str(exc), "resolution error", 1000
                ),
            })
            atomic_write_json(resolution_path, resolution)
            raise
        resolution.update({
            "status": "resolved",
            "resolved_at": now_iso(),
            "revision_after": revision_after,
        })
        resolution.pop("error", None)
        validate_resolution_effect(paths, state, conflict, resolution)
        atomic_write_json(resolution_path, resolution)
        finalize_conflict_decisions(paths, resolution)
        rebuild_index_best_effort(paths, state)
    print(f"BIMRI: {conflict_id} resolved with {choice}.")
    return resolution


def lookup(table, value):
    for limit, multiplier in table:
        if value <= limit:
            return multiplier
    return table[-1][1]


def run_number(run_id):
    match = re.search(r"\d+", run_id or "")
    return int(match.group()) if match else 0


def composite(entry, state):
    importance = int(entry.get("imp", 3))
    current_run = state["run_count"]
    runs_since = max(0, current_run - run_number(entry.get("last")))
    run_mult = lookup(
        DECAY_RUNS.get(state["cadence_class"], DECAY_RUNS["interactive"]),
        runs_since,
    )
    last_date = state.get("run_dates", {}).get(entry.get("last"))
    if last_date:
        try:
            days = max(0, (dt.date.today() - dt.date.fromisoformat(last_date)).days)
            day_mult = lookup(DECAY_DAYS, days)
        except ValueError:
            day_mult = run_mult
    else:
        day_mult = run_mult
    return importance * min(day_mult, run_mult)


def cmd_maintain(paths):
    with engine_lock(paths):
        state = load_or_initialize(paths)
        sync_generated_view(paths, state)
        content = revision_path(paths, state["head_revision"]).read_text(encoding="utf-8")
        _, entries, errors = parse_hot(content)
        if errors:
            raise BimriError("maintenance stopped: " + "; ".join(errors))
        flagged = []
        for entry in entries:
            if entry["tier"] != 2:
                continue
            weight = composite(entry, state)
            if entry["status"] == "closed" or weight < state["flag_threshold"]:
                flagged.append((entry["id"], entry["key"], weight, entry["text"]))
        print("BIMRI maintenance is judgment-first in v5.")
        if flagged:
            print("JUDGMENT NEEDED:")
            for entry_id, key, weight, text in flagged:
                print(f"  - {entry_id} [{key}] w={weight:.2f}: {text}")
                print("    Ask the owner if uncertain; otherwise submit touch or close.")
        else:
            print("  hot memory is clean.")


def cmd_status(paths):
    with engine_lock(paths):
        state = load_or_initialize(paths)
        sync_generated_view(paths, state)
        content = revision_path(
            paths, state["head_revision"]
        ).read_text(encoding="utf-8")
        _, _, errors, counts = validate_hot_content(
            content, state, allow_legacy_overflow=True
        )
        outcomes = {}
        for log in paths.logs.glob("R*.md"):
            if log.is_symlink():
                raise BimriError(
                    f"run log cannot be a symbolic link: {log.name}"
                )
            match = re.search(
                r"^\[OUTCOME:(success|partial|overflow|fail)\]",
                log.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            if match:
                outcomes[match.group(1)] = outcomes.get(match.group(1), 0) + 1
        conflict_total = len(open_conflicts(paths, state))
        proposal_total = len(list(paths.proposals.glob("*.json")))
        revision_total = len(list(paths.revisions.glob("V*.md")))
    print(
        f"BIMRI v{VERSION} | revision V{state['head_revision']:06d} | "
        f"runs {state['run_count']} | active {len(state['active_runs'])}"
    )
    if state["active_runs"]:
        print(
            "Active runs: "
            + ", ".join(
                f"{run_id} ({meta['actor']})"
                for run_id, meta in sorted(state["active_runs"].items())
            )
        )
    print(
        f"Hot: ~{len(content)//4} tokens | "
        f"T1 {counts[1]}/{state['tier1_max']} "
        f"T2 {counts[2]}/{state['tier2_max']} "
        f"T3 {counts[3]}/{state['tier3_max']}"
    )
    print(
        f"Open conflicts: {conflict_total} | "
        f"proposals: {proposal_total} | "
        f"revisions: {revision_total}"
    )
    if outcomes:
        print("Outcomes: " + ", ".join(
            f"{key}={value}" for key, value in sorted(outcomes.items())
        ))
    if errors:
        print("Validation errors: " + str(len(errors)))


def doctor_errors(paths, state):
    errors = []
    warnings = []
    try:
        sync_generated_view(paths, state)
    except (BimriError, OSError, UnicodeError) as exc:
        errors.append(str(exc))
    rev = revision_path(paths, state["head_revision"])
    if rev.exists():
        try:
            content = rev.read_text(encoding="utf-8")
            _, entries, hot_errors, counts = validate_hot_content(content, state)
            for hot_error in hot_errors:
                if (
                    "exceeds cap:" in hot_error
                    or "exceeds byte cap:" in hot_error
                    or "inherited legacy text exceeds active entry cap:" in hot_error
                    or "inherited v4 pattern text exceeds active entry cap:" in hot_error
                ):
                    warnings.append(
                        "bounded-memory repair needed: " + hot_error
                    )
                else:
                    errors.append(hot_error)
            for entry in entries:
                pointer_error = pointer_validation_error(paths, entry)
                if pointer_error:
                    errors.append(pointer_error)
        except (UnicodeDecodeError, OSError) as exc:
            errors.append(f"head revision is unreadable: {exc}")
    else:
        errors.append("head revision is missing.")
    for rid in state["active_runs"]:
        if not run_log_path(paths, rid).exists():
            errors.append(f"active run {rid} has no log.")
    for path in sorted(paths.logs.glob("R*.md")):
        try:
            if path.is_symlink():
                raise BimriError("run log cannot be a symbolic link.")
            path.read_text(encoding="utf-8")
        except (BimriError, UnicodeDecodeError, OSError) as exc:
            errors.append(f"{path.name}: {exc}")
    for path in sorted(paths.archive.glob("*.md")):
        try:
            if path.is_symlink():
                raise BimriError("archive file cannot be a symbolic link.")
            path.read_text(encoding="utf-8")
        except (BimriError, UnicodeDecodeError, OSError) as exc:
            errors.append(f"{path.name}: {exc}")
    for skey, rid in state["session_runs"].items():
        if rid not in state["active_runs"]:
            warnings.append(
                f"stale session mapping {skey} points to inactive run {rid}."
            )
    referenced_revisions = referenced_revision_numbers(paths, state)
    revision_numbers = set()
    for path in sorted(paths.revisions.glob("V*.md")):
        if path.is_symlink():
            errors.append(f"revision cannot be a symbolic link: {path.name}")
            continue
        if not re.fullmatch(r"V\d{6}\.md", path.name):
            warnings.append(f"unrecognized revision filename: {path.name}")
            continue
        revision_number = int(path.stem[1:])
        revision_numbers.add(revision_number)
        if revision_number not in referenced_revisions:
            warnings.append(
                f"unreferenced immutable revision {path.name}; it may be an "
                "interrupted snapshot or an unresolved applying commit."
            )
        try:
            revision_content = path.read_text(encoding="utf-8")
            _, _, revision_errors, _ = validate_hot_content(
                revision_content, state, allow_legacy_overflow=True
            )
            errors.extend(
                f"{path.name}: {error}" for error in revision_errors
            )
        except (UnicodeDecodeError, OSError) as exc:
            errors.append(f"{path.name} is unreadable: {exc}")
    for revision_number in referenced_revisions:
        if revision_number not in revision_numbers:
            errors.append(
                f"referenced revision V{revision_number:06d}.md is missing."
            )
    for path in sorted(paths.proposals.glob("*.json")):
        try:
            if path.is_symlink():
                raise BimriError("proposal file cannot be a symbolic link.")
            proposal = validate_proposal(
                read_json_strict(path, path.name), state
            )
            if proposal["proposal_id"] != path.stem:
                raise BimriError("proposal filename does not match its ID.")
            if not revision_path(
                paths, proposal["base_revision"]
            ).exists():
                raise BimriError("proposal base revision is missing.")
        except BimriError as exc:
            errors.append(f"{path.name}: {exc}")
    for path in sorted(paths.decisions.glob("*.json")):
        try:
            if path.is_symlink():
                raise BimriError("decision file cannot be a symbolic link.")
            validate_fixed_id(path.stem, PROPOSAL_RE, "decision proposal ID")
            decision = validate_decision(
                read_json_strict(path, path.name), path.stem
            )
            validate_decision_effect(paths, state, decision)
            if not proposal_path(paths, path.stem).exists():
                raise BimriError("decision proposal file is missing.")
            if decision["outcome"] == "applying":
                recorded = parse_timestamp(
                    decision["recorded_at"], "decision timestamp"
                )
                age = max(
                    0,
                    int(
                        (
                            dt.datetime.now(dt.timezone.utc) - recorded
                        ).total_seconds()
                    ),
                )
                warnings.append(
                    f"unfinished applying decision {path.stem} "
                    f"({age}s old); retry that run's sync to recover it."
                )
            if decision["outcome"] == "contested":
                cpath = conflict_path(paths, decision["conflict_id"])
                if not cpath.exists():
                    raise BimriError("contested decision conflict is missing.")
            if decision["outcome"] == "accepted":
                if not revision_path(paths, decision["revision"]).exists():
                    raise BimriError("accepted decision revision is missing.")
        except BimriError as exc:
            errors.append(f"{path.name}: {exc}")
    for path in sorted(paths.conflicts.glob("*.json")):
        try:
            if path.is_symlink():
                raise BimriError("conflict file cannot be a symbolic link.")
            conflict = validate_conflict_record(
                paths,
                read_json_strict(path, path.name),
                expected_conflict_id=path.stem,
            )
            rpath = resolution_file_path(paths, path.stem)
            resolution = None
            if rpath.exists():
                resolution = validate_resolution_record(
                    read_json_strict(rpath, rpath.name),
                    conflict=conflict,
                    expected_conflict_id=path.stem,
                )
                if resolution["status"] == "resolved":
                    validate_resolution_effect(
                        paths, state, conflict, resolution
                    )
            validate_conflict_candidate_decisions(
                paths, conflict, resolution
            )
        except BimriError as exc:
            errors.append(f"{path.name}: {exc}")
    for path in sorted(paths.resolutions.glob("*.json")):
        try:
            if path.is_symlink():
                raise BimriError("resolution file cannot be a symbolic link.")
            cpath = conflict_path(paths, path.stem)
            if not cpath.exists():
                raise BimriError("resolution conflict file is missing.")
            conflict = validate_conflict_record(
                paths,
                read_json_strict(cpath, cpath.name),
                expected_conflict_id=path.stem,
            )
            resolution = validate_resolution_record(
                read_json_strict(path, path.name),
                conflict=conflict,
                expected_conflict_id=path.stem,
            )
            if resolution["status"] == "resolved":
                validate_resolution_effect(
                    paths, state, conflict, resolution
                )
        except BimriError as exc:
            errors.append(f"{path.name}: {exc}")
    if paths.index.exists():
        for number, line in enumerate(paths.index.read_text(encoding="utf-8").splitlines(), 1):
            if len(line.split("\t")) != 8:
                errors.append(f"index line {number} does not have 8 columns.")
    else:
        warnings.append("index is missing; run index to rebuild it.")
    temp_files = set()
    for directory in (paths.root, *paths.dirs):
        for pattern in (".bimri-tmp-*", ".bimri-new-*"):
            temp_files.update(directory.glob(pattern))
    for path in sorted(temp_files):
        warnings.append(
            f"abandoned temporary file {path.relative_to(paths.root)}; "
            "review it before cleanup."
        )
    return errors, warnings


def cmd_doctor(paths):
    with engine_lock(paths):
        state = load_or_initialize(paths)
        errors, warnings = doctor_errors(paths, state)
        if not errors:
            build_index(paths, state)
    if errors:
        print("BIMRI doctor: FAILED")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("BIMRI doctor: PASSED")
    for warning in warnings:
        print(f"  - {warning}")
    return 0


AGENT_BLOCK_START = "<!-- BIMRI:START -->"
AGENT_BLOCK_END = "<!-- BIMRI:END -->"
PYTHON_COMMAND_PLACEHOLDER = "__BIMRI_VERIFIED_PYTHON__"
PYTHON_SENTINEL_SWITCH = "--_bimri-python-sentinel"
INSTALL_CORE = (
    "bimri-engine.py",
    "BIMRI-PROTOCOL.md",
    "BIMRI-MEMORY.template.md",
    "BIMRI-STATE.template.json",
    "BIMRI-AGENT-BLOCK.md",
    "INSTALL.md",
    "MIGRATION.md",
    "hooks-example.json",
    "legacy/README.md",
    "legacy/v1/BIMRI-global-instructions.md",
    "legacy/v3/BIMRI-global-instructions.md",
    "legacy/v3/BIMRI-global-instructions-v3.md",
    "legacy/v3/README.md",
)
INSTALL_FILE_MAP = tuple((name, name) for name in INSTALL_CORE) + (
    ("LICENSE", "BIMRI-LICENSE"),
)
INSTALL_ADAPTERS = ("AGENTS.md", "CLAUDE.md")
INSTALL_DIRECTORIES = ("legacy", "legacy/v1", "legacy/v3")
INSTALL_LOCAL_ARTIFACTS = (
    ".bimri/runtime.local.json",
    ".bimri/hooks.claude.local.json",
)


def install_source_file(source_paths, source_name):
    """Resolve canonical sources and their self-contained installed names."""
    if source_name == "LICENSE":
        packaged = source_paths.root / "BIMRI-LICENSE"
        if packaged.exists() or packaged.is_symlink():
            return packaged
    return source_paths.root / source_name


def _python_sentinel_payload(token):
    return {
        "executable": str(Path(sys.executable).resolve()),
        "sentinel": token,
        "version": [sys.version_info.major, sys.version_info.minor],
    }


def verify_python_relaunch(executable, engine):
    """Prove that one exact interpreter can execute one exact BIMRI engine."""
    executable = Path(executable)
    engine = Path(engine)
    token = uuid.uuid4().hex
    try:
        check = subprocess.run(
            [
                str(executable),
                str(engine),
                PYTHON_SENTINEL_SWITCH,
                token,
            ],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        raise BimriError(
            "the current Python interpreter did not complete BIMRI's "
            "five-second verification check."
        ) from exc
    except OSError as exc:
        raise BimriError(
            f"the current Python interpreter could not relaunch BIMRI: {exc}"
        ) from exc

    if check.returncode != 0:
        detail = check.stderr.strip() or check.stdout.strip() or "no output"
        raise BimriError(
            "the current Python interpreter failed BIMRI's verification "
            f"check (exit {check.returncode}): {detail}"
        )
    try:
        payload = json.loads(check.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BimriError(
            "the current Python interpreter returned no valid BIMRI "
            "verification sentinel."
        ) from exc
    if not isinstance(payload, dict) or payload.get("sentinel") != token:
        raise BimriError(
            "the current Python interpreter returned the wrong BIMRI "
            "verification sentinel."
        )
    version = payload.get("version")
    if (
        not isinstance(version, list)
        or len(version) != 2
        or any(type(value) is not int for value in version)
        or tuple(version) < (3, 8)
    ):
        raise BimriError(
            "the relaunched Python interpreter is not Python 3.8 or newer."
        )
    reported = payload.get("executable")
    if not isinstance(reported, str):
        raise BimriError(
            "the relaunched Python interpreter did not report its executable."
        )
    try:
        reported_executable = Path(reported).resolve(strict=True)
    except OSError as exc:
        raise BimriError(
            "the relaunched Python interpreter reported an invalid "
            f"executable: {exc}"
        ) from exc
    if os.path.normcase(str(reported_executable)) != os.path.normcase(
        str(executable)
    ):
        raise BimriError(
            "the relaunched Python interpreter did not match sys.executable."
        )
    return str(executable)


def verified_python_executable():
    """Return the current interpreter only after a bounded self-relaunch."""
    if sys.version_info < (3, 8):
        raise BimriError(
            "BIMRI requires Python 3.8 or newer; the current interpreter is "
            f"{sys.version_info.major}.{sys.version_info.minor}."
        )
    raw_executable = Path(sys.executable)
    if not raw_executable.is_absolute():
        raise BimriError(
            "the current Python interpreter does not report an absolute "
            "sys.executable path."
        )
    try:
        executable = raw_executable.resolve(strict=True)
    except OSError as exc:
        raise BimriError(
            f"the current Python interpreter cannot be resolved: {exc}"
        ) from exc
    if not executable.is_file():
        raise BimriError(
            "the current Python interpreter is not an existing regular file: "
            f"{executable}"
        )
    return verify_python_relaunch(executable, Path(__file__).resolve())


def render_hooks_snippet(template, destination, python_executable):
    try:
        payload = json.loads(template.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BimriError(f"hooks-example.json is not valid JSON: {exc}") from exc
    try:
        hooks = payload["hooks"]
        expected = {
            "SessionStart": "hook-start",
            "SessionEnd": "hook-close",
        }
        rendered = 0
        for event, subcommand in expected.items():
            groups = hooks[event]
            if not isinstance(groups, list) or len(groups) != 1:
                raise KeyError(event)
            commands = groups[0]["hooks"]
            if not isinstance(commands, list) or len(commands) != 1:
                raise KeyError(event)
            command = commands[0]
            if command.get("type") != "command":
                raise KeyError(event)
            if command.get("command") != PYTHON_COMMAND_PLACEHOLDER:
                raise KeyError(event)
            if command.get("args") != [
                "${CLAUDE_PROJECT_DIR}/bimri-engine.py",
                subcommand,
            ]:
                raise KeyError(event)
            if command.get("timeout") != 15:
                raise KeyError(event)
            command["command"] = python_executable
            rendered += 1
    except (KeyError, TypeError, AttributeError) as exc:
        raise BimriError(
            "hooks-example.json does not match the BIMRI exec-form template."
        ) from exc
    if rendered != 2:
        raise BimriError(
            "hooks-example.json did not contain both BIMRI hook commands."
        )
    atomic_write_text(
        destination,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )


def write_local_runtime_binding(
    runtime_path, python_executable, engine_path
):
    engine_path = str(Path(engine_path).resolve())
    binding = {
        "version": VERSION,
        "host_bound": True,
        "python_executable": python_executable,
        "engine_path": engine_path,
        "argv_prefix": [python_executable, engine_path],
    }
    atomic_write_json(runtime_path, binding)


def merge_marked_block(path, block):
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    marked = f"{AGENT_BLOCK_START}\n{block.strip()}\n{AGENT_BLOCK_END}"
    pattern = re.compile(
        re.escape(AGENT_BLOCK_START) + r".*?" + re.escape(AGENT_BLOCK_END),
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(lambda _match: marked, existing)
    else:
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + marked + "\n"
    atomic_write_text(path, updated.rstrip() + "\n")


def collect_bimri_files(paths):
    files = set()
    if not paths.bdir.exists():
        return files
    for path in paths.bdir.rglob("*"):
        if not (path.is_file() or path.is_symlink()):
            continue
        relative = path.relative_to(paths.root).as_posix()
        if relative.startswith(".bimri/install-backups/"):
            continue
        files.add(relative)
    return files


def ensure_v4_install_is_quiescent(paths, raw_state):
    version = str(raw_state.get("bimri_version", ""))
    if not (
        version.startswith("4")
        or (not version and "current_run_id" in raw_state)
    ):
        return
    run_id = raw_state.get("current_run_id")
    if run_id is None:
        candidates = sorted(
            (
                path for path in paths.logs.glob("R*.md")
                if re.fullmatch(r"R\d+", path.stem)
            ),
            key=lambda path: int(path.stem[1:]),
        )
        for log in reversed(candidates):
            if log.is_symlink():
                raise BimriError("a v4 run log cannot be a symbolic link.")
            text = log.read_text(encoding="utf-8")
            if not any(
                line.strip().startswith(f"[CLOSED:{log.stem}")
                for line in text.splitlines()
            ):
                raise BimriError(
                    f"v4 run {log.stem} appears active. Stop or close every "
                    "v4 agent, then retry migration."
                )
        return
    if not isinstance(run_id, str) or not re.fullmatch(r"R\d+", run_id):
        raise BimriError(
            "v4 state has an invalid current run ID; repair it before upgrading."
        )
    if run_id == "R000":
        return
    log = paths.logs / f"{run_id}.md"
    appears_active = False
    if log.exists():
        if log.is_symlink():
            raise BimriError("the v4 current run log cannot be a symbolic link.")
        text = log.read_text(encoding="utf-8")
        appears_active = not any(
            line.strip().startswith(f"[CLOSED:{run_id}")
            for line in text.splitlines()
        )
    else:
        # A non-sentinel current run without its journal cannot prove that the
        # v4 writer closed.  Migration replaces the runtime, so ambiguity must
        # fail closed rather than relying on optional legacy timestamps.
        appears_active = True
    if appears_active:
        raise BimriError(
            f"v4 run {run_id} appears active. Stop or close every v4 agent, "
            "then ask the agent to retry installation. A v4 writer does not "
            "participate in the v5 lock."
        )


def validate_install_target(source_paths, target, target_paths, backup_root):
    if target_paths.bdir.is_symlink():
        raise BimriError("target .bimri cannot be a symbolic link.")
    if backup_root.is_symlink():
        raise BimriError(
            "target .bimri/install-backups cannot be a symbolic link."
        )
    for relative in INSTALL_DIRECTORIES:
        source_directory = source_paths.root / relative
        target_directory = target / relative
        if (
            not source_directory.is_dir()
            or source_directory.is_symlink()
            or source_directory.resolve() != source_directory
        ):
            raise BimriError(
                f"installer source directory is missing or unsafe: {relative}"
            )
        if (
            target_directory.is_symlink()
            or target_directory.resolve() != target_directory
        ):
            raise BimriError(
                f"refusing to install through redirected directory: {relative}"
            )
        if target_directory.exists() and not target_directory.is_dir():
            raise BimriError(
                f"installer target directory is not a directory: {relative}"
            )
    for source_name, destination_name in INSTALL_FILE_MAP:
        source = install_source_file(source_paths, source_name)
        destination = target / destination_name
        if (
            not source.is_file()
            or source.is_symlink()
            or source.resolve() != source
        ):
            raise BimriError(
                f"installer source is missing or unsafe: {source_name}"
            )
        if destination.is_symlink() or destination.resolve() != destination:
            raise BimriError(
                "refusing to replace redirected path in installer target: "
                f"{destination_name}"
            )
        if destination.exists() and not destination.is_file():
            raise BimriError(
                f"installer target is not a regular file: {destination_name}"
            )
    for name in INSTALL_ADAPTERS:
        destination = target / name
        if destination.is_symlink():
            raise BimriError(
                f"refusing to merge through symbolic link in installer target: {name}"
            )
        if destination.exists() and not destination.is_file():
            raise BimriError(
                f"installer instruction target is not a regular file: {name}"
            )
    for relative in INSTALL_LOCAL_ARTIFACTS:
        destination = target / relative
        if destination.is_symlink():
            raise BimriError(
                f"refusing to replace redirected local artifact: {relative}"
            )
        if destination.exists() and not destination.is_file():
            raise BimriError(
                f"installer local artifact is not a regular file: {relative}"
            )


def snapshot_install_target(target_paths, backup_dir):
    preexisting_files = collect_bimri_files(target_paths)
    actual_legacy = _actual_legacy_root_paths(
        target_paths, LEGACY_ACTIVE_NAMES + LEGACY_BACKUP_NAMES
    )
    legacy_names = [path.name for path in actual_legacy]
    canonical_hot_is_snapshotted = "bimri.md" in legacy_names
    if not canonical_hot_is_snapshotted and target_paths.hot.exists():
        for legacy_path in actual_legacy:
            try:
                if os.path.samefile(str(legacy_path), str(target_paths.hot)):
                    canonical_hot_is_snapshotted = True
                    break
            except OSError:
                continue
    if not canonical_hot_is_snapshotted:
        legacy_names.append("bimri.md")
    managed = [
        *(destination for _source, destination in INSTALL_FILE_MAP),
        *INSTALL_ADAPTERS,
        *legacy_names,
        *INSTALL_LOCAL_ARTIFACTS,
        ".bimri/state.json",
        ".bimri/index.tsv",
    ]
    managed.extend(
        path.relative_to(target_paths.root).as_posix()
        for path in sorted(target_paths.conflicts.glob("*.json"))
    )
    records = {}
    for relative in dict.fromkeys(managed):
        source = target_paths.root / relative
        if source.is_symlink():
            raise BimriError(
                f"installer snapshot target cannot be a symbolic link: {relative}"
            )
        existed = source.exists()
        backup_relative = None
        if existed:
            if not source.is_file():
                raise BimriError(
                    f"installer snapshot target is not a regular file: {relative}"
                )
            if "/" not in relative:
                backup_relative = relative
            else:
                backup_relative = f"runtime/{relative}"
            backup = backup_dir / backup_relative
            ensure_directory_durable(backup.parent)
            atomic_copy_file(source, backup)
        records[relative] = {
            "existed": existed,
            "backup": backup_relative,
        }
    directories = {
        relative: {
            "existed": (target_paths.root / relative).is_dir(),
        }
        for relative in INSTALL_DIRECTORIES
    }
    manifest = {
        "bimri_version": VERSION,
        "created_at": now_iso(),
        "target": str(target_paths.root),
        "status": "prepared",
        "records": records,
        "directories": directories,
        "preexisting_bimri_files": sorted(preexisting_files),
    }
    atomic_write_json(backup_dir / "install-manifest.json", manifest)
    return manifest


def restore_root_entry_case(target_paths, relative):
    """Restore an exact legacy filename on case-insensitive filesystems."""
    if "/" in relative or _legacy_root_role(relative) is None:
        return
    destination = target_paths.root / relative
    if not destination.exists():
        return
    candidates = [
        entry for entry in target_paths.root.iterdir()
        if entry.name.casefold() == relative.casefold()
    ]
    if len(candidates) != 1 or candidates[0].name == relative:
        return
    try:
        aliases_destination = os.path.samefile(
            str(candidates[0]), str(destination)
        )
    except OSError:
        aliases_destination = False
    if not aliases_destination:
        return
    temporary = target_paths.root / f".bimri-case-{uuid.uuid4().hex}"
    os.replace(str(candidates[0]), str(temporary))
    os.replace(str(temporary), str(destination))
    fsync_directory(target_paths.root)


def rollback_install(target_paths, backup_dir, manifest):
    errors = []
    preexisting = set(manifest["preexisting_bimri_files"])
    for relative in sorted(collect_bimri_files(target_paths) - preexisting):
        if relative == ".bimri/engine.lock":
            continue
        path = target_paths.root / relative
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
        except OSError as exc:
            errors.append(f"could not remove {relative}: {exc}")
    for relative, record in manifest["records"].items():
        destination = target_paths.root / relative
        try:
            if record["existed"]:
                restore_root_entry_case(target_paths, relative)
                atomic_copy_file(
                    backup_dir / record["backup"], destination
                )
            elif destination.exists() or destination.is_symlink():
                if destination.is_file() or destination.is_symlink():
                    destination.unlink()
                else:
                    errors.append(
                        f"could not remove non-file rollback target {relative}"
                    )
        except (BimriError, OSError) as exc:
            errors.append(f"could not restore {relative}: {exc}")
    for relative in reversed(INSTALL_DIRECTORIES):
        record = manifest.get("directories", {}).get(relative, {})
        if record.get("existed"):
            continue
        directory = target_paths.root / relative
        try:
            if directory.is_symlink():
                raise BimriError("rollback directory became a symbolic link")
            if directory.exists():
                directory.rmdir()
                fsync_directory(directory.parent)
        except (BimriError, OSError) as exc:
            errors.append(f"could not remove created directory {relative}: {exc}")
    manifest["status"] = "rollback-incomplete" if errors else "rolled-back"
    manifest["rolled_back_at"] = now_iso()
    if errors:
        manifest["rollback_errors"] = errors
    try:
        atomic_write_json(backup_dir / "install-manifest.json", manifest)
    except (BimriError, OSError) as exc:
        errors.append(f"could not update install manifest: {exc}")
    return errors


def migration_receipt_lines(receipt):
    """Render one invocation's migration facts without consulting old markers."""
    if not isinstance(receipt, dict):
        raise BimriError("memory operation completed without a migration receipt.")
    action = receipt.get("action")
    limits = receipt.get("limits", {})
    profile = (
        f"T1/T2/T3 {limits.get('tier1_max')}/{limits.get('tier2_max')}/"
        f"{limits.get('tier3_max')}, entry {limits.get('entry_max_chars')} chars, "
        f"{limits.get('hot_max_bytes'):,} bytes"
        if all(
            isinstance(limits.get(key), int)
            for key in (
                "tier1_max", "tier2_max", "tier3_max",
                "entry_max_chars", "hot_max_bytes",
            )
        )
        else None
    )
    if action == "initialized":
        line = f"Memory: initialized at v{VERSION}"
        if profile:
            line += f" ({profile})"
        return [line + ".", "Validation: PASSED."]
    if action == "verified":
        lines = [f"Memory: existing v{VERSION} verified."]
        if receipt.get("metadata_revision"):
            lines.append(
                "Memory metadata normalized in immutable revision "
                + receipt["metadata_revision"]
                + "."
            )
        else:
            lines[0] = (
                f"Memory: existing v{VERSION} verified; no migration performed."
            )
        lines.append("Validation: PASSED.")
        return lines
    if action == "upgraded":
        source = receipt.get("source_version")
        old_limits = receipt.get("old_limits", {})
        old_profile = (
            f"T1/T2/T3 {old_limits.get('tier1_max')}/"
            f"{old_limits.get('tier2_max')}/{old_limits.get('tier3_max')}, "
            f"entry {old_limits.get('entry_max_chars')} chars, "
            f"{old_limits.get('hot_max_bytes'):,} bytes"
            if all(
                isinstance(old_limits.get(key), int)
                for key in (
                    "tier1_max", "tier2_max", "tier3_max",
                    "entry_max_chars", "hot_max_bytes",
                )
            )
            else None
        )
        disposition = (
            "stock limits expanded"
            if receipt.get("expanded_default_limits")
            else "custom limits preserved"
        )
        lines = [
            f"Memory: upgraded v{source} to v{VERSION}; {disposition}"
            + (f" ({profile})." if profile else ".")
        ]
        if old_profile:
            lines.append("Previous limits: " + old_profile + ".")
        backups = receipt.get("backups") or []
        if backups:
            lines.append("State backup: " + ", ".join(backups) + ".")
        if receipt.get("metadata_revision"):
            lines.append(
                "Memory metadata normalized in immutable revision "
                + receipt["metadata_revision"]
                + "."
            )
        lines.append("Validation: PASSED.")
        return lines
    if action == "migrated":
        source = receipt.get("source_version")
        source_file = receipt.get("source_file") or "detected legacy source"
        imported = receipt.get("imported") or {}
        if "claims_imported" in imported:
            tier1 = imported.get("tier1_imported", 0)
            tier2 = imported.get("tier2_imported", 0)
            tier3 = 0
            total = imported.get("claims_imported", tier1 + tier2)
            patterns = imported.get("patterns_converted_to_watches", 0)
            overlength = imported.get("inherited_overlength_claims", 0)
        else:
            tier1 = imported.get("tier1", 0)
            tier2 = imported.get("tier2", 0)
            tier3 = imported.get("tier3", 0)
            total = imported.get("total", tier1 + tier2 + tier3)
            patterns = tier3
            overlength = imported.get("inherited_overlength_claims", 0)
        lines = [
            f"Memory: migrated BIMRI v{source} from {source_file} to v{VERSION}.",
            "Imported: "
            f"Tier 1 {tier1}; Tier 2 {tier2}; Tier 3 {tier3}; total {total}; "
            f"converted patterns {patterns}; inherited overlength {overlength}.",
        ]
        backups = receipt.get("backups") or []
        if backups:
            lines.append("Byte-exact backups: " + ", ".join(backups) + ".")
        if receipt.get("metadata_revision"):
            lines.append(
                "Memory metadata normalized in immutable revision "
                + receipt["metadata_revision"]
                + "."
            )
        marker = (
            ".bimri/migrations/legacy-to-v5.json"
            if str(source) in {"1", "2", "3"}
            else ".bimri/migrations/v4-to-v5.json"
        )
        lines.append(f"Migration record: {marker}.")
        if receipt.get("expanded_default_limits") is not None:
            lines.append(
                "Limits: "
                + (
                    "stock profile expanded."
                    if receipt["expanded_default_limits"]
                    else "custom profile preserved."
                )
            )
        lines.append("Validation: PASSED.")
        return lines
    raise BimriError(f"memory operation returned an unknown receipt action: {action}")


def cmd_install(source_paths, target):
    python_executable = verified_python_executable()
    target = Path(target).resolve()
    if target.parent == target:
        raise BimriError("refusing to install BIMRI into a filesystem root.")
    target.mkdir(parents=True, exist_ok=True)
    target_paths = Paths(target)
    preflight_legacy_source(target_paths)
    if target_paths.bdir.is_symlink():
        raise BimriError("target .bimri cannot be a symbolic link.")
    target_paths.bdir.mkdir(parents=True, exist_ok=True)
    backup_root = target_paths.bdir / "install-backups"
    if backup_root.is_symlink():
        raise BimriError("target .bimri/install-backups cannot be a symbolic link.")
    state = None
    install_warnings = []
    with engine_lock(target_paths):
        validate_install_target(
            source_paths, target, target_paths, backup_root
        )
        if target_paths.state.exists():
            raw_state = read_json_strict(
                target_paths.state, "target state.json"
            )
            ensure_v4_install_is_quiescent(target_paths, raw_state)
            raw_version = str(raw_state.get("bimri_version", ""))
            if raw_version.startswith("4") or (
                not raw_version and "current_run_id" in raw_state
            ):
                reject_unclaimed_legacy_roots(target_paths)
        backup_root.mkdir(parents=True, exist_ok=True)
        fsync_directory(backup_root.parent)
        if target_paths.bdir not in backup_root.resolve().parents:
            raise BimriError(
                "installer backup directory escaped the target .bimri folder."
            )
        backup_dir = backup_root / (
            dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        backup_dir.mkdir(parents=True, exist_ok=False)
        fsync_directory(backup_dir.parent)
        manifest = snapshot_install_target(target_paths, backup_dir)
        try:
            for source_name, destination_name in INSTALL_FILE_MAP:
                source = install_source_file(source_paths, source_name)
                destination = target / destination_name
                if source.resolve() != destination.resolve():
                    atomic_copy_file(source, destination)
            installed_engine = (target / "bimri-engine.py").resolve()
            verify_python_relaunch(
                python_executable,
                installed_engine,
            )
            block = (
                source_paths.root / "BIMRI-AGENT-BLOCK.md"
            ).read_text(encoding="utf-8")
            merge_marked_block(target / "AGENTS.md", block)
            claude_block = (
                "@AGENTS.md\n\n"
                "Use BIMRI-PROTOCOL.md for the full memory protocol. "
                "BIMRI shared memory is engine-managed. Read the host-only "
                "argument prefix from `.bimri/runtime.local.json`. Merge the "
                "rendered `.bimri/hooks.claude.local.json` snippet into "
                "`.claude/settings.local.json` manually."
            )
            merge_marked_block(target / "CLAUDE.md", claude_block)
            state = load_or_initialize(target_paths)
            sync_generated_view(target_paths, state)
            build_index(target_paths, state)
            errors, install_warnings = doctor_errors(target_paths, state)
            if errors:
                raise BimriError(
                    "installation self-check failed: " + "; ".join(errors)
                )
            # Host-only adapters are deliberately written after memory has
            # been initialized or migrated. Legacy migration treats unknown
            # .bimri files as possible orphaned authority, so pre-creating
            # these local records would make a clean install look ambiguous.
            write_local_runtime_binding(
                target_paths.bdir / "runtime.local.json",
                python_executable,
                installed_engine,
            )
            render_hooks_snippet(
                source_paths.root / "hooks-example.json",
                target_paths.bdir / "hooks.claude.local.json",
                python_executable,
            )
            migration_receipt_lines(target_paths.migration_receipt)
            manifest["status"] = "installed"
            manifest["completed_at"] = now_iso()
            atomic_write_json(
                backup_dir / "install-manifest.json", manifest
            )
        except Exception as exc:
            rollback_errors = rollback_install(
                target_paths, backup_dir, manifest
            )
            backup_label = backup_dir.relative_to(target).as_posix()
            if rollback_errors:
                raise BimriError(
                    "installation failed and rollback was incomplete. "
                    f"Backups: {backup_label}. Cause: {exc}. Rollback: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise BimriError(
                "installation self-check failed; target files were rolled "
                f"back. Backups: {backup_label}. Cause: {exc}"
            ) from exc
    print(f"BIMRI {VERSION} installed.")
    print(f"Verified Python: {python_executable}")
    for line in migration_receipt_lines(target_paths.migration_receipt):
        print(line)
    print("Universal AGENTS.md adapter enabled.")
    print(
        "Host-only runtime binding written to .bimri/runtime.local.json."
    )
    print(
        "Claude Code hook snippet rendered to "
        ".bimri/hooks.claude.local.json; .claude settings unchanged."
    )
    print("Doctor passed.")
    for warning in install_warnings:
        print(f"Repair warning: {warning}")


def hook_payload():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        data = {}
    return data if isinstance(data, dict) else {}


def build_parser():
    parser = argparse.ArgumentParser(description="BIMRI portable memory engine")
    parser.add_argument(
        "--root", help="BIMRI project root; defaults to the engine file's folder."
    )
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start")
    start.add_argument("--actor", default="agent")
    start.add_argument("--session")

    journal = sub.add_parser("journal")
    journal.add_argument("--run", required=True)
    journal.add_argument("--text", required=True)
    journal.add_argument("--importance", type=int, default=3)

    propose = sub.add_parser("propose")
    propose.add_argument("--run", required=True)
    propose.add_argument("--operation", choices=sorted(OPERATIONS), default="set")
    propose.add_argument("--tier", type=int, choices=(1, 2, 3))
    propose.add_argument("--key", required=True)
    propose.add_argument("--target")
    propose.add_argument("--kind", choices=sorted(TIER1_KINDS), default="fact")
    propose.add_argument("--importance", type=int, choices=range(1, 6), default=3)
    propose.add_argument("--status", choices=sorted(TIER2_STATUSES), default="active")
    propose.add_argument("--trust", choices=sorted(TRUSTS), default="working")
    propose.add_argument("--source", choices=sorted(SOURCES), default="agent")
    propose.add_argument("--tags", default="")
    propose.add_argument("--text")
    propose.add_argument("--rationale")
    propose.add_argument("--needs-human", action="store_true")
    propose.add_argument("--question")
    propose.add_argument("--confidence", choices=sorted(PATTERN_CONFIDENCE), default="emerging")
    propose.add_argument("--observations", type=int, default=1)
    propose.add_argument("--evidence")
    propose.add_argument("--falsifier")

    sync = sub.add_parser("sync")
    sync.add_argument("--run", required=True)

    close = sub.add_parser("close")
    close.add_argument("--run")
    close.add_argument("--actor")
    close.add_argument("--session")
    close.add_argument("--outcome", choices=sorted(OUTCOMES), default="partial")
    close.add_argument("--summary")

    recover = sub.add_parser("recover-run")
    recover.add_argument("--run", required=True)
    recover.add_argument(
        "--outcome", choices=sorted(OUTCOMES), default="partial"
    )
    recover.add_argument("--summary", required=True)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("conflict_id")
    resolve.add_argument("--choose", required=True)

    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("validate")
    sub.add_parser("index")
    sub.add_parser("maintain")
    sub.add_parser("migrate")
    sub.add_parser("hook-start")
    sub.add_parser("hook-close")

    install = sub.add_parser("install")
    install.add_argument("--target", required=True)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if (
        len(argv) == 2
        and argv[0] == PYTHON_SENTINEL_SWITCH
        and re.fullmatch(r"[0-9a-f]{32}", argv[1])
    ):
        print(
            json.dumps(
                _python_sentinel_payload(argv[1]),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        source_root = Path(__file__).resolve().parent
        root = Path(args.root).resolve() if args.root else source_root
        paths = Paths(root)
        command = args.command or "status"
        if command == "start":
            cmd_start(paths, args.actor, args.session)
        elif command == "journal":
            cmd_journal(paths, args.run, args.text, args.importance)
        elif command == "propose":
            cmd_propose(paths, args)
        elif command == "sync":
            cmd_sync(paths, args.run)
        elif command == "close":
            cmd_close(
                paths, args.run, args.actor, args.session,
                args.outcome, args.summary,
            )
        elif command == "recover-run":
            cmd_recover_run(
                paths, args.run, args.outcome, args.summary
            )
        elif command == "resolve":
            cmd_resolve(paths, args.conflict_id, args.choose)
        elif command == "status":
            cmd_status(paths)
        elif command in {"doctor", "validate"}:
            return cmd_doctor(paths)
        elif command == "index":
            with engine_lock(paths):
                state = load_or_initialize(paths)
                sync_generated_view(paths, state)
                count = build_index(paths, state)
            print(f"BIMRI: index rebuilt with {count} rows.")
        elif command == "maintain":
            cmd_maintain(paths)
        elif command == "migrate":
            with engine_lock(paths):
                state = load_or_initialize(paths)
                sync_generated_view(paths, state)
                build_index(paths, state)
                migration_errors, migration_warnings = doctor_errors(
                    paths, state
                )
                if migration_errors:
                    raise BimriError(
                        "migration validation failed: "
                        + "; ".join(migration_errors)
                    )
            print(f"BIMRI: migration/initialization complete at v{VERSION}.")
            for line in migration_receipt_lines(paths.migration_receipt):
                print(line)
            for warning in migration_warnings:
                print(f"Repair warning: {warning}")
        elif command == "hook-start":
            payload = hook_payload()
            cmd_start(
                paths, "claude-code",
                str(payload.get("session_id") or payload.get("transcript_path") or uuid.uuid4()),
            )
        elif command == "hook-close":
            payload = hook_payload()
            session = str(payload.get("session_id") or payload.get("transcript_path") or "")
            cmd_close(
                paths, actor="claude-code", session=session or None,
                outcome="partial",
                summary=f"Claude Code SessionEnd: {payload.get('reason', 'ended')}",
            )
        elif command == "install":
            cmd_install(Paths(source_root), args.target)
        else:
            parser.print_help()
    except BimriError as exc:
        print(f"BIMRI ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"BIMRI ERROR: filesystem operation failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if os.environ.get("BIMRI_DEBUG") == "1":
            raise
        print(f"BIMRI ERROR: unexpected failure: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
