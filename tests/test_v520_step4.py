"""BIMRI v5.2 build step 4: a read opens nothing, with the docs.

Spine: design/DECISIONS.md D1.1, D1.13, D1.14, D1.15, D6.1, D8.1, D8.2,
D8.3 ("no degraded run is opened by start"); Decisions 3, 4, 9, 11, 17, 19,
30, 38. Detail: design/DESIGN.md section 01 5 and 6, section 06 1.1-1.4,
section 08 8.5.4-8.5.5, section 09 step 4 and section 10 gate 1.

Process model (section 10.0): every behaviour is driven through the public
command boundary in a separate process. Three small drivers live in this
file and are written into each test's temp workspace, because build step 4
adds no crash_worker mode:

- ``LOOP_SOURCE`` (section 10.4 gate 1 ``loop_start_<n>``): imports the
  engine by path and calls ``engine.main(argv)`` n times in one process,
  optionally with the read-brief forbid list installed (the
  ``valid_witness_forbid_full_audit`` list of tests/crash_worker.py plus
  ``archive_records`` and ``cold_entry``, section 06 1.2), and writes a JSON
  summary (exit codes, ``Observed:`` tokens, header lines, slowest call);
- ``FIRST_LINE_SOURCE`` (section 10.4 gate 1
  ``exit_after_first_stdout_line``): replaces ``sys.stdout`` with a stream
  that forwards the first complete line and then calls ``os._exit(0)``;
- ``LOCK_HOLDER_SOURCE``: holds ``engine_lock`` of one root until a release
  file appears.

Fixture F1 (section 10.2) is built once per class with the legacy lane
(``start --open-run``): T1 user/confirmed fact, T2 agent/working, T3
pattern, one cold subject, one held candidate, one open stale-base
conflict, every run closed, then one ``get`` so the checkpoint is warm.
"""

import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
PRIOR_RELEASE_COMMIT = "2c936ec"
# Section 10.2 F4: JJ's store copy, only when the environment names it.
STORE_COPY = Path(os.environ.get("BIMRI_STORE_COPY", "") or os.devnull)

RUN_RE = re.compile(r"BIMRI RUN HANDLE: (R\d{6})")
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# D6.1 / section 06 1.1 line grammar.
READ_HEADER_RE = re.compile(
    r"^=== BIMRI BRIEF read \| (\d{4}-\d{2}-\d{2}) \| ([^ ]+) ===$"
)
RUN_HEADER_RE = re.compile(
    r"^=== BIMRI BRIEF (R\d{6}) \| (\d{4}-\d{2}-\d{2}) \| ([^ ]+) ===$"
)
REVISION_RE = re.compile(
    r"^Memory revision V(\d{6}) \| ~(\d+) tokens \| "
    r"T1 (\d+) \(soft target (\d+)\) \| T2 (\d+) \(soft target (\d+)\) \| "
    r"T3 (\d+) \(soft target (\d+)\)$"
)
OBSERVED_RE = re.compile(r"^Observed: (V\d{6}:head:[0-9a-f]{64})$")
COLD_LINE_RE = re.compile(
    r"^Cold current: (\d+) subjects\.(?: List them: (.+) cold)?$"
)
UNFINISHED_RE = re.compile(
    r"^Unfinished save: (\S+) \(admitted ([^)]+)\); "
    r"the next save or doctor finishes it$"
)
ORPHAN_RE = re.compile(
    r"^ORPHAN CANDIDATES \(never auto-closed\): (.*)$", re.M
)
# Section 05 1.3 status line (the D5.1 count of open tickets).
STATUS_COLD_RE = re.compile(
    r"^Cold current: (\d+) \| Held candidates: (\d+|\?) \| "
    r"Unfinished saves: (\d+)$"
)
END_BRIEF = "=== END BRIEF ==="
SAVE_WITH_PREFIX = "Save with: "
FALLBACK_COMMAND = "python bimri-engine.py"
CHECKPOINT_UNPROVEN = "Checkpoint: unproven; run doctor"
CHECKPOINT_PRIOR_POLICY = "Checkpoint: prior policy; run doctor"
BANNER = "AUTHORITY RECOVERY NEEDED"
VIEW_DIFFERS_NOTICE = "BIMRI NOTICE: bimri.md differs from the accepted head"
JOURNAL_NOT_ACTIVE = (
    "journal needs an active legacy run (start --open-run); "
    "a save journals its --rationale."
)
CHAT_SENTENCE_RE = re.compile(
    r"A chat reads with `?start`? and remembers with `?save`?; "
    r"runs are for deliberate multi-step work\."
)
AGENT_BLOCK_HEADER = (
    "## BIMRI Engine v5.2.1 | Authority Format v5.1.0 | "
    "State Format v5.2.1 | Hot Grammar v5.0.2"
)
# D6.1 --json object (section 06 1.4 adds "checkpoint", local decision 15).
D61_JSON_KEYS = {
    "brief", "date", "actor", "head_revision", "head_hash", "token",
    "tokens_estimate", "tiers", "authority_issues", "orphan_candidates",
    "validation_errors", "cold_count", "cold_command", "unfinished_save",
}

# Section 10.4 gate 1: the six payloads of test_v512_defects.py:353-359
# plus a well-formed one.
HOOK_PAYLOADS = (
    ("empty", ""),
    ("invalid", "not json"),
    ("array", "[1, 2]"),
    ("no-ids", json.dumps({"foo": "bar"})),
    ("blank-id", json.dumps({"session_id": "   "})),
    ("bool-id", json.dumps({"session_id": True})),
    ("well-formed", json.dumps({
        "session_id": "sess-1",
        "transcript_path": "C:/x/sess-1.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
    })),
)

# Section 06 1.2: the read brief calls none of these, warm or cold (the
# valid_witness_forbid_full_audit list plus archive_records and cold_entry).
READ_BRIEF_FORBIDDEN = (
    "authority_storage_issues",
    "audit_witness_manifest",
    "audit_witness_run_facts",
    "load_audit_manifest_evidence",
    "scan_open_conflicts",
    "held_recall_records",
    "archive_records",
    "cold_entry",
)

PROTECTED_EXCLUSIONS = {
    ".bimri/engine.lock",
    ".bimri/runtime.local.json",
    ".bimri/hooks.claude.local.json",
}

# F1 contents.
F1_OWNER_KEY = "f1.owner"
F1_AGENT_KEY = "f1.agent"
F1_PATTERN_KEY = "f1.pattern"
F1_COLD_KEY = "f1.cold"
F1_PUSHER_KEY = "f1.pusher"
F1_TIERS = (1, 2, 1)


LOOP_SOURCE = r'''
import importlib.util
import io
import json
import sys
import time


def main():
    engine_path, out_path = sys.argv[1], sys.argv[2]
    count = int(sys.argv[3])
    forbid = json.loads(sys.argv[4])
    stdin_text = sys.argv[5]
    argv = sys.argv[6:]
    spec = importlib.util.spec_from_file_location(
        "bimri_v520_s4_loop_target", engine_path
    )
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    violations = []

    def make(label):
        def reject(*_args, **_kwargs):
            violations.append(label)
            raise AssertionError("read brief reached forbidden " + label)
        return reject

    for name in forbid:
        if hasattr(engine, name):
            setattr(engine, name, make(name))
    codes = {}
    observed = set()
    headers = set()
    checkpoints = set()
    stderr_samples = set()
    banners = 0
    handles = 0
    slowest = 0.0
    real_out, real_err, real_in = sys.stdout, sys.stderr, sys.stdin
    for _ in range(count):
        out = io.StringIO()
        err = io.StringIO()
        sys.stdout, sys.stderr, sys.stdin = out, err, io.StringIO(stdin_text)
        began = time.perf_counter()
        try:
            code = engine.main(list(argv))
        except SystemExit as exc:
            code = exc.code
        finally:
            elapsed = (time.perf_counter() - began) * 1000.0
            sys.stdout, sys.stderr, sys.stdin = real_out, real_err, real_in
        slowest = max(slowest, elapsed)
        key = str(code if isinstance(code, int) else 0)
        codes[key] = codes.get(key, 0) + 1
        lines = out.getvalue().splitlines()
        if lines:
            headers.add(lines[0])
        for line in lines:
            if line.startswith("Observed: "):
                observed.add(line[len("Observed: "):])
            if line.startswith("Checkpoint: "):
                checkpoints.add(line)
            if line.startswith("AUTHORITY RECOVERY NEEDED"):
                banners += 1
            if line.startswith("BIMRI RUN HANDLE"):
                handles += 1
        if err.getvalue():
            stderr_samples.add(err.getvalue()[:400])
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({
            "codes": codes,
            "observed": sorted(observed),
            "headers": sorted(headers),
            "checkpoints": sorted(checkpoints),
            "stderr": sorted(stderr_samples)[:5],
            "banners": banners,
            "handles": handles,
            "slowest_ms": slowest,
            "violations": sorted(set(violations)),
        }, handle)


if __name__ == "__main__":
    main()
'''


