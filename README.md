<div align="center">
  <img src="assets/banner.svg" alt="PawRecall — every conversation leaves a print. Follow it back." width="100%">

**Every conversation you've ever had with your AI coding agents — searchable by any of them.**

One Python file. Zero dependencies. MCP + CLI + agent skill.

[中文文档](docs/zh-CN.md) · [日本語](docs/ja.md) · MIT License

</div>

---

## The problem

You use several AI coding agents. Six months ago one of them helped you solve exactly
the problem you're facing now — but you can't remember **which agent**, or **which
project folder** you were in when it happened.

Every agent quietly saves its transcripts on your disk — but each one indexes them by
its own working directory, so `claude --resume` only knows about the folder you're in.
Your history isn't lost. It's just scattered.

**pawrecall** indexes all of those transcripts into one SQLite database and hands them
back to every agent through a single MCP server — path-independent, agent-independent.

## Why pawrecall

- **One file, zero dependencies** — pure Python stdlib. No npm install, no Rust binary,
  no embedding model download, no daemon. Read the entire codebase in ten minutes.
- **Perfect CJK search** — tokenizer-free substring matching. BM-25 and FTS-based tools
  break on Chinese/Japanese text (no whitespace to tokenize); pawrecall doesn't care.
- **Verbatim recall** — stores what was actually said, not an LLM-compressed summary.
  Free (no model calls), and the details survive.
- **Deterministic trigger** — an optional UserPromptSubmit hook injects a "search first"
  reminder whenever your message references past conversations (之前/上次/we discussed…),
  so recall doesn't depend on the agent's goodwill.
- **Three delivery channels** — an MCP server for any MCP-capable agent, a CLI for
  yourself, and a skill so agents know *when* to look.

## Install

```bash
git clone https://github.com/chaucerj/pawrecall.git
cd pawrecall && ./install.sh          # add --with-scheduler for 30-min auto-reindex (macOS)
```

`install.sh` indexes your history, registers the MCP server (Claude Code if present),
and installs the agent skill. No sudo, nothing outside your home directory.

Requirements: Python 3.9+ (that's it).

## Use it

**From any agent** (after restart):

> "I think we discussed resume project ordering with some AI before — find it."

The agent calls `paw_search` → sees which tool, which project, when → digs into
`paw_read` if it needs the full thread.

**From your terminal:**

```bash
python3 pawrecall.py search "召回率"                  # global search
python3 pawrecall.py search "MCP" --source codex      # one agent's logs only
python3 pawrecall.py index                            # incremental reindex (seconds)
```

## How it works

```
~/.claude/projects/**/*.jsonl ─┐
~/.codex/sessions/**/*.jsonl  ─┼─→ pawrecall.py index ─→ ~/.pawrecall/history.db
opencode.db (SQLite)          ─┘                                   │
                                                                    │ MCP (stdio)  → any agent
                                                                    │ CLI          → you
                                                                    └ skill        → when to look
```

Indexing is incremental (mtime/size tracked per file). A LaunchAgent or cron job can
keep it fresh every 30 minutes; without one, run `index` whenever you like.

## Evaluation

Don't take the numbers on faith — `scripts/eval.py` measures the pipeline on
your own corpus:

- **A. Indexing fidelity** — samples verbatim messages from the raw transcripts,
  verifies each exists in the DB after by-design filtering (target: 100%)
- **B. Query recall** — cuts random substrings out of indexed messages and runs
  the real search path; probes escaping edge cases (`%`, `_`, quotes, CJK punctuation)
- **C. Labeled precision@3** — optional; put your own ground-truth cases in
  `~/.pawrecall/eval_cases.json` (see `scripts/eval_cases.example.json`)

```bash
python3 pawrecall.py --db /tmp/omr.db index
python3 scripts/eval.py --db /tmp/omr.db --samples 300
```

Results on a ~32k-message corpus (macOS, Feb 2026): fidelity 100%, random-substring
recall ≈ 90% (misses are top-K window truncation on very common substrings — the
strings are in the DB, they just rank below the window), strict labeled precision@3
5/6. The harness found two real bugs during development (timestamp-vs-insertion-order
ranking; multi-word queries broken by spacing) — both fixed.

## Comparison (as of Feb 2026 — check the repos for current state)

| | **pawrecall** | [claude-mem](https://github.com/thedotmack/claude-mem) | [memex](https://github.com/nicosuave/memex) | [claude-historian-mcp](https://github.com/Vvkmnn/claude-historian-mcp) | [crispy-recall](https://github.com/TheSylvester/crispy-recall) |
|---|---|---|---|---|---|
| Agents covered | Claude Code, Codex, OpenCode, Cursor, Qoder* | many | 10 CLI agents | Claude Code only | Claude Code + Codex |
| Memory type | **verbatim transcripts** | LLM-compressed summaries | verbatim | verbatim | verbatim |
| Dependencies | **1 file, stdlib only** | runtime + models | Rust binary (brew) | Node/npm | Node + embedding runtime |
| MCP server | ✓ | ✓ | — | ✓ | ✓ |
| Agent skill | ✓ | ✓ | ✓ | — | ✓ |
| CJK / Chinese search | **✓ substring, tokenizer-free** | ? | ✗ (BM-25, whitespace) | ? | ? |
| Model calls / cost | **none** | yes (compression) | optional (embeddings) | none | optional (local embeddings) |

Different tools, different philosophies: claude-mem *compresses* your history,
pawrecall *indexes* it. Use both if you like — they don't conflict.

## Scoped search & session catalog

`paw_search` accepts a `project` filter (directory substring). Agents are
instructed — via the tool description, the skill, and the injected rules — to
scope searches to the current project and go global only on explicit request.
No keyword? Browse the classified session catalog instead:

```bash
python3 pawrecall.py sessions --project jiuge-mall     # one entry per chat: source, count, topic
python3 pawrecall.py sessions --source cursor          # everything Cursor discussed
```

Via MCP, `paw_sessions` exposes the same catalog to every agent. The index
itself is local-only (`chmod 600`) and never leaves the machine.

## FAQ

**Is my data sent anywhere?**
No. Everything stays in `~/.pawrecall/` on your machine. The MCP server talks stdio,
not network.

**Semantic search?** Not built in — this is exact substring search by design
(predictable, instant, zero cost). For "I know the concept but not the words" queries,
the roadmap includes an optional local-embedding mode.

**Which sources are indexed?** Currently Claude Code, Codex CLI, OpenCode,
Cursor (from its global SQLite store, project-mapped via workspace metadata),
and Qoder (session titles, agent memories and continuation summaries — its
message bodies are client-side encrypted, so only plaintext is indexed). Trae
stores chats in an encrypted proprietary database and cannot be indexed; its
agent can still *search* the index via the CLI fallback.

\* Qoder: titles/memories/summaries only.
Adding a new agent = one parser function (~30 lines). PRs welcome.

**Does it store my secrets?** It indexes everything said in your conversations,
so treat the DB like your transcripts: it's created `chmod 600`, local-only.

## Contact & Support

- 🐛 Bugs & feature requests: [open an issue](https://github.com/chaucerj/pawrecall/issues) — preferred, public discussion helps everyone
- 📬 Private inquiries / commercial usage: **767092677@qq.com**
- ⭐ If PawRecall saves you time, a star or a PR is the best support. Donation link lives in the sidebar ("Sponsor this project").

## License

MIT
