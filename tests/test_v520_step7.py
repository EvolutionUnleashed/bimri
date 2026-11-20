"""BIMRI v5.2 build step 7: the `status` quick path.

Spine: design/DECISIONS.md D1.6, D3.4.2, D5.1, D5.2 (Decisions 4, 6, 13-17,
34); detail from design/DESIGN.md section 01 3.6, section 05 1.1-1.8 and
section 10 gates 6-7 (`test_quick_status_reads_no_history`,
`test_status_exit_codes_follow_recorded_conditions`).

Process model (section 10.0): every behaviour is driven through the public
command boundary in a separate process. The history-scan seam
`status_forbid_history_scan` is exercised two ways:

- a probe written into each test's temp workspace (``PROBE_SOURCE``) that
  imports the engine by path, replaces the D5.1 / section 05 1.7 forbidden
  functions with ones that raise ``AssertionError``, refuses any open of a
  ``.bimri/log/*.md`` file and records every file the engine opens under
  ``.bimri/``; it patches module attributes and the open entry points only,
  the same rule as ``tests/crash_worker.py``;
- the crash-worker mode of the same name, when the worker carries it.

Save tickets (D2.1) are planted by hand because the `save` command is built
in a different step; the planted ticket carries the D2.1 field set and its
``ticket_hash`` seal (``audit_record_seal`` shape). Tests that need the
`save` command or its crash modes skip when the engine lacks them.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import uuid
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"
PRIOR_RELEASE_COMMIT = "2c936ec"
BUILD_ROOT = Path(__file__).resolve().parents[3]
REDTEAM_STORE_300 = BUILD_ROOT / "redteam" / "store-medium-300"
# Section 10.2 F4: JJ's store copy, only when the environment names it.
STORE_COPY = Path(os.environ.get("BIMRI_STORE_COPY", "") or os.devnull)

RUN_RE = re.compile(r"BIMRI RUN HANDLE: (R\d{6})")
PROPOSAL_RE = re.compile(r"\bR\d{6}-Q\d{3}\b")

# D5.1 / section 05 1.3 line grammar.
HEADER_RE = re.compile(
    r"^BIMRI engine v5\.2\.0 \| memory format v5\.1\.0 \| "
    r"state v(\d+\.\d+\.\d+) \| revision V(\d{6}) \| runs (\d+) \| "
    r"active (\d+)$"
)
LEGACY_RE = re.compile(
    r"^Legacy active runs: (\d+) \(list them: status --runs\)$"
)
HOT_RE = re.compile(
    r"^Hot: ~(\d+) tokens \| bytes (\d+)/(\d+) hard ceiling \| "
    r"T1 (\d+) \(soft target (\d+)\) \| T2 (\d+) \(soft target (\d+)\) \| "
    r"T3 (\d+) \(soft target (\d+)\)$"
)
COLD_RE = re.compile(
    r"^Cold current: (\d+) \| Held candidates: (\d+|\?) \| "
    r"Unfinished saves: (\d+)$"
)
HELD_RE = re.compile(r"^  held (R\d{6}-Q\d{3}) \[([^\]]*)\] (.+)$")
CONFLICTS_RE = re.compile(
    r"^Open conflicts: (\d+|\?) \| proposals: (\d+) \| revisions: (\d+)$"
)
CHECKPOINT_VALID_RE = re.compile(
    r"^Checkpoint: valid at V(\d{6}) epoch (\d+) \| deep audit (\S+) "
    r"(PASSED|FAILED)(?: as of V(\d{6}))? \| (\d+) scoped publications since$"
)
CHECKPOINT_UNPROVEN = "Checkpoint: unproven; run doctor"
CHECKPOINT_PRIOR_POLICY = "Checkpoint: prior policy; run doctor"
UNFINISHED_RE = re.compile(
    r"^Unfinished save: (\S+) \(admitted (\S+)\); "
    r"the next save or doctor finishes it$"
)
RUN_ROW_RE = re.compile(
    r"^(R\d{6})\t([^\t]+)\tlast (\S+)\tproposals (\d+|\?)\t"
    r"undecided (\d+|\?)$"
)
BANNER = "AUTHORITY RECOVERY NEEDED"
VIEW_NOTICE = "BIMRI NOTICE: generated view could not be refreshed:"
STATUS_HELD_DEFAULT_MESSAGE = "Candidate preserved; current unchanged."
CONFIRMED_HOLD_MESSAGE = (
    "Current confirmed truth is unchanged. A direct owner statement may "
    "update or close this subject."
)
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

PROTECTED_EXCLUSIONS = {
    ".bimri/engine.lock",
    ".bimri/runtime.local.json",
    ".bimri/hooks.claude.local.json",
}

# Section 05 1.7 (the union of its list and section 10.1.4's): status must
# never reach any of these, and never open a run log.
FORBIDDEN_FUNCTIONS = (
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
)

PROBE_SOURCE = r'''
import builtins
import importlib.util
import io
import json
import os
import pathlib
import sys
from pathlib import Path


def main():
    engine_path, root, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    forbidden = json.loads(sys.argv[4])
    argv = sys.argv[5:]
    spec = importlib.util.spec_from_file_location(
        "bimri_v520_s7_probe_target", engine_path
    )
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    bdir = os.path.normcase(os.path.abspath(os.path.join(root, ".bimri")))
    logs = os.path.join(bdir, "log")
    record = {"violations": [], "opened": []}

    def relative(target):
        try:
            text = os.fspath(target)
        except TypeError:
            return None
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        full = os.path.normcase(os.path.abspath(text))
        if full == bdir or full.startswith(bdir + os.sep):
            return full
        return None

    def check(target, flags_or_mode):
        full = relative(target)
        if full is None:
            return
        reading = True
        if isinstance(flags_or_mode, str):
            reading = "r" in flags_or_mode and "+" not in flags_or_mode
        elif isinstance(flags_or_mode, int):
            reading = (flags_or_mode & (os.O_WRONLY | os.O_RDWR)) == 0
        if reading:
            record["opened"].append(
                os.path.relpath(full, bdir).replace(os.sep, "/")
            )
        if (
            os.path.dirname(full) == os.path.normcase(logs)
            and full.endswith(".md")
        ):
            message = "status opened a run log: " + os.path.basename(full)
            record["violations"].append(message)
            raise AssertionError(message)

    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open

    def guarded_open(file, mode="r", *args, **kwargs):
        check(file, mode)
        return real_open(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        check(file, mode)
        return real_io_open(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        check(path, flags)
        return real_os_open(path, flags, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_io_open
    os.open = guarded_os_open
    for name in forbidden:
        if not hasattr(engine, name):
            continue

        def make(label):
            def reject(*_args, **_kwargs):
                message = "status reached forbidden " + label
                record["violations"].append(message)
                raise AssertionError(message)
            return reject

        setattr(engine, name, make(name))
    code = 2
    try:
        code = engine.main(argv)
    finally:
        builtins.open = real_open
        io.open = real_io_open
        os.open = real_os_open
        Path(out_path).write_text(json.dumps(record), encoding="utf-8")
        sys.stdout.flush()
        sys.stderr.flush()
    sys.exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()
'''


# Holds the engine lock of one store until a release file appears (or 60 s).
LOCK_HOLDER_SOURCE = r"""
import importlib.util
import sys
import time
from pathlib import Path

