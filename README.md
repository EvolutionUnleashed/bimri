# BIMRI: Local, Persistent Memory You Can Share Across AI Agents

BIMRI keeps your agents' project knowledge on your own machine, ready to use across sessions and supported agents. You can change the agent you work with, or use several on the same project, while keeping the decisions, corrections and context you've already built up.

That knowledge becomes more useful as your project develops. Each saved decision gives future work more context, and each recorded correction helps you avoid explaining the same thing again. You keep it when you move between supported tools.

Works with Claude Code, OpenAI Codex and other supported local agents. Free and open source under the MIT license.

## Why Use BIMRI?

- **You own the memory.** It is stored in your project folder as ordinary files you can inspect, back up and move.
- **It runs locally.** The memory engine needs no hosted service, database, account or API key. It runs on Python without extra packages.
- **You can choose your agents.** BIMRI is independent of any one agent provider. A new supported agent can use the project knowledge you've already saved.
- **Several agents can share what they know.** Agents working on the same project can contribute to one memory. Independent updates can be combined; conflicting changes to the same subject are kept for you to resolve.
- **Your decisions survive the session.** Once recorded as confirmed, your instruction cannot be silently replaced in memory by an agent's inference.

Concurrent sharing requires agents to use the BIMRI engine in the same supported filesystem environment on one machine. See [requirements and limits](#requirements-and-limits) before connecting different runtimes.

## A Simple Example

Suppose you're building a booking app with two agents. One works on the booking flow while another investigates the calendar integration. Later, you switch to a different supported agent to continue the project. These are fictional examples of the knowledge they can share through BIMRI:

| What happens | What BIMRI keeps for later work |
| --- | --- |
| You decide that customers can book without creating an account. | The confirmed decision, available to the next agent working on the app. |
| An agent discovers that a calendar integration uses UTC. | A saved finding, with its source recorded. |
| You change the cancellation period from 48 hours to 24 hours. | The current decision, with the previous version retained in history. |
| Two agents concurrently propose different changes to the same cancellation rule. | A conflict you can resolve, so one change does not silently replace the other. |

Agents still need to read and use the memory. BIMRI preserves what is recorded; it does not guarantee that an agent will follow every instruction correctly.

## Get Started

Give your local coding agent access to the project folder and paste:

```text
Install BIMRI in this project from https://github.com/EvolutionUnleashed/bimri.
Follow INSTALL.md, preserve my existing instructions and memory, and run the
self-check.
```

The [installation guide](https://github.com/EvolutionUnleashed/bimri/blob/main/INSTALL.md) covers setup and runtime requirements. For an existing installation, follow the [upgrade and migration guide](https://github.com/EvolutionUnleashed/bimri/blob/main/MIGRATION.md).

## How BIMRI Keeps Memory Useful

**It keeps track of what is current.** Each subject has one current entry. Updating a decision replaces that entry while retaining its history, so old and new answers do not accumulate as equally current instructions.

**It distinguishes your decisions from an agent's conclusions.** Facts and current work record their source and trust level. The engine keeps an agent's proposed change separate when it would replace something you confirmed.

**It handles updates from multiple agents.** Independent changes can merge automatically. Incompatible concurrent changes to the same subject are recorded for resolution. The [reference guide](https://github.com/EvolutionUnleashed/bimri/blob/main/REFERENCE.md) explains the exact conflict rules.

**It limits what loads at the start.** Agents read a bounded working memory. Other saved knowledge remains on disk and can be retrieved when needed, instead of loading the entire history into every session.

**It stores memory without another model call.** The engine uses fixed Python rules to accept, store and retrieve entries. It does not ask a model to rewrite saved text. The agent decides what to propose for memory.

**It preserves recovery information.** Accepted changes have a version history. Interrupted writes can be checked and recovered using the engine's recovery process.

## Requirements and Limits

Python 3.8 or newer and a local agent that can read project files, follow BIMRI's instructions and run its engine commands. The installer verifies the Python executable.

Claude Code and OpenAI Codex use project instructions to work with BIMRI. Other local agents need the same file and command access. Cowork support is limited to the documented local execution setup; cloud sessions are unsupported. See [runtime setup](https://github.com/EvolutionUnleashed/bimri/blob/main/INSTALL.md).

- Concurrent writers must share the same operating-system locking environment and use the engine. Multiple machines, synced folders and unverified VM or container boundaries are outside the sharing guarantee. Cowork's VM and native agents must not use the memory concurrently.
- To move memory, stop its agents and BIMRI processes, copy the project while it is idle, then rerun installation in the new environment.
- Retrieval uses subject keys and text search. Semantic search with embeddings is not included.
- BIMRI makes no network calls. Your agent provider's own data handling still applies to anything the agent reads.
- Filesystem permissions control who can change the files. Memory source labels are not access controls.

## Documentation

| Task | Guide |
| --- | --- |
| Install or configure BIMRI | [Installation](https://github.com/EvolutionUnleashed/bimri/blob/main/INSTALL.md) |
| Upgrade an existing installation | [Migration](https://github.com/EvolutionUnleashed/bimri/blob/main/MIGRATION.md) |
| Store and retrieve your first memory | [Quick start](https://github.com/EvolutionUnleashed/bimri/blob/main/REFERENCE.md#quick-start-store-and-retrieve-project-memory) |
| Understand commands, sharing and recovery | [Reference](https://github.com/EvolutionUnleashed/bimri/blob/main/REFERENCE.md) |
| Read the full memory rules | [Protocol](https://github.com/EvolutionUnleashed/bimri/blob/main/BIMRI-PROTOCOL.md) |
| Review changes or report a problem | [Changelog](https://github.com/EvolutionUnleashed/bimri/blob/main/CHANGELOG.md) · [Issues](https://github.com/EvolutionUnleashed/bimri/issues) |

Created by Stu Jordan. [AgentGuru](https://agentguru.ai) · [MIT license](https://github.com/EvolutionUnleashed/bimri/blob/main/LICENSE)
