<div align="center">
  <img src="assets/banner.svg" alt="PawRecall — every conversation leaves a print. Follow it back." width="100%">

**Every conversation you've ever had with your AI coding agents — searchable by any of them.**

Zero dependencies. MCP + CLI + agent skill. Optional semantic and fact layers that degrade gracefully to plain keyword search.

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

- **Zero dependencies at the core** — pure Python stdlib (SQLite + a few files).
  No npm install, no Rust binary, no daemon. The semantic and fact layers are
  optional extras, not requirements: with nothing configured, behavior is plain
  instant keyword search.
- **Hybrid retrieval (auto mode)** — exact substring match first (perfect for
  CJK: no tokenizer needed), semantic vectors fused in via RRF only on weak
  signals. Strong-signal queries never leave the machine (~100-150ms, zero
  network calls).
- **Distilled fact layer (v1.1)** — an optional chat model extracts decisions /
  preferences / constraints into atomic facts, ranked above raw transcripts at
  query time. "What did we decide?" gets an answer, not a reading assignment.
  Superseded facts are pushed down automatically — the newest decision wins,
  the old one stays for audit.
- **Injection budget** — search output is capped by a character budget
  (`PAWRECALL_MAX_CHARS`, default 6000) so results never blow up the agent's
  context window.
- **Verbatim recall** — stores what was actually said, not an LLM-compressed
  summary. Full sessions are always replayable via `paw_read`.
- **Deterministic trigger** — an optional UserPromptSubmit hook injects a
  "search first" reminder whenever your message references past conversations
  (之前/上次/we discussed…), so recall doesn't depend on the agent's goodwill.
- **Three delivery channels** — an MCP server for any MCP-capable agent, a CLI
  for yourself, and a skill so agents know *when* to look.

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

The agent calls `paw_search` → fact-layer hits (★事实) come first, then raw
transcript matches with tool / project / timestamp → digs into `paw_read` if it
needs the full thread.

**From your terminal:**

```bash
python3 index.py search "召回率"                    # hybrid search (auto)
python3 index.py search "MCP" --source codex        # one agent's logs only
python3 index.py facts status                       # fact layer status
python3 index.py index                              # incremental reindex (seconds)
```

## How it works

```
~/.claude/projects/**/*.jsonl ─┐
~/.codex/sessions/**/*.jsonl  ─┤
~/.zcode/cli/rollout/*.jsonl  ─┼─→ index.py ─→ ~/.pawrecall/history.db
opencode.db / Cursor / Qoder  ─┘       │        ├─ msgs          原文（always on）
                                       │        ├─ vecs         语义向量（embed.json）
                                       │        └─ facts        原子事实（facts.json）
                                       ├─ MCP (stdio)   → any agent
                                       ├─ CLI           → you
                                       └─ skill         → when to look
```

Indexing is incremental (mtime/size tracked per file). A LaunchAgent or cron job
keeps it fresh every 30 minutes; the same pass backfills vectors and extracts
facts in small batches, so it never runs long. Every layer degrades gracefully:
no embedding provider → exact search; no chat model → no facts; neither → the
original instant keyword search.

## The fact layer (v1.1)

```bash
cp facts.json.example ~/.pawrecall/facts.json   # point it at any OpenAI-compatible
                                                # chat endpoint (or ollama pull qwen3:4b)
```

After each indexing pass, recent sessions are distilled into atomic facts
(`decision` / `preference` / `constraint` / `fact`) with full provenance
(source · project · session · timestamp). At query time `paw_search` puts matching
facts first; `paw_facts` browses the layer, and `include_superseded=true` audits
how a decision evolved. The original transcript is never touched.

**Contradictions are human-in-the-loop by default.** When the extraction model
thinks a new fact contradicts an older one, it only gets a *proposal* — the pair
is queued as pending and both facts stay visible, flagged `⚠ pending conflict`
in search results. A human (or the agent, after asking the user) resolves each
one via `paw_facts_resolve`: the new fact wins, the old one wins, or it wasn't a
contradiction at all. Every verdict is audited (`resolved_by`). Opt out with
`conflict_mode: auto` (trust the model, legacy behavior) or add an escape hatch
with `auto_resolve_days` (auto-close unresolved conflicts after N days).

