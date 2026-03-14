# BIMRI — Brief Interaction Memory & Retrieval Intelligence

A persistent memory architecture for Claude Cowork that gives your agent continuity across sessions.

Cowork doesn't have memory. Every session starts from scratch. BIMRI fixes that by maintaining a structured memory file in each workspace folder that the agent reads at session start and updates at session end. The file self-maintains through importance scoring, freshness decay, and automatic pruning — which means it gets smarter over time without growing indefinitely.

**Status:** Experimental. Running in production across multiple workspaces. Feedback welcome.

## The Problem

Every flat-file memory system hits the same wall. The agent appends summaries after each session, the file grows linearly, and after a few weeks it's eating a third of the context window before the agent does any real work. Old entries that stopped being relevant sit at the same priority as yesterday's critical decisions. Signal-to-noise collapses quietly until the memory file becomes the problem it was supposed to solve.

This is context rot. BIMRI is built specifically so it can't happen.

## How It Works

BIMRI splits memory into three tiers that serve different cognitive functions.

**Tier 1 — Core Intelligence (~3,000 tokens).** Permanent foundational knowledge. Workspace purpose, user preferences, key objectives, hard constraints. Never decays. Sits at the top of the file so the agent reads it first.

**Tier 2 — Active Context (~8,000 tokens).** Session summaries, current work state, open tasks, recent decisions. Every entry carries an importance score (1–5) and a timestamp. A freshness multiplier decays relevance over time using a static lookup table. Composite weight = importance × freshness. Entries below a threshold of 1.5 get pruned automatically.

**Tier 3 — Pattern Recognition (~3,500 tokens).** Derived behavioral insights rather than raw events. The agent identifies recurring dynamics across sessions and tracks them with confidence scores — emerging (1–2 observations), developing (3–5), established (6+). Established patterns are treated as reliable predictions.

The total file stabilizes around 10,000–15,000 tokens regardless of how many sessions you've run. At current pricing, that adds roughly 3–7 cents per conversation in input token cost.

## Key Design Decisions

**Lookup table instead of formula.** The first version used exponential decay math. Language models compute math inconsistently across sessions, which caused weight drift over time. A static 7-row lookup table eliminated that problem entirely.

**Concrete scoring examples.** Abstract importance descriptions like "changes the fundamental understanding of the workspace" mean different things to different model instances. Real example sentences anchor each score level and tighten variance.

**Automatic backup.** Every session copies bimri.md to bimri-backup.md before making any modifications. If the agent botches a write, the previous version is right there.

**When in doubt, record it.** A low-importance entry that decays naturally costs almost nothing. A missed insight is gone forever. The system is designed to over-record and let pruning handle the rest.

## Setup (90 Seconds)

You need Claude Desktop with Cowork on a paid plan.

1. Copy the contents of [`BIMRI-global-instructions.md`](BIMRI-global-instructions.md) from this repo.
2. Open Claude Desktop → Settings → Cowork → Global Instructions → Edit.
3. Paste. Save.

That's it. Every folder you open in Cowork from this point forward gets BIMRI automatically. The agent checks for a memory file, creates one if it doesn't exist, runs a brief intake to seed foundational context, and starts working. The memory file self-maintains from there.

## File Structure (Auto-Created Per Folder)

```
📁 Any-Workspace/
├── bimri.md              ← Memory file (created and maintained by agent)
├── bimri-backup.md       ← Rolling backup (created before each write)
└── 📁 working/           ← Agent scratch space
```

## Freshness Lookup Table

| Days Since Entry | Freshness Multiplier |
|-----------------|---------------------|
| 0–1             | 1.0                 |
| 2–3             | 0.8                 |
| 4–5             | 0.5                 |
| 6–10            | 0.35                |
| 11–15           | 0.2                 |
| 16–20           | 0.15                |
| 21+             | 0.1                 |

**Floor rule:** Entries scored at importance 4 or 5 have a minimum composite weight of 4.0 regardless of age.

## Maintenance

**Every session:** Backup → write new entry → recalculate weights → prune below threshold → detect patterns → confirm write. Overhead: ~300–500 tokens.

**Every 15th session:** Deep review of Core Intelligence accuracy, pattern merging, full weight recalibration, orphaned tag cleanup. Summary reported to user.

**Scheduled (optional):** Use `/schedule` in Cowork to automate weekly deep maintenance.

## Customisation

The numbers that control BIMRI's behavior all live in the global instructions file. You can adjust tier budgets, the total token target, and the maintenance cadence to suit your usage. The comments inside each bimri.md file are human-readable labels only — the agent follows the global instructions, not the file comments.

Keep in mind that total memory budget is a tradeoff. At 15,000 tokens you're using less than 10% of the 200k context window. Going beyond 30,000 starts to meaningfully reduce the agent's working capacity during sessions.

## Known Limitations

These are real and worth understanding before you deploy.

- **Agent compliance is probabilistic.** The protocol tells the agent what to do, but there's no enforcement layer guaranteeing it follows every step every session. The confirmation step ("BIMRI updated") exists so you can verify the write happened.
- **Importance scoring varies between sessions.** Concrete examples in the protocol reduce this but don't eliminate it. Different model instances may score the same type of information slightly differently.
- **Token counting is estimated.** The agent doesn't have a precise token counter. Budget thresholds are approximate. The scheduled maintenance pass helps catch drift.
- **Pattern recognition can hallucinate.** The agent derives patterns from written summaries, not from the actual conversations. Ambiguous entry language could produce false patterns. Confidence scoring mitigates this but doesn't prevent it.
- **No cross-folder intelligence.** Each workspace maintains independent memory. Knowledge doesn't transfer between folders without manual intervention.

## Contributing

This is an active experiment. If you deploy BIMRI and hit edge cases, unexpected behaviors, or have ideas for improving the architecture, open an issue. Specific feedback on scoring consistency, pattern detection accuracy, and token budget stability is especially valuable.

## License

MIT

## Author

**Stu Jordan** — Agent Orchestrator
- Community: [Evolution Unleashed](https://evolutionunleashed.com) (58,000+ members)
- Patreon: [www.patreon.com/evolutionunleashedvip](https://www.patreon.com/evolutionunleashedvip)
- Web: [evolutionunleashed.com](https://evolutionunleashed.com)
