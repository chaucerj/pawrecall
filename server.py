#!/usr/bin/env python3
"""pawrecall: a zero-dependency MCP stdio server.

Exposes the local cross-agent conversation index (~/.pawrecall/history.db,
built by index.py) to ANY MCP-capable coding agent, regardless of the
directory the agent was started in.

Tools:
  - search_history: full-text search across all Claude Code / Codex / OpenCode logs
  - read_session:   read one session's messages back in order

Register with:
  claude mcp add --scope user pawrecall -- python3 ~/.pawrecall/server.py
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from index import DB_PATH, search, fmt_results, list_sessions, fmt_sessions  # noqa: E402
from facts import (facts_search, list_facts, fmt_facts,  # noqa: E402
                   list_conflicts, fmt_conflicts, resolve_conflict)

HOME = Path.home()
SERVER_INFO = {"name": "pawrecall", "version": "1.2.0"}

TOOLS = [
    {
        "name": "paw_search",
        "description": (
            "Search past AI coding-agent conversations on this machine "
            "(Claude Code, Codex, OpenCode, Cursor, Qoder, ZCode), regardless of which folder each agent "
            "was started in. IMPORTANT scoping rule: pass the current project's directory name "
            "(or path substring) as 'project' to search only that project's history; search "
            "without 'project' (full scan) ONLY when the user explicitly asks for cross-project "
            "or global search. Use at the start of a task or whenever the user says "
            "'we discussed this before' / 'I remember chatting about this'. Supports Chinese and "
            "English keywords. Results may start with ★事实 entries — distilled decisions/"
            "preferences/constraints extracted from past sessions; trust those for 'what did we "
            "decide' questions and use paw_read on their session_id for the full context. "
            "mode='auto' (default) fuses exact keyword matches with semantic vector matches (RRF) "
            "and puts fact-layer hits first; mode='exact' is verbatim substring search only; "
            "mode='semantic' is meaning-based only (needs vectors indexed); mode='facts' queries "
            "only the distilled fact layer. Output is capped by a character budget to protect "
            "your context window."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword / phrase to look for"},
                "source": {
                    "type": "string",
                    "enum": ["claude", "codex", "opencode", "cursor", "qoder", "zcode"],
                    "description": "Optional: restrict to one tool's logs",
                },
                "project": {
                    "type": "string",
                    "description": "Project directory substring to scope results, e.g. current repo name. Prefer this over global search.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "exact", "semantic"],
                    "description": "auto (default) = hybrid exact+semantic RRF; exact = verbatim substring; semantic = meaning-based",
                },
                "limit": {"type": "number", "description": "Max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "paw_sessions",
        "description": (
            "List classified conversation sessions (one per chat), filterable by project "
            "directory or source tool, newest first, with topic preview. Use to browse "
            "what was discussed in a project instead of full-text search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["claude", "codex", "opencode", "cursor", "qoder", "zcode"],
                    "description": "Optional: restrict to one tool's logs",
                },
                "project": {"type": "string", "description": "Project directory substring"},
                "limit": {"type": "number", "description": "Max sessions (default 50)"},
            },
        },
    },
    {
        "name": "paw_facts",
        "description": (
            "Browse the distilled fact layer: decisions, preferences, constraints and stable "
            "facts extracted from past conversations (optional layer; empty until a chat model "
            "is configured). With 'query', runs keyword+semantic fact search; without it, "
            "lists the most recent facts for the project. Facts involved in an unconfirmed "
            "contradiction are marked with ⚠ and the model's proposed reason — when such a "
            "fact is relevant to the user's question, ASK THE USER which version is right, "
            "then resolve it with paw_facts_resolve (never decide silently). "
            "Superseded (outdated) facts are hidden unless include_superseded=true; "
            "pending_conflicts=true lists the contradiction queue instead of facts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional keyword/phrase to filter facts"},
                "project": {"type": "string", "description": "Project directory substring to scope results"},
                "include_superseded": {"type": "boolean", "description": "Show outdated facts superseded by newer ones (default false)"},
                "pending_conflicts": {"type": "boolean", "description": "List unconfirmed fact contradictions instead of facts (HITL queue)"},
                "limit": {"type": "number", "description": "Max results (default 30)"},
            },
        },
    },
    {
        "name": "paw_facts_resolve",
        "description": (
            "Resolve an unconfirmed fact contradiction after the USER decided which side is "
            "right. decision='new_wins': the newer fact replaces the older one; "
            "'old_wins': the newer fact is rejected; 'both': not actually a contradiction, "
            "keep both. Only call this AFTER asking the user — do not resolve on your own "
            "judgment. Get conflict ids from paw_facts(pending_conflicts=true) or the ⚠ "
            "marks in paw_search output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conflict_id": {"type": "number", "description": "Conflict id from paw_facts pending list"},
                "decision": {"type": "string", "enum": ["new_wins", "old_wins", "both"],
                             "description": "User's verdict"},
                "by": {"type": "string", "description": "Optional: who decided (shown in audit log)"},
            },
            "required": ["conflict_id", "decision"],
        },
    },
    {
        "name": "paw_read",
        "description": "Read one indexed conversation session back in order. Get session_id or file_path from search_history results first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID from search_history"},
                "file_path": {"type": "string", "description": "Transcript file path from search_history"},
                "limit": {"type": "number", "description": "Max messages (default 60)"},
            },
        },
    },
]


def read_session(session_id=None, file_path=None, limit=60):
    if not DB_PATH.exists():
        return "索引不存在，请先运行: python3 ~/.pawrecall/index.py"
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    sql = "SELECT source,project,session_id,role,ts,text FROM msgs WHERE "
    args = []
    if session_id:
        sql += "session_id=?"
        args.append(session_id)
    elif file_path:
        sql += "file_path=?"
        args.append(file_path)
    else:
        return "需要提供 session_id 或 file_path 之一。"
    sql += " ORDER BY id ASC LIMIT ?"
    args.append(int(limit))
    lines = []
    for src, proj, sid, role, ts, text in con.execute(sql, args):
        proj = (proj or "").replace(HOME.as_posix(), "~")
        text = text if len(text) <= 600 else text[:600] + "…"
        lines.append(f"[{ts}] ({src} · {role} · {proj}) {text}")
    con.close()
    if not lines:
        return "该 session 在索引中没有消息（可能尚未重新索引）。"
    return f"共 {len(lines)} 条消息：\n\n" + "\n\n".join(lines)


def handle_call(name, args):
    if name == "paw_search":
        results, note = search(
            args.get("query", ""), args.get("source"), args.get("project"),
            int(args.get("limit", 10)), args.get("mode", "auto"),
        )
        return f"[{note}]\n" + fmt_results(results, args.get("query", ""))
    if name == "paw_sessions":
        return fmt_sessions(
            list_sessions(args.get("source"), args.get("project"), int(args.get("limit", 50)))
        )
    if name == "paw_facts":
        if args.get("pending_conflicts"):
            return fmt_conflicts(
                list_conflicts(project=args.get("project"), state="pending",
                               limit=int(args.get("limit", 30))))
        q = args.get("query")
        if q:
            rows = facts_search(q, project=args.get("project"), limit=int(args.get("limit", 30)))
        else:
            rows = list_facts(args.get("project"), None,
                              bool(args.get("include_superseded", False)),
                              int(args.get("limit", 30)))
        return fmt_facts(rows)
    if name == "paw_facts_resolve":
        return resolve_conflict(
            int(args.get("conflict_id", 0)), args.get("decision", ""),
            by=args.get("by") or "agent")
    if name == "paw_read":
        return read_session(
            args.get("session_id"),
            args.get("file_path"),
            int(args.get("limit", 60)),
        )
    return f"未知工具: {name}"


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method", "")
        rid = req.get("id")
        if method.startswith("notifications/"):
            continue
        try:
            if method == "initialize":
                pver = (req.get("params") or {}).get("protocolVersion", "2025-06-18")
                result = {
                    "protocolVersion": pver,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = req.get("params") or {}
                try:
                    out = handle_call(params.get("name", ""), params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": out}]}
                except Exception as e:
                    result = {"content": [{"type": "text", "text": f"工具执行出错: {e}"}], "isError": True}
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "ping":
                result = {}
            else:
                if rid is None:
                    continue
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未知方法: {method}"}})
                continue
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "result": result})
        except Exception as e:
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
