@AGENTS.md

## Claude Code

Use `BIMRI-PROTOCOL.md` as the normative memory protocol.

When the hooks from `hooks-example.json` are installed, `hook-start` uses
Claude Code's session ID to allocate or resume the correct run and injects the
BIMRI brief. `hook-close` closes only that mapped session. Other agents may be
working in the same folder and must never be treated as orphans.

Without hooks, use the explicit start and close commands in `AGENTS.md`.
