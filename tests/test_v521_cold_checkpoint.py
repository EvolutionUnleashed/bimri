"""Frozen cold-proof publication and public command availability."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "bimri-engine.py"
spec = importlib.util.spec_from_file_location("cold_engine", ENGINE)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class ColdCheckpointTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = engine.Paths(self.root)
        self.cli("doctor")
        self.paths.audit_witness.unlink()

    def cli(self, *args, expected=0, script=ENGINE):
        result = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), *args],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def save(self, key):
        state = json.loads(self.paths.state.read_bytes())
        return self.cli("save", "--request", "cold-request", "--key", key,
                        "--observed", "V{:06d}:absent".format(state["head_revision"]),
                        "--new-subject", "--tier", "2", "--text", "The corrected value.")

    def paused_proof(self, while_paused, read):
        ready = threading.Event()
        resume = threading.Event()
        original = engine.prove_cold_checkpoint

        def prove(snapshot):
            result = original(snapshot)
            ready.set()
            if not resume.wait(15):
                raise RuntimeError("reader was never released")
            return result

        with mock.patch.object(engine, "prove_cold_checkpoint", prove):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(read)
                try:
                    self.assertTrue(ready.wait(15), "cold proof never reached its publication barrier")
                    while_paused()
                finally:
                    resume.set()
                return future.result(15)

    def test_read_and_save_finish_during_cold_get_and_get_uses_new_head(self):
        def compete():
            brief = self.cli("start", "--json")
            self.assertIn("head_revision", brief.stdout)
            self.assertIn("accepted", self.save("cold.corrected").stdout)

        state, records = self.paused_proof(
            compete, lambda: engine.exact_recall_records_live(self.paths, "cold.corrected")
        )
        self.assertEqual(records[0]["detail"], "The corrected value.")
        witness = json.loads(self.paths.audit_witness.read_bytes())
        self.assertEqual(witness["head_revision"], state["head_revision"])
        self.assertEqual(witness["audit_origin"], "scoped")

    def test_same_stat_change_anywhere_refuses_stale_promotion(self):
        path = self.paths.proposals / "operator-note"
        path.write_bytes(b"before")
        original = path.stat()

        def change():
            path.write_bytes(b"new123")
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

        with self.assertRaisesRegex(engine.BimriBusy, "changed"):
            self.paused_proof(change, lambda: engine.rebuild_cold_checkpoint(self.paths))
        self.assertFalse(self.paths.audit_witness.exists())

    def after_final_roster(self, change):
        original = engine.checkpoint_input_inventory

        def compare(paths, *args, **kwargs):
            result = original(paths, *args, **kwargs)
            if kwargs.get("metadata_only"):
                change()
            return result

        return mock.patch.object(engine, "checkpoint_input_inventory", compare)

    def test_managed_writer_fences_a_proof_even_without_changed_files(self):
        def writer():
            with engine.engine_lock(engine.Paths(self.root)):
                pass

        with self.after_final_roster(writer), self.assertRaisesRegex(engine.BimriBusy, "changed"):
            engine.rebuild_cold_checkpoint(self.paths)
        self.assertFalse(self.paths.audit_witness.exists())

    def test_state_bytes_rechecked_after_the_final_roster(self):
        def raw_change():
            self.paths.state.write_bytes(self.paths.state.read_bytes() + b" ")

        with self.after_final_roster(raw_change), self.assertRaisesRegex(engine.BimriBusy, "changed"):
            engine.rebuild_cold_checkpoint(self.paths)
        self.assertFalse(self.paths.audit_witness.exists())

    def test_view_read_during_byte_verification_does_not_invalidate_the_proof(self):
        original = engine.checkpoint_input_inventory

        def compare(paths, *args, **kwargs):
            if not kwargs:
                self.cli("start", "--json")
                self.cli("status")
            return original(paths, *args, **kwargs)

        with mock.patch.object(engine, "checkpoint_input_inventory", compare):
            engine.rebuild_cold_checkpoint(self.paths)
        self.assertEqual(json.loads(self.paths.audit_witness.read_bytes())["audit_origin"], "full")

    def test_cleanup_keeps_leased_and_unowned_project_directories(self):
        unrelated = self.root / (engine.COLD_COPY_PREFIX + "a" * 32)
        unrelated.mkdir()
        (unrelated / "my-file").touch()
        with engine.private_checkpoint_copy(self.paths) as active:
            engine.reap_checkpoint_copies(self.paths)
            self.assertTrue(active.lock.is_file())
        self.assertFalse(active.root.exists())
        self.assertTrue((unrelated / "my-file").is_file())

    def test_moved_exact_read_uses_an_already_verified_winner(self):
        def moved(*args, **kwargs):
            self.save("cold.corrected")
            raise engine.ColdCheckpointMoved("checkpoint inputs changed")

        with mock.patch.object(engine, "rebuild_cold_checkpoint", side_effect=moved) as proof:
            _, records = engine.exact_recall_records_live(self.paths, "cold.corrected")
        self.assertEqual(proof.call_count, 1)
        self.assertEqual(records[0]["detail"], "The corrected value.")

    def test_missing_old_checkpoint_rebuilds_before_accepting_correction(self):
        # Build the fixture with the actual prior release. The 5.1.2 engine
        # lives in the repository's ancestry, never in a user's workspace.
        prior = subprocess.run(["git", "show", "2c936ec:bimri-engine.py"],
                               cwd=REPO, capture_output=True)
        if prior.returncode:
            self.skipTest("5.1.2 commit unavailable in this checkout")
        older = self.root / "previous"
        older.mkdir()
        old_engine = older / "bimri-engine.py"
        old_engine.write_bytes(prior.stdout)
        self.root = older
        self.paths = engine.Paths(older)
        self.cli("doctor", script=old_engine)
        self.paths.audit_witness.unlink()
        self.assertIn("accepted", self.save("cold.corrected").stdout)
        self.assertEqual(json.loads(self.paths.audit_witness.read_bytes())["audit_origin"], "scoped")

    def test_damaged_authority_stops_both_get_and_save(self):
        self.paths.proposals.joinpath("R000001-Q001.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(engine.BimriError, "authority recovery"):
            engine.exact_recall_records_live(self.paths, "cold.corrected")
        self.assertFalse(self.paths.audit_witness.exists())
        refused = self.cli("save", "--request", "cold-request", "--key", "cold.corrected",
                           "--observed", "V000000:absent", "--new-subject", "--tier", "2",
                           "--text", "The corrected value.", expected=2)
        self.assertIn("recovery", refused.stderr)
        self.assertFalse(self.paths.audit_witness.exists())


if __name__ == "__main__":
    unittest.main()
