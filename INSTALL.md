# Install BIMRI

## The One-Sentence Installation

Tell any local coding agent:

> Install BIMRI in this project from
> https://github.com/EvolutionUnleashed/bimri. Follow INSTALL.md,
> preserve my existing instructions and memory, and run the self-check.

The owner should not need to copy files, configure a database or answer setup
questions.

Commands below use `<verified-python>`. This is a placeholder for the exact,
absolute Python 3.8+ executable verified on the current machine. It is never a
literal command and must never be replaced by an untested PATH name.

## Verify Python Before Installation

The installing agent must discover Python by execution, not by filename or a
successful-looking process status. Probe the normal candidates for the current
operating system. On Windows, try `py -3`, `python`, then `python3`; on POSIX,
try `python3`, `python`, then `py -3` if available. Each candidate must execute
a sentinel equivalent to:

```python
import pathlib, sys
assert sys.version_info >= (3, 8)
executable = pathlib.Path(sys.executable).resolve(strict=True)
assert executable.is_file()
print("BIMRI_PYTHON_OK\t" + str(executable))
```

Accept a candidate only when it exits successfully and prints exactly one
non-empty `BIMRI_PYTHON_OK` record containing an absolute regular-file path.
Zero output is failure, even with exit status zero. This rejects aliases such
as a silent Microsoft Store stub. Re-run the same sentinel through the returned
absolute path and require it to resolve to the same file. That path is
`<verified-python>` for the rest of installation and for every adapter written
on this machine.

If no candidate passes, stop before changing the target and tell the owner to
install Python 3.8 or newer. After the project moves to another machine or the
interpreter is upgraded or removed, repeat discovery and rebind every local
adapter before using BIMRI.

## Local Runtime Binding Records

Keep host-specific runtime data out of canonical memory and shared
configuration. The installer writes:

- `.bimri/runtime.local.json` for the verified absolute Python and engine argv
  prefix; and
- `.bimri/hooks.claude.local.json` for the Claude hook source rendered from the
  portable `hooks-example.json` template.

These files are local adapter records, not canonical state or memory authority.
Do not commit, publish, reuse them on another host, or copy their absolute paths
into shared instructions. The root templates remain portable. After moving the
folder or replacing Python, rerun installation to replace both local records
before normal use. If the project versions other `.bimri/` content, add these
two paths to its ignore rules explicitly.

## Agent Installation Contract

1. Obtain this repository in a temporary local folder.
2. Inspect the target for BIMRI v1-v4 or v5 files. Before upgrading any
   earlier version, stop every agent and command using that folder and keep it
   quiescent until installation completes.
3. If Claude Cowork Global Instructions contain a v1-v3 BIMRI block, ask
   the owner to disable or remove it before installation. Those instructions
   directly rewrite the old hot-memory file and must not run alongside v5.
4. Verify Python as described above. From the temporary folder run:

   ```text
   <verified-python> bimri-engine.py install --target /absolute/path/to/the/project
   ```

5. Do not replace existing `AGENTS.md` or `CLAUDE.md`. The installer merges a
   marked BIMRI block and backs up files it upgrades. It packages `legacy/` as
   inert rollback material and copies BIMRI's MIT notice to `BIMRI-LICENSE`;
   neither action replaces the target project's root `LICENSE`.
6. Existing BIMRI v1-v4 memory migrates automatically and idempotently.
   Migration preserves exact source and backup bytes, records their hashes,
   and stops without overwriting ambiguous or malformed input. Inherited claim
   text may exceed 500 characters when the complete converted entry, including
   v5 metadata, fits the 4,096-character safety ceiling. Conversion fails
   closed rather than truncating anything that cannot fit losslessly. New and
   edited claims retain the 500-character text limit.
   Existing v5.0 and v5.0.1 state also upgrades automatically to v5.0.2 with
   a byte-preserving backup. A v5.0 upgrade adopts the 12,000-token capacity
   profile when its limits are still stock; a v5.0.1 upgrade preserves its
   configured limits.
7. Run `<verified-python> bimri-engine.py doctor` from the target project if
   you need to repeat the self-check.
