# Historical BIMRI Instructions

This directory preserves the original BIMRI v1 and v3 public files for
history, migration verification, and deliberate rollback.

These files are non-executable references. Do not paste either historical
instruction block into Claude Cowork Global Instructions while BIMRI v5 is
installed. The old instructions directly rewrite the hot-memory Markdown file
and bypass v5 locking, proposals, immutable revisions, and human conflict
resolution.

Current installation instructions are in [`../INSTALL.md`](../INSTALL.md).
Migration and rollback rules are in [`../MIGRATION.md`](../MIGRATION.md).

## Preserved Versions

- [`v1/BIMRI-global-instructions.md`](v1/BIMRI-global-instructions.md) is the
  original v1 Global Instructions file.
- [`v3/BIMRI-global-instructions-v3.md`](v3/BIMRI-global-instructions-v3.md)
  is the streamlined v3 Global Instructions file.
- [`v3/README.md`](v3/README.md) is the exact README published by the canonical
  repository immediately before the v5 upgrade.
- [`v3/BIMRI-global-instructions.md`](v3/BIMRI-global-instructions.md) is a
  safety stub for the historical README's retired setup link.

Preservation does not imply current support for running these instructions.
Use them only to understand an old workspace or to perform the explicit,
quiescent rollback described in the migration guide.
