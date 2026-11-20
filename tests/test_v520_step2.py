"""BIMRI v5.2 build step 2: save ticket, save, resume ladder, lifecycle
refusal, recover-save, version token.

Spine: design/DECISIONS.md D1.2, D1.3, D1.10, D2.1, D2.7 (lookup order),
D2.9, D3.1-D3.5, section 10 R-A, R-B, R-E, R-F, R-N, R-Q; detail from
design/DESIGN.md section 03 (1-12), section 09 step 2 and section 10
(10.0 conventions, 10.1.1 counter seam, 10.1.2 named seams, 10.1.3 ENOSPC,
10.4 gates 2-4).

Process model (section 10.0): every behavioural assertion drives the engine
as a subprocess. Fault injection goes through tests/crash_worker.py modes
that build step 2 adds (save_crash_after_write_<n>, save_fail_enospc_at_<n>,
the named seams 121-133, save_write_undeclared_path) and the reused
witness_crash_before_replace (110). The soak test's 800 in-process saves use
a small driver that imports the engine and calls engine.main in a loop (the
shape section 10.4 gives worker mode loop_save_<n>, which step 2 does not
add to the worker).

Each test proves one promise; the comment above it names the spine id.
"""

import hashlib
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
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ENGINE = REPOSITORY / "bimri-engine.py"
CRASH_WORKER = REPOSITORY / "tests" / "crash_worker.py"

RUN_RE = re.compile(r"BIMRI RUN HANDLE: (R\d{6})")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SAVE_LINE_RE = re.compile(
    r"^BIMRI SAVE (accepted|unchanged|held|stale|conflict) (.+)$", re.M
)
TOKEN_ERROR = (
    "observed token must be V######:hot|cold|head:<64hex> or V######:absent"
)
REQUEST_ERROR = "request id must be 1-100 characters of a-z 0-9 . _ -"
RESERVED_ERROR = "request id is a reserved filename"
UNFINISHED_SAVE_LINE = (
    r"Unfinished save: {rid} \(admitted [^)]+\); "
    r"the next save or doctor finishes it"
)
LIFECYCLE_REFUSAL = (
    r"an engine authority operation is unfinished \(save {rid}\); run doctor"
)
WEDGED_LINE = r"save {rid} is wedged: run recover-save {rid}"
ALREADY_REACHED = (
    "save {rid} already reached its decision or effect; "
    "run recover-save {rid} --rebuild"
)
CRASH_POINT = ".test-save-crash-point"
CRASH_EXIT = 150
NO_SPACE = "No space left"

PROTECTED_EXCLUSIONS = {
    ".bimri/engine.lock",
    ".bimri/runtime.local.json",
    ".bimri/hooks.claude.local.json",
}

TICKET_FIELDS = {
    "ticket_schema", "bimri_version", "engine_release", "request_id",
    "actor", "status", "created_at", "updated_at", "started_on", "intent",
    "intent_hash", "observed", "run_id", "proposal_id", "entry_id",
    "revision", "archive_months", "planned_at", "proposal_sha256",
    "log_sha256", "result", "finished_at", "ticket_hash",
}
INTENT_FIELDS = {
    "operation", "new_subject", "tier", "key", "target", "kind",
    "importance", "status", "trust", "source", "tags", "text", "rationale",
    "confidence", "observations", "evidence", "falsifier", "settles",
    "observed",
}
RESULT_FIELDS = {
    "outcome", "decision", "revision", "conflict_id", "reason", "token",
    "settled", "cooled", "error",
}
PLANNED_FIELDS = (
    "run_id", "proposal_id", "entry_id", "revision", "archive_months",
    "planned_at", "proposal_sha256", "log_sha256",
)
SAVE_JSON_FIELDS = {
    "result", "request", "key", "run", "proposal", "decision", "revision",
    "token", "reason", "observed", "current", "conflict", "settled",
    "cooled", "replayed", "error", "lock_held_ms",
}

# Section 10 10.1.1: the counter seam's boundary table for an accepted save
# of a new subject without --settles (so no B10 row and zero B6 archive
# rows; "*" marks a boundary that may repeat 0..k times). Paths are relative
# to the store root with the ".bimri/" prefix removed; "<rid>" is replaced
# by the escaped request id. After B14d the remaining labels are matched as
# a multiset: the B15 marker unlink and the prune of the superseded manifest
# and facts generations (iterdir order is filesystem-dependent).
ACCEPTED_BOUNDARIES = (
    ("B1", r"saves/open/<rid>\.json"),
    ("B2a", r"audit-transition\.json"),
    ("B2b", r"state\.json"),
    ("B3", r"proposals/R\d{6}-Q001\.json"),
    ("B4", r"log/R\d{6}\.md"),
    ("B5", r"decisions/R\d{6}-Q001\.json"),
    ("B6*", r"archive/[^/]+\.md"),
    ("B7", r"revisions/V\d{6}\.md"),
    ("B8a", r"state\.json"),
    ("B8b", r"bimri\.md"),
    ("B9", r"decisions/R\d{6}-Q001\.json"),
    ("B11a", r"log/R\d{6}\.md"),
    ("B11b", r"log/R\d{6}\.md"),
    ("B12a", r"saves/done/<rid>\.json"),
    ("B12b", r"saves/open/<rid>\.json"),
    ("B13", r"audit-transition\.json"),
    ("B14a", r"audit-manifests/facts-[0-9a-f]{64}\.json"),
    ("B14b", r"audit-manifests/[0-9a-f]{64}\.json"),
    ("B14c", r"audit-manifest\.json"),
    ("B14d", r"audit-witness\.json"),
)

SEED_KEY = "seed.alpha"
SEED_TEXT = "Alpha seed value."
OWNER_KEY = "seed.owner"
OWNER_TEXT = "Owner confirmed rule."
MATRIX_KEY = "matrix.subject"
MATRIX_TEXT = "Matrix subject value."
AGENT_REWRITE = "Agent rewrite."

# The three intents of the gate-3 matrix (section 10.4): an accepted new
# subject, a held agent set over confirmed memory, and an unchanged set.
INTENTS = {
    "accepted": {
        "key": MATRIX_KEY, "text": MATRIX_TEXT, "new_subject": True,
        "outcome": "accepted", "code": 0,
    },
    "held": {
        "key": OWNER_KEY, "text": AGENT_REWRITE, "new_subject": False,
        "outcome": "held", "code": 1,
    },
    "unchanged": {
        "key": SEED_KEY, "text": SEED_TEXT, "new_subject": False,
        "outcome": "unchanged", "code": 0,
    },
}

SOAK_DRIVER = r"""
import contextlib, importlib.util, io, json, re, sys
engine_path, root, plan_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("bimri_soak_target", engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
with open(plan_path, encoding="utf-8") as handle:
    plan = json.load(handle)
token = plan["token"]
done = 0
for index in range(plan["start"], plan["start"] + plan["count"]):
    arguments = [
        "save", "--request", "test-soak-%d" % index, "--observed", token,
        "--key", plan["key"], "--text", "Soak value number %d." % index,
    ]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = engine.main(["--root", root] + arguments)
    text = buffer.getvalue()
    match = re.search(r"^BIMRI SAVE accepted .* token=(\S+)$", text, re.M)
    if code != 0 or match is None:
        sys.stderr.write("soak save %d failed: rc=%r\n%s\n" % (index, code, text))
        sys.exit(3)
    token = match.group(1)
    done += 1
print(json.dumps({"done": done, "token": token}))
"""


def protected_tree_snapshot(root):
    """Copy of tests/test_bimri_engine.py:62 (bytes of bimri.md and .bimri)."""
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


def parse_save_line(stdout):
    match = SAVE_LINE_RE.search(stdout)
    if match is None:
        return None
    fields = {"result": match.group(1), "line": match.group(0)}
    for pair in match.group(2).split(" "):
        name, _, value = pair.partition("=")
        fields[name] = value
    return fields


def copy_store(source, destination):
    shutil.copytree(
        str(source),
        str(destination),
        ignore=shutil.ignore_patterns("engine.lock", "runtime.local.json"),
    )
    lock = Path(destination) / ".bimri" / "engine.lock"
    if not lock.exists():
        lock.write_bytes(b"")


def match_accepted_boundaries(labels, rid):
    """Walk ACCEPTED_BOUNDARIES over the recorded labels. Returns
    (positions, tail) with 1-based n per boundary name; raises
    AssertionError naming the first mismatch."""
    positions = {}
    index = 0
    for name, pattern in ACCEPTED_BOUNDARIES:
        regex = re.compile("^" + pattern.replace("<rid>", re.escape(rid)) + "$")
        if name.endswith("*"):
            positions[name] = []
            while index < len(labels) and regex.match(labels[index]):
                positions[name].append(index + 1)
                index += 1
            continue
        if index >= len(labels) or not regex.match(labels[index]):
            raise AssertionError(
                "boundary {} expected {} at n={}, got {!r}; sequence {!r}".format(
                    name, pattern, index + 1, labels[index:index + 1], labels
                )
            )
        positions[name] = index + 1
        index += 1
    tail = labels[index:]
    if "audit-transition.json" in tail:
        positions["B15"] = index + tail.index("audit-transition.json") + 1
    return positions, tail


def worker_has_mode(mode):
    return ('"' + mode + '"') in CRASH_WORKER.read_text("utf-8")


