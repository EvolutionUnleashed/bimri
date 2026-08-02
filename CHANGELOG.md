# Changelog

This file records the public BIMRI architecture history. Historical instruction
files are preserved under [`legacy/`](legacy/) and are not current installers.

## 5.0.0

- Made BIMRI a portable memory layer for Claude, Codex, and other local agents.
- Replaced direct hot-file writes with isolated run logs, proposals, immutable
  revisions, stable keys, deterministic conflict detection, and durable human
  resolutions.
- Made `bimri.md` a bounded generated view while preserving full evidence and
  history under `.bimri/`.
- Added cross-process locking, atomic durable writes, crash recovery,
  byte-exact direct-edit preservation, transactional installation, validation,
  and a rebuildable retrieval index.
- Added direct, conservative migration from v1-v3 tiered workspaces and
  engine-based v4 workspaces, with byte-exact backups and deterministic,
  semantically validated source-to-v5 authority records.
- Made rollback self-contained by packaging the inert historical references
  and BIMRI's MIT notice without replacing a target project's own license.
- Kept Claude Code hooks as an optional adapter rather than a requirement.

## 3.0.0

- Simplified the public Claude Cowork protocol to one active memory file and
  one rolling backup.
- Defined memory deltas, day-and-session freshness, bounded tiers, mandatory
  pruning, and stronger separation between active context and session history.
- Used uppercase filenames for new workspaces while retaining lowercase
  compatibility.

The exact pre-v5 public explanation is preserved at
[`legacy/v3/README.md`](legacy/v3/README.md).

## 1.0.0

- Introduced Brief Interaction Memory and Retrieval Intelligence as a tiered
  Markdown memory protocol for Claude Cowork.
- Defined core intelligence, active context, pattern recognition, freshness,
  backups, and session maintenance through Global Instructions.
