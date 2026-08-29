#!/usr/bin/env python3
"""omnirecall — zero-dependency cross-agent conversation history search.

One file. Python stdlib only. Indexes the local transcripts of multiple AI
coding agents (Claude Code, Codex CLI, OpenCode, Cursor) into a single SQLite database,
then exposes them to ANY agent via MCP, CLI, or skill — regardless of which
directory each agent was started in.

Usage:
  python3 omnirecall.py index                    incremental reindex
  python3 omnirecall.py search "keyword"         CLI search (--source, --limit)
  python3 omnirecall.py serve                    MCP stdio server
  python3 omnirecall.py hook                     Claude Code UserPromptSubmit hook

Database: ~/.omnirecall/history.db  (override with --db or $OMNIRECALL_DB)

MIT License — see LICENSE.
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path.home()
PROG = "omnirecall"

CLAUDE_DIR = HOME / ".claude" / "projects"
CODEX_DIR = HOME / ".codex" / "sessions"
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"
CURSOR_GLOBAL = HOME / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
CURSOR_WS = HOME / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SKIP_PREFIXES = ("<command", "<local-command", "Caveat:", "[Request interrupted", "<system-reminder>")
HOOK_KEYWORDS = ("之前", "上次", "上回", "记得", "聊过", "讨论过", "历史对话", "前几天", "以前",
                 "we discussed", "i remember", "last time", "previously")


def default_db():
    env = os.environ.get("OMNIRECALL_DB")
    if env:
        return Path(env)
    return HOME / ".omnirecall" / "history.db"


def ms_to_iso(ms):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000, datetime.timezone.utc).astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def clean_text(t):
    if not t:
        return None
    t = re.sub(r"<system-reminder>.*?</system-reminder>", "", t, flags=re.S)
    t = t.strip()
    if not t or any(t.startswith(p) for p in SKIP_PREFIXES):
        return None
    return t


def decode_claude_project(dirname):
    """'-Users-chaucer-foo' -> '/Users/chaucer/foo' (approximate; cwd is preferred)."""
    return "/" + dirname.lstrip("-").replace("-", "/")


# ---------------- parsers ----------------

def parse_claude_file(fp):
    rows = []
    fallback_project = decode_claude_project(fp.parent.name)
    project = None
    sid_default = fp.stem
    with open(fp, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if project is None and obj.get("cwd"):
                project = obj["cwd"]
            t = obj.get("type")
            if t not in ("user", "assistant") or obj.get("isMeta"):
                continue
            msg = obj.get("message") or {}
            content = msg.get("content")
            texts = []
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            for raw in texts:
                txt = clean_text(raw)
                if txt:
                    rows.append(("claude", project or fallback_project,
                                 obj.get("sessionId") or sid_default, str(fp),
                                 msg.get("role") or t, obj.get("timestamp", "") or "", txt))
    return rows


def parse_codex_file(fp):
    rows = []
    m = UUID_RE.search(fp.stem)
    sid = m.group(0) if m else fp.stem
    project = None
    with open(fp, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload") or {}
            if obj.get("type") == "session_meta" and not project:
                project = payload.get("cwd")
                continue
            if obj.get("type") == "response_item" and payload.get("type") == "message":
                texts = [c.get("text", "") for c in (payload.get("content") or [])
                         if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")]
                for raw in texts:
                    txt = clean_text(raw)
                    if txt:
                        rows.append(("codex", project, sid, str(fp),
                                     payload.get("role") or "", obj.get("timestamp", "") or "", txt))
    return rows


# ---------------- database ----------------

def ensure_db(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
        CREATE TABLE IF NOT EXISTS msgs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL, project TEXT, session_id TEXT,
          file_path TEXT, role TEXT, ts TEXT, text TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_msgs_file ON msgs(file_path);
        CREATE INDEX IF NOT EXISTS idx_msgs_session ON msgs(session_id);
        CREATE INDEX IF NOT EXISTS idx_msgs_source ON msgs(source);
    """)


def insert_rows(con, rows):
    con.executemany("INSERT INTO msgs(source,project,session_id,file_path,role,ts,text) VALUES(?,?,?,?,?,?,?)", rows)