class V520Step2Base(unittest.TestCase):
    maxDiff = None
    _accepted_labels = None

    @classmethod
    def setUpClass(cls):
        cls._class_temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s2-")
        cls.class_workspace = Path(cls._class_temp.name)
        cls.template = cls.class_workspace / "template"
        cls.build_template(cls.template)
        probe = cls.class_workspace / "probe"
        copy_store(cls.template, probe)
        cls.template_tokens = {}
        for key in (SEED_KEY, OWNER_KEY, MATRIX_KEY):
            result = cls.run_engine(probe, "get", "--key", key, "--json")
            payload = json.loads(result.stdout)
            cls.template_tokens[key] = cls.token_from_payload(payload, key)
        state = json.loads(
            (cls.template / ".bimri" / "state.json").read_text("utf-8")
        )
        cls.template_head = state["head_revision"]
        cls.template_witness = json.loads(
            (cls.template / ".bimri" / "audit-witness.json").read_text("utf-8")
        )

    @classmethod
    def tearDownClass(cls):
        cls._class_temp.cleanup()

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bimri-v520-s2-")
        self.workspace = Path(self._temp.name)
        self.root = self.workspace / "store"
        copy_store(self.template, self.root)

    def tearDown(self):
        self._temp.cleanup()

    # ---- process helpers (test_v512_defects.py:59 recipe) ------------------

    @classmethod
    def run_engine(cls, root, *arguments, input_text=None, timeout=300):
        command = [
            sys.executable, str(ENGINE), "--root", str(root),
        ] + [str(argument) for argument in arguments]
        return subprocess.run(
            command, text=True, capture_output=True, input=input_text,
            timeout=timeout, encoding="utf-8",
        )

    @staticmethod
    def token_from_payload(payload, key):
        for match in payload.get("matches", []):
            if match.get("key") == key and match.get("location") in ("HOT", "COLD"):
                return match["token"]
        return payload["token"]

    @classmethod
    def start_arguments(cls):
        # D1.1 / build step 2 (k): start gains --open-run as an accepted no-op;
        # the legacy lane is spelled start --open-run from this step on.
        return ["start", "--open-run"]

    @classmethod
    def class_cli(cls, root, *arguments):
        result = cls.run_engine(root, *arguments)
        if result.returncode != 0:
            raise AssertionError(
                "seed command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    arguments, result.stdout, result.stderr
                )
            )
        return result

    @classmethod
    def build_template(cls, root):
        started = cls.class_cli(root, *cls.start_arguments(), "--actor", "seed")
        run_id = RUN_RE.search(started.stdout).group(1)
        cls.class_cli(
            root, "propose", "--run", run_id, "--tier", "2", "--key",
            SEED_KEY, "--text", SEED_TEXT, "--source", "agent", "--trust",
            "working", "--new-subject",
        )
        cls.class_cli(
            root, "propose", "--run", run_id, "--tier", "2", "--key",
            OWNER_KEY, "--text", OWNER_TEXT, "--source", "user", "--trust",
            "confirmed", "--new-subject",
        )
        cls.class_cli(root, "sync", "--run", run_id)
        cls.class_cli(
            root, "close", "--run", run_id, "--outcome", "success",
            "--summary", "seeded",
        )
        # One read publishes the witness so every copy starts warm.
        cls.run_engine(root, "get", "--key", SEED_KEY, "--json")

    def cli(self, *arguments, root=None, check=True, input_text=None):
        result = self.run_engine(
            root or self.root, *arguments, input_text=input_text
        )
        if check and result.returncode != 0:
            self.fail(
                "command failed: {!r}\nstdout:\n{}\nstderr:\n{}".format(
                    arguments, result.stdout, result.stderr
                )
            )
        return result

    def worker(self, mode, *arguments, root=None, timeout=300):
        command = [
            sys.executable, str(CRASH_WORKER), str(ENGINE), mode,
            str(root or self.root),
        ] + [str(argument) for argument in arguments]
        return subprocess.run(
            command, text=True, capture_output=True, timeout=timeout,
            encoding="utf-8",
        )

    def fresh_store(self, label):
        destination = self.workspace / label
        if destination.exists():
            shutil.rmtree(str(destination))
        copy_store(self.template, destination)
        return destination

    def start(self, actor, root=None):
        result = self.cli(*self.start_arguments(), "--actor", actor, root=root)
        match = RUN_RE.search(result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def propose(self, run_id, key, text, root=None, new_subject=False,
                source="agent", trust="working", check=True):
        arguments = [
            "propose", "--run", run_id, "--key", key, "--text", text,
            "--source", source, "--trust", trust,
        ]
        if new_subject:
            arguments.extend(["--tier", "2", "--new-subject"])
        return self.cli(*arguments, root=root, check=check)

    def apply_set(self, actor, key, text, root=None, new_subject=False):
        run_id = self.start(actor, root=root)
        self.propose(run_id, key, text, root=root, new_subject=new_subject)
        self.cli("sync", "--run", run_id, root=root)
        self.cli(
            "close", "--run", run_id, "--outcome", "success", "--summary",
            "applied", root=root,
        )
        return run_id

    def save_arguments(self, rid, key, observed, text=None, new_subject=False,
                       extra=(), json_out=False):
        arguments = ["save", "--request", rid, "--observed", observed,
                     "--key", key]
        if new_subject:
            arguments.extend(["--new-subject", "--tier", "2"])
        if text is not None:
            arguments.extend(["--text", text])
        arguments.extend(extra)
        if json_out:
            arguments.append("--json")
        return arguments

    def save(self, rid, key, observed, text=None, new_subject=False,
             extra=(), root=None, json_out=False):
        return self.cli(
            *self.save_arguments(
                rid, key, observed, text, new_subject, extra, json_out
            ),
            root=root, check=False,
        )

    def save_intent(self, rid, intent, root=None, json_out=False):
        """Save one of INTENTS with the template's observed token (never read
        through get on a crashed root: that get would finish the save)."""
        return self.save(
            rid, intent["key"], self.template_tokens[intent["key"]],
            intent["text"], intent["new_subject"], root=root,
            json_out=json_out,
        )

    def save_worker(self, mode, rid, key, observed, text=None,
                    new_subject=False, extra=(), root=None):
        return self.worker(
            mode,
            *self.save_arguments(rid, key, observed, text, new_subject, extra),
            root=root,
        )

    def assert_save(self, result, outcome, code):
        self.assertEqual(
            result.returncode, code, result.stdout + result.stderr
        )
        fields = parse_save_line(result.stdout)
        self.assertIsNotNone(fields, result.stdout + result.stderr)
        self.assertEqual(fields["result"], outcome, result.stdout)
        return fields

    def assert_failed(self, result, rid=None):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIsNone(parse_save_line(result.stdout), result.stdout)
        if rid is not None:
            self.assertIn(
                "BIMRI ERROR: save {} failed:".format(rid), result.stderr
            )

    # ---- store readers ---------------------------------------------------

    def bdir(self, root=None):
        return Path(root or self.root) / ".bimri"

    def state(self, root=None):
        return json.loads(self.bdir(root).joinpath("state.json").read_text("utf-8"))

    def head_token(self, root=None):
        state = self.state(root)
        return "V{:06d}:head:{}".format(state["head_revision"], state["head_hash"])

    def get_json(self, key, root=None):
        result = self.cli("get", "--key", key, "--json", root=root, check=False)
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        return result, json.loads(result.stdout)

    def key_token(self, key, root=None):
        _, payload = self.get_json(key, root=root)
        return self.token_from_payload(payload, key)

    def current_match(self, key, root=None):
        _, payload = self.get_json(key, root=root)
        for match in payload.get("matches", []):
            if match.get("key") == key and match.get("location") in ("HOT", "COLD"):
                return match
        return None

    def current_text(self, key, root=None):
        match = self.current_match(key, root=root)
        return None if match is None else match["text"]

    def marker_path(self, root=None):
        return self.bdir(root) / "audit-transition.json"

    def open_ticket_path(self, rid, root=None):
        return self.bdir(root) / "saves" / "open" / "{}.json".format(rid)

    def done_ticket_path(self, rid, root=None):
        return self.bdir(root) / "saves" / "done" / "{}.json".format(rid)

    def open_tickets(self, root=None):
        directory = self.bdir(root) / "saves" / "open"
        return sorted(path.name for path in directory.iterdir())

    def ticket(self, rid, root=None):
        for path in (self.open_ticket_path(rid, root), self.done_ticket_path(rid, root)):
            if path.is_file():
                return json.loads(path.read_text("utf-8"))
        self.fail("no ticket for {}".format(rid))

    def save_logs(self, rid, root=None):
        found = []
        for path in sorted(self.bdir(root).joinpath("log").glob("R*.md")):
            with path.open("r", encoding="utf-8") as handle:
                header = handle.readline()
            # Anchor on the header's end: a substring match would let
            # save:test-soak-1 also match save:test-soak-10..199.
            if header.rstrip("\r\n").endswith("| save:{}".format(rid)):
                found.append(path)
        return found

    def proposals_for(self, rid, root=None):
        found = []
        for path in sorted(self.bdir(root).joinpath("proposals").glob("*.json")):
            try:
                record = json.loads(path.read_text("utf-8"))
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("request_id") == rid:
                found.append(path.stem)
        return found

    def decision(self, proposal_id, root=None):
        path = self.bdir(root) / "decisions" / "{}.json".format(proposal_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text("utf-8"))

    def revision_numbers(self, root=None):
        return sorted(
            int(path.stem[1:])
            for path in self.bdir(root).joinpath("revisions").glob("V*.md")
        )

    def archive_rows(self, root=None):
        rows = []
        directory = self.bdir(root) / "archive"
        if directory.is_dir():
            for path in sorted(directory.glob("*.md")):
                rows.extend(
                    line for line in path.read_text("utf-8").splitlines()
                    if line.startswith("[")
                )
        return rows

    def conflict_records(self, root=None):
        directory = self.bdir(root) / "conflicts"
        return [
            json.loads(path.read_text("utf-8"))
            for path in sorted(directory.glob("C*.json"))
        ]

    def drift_receipts(self, root=None):
        directory = self.bdir(root) / "audit-drift"
        if not directory.is_dir():
            return []
        return sorted(directory.glob("D*.json"))

    def witness(self, root=None):
        path = self.bdir(root) / "audit-witness.json"
        self.assertTrue(path.is_file(), "missing audit witness: {}".format(path))
        return json.loads(path.read_text("utf-8"))

    def generation_names(self, root=None):
        return sorted(
            path.name
            for path in self.bdir(root).joinpath("audit-manifests").glob("*.json")
        )

    def manifest_paths(self, root=None):
        witness = self.witness(root)
        path = self.bdir(root) / "audit-manifests" / "{}.json".format(
            witness["manifest_hash"]
        )
        rows = json.loads(path.read_text("utf-8"))["manifest"]
        if isinstance(rows, dict):
            return sorted(rows)
        paths = []
        for row in rows:
            if isinstance(row, dict):
                paths.append(row.get("path"))
            else:
                paths.append(row[0])
        return sorted(paths)

    def assert_manifest_bound(self, root=None):
        # assert_steady_manifest_generation_bound (test_v511_performance.py:330)
        # extended by section 10.4: exactly one manifest generation and one
        # facts generation, both named by the live checkpoint.
        witness = self.witness(root)
        self.assertEqual(witness["audit_epoch"], self.state(root)["_audit_epoch"])
        self.assertEqual(
            self.generation_names(root),
            sorted([
                "{}.json".format(witness["manifest_hash"]),
                "facts-{}.json".format(witness["facts_hash"]),
            ]),
        )

    def assert_doctor_read_only_passed(self, root=None):
        # From step 8 the read-only doctor audits a copy whose capture has a
        # lock budget (1500 ms); this helper checks the store's health, not
        # the capture time, so a loaded machine must not defer it (exit 3).
        result = self.cli(
            "doctor", "--read-only", "--budget-ms", "600000", root=root,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASSED", result.stdout)
        return result

    def clean_run(self, rid, intent):
        """The same save on an untouched template copy: the stdout and code a
        resumed or replayed save must reproduce byte for byte."""
        root = self.fresh_store("clean-" + rid)
        result = self.save_intent(rid, intent, root=root)
        self.assert_save(result, intent["outcome"], intent["code"])
        shutil.rmtree(str(root), ignore_errors=True)
        return result

    def crash_point(self, root):
        path = Path(root) / CRASH_POINT
        if not path.is_file():
            return None
        number, _, relative = path.read_text("utf-8").strip().partition("\t")
        path.unlink()
        relative = relative.replace("\\", "/")
        if relative.startswith(".bimri/"):
            relative = relative[len(".bimri/"):]
        return int(number), relative

    def crash_named(self, mode, code, rid, key, text, new_subject=True,
                    root=None, observed=None, extra=()):
        root = root or self.root
        observed = observed or self.key_token(key, root=root)
        result = self.save_worker(
            mode, rid, key, observed, text, new_subject, extra, root=root
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return observed

    def enumerate_points(self, mode_prefix, rid, intent, per_point,
                         label="n"):
        """Run mode_prefix<n> for n = 1, 2, ... on fresh template copies
        until a run completes (no crash point, no ENOSPC). per_point(number,
        root, result, point) runs for every interrupted n. Returns the list
        of recorded crash labels (None for an ENOSPC point)."""
        labels = []
        observed = self.template_tokens[intent["key"]]
        for number in range(1, 80):
            root = self.fresh_store("{}{}".format(label, number))
            result = self.save_worker(
                "{}{}".format(mode_prefix, number), rid, intent["key"],
                observed, intent["text"], intent["new_subject"], root=root,
            )
            point = self.crash_point(root)
            if point is None and NO_SPACE not in result.stderr:
                self.assert_save(result, intent["outcome"], intent["code"])
                shutil.rmtree(str(root), ignore_errors=True)
                return labels
            labels.append(point[1] if point is not None else None)
            per_point(number, root, result, point)
            shutil.rmtree(str(root), ignore_errors=True)
        self.fail("the save never completed within 80 counted writes")

    def accepted_labels(self):
        """The counter-seam labels of the accepted intent, enumerated once
        per process (crash only, no retries)."""
        if V520Step2Base._accepted_labels is None:
            rid = "test-labels-1"

            def per_point(number, root, result, point):
                self.assertEqual(result.returncode, CRASH_EXIT, result.stderr)
                self.assertIsNotNone(point, "n={}".format(number))

            labels = self.enumerate_points(
                "save_crash_after_write_", rid, INTENTS["accepted"], per_point,
                label="labels",
            )
            V520Step2Base._accepted_labels = (rid, labels)
        return V520Step2Base._accepted_labels

    def assert_exactly_once(self, root, rid, intent, base_revisions,
                            base_archive):
        logs = self.save_logs(rid, root=root)
        proposals = self.proposals_for(rid, root=root)
        if intent["outcome"] == "unchanged":
            self.assertEqual(logs, [])
            self.assertEqual(proposals, [])
            self.assertEqual(self.revision_numbers(root=root), base_revisions)
        else:
            self.assertEqual(len(logs), 1, [path.name for path in logs])
            self.assertEqual(len(proposals), 1, proposals)
            decision = self.decision(proposals[0], root=root)
            self.assertIsNotNone(decision)
            self.assertEqual(decision["outcome"], intent["outcome"])
            grown = 1 if intent["outcome"] == "accepted" else 0
            self.assertEqual(
                len(self.revision_numbers(root=root)),
                len(base_revisions) + grown,
            )
            lines = [
                line for line in logs[0].read_text("utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(lines[-1].startswith("[CLOSED:"), lines[-1])
        rows = self.archive_rows(root=root)
        self.assertEqual(len(rows), len(set(rows)), "duplicate archive row")
        self.assertTrue(set(base_archive) <= set(rows))
        self.assertFalse(self.marker_path(root).exists())
        self.assertEqual(self.open_tickets(root), [])
        self.assert_manifest_bound(root)
        self.assert_doctor_read_only_passed(root)

    def assert_byte_free_replay(self, root, rid, intent, expected):
        before = protected_tree_snapshot(root)
        third = self.save_intent(rid, intent, root=root)
        self.assertEqual(
            (third.returncode, third.stdout),
            (expected.returncode, expected.stdout),
        )
        replay = self.save_intent(rid, intent, root=root, json_out=True)
        self.assertEqual(replay.returncode, expected.returncode, replay.stderr)
        payload = json.loads(replay.stdout)
        self.assertIs(payload["replayed"], True)
        self.assertEqual(payload["result"], intent["outcome"])
        self.assertEqual(changed_paths(before, protected_tree_snapshot(root)), [])

    def seed_cold(self, key, root=None):
        """seed_cold_current recipe (test_v510_lifecycle.py:224) adapted to a
        store with residents: a long importance-1 subject, a hot ceiling 4 bytes
        above the current view, then one short new subject; the pressure plan
        cools the lowest-ranked resident, this subject. The ceiling is put back
        afterwards so later saves meet no capacity hold."""
        root = root or self.root
        run_id = self.start("cold-create", root=root)
        self.cli(
            "propose", "--run", run_id, "--key", key, "--text",
            "Current cold value. " + "c" * 390, "--tier", "2",
            "--new-subject", "--importance", "1", "--source", "agent",
            "--trust", "working", root=root,
        )
        self.cli("sync", "--run", run_id, root=root)
        self.cli("close", "--run", run_id, "--outcome", "success",
                 "--summary", "created", root=root)
        path = self.bdir(root) / "state.json"
        state = json.loads(path.read_text("utf-8"))
        ceiling = state["hot_max_bytes"]
        state["hot_max_bytes"] = len(Path(root).joinpath("bimri.md").read_bytes()) + 4
        path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.apply_set("cold-pusher", "cold.pusher", "Pushes the view.",
                       root=root, new_subject=True)
        state = json.loads(path.read_text("utf-8"))
        self.assertEqual(sorted(state["cold_current"]), [key])
        state["hot_max_bytes"] = ceiling
        path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        token = self.key_token(key, root=root)
        self.assertRegex(token, r"^V\d{6}:cold:[0-9a-f]{64}$")
        return token

    def wedge_by_truncation(self, mode, code, rid, key=MATRIX_KEY,
                            text=MATRIX_TEXT, new_subject=True,
                            delete_ticket=False):
        """Crash at a named boundary past B3 and truncate the proposal: a Q
        whose bytes no longer match its sealed hash is R0c (D3.4.4 R2 / R0).
        Returns (proposal id, proposal path, original bytes, observed)."""
        observed = self.crash_named(mode, code, rid, key, text, new_subject)
        proposal_id = self.ticket(rid)["proposal_id"]
        if delete_ticket:
            self.open_ticket_path(rid).unlink()
        path = self.bdir() / "proposals" / "{}.json".format(proposal_id)
        original = path.read_bytes()
        path.write_bytes(original[: len(original) // 2])
        return proposal_id, path, original, observed


class SaveVisibilityTest(V520Step2Base):
    """Gate 2: an acknowledged save is memory every later process sees."""

    # Spine D3.2 L10, D3.3.1, D1.3 (gate 2): for accepted, unchanged, held and
    # stale, a fresh process sees exactly what the result line reported.
    def test_acknowledged_save_is_visible_to_a_fresh_process(self):
        for outcome in ("accepted", "unchanged", "held", "stale"):
            with self.subTest(outcome=outcome):
                root = self.fresh_store("visible-" + outcome)
                rid = "test-visible-" + outcome
                active = self.state(root)["active_runs"]
                if outcome == "stale":
                    token = self.key_token(SEED_KEY, root=root)
                    self.assert_save(
                        self.save("test-visible-mover", SEED_KEY, token,
                                  "Moved first.", root=root),
                        "accepted", 0,
                    )
                    before = protected_tree_snapshot(root)
                    result = self.save(rid, SEED_KEY, token, "Late value.",
                                       root=root)
                    fields = self.assert_save(result, "stale", 1)
                    self.assertEqual(
                        changed_paths(before, protected_tree_snapshot(root)),
                        [".bimri/saves/done/{}.json".format(rid)],
                    )
                    self.assertEqual(
                        self.key_token(SEED_KEY, root=root), fields["current"]
                    )
                else:
                    intent = INTENTS[outcome]
                    result = self.save_intent(rid, intent, root=root)
                    fields = self.assert_save(
                        result, intent["outcome"], intent["code"]
                    )
                if outcome == "accepted":
                    self.assertRegex(fields["token"], r"^V\d{6}:hot:[0-9a-f]{64}$")
                    self.assertEqual(
                        self.key_token(MATRIX_KEY, root=root), fields["token"]
                    )
                    self.assertEqual(
                        self.current_text(MATRIX_KEY, root=root), MATRIX_TEXT
                    )
                if outcome == "unchanged":
                    self.assertEqual(
                        self.key_token(SEED_KEY, root=root),
                        self.template_tokens[SEED_KEY],
                    )
                    self.assertIsNone(self.ticket(rid, root=root)["run_id"])
                if outcome == "held":
                    self.assertEqual(
                        self.decision(fields["proposal"], root=root)["outcome"],
                        "held",
                    )
                    history = self.cli(
                        "recall", "--key", OWNER_KEY, "--history", root=root,
                        check=False,
                    )
                    self.assertIn(fields["proposal"], history.stdout)
                    self.assertEqual(
                        self.current_text(OWNER_KEY, root=root), OWNER_TEXT
                    )
                if outcome in ("accepted", "held"):
                    log = self.bdir(root) / "log" / "{}.md".format(fields["run"])
                    lines = [
                        line for line in log.read_text("utf-8").splitlines()
                        if line.strip()
                    ]
                    self.assertTrue(lines[-1].startswith("[CLOSED:"), lines[-1])
                self.assertEqual(self.state(root)["active_runs"], active)
                self.assertEqual(self.open_tickets(root), [])
                self.assertTrue(self.done_ticket_path(rid, root).is_file())

    # Spine D3.4.1 R3 (gate 2): a crash at the done/ ticket write leaves the
    # effect durable; a fresh get sees the new token and finishes the
    # publication (marker gone, scoped publication, epochs agree).
    def test_save_with_crash_after_ack_is_visible(self):
        rid = "test-ack-crash-1"
        before_scoped = self.witness()["scoped_publications"]
        self.crash_named(
            "save_crash_after_ticket_finished", 133, rid, MATRIX_KEY,
            MATRIX_TEXT,
        )
        self.assertTrue(self.done_ticket_path(rid).is_file())
        self.assertTrue(self.marker_path().is_file())
        recorded = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(recorded["result"]["outcome"], "accepted")
        self.assertEqual(self.key_token(MATRIX_KEY), recorded["result"]["token"])
        self.assertEqual(self.current_text(MATRIX_KEY), MATRIX_TEXT)
        self.assertFalse(self.marker_path().exists())
        self.assertFalse(self.open_ticket_path(rid).exists())
        witness = self.witness()
        self.assertEqual(witness["audit_epoch"], self.state()["_audit_epoch"])
        self.assertEqual(witness["audit_origin"], "scoped")
        self.assertEqual(witness["scoped_publications"], before_scoped + 1)
        self.assert_manifest_bound()

    # Spine D3.2 ("the run never enters active_runs"), gate 2: one thousand
    # successful saves (200 by subprocess, 800 in one process) leave no open
    # run; run_count grows by exactly the count. The compaction assertions
    # step 11 adds (D2.7) run on the same soak in
    # test_v520_step11.ThousandSaveCompactionTest, so the suite pays for
    # one thousand-save compaction soak, not two.
    def test_successful_saves_leave_no_open_run_over_one_thousand_saves(self):
        count = int(os.environ.get("BIMRI_V520_SOAK_SAVES", "1000"))
        by_subprocess = max(1, count // 5)
        before = self.state()
        token = self.template_tokens[MATRIX_KEY]
        for index in range(by_subprocess):
            result = self.save(
                "test-soak-{}".format(index), MATRIX_KEY, token,
                "Soak value number {}.".format(index),
                new_subject=(index == 0),
            )
            token = self.assert_save(result, "accepted", 0)["token"]
        remaining = count - by_subprocess
        if remaining:
            plan = self.workspace / "soak-plan.json"
            plan.write_text(json.dumps({
                "token": token, "key": MATRIX_KEY, "start": by_subprocess,
                "count": remaining,
            }), encoding="utf-8")
            driven = subprocess.run(
                [sys.executable, "-c", SOAK_DRIVER, str(ENGINE),
                 str(self.root), str(plan)],
                text=True, capture_output=True, timeout=7200,
                encoding="utf-8",
            )
            self.assertEqual(driven.returncode, 0, driven.stderr[-4000:])
            self.assertEqual(
                json.loads(driven.stdout.strip().splitlines()[-1])["done"],
                remaining,
            )
        after = self.state()
        self.assertEqual(after["active_runs"], before["active_runs"])
        self.assertEqual(after["session_runs"], before["session_runs"])
        self.assertEqual(after["run_count"], before["run_count"] + count)
        self.assertEqual(self.open_tickets(), [])
        self.assertEqual(
            len(list(self.bdir().joinpath("saves", "done").glob("*.json"))), count
        )
        for index in range(count):
            logs = self.save_logs("test-soak-{}".format(index))
            self.assertEqual(len(logs), 1, index)
            self.assertIn("[CLOSED:", logs[0].read_text("utf-8"))
        self.assert_doctor_read_only_passed()


class SaveRecordTest(V520Step2Base):
    """D2.1 ticket, D2.9 proposal request_id and log header, D1.3 token."""

    # Spine D2.1: the finished ticket carries exactly the 23 fields, the 19
    # intent keys, intent_hash over intent, the seal, and the planned ids.
    def test_finished_ticket_has_the_exact_sealed_shape(self):
        rid = "test-ticket-shape-1"
        observed = self.key_token(MATRIX_KEY)
        fields = self.assert_save(
            self.save(rid, MATRIX_KEY, observed, MATRIX_TEXT, True),
            "accepted", 0,
        )
        self.assertFalse(self.open_ticket_path(rid).exists())
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(set(ticket), TICKET_FIELDS)
        self.assertEqual(set(ticket["intent"]), INTENT_FIELDS)
        self.assertEqual(set(ticket["result"]), RESULT_FIELDS)
        self.assertEqual(ticket["ticket_schema"], 1)
        self.assertEqual(ticket["engine_release"], "5.2.0")
        self.assertEqual(ticket["status"], "finished")
        self.assertEqual(ticket["request_id"], rid)
        self.assertEqual(ticket["intent"]["observed"], observed)
        self.assertEqual(ticket["observed"]["token"], observed)
        self.assertEqual(ticket["observed"]["storage"], "absent")
        self.assertEqual(ticket["observed"]["key_hash"], "absent")
        self.assertEqual(ticket["intent_hash"], audit_digest(ticket["intent"]))
        self.assertEqual(ticket["ticket_hash"], record_seal(ticket, "ticket_hash"))
        self.assertEqual(ticket["run_id"], fields["run"])
        self.assertEqual(ticket["proposal_id"], fields["proposal"])
        self.assertEqual(ticket["entry_id"], ticket["run_id"] + "-E001")
        self.assertEqual(
            "V{:06d}".format(ticket["revision"]), fields["revision"]
        )
        self.assertEqual(ticket["archive_months"], [ticket["started_on"][:7]])
        self.assertRegex(ticket["proposal_sha256"], HASH_RE)
        self.assertRegex(ticket["log_sha256"], HASH_RE)
        self.assertEqual(ticket["result"]["outcome"], "accepted")
        self.assertEqual(ticket["result"]["decision"], "accepted")
        self.assertEqual(ticket["result"]["token"], fields["token"])
        self.assertIsNotNone(ticket["finished_at"])

    # Spine D2.1 / Decisions 1: actor is a top-level field outside intent;
    # omitted flags are null, tags and settles [] and new_subject false.
    def test_intent_records_the_callers_words_only(self):
        rid = "test-intent-words-1"
        observed = self.key_token(SEED_KEY)
        self.assert_save(
            self.save(rid, SEED_KEY, observed, "Alpha rewritten.",
                      extra=("--actor", "jett")),
            "accepted", 0,
        )
        ticket = self.ticket(rid)
        self.assertEqual(ticket["actor"], "jett")
        self.assertNotIn("actor", ticket["intent"])
        intent = ticket["intent"]
        self.assertFalse(intent["new_subject"])
        self.assertEqual(intent["tags"], [])
        self.assertEqual(intent["settles"], [])
        for name in ("tier", "target", "kind", "importance", "status",
                     "rationale", "confidence", "observations", "evidence",
                     "falsifier"):
            self.assertIsNone(intent[name], name)
        self.assertEqual(intent["operation"], "set")
        self.assertEqual(intent["key"], SEED_KEY)
        self.assertEqual(intent["text"], "Alpha rewritten.")
        self.assertEqual(intent["trust"], "working")
        self.assertEqual(intent["source"], "agent")

    # Spine D2.9, D3.2 B4/B11: the proposal carries request_id; the log
    # header names save:<id>; the log closes with the save outcome then
    # [CLOSED:].
    def test_save_log_and_proposal_record_the_request(self):
        rid = "test-log-shape-1"
        fields = self.assert_save(
            self.save(rid, MATRIX_KEY, self.key_token(MATRIX_KEY), MATRIX_TEXT, True),
            "accepted", 0,
        )
        proposal = json.loads(
            self.bdir().joinpath("proposals", fields["proposal"] + ".json")
            .read_text("utf-8")
        )
        self.assertEqual(proposal["request_id"], rid)
        logs = self.save_logs(rid)
        self.assertEqual([path.stem for path in logs], [fields["run"]])
        text = logs[0].read_text("utf-8")
        header = text.splitlines()[0]
        self.assertRegex(
            header,
            r"^# Run {} \| \S+ \| actor:\S+ \| base:V\d{{6}} \| save:{}$".format(
                fields["run"], re.escape(rid)
            ),
        )
        self.assertIn("[ID:{}-E001]".format(fields["run"]), text)
        self.assertIn("[PROPOSE:{}]".format(fields["proposal"]), text)
        self.assertIn(
            "[OUTCOME:success] save {} accepted".format(rid), text
        )
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(lines[-1].startswith("[CLOSED:{}".format(fields["run"])), lines[-1])
        self.assertEqual(self.decision(fields["proposal"])["outcome"], "accepted")

    # Spine D1.3: get --key --json carries the token per HOT record, hashed
    # with line_hash over the stripped raw hot line.
    def test_get_json_carries_hot_token(self):
        state = self.state()
        token = self.key_token(SEED_KEY)
        raw = [
            line for line in self.root.joinpath("bimri.md").read_text("utf-8").splitlines()
            if "[K:{}]".format(SEED_KEY) in line
        ]
        self.assertEqual(len(raw), 1)
        digest = hashlib.sha256(raw[0].strip().encode("utf-8")).hexdigest()
        self.assertEqual(
            token, "V{:06d}:hot:{}".format(state["head_revision"], digest)
        )

    # Spine D1.3: recall --key --json carries the same token as get.
    def test_recall_json_carries_the_same_token(self):
        result = self.cli("recall", "--key", SEED_KEY, "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(
            self.token_from_payload(payload, SEED_KEY), self.key_token(SEED_KEY)
        )

    # Spine D1.3: a miss carries "token": "V<head>:absent" and exits 1.
    def test_get_json_miss_carries_absent_token_and_exit_one(self):
        result, payload = self.get_json("nothing.here")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            payload["token"], "V{:06d}:absent".format(self.state()["head_revision"])
        )

    # Spine D3.3.4, section 03 11, Decisions 21: save --json prints the fixed
    # key set with replayed false and an integer lock_held_ms.
    def test_save_json_object_has_the_fixed_key_set(self):
        result = self.save(
            "test-json-shape-1", MATRIX_KEY, self.key_token(MATRIX_KEY),
            MATRIX_TEXT, True, json_out=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), SAVE_JSON_FIELDS)
        self.assertEqual(payload["result"], "accepted")
        self.assertEqual(payload["request"], "test-json-shape-1")
        self.assertIs(payload["replayed"], False)
        self.assertIsInstance(payload["revision"], int)
        self.assertIsInstance(payload["lock_held_ms"], int)
        self.assertIsNone(payload["error"])

    # Build step 2 (a): ensure_layout creates saves, saves/open, saves/done.
    def test_layout_creates_save_directories(self):
        for name in ("saves", "saves/open", "saves/done"):
            self.assertTrue(self.bdir().joinpath(name).is_dir(), name)


class SaveArgumentTest(V520Step2Base):
    """D1.2 / section 03 1: refusals before the lock write nothing."""

    def assert_refused_without_writing(self, arguments, message=None):
        before = protected_tree_snapshot(self.root)
        result = self.cli(*arguments, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIsNone(parse_save_line(result.stdout))
        if message is not None:
            self.assertIn(message, result.stderr)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])

    # Spine D3.1: a token outside the grammar (prefix hash, bad storage,
    # short revision, uppercase hex) is refused, exit 2, nothing written.
    def test_malformed_or_prefix_token_is_refused(self):
        head = self.state()["head_revision"]
        full = "a" * 64
        for token in (
            "V{:06d}:hot:{}".format(head, full[:16]),
            "V{:06d}:warm:{}".format(head, full),
            "V{}:absent".format(head),
            "V{:06d}:hot:{}".format(head, full.upper()),
            "V{:06d}:absent:{}".format(head, full),
        ):
            with self.subTest(token=token):
                self.assert_refused_without_writing(
                    self.save_arguments("test-bad-token-1", SEED_KEY, token, "x"),
                    TOKEN_ERROR,
                )

    # Spine D2.1 clean_request_id: ids outside ^[a-z0-9][a-z0-9._-]{0,99}$
    # after lowercasing are refused, exit 2, nothing written.
    def test_request_id_grammar_is_enforced(self):
        token = self.key_token(SEED_KEY)
        for rid in ("-lead", ".lead", "a/b", "a\\b", "a b", "x" * 101, "a:b"):
            with self.subTest(rid=rid):
                self.assert_refused_without_writing(
                    self.save_arguments(rid, SEED_KEY, token, "x"),
                    REQUEST_ERROR,
                )

    # Section 10 CROSSREF (spine D2.1 revised): Windows device stems never
    # become ticket paths; near misses are ordinary ids.
    def test_request_id_reserved_device_stems_refused(self):
        token = self.key_token(SEED_KEY)
        for rid in ("con", "nul", "aux", "com1", "lpt9", "CON.x"):
            with self.subTest(rid=rid):
                self.assert_refused_without_writing(
                    self.save_arguments(rid, SEED_KEY, token, "x"),
                    RESERVED_ERROR,
                )
        for index, rid in enumerate(("console", "com10", "nul-1")):
            with self.subTest(admitted=rid):
                key = "device.ok{}".format(index)
                result = self.save(rid, key, self.key_token(key), "Admitted.", True)
                self.assert_save(result, "accepted", 0)
                self.assertTrue(self.done_ticket_path(rid).is_file())

    # Spine D1.2: --request and --observed are required (no minted ids).
    def test_request_and_observed_are_required(self):
        token = self.key_token(SEED_KEY)
        self.assert_refused_without_writing(
            ["save", "--observed", token, "--key", SEED_KEY, "--text", "x"]
        )
        self.assert_refused_without_writing(
            ["save", "--request", "test-required-1", "--key", SEED_KEY,
             "--text", "x"]
        )

    # Section 03 1.3: --new-subject only with set.
    def test_new_subject_only_with_set(self):
        self.assert_refused_without_writing(
            self.save_arguments(
                "test-new-touch-1", SEED_KEY, self.key_token(SEED_KEY),
                extra=("--operation", "touch", "--new-subject"),
            )
        )

    # Section 03 5.3: --new-subject on an existing key is failed before B1
    # (no ticket) with the cmd_propose wording.
    def test_new_subject_on_existing_key_fails_before_the_ticket(self):
        self.assert_refused_without_writing(
            self.save_arguments(
                "test-new-existing-1", SEED_KEY, self.key_token(SEED_KEY),
                "Again.", new_subject=True,
            ),
            "already exists; omit --new-subject",
        )

    # Section 03 1.3 and D1.2 (gate 4): --source system, --trust contested
    # and confirmed agent claims are refused before any write.
    def test_system_source_contested_trust_and_agent_confirmed_are_refused(self):
        token = self.key_token(OWNER_KEY)
        for rid, extra in (
            ("test-system-1", ("--source", "system")),
            ("test-contested-1", ("--trust", "contested")),
            ("test-agent-confirmed-1",
             ("--source", "agent", "--trust", "confirmed")),
        ):
            with self.subTest(rid=rid):
                self.assert_refused_without_writing(
                    self.save_arguments(rid, OWNER_KEY, token, "x", extra=extra)
                )

    # Build step 2 (k): --settles is parsed and validated; a non-empty value
    # is refused before any write until step 6.
    def test_settles_refused_before_any_write_until_step_six(self):
        token = self.key_token(SEED_KEY)
        self.assert_refused_without_writing(
            self.save_arguments(
                "test-settles-1", SEED_KEY, token, "x",
                extra=("--settles", "R000001-Q001"),
            )
        )
        self.assert_refused_without_writing(
            self.save_arguments(
                "test-settles-2", SEED_KEY, token, "x",
                extra=("--settles", "not-a-proposal"),
            )
        )

    # Spine D1.1 / step 2 (k): start accepts --open-run (still opens a run).
    def test_start_accepts_open_run(self):
        result = self.cli("start", "--open-run", "--actor", "legacy")
        self.assertRegex(result.stdout, RUN_RE)

    # Spine D1.10 revised: recover-save --cancel requires --human-approved
    # before any lock; nothing is written.
    def test_recover_save_cancel_requires_human_approved(self):
        before = protected_tree_snapshot(self.root)
        result = self.cli(
            "recover-save", "test-cancel-flag-1", "--cancel", "--summary",
            "owner said stop", check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--human-approved", result.stderr)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])

    # Section 01 3.9: recover-save of an id with no live save marker and no
    # admitted ticket is refused, nothing written.
    def test_recover_save_of_unknown_id_is_refused(self):
        before = protected_tree_snapshot(self.root)
        result = self.cli(
            "recover-save", "test-no-such-save", "--rebuild", "--summary",
            "nothing here", check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no wedged save named test-no-such-save", result.stderr)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])


class SaveIdempotencyTest(V520Step2Base):
    """D3.3.2 identity rules and D3.3.4 replay."""

    # Spine D3.3.4 (gate 4): a replayed finished ticket prints identical
    # stdout and exits with the identical code; nothing is written.
    def test_replayed_ticket_prints_identical_stdout_and_code(self):
        for outcome in ("accepted", "held", "unchanged"):
            with self.subTest(outcome=outcome):
                root = self.fresh_store("replay-" + outcome)
                rid = "test-replay-" + outcome
                intent = INTENTS[outcome]
                first = self.save_intent(rid, intent, root=root)
                self.assert_save(first, intent["outcome"], intent["code"])
                before = protected_tree_snapshot(root)
                second = self.save_intent(rid, intent, root=root)
                self.assertEqual(
                    (second.returncode, second.stdout),
                    (first.returncode, first.stdout),
                )
                self.assertEqual(
                    changed_paths(before, protected_tree_snapshot(root)), []
                )

    # Spine D3.3.4: --json on a replay adds "replayed": true; every other
    # field equals the first answer.
    def test_json_replay_adds_replayed_true(self):
        rid = "test-replay-json-1"
        token = self.key_token(MATRIX_KEY)
        first = self.save(rid, MATRIX_KEY, token, MATRIX_TEXT, True, json_out=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = protected_tree_snapshot(self.root)
        second = self.save(rid, MATRIX_KEY, token, MATRIX_TEXT, True, json_out=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        one = json.loads(first.stdout)
        two = json.loads(second.stdout)
        self.assertIs(one["replayed"], False)
        self.assertIs(two["replayed"], True)
        for name in SAVE_JSON_FIELDS - {"replayed", "lock_held_ms"}:
            self.assertEqual(two[name], one[name], name)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])

    # Spine D3.3.2 rule 2 (gate 3): same id with different content fails
    # before any mutation, against a finished and against an admitted ticket.
    def test_same_request_different_content_fails_before_mutation(self):
        rid = "test-same-id-1"
        token = self.key_token(MATRIX_KEY)
        self.assert_save(
            self.save(rid, MATRIX_KEY, token, MATRIX_TEXT, True), "accepted", 0
        )
        before = protected_tree_snapshot(self.root)
        result = self.save(rid, MATRIX_KEY, token, "Different words.", True)
        self.assert_failed(result, rid)
        self.assertIn(rid, result.stderr)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])

        admitted = "test-same-id-2"
        admitted_token = self.crash_named(
            "save_crash_after_ticket", 121, admitted, SEED_KEY, "Admitted.",
            new_subject=False,
        )
        before = protected_tree_snapshot(self.root)
        result = self.save(admitted, SEED_KEY, admitted_token, "Other words.")
        self.assert_failed(result, admitted)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])
        self.assertEqual(self.ticket(admitted)["status"], "admitted")

    # Spine D2.1 / Decisions 1 (section 03 12): actor is outside intent_hash,
    # so the same id under another --actor replays the recorded line and code.
    def test_same_request_under_another_actor_replays(self):
        rid = "test-actor-replay-1"
        token = self.key_token(MATRIX_KEY)
        first = self.save(rid, MATRIX_KEY, token, MATRIX_TEXT, True,
                          extra=("--actor", "first"))
        self.assert_save(first, "accepted", 0)
        before = protected_tree_snapshot(self.root)
        second = self.save(rid, MATRIX_KEY, token, MATRIX_TEXT, True,
                           extra=("--actor", "second"))
        self.assertEqual((second.returncode, second.stdout), (0, first.stdout))
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])
        self.assertEqual(self.ticket(rid)["actor"], "first")

    # Spine D2.1 clean_request_id: ids are lowercased first, so
    # Sess1-Turn7 and sess1-turn7 are one request.
    def test_request_id_case_folds_to_one_request(self):
        token = self.key_token(MATRIX_KEY)
        first = self.save("Sess1-Turn7", MATRIX_KEY, token, MATRIX_TEXT, True)
        fields = self.assert_save(first, "accepted", 0)
        self.assertEqual(fields["request"], "sess1-turn7")
        second = self.save("sess1-turn7", MATRIX_KEY, token, MATRIX_TEXT, True)
        self.assertEqual((second.returncode, second.stdout), (0, first.stdout))
        self.assertEqual(len(self.save_logs("sess1-turn7")), 1)

    # Spine D3.3.2 rule 5: two ids with identical intent are two requests;
    # the second is unchanged when the key has not moved since the first.
    def test_two_ids_identical_intent_second_is_unchanged(self):
        token = self.key_token(SEED_KEY)
        self.assert_save(
            self.save("test-twin-1", SEED_KEY, token, "Twin value."), "accepted", 0
        )
        result = self.save("test-twin-2", SEED_KEY, token, "Twin value.")
        fields = self.assert_save(result, "unchanged", 0)
        self.assertEqual(fields["run"], "-")


class SaveResultTest(V520Step2Base):
    """Gate 4: the binding table (D3.1) and the six results (D3.3.1)."""

    # Spine D3.3.1 unchanged, section 03 2 table row 1 (gate 4): the same
    # value through a different request id is unchanged with run=- and
    # writes only its finished ticket; the head does not move.
    def test_unchanged_is_effect_free(self):
        rid = "test-unchanged-1"
        token = self.key_token(SEED_KEY)
        head = self.state()["head_revision"]
        logs = sorted(path.name for path in self.bdir().joinpath("log").glob("*.md"))
        before = protected_tree_snapshot(self.root)
        result = self.save(rid, SEED_KEY, token, SEED_TEXT)
        fields = self.assert_save(result, "unchanged", 0)
        self.assertEqual(
            (fields["run"], fields["proposal"], fields["decision"]), ("-", "-", "-")
        )
        self.assertEqual(fields["token"], token)
        self.assertEqual(fields["revision"], "V{:06d}".format(head))
        self.assertEqual(
            changed_paths(before, protected_tree_snapshot(self.root)),
            [".bimri/saves/done/{}.json".format(rid)],
        )
        self.assertEqual(
            sorted(path.name for path in self.bdir().joinpath("log").glob("*.md")),
            logs,
        )
        ticket = self.ticket(rid)
        for name in PLANNED_FIELDS:
            self.assertIsNone(ticket[name], name)

    # Section 03 2 table row 1 (gate 4): a touch with a current token is
    # never unchanged; it writes a new revision.
    def test_touch_with_current_token_is_never_unchanged(self):
        head = self.state()["head_revision"]
        fields = self.assert_save(
            self.save("test-touch-1", SEED_KEY, self.key_token(SEED_KEY),
                      extra=("--operation", "touch")),
            "accepted", 0,
        )
        self.assertEqual(fields["revision"], "V{:06d}".format(head + 1))
        self.assertEqual(self.state()["head_revision"], head + 1)

    # Section 03 2 table row 4: an outdated token whose request equals the
    # live value is unchanged, not stale.
    def test_outdated_token_equal_to_live_value_is_unchanged(self):
        old = self.key_token(SEED_KEY)
        self.assert_save(
            self.save("test-eq-1", SEED_KEY, old, "Moved value."), "accepted", 0
        )
        result = self.save("test-eq-2", SEED_KEY, old, "Moved value.")
        self.assert_save(result, "unchanged", 0)

    # Spine D3.3.3 / R-F (gate 4): the second writer from one observed key
    # token is told stale, writes nothing beyond its ticket, and current=
    # names the new token; re-read with a new id it is accepted.
    def test_stale_writer_is_told_stale_and_never_rebased(self):
        token = self.key_token(SEED_KEY)
        first = self.assert_save(
            self.save("test-stale-a", SEED_KEY, token, "Writer A."), "accepted", 0
        )
        before = protected_tree_snapshot(self.root)
        result = self.save("test-stale-b", SEED_KEY, token, "Writer B.")
        fields = self.assert_save(result, "stale", 1)
        self.assertEqual(fields["observed"], token)
        self.assertEqual(fields["current"], first["token"])
        self.assertEqual(
            (fields["run"], fields["proposal"], fields["decision"]), ("-", "-", "-")
        )
        self.assertEqual(
            changed_paths(before, protected_tree_snapshot(self.root)),
            [".bimri/saves/done/test-stale-b.json"],
        )
        self.assertEqual(self.current_text(SEED_KEY), "Writer A.")
        reread = self.key_token(SEED_KEY)
        self.assertEqual(reread, first["token"])
        self.assert_save(
            self.save("test-stale-b2", SEED_KEY, reread, "Writer B."),
            "accepted", 0,
        )
        self.assertEqual(self.current_text(SEED_KEY), "Writer B.")

    # Spine D3.3.3 (legacy lane untouched, test_bimri_engine.py:626): the
    # same shape through propose/sync still raises a stale-base conflict.
    def test_legacy_lane_still_raises_stale_base_conflict(self):
        late = self.start("late-writer")
        early = self.start("early-writer")
        self.propose(late, SEED_KEY, "Late legacy value.")
        self.propose(early, SEED_KEY, "Early legacy value.")
        self.cli("sync", "--run", early)
        self.cli("sync", "--run", late, check=False)
        types = [record.get("type") for record in self.conflict_records()]
        self.assertIn("stale-base", types)

    # Section 10 CROSSREF, R-F (gate 4): two writers from one observed head
    # generation; the second is stale with zero authority bytes and no
    # conflict file.
    def test_two_writers_from_one_observed_generation_second_is_stale(self):
        head = self.head_token()
        first = self.assert_save(
            self.save("test-gen-a", SEED_KEY, head, "Generation A."), "accepted", 0
        )
        conflicts = self.conflict_records()
        before = protected_tree_snapshot(self.root)
        result = self.save("test-gen-b", SEED_KEY, head, "Generation B.")
        fields = self.assert_save(result, "stale", 1)
        self.assertEqual(fields["current"], first["token"])
        self.assertEqual(fields["observed"], head)
        self.assertEqual(
            changed_paths(before, protected_tree_snapshot(self.root)),
            [".bimri/saves/done/test-gen-b.json"],
        )
        self.assertEqual(self.conflict_records(), conflicts)

    # Section 03 2 table: a current head token on a hot key proceeds.
    def test_current_head_token_proceeds(self):
        result = self.save("test-head-1", SEED_KEY, self.head_token(), "Via head.")
        self.assert_save(result, "accepted", 0)

    # Section 03 2 table: a moved head with the key unchanged since the
    # observed revision proceeds (unrelated movement).
    def test_moved_head_token_with_unrelated_movement_proceeds(self):
        head = self.head_token()
        self.assert_save(
            self.save("test-move-1", MATRIX_KEY, self.key_token(MATRIX_KEY),
                      MATRIX_TEXT, True),
            "accepted", 0,
        )
        result = self.save("test-move-2", SEED_KEY, head, "Unrelated head move.")
        self.assert_save(result, "accepted", 0)

    # Section 03 2 table, last row: a moved head token whose hash names no
    # accepted revision is failed before any write.
    def test_head_token_naming_no_revision_fails(self):
        old_head = self.state()["head_revision"]
        self.assert_save(
            self.save("test-badhead-1", MATRIX_KEY, self.key_token(MATRIX_KEY),
                      MATRIX_TEXT, True),
            "accepted", 0,
        )
        before = protected_tree_snapshot(self.root)
        result = self.save(
            "test-badhead-2", SEED_KEY,
            "V{:06d}:head:{}".format(old_head, "0" * 64), "x",
        )
        self.assert_failed(result, "test-badhead-2")
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])


