# Install BIMRI

## The One-Sentence Installation

Tell any local coding agent:

> Install BIMRI in this project from
> https://github.com/EvolutionUnleashed/bimri. Follow INSTALL.md,
> preserve my existing instructions and memory, and run the self-check.

The owner should not need to copy files, configure a database or answer setup
questions.

Commands below use `python3`. The agent must use whichever executable provides
Python 3.8 or newer: commonly `python3` outside Windows, and `python` on a
standard Windows installation. Adapt POSIX line continuation and absolute-path
examples to the active Windows shell when needed.

## Agent Installation Contract

1. Obtain this repository in a temporary local folder.
2. Inspect the target for BIMRI v1-v4 or v5 files. Before upgrading any
   earlier version, stop every agent and command using that folder and keep it
   quiescent until installation completes.
3. If Claude Cowork Global Instructions contain a v1-v3 BIMRI block, ask
   the owner to disable or remove it before installation. Those instructions
   directly rewrite the old hot-memory file and must not run alongside v5.
4. From the temporary folder run:

   ```text
   python3 bimri-engine.py install --target /absolute/path/to/the/project
   ```

5. Do not replace existing `AGENTS.md` or `CLAUDE.md`. The installer merges a
   marked BIMRI block and backs up files it upgrades. It packages `legacy/` as
   inert rollback material and copies BIMRI's MIT notice to `BIMRI-LICENSE`;
   neither action replaces the target project's root `LICENSE`.
6. Existing BIMRI v1-v4 memory migrates automatically and
   idempotently. Migration preserves exact source and backup bytes, records
   their hashes, and stops without overwriting ambiguous or malformed input.
7. Run `python3 bimri-engine.py doctor` from the target project if you need to
   repeat the self-check.
8. Report only:
   - BIMRI version installed
   - whether memory was initialized or migrated
   - adapters enabled
   - whether doctor passed
   - any bounded-memory repair warning printed by the installer

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

## Claude Code Hooks

The universal instructions work without hooks. For automatic Claude Code start
and close, merge `hooks-example.json` into `.claude/settings.json` and verify
it with `/hooks`. Preserve all existing settings.

Hooks are an adapter, not part of the memory. Claude, Codex and another local
agent can all use the same BIMRI folder when they share one verified lock
domain. Set hook commands to the local Python 3.8+ executable; do not assume
that `python3` exists on Windows.

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