8. Confirm the installer wrote both local binding records above; never commit
   them.
9. Report only:
   - BIMRI version installed;
   - the verified absolute Python executable;
   - whether memory was initialized or migrated;
   - for migration, the detected version and source file, imported counts,
     converted-pattern count, backup location, and validation result;
   - adapters enabled and whether Claude hooks were rebound;
   - whether the hook smoke test and `doctor` passed, or whether installation
     completed in authority-recovery mode; and
   - any inherited-limit repair warning printed by the installer.

For an existing v5 target, install uses the target's engine lock from before
its first mutation through the self-check, so it serializes with v5 commands in
the same lock domain. This guarantee does not cover concurrent installers or
agents operating through different runtime/lock boundaries.

Earlier BIMRI versions do not use the v5 engine lock. Quiescence and removal of
the old Global Instructions are therefore required preconditions, rather than
steps the installer can enforce across every running agent or Claude setting.

Before changing target files, install writes rollback copies to
`.bimri/install-backups/<timestamp>/`. If the self-check fails, it restores the
touched paths and reports that exact backup directory. Do not discard the
reported backup until the owner confirms the installation is healthy.

Malformed canonical authority records do not force the installer to roll back
the v5.0.2 recovery tools. When the core memory and state are sound, install
finishes in `installed-recovery-required` mode, lists every blocker, and keeps
shared-memory writes paused. `start` remains available with a loud recovery
brief, while `status` and `doctor` remain nonzero for automation. After the
owner reviews the evidence, use the human-attested `quarantine-authority` and
`restore-authority` workflow in `BIMRI-PROTOCOL.md`; never hand-edit `.bimri/`.

## Claude Code Hooks

The universal instructions work without hooks. For automatic Claude Code start
and close, use the rendered `.bimri/hooks.claude.local.json`. Merge its BIMRI
entries into the target's machine-local
`.claude/settings.local.json`; never put a machine-specific absolute Python
path in shared `.claude/settings.json`. Preserve unrelated settings and hooks.
Replace existing BIMRI `hook-start` and `hook-close` entries instead of adding
duplicates.

The installed hook commands use argv form with the exact verified Python
executable and the project-relative engine path. Inspect them with `/hooks`,
then execute a real smoke test: invoke `hook-start` with a unique synthetic
Claude session ID and require non-empty output containing `BIMRI RUN HANDLE`
and `=== BIMRI BRIEF`; invoke `hook-close` with the same ID and require a normal
close; then run `doctor`. A zero-output hook is a failed hook even if its
process reports success. Confirm that the synthetic run is closed before
reporting the installation complete.

An automatic `hook-close` for an unknown or already-unmapped session is a
successful no-op. The explicit `close` command remains strict so operator
mistakes are still visible.

The root `hooks-example.json` remains a portable placeholder template. Never
paste its unresolved placeholder into Claude settings; use only the local
rendered copy.

Hooks are an adapter, not part of the memory. Claude, Codex and another local
agent can all use the same BIMRI folder when they share one verified lock
domain. If the folder moves to another host, repeat Python discovery and rerun
the self-contained installed engine against its own folder, or use a clean
BIMRI source. Then replace the local BIMRI hook entries from the newly rendered
template and repeat the smoke test before a Claude session uses them.

Claude Code support is optional. BIMRI's canonical interface is the engine plus
the universal `AGENTS.md` block; other local agents can use the same memory
without installing Claude hooks or relying on Claude Cowork Global
Instructions.

## Supported Concurrency

BIMRI supports multiple agents only when every process observes the same
operating-system lock on `.bimri/engine.lock` and the same atomic-rename
semantics. Portability means the folder can move between runtime boundaries
while no run or engine command is active.

Simultaneous writes through Dropbox-style synchronization, NFS, multiple
machines, or independent copies are outside the v5 guarantee. A VM-backed or
sandboxed agent working through a mounted host folder is also outside the
guarantee unless that mount is verified to share lock and rename behavior with
host processes. Containers vary. The safe default is one runtime boundary
active at a time, followed by a quiescent handoff.