class SaveSubjectTokenTest(V520Step2Base):
    """Gate 4 test_new_subject_absent_and_cold_tokens, one row per test."""

    # Section 03 2 table, absent row (gate 4): V:absent + --new-subject
    # creates the subject, and its token is the token get returns.
    def test_new_subject_absent_and_cold_tokens(self):
        token = self.key_token("fresh.subject")
        self.assertRegex(token, r"^V\d{6}:absent$")
        fields = self.assert_save(
            self.save("test-absent-1", "fresh.subject", token, "Fresh.", True),
            "accepted", 0,
        )
        self.assertEqual(self.key_token("fresh.subject"), fields["token"])

    # Section 03 2 table / hold ladder l.7794: an absent token without
    # --new-subject is held classification-required, exit 1.
    def test_absent_token_without_new_subject_is_held_for_classification(self):
        token = self.key_token("unclassified.subject")
        fields = self.assert_save(
            self.save("test-unclassified-1", "unclassified.subject", token, "x"),
            "held", 1,
        )
        self.assertEqual(fields["reason"], "classification-required")
        self.assertEqual(fields["decision"], "held")

    # Section 03 2 table, hash-differs row: an absent token on a key that
    # exists (with another value) is stale.
    def test_absent_token_on_existing_key_is_stale(self):
        token = "V{:06d}:absent".format(self.state()["head_revision"])
        fields = self.assert_save(
            self.save("test-absent-stale-1", SEED_KEY, token, "Other value."),
            "stale", 1,
        )
        self.assertEqual(fields["current"], self.key_token(SEED_KEY))

    # Section 03 2 table (attack revision): a still-absent key with a moved
    # head token proceeds.
    def test_absent_key_with_moved_head_token_proceeds(self):
        head = self.head_token()
        self.assert_save(
            self.save("test-absent-move-1", SEED_KEY, self.key_token(SEED_KEY),
                      "Head mover."),
            "accepted", 0,
        )
        self.assert_save(
            self.save("test-absent-move-2", "late.subject", head, "Late.", True),
            "accepted", 0,
        )

    # Section 03 2 table, cold row (gate 4): a cold token whose hash matches
    # the cold record is accepted and the subject is hot again.
    def test_cold_token_rehydrates_the_subject(self):
        key = "cold.subject"
        token = self.seed_cold(key)
        fields = self.assert_save(
            self.save("test-cold-set-1", key, token, "Back in the hot view."),
            "accepted", 0,
        )
        self.assertNotIn(key, self.state()["cold_current"])
        match = self.current_match(key)
        self.assertEqual(match["location"], "HOT")
        self.assertEqual(match["text"], "Back in the hot view.")
        self.assertEqual(self.key_token(key), fields["token"])

    # Section 03 2 table (gate 4, D2.10): cold touch and cold close with the
    # cold token are accepted; a close's token is V<head>:absent.
    def test_cold_touch_and_close_with_cold_token_are_accepted(self):
        key = "cold.subject"
        token = self.seed_cold(key)
        self.assert_save(
            self.save("test-cold-touch-1", key, token,
                      extra=("--operation", "touch")),
            "accepted", 0,
        )
        root = self.fresh_store("cold-close")
        token = self.seed_cold(key, root=root)
        fields = self.assert_save(
            self.save("test-cold-close-1", key, token,
                      extra=("--operation", "close"), root=root),
            "accepted", 0,
        )
        self.assertRegex(fields["token"], r"^V\d{6}:absent$")
        self.assertIsNone(self.current_match(key, root=root))

    # Section 03 2 table (L18): a cooled subject saved with a current head
    # token is stale with current=V<head>:cold:<hash>; no run.
    def test_cold_subject_with_current_head_token_is_stale(self):
        key = "cold.subject"
        cold_token = self.seed_cold(key)
        fields = self.assert_save(
            self.save("test-cold-head-1", key, self.head_token(), "New cold text."),
            "stale", 1,
        )
        self.assertEqual(fields["current"], cold_token)
        self.assertEqual(fields["run"], "-")

    # Section 03 2 table (gate 4): a cold key saved with a head token after
    # the head moved is stale.
    def test_cold_subject_with_moved_head_token_is_stale(self):
        key = "cold.subject"
        cold_token = self.seed_cold(key)
        head = self.head_token()
        self.assert_save(
            self.save("test-cold-mover-1", SEED_KEY, self.key_token(SEED_KEY),
                      "Head mover."),
            "accepted", 0,
        )
        fields = self.assert_save(
            self.save("test-cold-head-2", key, head, "New cold text."),
            "stale", 1,
        )
        # Section 03 2 (L18): current is the LIVE token, V<head>:cold:<hash>
        # at the moved head; the cold line hash is the one seeded.
        live = self.key_token(key)
        self.assertEqual(fields["current"], live)
        self.assertNotEqual(live, cold_token)
        self.assertEqual(live.split(":", 1)[1], cold_token.split(":", 1)[1])
        self.assertRegex(live, r"^V\d{6}:cold:[0-9a-f]{64}$")


