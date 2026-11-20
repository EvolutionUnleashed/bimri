"""BIMRI v5.2 build step 3: scoped authority writes everywhere, facts replace
the five scans, gate 6.

Spine: design/DECISIONS.md D2.2 (facts replace the scans), D2.3, D4.1
(common preconditions), D4.2 (dependency hash check, drift receipt, full
audit), D4.3 (cold base on the warm path), D4.4 (scoped publication), D4.5
(protocol sentence), section 9 step 3, section 10 R-J and R-V. Detail from
design/DESIGN.md section 04 (4.2 S1-S11, 4.3 dependency table, 4.5 P1-P6,
4.9 items 1 and 9, 4.11), section 09 step 3 and section 10 (10.1.4 forbid
seam, 10.2 F2/F2x3/F4, 10.3 timing, 10.4 gate 6, LD10.4).

Process model (section 10.0): every behavioural assertion drives the engine
as a subprocess. The gate-6 instrument is the section 10 10.1.4
`save_forbid_history_scan` seam; this file carries its own copy of it
(STEP3_DRIVER, written into the test workspace) so the gate does not depend
on a worker mode name: it imports the engine, makes every whole-history
collector raise AssertionError (and records the hit in <root>/.test-forbid-
hits, so an engine-side `except Exception` fallback cannot hide it), wraps
Path.glob/rglob/iterdir, os.listdir and os.scandir so any listing of a
directory under .bimri/ other than saves/open, saves/done, settlements and
audit-manifests raises (one exemption: Path.glob on proposals/ or decisions/
with the exact one-run pattern `R######-Q*.json`), counts the distinct
.bimri/ files opened for reading into <root>/.test-open-count, and then runs
engine.main. Fixtures F2 (300 accepted saves, ~50 cold subjects, 8 Tier 3
patterns, an open stale-base conflict, a held candidate) and F2x3 (F2 plus
600 saves) are grown by an in-process driver (GROW_DRIVER) because the
subprocess model costs minutes per hundred saves on Windows (LD10.2).

Each test proves one promise; the comment above it names the spine id.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
PROTOCOL = REPOSITORY / "BIMRI-PROTOCOL.md"
BENCHMARK = REPOSITORY / "tests" / "benchmark_v511.py"

RUN_RE = re.compile(r"BIMRI RUN HANDLE: (R\d{6})")
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")
SAVE_LINE_RE = re.compile(
    r"^BIMRI SAVE (accepted|unchanged|held|stale|conflict) (.+)$", re.M
)
PRIOR_EVIDENCE_INVALID_PREFIX = "prior audit manifest evidence is invalid"
SCOPED_MISS_PREFIX = "scoped dependency changed: "
RECOVERY_NEEDED = "AUTHORITY RECOVERY NEEDED"

F2_SAVES = 300
F2_HOT_FIT = 250
F2_PATTERNS = 8
F2X3_EXTRA_SAVES = 600
OPEN_COUNT_CEILING = 64
OPEN_COUNT_SLOPE = 8
# These F2 checks exercise the audit verdict after the copy completes.
FULL_AUDIT_CAPTURE_BUDGET_MS = "120000"

OWNER_KEY = "seed.owner"
OWNER_TEXT = "Owner confirmed rule for the step three fixture."
PATTERN_KEY = "seed.pattern"
CONFLICT_KEY = "conflict.subject"
GROW_TEXT = "Grow subject number %d for the step three history fixture."

D45_SENTENCE = (
    "A valid checkpoint certifies the protected records, run facts and "
    "current state the last full audit proved, as of its head and epoch, "
    "plus a chain of declared, verified engine writes since. An ordinary "
    "write re-validates only the records it depends on, records every "
    "protected path it writes, and publishes the successor checkpoint from "
    "the prior inventory plus those paths. Damage to a record no write "
    "depends on is caught when that record is next used or by the deep "
    "audit, which condemns on failure. A write never adopts a changed byte "
    "it did not verify."
)

COPY_IGNORE = shutil.ignore_patterns(
    "engine.lock", "runtime.local.json", "hooks.claude.local.json",
    ".test-*",
)


# ---- the gate-6 instrument (section 10 10.1.4), run as a subprocess -------

STEP3_DRIVER = r'''
import atexit, builtins, importlib.util, io, json, os, re, sys, time
from pathlib import Path

engine_path, mode, root = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
command = sys.argv[4:]
spec = importlib.util.spec_from_file_location("bimri_step3_target", engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


def norm(target):
    try:
        text = os.fspath(target)
    except TypeError:
        return None
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    return os.path.normcase(os.path.abspath(text))


BDIR = norm(root / ".bimri")
HITS = root / ".test-forbid-hits"
_real_open = io.open

# Section 10 10.1.4, section 04 4.3 and the PLAN step-3 list: every
# whole-history collector a warm write must not call. next_proposal_id and
# the old scanning next_pattern_id stay forbidden by name (the watermark
# readers are new names); exact_archive_effect is the all-months form.
# authority_storage_issues and scan_open_conflicts pass only in their
# bounded only= form, validate_cold_archive_bindings only with its
# checkpoint_months bypass.
FORBIDDEN = (
    "audit_witness_manifest", "audit_witness_run_facts",
    "archive_records", "accepted_key_writer_after",
    "accepted_writers_for_key", "accepted_cold_current_at_revision",
    "uncommitted_owner_resolution", "next_proposal_id", "next_pattern_id",
    "exact_archive_effect", "referenced_revision_numbers", "recall_records",
    "held_recall_records", "classify_review_records", "build_index",
    "authority_has_unsettled_records", "authority_has_recoverable_applying",
)
ALLOWED_DIRS = set(
    norm(root / ".bimri" / part)
    for part in ("saves/open", "saves/done", "settlements", "audit-manifests")
)
PREFIX_DIRS = set(norm(root / ".bimri" / part) for part in ("proposals", "decisions"))
ONE_RUN = re.compile(r"^R\d{6}-Q\*\.json$")
depth = {"listing": 0}
opened = set()


def hit(text):
    with _real_open(str(HITS), "a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    raise AssertionError("save_forbid_history_scan: " + text)


def under_bdir(path):
    return path is not None and path.startswith(BDIR + os.sep)


def check_listing(kind, target, pattern=None):
    if depth["listing"]:
        return
    path = norm(target)
    if not under_bdir(path) or path in ALLOWED_DIRS:
        return
    if kind == "glob" and path in PREFIX_DIRS and pattern and ONE_RUN.match(pattern):
        return
    relative = os.path.relpath(path, BDIR).replace(os.sep, "/")
    hit("{} .bimri/{} {}".format(kind, relative, pattern or "").strip())


def install_seam():
    def forbid(name):
        def reject(*_args, **_kwargs):
            hit("call " + name)
        return reject

    for name in FORBIDDEN:
        if hasattr(engine, name):
            setattr(engine, name, forbid(name))
    storage = engine.authority_storage_issues

    def scoped_storage(*args, **kwargs):
        # D3.4.3: only the scoped form (non-empty only=) is allowed.
        if not kwargs.get("only"):
            hit("call authority_storage_issues")
        return storage(*args, **kwargs)

    engine.authority_storage_issues = scoped_storage
    conflicts = engine.scan_open_conflicts

    def scoped_conflicts(*args, **kwargs):
        # Section 04 4.3 L3: only the conflicts named in F0 plus the delta.
        only = kwargs.get("only")
        if only is None and len(args) >= 3:
            only = args[2]
        if only is None:
            hit("call scan_open_conflicts")
        return conflicts(*args, **kwargs)

    engine.scan_open_conflicts = scoped_conflicts
    bindings = engine.validate_cold_archive_bindings

    def checkpointed_bindings(*args, **kwargs):
        # D4.3: residency at commit re-reads only months whose hash moved.
        if kwargs.get("checkpoint_months") is None:
            hit("call validate_cold_archive_bindings")
        return bindings(*args, **kwargs)

    engine.validate_cold_archive_bindings = checkpointed_bindings
    original_glob, original_rglob = Path.glob, Path.rglob
    original_iterdir = Path.iterdir
    original_listdir, original_scandir = os.listdir, os.scandir

    def nested(function, *args):
        depth["listing"] += 1
        try:
            return list(function(*args))
        finally:
            depth["listing"] -= 1

    def guarded_glob(self, pattern):
        check_listing("glob", self, pattern)
        return iter(nested(original_glob, self, pattern))

    def guarded_rglob(self, pattern):
        check_listing("rglob", self, pattern)
        return iter(nested(original_rglob, self, pattern))

    def guarded_iterdir(self):
        check_listing("iterdir", self)
        return iter(nested(original_iterdir, self))

    def guarded_listdir(path="."):
        check_listing("listdir", path)
        return original_listdir(path)

    def guarded_scandir(path="."):
        check_listing("scandir", path)
        return original_scandir(path)

    Path.glob, Path.rglob, Path.iterdir = guarded_glob, guarded_rglob, guarded_iterdir
    os.listdir, os.scandir = guarded_listdir, guarded_scandir


def install_open_counter():
    original_os_open = os.open
    original_path_open = Path.open

    def note(target, reading):
        if not reading or isinstance(target, int):
            return
        path = norm(target)
        if under_bdir(path) and not os.path.isdir(path):
            opened.add(os.path.relpath(path, BDIR).replace(os.sep, "/"))

    def guarded_open(file, mode="r", *args, **kwargs):
        note(file, not any(flag in mode for flag in "wax+"))
        return _real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        note(path, (flags & (os.O_WRONLY | os.O_RDWR)) == 0)
        return original_os_open(path, flags, *args, **kwargs)

    def guarded_path_open(self, mode="r", *args, **kwargs):
        note(self, not any(flag in mode for flag in "wax+"))
        return original_path_open(self, mode, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_open
    os.open = guarded_os_open
    Path.open = guarded_path_open

    def write_count():
        with _real_open(str(root / ".test-open-count"), "w", encoding="utf-8") as handle:
            handle.write("{}\n".format(len(opened)))
            for name in sorted(opened):
                handle.write(name + "\n")

    atexit.register(write_count)


if mode == "forbid":
    install_seam()
    install_open_counter()
    code = engine.main(["--root", str(root)] + command)
    sys.stdout.flush()
    sys.exit(code)
elif mode == "selfcheck":
    # The one-run exemption must not widen into a history scan.
    install_seam()
    paths = engine.Paths(root)
    results = {}
    probes = (
        ("proposals", "R*-Q*.json"), ("decisions", "*.json"),
        ("proposals", command[0] + "-Q*.json"),
        ("decisions", command[0] + "-Q*.json"),
        ("log", "R*.md"), ("revisions", "V*.md"), ("archive", "*.md"),
    )
    for directory, pattern in probes:
        label = directory + "/" + pattern
        try:
            list(getattr(paths, {"log": "logs"}.get(directory, directory)).glob(pattern))
            results[label] = "ok"
        except AssertionError:
            results[label] = "raised"
    for directory in ("proposals", "decisions"):
        try:
            list(getattr(paths, directory).iterdir())
            results[directory + "/iterdir"] = "ok"
        except AssertionError:
            results[directory + "/iterdir"] = "raised"
    print(json.dumps(results, sort_keys=True))
    sys.exit(0)
elif mode == "time_publish":
    # Section 04 4.5 cost paragraph / 4.11 test 12: time publication only.
    timing = {"calls": 0, "ms": 0.0}
    if hasattr(engine, "publish_scoped_checkpoint"):
        original = engine.publish_scoped_checkpoint

        def timed(*args, **kwargs):
            began = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timing["calls"] += 1
                timing["ms"] += (time.perf_counter() - began) * 1000.0

        engine.publish_scoped_checkpoint = timed

    def write_timing():
        with _real_open(str(root / ".test-publish-ms"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(timing))

    atexit.register(write_timing)
    code = engine.main(["--root", str(root)] + command)
    sys.stdout.flush()
    sys.exit(code)
elif mode == "facts_probe":
    # Risk note of section 09 step 3: the full rebuild equals the chain.
    paths = engine.Paths(root)
    with _real_open(str(paths.state), encoding="utf-8") as handle:
        state = json.load(handle)
    with _real_open(str(paths.audit_witness), encoding="utf-8") as handle:
        witness = json.load(handle)
    published_rows = engine.load_audit_manifest_evidence(paths, witness)
    published_facts = engine.load_audit_facts_evidence(paths, witness)
    full_rows = engine.normalize_audit_manifest(engine.audit_witness_manifest(paths))
    manifest_hash = engine.audit_witness_digest(full_rows)
    run_facts = engine.audit_witness_run_facts(paths, state)
    full_facts = engine.build_audit_facts(
        paths, state, run_facts, manifest_hash,
        created_at=published_facts.get("created_at"),
    )
    ignored = ("created_at", "facts_hash")
    keys = sorted(set(published_facts) | set(full_facts))
    print(json.dumps({
        "manifest_equal": engine.normalize_audit_manifest(published_rows) == full_rows,
        "manifest_hash_equal": witness.get("manifest_hash") == manifest_hash,
        "run_authority_equal": witness.get("run_authority_hash")
            == engine.audit_witness_digest(engine.normalize_audit_run_facts(run_facts)),
        "facts_diff": [
            key for key in keys
            if key not in ignored and published_facts.get(key) != full_facts.get(key)
        ],
        "row_diff": sorted(
            set(json.dumps(row, sort_keys=True) for row in full_rows)
            ^ set(json.dumps(row, sort_keys=True)
                  for row in engine.normalize_audit_manifest(published_rows))
        )[:10],
    }, sort_keys=True))
    sys.exit(0)
else:
    raise SystemExit("unknown step-3 driver mode: " + mode)
'''


GROW_DRIVER = r'''
import contextlib, importlib.util, io, json, re, sys
engine_path, root, plan_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("bimri_step3_grow", engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
with open(plan_path, encoding="utf-8") as handle:
    plan = json.load(handle)


def run(arguments):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = engine.main(["--root", root] + arguments)
    return code, buffer.getvalue()


done = 0
for index in range(plan["start"], plan["start"] + plan["count"]):
    key = "%s.%05d" % (plan["prefix"], index)
    code, text = run(["get", "--key", key, "--json"])
    token = json.loads(text)["token"]
    arguments = [
        "save", "--request", "%s-%05d" % (plan["request"], index),
        "--observed", token, "--key", key, "--new-subject",
        "--tier", str(plan["tier"]), "--text", plan["text"] % index,
    ]
    if plan["tier"] == 3:
        arguments += ["--confidence", "emerging", "--observations", "1",
                      "--falsifier", "A counterexample appears."]
    code, text = run(arguments)
    if code != 0 or not re.search(r"^BIMRI SAVE accepted ", text, re.M):
        sys.stderr.write("grow save %s failed: rc=%r\n%s\n" % (key, code, text))
        sys.exit(3)
    done += 1
print(json.dumps({"done": done}))
'''


# ---- module helpers ---------------------------------------------------------

def run_engine(root, *arguments, timeout=600):
    command = [sys.executable, str(ENGINE), "--root", str(root)] + [
        str(argument) for argument in arguments
    ]
    return subprocess.run(
        command, text=True, capture_output=True, timeout=timeout,
        encoding="utf-8",
    )


def must(root, *arguments):
    result = run_engine(root, *arguments)
    if result.returncode != 0:
        raise AssertionError(
            "fixture command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                arguments, result.stdout, result.stderr
            )
        )
    return result


def copy_store(source, destination):
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(str(destination))
    shutil.copytree(str(source), str(destination), ignore=COPY_IGNORE)
    lock = destination / ".bimri" / "engine.lock"
    if not lock.exists():
        lock.write_bytes(b"")


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


def write_state(root, mutate):
    path = Path(root) / ".bimri" / "state.json"
    state = read_json(path)
    mutate(state)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8")


def token_from_payload(payload, key):
    for match in payload.get("matches", []):
        if match.get("key") == key and match.get("location") in ("HOT", "COLD"):
            return match["token"]
    return payload["token"]


def key_token(root, key):
    result = run_engine(root, "get", "--key", key, "--json")
    if result.returncode not in (0, 1):
        raise AssertionError(result.stdout + result.stderr)
    return token_from_payload(json.loads(result.stdout), key)


def start_run(root, actor):
    # Build step 2 accepts --open-run as a no-op; step 4 makes it required
    # for the legacy lane (D1.1).
    result = must(root, "start", "--open-run", "--actor", actor)
    return RUN_RE.search(result.stdout).group(1)


def grow(root, prefix, start, count, tier=2, text=GROW_TEXT, request=None):
    plan_dir = Path(tempfile.mkdtemp(prefix="bimri-s3-plan-"))
    try:
        driver = plan_dir / "grow_driver.py"
        driver.write_text(GROW_DRIVER, "utf-8")
        plan = plan_dir / "plan.json"
        plan.write_text(json.dumps({
            "prefix": prefix, "start": start, "count": count, "tier": tier,
            "text": text, "request": request or prefix,
        }), "utf-8")
        result = subprocess.run(
            [sys.executable, str(driver), str(ENGINE), str(root), str(plan)],
            text=True, capture_output=True, timeout=3600, encoding="utf-8",
        )
        if result.returncode != 0:
            raise AssertionError(
                "fixture growth failed:\n" + result.stdout + result.stderr
            )
    finally:
        shutil.rmtree(str(plan_dir), ignore_errors=True)


def hot_bytes(root):
    return len((Path(root) / "bimri.md").read_bytes())


def build_f2(root):
    """Section 10 10.2 F2 (no --settles saves before build step 6)."""
    root = Path(root)
    seed = start_run(root, "step3-seed")
    must(
        root, "propose", "--run", seed, "--tier", "2", "--key", OWNER_KEY,
        "--text", OWNER_TEXT, "--source", "user", "--trust", "confirmed",
        "--new-subject",
    )
    must(
        root, "propose", "--run", seed, "--operation", "set", "--tier", "3",
        "--key", PATTERN_KEY, "--text", "Seed pattern for the fixture.",
        "--confidence", "established", "--observations", "3",
        "--falsifier", "A counterexample appears.", "--source", "agent",
        "--trust", "working", "--new-subject",
    )
    must(root, "sync", "--run", seed)
    must(root, "close", "--run", seed, "--outcome", "success", "--summary", "seeded")
    # 300 accepted saves; the view holds about 250 of them so ~50 cool.
    before = hot_bytes(root)
    grow(root, "grow", 0, 1)
    line = max(1, hot_bytes(root) - before)
    ceiling = hot_bytes(root) + (F2_HOT_FIT - 1) * line + line // 2
    write_state(root, lambda state: state.__setitem__("hot_max_bytes", ceiling))
    grow(root, "grow", 1, F2_SAVES - 1)
    grow(
        root, "pattern", 0, F2_PATTERNS, tier=3,
        text="Pattern number %d seen across the fixture runs.",
    )
    # One open stale-base conflict on a hot key (section 10 10.2 F1).
    create = start_run(root, "step3-conflict-create")
    must(
        root, "propose", "--run", create, "--tier", "2", "--key", CONFLICT_KEY,
        "--text", "Conflict subject original.", "--source", "agent",
        "--trust", "working", "--new-subject",
    )
    must(root, "sync", "--run", create)
    must(root, "close", "--run", create, "--outcome", "success", "--summary", "s")
    candidate = start_run(root, "step3-candidate")
    writer = start_run(root, "step3-writer")
    must(
        root, "propose", "--run", candidate, "--tier", "2", "--key",
        CONFLICT_KEY, "--text", "Candidate rewrite of the conflict subject.",
        "--source", "agent", "--trust", "working",
    )
    must(
        root, "propose", "--run", writer, "--tier", "2", "--key",
        CONFLICT_KEY, "--text", "Writer rewrite of the conflict subject.",
        "--source", "agent", "--trust", "working",
    )
    must(root, "sync", "--run", writer)
    run_engine(root, "sync", "--run", candidate)
    must(root, "close", "--run", writer, "--outcome", "success", "--summary", "s")
    run_engine(
        root, "close", "--run", candidate, "--outcome", "partial", "--summary", "s"
    )
    # One held candidate: an agent set over confirmed owner memory.
    held = run_engine(
        root, "save", "--request", "fixture-held", "--observed",
        key_token(root, OWNER_KEY), "--key", OWNER_KEY, "--text",
        "Agent rewrite of the owner rule.",
    )
    if "BIMRI SAVE held" not in held.stdout:
        raise AssertionError("fixture held save: " + held.stdout + held.stderr)
    run_engine(root, "get", "--key", OWNER_KEY, "--json")


_FIXTURES = {}
_FIXTURE_TEMP = []


def fixture(name):
    """Build F2 / F2x3 once per process (the sharded runner gives each worker
    its own copy) and return the template root; tests copy it."""
    if name in _FIXTURES:
        return _FIXTURES[name]
    if not _FIXTURE_TEMP:
        _FIXTURE_TEMP.append(tempfile.TemporaryDirectory(prefix="bimri-v520-s3-fx-"))
    base = Path(_FIXTURE_TEMP[0].name)
    if name == "F2":
        root = base / "F2"
        root.mkdir()
        build_f2(root)
    elif name == "F2x3":
        root = base / "F2x3"
        copy_store(fixture("F2"), root)
        grow(root, "grow3", 0, F2X3_EXTRA_SAVES, request="grow3")
        run_engine(root, "get", "--key", OWNER_KEY, "--json")
    else:
        raise KeyError(name)
    _FIXTURES[name] = root
    return root


def tearDownModule():
    for temporary in _FIXTURE_TEMP:
        temporary.cleanup()
    del _FIXTURE_TEMP[:]
    _FIXTURES.clear()


def store_copy_location():
    """Use a private acceptance snapshot only when explicitly supplied."""
    supplied = os.environ.get("BIMRI_STORE_COPY")
    if supplied:
        candidate = Path(supplied)
        if (candidate / ".bimri" / "state.json").is_file():
            return candidate
    return None


STORE_COPY = store_copy_location()


def parse_save_line(stdout):
    match = SAVE_LINE_RE.search(stdout)
    if match is None:
        return None
    fields = {"result": match.group(1), "line": match.group(0)}
    for pair in match.group(2).split(" "):
        name, _, value = pair.partition("=")
        fields[name] = value
    return fields


def normalized_text(text):
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        while stripped.startswith(">"):
            stripped = stripped[1:].strip()
        lines.append(stripped)
    return re.sub(r"\s+", " ", " ".join(lines)).replace("`", "")


# ---- base class ------------------------------------------------------------

class Step3Base(unittest.TestCase):
    maxDiff = None
    FIXTURE = "F2"

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s3-")
        self.workspace = Path(self._temp.name)
        self.driver = self.workspace / "step3_driver.py"
        self.driver.write_text(STEP3_DRIVER, "utf-8")
        self.root = self.workspace / "store"
        copy_store(fixture(self.FIXTURE), self.root)

    def tearDown(self):
        self._temp.cleanup()

    # process helpers

    def cli(self, *arguments, root=None, check=True):
        result = run_engine(root or self.root, *arguments)
        if check and result.returncode != 0:
            self.fail(
                "command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    arguments, result.stdout, result.stderr
                )
            )
        return result

    def drive(self, mode, *arguments, root=None, timeout=600):
        root = Path(root or self.root)
        for name in (".test-forbid-hits", ".test-open-count", ".test-publish-ms"):
            target = root / name
            if target.exists():
                target.unlink()
        command = [
            sys.executable, str(self.driver), str(ENGINE), mode, str(root),
        ] + [str(argument) for argument in arguments]
        return subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )

    def forbid(self, *arguments, root=None):
        return self.drive("forbid", *arguments, root=root)

    def assert_no_history_read(self, result, root=None):
        hits = Path(root or self.root) / ".test-forbid-hits"
        recorded = hits.read_text("utf-8") if hits.exists() else ""
        self.assertEqual(
            recorded, "",
            "warm write read history:\n{}\nstdout:\n{}\nstderr:\n{}".format(
                recorded, result.stdout, result.stderr
            ),
        )
        self.assertNotIn("save_forbid_history_scan", result.stderr)

    def open_count(self, root=None):
        lines = (Path(root or self.root) / ".test-open-count").read_text(
            "utf-8"
        ).splitlines()
        return int(lines[0]), lines[1:]

    def save_arguments(self, rid, key, observed, text=None, new_subject=False,
                       tier=None, extra=()):
        arguments = ["save", "--request", rid, "--observed", observed, "--key", key]
        if new_subject:
            arguments.append("--new-subject")
        if tier is not None or new_subject:
            arguments.extend(["--tier", str(tier or 2)])
        if text is not None:
            arguments.extend(["--text", text])
        arguments.extend(extra)
        return arguments

    def save(self, rid, key, observed, text=None, new_subject=False, tier=None,
             extra=(), root=None):
        return self.cli(
            *self.save_arguments(rid, key, observed, text, new_subject, tier, extra),
            root=root, check=False,
        )

    def forbid_save(self, rid, key, observed, text=None, new_subject=False,
                    tier=None, extra=(), root=None):
        return self.forbid(
            *self.save_arguments(rid, key, observed, text, new_subject, tier, extra),
            root=root,
        )

    def assert_save(self, result, outcome, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        fields = parse_save_line(result.stdout)
        self.assertIsNotNone(fields, result.stdout + result.stderr)
        self.assertEqual(fields["result"], outcome, result.stdout)
        return fields

    def token(self, key, root=None):
        return key_token(root or self.root, key)

    def start(self, actor, root=None):
        return start_run(root or self.root, actor)

    # store readers

    def bdir(self, root=None):
        return Path(root or self.root) / ".bimri"

    def state(self, root=None):
        return read_json(self.bdir(root) / "state.json")

    def witness(self, root=None):
        return read_json(self.bdir(root) / "audit-witness.json")

    def witness_bytes(self, root=None):
        return (self.bdir(root) / "audit-witness.json").read_bytes()

    def manifest_rows(self, witness=None, root=None):
        witness = witness or self.witness(root)
        record = read_json(
            self.bdir(root) / "audit-manifests"
            / "{}.json".format(witness["manifest_hash"])
        )
        rows = record["manifest"] if isinstance(record, dict) else record
        return {row["path"]: row["sha256"] for row in rows}

    def facts(self, witness=None, root=None):
        witness = witness or self.witness(root)
        return read_json(
            self.bdir(root) / "audit-manifests"
            / "facts-{}.json".format(witness["facts_hash"])
        )

    def drift_receipts(self, root=None):
        directory = self.bdir(root) / "audit-drift"
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.glob("D*.json"))

    def receipt_reasons(self, name, root=None):
        record = read_json(self.bdir(root) / "audit-drift" / name)
        return [str(reason) for reason in record.get("reasons", [])]

    def cold_keys(self, prefix="grow.", root=None):
        cold = self.state(root).get("cold_current", {})
        return sorted(key for key in cold if key.startswith(prefix))

    def hot_keys(self, prefix="grow.", root=None):
        found = []
        for line in (Path(root or self.root) / "bimri.md").read_text("utf-8").splitlines():
            match = re.search(r"\[K:([^\]]+)\]", line)
            if match and match.group(1).startswith(prefix):
                found.append(match.group(1))
        return sorted(found)

    def proposal_for_key(self, key, root=None):
        found = []
        for path in sorted(self.bdir(root).joinpath("proposals").glob("R*-Q*.json")):
            record = read_json(path)
            if record.get("key") == key:
                found.append(path.stem)
        return found

    def open_conflict_id(self, root=None):
        for path in sorted(self.bdir(root).joinpath("conflicts").glob("C*.json")):
            record = read_json(path)
            if record.get("key") == CONFLICT_KEY:
                resolution = self.bdir(root) / "resolutions" / path.name
                if not resolution.exists():
                    return path.stem, record
        self.fail("fixture has no open conflict on {}".format(CONFLICT_KEY))

    def held_proposal_id(self, root=None):
        for path in sorted(self.bdir(root).joinpath("decisions").glob("R*-Q*.json")):
            if read_json(path).get("outcome") == "held":
                return path.stem
        self.fail("fixture has no held candidate")

    def today_month(self):
        return time.strftime("%Y-%m")


# ---- gate 6: instrumented warm writes reject any whole-history scan ---------

class WarmWritesReadNoHistoryTest(Step3Base):
    """Section 10 10.4 gate 6 under the 10.1.4 save_forbid_history_scan seam."""

    def test_fixture_is_the_f2_shape(self):
        # Section 10 10.2 F2: 300 accepted saves, ~50 cooled, 8 Tier 3
        # patterns, one open stale-base conflict, one held candidate, warm.
        state = self.state()
        self.assertGreaterEqual(len(self.cold_keys()), 30)
        self.assertLessEqual(len(self.cold_keys()), 90)
        self.assertGreaterEqual(state["pattern_count"], F2_PATTERNS)
        conflict_id, record = self.open_conflict_id()
        self.assertEqual(record.get("type"), "stale-base", record)
        self.assertIn(CONFLICT_KEY, self.hot_keys(prefix=CONFLICT_KEY))
        self.held_proposal_id()
        witness = self.witness()
        self.assertEqual(witness["witness_schema"], 2)
        self.assertEqual(witness["head_revision"], state["head_revision"])

    def test_seam_control_missing_witness_trips_the_forbid_list(self):
        # D4.1 S3 / 4.8 item 2: with no witness the write takes the full
        # audit, so the instrument must see a history scan. This control keeps
        # the gate-6 tests from passing vacuously.
        # A read without a witness re-seeds it, so the token comes first.
        observed = self.token("probe.control")
        (self.bdir() / "audit-witness.json").unlink()
        result = self.forbid_save(
            "test-control-1", "probe.control", observed, "Control subject.",
            new_subject=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        hits = self.root / ".test-forbid-hits"
        self.assertTrue(hits.is_file(), result.stdout + result.stderr)
        self.assertRegex(hits.read_text("utf-8"), r"(?m)^(call|glob|iterdir|listdir|scandir|rglob) ")
        self.assertEqual(self.proposal_for_key("probe.control"), [])

    def test_warm_save_reads_no_history(self):
        # Gate 6 (BRIEF), D4.2/D4.3, section 10 10.4: on F2 under the seam a
        # plain set, a Tier 3 pattern save, a cold-base save and a legacy
        # close all succeed with no whole-history read, each on the scoped
        # path (audit_origin scoped, no drift receipt).
        receipts = self.drift_receipts()
        with self.subTest(write="set"):
            observed = self.token("gate6.set")
            result = self.forbid_save(
                "test-gate6-set", "gate6.set", observed, "Gate six set.",
                new_subject=True,
            )
            self.assert_no_history_read(result)
            self.assert_save(result, "accepted", 0)
            self.assertEqual(self.witness()["audit_origin"], "scoped")
        with self.subTest(write="tier 3 pattern"):
            observed = self.token("gate6.pattern")
            result = self.forbid_save(
                "test-gate6-pattern", "gate6.pattern", observed,
                "Gate six pattern seen twice.", new_subject=True, tier=3,
                extra=("--confidence", "emerging", "--observations", "1",
                       "--falsifier", "A counterexample appears."),
            )
            self.assert_no_history_read(result)
            self.assert_save(result, "accepted", 0)
            self.assertEqual(self.witness()["audit_origin"], "scoped")
        with self.subTest(write="cold base"):
            key = self.cold_keys()[0]
            observed = self.token(key)
            self.assertRegex(observed, r"^V\d{6}:cold:[0-9a-f]{64}$")
            result = self.forbid_save(
                "test-gate6-cold", key, observed, "Cold subject rewritten warm.",
            )
            self.assert_no_history_read(result)
            self.assert_save(result, "accepted", 0)
            self.assertEqual(self.witness()["audit_origin"], "scoped")
        with self.subTest(write="legacy close"):
            run_id = self.start("gate6-legacy")
            self.cli(
                "propose", "--run", run_id, "--tier", "2", "--key",
                "gate6.legacy", "--text", "Legacy staged subject.",
                "--source", "agent", "--trust", "working", "--new-subject",
            )
            result = self.forbid(
                "close", "--run", run_id, "--outcome", "success", "--summary",
                "gate six legacy close",
            )
            self.assert_no_history_read(result)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn(run_id, self.state()["active_runs"])
            self.assertEqual(self.witness()["audit_origin"], "scoped")
        self.assertEqual(self.drift_receipts(), receipts)

    def test_warm_unchanged_stale_and_held_saves_read_no_history(self):
        # Section 10 10.4 gate 6 row: unchanged, stale and held saves finish
        # under the seam with their D3.3.1 results and exit codes.
        key = self.hot_keys()[-1]
        current = self.token(key)
        with self.subTest(result="unchanged"):
            text = read_json(
                self.bdir() / "proposals"
                / "{}.json".format(self.proposal_for_key(key)[-1])
            )["text"]
            result = self.forbid_save("test-gate6-unchanged", key, current, text)
            self.assert_no_history_read(result)
            self.assert_save(result, "unchanged", 0)
        with self.subTest(result="stale"):
            moved = self.save("test-gate6-mover", key, current, "Moved on since.")
            self.assert_save(moved, "accepted", 0)
            result = self.forbid_save(
                "test-gate6-stale", key, current, "Written from an old read.",
            )
            self.assert_no_history_read(result)
            self.assert_save(result, "stale", 1)
        with self.subTest(result="held"):
            result = self.forbid_save(
                "test-gate6-held", OWNER_KEY, self.token(OWNER_KEY),
                "Another agent rewrite of owner memory.",
            )
            self.assert_no_history_read(result)
            self.assert_save(result, "held", 1)

    def test_warm_settles_save_reads_no_history(self):
        # Gate 6 row "a --settles save (from step 6 on)": settling the
        # fixture's held candidate reads no history.
        held = self.held_proposal_id()
        result = self.forbid_save(
            "test-gate6-settles", OWNER_KEY, self.token(OWNER_KEY),
            "Owner restated rule for the step three fixture.",
            extra=("--source", "user", "--trust", "confirmed", "--settles", held),
        )
        self.assert_no_history_read(result)
        self.assert_save(result, "accepted", 0)
        self.assertTrue((self.bdir() / "settlements" / "{}.json".format(held)).is_file())

    def test_warm_resolve_reads_no_history(self):
        # Section 10 10.4 gate 6 row and section 04 4.3 resolve row: an owner
        # resolution of the fixture conflict is scoped (the conflict, its
        # candidates, V<head>), never a conflict scan.
        conflict_id, record = self.open_conflict_id()
        candidate = sorted(set(PROPOSAL_RE.findall(json.dumps(record))))
        candidate = [
            proposal for proposal in candidate
            if read_json(self.bdir() / "decisions" / "{}.json".format(proposal))
            .get("outcome") == "contested"
        ]
        self.assertTrue(candidate, record)
        result = self.forbid(
            "resolve", conflict_id, "--choose", candidate[0], "--human-approved",
        )
        self.assert_no_history_read(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("{} resolved with {}".format(conflict_id, candidate[0]), result.stdout)
        self.assertEqual(self.witness()["audit_origin"], "scoped")

    def test_warm_legacy_propose_and_sync_read_no_history(self):
        # Section 09 step 3 callers cmd_propose and legacy sync; section 04
        # 4.3 rows: the one-run proposal glob is the only listing, the
        # proposal id comes from F0.watermarks, never next_proposal_id.
        run_id = self.start("gate6-propose")
        proposed = self.forbid(
            "propose", "--run", run_id, "--tier", "2", "--key",
            "gate6.propose", "--text", "Legacy proposed subject.",
            "--source", "agent", "--trust", "working", "--new-subject",
        )
        self.assert_no_history_read(proposed)
        self.assertEqual(proposed.returncode, 0, proposed.stdout + proposed.stderr)
        self.assertIn("{}-Q001".format(run_id), proposed.stdout)
        synced = self.forbid("sync", "--run", run_id)
        self.assert_no_history_read(synced)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        decision = read_json(self.bdir() / "decisions" / "{}-Q001.json".format(run_id))
        self.assertEqual(decision["outcome"], "accepted")

    def test_legacy_close_globs_one_run_only(self):
        # Section 10 10.4 test_legacy_close_and_abandon_glob_one_run_only
        # (close half): `close --run L1` with one staged proposal passes under
        # the seam; its <R>-Q*.json globs are the only listing exemption.
        run_id = self.start("gate6-close")
        self.cli(
            "propose", "--run", run_id, "--tier", "2", "--key", "gate6.close",
            "--text", "Staged before close.", "--source", "agent",
            "--trust", "working", "--new-subject",
        )
        result = self.forbid(
            "close", "--run", run_id, "--outcome", "success", "--summary", "s",
        )
        self.assert_no_history_read(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        decision = read_json(self.bdir() / "decisions" / "{}-Q001.json".format(run_id))
        self.assertEqual(decision["outcome"], "accepted")

    def test_legacy_abandon_globs_one_run_only(self):
        # Section 10 10.4 (abandon half): `recover-run --run L2 --abandon
        # --human-approved --summary s` passes under the seam.
        run_id = self.start("gate6-abandon")
        self.cli(
            "propose", "--run", run_id, "--tier", "2", "--key",
            "gate6.abandon", "--text", "Staged then abandoned.", "--source",
            "agent", "--trust", "working", "--new-subject",
        )
        result = self.forbid(
            "recover-run", "--run", run_id, "--abandon", "--human-approved",
            "--summary", "s",
        )
        self.assert_no_history_read(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        decision = read_json(self.bdir() / "decisions" / "{}-Q001.json".format(run_id))
        self.assertEqual(decision["outcome"], "abandoned")

    def test_forbid_seam_one_run_exemption_does_not_widen(self):
        # Section 10 10.4 worker self-check: under the same seam a history
        # glob of proposals/ or decisions/ raises; only the exact one-run
        # prefix R######-Q*.json is exempt.
        result = self.drive("selfcheck", "R000001")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["proposals/R*-Q*.json"], "raised")
        self.assertEqual(observed["decisions/*.json"], "raised")
        self.assertEqual(observed["proposals/iterdir"], "raised")
        self.assertEqual(observed["decisions/iterdir"], "raised")
        self.assertEqual(observed["log/R*.md"], "raised")
        self.assertEqual(observed["revisions/V*.md"], "raised")
        self.assertEqual(observed["archive/*.md"], "raised")
        self.assertEqual(observed["proposals/R000001-Q*.json"], "ok")
        self.assertEqual(observed["decisions/R000001-Q*.json"], "ok")


# ---- the scoped checkpoint: what a write looks at and what it adopts -------

class ScopedCheckpointTest(Step3Base):
    """D4.1-D4.5 on F2: publication, drift, damage and the facts round trip."""

    def test_warm_save_publishes_a_scoped_successor(self):
        # D2.3 / D4.4 / 4.5 P4: a warm save publishes audit_origin scoped,
        # scoped_publications + 1, audited_at carried forward, no receipt.
        before = self.witness()
        receipts = self.drift_receipts()
        observed = self.token("scoped.successor")
        result = self.save(
            "test-successor-1", "scoped.successor", observed,
            "Scoped successor subject.", new_subject=True,
        )
        self.assert_save(result, "accepted", 0)
        after = self.witness()
        self.assertEqual(after["audit_origin"], "scoped")
        self.assertEqual(after["scoped_publications"], before["scoped_publications"] + 1)
        self.assertEqual(after["audited_at"], before["audited_at"])
        self.assertEqual(after["head_revision"], before["head_revision"] + 1)
        self.assertEqual(self.drift_receipts(), receipts)

    def test_scoped_publish_carries_untouched_rows_byte_exact(self):
        # D4.4 / 4.5 P2 / 4.9 item 1: M1 equals M0 row for row except the
        # paths this save wrote (its proposal, decision, revision and the
        # archive month it cooled into).
        m0 = self.manifest_rows()
        observed = self.token("scoped.rows")
        result = self.save(
            "test-rows-1", "scoped.rows", observed, "Rows subject.",
            new_subject=True,
        )
        fields = self.assert_save(result, "accepted", 0)
        m1 = self.manifest_rows()
        proposal = fields["proposal"]
        allowed = {
            ".bimri/proposals/{}.json".format(proposal),
            ".bimri/decisions/{}.json".format(proposal),
            ".bimri/{}".format("revisions/{}.md".format(fields["revision"])),
            ".bimri/archive/{}.md".format(self.today_month()),
        }
        changed = sorted(
            path for path in set(m0) | set(m1) if m0.get(path) != m1.get(path)
        )
        self.assertTrue(set(changed) <= allowed, changed)
        for path in (
            ".bimri/proposals/{}.json".format(proposal),
            ".bimri/decisions/{}.json".format(proposal),
            ".bimri/revisions/{}.md".format(fields["revision"]),
        ):
            self.assertIn(path, changed)
            self.assertEqual(
                m1[path],
                hashlib.sha256((self.root / path).read_bytes()).hexdigest(),
            )
        untouched = [path for path in m0 if path not in allowed]
        self.assertGreater(len(untouched), F2_SAVES)
        self.assertEqual({path: m1[path] for path in untouched},
                         {path: m0[path] for path in untouched})

    def test_unrelated_out_of_band_change_is_never_adopted_by_saves(self):
        # 4.9 items 1 and 9 (and v511:1003 re-aimed): a benign out-of-band
        # change to an old proposal no save depends on keeps its OLD hash in
        # every scoped successor with no receipt; the next doctor receipts it.
        key = self.cold_keys()[1]
        proposal = self.proposal_for_key(key)[0]
        relative = ".bimri/proposals/{}.json".format(proposal)
        target = self.root / relative
        old_hash = self.manifest_rows()[relative]
        target.write_text(
            json.dumps(read_json(target), indent=4, sort_keys=True) + "\n", "utf-8"
        )
        self.assertNotEqual(hashlib.sha256(target.read_bytes()).hexdigest(), old_hash)
        receipts = self.drift_receipts()
        for index in range(3):
            key_name = "scoped.unrelated.{}".format(index)
            result = self.save(
                "test-unrelated-{}".format(index), key_name,
                self.token(key_name), "Unrelated subject {}.".format(index),
                new_subject=True,
            )
            self.assert_save(result, "accepted", 0)
            self.assertEqual(self.manifest_rows()[relative], old_hash)
            self.assertEqual(self.witness()["audit_origin"], "scoped")
        self.assertEqual(self.drift_receipts(), receipts)
        doctor = self.cli("doctor", "--budget-ms", FULL_AUDIT_CAPTURE_BUDGET_MS, check=False)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        new = [name for name in self.drift_receipts() if name not in receipts]
        self.assertEqual(len(new), 1, doctor.stdout)
        self.assertTrue(
            any(relative in reason for reason in self.receipt_reasons(new[0])),
            self.receipt_reasons(new[0]),
        )

    def test_unrelated_history_damage_is_caught_by_deep_audit_not_write(self):
        # D4.5: damage to an old proposal no write depends on does not stop
        # an ordinary save (accepted, scoped, no receipt); doctor's deep audit
        # condemns and names the damaged record.
        key = self.cold_keys()[2]
        proposal = self.proposal_for_key(key)[0]
        target = self.bdir() / "proposals" / "{}.json".format(proposal)
        record = read_json(target)
        record["text"] = "Tampered out of band; no write depends on this."
        target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", "utf-8")
        receipts = self.drift_receipts()
        result = self.save(
            "test-damage-1", "scoped.damage", self.token("scoped.damage"),
            "Saved beside unrelated damage.", new_subject=True,
        )
        self.assert_save(result, "accepted", 0)
        self.assertEqual(self.witness()["audit_origin"], "scoped")
        self.assertEqual(self.drift_receipts(), receipts)
        doctor = self.cli("doctor", "--budget-ms", FULL_AUDIT_CAPTURE_BUDGET_MS, check=False)
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        self.assertIn("FAILED", doctor.stdout)
        self.assertIn(proposal, doctor.stdout + doctor.stderr)
        verdict = read_json(self.bdir() / "audit-verdict.json")
        self.assertEqual(verdict["status"], "failed")
        status = self.cli("status", check=False)
        self.assertIn(RECOVERY_NEEDED, status.stdout)

    def test_dependency_drift_forces_full_audit_before_write(self):
        # D4.2 S8 / 4.2 FULL / 4.9 item 2: a byte change to the cold month a
        # cold-base save depends on is a dependency miss: exactly one receipt
        # naming it as a scoped miss, then the full audit (benign change:
        # passes) and the write proceeds.
        key = self.cold_keys()[3]
        month = self.state()["cold_current"][key]["archived_on"][:7]
        relative = ".bimri/archive/{}.md".format(month)
        with open(str(self.root / relative), "a", encoding="utf-8") as handle:
            handle.write("Owner note added by hand.\n")
        observed = self.token(key)
        receipts = self.drift_receipts()
        result = self.save(
            "test-drift-1", key, observed, "Cold subject after a month edit.",
        )
        self.assert_save(result, "accepted", 0)
        new = [name for name in self.drift_receipts() if name not in receipts]
        self.assertEqual(len(new), 1, new)
        reasons = self.receipt_reasons(new[0])
        self.assertTrue(
            any(
                reason.startswith(SCOPED_MISS_PREFIX + relative)
                for reason in reasons
            ),
            reasons,
        )
        # The miss was paid once: the next warm save is scoped and silent.
        follow = self.forbid_save(
            "test-drift-2", "scoped.after.drift", self.token("scoped.after.drift"),
            "After the drift.", new_subject=True,
        )
        self.assert_no_history_read(follow)
        self.assert_save(follow, "accepted", 0)
        self.assertEqual(len(self.drift_receipts()), len(receipts) + 1)

    def test_dependency_drift_with_damage_condemns_before_write(self):
        # D4.2 / 4.9 items 2 and 4: a dependency miss whose full audit fails
        # condemns (receipt first) and the save writes no effect; the store
        # reports AUTHORITY RECOVERY NEEDED.
        cold = self.state()["cold_current"]
        key, other = self.cold_keys()[4], self.cold_keys()[5]
        month = cold[key]["archived_on"][:7]
        self.assertEqual(cold[other]["archived_on"][:7], month)
        relative = ".bimri/archive/{}.md".format(month)
        target = self.root / relative
        text = target.read_text("utf-8")
        marker = "[K:{}]".format(other)
        lines = text.splitlines(True)
        index = max(i for i, line in enumerate(lines) if marker in line)
        self.assertIn("Grow subject number", lines[index])
        lines[index] = lines[index].replace("Grow subject number", "Grew subject number", 1)
        target.write_text("".join(lines), "utf-8")
        observed = self.token(key)
        receipts = self.drift_receipts()
        head = self.state()["head_revision"]
        result = self.save(
            "test-condemn-1", key, observed, "Never lands.",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIsNone(parse_save_line(result.stdout))
        new = [name for name in self.drift_receipts() if name not in receipts]
        self.assertEqual(len(new), 1, new)
        self.assertTrue(
            any(
                reason.startswith(SCOPED_MISS_PREFIX + relative)
                for reason in self.receipt_reasons(new[0])
            ),
            self.receipt_reasons(new[0]),
        )
        self.assertEqual(self.state()["head_revision"], head)
        status = self.cli("status", check=False)
        self.assertIn(RECOVERY_NEEDED, status.stdout)

    def test_missing_facts_evidence_refuses_scoped_save(self):
        # D2.2 / D4.1 S5 / 4.9 item 6 / 4.11 item 5: a valid witness whose
        # facts generation is missing is damaged evidence: the save refuses
        # with PRIOR_EVIDENCE_INVALID_PREFIX, never rebaselines, never takes
        # the scoped path.
        witness_bytes = self.witness_bytes()
        facts = self.bdir() / "audit-manifests" / "facts-{}.json".format(
            self.witness()["facts_hash"]
        )
        facts.unlink()
        result = self.save(
            "test-nofacts-1", "scoped.nofacts", "V{:06d}:absent".format(
                self.state()["head_revision"]
            ),
            "Never lands.", new_subject=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(PRIOR_EVIDENCE_INVALID_PREFIX, result.stderr)
        self.assertEqual(self.witness_bytes(), witness_bytes)
        self.assertFalse(facts.exists())
        self.assertEqual(self.proposal_for_key("scoped.nofacts"), [])

    def test_full_facts_equal_facts_after_scoped_writes(self):
        # Section 09 step 3 risk note / D2.2 / 4.11 item 10: after a chain of
        # scoped writes (resolve, accepted, Tier 3, held, cold base, stale,
        # legacy propose+sync+close) the published manifest and facts equal
        # a full rebuild from the files (writers_by_key, watermarks, held,
        # open_conflicts, run facts), apart from created_at. The resolve goes
        # first: the conflict key is the oldest tie in the full view, so the
        # first admission would cool it.
        conflict_id, record = self.open_conflict_id()
        contested = [
            proposal for proposal in sorted(set(PROPOSAL_RE.findall(json.dumps(record))))
            if read_json(self.bdir() / "decisions" / "{}.json".format(proposal))
            .get("outcome") == "contested"
        ]
        self.cli("resolve", conflict_id, "--choose", contested[0], "--human-approved")
        self.assert_save(self.save(
            "test-round-1", "round.set", self.token("round.set"),
            "Round trip set.", new_subject=True,
        ), "accepted", 0)
        self.assert_save(self.save(
            "test-round-2", "round.pattern", self.token("round.pattern"),
            "Round trip pattern.", new_subject=True, tier=3,
            extra=("--confidence", "emerging", "--observations", "1",
                   "--falsifier", "A counterexample appears."),
        ), "accepted", 0)
        self.assert_save(self.save(
            "test-round-3", OWNER_KEY, self.token(OWNER_KEY),
            "Round trip agent rewrite.",
        ), "held", 1)
        cold = self.cold_keys()[6]
        cold_token = self.token(cold)
        self.assert_save(self.save(
            "test-round-4", cold, cold_token, "Round trip cold rewrite.",
        ), "accepted", 0)
        self.assert_save(self.save(
            "test-round-5", cold, cold_token, "Round trip stale write.",
        ), "stale", 1)
        run_id = self.start("round-legacy")
        self.cli(
            "propose", "--run", run_id, "--tier", "2", "--key", "round.legacy",
            "--text", "Round trip legacy.", "--source", "agent", "--trust",
            "working", "--new-subject",
        )
        self.cli("sync", "--run", run_id)
        self.cli("close", "--run", run_id, "--outcome", "success", "--summary", "s")
        self.assertEqual(self.witness()["audit_origin"], "scoped")
        probe = self.drive("facts_probe")
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        report = json.loads(probe.stdout)
        self.assertEqual(report["row_diff"], [], report)
        self.assertTrue(report["manifest_equal"], report)
        self.assertTrue(report["manifest_hash_equal"], report)
        self.assertTrue(report["run_authority_equal"], report)
        self.assertEqual(report["facts_diff"], [], report)

    def test_scoped_save_facts_delta_names_writer_and_watermarks(self):
        # D2.2 / 4.5 P3 save row: F1.writers_by_key[K] names the save's Q,
        # revision and run; watermarks.run and proposal[R] advance; the run
        # fact is closed; nothing is left unsettled.
        before = self.facts()
        result = self.save(
            "test-delta-1", "scoped.delta", self.token("scoped.delta"),
            "Delta subject.", new_subject=True,
        )
        fields = self.assert_save(result, "accepted", 0)
        after = self.facts()
        run_id = fields["run"]
        writer = after["writers_by_key"]["scoped.delta"]
        self.assertEqual(writer["proposal_id"], fields["proposal"])
        self.assertEqual(writer["run_id"], run_id)
        self.assertEqual(writer["revision"], int(fields["revision"][1:]))
        self.assertEqual(after["watermarks"]["run"], int(run_id[1:]))
        self.assertGreater(after["watermarks"]["run"], before["watermarks"]["run"])
        self.assertEqual(after["watermarks"]["proposal"].get(run_id), 1)
        facts = [row for row in after["run_facts"] if row["proposal_id"] == fields["proposal"]]
        self.assertEqual(len(facts), 1)
        self.assertTrue(facts[0]["closed"])
        self.assertEqual(after["unsettled"], before["unsettled"])
        self.assertEqual(
            {k: v for k, v in after["writers_by_key"].items() if k != "scoped.delta"},
            {k: v for k, v in before["writers_by_key"].items() if k != "scoped.delta"},
        )

    def test_tier3_save_advances_pattern_watermark(self):
        # D2.2 / D3.2 L8: a Tier 3 save takes its pattern id from
        # watermarks.pattern + 1 (never the proposal glob of next_pattern_id)
        # and the published watermark advances by one.
        before = self.facts()
        result = self.forbid_save(
            "test-pattern-1", "scoped.pattern", self.token("scoped.pattern"),
            "Scoped pattern subject.", new_subject=True, tier=3,
            extra=("--confidence", "emerging", "--observations", "1",
                   "--falsifier", "A counterexample appears."),
        )
        self.assert_no_history_read(result)
        self.assert_save(result, "accepted", 0)
        after = self.facts()
        self.assertEqual(after["watermarks"]["pattern"], before["watermarks"]["pattern"] + 1)
        self.assertEqual(self.state()["pattern_count"], after["watermarks"]["pattern"])


# ---- publication cost and the open-file bound (F2 against F2x3) ------------

class PublicationCostTest(Step3Base):
    """Section 10 LD10.4 and 04 4.11 test 12, on F2 and F2x3."""

    def save_on(self, root, rid, key, mode):
        observed = key_token(root, key)
        return self.drive(
            mode, *self.save_arguments(rid, key, observed, "Cost subject.", True),
            root=root,
        )

    def test_warm_save_open_file_count_is_bounded(self):
        # LD10.4 / Decisions 22: a warm accepted save reads at most 64
        # distinct .bimri/ files on F2, and F2x3 (three times the history)
        # reads at most 8 more.
        large = self.workspace / "large"
        copy_store(fixture("F2x3"), large)
        small = self.forbid_save(
            "test-open-small", "cost.open", self.token("cost.open"),
            "Open count subject.", new_subject=True,
        )
        self.assert_no_history_read(small)
        self.assert_save(small, "accepted", 0)
        small_count, small_files = self.open_count()
        big = self.forbid_save(
            "test-open-large", "cost.open", key_token(large, "cost.open"),
            "Open count subject.", new_subject=True, root=large,
        )
        self.assert_no_history_read(big, root=large)
        self.assert_save(big, "accepted", 0)
        large_count, large_files = self.open_count(large)
        sys.stderr.write(
            "\n[step3] warm save distinct .bimri files read: F2={} F2x3={}\n".format(
                small_count, large_count
            )
        )
        self.assertLessEqual(small_count, OPEN_COUNT_CEILING, small_files)
        self.assertLessEqual(large_count, OPEN_COUNT_CEILING, large_files)
        self.assertLessEqual(large_count, small_count + OPEN_COUNT_SLOPE, large_files)

    def test_scoped_publication_cost_slope(self):
        # 04 4.5 cost paragraph / 4.11 test 12 / section 10 10.4: the one
        # O(inventory) term a warm save keeps grows linearly: F2x3 <= 4 x F2
        # + 50 ms.
        large = self.workspace / "large"
        copy_store(fixture("F2x3"), large)
        figures = {}
        for label, root in (("F2", self.root), ("F2x3", large)):
            result = self.save_on(root, "test-slope-" + label.lower(), "cost.slope", "time_publish")
            self.assert_save(result, "accepted", 0)
            timing = json.loads((root / ".test-publish-ms").read_text("utf-8"))
            self.assertGreaterEqual(timing["calls"], 1, timing)
            figures[label] = timing["ms"]
        sys.stderr.write(
            "\n[step3] save_publish_ms F2={:.1f} F2x3={:.1f}\n".format(
                figures["F2"], figures["F2x3"]
            )
        )
        self.assertLessEqual(figures["F2x3"], 4.0 * figures["F2"] + 50.0, figures)


# ---- F4: the live-store copy on this machine (skipped without it) ----------

@unittest.skipIf(STORE_COPY is None, "no store-copy on this machine (BIMRI_STORE_COPY)")
class LiveCopyTest(unittest.TestCase):
    """Gate 6 on F4 (section 10 10.2/10.3); never CI."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s3-f4-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"
        self.root.mkdir()
        shutil.copy2(str(STORE_COPY / "bimri.md"), str(self.root / "bimri.md"))
        shutil.copytree(
            str(STORE_COPY / ".bimri"), str(self.root / ".bimri"), ignore=COPY_IGNORE
        )
        (self.root / ".bimri" / "engine.lock").write_bytes(b"")

    def tearDown(self):
        self._temp.cleanup()

    def test_save_p95_under_one_second_on_live_copy(self):
        # Gate 6 (BRIEF) / section 10 10.3: the store-copy benchmark's warm
        # saves stay under 1000 ms at p95 (interpreter start-up included);
        # the benchmark also records the lock hold per save.
        result = subprocess.run(
            [
                sys.executable, str(BENCHMARK), "--store", str(self.root),
                "--samples", "20", "--gate", "save_p95_ms=1000",
            ],
            text=True, capture_output=True, timeout=3600, encoding="utf-8",
        )
        retained = re.search(r"disposable copy retained at (.+)$", result.stdout, re.M)
        if retained:
            shutil.rmtree(retained.group(1).strip(), ignore_errors=True)
        sys.stderr.write("\n[step3] store-copy benchmark:\n" + result.stdout + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all gates passed", result.stdout)
        self.assertRegex(result.stdout, r"(?mi)^.*\bsave\b.*p95\s+[\d.]+ ms")

    def test_scoped_publication_cost_on_live_copy(self):
        # 04 4.11 test 12: publication on the live-store copy stays <= 250 ms
        # (measured ~30 ms) after the seeding full audit.
        seeded = run_engine(self.root, "doctor", "--live", timeout=1800)
        self.assertIn(seeded.returncode, (0,), seeded.stdout + seeded.stderr)
        driver = self.workspace / "step3_driver.py"
        driver.write_text(STEP3_DRIVER, "utf-8")
        observed = key_token(self.root, "bench.slope.00001")
        result = subprocess.run(
            [
                sys.executable, str(driver), str(ENGINE), "time_publish",
                str(self.root), "save", "--request", "test-f4-slope",
                "--observed", observed, "--key", "bench.slope.00001",
                "--new-subject", "--tier", "2", "--text", "Slope probe.",
            ],
            text=True, capture_output=True, timeout=1800, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BIMRI SAVE accepted", result.stdout)
        timing = json.loads((self.root / ".test-publish-ms").read_text("utf-8"))
        sys.stderr.write("\n[step3] save_publish_ms F4={:.1f}\n".format(timing["ms"]))
        self.assertGreaterEqual(timing["calls"], 1, timing)
        self.assertLessEqual(timing["ms"], 250.0, timing)


# ---- documents and gates that land with this step --------------------------

class StepThreeDocumentsTest(unittest.TestCase):

    def protocol_section(self, number):
        text = PROTOCOL.read_text("utf-8")
        match = re.search(
            r"(?ms)^## {}\. .*?(?=^## \d+\. |\Z)".format(number), text
        )
        self.assertIsNotNone(match, "protocol section {} missing".format(number))
        return normalized_text(match.group(0))

    def test_protocol_carries_d45_sentence_in_sections_3_and_12(self):
        # D4.5 / D8.3 / section 09 step 3 (g): the scoped-checkpoint sentence
        # lands verbatim in BIMRI-PROTOCOL.md sections 3 and 12.
        sentence = normalized_text(D45_SENTENCE)
        self.assertIn(sentence, self.protocol_section(3))
        self.assertIn(sentence, self.protocol_section(12))

    def test_benchmark_canonical_gates_include_save_p50(self):
        # Section 09 step 3 (f) / LD10.7: CANONICAL_GATES gains save_p50_ms at
        # the propose ceiling (6000 ms) for --init.
        spec = importlib.util.spec_from_file_location("bimri_step3_bench", BENCHMARK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.CANONICAL_GATES.get("save_p50_ms"), 6000.0)
        self.assertEqual(
            module.CANONICAL_GATES["save_p50_ms"],
            module.CANONICAL_GATES["propose_p50_ms"],
        )


if __name__ == "__main__":
    unittest.main()
