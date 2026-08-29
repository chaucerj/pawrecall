---
name: omnirecall
description: Search the local cross-agent conversation archive covering every past Claude Code, Codex, OpenCode, Cursor and Qoder session on this machine, regardless of which directory each agent was started in. Use when the user refers to previous conversations or prior decisions — e.g. 之前/上次/上回/记得/聊过/讨论过/以前, "we discussed this before", "I remember talking about X", "what did we decide last time" — or when a task needs context or conclusions from earlier sessions with any AI tool.
---

# omnirecall

Search ALL past AI agent conversations (Claude Code / Codex / OpenCode / Cursor) on this
machine. Path-independent: works no matter which directory an agent was started in.

## Data

- Single SQLite index at `~/.omnirecall/history.db`, kept fresh by scheduled
  incremental reindexing (or run `python3 ~/omnirecall/omnirecall.py index` manually)
- Contains conversation text only (user + assistant messages), not tool-output logs

## Preferred: MCP tools (omnirecall server)

1. `search_history(query, source?, limit?)` — full-text search; returns source,
   project path, timestamp, snippet
2. `read_session(session_id or file_path, limit?)` — read one session back in order

## Fallback: CLI (when MCP tools are unavailable, run via bash)

```bash
python3 ~/omnirecall/omnirecall.py search "keyword" [--source claude|codex|opencode|cursor] [--limit 20]
```

## Workflow

1. Distill 1-3 keywords that would **literally appear in the conversation text**
   (Chinese and English both work; this is exact substring search, not semantic)
2. Search; when presenting results, cite provenance (which tool · which project · when)
3. Use `read_session` when deeper context is needed
4. Zero results: try synonyms / more specific words; if the index may be stale
   (user just finished a session), reindex first: `python3 ~/omnirecall/omnirecall.py index`

## Discipline

- Before answering "what did we decide about X / when did we discuss Y", **search first**.
  Never fabricate or answer from memory.
- Always cite provenance when quoting past conclusions.