class SaveAuthorityTest(V520Step2Base):
    """Gate 4: today's protections hold through save; nothing invents human
    approval."""

    # Gate 4 (engine:907 / v510:2041 ladder through save): agent set over
    # confirmed memory is held confirmed-user-authority-required, an agent
    # Tier 1 is held tier1-human-authority-required, the user/confirmed set
    # is accepted, and no record a save writes says human-asserted.
    def test_agent_claims_cannot_replace_confirmed_memory_via_save(self):
        result = self.save(
            "test-confirmed-1", OWNER_KEY, self.key_token(OWNER_KEY),
            "Agent says otherwise.",
        )
        fields = self.assert_save(result, "held", 1)
        self.assertEqual(fields["reason"], "confirmed-user-authority-required")
        self.assertIn(
            "BIMRI HELD: Current confirmed truth is unchanged.", result.stdout
        )
        self.assertEqual(self.current_text(OWNER_KEY), OWNER_TEXT)
        tier1 = self.save(
            "test-tier1-1", "agent.tier-one", self.key_token("agent.tier-one"),
            "Agent rule.", extra=("--new-subject", "--tier", "1"),
        )
        fields = self.assert_save(tier1, "held", 1)
        self.assertEqual(fields["reason"], "tier1-human-authority-required")
        accepted = self.save(
            "test-owner-1", OWNER_KEY, self.key_token(OWNER_KEY),
            "Owner changed the rule.",
            extra=("--source", "user", "--trust", "confirmed"),
        )
        self.assert_save(accepted, "accepted", 0)
        self.assertEqual(self.current_text(OWNER_KEY), "Owner changed the rule.")
        for path in self.bdir().rglob("*"):
            if path.is_file() and path.suffix in (".json", ".md", ".tsv"):
                self.assertNotIn(
                    "human-asserted", path.read_text("utf-8", errors="replace"),
                    str(path),
                )

    # Spine D3.3.1 held, section 03 8 (gate 4): held is never reported as
    # remembered: exit 1, decision=held, listed by status and search, an
    # identical request replays exit 1, get still returns the confirmed text.
    def test_held_or_conflict_never_reported_as_remembered(self):
        token = self.key_token(OWNER_KEY)
        first = self.save("test-held-1", OWNER_KEY, token, AGENT_REWRITE)
        fields = self.assert_save(first, "held", 1)
        self.assertEqual(fields["decision"], "held")
        self.assertRegex(fields["run"], r"^R\d{6}$")
        status = self.cli("status", check=False)
        self.assertIn(
            "held {} [{}]".format(fields["proposal"], OWNER_KEY), status.stdout
        )
        search = self.cli("search", "--query", AGENT_REWRITE, check=False)
        self.assertIn(fields["proposal"], search.stdout)
        second = self.save("test-held-1", OWNER_KEY, token, AGENT_REWRITE)
        self.assertEqual((second.returncode, second.stdout), (1, first.stdout))
        self.assertEqual(self.current_text(OWNER_KEY), OWNER_TEXT)

    # Section 03 8: a held save closes its engine-owned run with
    # [OUTCOME:partial] save <id> held and never enters active_runs.
    def test_held_save_closes_its_run(self):
        before = self.state()["active_runs"]
        fields = self.assert_save(
            self.save("test-held-close-1", OWNER_KEY, self.key_token(OWNER_KEY),
                      AGENT_REWRITE),
            "held", 1,
        )
        text = self.bdir().joinpath("log", fields["run"] + ".md").read_text("utf-8")
        self.assertIn("[OUTCOME:partial] save test-held-close-1 held", text)
        self.assertIn("[CLOSED:{}".format(fields["run"]), text)
        self.assertEqual(self.state()["active_runs"], before)
        self.assertEqual(self.decision(fields["proposal"])["outcome"], "held")