FIRST_LINE_SOURCE = r'''
import importlib.util
import os
import sys

engine_path = sys.argv[1]
argv = sys.argv[2:]
spec = importlib.util.spec_from_file_location(
    "bimri_v520_s4_first_line", engine_path
)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
real = sys.stdout


class FirstLineThenDie(object):
    encoding = "utf-8"
    errors = "strict"

    def __init__(self):
        self.pending = ""

    def write(self, text):
        self.pending += text
        if "\n" in self.pending:
            real.write(self.pending.split("\n", 1)[0] + "\n")
            real.flush()
            os._exit(0)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


sys.stdout = FirstLineThenDie()
code = engine.main(argv)
sys.exit(code if isinstance(code, int) else 0)
'''


LOCK_HOLDER_SOURCE = r"""
import importlib.util
import sys
import time
from pathlib import Path

engine_path, root, signal, release = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("bimri_v520_s4_lock", engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
with engine.engine_lock(engine.Paths(Path(root))):
    Path(signal).write_text("held\n", encoding="utf-8")
    deadline = time.monotonic() + 60
    while not Path(release).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""


def protected_tree_snapshot(root):
    """Copy of tests/test_bimri_engine.py:62 (kept local)."""
    root = Path(root)
    records = {}

    def record(path):
        metadata = path.lstat()
        mode = metadata.st_mode
        reparse = bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        )
        if stat.S_ISLNK(mode) or reparse:
            return {"type": "symlink", "target": os.readlink(path)}
        if stat.S_ISDIR(mode):
            return {"type": "directory"}
        if stat.S_ISREG(mode):
            content = path.read_bytes()
            return {
                "type": "file",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        return {"type": "other", "mode": stat.S_IFMT(mode)}

    hot = root / "bimri.md"
    records["bimri.md"] = (
        record(hot) if hot.exists() or hot.is_symlink() else {"type": "missing"}
    )

    def visit(directory):
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if relative in PROTECTED_EXCLUSIONS:
                continue
            value = record(path)
            records[relative] = value
            if value["type"] == "directory":
                visit(path)

    if (root / ".bimri").is_dir():
        visit(root / ".bimri")
    return records


def changed_paths(before, after):
    return sorted(
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def audit_digest(value):
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def record_seal(record, seal_field):
    return audit_digest(
        {key: value for key, value in record.items() if key != seal_field}
    )


def copy_store(source, destination):
    """BRIEF recipe: copy ignoring the lock and runtime binding, then an
    empty engine.lock; host-only hook records never travel (10.2 F4)."""
    shutil.copytree(
        str(source), str(destination),
        ignore=shutil.ignore_patterns("engine.lock", "runtime.local.json"),
    )
    (Path(destination) / ".bimri" / "engine.lock").write_bytes(b"")
    hooks = Path(destination) / ".bimri" / "hooks.claude.local.json"
    if hooks.exists():
        hooks.unlink()


def stamp(hours_ago):
    return (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_release_engine(workspace, commit):
    shown = subprocess.run(
        ["git", "-c", f"safe.directory={REPOSITORY.as_posix()}", "show",
         f"{commit}:bimri-engine.py"],
        cwd=str(REPOSITORY), capture_output=True, timeout=60,
    )
    if shown.returncode == 0:
        engine = workspace / f"release-{commit}.py"
        engine.write_bytes(shown.stdout)
        return engine
    return None


def frozen_release_engine(workspace):
    """Use the public 5.1.2 commit in a full checkout, or a supplied fixture."""
    named = os.environ.get("BIMRI_FROZEN_ENGINE_512")
    if named:
        candidate = Path(named)
        if not candidate.is_file():
            raise AssertionError("BIMRI_FROZEN_ENGINE_512 does not name a file")
        return candidate
    public = public_release_engine(workspace, PRIOR_RELEASE_COMMIT)
    if public is not None:
        return public
    for parent in Path(__file__).resolve().parents[1:6]:
        candidate = parent / "redteam" / "engine-5.1.2-69757b3.py"
        if candidate.is_file():
            return candidate
    return None


def normalized(text):
    return " ".join(text.split())


class V520Step4ReadBriefTest(unittest.TestCase):
    maxDiff = None

    # ---- class fixture F1 --------------------------------------------------

    @classmethod
    def setUpClass(cls):
        cls._class_temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s4-")
        cls.class_workspace = Path(cls._class_temp.name)
        cls.template = cls.class_workspace / "f1"
        try:
            cls.build_f1(cls.template)
        except Exception:
            cls._class_temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._class_temp.cleanup()

    @classmethod
    def run_engine(cls, root, *arguments, input_text="", engine=None,
                   timeout=300):
        command = [
            sys.executable, str(engine or ENGINE), "--root", str(root),
        ] + [str(argument) for argument in arguments]
        return subprocess.run(
            command, text=True, capture_output=True, input=input_text,
            timeout=timeout, encoding="utf-8",
        )

    @classmethod
    def class_cli(cls, root, *arguments):
        result = cls.run_engine(root, *arguments)
        if result.returncode != 0:
            raise AssertionError(
                "fixture command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    arguments, result.stdout, result.stderr
                )
            )
        return result

    @classmethod
    def class_open_run(cls, root, actor):
        started = cls.class_cli(root, "start", "--open-run", "--actor", actor)
        match = RUN_RE.search(started.stdout)
        if match is None:
            raise AssertionError("no run handle:\n" + started.stdout)
        return match.group(1)

    @classmethod
    def class_close(cls, root, run_id):
        cls.class_cli(
            root, "close", "--run", run_id, "--outcome", "success",
            "--summary", "fixture",
        )

    @classmethod
    def build_f1(cls, root):
        """Section 10.2 F1 through the legacy lane."""
        seed = cls.class_open_run(root, "f1-seed")
        cls.class_cli(
            root, "propose", "--run", seed, "--tier", "1", "--kind", "fact",
            "--key", F1_OWNER_KEY, "--text", "The owner fact.",
            "--source", "user", "--trust", "confirmed", "--new-subject",
        )
        cls.class_cli(
            root, "propose", "--run", seed, "--tier", "2",
            "--key", F1_AGENT_KEY, "--text", "The agent note.",
            "--source", "agent", "--trust", "working", "--new-subject",
        )
        cls.class_cli(
            root, "propose", "--run", seed, "--tier", "3",
            "--key", F1_PATTERN_KEY, "--text", "The pattern holds.",
            "--confidence", "emerging", "--observations", "1",
            "--falsifier", "F.", "--source", "agent", "--trust", "working",
            "--new-subject",
        )
        cls.class_cli(root, "sync", "--run", seed)
        cls.class_close(root, seed)
        # seed_cold_current recipe (test_v510_lifecycle.py:224 shape).
        cold = cls.class_open_run(root, "f1-cold")
        cls.class_cli(
            root, "propose", "--run", cold, "--tier", "2",
            "--key", F1_COLD_KEY, "--text", "Current cold value. " + "c" * 390,
            "--importance", "1", "--source", "agent", "--trust", "working",
            "--new-subject",
        )
        cls.class_cli(root, "sync", "--run", cold)
        cls.class_close(root, cold)
        state_path = root / ".bimri" / "state.json"
        state = json.loads(state_path.read_text("utf-8"))
        ceiling = state["hot_max_bytes"]
        state["hot_max_bytes"] = len((root / "bimri.md").read_bytes()) + 4
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        pusher = cls.class_open_run(root, "f1-push")
        cls.class_cli(
            root, "propose", "--run", pusher, "--tier", "2",
            "--key", F1_PUSHER_KEY, "--text", "Pushes the view.",
            "--source", "agent", "--trust", "working", "--new-subject",
        )
        cls.class_cli(root, "sync", "--run", pusher)
        cls.class_close(root, pusher)
        state = json.loads(state_path.read_text("utf-8"))
        if sorted(state["cold_current"]) != [F1_COLD_KEY]:
            raise AssertionError(
                "fixture cooled {!r}".format(sorted(state["cold_current"]))
            )
        state["hot_max_bytes"] = ceiling
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        # One held candidate: an agent set over the confirmed owner key.
        held = cls.class_open_run(root, "f1-held")
        cls.class_cli(
            root, "propose", "--run", held, "--key", F1_OWNER_KEY,
            "--text", "Agent rewrite.", "--source", "agent",
            "--trust", "working",
        )
        cls.class_cli(root, "sync", "--run", held)
        cls.class_close(root, held)
        # One open stale-base conflict (stage_concurrent_candidate shape).
        candidate = cls.class_open_run(root, "f1-candidate")
        writer = cls.class_open_run(root, "f1-writer")
        cls.class_cli(
            root, "propose", "--run", candidate, "--key", F1_AGENT_KEY,
            "--text", "Candidate text.", "--source", "agent",
            "--trust", "working",
        )
        cls.class_cli(
            root, "propose", "--run", writer, "--key", F1_AGENT_KEY,
            "--text", "Writer text.", "--source", "agent",
            "--trust", "working",
        )
        cls.class_cli(root, "sync", "--run", writer)
        cls.class_close(root, writer)
        cls.class_cli(root, "sync", "--run", candidate)
        cls.class_close(root, candidate)
        # One read publishes the checkpoint so every copy starts warm.
        cls.run_engine(root, "get", "--key", F1_AGENT_KEY, "--json")
        state = json.loads(state_path.read_text("utf-8"))
        if state["active_runs"]:
            raise AssertionError("fixture left active runs")
        if not (root / ".bimri" / "audit-witness.json").is_file():
            raise AssertionError("fixture has no checkpoint")

    # ---- per-test setup ----------------------------------------------------

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s4-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"
        copy_store(self.template, self.root)
        self._processes = []

    def tearDown(self):
        for process in self._processes:
            if process.poll() is None:
                process.kill()
                try:
                    process.communicate(timeout=30)
                except Exception:
                    pass
        self._temp.cleanup()

    # ---- process helpers (test_v512_defects.py:59 recipe) ------------------

    def cli(self, *arguments, root=None, input_text="", check=True,
            timeout=300, engine=None):
        result = self.run_engine(
            root or self.root, *arguments, input_text=input_text,
            engine=engine, timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(
                "command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    arguments, result.stdout, result.stderr
                )
            )
        return result

    def open_run(self, actor, *extra, root=None):
        result = self.cli(
            "start", "--open-run", "--actor", actor, *extra, root=root
        )
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def propose(self, run_id, key, text, root=None, new_subject=True,
                source="agent", trust="working"):
        arguments = [
            "propose", "--run", run_id, "--tier", "2", "--key", key,
            "--text", text, "--source", source, "--trust", trust,
        ]
        if new_subject:
            arguments.append("--new-subject")
        result = self.cli(*arguments, root=root)
        match = PROPOSAL_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(0)

    def worker(self, mode, *arguments, root=None, input_text="",
               timeout=300):
        command = [
            sys.executable, str(CRASH_WORKER), str(ENGINE), mode,
            str(root or self.root),
        ] + [str(argument) for argument in arguments]
        return subprocess.run(
            command, text=True, capture_output=True, input=input_text,
            timeout=timeout, encoding="utf-8",
        )

    def script(self, name, source):
        path = self.workspace / name
        if not path.is_file():
            path.write_text(source, encoding="utf-8")
        return path

    def loop(self, count, *arguments, root=None, forbid=False,
             stdin_text="", timeout=900):
        """Section 10.4 gate 1 loop_start_<n>: count calls of engine.main
        in one process; returns the JSON summary."""
        script = self.script("v520_s4_loop.py", LOOP_SOURCE)
        out = self.workspace / "loop-{}.json".format(uuid.uuid4().hex)
        root = Path(root or self.root)
        command = [
            sys.executable, str(script), str(ENGINE), str(out), str(count),
            json.dumps(list(READ_BRIEF_FORBIDDEN) if forbid else []),
            stdin_text, "--root", str(root),
        ] + [str(argument) for argument in arguments]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(out.is_file(), result.stdout + result.stderr)
        return json.loads(out.read_text("utf-8"))

    def hold_lock(self, root):
        script = self.script("v520_s4_lock.py", LOCK_HOLDER_SOURCE)
        tag = uuid.uuid4().hex[:8]
        signal = self.workspace / "lock-held-{}".format(tag)
        release = self.workspace / "lock-release-{}".format(tag)
        process = subprocess.Popen(
            [sys.executable, str(script), str(ENGINE), str(root),
             str(signal), str(release)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self._processes.append(process)
        deadline = time.monotonic() + 60
        while not signal.exists():
            if process.poll() is not None:
                self.fail("lock holder exited: " + process.communicate()[1])
            if time.monotonic() > deadline:
                self.fail("lock holder never took the lock")
            time.sleep(0.01)

        def let_go():
            release.write_text("go\n", encoding="utf-8")
            process.communicate(timeout=60)

        return let_go

    # ---- store readers -----------------------------------------------------

    def bdir(self, root=None):
        return Path(root or self.root) / ".bimri"

    def state(self, root=None):
        return json.loads((self.bdir(root) / "state.json").read_text("utf-8"))

    def state_bytes(self, root=None):
        return (self.bdir(root) / "state.json").read_bytes()

    def edit_state(self, mutate, root=None):
        path = self.bdir(root) / "state.json"
        state = json.loads(path.read_text("utf-8"))
        mutate(state)
        path.write_text(json.dumps(state, indent=2, sort_keys=True), "utf-8")

    def witness_path(self, root=None):
        return self.bdir(root) / "audit-witness.json"

    def witness(self, root=None):
        return json.loads(self.witness_path(root).read_text("utf-8"))

    def optional_bytes(self, path):
        return path.read_bytes() if path.is_file() else None

    def verdict_path(self, root=None):
        return self.bdir(root) / "audit-verdict.json"

    def verdict(self, root=None):
        path = self.verdict_path(root)
        if not path.is_file():
            return None
        return json.loads(path.read_text("utf-8"))

    def head_token(self, root=None):
        state = self.state(root)
        return "V{:06d}:head:{}".format(
            state["head_revision"], state["head_hash"]
        )

    def log_listing(self, root=None):
        directory = self.bdir(root) / "log"
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.iterdir())

    def open_tickets(self, root=None):
        directory = self.bdir(root) / "saves" / "open"
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.iterdir())

    def done_tickets(self, root=None):
        directory = self.bdir(root) / "saves" / "done"
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.iterdir())

    def lines(self, result):
        return result.stdout.splitlines()

    def observed_line(self, result):
        tokens = [
            OBSERVED_RE.match(line).group(1) for line in self.lines(result)
            if OBSERVED_RE.match(line)
        ]
        self.assertEqual(len(tokens), 1, result.stdout)
        return tokens[0]

    def cold_line(self, result):
        found = [line for line in self.lines(result)
                 if line.startswith("Cold current: ")]
        self.assertEqual(len(found), 1, result.stdout)
        return found[0]

    def run_core_state(self, root=None):
        state = self.state(root)
        return {
            name: state[name]
            for name in ("active_runs", "session_runs", "run_count",
                         "run_dates")
        }

    def today_set(self):
        return {dt.date.today().isoformat()}

    def assert_read_brief(self, result, actor):
        """The D6.1 frame: rc 0, read header, END last, no run handle."""
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.lines(result)
        self.assertTrue(lines, result.stderr)
        header = READ_HEADER_RE.match(lines[0])
        self.assertIsNotNone(header, result.stdout)
        self.assertEqual(header.group(2), actor, lines[0])
        self.assertEqual(lines[-1], END_BRIEF, result.stdout)
        self.assertNotIn("BIMRI RUN HANDLE", result.stdout)
        self.assertNotRegex(result.stdout, r"(?m)^Run handle: ")
        return lines

    def assert_no_audit_copy(self, root=None):
        root = Path(root or self.root)
        leftovers = [
            path.name for path in root.iterdir()
            if path.name.startswith(".bimri-audit-copy-")
        ]
        self.assertEqual(leftovers, [])

    def plant_ticket(self, request_id, created_at, root=None):
        """An admitted D2.1 ticket in saves/open/ (no marker: D3.4.2);
        tests/test_v520_step7.py plant_ticket recipe."""
        state = self.state(root)
        head = state["head_revision"]
        run_id = "R{:06d}".format(state["run_count"] + 1)
        intent = {
            "operation": "set", "new_subject": True, "tier": 2,
            "key": "ticket.{}".format(request_id), "target": None,
            "kind": None, "importance": 3, "status": "active",
            "trust": "working", "source": "agent", "tags": [],
            "text": "Ticket intent.", "rationale": None, "confidence": None,
            "observations": None, "evidence": None, "falsifier": None,
            "settles": [], "observed": "V{:06d}:absent".format(head),
        }
        ticket = {
            "ticket_schema": 1,
            "bimri_version": "5.1.0",
            "engine_release": "5.2.1",
            "request_id": request_id,
            "actor": "ticket-agent",
            "status": "admitted",
            "created_at": created_at,
            "updated_at": created_at,
            "started_on": created_at[:10],
            "intent": intent,
            "intent_hash": audit_digest(intent),
            "observed": {
                "token": "V{:06d}:absent".format(head), "revision": head,
                "storage": "absent", "key_hash": "absent",
            },
            "run_id": run_id,
            "proposal_id": "{}-Q001".format(run_id),
            "entry_id": "{}-E001".format(run_id),
            "revision": head + 1,
            "archive_months": [created_at[:7]],
            "proposal_sha256": "0" * 64,
            "log_sha256": "0" * 64,
            "result": None,
            "finished_at": None,
        }
        ticket["ticket_hash"] = record_seal(ticket, "ticket_hash")
        directory = self.bdir(root) / "saves" / "open"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "{}.json".format(request_id)
        path.write_text(
            json.dumps(ticket, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        return path

    def prior_policy_store(self):
        """A store built and checkpointed by the frozen 5.1.2 engine."""
        frozen = frozen_release_engine(self.workspace)
        if frozen is None:
            self.skipTest("the frozen 5.1.2 engine is not in this workspace")
        root = self.workspace / "prior"
        started = self.cli(
            "start", "--actor", "prior", root=root, engine=frozen
        )
        run_id = RUN_RE.search(started.stdout).group(1)
        self.cli(
            "propose", "--run", run_id, "--tier", "2", "--key", "prior.key",
            "--text", "Prior value.", "--source", "agent",
            "--trust", "working", "--new-subject", root=root, engine=frozen,
        )
        self.cli("sync", "--run", run_id, root=root, engine=frozen)
        self.cli(
            "close", "--run", run_id, "--outcome", "success",
            "--summary", "prior", root=root, engine=frozen,
        )
        self.assertEqual(
            self.witness(root)["policy_version"], "5.1.2-authority-1"
        )
        return root

    def store_copy_clone(self):
        if not (STORE_COPY / ".bimri" / "state.json").is_file():
            self.skipTest("BIMRI_STORE_COPY does not name a store copy (F4)")
        root = self.workspace / "store-copy"
        root.mkdir()
        shutil.copy2(str(STORE_COPY / "bimri.md"), str(root / "bimri.md"))
        shutil.copytree(
            str(STORE_COPY / ".bimri"), str(root / ".bimri"),
            ignore=shutil.ignore_patterns(
                "engine.lock", "runtime.local.json", "hooks.claude.local.json"
            ),
        )
        (root / ".bimri" / "engine.lock").write_bytes(b"")
        return root

    # ========================================================================
    # Gate 1: reads create zero runs
    # ========================================================================

    def test_two_thousand_read_starts_create_no_runs(self):
        # D1.1 / D6.1 / gate 1: 1,800 in-process + 200 sequential subprocess
        # + 24 concurrent read starts open nothing and all observe one head.
        token = self.head_token()
        state_before = self.state_bytes()
        witness_before = self.witness_path().read_bytes()
        logs_before = self.log_listing()
        core_before = self.run_core_state()
        snapshot_before = protected_tree_snapshot(self.root)

        summary = self.loop(1800, "start", "--actor", "reader")
        self.assertEqual(summary["codes"], {"0": 1800}, summary)
        self.assertEqual(summary["observed"], [token], summary)
        self.assertEqual(summary["handles"], 0, summary)
        for header in summary["headers"]:
            self.assertIsNotNone(READ_HEADER_RE.match(header), header)
            self.assertTrue(header.endswith("| reader ==="), header)

        observed = []
        first = None
        for _ in range(200):
            result = self.cli("start", "--actor", "reader")
            self.assert_read_brief(result, "reader")
            observed.append(self.observed_line(result))
            first = first or result

        processes = [
            subprocess.Popen(
                [sys.executable, str(ENGINE), "--root", str(self.root),
                 "start", "--actor", "reader"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            for _ in range(24)
        ]
        self._processes.extend(processes)
        for process in processes:
            stdout, stderr = process.communicate(timeout=300)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertNotIn("BIMRI RUN HANDLE", stdout)
            match = re.search(r"(?m)^Observed: (\S+)$", stdout)
            self.assertIsNotNone(match, stdout)
            observed.append(match.group(1))

        self.assertEqual(len(observed), 224)
        self.assertEqual(set(observed), {token})
        revision = REVISION_RE.match(self.lines(first)[1])
        self.assertIsNotNone(revision, first.stdout)
        self.assertEqual(
            (int(revision.group(3)), int(revision.group(5)),
             int(revision.group(7))),
            F1_TIERS,
        )
        cold = COLD_LINE_RE.match(self.cold_line(first))
        self.assertIsNotNone(cold, first.stdout)
        self.assertEqual(cold.group(1), "1")
        self.assertEqual(self.run_core_state(), core_before)
        self.assertEqual(self.state_bytes(), state_before)
        self.assertEqual(self.witness_path().read_bytes(), witness_before)
        self.assertEqual(self.log_listing(), logs_before)
        self.assertEqual(protected_tree_snapshot(self.root), snapshot_before)
        listed = self.cli("cold")
        self.assertIn(F1_COLD_KEY, listed.stdout)

    def test_hook_start_prints_read_brief_for_any_payload(self):
        # D1.14 / gate 1: every payload (the six v512:352 cases and a
        # well-formed one) prints the read brief as claude-code, rc 0, empty
        # stderr, no [hook-identity-missing], store bytes unchanged.
        before = protected_tree_snapshot(self.root)
        for label, payload in HOOK_PAYLOADS:
            result = self.cli("hook-start", input_text=payload, check=False)
            self.assertEqual(result.returncode, 0, (label, result.stderr))
            self.assertTrue(
                result.stdout.startswith("=== BIMRI BRIEF read | "),
                (label, result.stdout),
            )
            self.assert_read_brief(result, "claude-code")
            self.assertEqual(result.stderr, "", label)
            self.assertNotIn("[hook-identity-missing]", result.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), before)

    def test_hook_start_initialises_a_missing_store_without_a_run(self):
        # D1.1 "a missing store is still initialized" / D1.14: hook-start on
        # a fresh folder creates the store and opens nothing.
        root = self.workspace / "fresh-hook"
        result = self.cli("hook-start", root=root, input_text="", check=False)
        self.assert_read_brief(result, "claude-code")
        state = self.state(root)
        self.assertEqual(state["active_runs"], {})
        self.assertEqual(state["session_runs"], {})
        self.assertEqual(state["run_count"], 0)
        self.assertEqual(self.log_listing(root), [])
        for _label, payload in HOOK_PAYLOADS:
            again = self.cli("hook-start", root=root, input_text=payload,
                             check=False)
            self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.state(root)["active_runs"], {})
        self.assertEqual(self.log_listing(root), [])

    def test_hook_close_is_a_noop_for_any_payload(self):
        # D1.15: with nothing mapped, hook-close returns 0 with empty stdout
        # for every payload and leaves the store byte-identical.
        before = protected_tree_snapshot(self.root)
        for label, payload in HOOK_PAYLOADS:
            closed = self.cli("hook-close", input_text=payload, check=False)
            self.assertEqual(closed.returncode, 0, (label, closed.stderr))
            self.assertEqual(closed.stdout, "", label)
        self.assertEqual(protected_tree_snapshot(self.root), before)

    def test_hook_close_without_a_store_creates_nothing(self):
        # D1.15 / section 01 5.2: no .bimri/state.json -> exit 0 before any
        # lock or layout; a close never creates a store.
        root = self.workspace / "empty-folder"
        root.mkdir()
        for label, payload in HOOK_PAYLOADS:
            closed = self.cli("hook-close", root=root, input_text=payload,
                              check=False)
            self.assertEqual(closed.returncode, 0, (label, closed.stderr))
            self.assertEqual(closed.stdout, "", label)
        self.assertEqual(sorted(path.name for path in root.iterdir()), [])

    def test_hook_close_without_state_returns_instantly_while_lock_is_held(self):
        # D1.15 "exits 0 before taking any lock": another process holds this
        # very folder's engine lock (no state.json); hook-close still returns
        # 0 in under a second instead of waiting out the 10 s lock timeout.
        root = self.workspace / "held-no-state"
        root.mkdir()
        let_go = self.hold_lock(root)
        try:
            self.assertFalse((root / ".bimri" / "state.json").exists())
            timings = []
            for _ in range(3):
                began = time.perf_counter()
                closed = self.cli(
                    "hook-close", root=root, check=False,
                    input_text=json.dumps({"session_id": "held-1"}),
                    timeout=60,
                )
                timings.append(time.perf_counter() - began)
                self.assertEqual(closed.returncode, 0, closed.stderr)
                self.assertEqual(closed.stdout, "")
                self.assertNotIn("busy", closed.stderr)
        finally:
            let_go()
        self.assertLess(min(timings), 1.0, timings)
        self.assertFalse((root / ".bimri" / "state.json").exists())

    def test_hook_start_exits_2_only_when_busy(self):
        # D1.14 "exit 2 only busy/error": a held lock makes hook-start exit 2
        # with the busy error and no brief.
        let_go = self.hold_lock(self.root)
        try:
            busy = self.cli("hook-start", input_text="", check=False,
                            timeout=120)
        finally:
            let_go()
        self.assertEqual(busy.returncode, 2, busy.stdout + busy.stderr)
        self.assertIn("busy", busy.stderr)
        self.assertNotIn("=== BIMRI BRIEF", busy.stdout)

    def test_abandoned_chats_leave_nothing(self):
        # Gate 1 (section 10.4): 50 chats killed with os._exit right after
        # the brief's first stdout line, interleaved with get, then a start:
        # no marker, no run, no log, the same token.
        token = self.head_token()
        state_before = self.state_bytes()
        logs_before = self.log_listing()
        script = self.script("v520_s4_first_line.py", FIRST_LINE_SOURCE)
        for _ in range(50):
            killed = subprocess.run(
                [sys.executable, str(script), str(ENGINE), "--root",
                 str(self.root), "start", "--actor", "abandoned"],
                text=True, capture_output=True, input="", timeout=120,
                encoding="utf-8",
            )
            self.assertEqual(killed.returncode, 0, killed.stderr)
            self.assertEqual(len(killed.stdout.splitlines()), 1, killed.stdout)
            self.assertIsNotNone(
                READ_HEADER_RE.match(killed.stdout.splitlines()[0]),
                killed.stdout,
            )
            got = self.cli("get", "--key", F1_AGENT_KEY)
            self.assertIn("Writer text.", got.stdout)
        after = self.cli("start", "--actor", "abandoned")
        self.assert_read_brief(after, "abandoned")
        self.assertEqual(self.observed_line(after), token)
        self.assertFalse((self.bdir() / "audit-transition.json").exists())
        self.assertEqual(self.log_listing(), logs_before)
        self.assertEqual(self.state_bytes(), state_before)
        self.assertEqual(self.state()["active_runs"], {})

    def test_read_brief_never_audits_a_store_without_a_checkpoint(self):
        # Gate 1 / section 06 1.2: with no witness, start and hook-start
        # under valid_witness_forbid_full_audit print
        # `Checkpoint: unproven; run doctor` and publish nothing; doctor
        # seeds and the next read is warm.
        self.witness_path().unlink()
        state_before = self.state_bytes()
        logs_before = self.log_listing()
        verdict_before = self.optional_bytes(self.verdict_path())
        for command in (("start", "--actor", "cold-reader"), ("hook-start",)):
            result = self.worker(
                "valid_witness_forbid_full_audit", *command, input_text=""
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("AssertionError", result.stderr)
            lines = self.lines(result)
            self.assertIsNotNone(READ_HEADER_RE.match(lines[0]), result.stdout)
            self.assertIn(CHECKPOINT_UNPROVEN, lines)
            observed = [i for i, line in enumerate(lines)
                        if line.startswith("Observed: ")]
            self.assertEqual(
                lines[observed[0] + 1], CHECKPOINT_UNPROVEN, result.stdout
            )
            self.assertNotIn(BANNER, result.stdout)
            self.assertFalse(self.witness_path().exists())
            self.assertEqual(
                self.optional_bytes(self.verdict_path()), verdict_before
            )
            self.assertEqual(self.state_bytes(), state_before)
            self.assertEqual(self.log_listing(), logs_before)
        self.cli("doctor", timeout=600)
        self.assertTrue(self.witness_path().is_file())
        warm = self.worker(
            "valid_witness_forbid_full_audit", "start", "--actor", "warm"
        )
        self.assertEqual(warm.returncode, 0, warm.stdout + warm.stderr)
        self.assertNotRegex(warm.stdout, r"(?m)^Checkpoint: ")

    def test_read_brief_never_audits_a_condemned_store(self):
        # Gate 1 / section 06 1.2 (JJ's 2 Sep rule): a condemned checkpoint
        # is shown from the record as AUTHORITY RECOVERY NEEDED, never
        # re-audited; nothing is published or written.
        epoch = self.witness()["audit_epoch"]
        self.edit_state(lambda state: state.update(_audit_epoch=epoch + 1))
        state_before = self.state_bytes()
        witness_before = self.witness_path().read_bytes()
        verdict_before = self.optional_bytes(self.verdict_path())
        logs_before = self.log_listing()
        for command in (("start", "--actor", "condemned"), ("hook-start",)):
            result = self.worker(
                "valid_witness_forbid_full_audit", *command, input_text=""
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("AssertionError", result.stderr)
            self.assertIn(BANNER, result.stdout)
            self.assertIn(
                "audit checkpoint condemned at epoch {} (witness epoch {})"
                .format(epoch + 1, epoch),
                result.stdout,
            )
            self.assertNotRegex(result.stdout, r"(?m)^Checkpoint: ")
            self.assertEqual(self.lines(result)[-1], END_BRIEF)
            self.assertEqual(self.witness_path().read_bytes(), witness_before)
            self.assertEqual(
                self.optional_bytes(self.verdict_path()), verdict_before
            )
            self.assertEqual(self.state_bytes(), state_before)
            self.assertEqual(self.log_listing(), logs_before)

    def test_read_brief_never_audits_a_prior_policy_store(self):
        # Gate 1 / D0.5 / section 05 1.4 item 4: a 5.1.2 witness reads as
        # `Checkpoint: prior policy; run doctor`; state stays 5.1.0 (D0.2)
        # and nothing is published.
        root = self.prior_policy_store()
        before = protected_tree_snapshot(root)
        state_before = self.state_bytes(root)
        witness_before = self.witness_path(root).read_bytes()
        logs_before = self.log_listing(root)
        for command in (("start", "--actor", "prior-reader"), ("hook-start",)):
            result = self.worker(
                "valid_witness_forbid_full_audit", *command, root=root,
                input_text="",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("AssertionError", result.stderr)
            self.assertIn(CHECKPOINT_PRIOR_POLICY, self.lines(result))
            self.assertNotIn(BANNER, result.stdout)
            self.assertEqual(self.witness_path(root).read_bytes(), witness_before)
            self.assertFalse(self.verdict_path(root).exists())
            self.assertEqual(self.state_bytes(root), state_before)
            self.assertEqual(self.log_listing(root), logs_before)
            self.assertEqual(changed_paths(before, protected_tree_snapshot(root)), [])
        self.assertEqual(self.state(root)["bimri_version"], "5.1.0")

        # A 5.1.2 store without a witness also remains byte-for-byte and
        # directory-for-directory unchanged on its first 5.2 reads.
        self.witness_path(root).unlink()
        cold_before = protected_tree_snapshot(root)
        for command in (("start",), ("hook-start",)):
            brief = self.cli(*command, root=root, input_text="{}")
            self.assertIn(CHECKPOINT_UNPROVEN, self.lines(brief))
            self.assertEqual(changed_paths(cold_before, protected_tree_snapshot(root)), [])

    def test_5_1_0_read_preserves_pre_manifest_layout(self):
        old_engine = public_release_engine(self.workspace, "2b34cbc")
        self.assertIsNotNone(old_engine, "checkout must include public 5.1.0 history")
        root = self.workspace / "pre-manifest"
        started = self.cli("start", "--actor", "historical", root=root, engine=old_engine)
        self.cli("close", "--run", RUN_RE.search(started.stdout).group(1),
                 "--summary", "finished", root=root, engine=old_engine)
        self.assertFalse((root / ".bimri" / "audit-manifests").exists())
        before = protected_tree_snapshot(root)
        for command in (("start",), ("hook-start",)):
            brief = self.cli(*command, root=root, input_text="{}")
            self.assertIn("=== BIMRI BRIEF read", brief.stdout)
            self.assertEqual(changed_paths(before, protected_tree_snapshot(root)), [])

    def test_read_brief_on_cold_and_condemned_stores_returns_fast(self):
        # Section 06 attack revision (gate 1): 200 starts and 200 hook-starts
        # on a store with no witness and on a condemned one each return 0 in
        # under 3 s with the recorded line, reaching no audit, writing
        # nothing and leaving no audit copy. In-process loop; one timed
        # subprocess hook-start per store bounds the wall time too.
        cold = self.workspace / "cold-store"
        copy_store(self.template, cold)
        (cold / ".bimri" / "audit-witness.json").unlink()
        condemned = self.workspace / "condemned-store"
        copy_store(self.template, condemned)
        epoch = self.witness(condemned)["audit_epoch"]
        self.edit_state(
            lambda state: state.update(_audit_epoch=epoch + 1), root=condemned
        )
        for root, expect_banner in ((cold, False), (condemned, True)):
            before = protected_tree_snapshot(root)
            for arguments in (("start", "--actor", "fast"), ("hook-start",)):
                summary = self.loop(200, *arguments, root=root, forbid=True)
                self.assertEqual(summary["codes"], {"0": 200}, summary)
                self.assertEqual(summary["violations"], [], summary)
                self.assertLess(summary["slowest_ms"], 3000.0, summary)
                self.assertEqual(summary["handles"], 0, summary)
                if expect_banner:
                    self.assertEqual(summary["banners"], 200, summary)
                    self.assertEqual(summary["checkpoints"], [], summary)
                else:
                    self.assertEqual(summary["banners"], 0, summary)
                    self.assertEqual(
                        summary["checkpoints"], [CHECKPOINT_UNPROVEN], summary
                    )
            began = time.perf_counter()
            wall = self.cli("hook-start", root=root, input_text="")
            self.assertLess(time.perf_counter() - began, 3.0)
            self.assertIn("=== BIMRI BRIEF read", wall.stdout)
            self.assertEqual(protected_tree_snapshot(root), before)
            self.assert_no_audit_copy(root)

    def test_legacy_session_mapping_still_drains_through_hook_close(self):
        # D1.15 (JJ's 55 mappings): a `start --actor claude-code --open-run
        # --session S` run with nothing undecided is closed by hook-close
        # with payload {"session_id": S} and its mapping drains.
        run_id = self.open_run("claude-code", "--session", "legacy-s1")
        self.assertEqual(
            list(self.state()["session_runs"].values()), [run_id]
        )
        closed = self.cli(
            "hook-close",
            input_text=json.dumps({"session_id": "legacy-s1", "reason": "other"}),
        )
        self.assertIn("run {} closed".format(run_id), closed.stdout)
        state = self.state()
        self.assertNotIn(run_id, state["active_runs"])
        self.assertNotIn(run_id, state["session_runs"].values())
        log = (self.bdir() / "log" / "{}.md".format(run_id)).read_text("utf-8")
        self.assertIn("[CLOSED:", log)

    def test_hook_close_leaves_a_mapped_run_with_undecided_work(self):
        # D1.15 / section 01 5.2: a mapped legacy run that owns an undecided
        # proposal is left active and mapped by hook-close: rc 0, empty
        # stdout, no decision written, no [CLOSED:].
        run_id = self.open_run("claude-code", "--session", "legacy-s2")
        proposal = self.propose(run_id, "legacy.undecided", "Waits for JJ.")
        before = protected_tree_snapshot(self.root)
        closed = self.cli(
            "hook-close", input_text=json.dumps({"session_id": "legacy-s2"})
        )
        self.assertEqual(closed.stdout, "")
        state = self.state()
        self.assertIn(run_id, state["active_runs"])
        self.assertIn(run_id, state["session_runs"].values())
        self.assertFalse(
            (self.bdir() / "decisions" / "{}.json".format(proposal)).exists()
        )
        log = (self.bdir() / "log" / "{}.md".format(run_id)).read_text("utf-8")
        self.assertNotIn("[CLOSED:", log)
        self.assertEqual(protected_tree_snapshot(self.root), before)

    def test_brief_reports_ticket_only_save_and_does_not_finish_it(self):
        # D3.4.2 (Decisions 9): a save crashed after its ticket (121) is
        # named by start, hook-start and status, never finished by them or
        # by get; the next doctor finishes it.
        request_id = "test-brief-ticket-1"
        key = "f1.ticketed"
        got = self.cli("get", "--key", key, "--json", check=False)
        observed = json.loads(got.stdout)["token"]
        crashed = self.worker(
            "save_crash_after_ticket", "save", "--request", request_id,
            "--observed", observed, "--key", key, "--new-subject",
            "--tier", "2", "--text", "Admitted, never finished.",
        )
        self.assertEqual(crashed.returncode, 121, crashed.stdout + crashed.stderr)
        self.assertEqual(self.open_tickets(), [request_id + ".json"])
        witness_before = self.witness_path().read_bytes()
        line_re = re.compile(
            r"(?m)^Unfinished save: " + re.escape(request_id)
            + r" \(admitted [^)]+\); the next save or doctor finishes it$"
        )
        for command, payload in ((("start", "--actor", "a"), ""),
                                 (("hook-start",), "{}"),
                                 (("start", "--actor", "b"), "")):
            result = self.cli(*command, input_text=payload)
            self.assertRegex(result.stdout, line_re)
            lines = self.lines(result)
            index = [i for i, line in enumerate(lines)
                     if UNFINISHED_RE.match(line)][0]
            self.assertTrue(lines[index - 1].startswith("Cold current: "))
            self.assertTrue(lines[index + 1].startswith(SAVE_WITH_PREFIX))
        for _ in range(2):
            self.cli("get", "--key", F1_AGENT_KEY)
        status = self.cli("status", check=False)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertRegex(status.stdout, line_re)
        counts = [STATUS_COLD_RE.match(line) for line in self.lines(status)]
        counts = [match for match in counts if match]
        self.assertEqual(len(counts), 1, status.stdout)
        self.assertEqual(counts[0].group(3), "1")
        self.assertEqual(self.open_tickets(), [request_id + ".json"])
        self.assertEqual(self.witness_path().read_bytes(), witness_before)
        self.cli("doctor", timeout=600)
        self.assertEqual(self.open_tickets(), [])
        self.assertIn(request_id + ".json", self.done_tickets())

    def test_verdict_writers_follow_decisions_11(self):
        # Decisions 11 (section 10 gate 1 crossref): reads and the read brief
        # never write a verdict; the first `start --open-run` cold load seals
        # a live verdict, a warm one leaves it, doctor replaces it with copy.
        self.witness_path().unlink()
        if self.verdict_path().exists():
            self.verdict_path().unlink()
        for arguments in (
            ("review",),
            ("search", "--query", "agent", "--audit"),
            ("get", "--key", F1_AGENT_KEY, "--history", "--audit"),
            ("doctor", "--read-only"),
            ("start", "--actor", "reader"),
        ):
            result = self.cli(*arguments, check=False, timeout=600)
            self.assertIn(result.returncode, (0, 1, 3),
                          (arguments, result.stdout, result.stderr))
            self.assertFalse(self.verdict_path().exists(), arguments)
        self.open_run("legacy")
        verdict = self.verdict()
        self.assertIsNotNone(verdict)
        self.assertEqual(
            (verdict["source"], verdict["status"]), ("live", "passed")
        )
        self.assertIsNone(verdict["copy_capture_ms"])
        self.assertRegex(verdict["verdict_hash"], HASH_RE)
        sealed = self.verdict_path().read_bytes()
        self.open_run("legacy")
        self.assertEqual(self.verdict_path().read_bytes(), sealed)
        self.cli("doctor", timeout=600)
        self.assertEqual(self.verdict()["source"], "copy")

    # ========================================================================
    # D6.1: the exact lines
    # ========================================================================

    def test_read_brief_prints_d61_lines_in_order(self):
        # D6.1: header, revision line, Observed, Cold current, Save with,
        # END; nothing else on a warm store with no active run.
        result = self.cli("start", "--actor", "jett")
        lines = self.assert_read_brief(result, "jett")
        self.assertEqual(len(lines), 6, result.stdout)
        header = READ_HEADER_RE.match(lines[0])
        self.assertIn(header.group(1), self.today_set())
        revision = REVISION_RE.match(lines[1])
        self.assertIsNotNone(revision, result.stdout)
        self.assertEqual(int(revision.group(1)), self.state()["head_revision"])
        self.assertEqual(lines[2], "Observed: " + self.head_token())
        self.assertIsNotNone(COLD_LINE_RE.match(lines[3]), result.stdout)
        self.assertTrue(lines[4].startswith(SAVE_WITH_PREFIX), result.stdout)
        self.assertIn(" save --request ", lines[4])
        self.assertIn("--observed", lines[4])
        self.assertEqual(lines[5], END_BRIEF)
        self.assertEqual(result.stderr, "")

    def test_read_brief_revision_line_matches_the_run_brief(self):
        # D6.1 / section 06 1.1 line 2: byte for byte the run brief's line.
        read = self.cli("start", "--actor", "jett")
        opened = self.cli("start", "--open-run", "--actor", "jett")
        run_lines = self.lines(opened)
        header = [i for i, line in enumerate(run_lines)
                  if RUN_HEADER_RE.match(line)][0]
        self.assertEqual(self.lines(read)[1], run_lines[header + 1])

    def test_cold_line_prints_count_and_command_never_keys(self):
        # D6.1 / section 06 1.1: the cold line is a count plus the rendered
        # `cold` command (fallback without a runtime binding); no key.
        result = self.cli("start", "--actor", "jett")
        self.assertEqual(
            self.cold_line(result),
            "Cold current: 1 subjects. List them: {} cold".format(
                FALLBACK_COMMAND
            ),
        )
        self.assertNotIn(F1_COLD_KEY, result.stdout)

    def test_zero_cold_prints_count_only(self):
        # D6.1: zero cold prints exactly `Cold current: 0 subjects.`
        root = self.workspace / "fresh-zero"
        result = self.cli("start", "--actor", "jett", root=root)
        self.assertEqual(self.cold_line(result), "Cold current: 0 subjects.")

    def test_cold_command_renders_the_runtime_binding_argv_prefix(self):
        # Section 06 1.4 render_engine_command: argv_prefix from
        # runtime.local.json, elements with whitespace or a double quote are
        # double-quoted with inner quotes doubled; --json carries the same.
        prefix = ['/opt/py"3/python', "/srv/x y/bimri-engine.py"]
        (self.bdir() / "runtime.local.json").write_text(
            json.dumps({
                "version": "5.2.1", "host_bound": True,
                "python_executable": prefix[0], "engine_path": prefix[1],
                "argv_prefix": prefix,
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rendered = '"/opt/py""3/python" "/srv/x y/bimri-engine.py" cold'
        result = self.cli("start", "--actor", "jett")
        self.assertEqual(
            self.cold_line(result),
            "Cold current: 1 subjects. List them: " + rendered,
        )
        payload = json.loads(self.cli("start", "--actor", "jett", "--json").stdout)
        self.assertEqual(payload["cold_command"], rendered)

    def test_cold_command_falls_back_when_the_runtime_binding_is_unusable(self):
        # Section 06 1.4: anything but an object with a 1-8 element list of
        # non-empty strings renders `python bimri-engine.py`.
        binding = self.bdir() / "runtime.local.json"
        expected = "Cold current: 1 subjects. List them: {} cold".format(
            FALLBACK_COMMAND
        )
        for label, content in (
            ("array", "[1, 2]"),
            ("not-json", "{not json"),
            ("empty-element", json.dumps({"argv_prefix": ["python", ""]})),
            ("nine-elements", json.dumps({"argv_prefix": ["a"] * 9})),
            ("not-a-list", json.dumps({"argv_prefix": "python x.py"})),
            ("empty-list", json.dumps({"argv_prefix": []})),
        ):
            binding.write_text(content, encoding="utf-8")
            result = self.cli("start", "--actor", "jett", check=False)
            self.assertEqual(result.returncode, 0, (label, result.stderr))
            self.assertEqual(self.cold_line(result), expected, label)

    def test_start_json_prints_the_d61_object(self):
        # D6.1 --json: one sorted-key line carrying the brief's values.
        result = self.cli("start", "--actor", "jett", "--json")
        text = result.stdout.strip()
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        payload = json.loads(text)
        self.assertEqual(
            text, json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        self.assertTrue(D61_JSON_KEYS <= set(payload), sorted(payload))
        state = self.state()
        self.assertEqual(payload["brief"], "read")
        self.assertEqual(payload["actor"], "jett")
        self.assertIn(payload["date"], self.today_set())
        self.assertEqual(payload["head_revision"], state["head_revision"])
        self.assertEqual(payload["head_hash"], state["head_hash"])
        self.assertEqual(payload["token"], self.head_token())
        self.assertEqual(
            payload["tiers"],
            {
                "1": [F1_TIERS[0], state["tier1_max"]],
                "2": [F1_TIERS[1], state["tier2_max"]],
                "3": [F1_TIERS[2], state["tier3_max"]],
            },
        )
        tsv = REVISION_RE.match(self.lines(self.cli("start", "--actor", "j"))[1])
        self.assertEqual(payload["tokens_estimate"], int(tsv.group(2)))
        self.assertEqual(payload["authority_issues"], [])
        self.assertEqual(payload["orphan_candidates"], [])
        self.assertEqual(payload["validation_errors"], [])
        self.assertEqual(payload["cold_count"], 1)
        self.assertEqual(payload["cold_command"], FALLBACK_COMMAND + " cold")
        self.assertIsNone(payload["unfinished_save"])
        self.assertIsNone(payload.get("checkpoint"))

    def test_start_json_with_open_run_is_an_argparse_error(self):
        # Section 01 local decision 1: --open-run with --json is a parser
        # error (exit 2) that writes nothing.
        before = protected_tree_snapshot(self.root)
        result = self.cli("start", "--open-run", "--json", "--actor", "x",
                          check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn("BIMRI RUN HANDLE", result.stdout)
        self.assertEqual(protected_tree_snapshot(self.root), before)

    def test_open_run_brief_inserts_observed_and_cold_lines(self):
        # D6.1: `start --open-run` is the 5.1.2 run brief plus `Observed:`
        # after the revision line and `Cold current:` before `Run handle:`;
        # no `Save with:`; the run is allocated.
        result = self.cli("start", "--open-run", "--actor", "legacy")
        lines = self.lines(result)
        handle = RUN_RE.match(lines[0])
        self.assertIsNotNone(handle, result.stdout)
        run_id = handle.group(1)
        header = RUN_HEADER_RE.match(lines[1])
        self.assertIsNotNone(header, result.stdout)
        self.assertEqual(header.group(1), run_id)
        self.assertIsNotNone(REVISION_RE.match(lines[2]), result.stdout)
        self.assertEqual(lines[3], "Observed: " + self.head_token())
        run_line = [i for i, line in enumerate(lines)
                    if line.startswith("Run handle: {}.".format(run_id))]
        self.assertEqual(len(run_line), 1, result.stdout)
        self.assertEqual(
            lines[run_line[0] - 1],
            "Cold current: 1 subjects. List them: {} cold".format(
                FALLBACK_COMMAND
            ),
        )
        self.assertEqual(lines[-1], END_BRIEF)
        self.assertNotRegex(result.stdout, r"(?m)^Save with: ")
        self.assertIn(run_id, self.state()["active_runs"])

    def test_open_run_with_session_resumes_the_mapped_run(self):
        # D1.1: `--open-run` is v5.1.2 cmd_start byte for byte, session
        # resume included.
        first = self.open_run("claude-code", "--session", "resume-1")
        count = self.state()["run_count"]
        again = self.cli(
            "start", "--open-run", "--actor", "claude-code",
            "--session", "resume-1",
        )
        self.assertIn(
            "Resumed existing run handle for this harness session.",
            again.stdout,
        )
        self.assertRegex(
            again.stdout,
            r"(?m)^=== BIMRI BRIEF {} \| ".format(re.escape(first)),
        )
        self.assertEqual(self.state()["run_count"], count)

    def test_session_without_open_run_is_accepted_and_ignored(self):
        # D1.1: `start --session S` without --open-run prints the read brief
        # and maps nothing; a later hook-close for S finds nothing.
        before = protected_tree_snapshot(self.root)
        result = self.cli("start", "--actor", "claude-code",
                          "--session", "ignored-1")
        self.assert_read_brief(result, "claude-code")
        self.assertEqual(protected_tree_snapshot(self.root), before)
        closed = self.cli(
            "hook-close", input_text=json.dumps({"session_id": "ignored-1"})
        )
        self.assertEqual(closed.stdout, "")

    def test_read_start_initialises_a_missing_store_without_a_run(self):
        # D1.1: a missing store is still initialised by a plain start, which
        # allocates no run, maps no session and writes no log.
        root = self.workspace / "fresh-start"
        result = self.cli("start", "--actor", "jett", root=root)
        self.assert_read_brief(result, "jett")
        state = self.state(root)
        self.assertEqual(state["active_runs"], {})
        self.assertEqual(state["session_runs"], {})
        self.assertEqual(state["run_count"], 0)
        self.assertEqual(self.log_listing(root), [])
        self.assertEqual(self.observed_line(result), self.head_token(root))

    def test_read_brief_lists_orphan_candidates(self):
        # D6.1 / section 06 1.1: the orphan block renders
        # orphan_candidates(state, None) - no current run is excluded.
        run_id = self.open_run("idle")

        def age(state):
            state["active_runs"][run_id]["started_at"] = stamp(48)
            state["active_runs"][run_id]["last_activity_at"] = stamp(48)

        self.edit_state(age)
        state_before = self.state_bytes()
        result = self.cli("start", "--actor", "jett")
        self.assert_read_brief(result, "jett")
        orphan = ORPHAN_RE.search(result.stdout)
        self.assertIsNotNone(orphan, result.stdout)
        self.assertIn(run_id, orphan.group(1))
        self.assertEqual(self.state_bytes(), state_before)
        self.assertIn(run_id, self.state()["active_runs"])

    def test_read_brief_lists_unfinished_saves_sorted_at_most_five(self):
        # D3.4.2 / D6.1: one line per open ticket, sorted by name, at most
        # five, after `Cold current:` and before `Save with:`; reads leave
        # every ticket in open/.
        names = ["t-a", "t-b", "t-c", "t-d", "t-e", "t-f"]
        for index, name in enumerate(reversed(names)):
            self.plant_ticket(
                name, "2026-10-15T09:0{}:00Z".format(index)
            )
        result = self.cli("start", "--actor", "jett")
        lines = self.assert_read_brief(result, "jett")
        found = [(i, UNFINISHED_RE.match(line)) for i, line in enumerate(lines)
                 if UNFINISHED_RE.match(line)]
        self.assertEqual([match.group(1) for _i, match in found], names[:5])
        cold_index = [i for i, line in enumerate(lines)
                      if line.startswith("Cold current: ")][0]
        save_index = [i for i, line in enumerate(lines)
                      if line.startswith(SAVE_WITH_PREFIX)][0]
        self.assertEqual([i for i, _m in found],
                         list(range(cold_index + 1, save_index)))
        self.assertEqual(found[0][1].group(2), "2026-10-15T09:05:00Z")
        self.assertEqual(self.open_tickets(), [n + ".json" for n in names])

    def test_start_json_unfinished_save_is_the_oldest_ticket(self):
        # Section 06 1.4: `unfinished_save` is the oldest open ticket by
        # created_at (then id), not the first by name.
        self.plant_ticket("a-later", "2026-10-15T10:00:00Z")
        self.plant_ticket("b-earlier", "2026-10-15T09:00:00Z")
        payload = json.loads(
            self.cli("start", "--actor", "jett", "--json").stdout
        )
        self.assertEqual(
            payload["unfinished_save"],
            {"request_id": "b-earlier", "admitted_at": "2026-10-15T09:00:00Z"},
        )

    def test_read_brief_leaves_a_diverged_view_alone(self):
        # Section 06 1.2 read_brief_view_check: a hand-edited bimri.md is
        # not healed by a read; a notice goes to stderr; no authority write.
        hot = self.root / "bimri.md"
        edited = hot.read_bytes() + b"\nA hand edit.\n"
        hot.write_bytes(edited)
        state_before = self.state_bytes()
        witness_before = self.witness_path().read_bytes()
        result = self.cli("start", "--actor", "jett")
        self.assert_read_brief(result, "jett")
        self.assertIn(VIEW_DIFFERS_NOTICE, result.stderr)
        head = self.state()["head_revision"]
        relative = f".bimri/revisions/V{head:06d}.md"
        expected = "Read accepted memory from: " + relative
        self.assertIn(expected, result.stdout.splitlines())
        self.assertIn(expected, self.cli("hook-start", input_text="{}").stdout.splitlines())
        record = json.loads(self.cli("start", "--json").stdout)
        self.assertEqual(record["accepted_memory_path"], relative)
        accepted = (self.root / relative).read_bytes()
        self.assertEqual(hashlib.sha256(accepted).hexdigest(), record["head_hash"])
        self.assertNotIn(b"A hand edit.", accepted)
        self.assertEqual(hot.read_bytes(), edited)
        self.assertEqual(self.state_bytes(), state_before)
        self.assertEqual(self.witness_path().read_bytes(), witness_before)

    def test_read_brief_rewrites_a_missing_view_from_the_head(self):
        # Section 06 1.2: a missing bimri.md is rewritten from the head (the
        # one write a read brief may make; no authority write).
        hot = self.root / "bimri.md"
        original = hot.read_bytes()
        hot.unlink()
        state_before = self.state_bytes()
        witness_before = self.witness_path().read_bytes()
        result = self.cli("start", "--actor", "jett")
        self.assert_read_brief(result, "jett")
        self.assertEqual(hot.read_bytes(), original)
        self.assertEqual(self.state_bytes(), state_before)
        self.assertEqual(self.witness_path().read_bytes(), witness_before)

    def test_journal_on_an_inactive_run_names_the_open_run_lane(self):
        # D1.13: journal against a run that is not active says
        # `journal needs an active legacy run (start --open-run); ...`.
        before = protected_tree_snapshot(self.root)
        for run_id in ("R000001", "R999999"):
            result = self.cli("journal", "--run", run_id, "--text", "x",
                              check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(JOURNAL_NOT_ACTIVE, result.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), before)

    # ========================================================================
    # D8.1-D8.3: the docs land in the same commit
    # ========================================================================

    def test_agent_block_carries_the_d81_protocol(self):
        # D8.1: header line, `start --actor` read, `Observed:`, `--observed`,
        # the six results, search and cold before saving, --settles, the
        # optional legacy lane with --abandon.
        block = (REPOSITORY / "BIMRI-AGENT-BLOCK.md").read_text("utf-8")
        self.assertEqual(block.splitlines()[0], AGENT_BLOCK_HEADER)
        flat = normalized(block)
        for phrase in (
            "bimri-engine.py start --actor <your-agent-name>",
            "=== BIMRI BRIEF read",
            "Observed:",
            "save --request <id> --observed <token>",
            "`accepted` or `unchanged`",
            "`held`",
            "`stale`",
            "`conflict`",
            "`failed`",
            "search --query",
            "cold --prefix",
            "--settles <held-proposal-id>",
            "start --open-run --actor <you>",
            "--abandon",
            "<!-- BIMRI:RUNTIME-BINDING:START -->",
            "<!-- BIMRI:RUNTIME-BINDING:END -->",
        ):
            self.assertIn(phrase, flat, phrase)
        self.assertNotIn("BIMRI RUN HANDLE", block)

    def test_agents_md_is_the_agent_block(self):
        agents = (REPOSITORY / "AGENTS.md").read_text("utf-8")
        block = (REPOSITORY / "BIMRI-AGENT-BLOCK.md").read_text("utf-8")
        self.assertEqual(
            agents, "# AGENTS.md\n\n<!-- BIMRI:START -->\n"
            + block.strip() + "\n<!-- BIMRI:END -->\n"
        )

    def test_install_and_claude_docs_describe_the_read_brief_hooks(self):
        # D8.2 / section 08 8.5.4-8.5.5: INSTALL's smoke test expects the
        # read brief and retires [hook-identity-missing]; CLAUDE.md names
        # v5.2.0 and no longer asks to confirm a test run closes.
        install = (REPOSITORY / "INSTALL.md").read_text("utf-8")
        self.assertIn("=== BIMRI BRIEF read", install)
        self.assertNotIn("[hook-identity-missing]", install)
        claude = normalized((REPOSITORY / "CLAUDE.md").read_text("utf-8"))
        self.assertIn("Use BIMRI engine v5.2.1", claude)
        self.assertNotIn("v5.1.2", claude)
        self.assertNotIn("confirm the test run closes", claude)
        self.assertNotIn("allocate or resume the correct run", claude)

    def test_protocol_states_no_degraded_run_is_opened_by_start(self):
        # D8.3: section 9.5 of BIMRI-PROTOCOL.md carries the sentence.
        protocol = (REPOSITORY / "BIMRI-PROTOCOL.md").read_text("utf-8")
        section = protocol.split("### 9.5", 1)[1].split("\n## ", 1)[0]
        self.assertRegex(
            normalized(section), r"no degraded run is opened by `?start`?"
        )

    def test_installer_claude_block_names_start_and_save(self):
        target = self.workspace / "install-target"
        target.mkdir()
        installed = subprocess.run(
            [sys.executable, str(ENGINE), "install", "--target", str(target)],
            cwd=str(self.workspace), text=True, capture_output=True,
            timeout=300, encoding="utf-8",
        )
        self.assertEqual(
            installed.returncode, 0, installed.stdout + installed.stderr
        )
        claude = (target / "CLAUDE.md").read_text("utf-8")
        block = claude.split("<!-- BIMRI:START -->", 1)[1].split(
            "<!-- BIMRI:END -->", 1
        )[0]
        self.assertRegex(normalized(block), CHAT_SENTENCE_RE)

    def test_install_smoke_test_reads_a_brief_and_closes_nothing(self):
        # Section 01 5.3 (INSTALL smoke test): on a fresh install hook-start
        # prints the read brief with no run handle, hook-close exits 0 with
        # empty stdout, doctor passes; no run exists.
        target = self.workspace / "smoke-target"
        target.mkdir()
        installed = subprocess.run(
            [sys.executable, str(ENGINE), "install", "--target", str(target)],
            cwd=str(self.workspace), text=True, capture_output=True,
            timeout=300, encoding="utf-8",
        )
        self.assertEqual(
            installed.returncode, 0, installed.stdout + installed.stderr
        )
        engine = target / "bimri-engine.py"
        payload = json.dumps({"session_id": "smoke-1"})
        started = self.cli("hook-start", root=target, engine=engine,
                           input_text=payload)
        self.assertIn("=== BIMRI BRIEF read", started.stdout)
        self.assertIn(END_BRIEF, started.stdout)
        self.assertNotIn("BIMRI RUN HANDLE", started.stdout)
        closed = self.cli("hook-close", root=target, engine=engine,
                          input_text=payload)
        self.assertEqual(closed.stdout, "")
        doctor = self.cli("doctor", root=target, engine=engine, timeout=600)
        self.assertIn("PASSED", doctor.stdout)
        self.assertEqual(self.state(target)["active_runs"], {})

    def test_hooks_example_is_unchanged(self):
        # D8.2: hooks-example.json is unchanged from the 5.1.2 release.
        shown = subprocess.run(
            ["git", "-c", "safe.directory={}".format(REPOSITORY.as_posix()),
             "show", "{}:hooks-example.json".format(PRIOR_RELEASE_COMMIT)],
            cwd=str(REPOSITORY), capture_output=True, timeout=60,
        )
        if shown.returncode != 0:
            self.skipTest("release commit {} unavailable".format(
                PRIOR_RELEASE_COMMIT))
        current = (REPOSITORY / "hooks-example.json").read_bytes()
        self.assertEqual(
            current.replace(b"\r\n", b"\n"),
            shown.stdout.replace(b"\r\n", b"\n"),
        )

    # ========================================================================
    # F4 (JJ's store copy; skipped without BIMRI_STORE_COPY)
    # ========================================================================

    def test_store_copy_warm_hook_start_is_fast_and_counts_cold(self):
        # Step 4 acceptance: on a warm store-copy clone hook-start with
        # empty stdin prints the brief in under a second and the cold line
        # reads `Cold current: 190 subjects.`
        root = self.store_copy_clone()
        self.cli("doctor", root=root, check=False, timeout=900)
        timings = []
        result = None
        for _ in range(3):
            began = time.perf_counter()
            result = self.cli("hook-start", root=root, input_text="")
            timings.append(time.perf_counter() - began)
        self.assertLess(min(timings), 1.0, timings)
        self.assertIn("=== BIMRI BRIEF read", result.stdout)
        self.assertTrue(
            self.cold_line(result).startswith("Cold current: 190 subjects."),
            self.cold_line(result),
        )

    def test_store_copy_cold_hook_start_returns_inside_the_hook_timeout(self):
        # Section 06 attack revision: on a fresh F4 clone with the witness
        # deleted hook-start returns under 10 s wall and publishes nothing.
        root = self.store_copy_clone()
        witness = root / ".bimri" / "audit-witness.json"
        if witness.exists():
            witness.unlink()
        before = protected_tree_snapshot(root)
        began = time.perf_counter()
        result = self.cli("hook-start", root=root, input_text="", timeout=120)
        self.assertLess(time.perf_counter() - began, 10.0)
        self.assertIn("=== BIMRI BRIEF read", result.stdout)
        self.assertFalse(witness.exists())
        self.assertEqual(changed_paths(before, protected_tree_snapshot(root)), [])


if __name__ == "__main__":
    unittest.main()