def index_file_based(con, root, parser, recursive):
    if not root.exists():
        return 0
    it = root.rglob("*.jsonl") if recursive else root.glob("*/*.jsonl")
    n = 0
    for fp in it:
        try:
            st = fp.stat()
        except OSError:
            continue
        key = str(fp)
        prev = con.execute("SELECT mtime, size FROM files WHERE path=?", (key,)).fetchone()
        if prev and abs(prev[0] - st.st_mtime) < 1 and prev[1] == st.st_size:
            continue
        con.execute("DELETE FROM msgs WHERE file_path=?", (key,))
        try:
            rows = parser(fp)
        except Exception as e:
            print(f"  ! parse failed {fp}: {e}", file=sys.stderr)
            continue
        insert_rows(con, rows)
        con.execute("INSERT INTO files(path,mtime,size) VALUES(?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                    (key, st.st_mtime, st.st_size))
        n += len(rows)
    return n


def index_opencode(con):
    if not OPENCODE_DB.exists():
        return 0
    con.execute("DELETE FROM msgs WHERE source='opencode'")
    sconn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    q = """
        SELECT s.directory, m.session_id,
               json_extract(m.data, '$.role'),
               json_extract(p.data, '$.text'),
               p.time_created
        FROM message m
        JOIN part p ON p.message_id = m.id
        JOIN session s ON s.id = m.session_id
        WHERE json_extract(p.data, '$.type') = 'text'
          AND json_extract(p.data, '$.text') IS NOT NULL
    """
    rows = []
    for directory, sid, role, text, tc in sconn.execute(q):
        txt = clean_text(text)
        if txt:
            rows.append(("opencode", directory, sid, str(OPENCODE_DB), role or "", ms_to_iso(tc), txt))
    sconn.close()
    insert_rows(con, rows)
    return len(rows)


# ---------------- Cursor ----------------

def strip_html(s):
    return re.sub(r"<[^>]+>", " ", s or "")


def cursor_project_map():
    """composerId -> project path, via each workspace's composer.composerData."""
    proj_by_composer = {}
    if not CURSOR_WS.exists():
        return proj_by_composer
    for d in CURSOR_WS.iterdir():
        wj, st = d / "workspace.json", d / "state.vscdb"
        if not wj.exists() or not st.exists():
            continue
        try:
            proj = (json.load(open(wj)).get("folder") or "")[8:] or None
        except Exception:
            proj = None
        try:
            s = sqlite3.connect(f"file:{st}?mode=ro", uri=True)
            row = s.execute("SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
            s.close()
        except Exception:
            continue
        if not row or not row[0]:
            continue
        try:
            doc = json.loads(row[0])
        except Exception:
            continue
        for c in doc.get("allComposers") or []:
            cid = c.get("composerId")
            if cid:
                proj_by_composer[cid] = proj
    return proj_by_composer


def index_cursor(con):
    """Cursor stores message content globally: composerData:<id> docs hold ordered
    refs (fullConversationHeadersOnly / conversationMap); text lives in separate
    bubbleId:<composerId>:<bubbleId> rows."""
    if not CURSOR_GLOBAL.exists():
        return 0
    key = str(CURSOR_GLOBAL)
    st = CURSOR_GLOBAL.stat()
    prev = con.execute("SELECT mtime, size FROM files WHERE path=?", (key,)).fetchone()
    if prev and abs(prev[0] - st.st_mtime) < 1 and prev[1] == st.st_size:
        return 0
    con.execute("DELETE FROM msgs WHERE file_path=?", (key,))
    proj_map = cursor_project_map()
    sconn = sqlite3.connect(f"file:{CURSOR_GLOBAL}?mode=ro", uri=True)

    def bubble_text(cid, bid):
        row = sconn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f"bubbleId:{cid}:{bid}",)).fetchone()
        if not row or not row[0]:
            return None
        try:
            b = json.loads(row[0])
        except Exception:
            return None
        return clean_text(b.get("text") or strip_html(b.get("richText") or ""))

    rows = []
    for k, v in sconn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
        if not v:
            continue
        try:
            doc = json.loads(v)
        except Exception:
            continue
        cid = doc.get("composerId") or k.split(":", 1)[1]
        ts = ms_to_iso(doc.get("lastUpdatedAt") or doc.get("createdAt"))
        project = proj_map.get(cid)
        cm = doc.get("conversationMap") or {}
        if cm:
            for b in cm.values():
                if b.get("type") not in (1, 2):
                    continue
                txt = clean_text(b.get("text") or strip_html(b.get("richText") or ""))
                if txt:
                    rows.append(("cursor", project, cid, key, "user" if b.get("type") == 1 else "assistant", ts, txt))
        for h in doc.get("fullConversationHeadersOnly") or []:
            if h.get("type") not in (1, 2):
                continue
            txt = bubble_text(cid, h.get("bubbleId"))
            if txt:
                rows.append(("cursor", project, cid, key, "user" if h.get("type") == 1 else "assistant", ts, txt))
    sconn.close()
    insert_rows(con, rows)
    con.execute("INSERT INTO files(path,mtime,size) VALUES(?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                (key, st.st_mtime, st.st_size))
    return len(rows)


def reindex(db):
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    ensure_db(con)
    counts = {
        "claude": index_file_based(con, CLAUDE_DIR, parse_claude_file, recursive=False),
        "codex": index_file_based(con, CODEX_DIR, parse_codex_file, recursive=True),
        "opencode": index_opencode(con),
        "cursor": index_cursor(con),
    }
    con.commit()
    print("index complete:")
    for src, n in counts.items():
        total = con.execute("SELECT COUNT(*) FROM msgs WHERE source=?", (src,)).fetchone()[0]
        print(f"  {src:9s} +{n:6d} new this run   {total:6d} total")
    con.close()


# ---------------- search ----------------

def esc_like(q):
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(db, query, source=None, limit=10):
    """Whitespace-separated terms are ANDed: "RAGAS 课程基线" matches texts
    containing both parts regardless of their spacing in the original.
    Ranked by timestamp (newest first), NOT insertion order."""
    if not db.exists():
        return []
    terms = [t for t in (query or "").split() if t] or [str(query or "")]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    clauses = " AND ".join("text LIKE ? ESCAPE '\\'" for _ in terms)
    sql = f"SELECT source,project,session_id,file_path,role,ts,text FROM msgs WHERE {clauses}"
    args = [f"%{esc_like(t)}%" for t in terms]
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY (ts = '') ASC, ts DESC LIMIT ?"
    args.append(min(max(limit * 25, 200), 2000))
    out = []
    for src, proj, sid, fpath, role, ts, text in con.execute(sql, args):
        low = text.lower()
        idx = min((low.find(t.lower()) for t in terms if low.find(t.lower()) >= 0), default=-1)
        if idx < 0:
            continue
        start = max(0, idx - 120)
        end = min(len(text), idx + max(len(t) for t in terms) + 200)
        snippet = text[start:end].replace("\n", " ").strip()
        out.append({"source": src, "project": proj or "", "session_id": sid or "",
                    "file_path": fpath or "", "role": role or "", "ts": ts or "",
                    "snippet": ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")})
        if len(out) >= limit:
            break
    con.close()
    return out


def fmt_results(results, query):
    home = HOME.as_posix()
    if not results:
        return (f'No history matches "{query}". Try synonyms / more specific words, '
                f"or reindex first: python3 omnirecall.py index")
    lines = [f'{len(results)} result(s) for "{query}":']
    for i, r in enumerate(results, 1):
        proj = r["project"].replace(home, "~")
        lines.append(f"\n[{i}] {r['source']} · {r['role']} · {r['ts']} · {proj}")
        lines.append(f"    {r['snippet']}")
        lines.append(f"    session: {r['session_id']}  file: {r['file_path']}")
    return "\n".join(lines)


def read_session(db, session_id=None, file_path=None, limit=60):
    if not db.exists():
        return "Index not found. Run: python3 omnirecall.py index"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    if session_id:
        sql, args = "SELECT source,project,session_id,role,ts,text FROM msgs WHERE session_id=?", [session_id]
    elif file_path:
        sql, args = "SELECT source,project,session_id,role,ts,text FROM msgs WHERE file_path=?", [file_path]
    else:
        return "Provide session_id or file_path."
    sql += " ORDER BY id ASC LIMIT ?"
    args.append(int(limit))
    lines = []
    for src, proj, sid, role, ts, text in con.execute(sql, args):
        proj = (proj or "").replace(HOME.as_posix(), "~")
        text = text if len(text) <= 600 else text[:600] + "…"
        lines.append(f"[{ts}] ({src} · {role} · {proj}) {text}")
    con.close()
    if not lines:
        return "No messages for that session in the index (reindex may be needed)."
    return f"{len(lines)} message(s):\n\n" + "\n\n".join(lines)


# ---------------- MCP server (stdio) ----------------

TOOLS = [
    {
        "name": "search_history",
        "description": (
            "Search ALL past AI coding-agent conversations on this machine "
            "(Claude Code, Codex, OpenCode, Cursor), regardless of which folder each agent "
            "was started in. Use at task start or whenever the user says "
            "'we discussed this before' / 'I remember talking about X' to recover "
            "prior decisions and context. Works for Chinese and English keywords."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword / phrase to look for"},
                "source": {"type": "string", "enum": ["claude", "codex", "opencode", "cursor"],
                           "description": "Optional: restrict to one tool's logs"},
                "limit": {"type": "number", "description": "Max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_session",
        "description": "Read one indexed conversation session back in order. Get session_id or file_path from search_history results first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "file_path": {"type": "string"},
                "limit": {"type": "number", "description": "Max messages (default 60)"},
            },
        },
    },
]


def handle_call(db, name, args):
    if name == "search_history":
        return fmt_results(search(db, args.get("query", ""), args.get("source"),
                                  int(args.get("limit", 10))), args.get("query", ""))
    if name == "read_session":
        return read_session(db, args.get("session_id"), args.get("file_path"),
                            int(args.get("limit", 60)))
    return f"Unknown tool: {name}"


def serve(db):
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
                result = {"protocolVersion": pver, "capabilities": {"tools": {}},
                          "serverInfo": {"name": PROG, "version": "1.0.0"}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = req.get("params") or {}
                try:
                    out = handle_call(db, params.get("name", ""), params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": out}]}
                except Exception as e:
                    result = {"content": [{"type": "text", "text": f"tool error: {e}"}], "isError": True}
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "ping":
                result = {}
            else:
                if rid is None:
                    continue
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method: {method}"}})
                continue
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "result": result})
        except Exception as e:
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------------- Claude Code prompt hook ----------------

def hook():
    """UserPromptSubmit hook: when the prompt refers to past conversations,
    inject a deterministic 'search first' reminder into context."""
    try:
        data = json.load(sys.stdin)
        prompt = str(data.get("prompt", ""))
    except Exception:
        return
    low = prompt.lower()
    if any(k in prompt for k in HOOK_KEYWORDS) or any(k in low for k in HOOK_KEYWORDS):
        print("[omnirecall] This message refers to past conversations. You MUST call "
              "search_history (omnirecall server) to检索 local history before answering; "
              "use read_session for full context. Never invent prior conclusions.")
    return


# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(prog=PROG, description="Zero-dependency cross-agent AI conversation history search")
    ap.add_argument("--db", type=Path, default=default_db(), help="SQLite db path (default ~/.omnirecall/history.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="incremental reindex of all agent transcripts")
    sp = sub.add_parser("search", help="search across all history")
    sp.add_argument("query")
    sp.add_argument("--source", choices=["claude", "codex", "opencode", "cursor"])
    sp.add_argument("--limit", type=int, default=10)
    sub.add_parser("serve", help="run MCP stdio server")
    sub.add_parser("hook", help="Claude Code UserPromptSubmit hook (reads JSON on stdin)")
    args = ap.parse_args()

    if args.cmd == "index":
        reindex(args.db)
        try:
            os.chmod(args.db, 0o600)
        except OSError:
            pass
    elif args.cmd == "search":
        print(fmt_results(search(args.db, args.query, args.source, args.limit), args.query))
    elif args.cmd == "serve":
        serve(args.db)
    elif args.cmd == "hook":
        hook()


if __name__ == "__main__":
    main()