class SaveCrashMatrixTest(V520Step2Base):
    """Gate 3: the counter seam matrix (section 10 10.1.1) and its rows."""

    def run_matrix(self, intent_name):
        intent = INTENTS[intent_name]
        rid = "test-matrix-" + intent_name
        clean = self.clean_run(rid, intent)
        base_revisions = self.revision_numbers()
        base_archive = self.archive_rows()
        base_state = self.state()
        seen = {}

        def per_point(number, root, result, point):
            self.assertEqual(
                result.returncode, CRASH_EXIT,
                "n={}\n{}{}".format(number, result.stdout, result.stderr),
            )
            self.assertIsNotNone(point, "no crash point at n={}".format(number))
            self.assertEqual(point[0], number)
            if point[1] == "audit-manifest.json":
                archive = self.bdir(root)
                alias = (archive / "audit-manifest.json").read_bytes()
                new_hash = json.loads(alias)["manifest_hash"]
                prior_hash = self.template_witness["manifest_hash"]
                self.assertNotEqual(new_hash, prior_hash)
                self.assertEqual(self.witness(root)["manifest_hash"], prior_hash)
                generation = archive / "audit-manifests" / (new_hash + ".json")
                self.assertEqual(alias, generation.read_bytes())
            # L10 prints only after B15, so a killed process printed no line.
            self.assertIsNone(parse_save_line(result.stdout), "n={}".format(number))
            state = self.state(root)
            proposals = self.proposals_for(rid, root=root)
            logs = self.save_logs(rid, root=root)
            log_text = logs[0].read_text("utf-8") if logs else ""
            decision = self.decision(proposals[0], root=root) if proposals else None
            seen[number] = {
                "open": self.open_ticket_path(rid, root).exists(),
                "done": self.done_ticket_path(rid, root).exists(),
                "marker": self.marker_path(root).exists(),
                "proposal": bool(proposals),
                "log": bool(logs),
                "decision": None if decision is None else decision["outcome"],
                "revisions": len(self.revision_numbers(root=root)),
                "head": state["head_revision"],
                "epoch": state["_audit_epoch"],
                "outcome_line": "[OUTCOME:" in log_text,
                "closed_line": "[CLOSED:" in log_text,
            }
            retry = self.save_intent(rid, intent, root=root)
            self.assertEqual(
                (retry.returncode, retry.stdout),
                (clean.returncode, clean.stdout),
                "n={}\n{}".format(number, retry.stderr),
            )
            self.assert_exactly_once(
                root, rid, intent, base_revisions, base_archive
            )
            self.assert_byte_free_replay(root, rid, intent, clean)

        labels = self.enumerate_points(
            "save_crash_after_write_", rid, intent, per_point
        )
        return rid, labels, seen, base_state, base_revisions

    # Spine D3.2 write order, D3.4.4 ladder, crash-first 3.5 (gate 3), the
    # accepted intent: every boundary exits 150 with no result line, the
    # on-disk row matches the boundary that wrote last, the retry by identity
    # prints the clean stdout once, and later retries are byte-free replays.
    def test_save_crash_matrix_retries_exactly_once(self):
        rid, labels, seen, base_state, base_revisions = self.run_matrix("accepted")
        positions, _ = match_accepted_boundaries(labels, rid)
        for number, facts in sorted(seen.items()):
            with self.subTest(n=number, label=labels[number - 1]):
                self.assertEqual(facts["open"], number < positions["B12b"])
                self.assertEqual(facts["done"], number >= positions["B12a"])
                self.assertEqual(
                    facts["marker"],
                    positions["B2a"] <= number < positions.get("B15", 10 ** 6),
                )
                self.assertEqual(facts["proposal"], number >= positions["B3"])
                self.assertEqual(facts["log"], number >= positions["B4"])
                if number < positions["B5"]:
                    self.assertIsNone(facts["decision"])
                elif number < positions["B9"]:
                    self.assertEqual(facts["decision"], "applying")
                else:
                    self.assertEqual(facts["decision"], "accepted")
                self.assertEqual(
                    facts["revisions"],
                    len(base_revisions) + (1 if number >= positions["B7"] else 0),
                )
                self.assertEqual(
                    facts["head"],
                    base_state["head_revision"]
                    + (1 if number >= positions["B8a"] else 0),
                )
                self.assertEqual(
                    facts["epoch"] == base_state["_audit_epoch"],
                    number < positions["B2b"],
                )
                self.assertEqual(facts["outcome_line"], number >= positions["B11a"])
                self.assertEqual(facts["closed_line"], number >= positions["B11b"])

    # Gate 3 matrix, held intent: every boundary resumes to the same held
    # answer (exit 1) with one run, one proposal, one held decision and no
    # revision; the held save never writes a revision or the hot view.
    def test_save_crash_matrix_retries_exactly_once_held_intent(self):
        rid, labels, seen, base_state, _ = self.run_matrix("held")
        head = [
            r"saves/open/{}\.json".format(re.escape(rid)),
            r"audit-transition\.json",
            r"state\.json",
            r"proposals/R\d{6}-Q001\.json",
            r"log/R\d{6}\.md",
            r"decisions/R\d{6}-Q001\.json",
        ]
        for index, pattern in enumerate(head):
            self.assertRegex(labels[index], "^" + pattern + "$", labels)
        for label in labels:
            self.assertFalse(label.startswith("revisions/"), labels)
            self.assertNotEqual(label, "bimri.md", labels)
        for number, facts in seen.items():
            self.assertEqual(facts["head"], base_state["head_revision"], number)
            self.assertIn(facts["decision"], (None, "held"), number)

    # Gate 3 matrix, unchanged intent: the only durable boundary is the
    # finished ticket (section 10 10.1.1 "stale/unchanged: 1 as done/ only");
    # the retry replays the clean answer.
    def test_save_crash_matrix_retries_exactly_once_unchanged_intent(self):
        rid, labels, _, _, _ = self.run_matrix("unchanged")
        self.assertEqual(labels, ["saves/done/{}.json".format(rid)])

    # Section 10 10.1.1 (gate 3): the counter seam records the boundary
    # table exactly, on every Python (deletions count only through
    # remove_protected_file, never a patched os.unlink).
    def test_crash_matrix_boundaries_identical_across_python_versions(self):
        rid, labels = self.accepted_labels()
        positions, tail = match_accepted_boundaries(labels, rid)
        self.assertEqual(positions["B6*"], [])
        witness = self.template_witness
        self.assertEqual(
            sorted(tail),
            sorted([
                "audit-transition.json",
                "audit-manifests/{}.json".format(witness["manifest_hash"]),
                "audit-manifests/facts-{}.json".format(witness["facts_hash"]),
            ]),
        )

    # Spine R-E, D3.4.4 R0a/R0b (gate 3): with the ticket deleted after every
    # boundary from B2a to B14d, one get clears B2a as finished-before-
    # mutation, cancels B2b (R0b), and rebuilds B3..B14d (R0a); a retry by id
    # never makes a second effect.
    def test_matrix_with_the_ticket_deleted_after_every_boundary(self):
        rid = "test-deleted-1"
        intent = INTENTS["accepted"]
        base_revisions = self.revision_numbers()
        base_archive = self.archive_rows()
        base_state = self.state()
        recorded = []

        def per_point(number, root, result, point):
            self.assertEqual(result.returncode, CRASH_EXIT, result.stderr)
            recorded.append(point[1])
            labels = list(recorded)
            if number == 1 or "audit-witness.json" in labels[:-1]:
                return
            first_marker = labels.index("audit-transition.json") + 1
            has_proposal = any(label.startswith("proposals/") for label in labels)
            receipts = len(self.drift_receipts(root))
            for path in (self.open_ticket_path(rid, root),
                         self.done_ticket_path(rid, root)):
                if path.exists():
                    path.unlink()
            got = self.cli("get", "--key", MATRIX_KEY, "--json", root=root,
                           check=False)
            self.assertIn(got.returncode, (0, 1), got.stdout + got.stderr)
            self.assertFalse(self.marker_path(root).exists(), "n={}".format(number))
            self.assertEqual(len(self.drift_receipts(root)), receipts)
            if number == first_marker:
                # B2a: finished-before-mutation; nothing else happened.
                self.assertEqual(self.proposals_for(rid, root=root), [])
                self.assertEqual(self.save_logs(rid, root=root), [])
                self.assertFalse(self.done_ticket_path(rid, root).exists())
                self.assertEqual(
                    self.state(root)["_audit_epoch"], base_state["_audit_epoch"]
                )
            elif not has_proposal:
                # B2b: R0b cancel, witness resealed with the new epoch.
                self.assertEqual(self.proposals_for(rid, root=root), [])
                self.assertEqual(self.save_logs(rid, root=root), [])
                witness = self.witness(root)
                self.assertEqual(witness["audit_origin"], "lifecycle")
                self.assertEqual(
                    witness["audit_epoch"], self.state(root)["_audit_epoch"]
                )
                self.assertIsNone(self.current_text(MATRIX_KEY, root=root))
            else:
                # B3..B14d: R0a rebuilt the ticket (or the frozen completion
                # republished) and the save is finished, once.
                self.assertEqual(
                    self.current_text(MATRIX_KEY, root=root), MATRIX_TEXT
                )
                done = self.done_ticket_path(rid, root)
                if not any(label.startswith("saves/done/") for label in labels):
                    self.assertTrue(done.exists(), "n={}".format(number))
                if done.exists():
                    ticket = json.loads(done.read_text("utf-8"))
                    self.assertEqual(ticket["result"]["outcome"], "accepted")
                self.assertEqual(len(self.save_logs(rid, root=root)), 1)
                self.assertEqual(
                    len(self.revision_numbers(root=root)), len(base_revisions) + 1
                )
            self.assertEqual(self.open_tickets(root), [])
            self.assert_doctor_read_only_passed(root)
            retry = self.save_intent(rid, intent, root=root)
            self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            self.assertIn(
                parse_save_line(retry.stdout)["result"], ("accepted", "unchanged")
            )
            self.assert_exactly_once(root, rid, intent, base_revisions, base_archive)

        self.enumerate_points("save_crash_after_write_", rid, intent, per_point)

    # Section 10 10.1.3, gate 8 shape (gate 3): ENOSPC at every counted write
    # never prints a result line; the retry by id completes once with the
    # clean stdout; later retries replay byte-free.
    def test_full_disk_at_every_boundary_resumes(self):
        rid = "test-enospc-1"
        intent = INTENTS["accepted"]
        clean = self.clean_run(rid, intent)
        base_revisions = self.revision_numbers()
        base_archive = self.archive_rows()
        counted = []

        def per_point(number, root, result, point):
            self.assertEqual(
                result.returncode, 2,
                "n={}\n{}{}".format(number, result.stdout, result.stderr),
            )
            self.assertNotIn("BIMRI SAVE", result.stdout)
            self.assertIn(NO_SPACE, result.stderr)
            counted.append(number)
            retry = self.save_intent(rid, intent, root=root)
            self.assertEqual(
                (retry.returncode, retry.stdout),
                (clean.returncode, clean.stdout),
                "n={}\n{}".format(number, retry.stderr),
            )
            self.assert_exactly_once(root, rid, intent, base_revisions, base_archive)
            self.assert_byte_free_replay(root, rid, intent, clean)

        self.enumerate_points("save_fail_enospc_at_", rid, intent, per_point)
        # Removes are never failed (10.1.3), so the writes alone are counted:
        # at least B1-B5, B7-B9, B11a-B12a, B13, B14a-d.
        self.assertGreaterEqual(len(counted), 15)