engine_path, root, signal, release = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("bimri_v520_s7_lock", engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
with engine.engine_lock(engine.Paths(Path(root))):
    Path(signal).write_text("held\n", encoding="utf-8")
    deadline = time.monotonic() + 60
    while not Path(release).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""

# Wraps named engine functions with call counters, runs main(argv) and
# writes {name: calls} (section 05 1.7: the finisher's scope proof).
COUNTER_PROBE_SOURCE = r"""
import importlib.util
import json
import sys
from pathlib import Path


def main():
    engine_path, out_path = sys.argv[1], sys.argv[2]
    names = json.loads(sys.argv[3])
    argv = sys.argv[4:]
    spec = importlib.util.spec_from_file_location(
        "bimri_v520_s7_counter", engine_path
    )
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    counts = {name: 0 for name in names}
    for name in names:
        original = getattr(engine, name, None)
        if original is None:
            continue

        def make(label, wrapped):
            def counted(*args, **kwargs):
                counts[label] += 1
                return wrapped(*args, **kwargs)
            return counted

        setattr(engine, name, make(name, original))
    code = 2
    try:
        code = engine.main(argv)
    finally:
        Path(out_path).write_text(json.dumps(counts), encoding="utf-8")
        sys.stdout.flush()
        sys.stderr.flush()
    sys.exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()
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

    visit(root / ".bimri")
    return records


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
    empty engine.lock."""
    shutil.copytree(
        str(source), str(destination),
        ignore=shutil.ignore_patterns("engine.lock", "runtime.local.json"),
    )
    (Path(destination) / ".bimri" / "engine.lock").write_bytes(b"")
    # Section 10.2 F4: host-only bindings are never carried into a copy.
    hooks = Path(destination) / ".bimri" / "hooks.claude.local.json"
    if hooks.exists():
        hooks.unlink()


class V520Step7StatusTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._class_temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s7-")
        cls.class_workspace = Path(cls._class_temp.name)
        cls._prior_release = None
        cls._help_cache = {}

    @classmethod
    def tearDownClass(cls):
        cls._class_temp.cleanup()

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s7-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"

    def tearDown(self):
        self._temp.cleanup()

    # ---- process helpers ---------------------------------------------------

    def cli(
        self, *arguments, root=None, engine=None, check=True, timeout=240,
    ):
        command = [
            sys.executable, str(engine or ENGINE),
            "--root", str(root or self.root), *map(str, arguments),
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def help_text(self, engine, *command):
        key = (str(engine), command)
        if key not in self._help_cache:
            result = subprocess.run(
                [sys.executable, str(engine), *command, "--help"],
                text=True, capture_output=True, timeout=60, encoding="utf-8",
            )
            type(self)._help_cache[key] = result.stdout + result.stderr
        return self._help_cache[key]

    def has_command(self, name):
        return re.search(
            r"\b" + re.escape(name) + r"\b", self.help_text(ENGINE)
        ) is not None and "invalid choice" not in self.help_text(ENGINE, name)

    def start_arguments(self, engine=None):
        engine = engine or ENGINE
        arguments = ["start"]
        if "--open-run" in self.help_text(engine, "start"):
            arguments.append("--open-run")
        return arguments

    def live_doctor(self, root=None, check=True):
        arguments = ["doctor"]
        if "--live" in self.help_text(ENGINE, "doctor"):
            arguments.append("--live")
        return self.cli(*arguments, root=root, check=check, timeout=600)

    def start(self, actor, root=None, engine=None):
        result = self.cli(
            *self.start_arguments(engine), "--actor", actor,
            root=root, engine=engine,
        )
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def propose(
        self, run_id, key, text, root=None, engine=None, new_subject=True,
        source="agent", trust="working",
    ):
        arguments = [
            "propose", "--run", run_id, "--tier", "2", "--key", key,
            "--text", text, "--source", source, "--trust", trust,
        ]
        if new_subject:
            arguments.append("--new-subject")
        result = self.cli(*arguments, root=root, engine=engine)
        match = PROPOSAL_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(0)

    def status(self, *flags, root=None, check=False):
        return self.cli("status", *flags, root=root, check=check)

    def worker(self, mode, *arguments, root=None, check=True, timeout=240):
        command = [
            sys.executable, str(CRASH_WORKER), str(ENGINE), mode,
            str(root or self.root), *map(str, arguments),
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )
        if check and result.returncode != 0:
            self.fail(
                f"fault worker failed: {command!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def worker_has_mode(self, mode):
        return f'"{mode}"' in CRASH_WORKER.read_text("utf-8")

    def probe_status(self, *flags, root=None, timeout=240):
        script = self.workspace / "v520_s7_probe.py"
        if not script.is_file():
            script.write_text(PROBE_SOURCE, encoding="utf-8")
        root = Path(root or self.root)
        out = self.workspace / f"probe-{uuid.uuid4().hex}.json"
        command = [
            sys.executable, str(script), str(ENGINE), str(root), str(out),
            json.dumps(list(FORBIDDEN_FUNCTIONS)),
            "--root", str(root), "status", *flags,
        ]
        began = time.monotonic()
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )
        elapsed = time.monotonic() - began
        self.assertTrue(out.is_file(), result.stdout + result.stderr)
        return result, json.loads(out.read_text("utf-8")), elapsed

    def load_engine(self):
        name = f"bimri_v520_s7_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(name, ENGINE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def prior_engine(self):
        cls = type(self)
        if cls._prior_release is None:
            archived = subprocess.run(
                ["git", "-c", f"safe.directory={REPOSITORY.as_posix()}",
                 "archive", "--format=tar", PRIOR_RELEASE_COMMIT],
                cwd=str(REPOSITORY), capture_output=True, timeout=60,
            )
            if archived.returncode != 0:
                self.skipTest(
                    f"release commit {PRIOR_RELEASE_COMMIT} unavailable"
                )
            destination = cls.class_workspace / "release-5.1.2"
            destination.mkdir()
            package_root = destination.resolve()
            with tarfile.open(
                fileobj=io.BytesIO(archived.stdout), mode="r:"
            ) as archive:
                for member in archive.getmembers():
                    target = (destination / member.name).resolve()
                    self.assertTrue(
                        target == package_root or package_root in target.parents,
                        member.name,
                    )
                archive.extractall(str(destination))
            engine = destination / "bimri-engine.py"
            self.assertIn(b'ENGINE_VERSION = "5.1.2"', engine.read_bytes())
            cls._prior_release = engine
        return cls._prior_release

    # ---- store readers -----------------------------------------------------

    def bdir(self, root=None):
        return Path(root or self.root) / ".bimri"

    def state(self, root=None):
        return json.loads((self.bdir(root) / "state.json").read_text("utf-8"))

    def edit_state(self, mutate, root=None):
        path = self.bdir(root) / "state.json"
        state = json.loads(path.read_text("utf-8"))
        mutate(state)
        path.write_text(json.dumps(state, indent=2, sort_keys=True), "utf-8")

    def witness_path(self, root=None):
        return self.bdir(root) / "audit-witness.json"

    def witness(self, root=None):
        return json.loads(self.witness_path(root).read_text("utf-8"))

    def verdict(self, root=None):
        path = self.bdir(root) / "audit-verdict.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text("utf-8"))

    def facts(self, root=None):
        witness = self.witness(root)
        path = (
            self.bdir(root) / "audit-manifests"
            / f"facts-{witness['facts_hash']}.json"
        )
        return json.loads(path.read_text("utf-8"))

    def head_text(self, root=None):
        state = self.state(root)
        return (
            self.bdir(root) / "revisions" / f"V{state['head_revision']:06d}.md"
        ).read_text("utf-8")

    def lines(self, result):
        return result.stdout.splitlines()

    def line_matching(self, result, pattern):
        for line in self.lines(result):
            match = pattern.match(line)
            if match:
                return match
        self.fail(f"no line matches {pattern.pattern!r}:\n{result.stdout}")

    def assert_no_traceback(self, result):
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        self.assertNotIn("AssertionError", result.stderr, result.stderr)

    # ---- store builders ----------------------------------------------------

    def seed_store(self, root=None):
        """Two active legacy runs, two accepted keys, a live checkpoint."""
        root = root or self.root
        run_a = self.start("seed-a", root=root)
        self.propose(run_a, "status.alpha", "Alpha value.", root=root)
        self.propose(run_a, "status.beta", "Beta value.", root=root)
        self.cli("sync", "--run", run_a, root=root)
        run_b = self.start("seed-b", root=root)
        undecided = self.propose(
            run_b, "status.gamma", "Gamma value waits.", root=root
        )
        self.live_doctor(root=root)
        self.assertTrue(self.witness_path(root).is_file())
        return run_a, run_b, undecided

    def seed_held_store(self, count=6):
        """`count` confirmed owner keys and one agent update per key, held
        with confirmed-user-authority-required."""
        owner = self.start("owner")
        keys = [f"held.key{index:02d}" for index in range(count)]
        for key in keys:
            self.propose(
                owner, key, f"Owner value for {key}.",
                source="user", trust="confirmed",
            )
        self.cli("sync", "--run", owner)
        agent = self.start("agent")
        held = []
        for key in keys:
            held.append(self.propose(
                agent, key, f"Agent rewrite of {key}.", new_subject=False,
            ))
        self.cli("sync", "--run", agent)
        for proposal_id in held:
            decision = json.loads(
                (self.bdir() / "decisions" / f"{proposal_id}.json")
                .read_text("utf-8")
            )
            self.assertEqual(decision["outcome"], "held", proposal_id)
        self.live_doctor()
        return dict(zip(held, keys))

    def plant_ticket(self, request_id, created_at, root=None):
        """An admitted D2.1 ticket in saves/open/ (no marker: D3.4.2)."""
        state = self.state(root)
        head = state["head_revision"]
        run_id = f"R{state['run_count'] + 1:06d}"
        intent = {
            "operation": "set", "new_subject": True, "tier": 2,
            "key": f"ticket.{request_id}", "target": None, "kind": None,
            "importance": 3, "status": "active", "trust": "working",
            "source": "agent", "tags": [], "text": "Ticket intent.",
            "rationale": None, "confidence": None, "observations": None,
            "evidence": None, "falsifier": None, "settles": [],
            "observed": f"V{head:06d}:absent",
        }
        ticket = {
            "ticket_schema": 1,
            "bimri_version": "5.1.0",
            "engine_release": "5.2.0",
            "request_id": request_id,
            "actor": "ticket-agent",
            "status": "admitted",
            "created_at": created_at,
            "updated_at": created_at,
            "started_on": created_at[:10],
            "intent": intent,
            "intent_hash": audit_digest(intent),
            "observed": {
                "token": f"V{head:06d}:absent", "revision": head,
                "storage": "absent", "key_hash": "absent",
            },
            "run_id": run_id,
            "proposal_id": f"{run_id}-Q001",
            "entry_id": f"{run_id}-E001",
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
        path = directory / f"{request_id}.json"
        path.write_text(
            json.dumps(ticket, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        return path

    def clone_store(self, name):
        """A byte copy of the seeded self.root for one more condition."""
        destination = self.workspace / name
        shutil.copytree(str(self.root), str(destination))
        return destination

    def store_copy_clone(self):
        if not (STORE_COPY / ".bimri" / "state.json").is_file():
            self.skipTest("BIMRI_STORE_COPY does not name a store copy (F4)")
        root = self.workspace / "store-copy"
        copy_store(STORE_COPY, root)
        return root

    def require_save(self, *modes):
        if not self.has_command("save"):
            self.skipTest("the engine has no save command yet (step 2)")
        for mode in modes:
            if not self.worker_has_mode(mode):
                self.skipTest(f"crash_worker lacks {mode}")

    def crash_save_after_log(self, request_id, key, root=None):
        """Section 10.1 mode 125: a new-subject save crashed at B4."""
        root = Path(root or self.root)
        token = f"V{self.state(root)['head_revision']:06d}:absent"
        result = self.worker(
            "save_crash_after_log", "save", "--request", request_id,
            "--observed", token, "--key", key, "--new-subject",
            "--tier", "2", "--text", "Saved before the crash.",
            root=root, check=False,
        )
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        ticket_path = self.bdir(root) / "saves" / "open" / f"{request_id}.json"
        self.assertTrue(ticket_path.is_file())
        self.assertTrue((self.bdir(root) / "audit-transition.json").is_file())
        return json.loads(ticket_path.read_text("utf-8"))

    def counter_status(self, names, *flags, root=None):
        script = self.workspace / "v520_s7_counter.py"
        if not script.is_file():
            script.write_text(COUNTER_PROBE_SOURCE, encoding="utf-8")
        root = Path(root or self.root)
        out = self.workspace / f"counter-{uuid.uuid4().hex}.json"
        result = subprocess.run(
            [sys.executable, str(script), str(ENGINE), str(out),
             json.dumps(list(names)), "--root", str(root), "status", *flags],
            text=True, capture_output=True, timeout=240, encoding="utf-8",
        )
        self.assertTrue(out.is_file(), result.stdout + result.stderr)
        return result, json.loads(out.read_text("utf-8"))

    # ========================================================================
    # D5.1: the exact lines
    # ========================================================================

    def test_status_warm_prints_d51_lines_in_order(self):
        # D5.1 / section 05 1.3: header, legacy line, Hot, Cold, Open
        # conflicts, Checkpoint, in that order and nothing unlisted.
        self.seed_store()
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sequence = [
            HEADER_RE, LEGACY_RE, HOT_RE, COLD_RE, CONFLICTS_RE,
            CHECKPOINT_VALID_RE,
        ]
        lines = self.lines(result)
        self.assertEqual(len(lines), len(sequence), result.stdout)
        for line, pattern in zip(lines, sequence):
            self.assertRegex(line, pattern)

    def test_status_header_and_hot_values_come_from_state_and_head(self):
        # D5.1 / section 05 1.2: header fields, Hot line from the head
        # revision text and state ceilings.
        self.seed_store()
        state = self.state()
        content = self.head_text()
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        header = self.line_matching(result, HEADER_RE)
        self.assertEqual(header.group(1), state["bimri_version"])
        self.assertEqual(int(header.group(2)), state["head_revision"])
        self.assertEqual(int(header.group(3)), state["run_count"])
        self.assertEqual(int(header.group(4)), len(state["active_runs"]))
        hot = self.line_matching(result, HOT_RE)
        self.assertEqual(int(hot.group(1)), len(content) // 4)
        self.assertEqual(int(hot.group(2)), len(content.encode("utf-8")))
        self.assertEqual(int(hot.group(3)), state["hot_max_bytes"])
        self.assertEqual(int(hot.group(5)), state["tier1_max"])
        self.assertEqual(int(hot.group(7)), state["tier2_max"])
        self.assertEqual(int(hot.group(9)), state["tier3_max"])

    def test_status_counts_come_from_facts_and_two_listings(self):
        # D5.1 / section 05 1.2: Cold from state, Held and Open conflicts
        # from the facts generation, proposals/revisions as name counts.
        self.seed_store()
        state = self.state()
        facts = self.facts()
        proposals = sum(
            1 for path in (self.bdir() / "proposals").iterdir()
            if PROPOSAL_RE.fullmatch(path.stem) and path.suffix == ".json"
        )
        revisions = sum(
            1 for path in (self.bdir() / "revisions").iterdir()
            if re.fullmatch(r"V\d{6}\.md", path.name)
        )
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cold = self.line_matching(result, COLD_RE)
        self.assertEqual(int(cold.group(1)), len(state.get("cold_current", {})))
        self.assertEqual(int(cold.group(2)), len(facts["held"]))
        self.assertEqual(int(cold.group(3)), 0)
        conflicts = self.line_matching(result, CONFLICTS_RE)
        self.assertEqual(int(conflicts.group(1)), len(facts["open_conflicts"]))
        self.assertEqual(int(conflicts.group(2)), proposals)
        self.assertEqual(int(conflicts.group(3)), revisions)

    def test_status_checkpoint_line_names_witness_and_verdict(self):
        # D5.1 / section 05 1.4 item 1 (L11): valid at head, witness epoch,
        # deep-audit stamp from the sealed verdict, scoped publications.
        self.seed_store()
        state = self.state()
        witness = self.witness()
        verdict = self.verdict()
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = self.line_matching(result, CHECKPOINT_VALID_RE)
        self.assertEqual(int(line.group(1)), state["head_revision"])
        self.assertEqual(int(line.group(2)), witness["audit_epoch"])
        self.assertEqual(int(line.group(6)), witness["scoped_publications"])
        if verdict is not None:
            self.assertEqual(line.group(3), verdict["created_at"])
            self.assertEqual(line.group(4), verdict["status"].upper())
            self.assertEqual(int(line.group(5)), verdict["head_revision"])
        else:
            self.assertEqual(line.group(3), witness["audited_at"])
            self.assertEqual(line.group(4), "PASSED")
            self.assertIsNone(line.group(5))

    def test_status_checkpoint_stamp_without_verdict_uses_audited_at(self):
        # Section 05 1.4 item 1 / L11: no sealed verdict -> W.audited_at
        # PASSED, and no as-of head is invented.
        self.seed_store()
        verdict_path = self.bdir() / "audit-verdict.json"
        if verdict_path.exists():
            verdict_path.unlink()
        witness = self.witness()
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = self.line_matching(result, CHECKPOINT_VALID_RE)
        self.assertEqual(line.group(3), witness["audited_at"])
        self.assertEqual(line.group(4), "PASSED")
        self.assertIsNone(line.group(5))

    def test_status_drops_review_outcomes_and_active_run_id_lines(self):
        # D5.2 (last sentence) / section 05 1.3: the review-classification,
        # Outcomes: and unbounded `Active runs:` lines leave status.
        run_a, run_b, _ = self.seed_store()
        self.cli(
            "close", "--run", run_a, "--outcome", "success",
            "--summary", "Seeded and closed.",
        )
        self.live_doctor()
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Actionable concurrent conflicts", result.stdout)
        self.assertNotIn("Satisfied historical candidates", result.stdout)
        self.assertNotIn("Outcomes:", result.stdout)
        self.assertNotRegex(result.stdout, r"(?m)^Active runs:")
        self.assertNotIn(run_b, result.stdout)

    def test_status_legacy_active_runs_line_only_when_runs_are_active(self):
        # Section 01 3.6: `Legacy active runs: N (list them: status --runs)`
        # appears only when active_runs is non-empty.
        run_a, run_b, _ = self.seed_store()
        line = self.line_matching(self.status(), LEGACY_RE)
        self.assertEqual(int(line.group(1)), 2)
        self.cli("sync", "--run", run_b)
        for run_id in (run_a, run_b):
            self.cli(
                "close", "--run", run_id, "--outcome", "success",
                "--summary", "Done.",
            )
        self.live_doctor()
        self.assertEqual(self.state()["active_runs"], {})
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Legacy active runs:", result.stdout)

    def test_status_held_lines_first_five_in_id_order_with_hold_message(self):
        # D5.1 / section 05 1.3: Held candidates counts F.held; at most five
        # `  held <id> [<key>] <HOLD_MESSAGES[reason]>` lines, id order.
        held = self.seed_held_store(count=6)
        facts = self.facts()
        self.assertEqual(sorted(facts["held"]), sorted(held))
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cold = self.line_matching(result, COLD_RE)
        self.assertEqual(cold.group(2), "6")
        rows = [HELD_RE.match(line) for line in self.lines(result)]
        rows = [row for row in rows if row]
        expected = sorted(held)[:5]
        self.assertEqual([row.group(1) for row in rows], expected)
        for row in rows:
            self.assertEqual(row.group(2), held[row.group(1)])
            self.assertEqual(row.group(3), CONFIRMED_HOLD_MESSAGE)

    def test_status_held_detail_reads_at_most_five_proposals(self):
        # Spine step 7 ("held detail lines read at most five proposal files
        # by id") / section 05 1.2: <= 5 proposals and <= 5 decisions opened.
        self.seed_held_store(count=7)
        result, record, _ = self.probe_status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record["violations"], [])
        proposals = {p for p in record["opened"] if p.startswith("proposals/")}
        decisions = {p for p in record["opened"] if p.startswith("decisions/")}
        self.assertLessEqual(len(proposals), 5, sorted(proposals))
        self.assertLessEqual(len(decisions), 5, sorted(decisions))

    def test_status_held_detail_survives_unreadable_proposal(self):
        # Section 05 1.2: a missing/unreadable held proposal prints
        # `  held <id> [?] <file unreadable>` and never raises; exit by 1.5.
        held = self.seed_held_store(count=2)
        first = sorted(held)[0]
        (self.bdir() / "proposals" / f"{first}.json").write_text(
            "{not json", "utf-8"
        )
        result = self.status()
        self.assert_no_traceback(result)
        self.assertIn(f"  held {first} [?] ", result.stdout)
        self.assertIn(result.returncode, (0, 1))

    def test_status_header_prints_live_state_version_of_prior_store(self):
        # D0.2 / section 01 3.6: `state v` is the live state.bimri_version
        # (5.1.0 on a store no 5.2.0 authority write has touched).
        old = self.prior_engine()
        run_id = self.start("prior", engine=old)
        self.propose(run_id, "prior.key", "Prior value.", engine=old)
        self.cli("sync", "--run", run_id, engine=old)
        result = self.status()
        self.assert_no_traceback(result)
        header = self.line_matching(result, HEADER_RE)
        self.assertEqual(header.group(1), "5.1.0")
        self.assertEqual(self.state()["bimri_version"], "5.1.0")

    # ========================================================================
    # D5.2: checkpoint fallbacks and exit codes
    # ========================================================================

    def test_status_cold_store_reports_unproven_exit_0(self):
        # D5.2 / section 05 1.4 item 5 + L1: no witness -> exit 0,
        # `Checkpoint: unproven; run doctor`, `?` counts, no witness written.
        self.seed_store()
        self.witness_path().unlink()
        state_before = (self.bdir() / "state.json").read_bytes()
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(CHECKPOINT_UNPROVEN, self.lines(result))
        cold = self.line_matching(result, COLD_RE)
        self.assertEqual(cold.group(2), "?")
        conflicts = self.line_matching(result, CONFLICTS_RE)
        self.assertEqual(conflicts.group(1), "?")
        self.assertNotIn(BANNER, result.stdout)
        self.assertFalse(self.witness_path().exists())
        self.assertEqual((self.bdir() / "state.json").read_bytes(), state_before)

    def test_status_prior_policy_witness_exit_0(self):
        # D5.2 / section 05 1.4 item 4 (D0.5): a 5.1.2 witness ->
        # `Checkpoint: prior policy; run doctor`, exit 0.
        old = self.prior_engine()
        run_id = self.start("prior", engine=old)
        self.propose(run_id, "prior.key", "Prior value.", engine=old)
        self.cli("sync", "--run", run_id, engine=old)
        self.assertEqual(self.witness()["policy_version"], "5.1.2-authority-1")
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(CHECKPOINT_PRIOR_POLICY, self.lines(result))
        self.assertNotIn(BANNER, result.stdout)

    def test_status_stale_witness_same_epoch_is_unproven_exit_0(self):
        # Section 05 1.4 item 3 (second half): a current-policy witness at
        # the state's epoch that no longer matches state -> unproven, exit 0.
        self.seed_store()
        self.edit_state(lambda state: state.update(tier3_max=state["tier3_max"] + 1))
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(CHECKPOINT_UNPROVEN, self.lines(result))
        self.assertNotIn(BANNER, result.stdout)

    def test_status_exit_1_on_blocked_record(self):
        # D5.2: a blocked record is a recorded recovery condition -> exit 1
        # with the banner carrying the record's reasons.
        self.seed_store()
        engine = self.load_engine()
        paths = engine.Paths(self.root)
        reason = "planted blocked reason for step seven"
        with engine.engine_lock(paths):
            witness = engine.load_sealed_audit_witness(paths)
            self.assertIsNotNone(witness)
            engine.record_audit_blocked(paths, witness, [reason])
        self.assertTrue((self.bdir() / "audit-blocked.json").is_file())
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(BANNER, result.stdout)
        self.assertIn(reason, result.stdout)

    def test_status_exit_1_on_condemned_witness(self):
        # D5.2 / section 05 1.4 item 3 + 1.5: sealed witness epoch below the
        # state's (a condemnation) -> exit 1, banner names both epochs.
        self.seed_store()
        epoch = self.witness()["audit_epoch"]
        self.edit_state(lambda state: state.update(_audit_epoch=epoch + 1))
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(BANNER, result.stdout)
        self.assertIn(
            f"audit checkpoint condemned at epoch {epoch + 1} "
            f"(witness epoch {epoch})",
            result.stdout,
        )
        self.assertNotRegex(result.stdout, r"(?m)^Checkpoint: valid at")

    def test_status_exit_1_on_marker_reconcile_left(self):
        # D5.2: an authority marker that reconcile left (a failed resolution
        # keeps its `resolve` marker) -> exit 1 with the banner; status
        # never clears it.
        seed = self.start("marker-seed")
        self.propose(seed, "marker.anchor", "Anchor value.")
        self.cli("sync", "--run", seed)
        candidate_run = self.start("marker-candidate")
        writer_run = self.start("marker-writer")
        candidate = self.propose(
            candidate_run, "marker.conflict", "Candidate value."
        )
        self.propose(writer_run, "marker.conflict", "Accepted writer value.")
        self.cli("sync", "--run", writer_run)
        self.cli("sync", "--run", candidate_run)
        decision = json.loads(
            (self.bdir() / "decisions" / f"{candidate}.json").read_text("utf-8")
        )
        self.assertEqual(decision["outcome"], "contested")
        failed = self.worker(
            "resolution_fail_during_force_apply", "resolve",
            decision["conflict_id"], "--choose", candidate, "--human-approved",
            check=False,
        )
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        marker = self.bdir() / "audit-transition.json"
        self.assertTrue(marker.is_file())
        marker_bytes = marker.read_bytes()
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(BANNER, result.stdout)
        self.assertEqual(marker.read_bytes(), marker_bytes)

    def test_status_open_ticket_is_exit_0_and_ticket_untouched(self):
        # D3.4.2 / D5.2: a ticket-only unfinished save is not a recorded
        # condition: exit 0, the `Unfinished save:` line, ticket bytes kept.
        self.seed_store()
        ticket = self.plant_ticket("sess1-turn7-a1b2", "2026-10-15T09:12:03Z")
        before = ticket.read_bytes()
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(BANNER, result.stdout)
        self.assertIn(
            "Unfinished save: sess1-turn7-a1b2 (admitted 2026-10-15T09:12:03Z); "
            "the next save or doctor finishes it",
            self.lines(result),
        )
        self.assertEqual(ticket.read_bytes(), before)
        self.assertFalse((self.bdir() / "saves" / "done").joinpath(
            "sess1-turn7-a1b2.json").exists())

    def test_status_unfinished_saves_total_and_at_most_five_lines(self):
        # Decisions 4 / section 01 3.6: `Unfinished saves: N` carries the
        # total; at most five `Unfinished save:` lines, sorted by name.
        self.seed_store()
        names = [f"req-{index:02d}" for index in (6, 2, 4, 0, 5, 1, 3)]
        for index, name in enumerate(names):
            self.plant_ticket(name, f"2026-10-15T09:1{index}:00Z")
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cold = self.line_matching(result, COLD_RE)
        self.assertEqual(int(cold.group(3)), 7)
        rows = [UNFINISHED_RE.match(line) for line in self.lines(result)]
        rows = [row.group(1) for row in rows if row]
        self.assertEqual(rows, sorted(names)[:5])

    def test_status_unfinished_save_line_follows_checkpoint_line(self):
        # D5.1 block order: the `Unfinished save:` lines come after the
        # Checkpoint line.
        self.seed_store()
        self.plant_ticket("order-check", "2026-10-15T09:12:03Z")
        lines = self.lines(self.status())
        checkpoint = next(
            index for index, line in enumerate(lines)
            if CHECKPOINT_VALID_RE.match(line)
        )
        unfinished = next(
            index for index, line in enumerate(lines)
            if UNFINISHED_RE.match(line)
        )
        self.assertGreater(unfinished, checkpoint)

    def test_status_view_refresh_failure_notice_is_exit_0(self):
        # Section 05 1.3 / L8: `BIMRI NOTICE: generated view could not be
        # refreshed:` stays; a failed refresh alone is exit 0 (1.5).
        self.seed_store()
        (self.root / "bimri.md").unlink()
        result = self.worker("view_oserror", "status", check=False)
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(VIEW_NOTICE, result.stdout + result.stderr)
        self.assertRegex(result.stdout, HEADER_RE.pattern.replace("^", "(?m)^", 1))

    def test_status_heals_missing_generated_view(self):
        # Section 05 1.1: sync_generated_view kept; status heals the view.
        self.seed_store()
        expected = self.head_text()
        (self.root / "bimri.md").unlink()
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "bimri.md").read_text("utf-8"), expected)

    # ========================================================================
    # Gate 6 / D5.1: no history, no publication
    # ========================================================================

    def test_status_never_publishes_or_writes_on_a_warm_store(self):
        # Section 05 0.1 / 1.8: status writes nothing on a warm store in
        # sync: witness, verdict, facts, state and bimri.md bytes unchanged.
        self.seed_store()
        before = protected_tree_snapshot(self.root)
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), before)

    def test_status_never_turns_a_cold_store_warm(self):
        # Section 05 0.1 / L9: load_or_initialize(audit_when_cold=False):
        # no witness, verdict or facts generation is written by status.
        self.seed_store()
        self.witness_path().unlink()
        before = protected_tree_snapshot(self.root)
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(protected_tree_snapshot(self.root), before)

    def test_quick_status_reads_no_history(self):
        # Gate 6 (section 10) / D5.1 forbid list: warm status under the
        # seam exits 0 with `Checkpoint: valid at` and `Unfinished saves: 0`.
        self.seed_store()
        result, record, _ = self.probe_status()
        self.assert_no_traceback(result)
        self.assertEqual(record["violations"], [])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^Checkpoint: valid at ")
        self.assertIn("Unfinished saves: 0", result.stdout)
        self.assertFalse(
            [p for p in record["opened"] if p.startswith("log/")],
            record["opened"],
        )

    def test_quick_status_cold_store_reads_no_history(self):
        # Section 05 1.7: the guard holds on a store without a witness too
        # (status never audits).
        self.seed_store()
        self.witness_path().unlink()
        result, record, _ = self.probe_status()
        self.assert_no_traceback(result)
        self.assertEqual(record["violations"], [])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(CHECKPOINT_UNPROVEN, self.lines(result))

    def test_status_runs_reads_no_history(self):
        # Section 01 3.6: `--runs` adds one decisions/ listing only; still
        # no log read and nothing on the forbid list.
        self.seed_store()
        result, record, _ = self.probe_status("--runs")
        self.assert_no_traceback(result)
        self.assertEqual(record["violations"], [])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(
            [p for p in record["opened"] if p.startswith("decisions/")],
            record["opened"],
        )

    def test_status_under_valid_witness_forbid_full_audit(self):
        # Section 10.1.4: the existing worker mode also drives quick status.
        self.seed_store()
        result = self.worker(
            "valid_witness_forbid_full_audit", "status", check=False
        )
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^Checkpoint: valid at ")

    def test_status_forbid_history_scan_worker_mode(self):
        # Seam `status_forbid_history_scan` (crash_worker) on a warm store.
        if not self.worker_has_mode("status_forbid_history_scan"):
            self.skipTest("crash_worker lacks status_forbid_history_scan")
        self.seed_store()
        result = self.worker(
            "status_forbid_history_scan", "status", check=False
        )
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^Checkpoint: valid at ")

    def test_status_on_300_run_store_reads_no_history(self):
        # Gate 6 / spine step 7 test list: status under the seam on a
        # 300-run store after its seeding doctor; quick (< 3 s wall).
        if not (REDTEAM_STORE_300 / ".bimri" / "state.json").is_file():
            self.skipTest("redteam/store-medium-300 is absent")
        root = self.workspace / "store-300"
        copy_store(REDTEAM_STORE_300, root)
        self.assertGreaterEqual(self.state(root)["run_count"], 300)
        self.live_doctor(root=root)
        before = protected_tree_snapshot(root)
        self.probe_status(root=root)  # warm the OS file cache
        result, record, elapsed = self.probe_status(root=root)
        self.assert_no_traceback(result)
        self.assertEqual(record["violations"], [])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^Checkpoint: valid at ")
        self.assertIn("Unfinished saves: 0", result.stdout)
        self.assertEqual(protected_tree_snapshot(root), before)
        self.assertLess(elapsed, 3.0, f"status took {elapsed:.2f}s")

    # ========================================================================
    # D1.6: status --runs
    # ========================================================================

    def test_status_runs_rows_follow_legacy_line_in_tsv_format(self):
        # D1.6 / section 01 3.6 (Decisions 6): one TSV row per active run in
        # id order, right after the `Legacy active runs:` line.
        run_a, run_b, _ = self.seed_store()
        result = self.status("--runs")
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.lines(result)
        legacy = next(
            index for index, line in enumerate(lines) if LEGACY_RE.match(line)
        )
        rows = [RUN_ROW_RE.match(line) for line in lines[legacy + 1:legacy + 3]]
        self.assertTrue(all(rows), lines)
        self.assertEqual([row.group(1) for row in rows], sorted([run_a, run_b]))
        self.assertNotRegex(result.stdout, r"(?i)orphan")

    def test_status_runs_row_values(self):
        # Section 01 3.6: actor, `last` = run_last_seen, `proposals` from
        # F.run_facts, `undecided` = those without decisions/<Q>.json.
        run_a, run_b, _ = self.seed_store()
        state = self.state()
        result = self.status("--runs")
        rows = {
            row.group(1): row
            for row in (RUN_ROW_RE.match(line) for line in self.lines(result))
            if row
        }
        expected = {
            run_a: ("seed-a", "2", "0"),
            run_b: ("seed-b", "1", "1"),
        }
        for run_id, (actor, proposals, undecided) in expected.items():
            row = rows[run_id]
            meta = state["active_runs"][run_id]
            last = meta.get("last_activity_at") or meta.get("started_at")
            self.assertEqual(row.group(2), actor)
            self.assertEqual(row.group(3), last)
            self.assertRegex(row.group(3), TIMESTAMP_RE)
            self.assertEqual(row.group(4), proposals)
            self.assertEqual(row.group(5), undecided)

    def test_status_runs_unknown_last_seen(self):
        # Section 01 3.6: `last unknown` when run_last_seen is None.
        run_a, _, _ = self.seed_store()

        def strip(state):
            meta = state["active_runs"][run_a]
            meta.pop("last_activity_at", None)
            meta["started_at"] = "not-a-timestamp"

        self.edit_state(strip)
        result = self.status("--runs")
        self.assert_no_traceback(result)
        row = next(
            row for row in (RUN_ROW_RE.match(l) for l in self.lines(result))
            if row and row.group(1) == run_a
        )
        self.assertEqual(row.group(3), "unknown")

    def test_status_runs_counts_print_question_mark_without_checkpoint(self):
        # Section 01 3.6 / L1 (Decisions 6): both counts are `?` without a
        # valid checkpoint.
        self.seed_store()
        self.witness_path().unlink()
        result = self.status("--runs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [RUN_ROW_RE.match(line) for line in self.lines(result)]
        rows = [row for row in rows if row]
        self.assertEqual(len(rows), 2, result.stdout)
        for row in rows:
            self.assertEqual(row.group(4), "?")
            self.assertEqual(row.group(5), "?")

    def test_status_without_runs_flag_prints_no_rows(self):
        # D1.6: the full list only with --runs.
        self.seed_store()
        result = self.status()
        self.assertFalse(
            [line for line in self.lines(result) if RUN_ROW_RE.match(line)],
            result.stdout,
        )

    def test_status_runs_with_no_legacy_runs_prints_nothing_extra(self):
        # Section 05 1.6: without legacy runs `--runs` prints nothing extra.
        run_a, run_b, _ = self.seed_store()
        self.cli("sync", "--run", run_b)
        for run_id in (run_a, run_b):
            self.cli(
                "close", "--run", run_id, "--outcome", "success",
                "--summary", "Done.",
            )
        self.live_doctor()
        plain = self.status()
        listed = self.status("--runs")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertEqual(listed.stdout, plain.stdout)

    def test_status_parser_accepts_runs_flag(self):
        # D1.6 / section 05 1.6: the parser gains `--runs` (store_true).
        self.assertIn("--runs", self.help_text(ENGINE, "status"))

    def test_brief_orphan_trailer_points_at_status_runs(self):
        # Decisions 16 / section 08 L8.5: the brief's orphan trailer becomes
        # `full list: status --runs`.
        engine = self.load_engine()
        stale = [f"R{index:06d}" for index in range(1, 9)]
        line = engine.render_orphan_candidates(stale)
        self.assertTrue(line.endswith("; full list: status --runs"), line)


    # ========================================================================
    # Gate 7 (section 10) and the remaining D5.2 / section 05 1.5 rows
    # ========================================================================

    def test_status_exit_codes_follow_recorded_conditions(self):
        # Gate 7 / D5.2 (F1): witness deleted -> exit 0 unproven; a blocked
        # record -> exit 1; a marker reconcile left -> exit 1. The F4 and R0c
        # rows are their own tests below (they need F4 or the save command).
        self.seed_store()
        unproven = self.clone_store("unproven")
        self.witness_path(unproven).unlink()
        result = self.status(root=unproven)
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(CHECKPOINT_UNPROVEN, self.lines(result))

        blocked = self.clone_store("blocked")
        engine = self.load_engine()
        paths = engine.Paths(blocked)
        with engine.engine_lock(paths):
            witness = engine.load_sealed_audit_witness(paths)
            engine.record_audit_blocked(paths, witness, ["gate seven blocked"])
        result = self.status(root=blocked)
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(BANNER, result.stdout)

        marked = self.clone_store("marked")
        candidate_run = self.start("gate7-candidate", root=marked)
        writer_run = self.start("gate7-writer", root=marked)
        candidate = self.propose(
            candidate_run, "gate7.conflict", "Candidate.", root=marked
        )
        self.propose(writer_run, "gate7.conflict", "Writer.", root=marked)
        self.cli("sync", "--run", writer_run, root=marked)
        self.cli("sync", "--run", candidate_run, root=marked)
        decision = json.loads(
            (self.bdir(marked) / "decisions" / f"{candidate}.json")
            .read_text("utf-8")
        )
        failed = self.worker(
            "resolution_fail_during_force_apply", "resolve",
            decision["conflict_id"], "--choose", candidate, "--human-approved",
            root=marked, check=False,
        )
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        result = self.status(root=marked)
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(BANNER, result.stdout)

    def test_status_condemned_witness_lists_failed_verdict_issues(self):
        # Section 05 1.5 item 2: after a full audit condemns the checkpoint,
        # the banner carries the condemned line then the failed verdict's
        # issues (same epoch), without the `; run doctor` suffix; exit 1.
        self.seed_store()
        decision_path = self.bdir() / "decisions" / "R000001-Q001.json"
        text = decision_path.read_text("utf-8")
        self.assertIn('"accepted"', text)
        decision_path.write_text(
            text.replace('"accepted"', '"rejected"', 1), "utf-8"
        )
        audit = self.cli("doctor", check=False, timeout=600)
        self.assertEqual(audit.returncode, 1, audit.stdout + audit.stderr)
        verdict = self.verdict()
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["status"], "failed")
        epoch = self.state()["_audit_epoch"]
        self.assertEqual(verdict["audit_epoch"], epoch)
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(BANNER, result.stdout)
        condemned = [
            line for line in self.lines(result)
            if "audit checkpoint condemned at epoch " + str(epoch) in line
        ]
        self.assertEqual(len(condemned), 1, result.stdout)
        self.assertFalse(condemned[0].endswith("; run doctor"), condemned[0])
        self.assertIn("  - " + verdict["issues"][0], self.lines(result))

    def test_status_exit_1_on_r0c_wedged_save(self):
        # Gate 7 R0c row / D3.4.4 / section 05 1.5 item 3: a save marker left
        # by mode 125 whose proposal is truncated is wedged; status exits 1
        # and the banner names `recover-save <id>`; marker and ticket kept.
        self.require_save("save_crash_after_log")
        self.seed_store()
        request_id = "s7-wedged-1"
        ticket = self.crash_save_after_log(request_id, "wedge.key")
        proposal = self.bdir() / "proposals" / f"{ticket['proposal_id']}.json"
        data = proposal.read_bytes()
        proposal.write_bytes(data[: len(data) // 2])
        marker = self.bdir() / "audit-transition.json"
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(BANNER, result.stdout)
        self.assertIn(
            f"save {request_id} is wedged: run recover-save {request_id}",
            result.stdout,
        )
        self.assertTrue(marker.is_file())
        self.assertTrue(
            (self.bdir() / "saves" / "open" / f"{request_id}.json").is_file()
        )

    def test_status_finishes_live_save_marker(self):
        # Section 05 1.8 / D3.4.1 / D3.4.3: a save crashed at B4 is finished
        # by status's load through the scoped replay (no full manifest or
        # run-facts scan), allocates no second run, exits 0 with a valid
        # checkpoint afterwards.
        self.require_save("save_crash_after_log")
        self.seed_store()
        request_id = "s7-finish-1"
        self.crash_save_after_log(request_id, "finish.key")
        runs_before = self.state()["run_count"]
        result, counts = self.counter_status(
            ("audit_witness_manifest", "audit_witness_run_facts")
        )
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(counts["audit_witness_manifest"], 0, counts)
        self.assertEqual(counts["audit_witness_run_facts"], 0, counts)
        self.assertFalse((self.bdir() / "audit-transition.json").exists())
        self.assertFalse(
            (self.bdir() / "saves" / "open" / f"{request_id}.json").exists()
        )
        # Spine B8: a save commits run_count with its state, so a save that
        # crashed at B4 is counted once when it finishes, never twice.
        self.assertEqual(self.state()["run_count"], runs_before + 1)
        save_logs = [
            path for path in (self.bdir() / "log").glob("R*.md")
            if path.read_text("utf-8").splitlines()[0].endswith(
                f"| save:{request_id}"
            )
        ]
        self.assertEqual(len(save_logs), 1, save_logs)
        self.assertNotIn(BANNER, result.stdout)
        after = self.status()
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertRegex(after.stdout, r"(?m)^Checkpoint: valid at ")

    def test_status_open_ticket_keeps_checkpoint_valid(self):
        # D3.4.2: with a ticket-only save the witness stays valid and the
        # read stays warm: `Checkpoint: valid at`, witness bytes unchanged.
        self.seed_store()
        self.plant_ticket("warm-ticket-1", "2026-10-15T09:12:03Z")
        before = self.witness_path().read_bytes()
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^Checkpoint: valid at ")
        self.assertEqual(self.witness_path().read_bytes(), before)

    def test_status_validation_errors_line_is_exit_0(self):
        # Section 05 1.3 / 1.5: `Validation errors: N` prints when the head's
        # validate_hot_content returns errors; hot-grammar errors are exit 0.
        self.seed_store()
        state = self.state()
        head = self.bdir() / "revisions" / f"V{state['head_revision']:06d}.md"
        content = head.read_text("utf-8").replace(
            "\n## Tier 3:",
            "[R000001-E009] [K:bad key] nonsense\n\n## Tier 3:",
            1,
        )
        encoded = content.encode("utf-8")
        head.write_bytes(encoded)
        (self.root / "bimri.md").write_bytes(encoded)
        self.edit_state(
            lambda value: value.update(
                head_hash=hashlib.sha256(encoded).hexdigest()
            )
        )
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^Validation errors: [1-9]\d*$")
        self.assertNotIn(BANNER, result.stdout)

    def test_status_held_line_default_message_for_unknown_reason(self):
        # Section 05 1.3: HOLD_MESSAGES default
        # "Candidate preserved; current unchanged." for an unmapped reason.
        held = self.seed_held_store(count=1)
        proposal_id = next(iter(held))
        path = self.bdir() / "decisions" / f"{proposal_id}.json"
        decision = json.loads(path.read_text("utf-8"))
        decision["reason"] = "reason-no-message-maps"
        path.write_text(
            json.dumps(decision, indent=2, sort_keys=True), "utf-8"
        )
        result = self.status()
        self.assert_no_traceback(result)
        self.assertIn(
            f"  held {proposal_id} [{held[proposal_id]}] "
            + STATUS_HELD_DEFAULT_MESSAGE,
            self.lines(result),
        )

    def test_status_busy_lock_exits_2(self):
        # D1.6 / section 05 1.5: exit 2 stays for an error, which covers a
        # busy lock; no report is printed.
        self.seed_store()
        script = self.workspace / "v520_s7_lock.py"
        script.write_text(LOCK_HOLDER_SOURCE, encoding="utf-8")
        signal = self.workspace / "lock-held"
        release = self.workspace / "lock-release"
        holder = subprocess.Popen(
            [sys.executable, str(script), str(ENGINE), str(self.root),
             str(signal), str(release)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 60
            while not signal.exists():
                if holder.poll() is not None or time.monotonic() > deadline:
                    self.fail("lock holder never took the lock")
                time.sleep(0.02)
            result = self.status()
        finally:
            release.write_text("go\n", encoding="utf-8")
            try:
                holder.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.communicate()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("BIMRI engine v", result.stdout)

    def test_status_diverged_view_takes_manual_edit_recovery(self):
        # Section 05 1.1: a diverged view takes the recover-manual-hot-edit
        # write: the edit is preserved under recovery/, the view restored,
        # the notice printed, and (no recorded condition) exit 0.
        self.seed_store()
        expected = self.head_text()
        edited = expected.replace(
            "<!-- END BIMRI -->",
            "A human typed a note into the view.\n\n<!-- END BIMRI -->",
        )
        self.assertNotEqual(edited, expected)
        (self.root / "bimri.md").write_text(edited, "utf-8")
        result = self.status()
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "direct edit to bimri.md was preserved",
            result.stdout + result.stderr,
        )
        self.assertEqual((self.root / "bimri.md").read_text("utf-8"), expected)
        preserved = list((self.bdir() / "recovery").glob("manual-hot-*.md"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text("utf-8"), edited)

    # ========================================================================
    # Section 10.2 F4 (BIMRI_STORE_COPY): the owner's store copy
    # ========================================================================

    def test_status_store_copy_before_seeding_doctor_is_prior_policy(self):
        # Gate 7 F4 row / D0.5 / D5.2: a 5.1.2 witness reads as
        # `Checkpoint: prior policy; run doctor`, exit 0, store bytes kept.
        root = self.store_copy_clone()
        before = protected_tree_snapshot(root)
        result = self.status(root=root)
        self.assert_no_traceback(result)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(CHECKPOINT_PRIOR_POLICY, self.lines(result))
        cold = self.line_matching(result, COLD_RE)
        self.assertEqual(cold.group(2), "?")
        self.assertEqual(protected_tree_snapshot(root), before)

    def test_quick_status_reads_no_history_on_store_copy(self):
        # Gate 6 F4 row: after the seeding doctor, status under the seam
        # exits 0 with the D5.1 lines in under 3 s; Held = len(F.held).
        root = self.store_copy_clone()
        seeded = self.cli("doctor", root=root, check=False, timeout=1500)
        self.assertEqual(seeded.returncode, 0, seeded.stdout + seeded.stderr)
        self.probe_status(root=root)  # warm the OS file cache
        result, record, elapsed = self.probe_status(root=root)
        self.assert_no_traceback(result)
        self.assertEqual(record["violations"], [])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^Checkpoint: valid at ")
        self.assertIn("Unfinished saves: 0", result.stdout)
        cold = self.line_matching(result, COLD_RE)
        self.assertEqual(int(cold.group(2)), len(self.facts(root)["held"]))
        self.assertLess(elapsed, 3.0, f"status took {elapsed:.2f}s")

if __name__ == "__main__":
    unittest.main()
