<div align="center">

# omnirecall

**Every conversation you've ever had with your AI coding agents — searchable by any of them.**

One Python file. Zero dependencies. MCP + CLI + agent skill.

[中文文档](docs/zh-CN.md) · MIT License

</div>

---

## The problem

You use several AI coding agents. Six months ago one of them helped you solve exactly
the problem you're facing now — but you can't remember **which agent**, or **which
project folder** you were in when it happened.

Every agent quietly saves its transcripts on your disk — but each one indexes them by
its own working directory, so `claude --resume` only knows about the folder you're in.
Your history isn't lost. It's just scattered.

**omnirecall** indexes all of those transcripts into one SQLite database and hands them
back to every agent through a single MCP server — path-independent, agent-independent.

## Why omnirecall

- **One file, zero dependencies** — pure Python stdlib. No npm install, no Rust binary,
  no embedding model download, no daemon. Read the entire codebase in ten minutes.
- **Perfect CJK search** — tokenizer-free substring matching. BM-25 and FTS-based tools
  break on Chinese/Japanese text (no whitespace to tokenize); omnirecall doesn't care.
- **Verbatim recall** — stores what was actually said, not an LLM-compressed summary.
  Free (no model calls), and the details survive.
- **Three delivery channels** — an MCP server for any MCP-capable agent, a CLI for
  yourself, and a skill so agents know *when* to look.

## Install

```bash
git clone https://github.com/YOUR_NAME/omnirecall.git
cd omnirecall && ./install.sh          # add --with-scheduler for 30-min auto-reindex (macOS)
```

`install.sh` indexes your history, registers the MCP server (Claude Code if present),
and installs the agent skill. No sudo, nothing outside your home directory.

Requirements: Python 3.9+ (that's it).

## Use it

**From any agent** (after restart):

> "I think we discussed resume project ordering with some AI before — find it."

The agent calls `search_history` → sees which tool, which project, when → digs into
`read_session` if it needs the full thread.

**From your terminal:**

```bash
python3 omnirecall.py search "召回率"                  # global search
python3 omnirecall.py search "MCP" --source codex      # one agent's logs only
python3 omnirecall.py index                            # incremental reindex (seconds)
```

## How it works

```
~/.claude/projects/**/*.jsonl ─┐
~/.codex/sessions/**/*.jsonl  ─┼─→ omnirecall.py index ─→ ~/.omnirecall/history.db
opencode.db (SQLite)          ─┘                                   │
                                                                    │ MCP (stdio)  → any agent
                                                                    │ CLI          → you
                                                                    └ skill        → when to look
```

Indexing is incremental (mtime/size tracked per file). A LaunchAgent or cron job can
keep it fresh every 30 minutes; without one, run `index` whenever you like.

## Comparison (as of Feb 2026 — check the repos for current state)

| | **omnirecall** | [claude-mem](https://github.com/thedotmack/claude-mem) | [memex](https://github.com/nicosuave/memex) | [claude-historian-mcp](https://github.com/Vvkmnn/claude-historian-mcp) | [crispy-recall](https://github.com/TheSylvester/crispy-recall) |
|---|---|---|---|---|---|
| Agents covered | Claude Code, Codex, OpenCode | many | 10 CLI agents | Claude Code only | Claude Code + Codex |
| Memory type | **verbatim transcripts** | LLM-compressed summaries | verbatim | verbatim | verbatim |
| Dependencies | **1 file, stdlib only** | runtime + models | Rust binary (brew) | Node/npm | Node + embedding runtime |
| MCP server | ✓ | ✓ | — | ✓ | ✓ |
| Agent skill | ✓ | ✓ | ✓ | — | ✓ |
| CJK / Chinese search | **✓ substring, tokenizer-free** | ? | ✗ (BM-25, whitespace) | ? | ? |
| Model calls / cost | **none** | yes (compression) | optional (embeddings) | none | optional (local embeddings) |

Different tools, different philosophies: claude-mem *compresses* your history,
omnirecall *indexes* it. Use both if you like — they don't conflict.

## FAQ

**Is my data sent anywhere?**
No. Everything stays in `~/.omnirecall/` on your machine. The MCP server talks stdio,
not network.

**Semantic search?** Not built in — this is exact substring search by design
(predictable, instant, zero cost). For "I know the concept but not the words" queries,
the roadmap includes an optional local-embedding mode.

**Which sources are indexed?** Currently Claude Code, Codex CLI, and OpenCode.
Adding a new agent = one parser function (~30 lines). PRs welcome.

**Does it store my secrets?** It indexes everything said in your conversations,
so treat the DB like your transcripts: it's created `chmod 600`, local-only.

## License

MIT