class SaveResumeTest(V520Step2Base):
    """D3.4 recovery: who finishes what, and the named rows."""

    # Spine D3.4.1 (section 10 CROSSREF): a live save marker left at B2a,
    # B3, B5 or B9 is finished by every reconciling invocation (get, start,
    # status, doctor); the later retry by id answers once.
    def test_matrix_resumed_by_every_reconciling_reader(self):
        intent = INTENTS["accepted"]
        boundaries = (
            ("b2a", "save_crash_after_marker_before_epoch", 122),
            ("b3", "save_crash_after_proposal", 124),
            ("b5", "save_crash_after_applying", 126),
            ("b9", "save_crash_after_final_decision", 129),
        )
        readers = (
            ("get", ("get", "--key", MATRIX_KEY, "--json")),
            ("start", tuple(self.start_arguments()) + ("--actor", "reader")),
            ("status", ("status",)),
            ("doctor", ("doctor",)),
        )
        clean = self.clean_run("test-reader", intent)
        for boundary, mode, code in boundaries:
            for name, arguments in readers:
                with self.subTest(boundary=boundary, reader=name):
                    rid = "test-reader"
                    root = self.fresh_store("reader-{}-{}".format(boundary, name))
                    base_revisions = self.revision_numbers(root)
                    base_archive = self.archive_rows(root)
                    self.crash_named(
                        mode, code, rid, MATRIX_KEY, MATRIX_TEXT, root=root,
                        observed=self.template_tokens[MATRIX_KEY],
                    )
                    self.assertTrue(self.marker_path(root).exists())
                    result = self.cli(*arguments, root=root, check=False)
                    self.assertIn(
                        result.returncode, (0, 1), result.stdout + result.stderr
                    )
                    self.assertFalse(self.marker_path(root).exists())
                    if boundary == "b2a" and name != "doctor":
                        # Ticket-only after the before-mutation clear (D3.4.2).
                        self.assertTrue(self.open_ticket_path(rid, root).exists())
                    else:
                        ticket = json.loads(
                            self.done_ticket_path(rid, root).read_text("utf-8")
                        )
                        self.assertEqual(ticket["result"]["outcome"], "accepted")
                        self.assertEqual(
                            self.current_text(MATRIX_KEY, root=root), MATRIX_TEXT
                        )
                    retry = self.save_intent(rid, intent, root=root)
                    if boundary == "b2a" and name == "start":
                        # start --open-run took the planned run (R1 re-plan).
                        fields = self.assert_save(retry, "accepted", 0)
                        self.assertNotEqual(
                            fields["run"], parse_save_line(clean.stdout)["run"]
                        )
                    else:
                        self.assertEqual(
                            (retry.returncode, retry.stdout),
                            (clean.returncode, clean.stdout),
                        )
                    self.assert_exactly_once(
                        root, rid, intent, base_revisions, base_archive
                    )

    # Section 10 10.1 table: doctor --read-only never reconciles and
    # finishes nothing.
    def test_read_only_doctor_finishes_nothing(self):
        rid = "test-ro-doctor-1"
        self.crash_named("save_crash_after_applying", 126, rid, MATRIX_KEY, MATRIX_TEXT)
        before = protected_tree_snapshot(self.root)
        self.cli("doctor", "--read-only", check=False)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])

    # Spine D3.4.2, LD9: a ticket-only save (B1, no marker) is reported by
    # start and status, is not finished by reads, and leaves the witness
    # valid.
    def test_ticket_only_save_is_reported_and_not_finished_by_reads(self):
        rid = "test-ticket-only-1"
        self.crash_named("save_crash_after_ticket", 121, rid, MATRIX_KEY, MATRIX_TEXT)
        self.assertTrue(self.open_ticket_path(rid).exists())
        self.assertFalse(self.marker_path().exists())
        witness_bytes = self.bdir().joinpath("audit-witness.json").read_bytes()
        for _ in range(2):
            self.cli("get", "--key", MATRIX_KEY, "--json", check=False)
        self.assertEqual(
            self.bdir().joinpath("audit-witness.json").read_bytes(), witness_bytes
        )
        started = self.cli(*self.start_arguments(), "--actor", "reader")
        self.assertRegex(
            started.stdout, UNFINISHED_SAVE_LINE.format(rid=re.escape(rid))
        )
        status = self.cli("status", check=False)
        self.assertIn("Unfinished saves: 1", status.stdout)
        self.assertRegex(
            status.stdout, UNFINISHED_SAVE_LINE.format(rid=re.escape(rid))
        )
        self.assertTrue(self.open_ticket_path(rid).exists())
        self.assertEqual(self.save_logs(rid), [])

    # Spine D3.4.2, build step 2 (j): doctor --read-only reports an admitted
    # ticket as a warning (names the id) and still passes.
    def test_read_only_doctor_warns_on_admitted_ticket(self):
        rid = "test-ticket-warn-1"
        self.crash_named("save_crash_after_ticket", 121, rid, MATRIX_KEY, MATRIX_TEXT)
        result = self.assert_doctor_read_only_passed()
        self.assertIn(rid, result.stdout)
        self.assertTrue(self.open_ticket_path(rid).exists())

    # Spine D3.4.2 R1: the next save of another id finishes a ticket-only
    # save first, then its own.
    def test_next_save_finishes_a_ticket_only_save(self):
        rid = "test-r1-1"
        self.crash_named("save_crash_after_ticket", 121, rid, MATRIX_KEY, MATRIX_TEXT)
        result = self.save("test-r1-2", SEED_KEY, self.key_token(SEED_KEY), "Other.")
        self.assert_save(result, "accepted", 0)
        self.assertFalse(self.open_ticket_path(rid).exists())
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(ticket["result"]["outcome"], "accepted")
        self.assertEqual(len(self.save_logs(rid)), 1)
        self.assertEqual(self.current_text(MATRIX_KEY), MATRIX_TEXT)

    # Spine D3.4.2: doctor finishes a ticket-only save.
    def test_doctor_finishes_a_ticket_only_save(self):
        rid = "test-r1-doctor-1"
        self.crash_named("save_crash_after_ticket", 121, rid, MATRIX_KEY, MATRIX_TEXT)
        self.cli("doctor", check=False)
        self.assertFalse(self.open_ticket_path(rid).exists())
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(ticket["result"]["outcome"], "accepted")

    # Spine R-A, D3.4.4 R1 (gate 3): a legacy start that takes the planned
    # run id after B1 makes the retry re-plan onto R+1; the ticket is
    # rewritten with the new ids and the legacy log is byte-unchanged.
    def test_planned_run_id_squatted_by_legacy_start_is_replanned(self):
        rid = "test-squat-1"
        observed = self.crash_named(
            "save_crash_after_ticket", 121, rid, MATRIX_KEY, MATRIX_TEXT
        )
        planned = self.ticket(rid)["run_id"]
        squatter = self.start("legacy-squatter")
        self.assertEqual(squatter, planned)
        legacy_log = self.bdir() / "log" / "{}.md".format(planned)
        legacy_bytes = legacy_log.read_bytes()
        fields = self.assert_save(
            self.save(rid, MATRIX_KEY, observed, MATRIX_TEXT, True), "accepted", 0
        )
        replanned = "R{:06d}".format(int(planned[1:]) + 1)
        self.assertEqual(fields["run"], replanned)
        self.assertEqual(legacy_log.read_bytes(), legacy_bytes)
        self.assertEqual([path.stem for path in self.save_logs(rid)], [replanned])
        ticket = self.ticket(rid)
        self.assertEqual(ticket["run_id"], replanned)
        self.assertEqual(ticket["proposal_id"], replanned + "-Q001")
        self.assertEqual(ticket["entry_id"], replanned + "-E001")

    # Crash-first 3.5 row B2a, section 10.4 (gate 3): a crash between the
    # marker and the epoch bump is a clean cancel: the next get clears the
    # marker with no drift receipt, the ticket stays admitted, and the next
    # save of another id finishes it from B1 (R1) and then runs its own.
    def test_crash_between_marker_and_epoch_is_a_clean_cancel(self):
        rid = "test-b2a-1"
        epoch = self.state()["_audit_epoch"]
        receipts = len(self.drift_receipts())
        self.crash_named(
            "save_crash_after_marker_before_epoch", 122, rid, MATRIX_KEY,
            MATRIX_TEXT,
        )
        self.assertTrue(self.marker_path().exists())
        self.assertEqual(self.state()["_audit_epoch"], epoch)
        self.cli("get", "--key", SEED_KEY, "--json")
        self.assertFalse(self.marker_path().exists())
        self.assertEqual(len(self.drift_receipts()), receipts)
        self.assertEqual(self.ticket(rid)["status"], "admitted")
        self.assertTrue(self.open_ticket_path(rid).exists())
        other = self.save("test-b2a-2", SEED_KEY, self.key_token(SEED_KEY),
                          "Other save.")
        self.assert_save(other, "accepted", 0)
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(ticket["result"]["outcome"], "accepted")
        self.assertEqual(self.current_text(MATRIX_KEY), MATRIX_TEXT)
        self.assertEqual(len(self.save_logs(rid)), 1)
        self.assert_doctor_read_only_passed()

    # Spine D3.3.3 / L17 (gate 3): a retry never rolls back a later change:
    # crash after the final decision on K, get finishes and shows V+1, a
    # second request lands V+2, the retry replays V+1, get still shows V+2.
    def test_retry_never_rolls_back_a_later_change(self):
        rid = "test-rollback-a"
        head = self.state()["head_revision"]
        observed = self.crash_named(
            "save_crash_after_final_decision", 129, rid, SEED_KEY, "Value A.",
            new_subject=False,
        )
        token = self.key_token(SEED_KEY)
        self.assertTrue(token.startswith("V{:06d}:hot:".format(head + 1)), token)
        self.assertEqual(self.current_text(SEED_KEY), "Value A.")
        second = self.assert_save(
            self.save("test-rollback-b", SEED_KEY, token, "Value B."), "accepted", 0
        )
        self.assertEqual(second["revision"], "V{:06d}".format(head + 2))
        retry = self.save(rid, SEED_KEY, observed, "Value A.")
        fields = self.assert_save(retry, "accepted", 0)
        self.assertEqual(fields["revision"], "V{:06d}".format(head + 1))
        self.assertEqual(fields["token"], token)
        self.assertEqual(self.state()["head_revision"], head + 2)
        self.assertEqual(self.current_text(SEED_KEY), "Value B.")
        history = self.cli("recall", "--key", SEED_KEY, "--history", check=False)
        self.assertIn("Value A.", history.stdout)
        self.assertIn("Value B.", history.stdout)

    # Section 10 CROSSREF (04 4.4-4.5 P2/P3, step-2 form): for every crash
    # from B3 to B12b, a get-finished root and a retry-finished sibling of
    # the same crash publish the same manifest path set, pass doctor
    # --read-only, serve a warm exact read with no full audit, advance the
    # facts run watermark, and add no drift receipt.
    def test_resumed_save_publishes_the_disk_not_the_process(self):
        rid, labels = self.accepted_labels()
        positions, _ = match_accepted_boundaries(labels, rid)
        intent = INTENTS["accepted"]
        for number in range(positions["B3"], positions["B12b"] + 1):
            with self.subTest(n=number, label=labels[number - 1]):
                root = self.fresh_store("disk-{}".format(number))
                result = self.save_worker(
                    "save_crash_after_write_{}".format(number), rid, MATRIX_KEY,
                    self.template_tokens[MATRIX_KEY], MATRIX_TEXT, True,
                    root=root,
                )
                self.assertEqual(result.returncode, CRASH_EXIT, result.stderr)
                self.crash_point(root)
                sibling = self.workspace / "disk-{}-retry".format(number)
                copy_store(root, sibling)
                receipts = len(self.drift_receipts(root))
                self.cli("get", "--key", MATRIX_KEY, "--json", root=root)
                retry = self.save_intent(rid, intent, root=sibling)
                fields = self.assert_save(retry, "accepted", 0)
                for finished in (root, sibling):
                    self.assertFalse(self.marker_path(finished).exists())
                    self.assert_manifest_bound(finished)
                    self.assert_doctor_read_only_passed(finished)
                    self.assertEqual(len(self.drift_receipts(finished)), receipts)
                    facts = json.loads(
                        self.bdir(finished).joinpath(
                            "audit-manifests",
                            "facts-{}.json".format(self.witness(finished)["facts_hash"]),
                        ).read_text("utf-8")
                    )
                    self.assertEqual(
                        facts["watermarks"]["run"], int(fields["run"][1:])
                    )
                    warm = self.worker(
                        "valid_witness_forbid_full_audit", "get", "--key",
                        MATRIX_KEY, "--json", root=finished,
                    )
                    self.assertEqual(warm.returncode, 0, warm.stdout + warm.stderr)
                self.assertEqual(self.manifest_paths(root), self.manifest_paths(sibling))

    # Section 10 CROSSREF (02 2.4, L17): a crash after the B13 completion
    # seal, after B14a, or before the witness replace (110) is republished
    # from the frozen completion: the witness equals the frozen one, its
    # facts generation is the only one, and no drift receipt is written.
    def test_crash_after_completion_seal_republishes_the_frozen_checkpoint(self):
        rid, labels = self.accepted_labels()
        positions, _ = match_accepted_boundaries(labels, rid)
        crashes = (
            ("b13", "save_crash_after_write_{}".format(positions["B13"]), CRASH_EXIT),
            ("b14a", "save_crash_after_write_{}".format(positions["B14a"]), CRASH_EXIT),
            ("b14d", "witness_crash_before_replace", 110),
        )
        for name, mode, code in crashes:
            with self.subTest(boundary=name):
                root = self.fresh_store("seal-" + name)
                receipts = len(self.drift_receipts(root))
                self.crash_named(
                    mode, code, rid, MATRIX_KEY, MATRIX_TEXT, root=root,
                    observed=self.template_tokens[MATRIX_KEY],
                )
                self.crash_point(root)
                marker = json.loads(self.marker_path(root).read_text("utf-8"))
                completion = marker["scope"]["completion"]
                self.assertIsNotNone(completion)
                frozen = completion["witness"]
                # A later clock: a fresh now_iso read would change the bytes.
                time.sleep(1.1)
                self.cli("get", "--key", MATRIX_KEY, "--json", root=root)
                self.assertFalse(self.marker_path(root).exists())
                self.assertEqual(self.witness(root), frozen)
                facts = [
                    item for item in self.generation_names(root)
                    if item.startswith("facts-")
                ]
                self.assertEqual(facts, ["facts-{}.json".format(frozen["facts_hash"])])
                record = json.loads(
                    self.bdir(root).joinpath("audit-manifests", facts[0])
                    .read_text("utf-8")
                )
                self.assertEqual(record["created_at"], frozen["created_at"])
                self.assertEqual(len(self.drift_receipts(root)), receipts)
                self.assert_doctor_read_only_passed(root)

    # Section 10 CROSSREF, spine D2.1 revised (L7/L12): a resume writes the
    # planned bytes: after a crash at B2b, a later process writes Q and the
    # log with created_at/header == planned_at and hashes equal to the
    # ticket's; the save then finishes with no R0c.
    def test_resume_reproduces_planned_bytes_from_planned_at(self):
        rid = "test-planned-bytes-1"
        observed = self.crash_named(
            "save_crash_after_epoch", 123, rid, MATRIX_KEY, MATRIX_TEXT
        )
        ticket = self.ticket(rid)
        self.assertEqual(self.proposals_for(rid), [])
        time.sleep(1.1)
        result = self.save_worker(
            "save_crash_after_log", rid, MATRIX_KEY, observed, MATRIX_TEXT, True
        )
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        proposal_path = self.bdir() / "proposals" / "{}.json".format(ticket["proposal_id"])
        self.assertEqual(
            hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
            ticket["proposal_sha256"],
        )
        self.assertEqual(
            json.loads(proposal_path.read_text("utf-8"))["created_at"],
            ticket["planned_at"],
        )
        marker = json.loads(self.marker_path().read_text("utf-8"))
        self.assertEqual(
            marker["scope"]["proposal_hashes"][ticket["proposal_id"]],
            ticket["proposal_sha256"],
        )
        log_path = self.bdir() / "log" / "{}.md".format(ticket["run_id"])
        self.assertEqual(
            hashlib.sha256(log_path.read_bytes()).hexdigest(), ticket["log_sha256"]
        )
        self.assertIn(ticket["planned_at"], log_path.read_text("utf-8").splitlines()[0])
        finished = self.save(rid, MATRIX_KEY, observed, MATRIX_TEXT, True)
        self.assert_save(finished, "accepted", 0)
        self.assertFalse(self.marker_path().exists())


