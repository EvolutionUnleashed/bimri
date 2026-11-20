@AGENTS.md

## Claude Code

Use BIMRI engine v5.2.0, authority format v5.1.0 and state format v5.2.0. The
readable hot-memory grammar remains v5.0.2.
`BIMRI-PROTOCOL.md` is the normative memory protocol.

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
both hooks with one synthetic session ID, require the read brief from
`hook-start` and exit 0 from `hook-close`, and run `doctor`.

`hook-start` injects the quiet read brief and opens nothing; a chat remembers
with `save`. It does not replay open reviews. Use the pull-based `review`
command when a concurrent choice matters to current work or the owner asks to
inspect pending memory. `hook-close` closes a mapped legacy run only when
nothing in it is left undecided; anything undecided waits for the owner's word.
Other agents may be working in the same folder and must never be treated as
orphans.

Without hooks, use the explicit start and save commands in `AGENTS.md`.
