"""Exercise adapter guard failures and repairs through the public installer."""

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
ENGINE = SOURCE / "bimri-engine.py"
START = "<!-- BIMRI:START -->"
END = "<!-- BIMRI:END -->"
HEADING = "## BIMRI Engine v5.1.2 | Authority Format v5.1.0 | Hot Grammar v5.0.2"
CLAUDE = ("@AGENTS.md\n\nUse BIMRI-PROTOCOL.md for the full memory protocol. "
          "BIMRI shared memory is engine-managed.")


def snapshot(root):
    return {
        path.relative_to(root): (
            hashlib.sha256(path.read_bytes()).digest() if path.is_file() else None
        )
        for path in root.rglob("*")
    }


class InstallerAdapterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="bimri-adapter-")
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name)

    def install(self, success=True, source=ENGINE):
        result = subprocess.run(
            [sys.executable, str(source), "install", "--target", str(self.target),
             "--quiescent"], capture_output=True, text=True, timeout=120,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0 if success else 2,
                         result.stdout + result.stderr)
        return result.stdout + result.stderr

    def assert_rejected(self, name, content):
        (self.target / name).write_text(content, encoding="utf-8")
        before = snapshot(self.target)
        error = self.install(success=False)
        self.assertRegex(error, name + r":\d+:")
        self.assertIn(START, error)
        self.assertIn("retry install", error)
        self.assertEqual(before, snapshot(self.target))

    def test_fresh_install_rejects_each_adapter_before_creating_store(self):
        valid = f"{START}\n{HEADING}\n{END}\n"
        for name, content in (
            ("AGENTS.md", f"Owner instructions.\n\n{HEADING}\nOld chat protocol.\n{END}\n"),
            ("AGENTS.md", "## BIMRI Memory Protocol v5.0.2\n\nOld chat protocol.\n"),
            ("AGENTS.md", valid + "\n" + valid),
            ("AGENTS.md", valid + "\n" + HEADING + "\n"),
            ("AGENTS.md", f"{START}\n{HEADING}\n{HEADING}\n{END}"),
            ("AGENTS.md", f"{START}\nKeep this owner note.\n{HEADING}\n{END}"),
            ("AGENTS.md", f"{START}\n<!-- private owner comment -->\n{HEADING}\n{END}"),
            ("AGENTS.md", f"{START}\n{HEADING}\n"),
            ("CLAUDE.md", END),
            ("CLAUDE.md", CLAUDE),
        ):
            with self.subTest(name=name, content=content):
                self.assert_rejected(name, content)
                (self.target / name).unlink()

    def test_code_update_rejects_both_bad_adapters_and_accepts_manual_repair(self):
        self.install()
        a = self.target / "AGENTS.md"
        c = self.target / "CLAUDE.md"
        good_a = a.read_text("utf-8")
        good_c = c.read_text("utf-8")
        self.assert_rejected(a.name, good_a.replace(START, "", 1))
        a.write_text(good_a, encoding="utf-8")
        self.assert_rejected(c.name, good_c + "\n" + good_c)
        c.write_text(good_c, encoding="utf-8")
        memory = (self.target / ".bimri/state.json").read_bytes()
        self.install()
        self.assertEqual(a.read_text("utf-8"), good_a)
        self.assertEqual(c.read_text("utf-8"), good_c)
        self.assertEqual((self.target / ".bimri/state.json").read_bytes(), memory)

    def test_update_recovery_refuses_to_overwrite_owner_edits_made_after_the_crash(self):
        self.install()
        adapter = self.target / "AGENTS.md"
        before = adapter.read_bytes()
        crashed = subprocess.run(
            [sys.executable, str(SOURCE / "tests" / "crash_worker.py"), str(ENGINE),
             "code_update_crash_after_engine", str(self.target), "install", "--target",
             str(self.target), "--quiescent"], capture_output=True, timeout=120)
        self.assertEqual(crashed.returncode, 96, crashed.stderr)
        edited = adapter.read_bytes() + b"Owner instruction added after interruption.\n"
        adapter.write_bytes(edited)
        state = (self.target / ".bimri" / "state.json").read_bytes()
        self.assertIn("AGENTS.md changed since the install", self.install(success=False))
        self.assertEqual(adapter.read_bytes(), edited)
        self.assertEqual((self.target / ".bimri" / "state.json").read_bytes(), state)
        adapter.write_bytes(before)
        self.install()

    def test_preserve_owner_bytes_and_examples_through_two_installs(self):
        prefix = (
            "\ufeffOwner mentions " + START + " and " + END + ".  \r\n\r\n"
            + START + "\r\nOwner prose after an orphan start.\r\n"
            "````md\r\n" + START + "\r\n" + HEADING + "\r\n```\r\n"
            + END + "\r\n````\r\n"
            "<!-- Archived heading:\r\n" + HEADING + "\r\n-->\r\n"
            "    " + END + "\r\n"
            "<PRE class='onboarding'>\r\n" + START + "\r\n" + HEADING + "\r\n"
            + END + "\r\n</PRE>\r\n"
        ).encode("utf-8")
        suffix = b"\r\n\r\nOwner text below with trailing spaces.  \r\n\r\n"
        for name, old_block in (("AGENTS.md", HEADING), ("CLAUDE.md", CLAUDE)):
            (self.target / name).write_bytes(
                prefix + (START + "\r\n" + old_block + "\r\n" + END).encode() + suffix
            )
        self.install()
        first = [(self.target / name).read_bytes() for name in ("AGENTS.md", "CLAUDE.md")]
        self.install()
        for name, content in zip(("AGENTS.md", "CLAUDE.md"), first):
            self.assertTrue(content.startswith(prefix))
            self.assertTrue(content.endswith(suffix))
            self.assertEqual((self.target / name).read_bytes(), content)

    def test_packaged_checkout_can_install_into_its_own_source_folder(self):
        for path in SOURCE.iterdir():
            if path.is_file() and not path.name.startswith("."):
                shutil.copyfile(path, self.target / path.name)
        shutil.copytree(SOURCE / "legacy", self.target / "legacy")
        self.install(source=self.target / ENGINE.name)
        for name in ("AGENTS.md", "CLAUDE.md"):
            text = (self.target / name).read_text("utf-8")
            self.assertEqual(text.count(START), 1)
            self.assertEqual(text.count(END), 1)


if __name__ == "__main__":
    unittest.main()
