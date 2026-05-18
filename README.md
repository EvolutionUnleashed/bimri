# BIMRI v3 — Brief Interaction Memory & Retrieval Intelligence

A compact persistent-memory protocol for Claude Cowork.

Cowork starts each fresh chat without reliable folder-level memory. BIMRI v3 fixes that by maintaining one structured memory file inside each workspace folder. Claude reads it at session start, uses it as local working context, and updates it before the final response.

BIMRI v3 is intentionally small: one active memory file, one rolling backup, no protocol sprawl.

**Status:** Experimental. Running in production across multiple workspaces. Feedback welcome.

## The Problem

Flat-file memory systems usually fail the same way: they become diaries.

The agent appends a session summary every time, stale context never truly leaves, and the file quietly becomes a context swamp. The result is worse than forgetting: old noise competes with current signal.

BIMRI v3 is designed around active memory, not historical logging.

## What Changed in v3

BIMRI v3 is a simplification and pruning hardening release.

Key changes:

- Uses only `BIMRI.md` and `BIMRI-backup.md` by default.
- Stops creating `working/`, `.bimri/`, `AGENTS.md`, `CLAUDE.md`, `INSTRUCTIONS.md`, archive folders, or extra backups.
- Replaces automatic session summaries with memory deltas.
- Adds session-based decay as well as date-based decay, so multiple Cowork chats in one day still age memory.
- Removes the IMPORTANCE 4/5 floor rule.
- Makes pruning mandatory and destructive inside active memory.
- Confirms updates with counts instead of only saying “BIMRI updated.”

## How It Works

BIMRI splits memory into three tiers.

**Tier 1 — Core Intelligence (~3,000 tokens).** Durable workspace purpose, permanent user preferences, standing constraints, and operating principles. Tier 1 does not decay.

**Tier 2 — Active Context (~6,000 tokens or 30 entries).** Current useful context only: active projects, recent decisions that still matter, open loops, and near-future context. Tier 2 is not a session log.

**Tier 3 — Pattern Recognition (~3,500 tokens).** Repeated behaviors, decision patterns, workflow preferences, strategic themes, and recurring constraints. Patterns are compressed so BIMRI does not need to preserve every raw observation.

Whole-file target: under ~12,000 tokens. Hard ceiling: 15,000 tokens.

## File Structure

Auto-created per workspace folder:

```txt
Any-Workspace/
├── BIMRI.md          ← Active memory file
└── BIMRI-backup.md   ← Rolling backup before each write
```

Legacy compatibility:

```txt
Any-Workspace/
├── bimri.md
└── bimri-backup.md
```

If a folder already has lowercase files, BIMRI v3 uses them and does not create duplicates. New folders use uppercase by default.

## Setup

You need Claude Desktop with Cowork.

1. Copy the contents of [`BIMRI-global-instructions.md`](BIMRI-global-instructions.md).
2. Open Claude Desktop → Settings → Cowork → Global Instructions → Edit.
3. Paste.
4. Save.

Every folder opened in Cowork from that point forward gets BIMRI automatically.

## Freshness Scoring

BIMRI v3 calculates freshness from `LAST_USED`, not only creation date.

Each Tier 2 entry tracks both:

- days since last used
- sessions since last used

The lower multiplier wins.

### Days Since Last Used

| Days | Multiplier |
|---|---:|
| 0–1 | 1.0 |
| 2–3 | 0.8 |
| 4–5 | 0.5 |
| 6–10 | 0.35 |
| 11–15 | 0.2 |
| 16–20 | 0.15 |
| 21+ | 0.1 |

### Sessions Since Last Used

| Sessions | Multiplier |
|---|---:|
| 0–1 | 1.0 |
| 2–3 | 0.8 |
| 4–6 | 0.5 |
| 7–10 | 0.35 |
| 11–15 | 0.2 |
| 16–25 | 0.15 |
| 26+ | 0.1 |

Composite weight:

```txt
IMP × freshness multiplier
```

There is no floor rule.

If an important Tier 2 item becomes stale, it should be promoted into Tier 1, merged into Tier 3, or pruned from active memory.

## Maintenance

Every session:

```txt
Backup → refresh weights → add useful memory delta if warranted → promote/compress → update patterns → prune → enforce budgets → update header → confirm counts
```

Confirmation format:

```txt
BIMRI updated: +X active, +Y core, +Z patterns, pruned N, Tier 2 now M entries, ~T tokens.
```

Every 15th session, BIMRI performs stricter maintenance: merge redundant Tier 1 entries, merge duplicate Tier 3 patterns, prune Tier 2 aggressively, remove stale completed context, and compress wording.

## Design Principles

### BIMRI is active memory, not a diary

A session only gets written if remembering it will improve future work.

### Tier 2 has no immortal entries

If something matters permanently, it belongs in Tier 1. If something repeats, it belongs in Tier 3. If something is done, stale, low-weight, or historical, it leaves active BIMRI.

### Pruned means removed

BIMRI v3 does not merely flag stale entries. It removes them from active memory. The backup file is the rollback point.

### Keep the filesystem clean

BIMRI v3 works in any folder with any Cowork agent. It does not spawn protocol folders or scratch directories by default.

## Known Limitations

These are real and worth understanding before you deploy.

- **Agent compliance is probabilistic.** The protocol tells the agent what to do, but there is no enforcement layer guaranteeing it follows every step every session.
- **Importance scoring still varies.** Concrete rules reduce variance, but different model instances may score the same memory candidate slightly differently.
- **Token counting is estimated.** The agent does not have a precise token counter. Budget thresholds are approximate.
- **Pattern recognition can hallucinate.** Patterns are derived from written memory, not from the full conversation history. Confidence scoring helps but does not eliminate this.
- **No cross-folder intelligence.** Each workspace maintains independent memory unless you manually copy context between folders.

## Migration from Earlier Versions

Existing `bimri.md` files continue to work.

For best results, ask Claude Cowork to perform a v3 migration pass in each workspace:

```txt
Migrate this BIMRI file to v3. Keep the three-tier structure. Convert useful session summaries into memory deltas, promote durable context into Tier 1, merge repeated observations into Tier 3, remove stale Tier 2 entries, update all weights using date + session freshness, and confirm counts.
```

## Contributing

This is an active experiment. If you deploy BIMRI and hit edge cases, unexpected behaviors, or have ideas for improving the architecture, open an issue.

Specific feedback on pruning reliability, session-based decay, pattern compression, and token budget stability is especially valuable.

## License

MIT

## Author

**Stu Jordan** — Agent Orchestrator

- Community: [Evolution Unleashed](https://evolutionunleashed.com)
- Patreon: [www.patreon.com/evolutionunleashedvip](https://www.patreon.com/evolutionunleashedvip)
- Web: [evolutionunleashed.com](https://evolutionunleashed.com)
