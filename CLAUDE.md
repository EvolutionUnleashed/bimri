@AGENTS.md

## Claude Code

Use `BIMRI-PROTOCOL.md` as the normative memory protocol.

`<verified-python>` in BIMRI instructions means the exact absolute Python 3.8+
executable verified for this machine according to `INSTALL.md`. Never assume a
PATH name executed correctly; zero output is failure. After moving this folder
to another host or replacing Python, rerun the self-contained installer against
this folder, or use a clean BIMRI source, with the newly verified executable
before replacing and smoke-testing the local hooks.

The installer writes `.bimri/runtime.local.json` as the host-only runtime
binding record and `.bimri/hooks.claude.local.json` as the host-only rendered
Claude hook source. They are local adapter files, not memory authority. Do not
commit either file, reuse it on another host, or copy its absolute paths into
shared instructions.

When the rendered hooks from `.bimri/hooks.claude.local.json` are enabled,
merge them into machine-local `.claude/settings.local.json`, preserving
unrelated hooks and replacing previous BIMRI entries. Keep the absolute
interpreter path out of shared `.claude/settings.json`. Inspect the result with `/hooks`, smoke-test
both hooks with one synthetic session ID, require their expected non-empty
output, confirm the test run closes, and run `doctor`.

`hook-start` uses Claude Code's session ID to allocate or resume the correct
run and injects the BIMRI brief. `hook-close` closes only that mapped session.
Other agents may be working in the same folder and must never be treated as
orphans.

Without hooks, use the explicit start and close commands in `AGENTS.md`.
