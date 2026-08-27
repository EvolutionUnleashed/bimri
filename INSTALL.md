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
2. Inspect the target for BIMRI v1-v4 or v5 files. Before every update, stop
   every process executing the old engine and keep the folder quiescent until
   installation completes. The lock serializes commands; it cannot fence an
   already-loaded old process waiting to write after the installer exits.
3. If Claude Cowork Global Instructions contain a v1-v3 BIMRI block, ask
   the owner to disable or remove it before installation. Those instructions
   directly rewrite the old hot-memory file and must not run alongside v5.
4. Verify Python as described above. For a fresh target or v1-v4 migration,
   run from the temporary folder:

   ```text
   <verified-python> bimri-engine.py install --target /absolute/path/to/the/project
   ```

   For any existing v5 store, first complete the step-2 external quiescent
   handoff, then run:

   ```text
   <verified-python> bimri-engine.py install --target /absolute/path/to/the/project --quiescent
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
   Existing v5.0 and v5.0.1 state also upgrades automatically with
   a byte-preserving backup. A v5.0 upgrade adopts the 12,000-token capacity
   profile when its limits are still stock; a v5.0.1 upgrade preserves its
   configured limits.
   An existing v5.0.2 store takes the lossless v5.1 authority-activation path
   documented below. Root `bimri.md` and every immutable evidence/history
   artifact remain byte-identical. The mutable state is backed up exactly and
   transactionally advanced to v5.1.0 so an old engine fails closed.
7. Run `<verified-python> bimri-engine.py doctor --read-only` from the updated
   target when you need to repeat the non-mutating audit. Use normal
   `doctor` only when repair-capable validation is intended.
8. Confirm the installer wrote both local binding records above; never commit
   them.
9. Report only:
   - BIMRI engine release, authority-format version, and hot-grammar version;
   - the verified absolute Python executable;
   - whether memory was initialized or migrated;
   - for migration, the detected version and source file, imported counts,
     converted-pattern count, backup location, and validation result;
   - adapters enabled and whether Claude hooks were rebound;
   - whether the hook smoke test and audit passed, or whether installation
     completed in authority-recovery mode;
   - for a v5.0.2 update, the unchanged hot-memory hash, immutable-evidence
     digest, accepted head before/after, old-state backup, and activation
     result; and
   - any inherited-limit repair warning printed by the installer.

For an existing v5 target, install uses the target's engine lock from before
its first mutation through the self-check, so it serializes with v5 commands in
the same lock domain. This guarantee does not cover concurrent installers or
agents operating through different runtime/lock boundaries.

Earlier BIMRI versions do not use the v5 engine lock. Quiescence and removal of
the old Global Instructions are therefore required preconditions, rather than
steps the installer can enforce across every running agent or Claude setting.

For legacy migrations, install writes rollback copies to
`.bimri/install-backups/<timestamp>/`. The v5.0.2 authority-activation path
stores program-file backups, the exact old mutable state, and its receipt
beside `.bimri`, under `.bimri-update-backups/<timestamp>/`; no partial copy of
memory is described as a complete memory backup.

Malformed canonical authority records do not force the installer to roll back
the v5.1.0 recovery tools. When the core memory and state are sound, install
finishes in `installed-recovery-required` mode, lists every blocker, and keeps
shared-memory writes paused. `start` remains available with a loud recovery
brief, while `status` and `doctor` remain nonzero for automation. After the
owner reviews the evidence, use the human-attested `quarantine-authority` and
`restore-authority` workflow in `BIMRI-PROTOCOL.md`; never hand-edit `.bimri/`.

## Existing v5.0.2 Store: Lossless v5.1.0 Authority Activation

Engine release v5.1.0 adds keyed cold-current residency and makes tier counts
soft. Public engine v5.0.3 deliberately kept its persisted state at v5.0.2, so
this is the normal v5.0.3-to-v5.1 upgrade path. Stop every process executing
the old engine, then explicitly attest that external quiescence when invoking
the updater:

```text
<verified-python> bimri-engine.py install --target /absolute/path/to/the/project --quiescent
```

The `--quiescent` flag is mandatory for an existing v5.0.2 store; it records
the caller's handoff attestation and does not claim that the lock can fence an
already-loaded old process. After that attestation, the installer detects the
existing state, acquires the already-present engine lock, rechecks the target,
and runs an internal source-version-aware, non-mutating audit. After successful
v5.1 activation, repeat validation with the installed engine:

```text
<verified-python> bimri-engine.py doctor --read-only
```

The source-aware audit and post-activation read-only doctor check state,
accepted head and hash, memory grammar,
pointers, authority records, and the authority graph. It compares `bimri.md`
with the accepted head without healing it. A direct hot-view edit is reported
as recovery needed and its exact bytes remain untouched.

Before package replacement, the updater records every pre-existing path under
`.bimri/`, plus root `bimri.md`: directories, regular files, symbolic links,
unknown files, recovery litter, unreferenced revisions, backups, and migration
evidence. It records path type and exact bytes or symlink target. Only these
host-local files are excluded from the authority manifest:

```text
.bimri/engine.lock
.bimri/runtime.local.json
.bimri/hooks.claude.local.json
```

The transaction may replace package files, marked BIMRI instruction blocks,
the two host-local bindings, and mutable `state.json`. It MUST NOT rewrite root
`bimri.md`, accepted revisions, logs, proposals, decisions, conflicts,
resolutions, archives, recovery evidence, migrations, or unknown owner files.
The exact old state is copied to the sibling update backup before activation.
The new state version and lifecycle fields commit last, after every immutable
path has been revalidated. A caught failure restores the old state and every
authorized program, adapter, and binding path. A repeated install resumes or
rolls back idempotently from the prepared receipt.

Before releasing the lock, the installer recomputes the authority manifest and
requires identical `bimri.md` bytes, immutable path sets, types, symlink
targets, file hashes, accepted head, and head hash. It validates the new state
and then runs the new read-only audit. Success is reported only with an
explicit receipt equivalent to:

```text
BIMRI 5.1.1 installed.
Existing v5.0.2 hot memory preserved; authority state activated at v5.1.0.
Accepted head unchanged: V...... <sha256>.
Memory preservation: PASSED (bimri.md and immutable evidence unchanged).
```

If state or accepted-head authority is invalid, package replacement does not
begin. Sound state/head with damaged governance may receive the recovery tools
in explicit `installed-recovery-required` mode. The updater never repairs,
rebuilds, reindexes, truncates, or rewrites memory content as part of
installation or rollback.

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