## Evaluation

Two layers of measurement, both run against a scratch copy of your database
(`PAWRECALL_DB`) — never the live index:

- **Personal regression** — `eval.py` on your own labeled cases, plus
  `scripts/recall_test.py` for paraphrase / cross-language recall.
- **Public benchmark** — `bench/bench_locomo.py` runs the
  [LoCoMo](https://huggingface.co/datasets/snap-research/locomo) long-conversation
  dataset through the real pipeline and reports evidence recall@k / MRR
  (judge-free, reproducible), with optional LLM answer scoring:

```bash
python3 bench/bench_locomo.py --hf --rebuild --mode semantic --cats 1,2
python3 bench/bench_locomo.py --data locomo10.json --judge      # needs facts.json
```

First public numbers (bge-m3 embeddings, evidence recall@5, LoCoMo categories 1-2,
602 questions): **30.9% / MRR 0.165** for pure semantic retrieval. These are
baseline numbers for regression tracking, not comparable to LoCoMo's official
LLM-judged protocol — wire up the fact layer and `--judge` for the answer-quality
view.

## Comparison (as of Oct 2026 — check the repos for current state)

| | **pawrecall** | [claude-mem](https://github.com/thedotmack/claude-mem) | [Hy-Memory 2.0](https://github.com/Tencent-Hunyuan/Hy-Memory) | [memex](https://github.com/nicosuave/memex) |
|---|---|---|---|---|
| Agents covered | Claude Code, Codex, OpenCode, Cursor, Qoder*, ZCode | many | ~10 (via MemHub) | 10 CLI agents |
| Memory type | **verbatim + optional distilled facts** | LLM-compressed summaries | distilled (model 30B) | verbatim |
| Dependencies | **stdlib only** (vectors/facts optional) | runtime + models | GPU / 30B MoE model | Rust binary (brew) |
| Degraded mode | **plain keyword search, zero config** | n/a | n/a | n/a |
| CJK / Chinese search | **✓ substring, tokenizer-free** | ? | ✓ | ✗ (BM-25, whitespace) |
| Model calls / cost | **none by default** | yes | yes (extraction model) | optional (embeddings) |

Different tools, different philosophies: Hy-Memory distills with a dedicated 30B
model; pawrecall indexes verbatim first and lets you add distillation one optional
layer at a time. Use both if you like — they don't conflict.

## Scoped search & session catalog

`paw_search` accepts a `project` filter (directory substring). Agents are
instructed — via the tool description, the skill, and the injected rules — to
scope searches to the current project and go global only on explicit request.
No keyword? Browse the classified session catalog or the fact layer:

```bash
python3 index.py sessions --project jiuge-mall    # one entry per chat: source, count, topic
python3 index.py facts status                     # distilled decisions so far
```

Via MCP, `paw_sessions` and `paw_facts` expose the same views to every agent.
The index itself is local-only (`chmod 600`) and never leaves the machine.

## FAQ

**Is my data sent anywhere?**
No. Everything stays in `~/.pawrecall/` on your machine. The MCP server talks stdio,
not network. Optional layers call out only to the endpoints *you* configure in
`embed.json` / `facts.json` (e.g. a local Ollama).

**Semantic search?** Built in as an optional layer — `ollama pull bge-m3` (or any
OpenAI-compatible embedding API / `pip install fastembed`) and hybrid retrieval
turns on by itself. Without it you get exact substring search: predictable,
instant, zero cost.

**Fact extraction?** Optional too — point `facts.json` at any chat model. Small
local models (qwen3:4b class) are enough. Without it, nothing changes.

**Which sources are indexed?** Currently Claude Code, Codex CLI, OpenCode,
Cursor (from its global SQLite store, project-mapped via workspace metadata),
Qoder (session titles, agent memories and continuation summaries — its
message bodies are client-side encrypted, so only plaintext is indexed), and
ZCode Desktop (from its rollout transcripts `~/.zcode/cli/rollout/*.jsonl`,
with the project path recovered from environment injections inside the logged
conversation). Trae
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