class SaveGuardTest(V520Step2Base):
    """D4.4 L9 observed-set guard and its owner repair."""

    # Spine D4.4 L9 guard, D1.10 revised (section 10 CROSSREF): an undeclared
    # protected write is a hard error, not a publication: rc 2, marker stays,
    # no new witness or facts generation, doctor names the path; --cancel is
    # refused past the effect and --rebuild finishes the real outcome.
    def test_undeclared_protected_write_is_a_hard_error_not_a_publication(self):
        rid = "test-undeclared-1"
        witness_bytes = self.bdir().joinpath("audit-witness.json").read_bytes()
        generations = self.generation_names()
        receipts = len(self.drift_receipts())
        result = self.save_worker(
            "save_write_undeclared_path", rid, MATRIX_KEY,
            self.key_token(MATRIX_KEY), MATRIX_TEXT, True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIsNone(parse_save_line(result.stdout))
        self.assertTrue(self.marker_path().exists())
        self.assertEqual(len(self.proposals_for(rid)), 1)
        self.assertEqual(len(self.save_logs(rid)), 1)
        self.assertEqual(
            self.bdir().joinpath("audit-witness.json").read_bytes(), witness_bytes
        )
        self.assertEqual(self.generation_names(), generations)
        doctor = self.cli("doctor", "--read-only", check=False)
        self.assertNotEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("recovery/x.bin", doctor.stdout + doctor.stderr)
        before = protected_tree_snapshot(self.root)
        refused = self.cli(
            "recover-save", rid, "--cancel", "--summary", "too late",
            "--human-approved", check=False,
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(ALREADY_REACHED.format(rid=rid), refused.stderr)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])
        rebuilt = self.cli(
            "recover-save", rid, "--rebuild", "--summary", "finish it", check=False
        )
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
        fields = parse_save_line(rebuilt.stdout)
        self.assertEqual((fields["result"], fields["decision"]), ("accepted", "accepted"))
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(ticket["result"]["outcome"], "accepted")
        self.assertNotIn("[OUTCOME:fail]", self.save_logs(rid)[0].read_text("utf-8"))
        self.assertFalse(self.marker_path().exists())
        self.assert_save(
            self.save("test-undeclared-2", SEED_KEY, self.key_token(SEED_KEY),
                      "Next write."),
            "accepted", 0,
        )
        self.assertEqual(len(self.drift_receipts()), receipts + 1)

    # Section 04 4.5 foreign-log rule (section 10 CROSSREF): an append to a
    # foreign legacy run log during a save refuses publication; the marker
    # stays and the witness and facts generations are unchanged.
    def test_foreign_run_log_append_refuses_publication(self):
        if not worker_has_mode("save_append_foreign_log"):
            self.skipTest("crash_worker has no save_append_foreign_log mode yet")
        rid = "test-foreign-1"
        witness_bytes = self.bdir().joinpath("audit-witness.json").read_bytes()
        generations = self.generation_names()
        result = self.save_worker(
            "save_append_foreign_log", rid, MATRIX_KEY,
            self.key_token(MATRIX_KEY), MATRIX_TEXT, True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIsNone(parse_save_line(result.stdout))
        self.assertRegex(result.stderr, r"\.bimri[/\\]log[/\\]R\d{6}\.md")
        self.assertTrue(self.marker_path().exists())
        self.assertEqual(
            self.bdir().joinpath("audit-witness.json").read_bytes(), witness_bytes
        )
        self.assertEqual(self.generation_names(), generations)
        refused = self.cli(
            "recover-save", rid, "--cancel", "--summary", "too late",
            "--human-approved", check=False,
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(ALREADY_REACHED.format(rid=rid), refused.stderr)
        rebuilt = self.cli(
            "recover-save", rid, "--rebuild", "--summary", "finish it", check=False
        )
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
        self.assertFalse(self.marker_path().exists())
        doctor = self.cli("doctor", check=False)
        self.assertNotIn("governance-relevant run facts differ", doctor.stdout)


class SaveWedgeTest(V520Step2Base):
    def test_new_chat_reads_accepted_head_beside_unrepairable_save(self):
        rid = "brief-interrupted"
        self.wedge_by_truncation("save_crash_after_log", 125, rid)
        state = self.state()
        token = f"V{state['head_revision']:06d}:head:{state['head_hash']}"
        before = protected_tree_snapshot(self.root)
        for command in (("start",), ("hook-start",)):
            with self.subTest(command=command):
                brief = self.cli(*command, input_text="{}")
                self.assertIn("=== BIMRI BRIEF read", brief.stdout)
                self.assertIn("Observed: " + token, brief.stdout)
                self.assertIn("AUTHORITY RECOVERY NEEDED", brief.stdout)
                self.assertIn("save " + rid + " is wedged", brief.stdout)
                self.assertNotIn("the next save or doctor finishes it", brief.stdout)
                self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])
        # In a damaged store recall can refuse because it validates proposal
        # history. The brief still directs the agent to the accepted hot view.
        hot = (self.root / "bimri.md").read_bytes()
        self.assertEqual(hashlib.sha256(hot).hexdigest(), state["head_hash"])
        self.assertIn(SEED_TEXT, hot.decode("utf-8"))
        self.assertNotIn(MATRIX_TEXT, hot.decode("utf-8"))

    """R0c wedge, D3.5 lifecycle refusal, D1.10 recover-save."""

    # Spine D3.4.4 R0c, D1.10 revised (section 10 CROSSREF): after a crash at
    # B4 with the proposal truncated, save of another id and propose name the
    # wedge, doctor prints it as an error, --cancel is refused naming the
    # damaged proposal, and after quarantine-authority --cancel closes the log
    # [OUTCOME:fail], finishes the ticket failed, and the next save works.
    def test_wedged_save_is_named_and_repaired_by_owner_word(self):
        legacy = self.start("legacy-open")
        rid = "test-wedge-1"
        proposal_id, _, _, _ = self.wedge_by_truncation(
            "save_crash_after_log", 125, rid
        )
        wedged = WEDGED_LINE.format(rid=re.escape(rid))
        other = self.save("test-wedge-other", SEED_KEY,
                          self.template_tokens[SEED_KEY], "x")
        self.assertEqual(other.returncode, 2, other.stdout + other.stderr)
        self.assertRegex(other.stderr, wedged)
        proposed = self.cli(
            "propose", "--run", legacy, "--key", SEED_KEY, "--text", "y",
            "--source", "agent", "--trust", "working", check=False,
        )
        self.assertEqual(proposed.returncode, 2, proposed.stdout + proposed.stderr)
        self.assertRegex(
            proposed.stderr,
            "(?:{}|{})".format(wedged, LIFECYCLE_REFUSAL.format(rid=re.escape(rid))),
        )
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        self.assertRegex(doctor.stdout, wedged)
        before = protected_tree_snapshot(self.root)
        refused = self.cli(
            "recover-save", rid, "--cancel", "--summary", "owner said stop",
            "--human-approved", check=False,
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(
            "proposal {} does not match its sealed hash".format(proposal_id),
            refused.stderr,
        )
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])
        self.cli(
            "quarantine-authority", "--kind", "proposal", "--id", proposal_id,
            "--human-approved",
        )
        cancelled = self.cli(
            "recover-save", rid, "--cancel", "--summary", "owner said stop",
            "--human-approved", check=False,
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stdout + cancelled.stderr)
        self.assertIn(
            "BIMRI: save {} cancelled; nothing was remembered.".format(rid),
            cancelled.stdout,
        )
        log_text = self.save_logs(rid)[0].read_text("utf-8")
        self.assertIn("[OUTCOME:fail] owner said stop", log_text)
        lines = [line for line in log_text.splitlines() if line.strip()]
        self.assertTrue(lines[-1].startswith("[CLOSED:"), lines[-1])
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(ticket["result"]["outcome"], "failed")
        self.assertFalse(self.marker_path().exists())
        self.assertIsNone(self.current_text(MATRIX_KEY))
        self.assert_save(
            self.save("test-wedge-after", SEED_KEY, self.key_token(SEED_KEY), "After."),
            "accepted", 0,
        )

    # Spine D1.10 (--rebuild = R0a forced): the intact-proposal variant of a
    # wedged save resumes to its real outcome, accepted, exit 0.
    def test_rebuild_on_intact_proposal_resumes_to_accepted(self):
        rid = "test-wedge-rebuild-1"
        _, path, original, _ = self.wedge_by_truncation(
            "save_crash_after_log", 125, rid, delete_ticket=True
        )
        path.write_bytes(original)
        rebuilt = self.cli(
            "recover-save", rid, "--rebuild", "--summary", "owner restored it",
            check=False,
        )
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
        self.assertRegex(
            rebuilt.stdout,
            r"(?m)^BIMRI SAVE accepted request={} ".format(re.escape(rid)),
        )
        self.assertFalse(self.marker_path().exists())
        self.assertEqual(self.current_text(MATRIX_KEY), MATRIX_TEXT)

    # Spine D1.10 revised case A (section 10 CROSSREF): before the effect
    # (applying decision, head unmoved) --cancel without the flag writes
    # nothing; with it the applying decision becomes abandoned, the log is
    # closed [OUTCOME:fail], the ticket is failed, the old value stands and
    # the abandoned ticket replays failed. (The wedge here is a truncated
    # proposal quarantined by the owner; a quarantine stub stays a doctor
    # blocker, so the spec's doctor --live pass belongs to its dependency-byte
    # variant, not asserted here.)
    def test_recover_save_cancel_before_effect_abandons_the_applying_decision(self):
        rid = "test-cancel-early-1"
        proposal_id, _, _, _ = self.wedge_by_truncation(
            "save_crash_after_applying", 126, rid, key=SEED_KEY,
            text="Never applied.", new_subject=False,
        )
        self.assertEqual(self.decision(proposal_id)["outcome"], "applying")
        self.cli(
            "quarantine-authority", "--kind", "proposal", "--id", proposal_id,
            "--human-approved",
        )
        before = protected_tree_snapshot(self.root)
        unflagged = self.cli(
            "recover-save", rid, "--cancel", "--summary", "owner said stop",
            check=False,
        )
        self.assertEqual(unflagged.returncode, 2, unflagged.stdout + unflagged.stderr)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])
        cancelled = self.cli(
            "recover-save", rid, "--cancel", "--summary", "owner said stop",
            "--human-approved", check=False,
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stdout + cancelled.stderr)
        decision = self.decision(proposal_id)
        self.assertEqual(decision["outcome"], "abandoned")
        self.assertEqual(decision["authority"], "human-asserted")
        log_text = self.save_logs(rid)[0].read_text("utf-8")
        self.assertIn("[OUTCOME:fail]", log_text)
        self.assertIn("[CLOSED:", log_text)
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(ticket["result"]["outcome"], "failed")
        self.assertFalse(self.marker_path().exists())
        self.assertEqual(self.witness()["witness_schema"], 2)
        self.assertEqual(self.current_text(SEED_KEY), SEED_TEXT)
        replay = self.save(rid, SEED_KEY, self.template_tokens[SEED_KEY],
                           "Never applied.")
        self.assertEqual(replay.returncode, 2, replay.stdout + replay.stderr)

    # Spine D1.10 revised case B (section 10 CROSSREF): past the effect
    # (final decision, head moved) --cancel is refused naming --rebuild and
    # writes nothing; --rebuild keeps the real outcome (accepted, exit 0) and
    # a retry by id replays accepted.
    def test_recover_save_cancel_only_before_effect_and_rebuild_keeps_real_outcome(self):
        rid = "test-cancel-late-1"
        _, path, original, observed = self.wedge_by_truncation(
            "save_crash_after_final_decision", 129, rid, delete_ticket=True
        )
        before = protected_tree_snapshot(self.root)
        refused = self.cli(
            "recover-save", rid, "--cancel", "--summary", "too late",
            "--human-approved", check=False,
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(ALREADY_REACHED.format(rid=rid), refused.stderr)
        self.assertEqual(changed_paths(before, protected_tree_snapshot(self.root)), [])
        path.write_bytes(original)
        rebuilt = self.cli(
            "recover-save", rid, "--rebuild", "--summary", "finish it", check=False
        )
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
        fields = parse_save_line(rebuilt.stdout)
        self.assertEqual((fields["result"], fields["decision"]), ("accepted", "accepted"))
        self.assertEqual(self.current_text(MATRIX_KEY), MATRIX_TEXT)
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(ticket["result"]["outcome"], "accepted")
        retry = self.save(rid, MATRIX_KEY, observed, MATRIX_TEXT, True)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(parse_save_line(retry.stdout)["line"], fields["line"])

    # Spine D1.10 revised case C: a resumed save that ends held under
    # --rebuild exits with its own code, 1, not 0.
    def test_rebuild_of_a_held_save_exits_one(self):
        rid = "test-rebuild-held-1"
        _, path, original, _ = self.wedge_by_truncation(
            "save_crash_after_log", 125, rid, key=OWNER_KEY, text=AGENT_REWRITE,
            new_subject=False, delete_ticket=True,
        )
        path.write_bytes(original)
        rebuilt = self.cli(
            "recover-save", rid, "--rebuild", "--summary", "finish it", check=False
        )
        self.assertEqual(rebuilt.returncode, 1, rebuilt.stdout + rebuilt.stderr)
        self.assertEqual(parse_save_line(rebuilt.stdout)["result"], "held")
        self.assertEqual(self.current_text(OWNER_KEY), OWNER_TEXT)

    # Section 03 10.1 / D1.10: recover-save --cancel finishes a ticket-only
    # id failed with decision null; nothing was allocated.
    def test_recover_save_cancel_ticket_only(self):
        rid = "test-cancel-ticket-1"
        self.crash_named("save_crash_after_ticket", 121, rid, MATRIX_KEY, MATRIX_TEXT)
        planned = self.ticket(rid)["run_id"]
        result = self.cli(
            "recover-save", rid, "--cancel", "--summary", "owner cancelled",
            "--human-approved", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.open_ticket_path(rid).exists())
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(ticket["result"]["outcome"], "failed")
        self.assertIsNone(ticket["result"]["decision"])
        self.assertFalse(self.bdir().joinpath("log", planned + ".md").exists())
        self.assertIsNone(self.current_text(MATRIX_KEY))

    # Spine D3.5, R-A (gate 3): while a save marker is live and cannot be
    # finished, start --open-run, journal, close and propose refuse and write
    # nothing; once the save can finish, the next get finishes it and the
    # lifecycle commands proceed.
    def test_lifecycle_writes_refuse_under_live_save_marker(self):
        legacy = self.start("legacy-open")
        rid = "test-lifecycle-1"
        _, path, original, _ = self.wedge_by_truncation(
            "save_crash_after_log", 125, rid
        )
        refusal = "(?:{}|{})".format(
            LIFECYCLE_REFUSAL.format(rid=re.escape(rid)),
            WEDGED_LINE.format(rid=re.escape(rid)),
        )
        commands = (
            ("start", tuple(self.start_arguments()) + ("--actor", "blocked")),
            ("journal", ("journal", "--run", legacy, "--text", "blocked note")),
            ("propose", ("propose", "--run", legacy, "--key", SEED_KEY,
                         "--text", "blocked", "--source", "agent", "--trust",
                         "working")),
            ("close", ("close", "--run", legacy, "--outcome", "success",
                       "--summary", "blocked")),
        )
        for name, arguments in commands:
            with self.subTest(command=name):
                before = protected_tree_snapshot(self.root)
                result = self.cli(*arguments, check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertRegex(result.stderr, refusal)
                self.assertEqual(
                    changed_paths(before, protected_tree_snapshot(self.root)), []
                )
        path.write_bytes(original)
        self.cli("get", "--key", MATRIX_KEY, "--json", check=False)
        self.assertFalse(self.marker_path().exists())
        self.assertEqual(self.current_text(MATRIX_KEY), MATRIX_TEXT)
        self.start("unblocked")
        self.cli("journal", "--run", legacy, "--text", "unblocked note")
        self.propose(legacy, "lifecycle.after", "After the save.", new_subject=True)
        self.cli("close", "--run", legacy, "--outcome", "success", "--summary", "done")

    # Spine D3.3.3, R-B, D3.4.4 R1b (section 10 CROSSREF, hand-built R1b): a
    # conflict arises only on resume of a durable proposal met by a later
    # writer; the proposal's base is never rewritten, the later value stands,
    # review lists the conflict and resolve still needs the owner's flag.
    def test_conflict_outcome_only_on_a_durable_proposal_never_a_rebase(self):
        rid = "test-r1b-1"
        observed = self.crash_named(
            "save_crash_after_log", 125, rid, SEED_KEY, "Durable proposal.",
            new_subject=False,
        )
        proposal_id = self.ticket(rid)["proposal_id"]
        proposal_path = self.bdir() / "proposals" / "{}.json".format(proposal_id)
        proposal_bytes = proposal_path.read_bytes()
        # The drift path / owner repair cleared the marker; the ticket is
        # moved aside so no finisher sees it while a legacy writer lands.
        self.marker_path().unlink()
        aside = self.workspace / "ticket-aside.json"
        shutil.move(str(self.open_ticket_path(rid)), str(aside))
        self.apply_set("later-writer", SEED_KEY, "Later legacy writer.")
        later_line = [
            line for line in self.root.joinpath("bimri.md").read_text("utf-8").splitlines()
            if "[K:{}]".format(SEED_KEY) in line
        ]
        shutil.move(str(aside), str(self.open_ticket_path(rid)))
        conflicts = len(self.conflict_records())
        result = self.save(rid, SEED_KEY, observed, "Durable proposal.")
        fields = self.assert_save(result, "conflict", 1)
        self.assertEqual(fields["decision"], "contested")
        self.assertRegex(fields["conflict"], r"^C\d{6}$")
        records = self.conflict_records()
        self.assertEqual(len(records), conflicts + 1)
        self.assertEqual(records[-1]["type"], "stale-base")
        self.assertIn(proposal_id, json.dumps(records[-1]))
        self.assertEqual(proposal_path.read_bytes(), proposal_bytes)
        self.assertEqual(self.current_text(SEED_KEY), "Later legacy writer.")
        self.assertEqual(
            [
                line for line in self.root.joinpath("bimri.md").read_text("utf-8").splitlines()
                if "[K:{}]".format(SEED_KEY) in line
            ],
            later_line,
        )
        review = self.cli("review", check=False)
        self.assertIn(fields["conflict"], review.stdout)
        unflagged = self.cli(
            "resolve", fields["conflict"], "--choose", proposal_id, check=False
        )
        self.assertEqual(unflagged.returncode, 2, unflagged.stdout + unflagged.stderr)


class SaveFixupTest(V520Step2Base):
    """Step 2 fix-up: an interrupted owner cancel finishes, a lost ticket is
    rebuilt for a cancel, a full disk never stops a read beside a pending
    save, and a finished request replays beside another save's wedge."""

    CANCEL_SUMMARY = "owner said stop"

    def cancel_arguments(self, rid):
        return (
            "recover-save", rid, "--cancel", "--summary",
            self.CANCEL_SUMMARY, "--human-approved",
        )

    def prepare_cancel(self, root, rid, stub, delete_ticket=False):
        """A save crashed after its log (B4); with stub the owner then
        quarantines its damaged proposal. Returns (proposal id, observed)."""
        observed = self.template_tokens[MATRIX_KEY]
        crashed = self.save_worker(
            "save_crash_after_log", rid, MATRIX_KEY, observed, MATRIX_TEXT,
            True, root=root,
        )
        self.assertEqual(crashed.returncode, 125, crashed.stderr)
        proposal_id = self.ticket(rid, root=root)["proposal_id"]
        if delete_ticket:
            self.open_ticket_path(rid, root).unlink()
        if stub:
            path = self.bdir(root) / "proposals" / "{}.json".format(proposal_id)
            original = path.read_bytes()
            path.write_bytes(original[: len(original) // 2])
            self.cli(
                "quarantine-authority", "--kind", "proposal", "--id",
                proposal_id, "--human-approved", root=root,
            )
        return proposal_id, observed

    def assert_cancelled(self, root, rid, proposal_id, observed, label):
        """The finished cancel: no marker, one done ticket failed/abandoned,
        the owner's abandoned decision, one closed [OUTCOME:fail] log, the
        old value, a clean doctor, a failed replay that allocates nothing
        and a store the next save can write."""
        self.assertFalse(self.marker_path(root).exists(), label)
        self.assertEqual(self.open_tickets(root), [], label)
        ticket = json.loads(self.done_ticket_path(rid, root).read_text("utf-8"))
        self.assertEqual(
            (ticket["result"]["outcome"], ticket["result"]["decision"]),
            ("failed", "abandoned"),
            label,
        )
        self.assertEqual(ticket["proposal_id"], proposal_id, label)
        self.assertEqual(ticket["result"]["error"], self.CANCEL_SUMMARY, label)
        decision = self.decision(proposal_id, root=root)
        self.assertEqual(
            (decision["outcome"], decision["authority"]),
            ("abandoned", "human-asserted"),
            label,
        )
        logs = self.save_logs(rid, root=root)
        self.assertEqual(len(logs), 1, label)
        text = logs[0].read_text("utf-8")
        self.assertEqual(text.count("[OUTCOME:"), 1, label)
        self.assertIn("[OUTCOME:fail] " + self.CANCEL_SUMMARY, text, label)
        self.assertEqual(text.count("[CLOSED:"), 1, label)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(lines[-1].startswith("[CLOSED:"), label)
        self.assertIsNone(self.current_text(MATRIX_KEY, root=root), label)
        self.assert_doctor_read_only_passed(root)
        proposals = self.proposals_for(rid, root=root)
        revisions = self.revision_numbers(root=root)
        retry = self.save(
            rid, MATRIX_KEY, observed, MATRIX_TEXT, True, root=root
        )
        self.assert_failed(retry, rid)
        self.assertIn(self.CANCEL_SUMMARY, retry.stderr, label)
        self.assertEqual(len(self.save_logs(rid, root=root)), 1, label)
        self.assertEqual(self.proposals_for(rid, root=root), proposals, label)
        self.assertEqual(self.revision_numbers(root=root), revisions, label)
        self.assertIsNone(self.current_text(MATRIX_KEY, root=root), label)
        after = self.save(
            rid + "-after", SEED_KEY, self.key_token(SEED_KEY, root=root),
            "After the cancel.", root=root,
        )
        self.assert_save(after, "accepted", 0)

    def run_cancel_matrix(self, stub):
        """recover-save --cancel killed after every counted write n; the
        owner's retry of the same command finishes it exactly once."""
        rid = "test-cancel-matrix-" + ("stub" if stub else "intact")
        wedged = self.fresh_store("cancel-wedged")
        proposal_id, observed = self.prepare_cancel(wedged, rid, stub)
        labels = []
        for number in range(1, 60):
            root = self.workspace / "cancel-n{}".format(number)
            copy_store(wedged, root)
            result = self.worker(
                "save_crash_after_write_{}".format(number),
                *self.cancel_arguments(rid), root=root
            )
            point = self.crash_point(root)
            if point is None:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_cancelled(
                    root, rid, proposal_id, observed, "uninterrupted"
                )
                shutil.rmtree(str(root), ignore_errors=True)
                break
            label = "n={} {}".format(number, point[1])
            labels.append(point[1])
            self.assertEqual(result.returncode, CRASH_EXIT, label)
            retried = self.cli(*self.cancel_arguments(rid), root=root, check=False)
            self.assertEqual(
                retried.returncode, 0,
                label + "\n" + retried.stdout + retried.stderr,
            )
            self.assertIn(
                "BIMRI: save {} cancelled; nothing was remembered.".format(rid),
                retried.stdout,
            )
            self.assert_cancelled(root, rid, proposal_id, observed, label)
            shutil.rmtree(str(root), ignore_errors=True)
        else:
            self.fail("the cancel never completed within 60 counted writes")
        # The decision is the cancel's first durable write; the log, state,
        # ticket and publication follow it.
        self.assertEqual(
            labels[0], "decisions/{}.json".format(proposal_id), labels
        )
        self.assertIn("audit-transition.json", labels)
        return labels

    # Review finding 1 (D1.10, D3.4.4 R0c): a crash inside recover-save
    # --cancel over a quarantined proposal, at every counted write, is
    # finished by the owner's retry; the stub never wedges the store.
    def test_recover_save_cancel_crash_matrix_over_a_quarantine_stub(self):
        self.run_cancel_matrix(stub=True)

    # Review finding 1, intact variant: --cancel over an intact proposal
    # (legal before the effect) killed at every counted write converges the
    # same way.
    def test_recover_save_cancel_crash_matrix_over_an_intact_proposal(self):
        self.run_cancel_matrix(stub=False)

    # Review finding 1 (D3.4.1): a cancel interrupted after its decision is
    # finished by the next reconciling read, stub or intact, never wedged on
    # the stub's hash.
    def test_interrupted_cancel_is_finished_by_the_next_read(self):
        for stub in (True, False):
            with self.subTest(stub=stub):
                rid = "test-cancel-read-{}".format("stub" if stub else "intact")
                root = self.fresh_store(rid)
                proposal_id, observed = self.prepare_cancel(root, rid, stub)
                crashed = self.worker(
                    "save_crash_after_write_1", *self.cancel_arguments(rid),
                    root=root
                )
                self.assertEqual(crashed.returncode, CRASH_EXIT, crashed.stderr)
                self.assertEqual(
                    self.crash_point(root)[1],
                    "decisions/{}.json".format(proposal_id),
                )
                got = self.cli("get", "--key", SEED_KEY, "--json", root=root,
                               check=False)
                self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
                self.assertEqual(
                    json.loads(got.stdout)["matches"][0]["text"], SEED_TEXT
                )
                self.assert_cancelled(root, rid, proposal_id, observed, rid)

    # Review finding 2 (D1.10, D3.3.2, section 03 L15): --cancel over a
    # quarantined proposal whose ticket was lost rebuilds the ticket from the
    # marker and log header and finishes it failed/abandoned; the same id
    # replays failed and never applies the cancelled change.
    def test_cancel_over_a_stub_with_a_lost_ticket_records_the_cancel(self):
        rid = "test-cancel-lost-ticket"
        proposal_id, observed = self.prepare_cancel(
            self.root, rid, stub=True, delete_ticket=True
        )
        planned_run = proposal_id.split("-")[0]
        self.assertFalse(self.open_ticket_path(rid).exists())
        cancelled = self.cli(*self.cancel_arguments(rid), check=False)
        self.assertEqual(
            cancelled.returncode, 0, cancelled.stdout + cancelled.stderr
        )
        ticket = json.loads(self.done_ticket_path(rid).read_text("utf-8"))
        self.assertEqual(set(ticket), TICKET_FIELDS)
        self.assertEqual(ticket["status"], "finished")
        self.assertEqual(ticket["run_id"], planned_run)
        self.assertEqual(ticket["entry_id"], planned_run + "-E001")
        self.assertEqual(ticket["intent"]["key"], MATRIX_KEY)
        self.assertEqual(ticket["intent"]["observed"], observed)
        self.assertEqual(
            ticket["ticket_hash"], record_seal(ticket, "ticket_hash")
        )
        self.assert_cancelled(self.root, rid, proposal_id, observed, rid)

    # Review finding 3 (section 03 9, gate 8): with a save marker pending, a
    # full disk at every write the read's resume attempts leaves the marker
    # resumable and the read still serves memory; a legacy start (a writer at
    # this step) fails with the filesystem error and allocates no run; the
    # next read with space finishes the save once.
    def test_full_disk_read_beside_a_pending_save_serves_memory(self):
        rid = "test-enospc-read"
        crashed = self.fresh_store("enospc-crashed")
        observed = self.template_tokens[MATRIX_KEY]
        result = self.save_worker(
            "save_crash_after_log", rid, MATRIX_KEY, observed, MATRIX_TEXT,
            True, root=crashed,
        )
        self.assertEqual(result.returncode, 125, result.stderr)
        counted = 0
        for number in range(1, 60):
            root = self.workspace / "enospc-n{}".format(number)
            copy_store(crashed, root)
            got = self.worker(
                "save_fail_enospc_at_{}".format(number),
                "get", "--key", SEED_KEY, "--json", root=root,
            )
            if NO_SPACE not in got.stderr:
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertFalse(self.marker_path(root).exists())
                shutil.rmtree(str(root), ignore_errors=True)
                break
            counted += 1
            label = "n={}".format(number)
            self.assertEqual(got.returncode, 0, label + "\n" + got.stderr)
            payload = json.loads(got.stdout)
            self.assertEqual(payload["matches"][0]["key"], SEED_KEY, label)
            self.assertEqual(payload["matches"][0]["text"], SEED_TEXT, label)
            self.assertIn(
                "BIMRI NOTICE: save {} is unfinished".format(rid), got.stderr
            )
            self.assertTrue(self.marker_path(root).exists(), label)
            if number == 1:
                # The disk is still full: a legacy start (a writer at this
                # step, D3.5) fails with the filesystem error at its resume's
                # first write and allocates no run beside the marker.
                logs_before = sorted(
                    self.bdir(root).joinpath("log").glob("R*.md")
                )
                started = self.worker(
                    "save_fail_enospc_at_1",
                    *(tuple(self.start_arguments()) + ("--actor", "reader")),
                    root=root
                )
                self.assertEqual(started.returncode, 2, started.stderr)
                self.assertIn(NO_SPACE, started.stderr)
                self.assertEqual(
                    sorted(self.bdir(root).joinpath("log").glob("R*.md")),
                    logs_before,
                )
                self.assertTrue(self.marker_path(root).exists())
            self.assertEqual(
                self.current_text(MATRIX_KEY, root=root), MATRIX_TEXT, label
            )
            self.assertFalse(self.marker_path(root).exists(), label)
            ticket = json.loads(self.done_ticket_path(rid, root).read_text("utf-8"))
            self.assertEqual(ticket["result"]["outcome"], "accepted", label)
            self.assertEqual(len(self.save_logs(rid, root=root)), 1, label)
            self.assert_doctor_read_only_passed(root)
            shutil.rmtree(str(root), ignore_errors=True)
        else:
            self.fail("the read never completed within 60 counted writes")
        self.assertGreaterEqual(counted, 5)

    # Review finding 4 (D3.2 L3 before L4, D3.3.4): a finished request
    # replays its recorded line and code beside another wedged save and on a
    # store an owner quarantine has blocked, writing nothing; a fresh request
    # still meets the wedge.
    def test_finished_request_replays_beside_a_wedge_and_a_blocked_store(self):
        rid = "test-replay-first"
        first = self.save(
            rid, SEED_KEY, self.template_tokens[SEED_KEY], "First value."
        )
        self.assert_save(first, "accepted", 0)
        proposal_id, _, _, _ = self.wedge_by_truncation(
            "save_crash_after_log", 125, "test-replay-wedge"
        )
        for stage in ("wedged", "blocked"):
            with self.subTest(stage=stage):
                if stage == "blocked":
                    self.cli(
                        "quarantine-authority", "--kind", "proposal", "--id",
                        proposal_id, "--human-approved",
                    )
                before = protected_tree_snapshot(self.root)
                again = self.save(
                    rid, SEED_KEY, self.template_tokens[SEED_KEY],
                    "First value.",
                )
                self.assertEqual(
                    (again.returncode, again.stdout),
                    (first.returncode, first.stdout),
                    again.stderr,
                )
                replay = self.save(
                    rid, SEED_KEY, self.template_tokens[SEED_KEY],
                    "First value.", json_out=True,
                )
                self.assertEqual(replay.returncode, 0, replay.stderr)
                self.assertIs(json.loads(replay.stdout)["replayed"], True)
                self.assertEqual(
                    changed_paths(before, protected_tree_snapshot(self.root)),
                    [],
                )
                fresh = self.save(
                    "test-replay-fresh-" + stage, SEED_KEY,
                    self.template_tokens[SEED_KEY], "Fresh value.",
                )
                self.assertEqual(fresh.returncode, 2, fresh.stdout + fresh.stderr)
                self.assertIn("is wedged", fresh.stderr)


class InterruptedWriteRecoveryTest(V520Step2Base):
    def test_interrupted_legacy_close_names_the_run_and_leaves_it_to_its_owner(self):
        run = self.start("legacy-owner")
        self.propose(run, SEED_KEY, "Correction from the legacy run.")
        interrupted = self.worker(
            "proposal_crash_after_applying_decision", "close", "--run", run,
            "--outcome", "success", "--summary", "finish legacy work",
        )
        self.assertEqual(interrupted.returncode, 108, interrupted.stderr)
        args = ("later-save", MATRIX_KEY, self.template_tokens[MATRIX_KEY], "Separate change.")
        refused = self.save(*args, new_subject=True)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("sync --run " + run, refused.stderr)
        for options in [(), ("--read-only",)]:
            report = self.cli("doctor", *options, check=False)
            self.assertEqual(report.returncode, 1, report.stdout + report.stderr)
            self.assertIn("COMPLETION REQUIRED", report.stdout)
            self.assertIn("sync --run " + run, report.stdout)
        self.assertEqual(self.decision(run + "-Q001")["outcome"], "applying")
        self.cli("sync", "--run", run)
        self.assert_save(self.save(*args, new_subject=True), "accepted", 0)
        self.assertEqual(self.current_text(SEED_KEY), "Correction from the legacy run.")

    def interrupted_archive(self):
        # A prior update populates the archive. Recovery must preserve those
        # exact bytes along with every accepted revision.
        first = self.save("prior", SEED_KEY, self.template_tokens[SEED_KEY], "Prior version.")
        self.assert_save(first, "accepted", 0)
        token = self.key_token(SEED_KEY)
        raw = next(line for line in (self.root / "bimri.md").read_text("utf-8").splitlines()
                   if "[K:" + SEED_KEY + "]" in line)
        stopped = self.save_worker("save_crash_after_applying", "interrupted", SEED_KEY,
                                   token, "Latest version.")
        self.assertEqual(stopped.returncode, 126, stopped.stderr)
        ticket = self.ticket("interrupted")
        month = self.bdir() / "archive" / (ticket["started_on"][:7] + ".md")
        before = month.read_bytes()
        row = (f"[ARCHIVED:{ticket['started_on']}] [BY:{ticket['proposal_id']}] "
               f"[replaced] {raw}\n").encode("utf-8")
        return token, month, before, row

    def test_retry_replaces_own_torn_archive_fragment_once(self):
        token, month, before, row = self.interrupted_archive()
        month.write_bytes(before + row[:-7])
        first = self.save("interrupted", SEED_KEY, token, "Latest version.")
        self.assert_save(first, "accepted", 0)
        self.assertEqual(month.read_bytes(), before + row)
        self.assertEqual(self.current_text(SEED_KEY), "Latest version.")
        replay = self.save("interrupted", SEED_KEY, token, "Latest version.")
        self.assertEqual((replay.returncode, replay.stdout), (0, first.stdout))
        self.assertEqual(month.read_bytes(), before + row)
        self.cli("doctor", "--read-only")

    def test_foreign_archive_fragment_survives_retry_rebuild_and_cancel(self):
        token, month, before, row = self.interrupted_archive()
        # Matching the proposal's stamp alone does not prove that the body
        # came from this save.
        damaged = before + row.split(b"[replaced]")[0] + b"[replaced] unrelated text"
        month.write_bytes(damaged)
        accepted_head = self.state()["head_revision"]
        attempts = [
            self.save_arguments("interrupted", SEED_KEY, token, "Latest version."),
            ["recover-save", "interrupted", "--rebuild", "--summary", "retry"],
            ["recover-save", "interrupted", "--cancel", "--human-approved", "--summary", "cancel"],
            self.save_arguments("unrelated", MATRIX_KEY, self.template_tokens[MATRIX_KEY],
                                "A later decision.", new_subject=True),
        ]
        for args in attempts:
            with self.subTest(command=args[0], option=args[2]):
                result = self.cli(*args, check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(month.name, result.stderr)
                self.assertEqual(month.read_bytes(), damaged)
                self.assertEqual(self.state()["head_revision"], accepted_head)
                self.assertFalse(self.open_ticket_path("unrelated").exists())
        month.write_bytes(before)
        self.assert_save(self.save("interrupted", SEED_KEY, token, "Latest version."), "accepted", 0)
        self.assertEqual(month.read_bytes(), before + row)


if __name__ == "__main__":
    unittest.main()
