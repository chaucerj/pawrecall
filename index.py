#!/usr/bin/env python3
"""Index all local AI agent conversation logs into one SQLite database.

Sources:
  - Claude Code:  ~/.claude/projects/*/*.jsonl
  - Codex:        ~/.codex/sessions/**/*.jsonl
  - OpenCode:     ~/.local/share/opencode/opencode.db
  - ZCode:        ~/.zcode/cli/rollout/*.jsonl

Usage:
  python3 index.py                 # incremental reindex (fast, skips unchanged files)
  python3 index.py search 关键词   # quick CLI search [--source claude|codex|opencode|zcode] [--limit N]
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HOME = Path.home()
AI_DIR = HOME / ".pawrecall"
# PAWRECALL_DB：指向副本库跑评测/基准，绝不碰生产库
DB_PATH = Path(os.environ.get("PAWRECALL_DB") or AI_DIR / "history.db")

CLAUDE_DIR = HOME / ".claude" / "projects"
CODEX_DIR = HOME / ".codex" / "sessions"
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"
CURSOR_GLOBAL = HOME / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
CURSOR_WS = HOME / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
QODER_DBS = (
    HOME / "Library" / "Application Support" / "Qoder" / "SharedClientCache" / "cache" / "db" / "local.db",
    HOME / "Library" / "Application Support" / "QoderCN" / "SharedClientCache" / "cache" / "db" / "local.db",
)
ZCODE_DIR = HOME / ".zcode" / "cli" / "rollout"

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SKIP_PREFIXES = ("<command", "<local-command", "Caveat:", "[Request interrupted", "<system-reminder>", "No response requested")


def ms_to_iso(ms):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000, datetime.timezone.utc).astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def ensure_db(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
        CREATE TABLE IF NOT EXISTS msgs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          project TEXT,
          session_id TEXT,
          file_path TEXT,
          role TEXT,
          ts TEXT,
          text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_msgs_file ON msgs(file_path);
        CREATE INDEX IF NOT EXISTS idx_msgs_session ON msgs(session_id);
        CREATE INDEX IF NOT EXISTS idx_msgs_source ON msgs(source);
        CREATE INDEX IF NOT EXISTS idx_msgs_project ON msgs(project);
        """
    )
    try:  # 语义层（可选，见 embed.py）；依赖缺失也不影响精确检索
        from embed import ensure_tables
        ensure_tables(con)
    except Exception:
        pass
    try:  # 事实层（可选，见 facts.py）；无 chat 模型时整层禁用
        from facts import ensure_tables as ensure_facts
        ensure_facts(con)
    except Exception:
        pass


def insert_rows(con, rows):
    con.executemany(
        "INSERT INTO msgs(source,project,session_id,file_path,role,ts,text) VALUES(?,?,?,?,?,?,?)",
        rows,
    )


# ---------------- Claude Code ----------------

def clean_text(t):
    if not t:
        return None
    t = re.sub(r"<system-reminder>.*?</system-reminder>", "", t, flags=re.S)
    t = t.strip()
    if not t or any(t.startswith(p) for p in SKIP_PREFIXES):
        return None
    if re.fullmatch(r"</?image>", t):  # codex 图片占位符，无文本价值且污染语义空间
        return None
    return t


def decode_claude_project(dirname):
    """'-Users-chaucer-foo' -> '/Users/chaucer/foo' (approximate, cwd is preferred)."""
    return "/" + dirname.lstrip("-").replace("-", "/")


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
                    rows.append(
                        (
                            "claude",
                            project or fallback_project,
                            obj.get("sessionId") or sid_default,
                            str(fp),
                            msg.get("role") or t,
                            obj.get("timestamp", "") or "",
                            txt,
                        )
                    )
    return rows


# ---------------- Codex ----------------

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
            ptype = payload.get("type")
            if obj.get("type") == "session_meta" and not project:
                project = payload.get("cwd")
                continue
            if obj.get("type") == "response_item" and ptype == "message":
                texts = [
                    c.get("text", "")
                    for c in (payload.get("content") or [])
                    if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")
                ]
                for raw in texts:
                    txt = clean_text(raw)
                    if txt:
                        rows.append(
                            ("codex", project, sid, str(fp), payload.get("role") or "", obj.get("timestamp", "") or "", txt)
                        )
    return rows


# ---------------- ZCode ----------------

ZCODE_CWD_RE = re.compile(r"Primary working directory:\s*(.+)")
ZCODE_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]|^/")


def zcode_local_ts(v):
    """ZCode stores UTC ISO timestamps ('2026-11-05T11:40:20.932Z');
    convert to local ISO seconds like the other parsers."""
    if not v:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone().isoformat(timespec="seconds")
    except Exception:
        return str(v)


def _zcode_last_user_text(messages):
    """Newest genuine user input in a request window. Tool results come back
    as user-role records too — skip them; walk backwards to the real prompt."""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if (isinstance(content, list) and content and isinstance(content[0], dict)
                and content[0].get("type") == "tool_result"):
            continue
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
        else:
            texts = []
        for raw in texts:
            txt = clean_text(raw)
            if txt:
                return txt
    return None


def _zcode_find_cwd(obj):
    """ZCode strips the environment section from the logged system prompt, so the
    project path only survives inside conversation content (environment-update
    injections). Deep-walk the record for the first path-shaped match."""
    stack = [obj]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            m = ZCODE_CWD_RE.search(v)
            if m:
                val = m.group(1).strip()
                if ZCODE_PATH_RE.match(val):
                    return val
        elif isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return None


def parse_zcode_file(fp):
    """ZCode Desktop rollout: one JSON record per model request/response —
    {sessionId, startedAt(UTC), request.messages[window], response.text}.
    The message window re-sends prior turns, so the newest real user message
    repeats across consecutive records; dedupe those repeats. Windows use a
    plain 'tool' role for tool results but user-role tool_result blocks also
    occur, hence the block check in _zcode_last_user_text."""
    rows = []
    m = UUID_RE.search(fp.stem)
    sid = m.group(0) if m else fp.stem
    project = None
    last_user = None
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
            if project is None:
                project = _zcode_find_cwd(obj)
            ts = zcode_local_ts(obj.get("startedAt"))
            req = obj.get("request") or {}
            if isinstance(req.get("messages"), list):
                user_txt = _zcode_last_user_text(req["messages"])
                if user_txt and user_txt != last_user:
                    last_user = user_txt
                    rows.append(("zcode", project, sid, str(fp), "user", ts, user_txt))
            asst_txt = clean_text((obj.get("response") or {}).get("text") or "")
            if asst_txt:
                rows.append(("zcode", project, sid, str(fp), "assistant", ts, asst_txt))
    return rows


# ---------------- OpenCode ----------------

def index_opencode(con):
    con.execute("DELETE FROM msgs WHERE source='opencode'")
    sconn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    q = """
        SELECT s.directory,
               m.session_id,
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

    def bubble_text(cid, bid, ctype):
        row = sconn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f"bubbleId:{cid}:{bid}",)).fetchone()
        if not row or not row[0]:
            return None
        try:
            b = json.loads(row[0])
        except Exception:
            return None
        t = b.get("text") or strip_html(b.get("richText") or "")
        return clean_text(t)

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
            txt = bubble_text(cid, h.get("bubbleId"), h.get("type"))
            if txt:
                rows.append(("cursor", project, cid, key, "user" if h.get("type") == 1 else "assistant", ts, txt))
    sconn.close()
    insert_rows(con, rows)
    con.execute("INSERT INTO files(path,mtime,size) VALUES(?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                (key, st.st_mtime, st.st_size))
    return len(rows)


# ---------------- reindex ----------------

def index_file_based(con, root, source, parser, recursive):
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
            print(f"  ! 解析失败 {fp}: {e}", file=sys.stderr)
            continue
        insert_rows(con, rows)
        con.execute(
            "INSERT INTO files(path,mtime,size) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
            (key, st.st_mtime, st.st_size),
        )
        n += len(rows)
    return n


# ---------------- Qoder ----------------

def qoder_ts(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return ms_to_iso(v if v > 1e11 else v * 1000)
    return str(v).replace(" ", "T")


def index_qoder(con):
    """Qoder: message contents are encrypted, but plaintext remains in
    chat_session.session_title (often full task prompts), agent_memory rows,
    and chat_message.summary (JSON conversation-continuation summaries).
    The db may be locked by a running IDE, so read a temp copy."""
    import shutil
    import tempfile
    n = 0
    for qdb in QODER_DBS:
        if not qdb.exists():
            continue
        key = str(qdb)
        st = qdb.stat()
        prev = con.execute("SELECT mtime, size FROM files WHERE path=?", (key,)).fetchone()
        if prev and abs(prev[0] - st.st_mtime) < 1 and prev[1] == st.st_size:
            continue
        con.execute("DELETE FROM msgs WHERE file_path=?", (key,))
        tmpdir = tempfile.mkdtemp(prefix="omnirecall-qoder-")
        rows = []
        try:
            tmp = os.path.join(tmpdir, "local.db")
            shutil.copy2(qdb, tmp)
            for ext in ("-wal", "-shm"):
                src = str(qdb) + ext
                if os.path.exists(src):
                    try:
                        shutil.copy2(src, tmp + ext)
                    except Exception:
                        pass
            try:
                s = sqlite3.connect(tmp)
            except Exception:
                continue
            try:
                for sid, title, puri, gmt in s.execute(
                        "SELECT session_id, session_title, project_uri, gmt_create FROM chat_session"):
                    txt = clean_text(title or "")
                    if not txt:
                        continue
                    project = (puri or "").replace("file://", "") or None
                    rows.append(("qoder", project, sid, key, "user", qoder_ts(gmt), f"[Qoder会话] {txt}"))
                for mid, title, content, gmt in s.execute(
                        "SELECT id, title, content, gmt_create FROM agent_memory"):
                    txt = clean_text(f"{title or ''}\n{content or ''}")
                    if not txt:
                        continue
                    rows.append(("qoder", None, f"agent-memory-{mid}", key, "assistant", qoder_ts(gmt), txt))
                for sid, summ in s.execute(
                        "SELECT session_id, summary FROM chat_message WHERE summary IS NOT NULL AND summary != ''"):
                    try:
                        arr = json.loads(summ)
                    except Exception:
                        continue
                    if not isinstance(arr, list):
                        continue
                    for item in arr:
                        if not isinstance(item, dict):
                            continue
                        role = item.get("role")
                        if role not in ("user", "assistant"):
                            continue
                        txt = clean_text(item.get("content") or "")
                        if txt:
                            rows.append(("qoder", None, sid, key, role, "", txt))
            finally:
                s.close()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        insert_rows(con, rows)
        con.execute("INSERT INTO files(path,mtime,size) VALUES(?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                    (key, st.st_mtime, st.st_size))
        n += len(rows)
    return n

def reindex():
    AI_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    ensure_db(con)
    n_claude = index_file_based(con, CLAUDE_DIR, "claude", parse_claude_file, recursive=False)
    n_codex = index_file_based(con, CODEX_DIR, "codex", parse_codex_file, recursive=True)
    n_zcode = index_file_based(con, ZCODE_DIR, "zcode", parse_zcode_file, recursive=True)
    n_open = index_opencode(con)
    n_cursor = index_cursor(con)
    n_qoder = index_qoder(con)
    con.commit()
    print("索引完成：")
    for src, n in (("claude", n_claude), ("codex", n_codex), ("zcode", n_zcode), ("opencode", n_open), ("cursor", n_cursor), ("qoder", n_qoder)):
        total = con.execute("SELECT COUNT(*) FROM msgs WHERE source=?", (src,)).fetchone()[0]
        print(f"  {src:9s} 本次新增 {n:6d} 条，库内共 {total:6d} 条")
    con.close()
    # 语义层：有 provider 就增量嵌入新消息（每次限量，避免 LaunchAgent 跑太久；
    # 剩余的下次自动续上。没有 provider 则静默跳过）
    try:
        import embed
        embed.backfill(limit=1000)
    except Exception as e:
        print(f"  (语义层跳过：{e})", file=sys.stderr)
    # 事实层：有 chat 模型就增量提取原子事实（每次限量几个会话，异步蒸馏）
    try:
        import facts
        facts.backfill()
    except Exception as e:
        print(f"  (事实层跳过：{e})", file=sys.stderr)


# ---------------- search ----------------

def esc_like(q):
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _conflict_note(project, source):
    """待确认矛盾的检索提示（HITL：提醒 agent 问用户再裁决）。"""
    try:
        from facts import pending_conflict_count
        n = pending_conflict_count(project=project, source=source)
        return f"；⚠ {n} 条待确认矛盾（paw_facts 查看，裁决用 paw_facts_resolve）" if n else ""
    except Exception:
        return ""


def search(query, source=None, project=None, limit=10, mode="auto"):
    """检索总入口：事实层（若有）前置，原文层（exact/semantic/auto）兜底。

    mode = facts 纯事实层；auto | exact | semantic 走原文层并在结果前插入事实命中。
    事实层未启用时行为与原版完全一致。
    """
    if mode == "facts":
        try:
            from facts import facts_search
            return (facts_search(query, source=source, project=project, limit=limit),
                    "事实层检索" + _conflict_note(project, source))
        except Exception as e:
            return [], f"事实层不可用（{e}）"
    results, note = _search_core(query, source, project, limit, mode)
    try:
        from facts import facts_search
        fr = facts_search(query, source=source, project=project,
                          limit=min(3, max(1, limit // 3)))
        if fr:
            results = fr + results[:max(0, limit - len(fr))]
            note = f"事实层 {len(fr)} 条 + {note}"
    except Exception:
        pass
    return results, note + _conflict_note(project, source)


def _search_core(query, source=None, project=None, limit=10, mode="auto"):
    """混合检索入口：mode = auto | exact | semantic。
    auto  = 精确 LIKE + 语义余弦两路召回，RRF 融合；语义不可用时自动回落精确。
    project: substring filter on the session's project directory — scoped search
    so agents don't scan every project's history by default."""
    if mode == "semantic":
        try:
            import embed
            results, note = embed.semantic_search(query, source, project, limit)
            if not results:
                return _search_exact(query, source, project, limit), note + "，回落到精确检索"
            return results, note
        except Exception as e:
            return _search_exact(query, source, project, limit), f"语义层异常({e})，回落到精确检索"
    exact = _search_exact(query, source, project, limit)
    if mode == "exact":
        return exact, "精确匹配"
    try:  # auto：exact 优先，语义按需（避免每个查询都付一次网络向量化）
        import embed
        POOL = 200
        exact_pool = _search_exact(query, source, project, POOL)
        if not exact_pool:
            agg = []
        else:
            agg = embed.group_sessions(exact_pool, limit)
        # 强信号：命中够多或最热会话聊得够深 → 本地结果已够好，直接返回（0 网络）
        best_hits = max((r.get("session_hits", 1) for r in agg), default=0)
        if exact_pool and (best_hits >= 2 or len(exact_pool) >= 5):
            return agg, "精确+会话聚合（本地快路径，未调用语义）"
        # 弱信号（0 命中/孤立命中）：语义才是主扈，融合补强
        sem_pool, _note = embed.semantic_search(query, source, project, POOL)
        if not sem_pool:
            return agg, "精确+会话聚合（语义不可用）"
        fused = embed.rrf_fuse(exact_pool, sem_pool, POOL * 2)
        grouped = embed.group_sessions(fused, limit)
        # 精确锚定：exact 命中的头部会话是不可否定的硬信号，若被语义聚合挤掉则强制前置
        have = {(r["source"], r["session_id"] or r["file_path"]) for r in grouped}
        for r in agg:
            k = (r["source"], r["session_id"] or r["file_path"])
            if k not in have:
                grouped.insert(0, r)
                have.add(k)
                if len(grouped) > limit:
                    grouped.pop()
        return grouped, "混合检索（精确+语义 RRF，按会话聚合）"
    except Exception:
        return exact, "精确匹配（语义层不可用）"


def _search_exact(query, source=None, project=None, limit=10):
    """Whitespace-separated terms are ANDed; ranked by timestamp (newest first).
    project: substring filter on the session's project directory — scoped search
    so agents don't scan every project's history by default."""
    if not DB_PATH.exists():
        return []
    terms = [t for t in (query or "").split() if t] or [str(query or "")]
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    clauses = " AND ".join("text LIKE ? ESCAPE '\\'" for _ in terms)
    sql = f"SELECT m.id, m.source, m.project, m.session_id, m.file_path, m.role, m.ts, m.text FROM msgs m WHERE {clauses}"
    args = [f"%{esc_like(t)}%" for t in terms]
    if source:
        sql += " AND m.source=?"
        args.append(source)
    if project:
        sql += " AND m.project LIKE ? ESCAPE '\\'"
        args.append(f"%{esc_like(project)}%")
    sql += " ORDER BY (m.ts = '') ASC, m.ts DESC LIMIT ?"
    args.append(min(max(limit * 25, 200), 2000))
    out = []
    for mid, src, proj, sid, fpath, role, ts, text in con.execute(sql, args):
        low = text.lower()
        idx = min((low.find(t.lower()) for t in terms if low.find(t.lower()) >= 0), default=-1)
        if idx < 0:
            continue
        start = max(0, idx - 120)
        end = min(len(text), idx + max(len(t) for t in terms) + 200)
        snippet = text[start:end].replace("\n", " ").strip()
        out.append(
            {
                "id": mid,
                "source": src,
                "project": proj or "",
                "session_id": sid or "",
                "file_path": fpath or "",
                "role": role or "",
                "ts": ts or "",
                "snippet": ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else ""),
            }
        )
        if len(out) >= limit:
            break
    con.close()
    return out


def list_sessions(source=None, project=None, limit=50):
    """Classified session catalog: one entry per conversation with project,
    message count, time range, and the first user message as its topic."""
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    sql = ("SELECT m.source, m.project, m.session_id, COUNT(*), MIN(m.ts), MAX(m.ts),"
           " (SELECT text FROM msgs m2 WHERE m2.session_id = m.session_id AND m2.role='user'"
           "  ORDER BY m2.id ASC LIMIT 1)"
           " FROM msgs m WHERE 1=1")
    args = []
    if source:
        sql += " AND m.source=?"
        args.append(source)
    if project:
        sql += " AND m.project LIKE ? ESCAPE '\\'"
        args.append(f"%{esc_like(project)}%")
    sql += " GROUP BY m.source, m.session_id ORDER BY MAX(m.ts) DESC LIMIT ?"
    args.append(int(limit))
    home = HOME.as_posix()
    out = []
    for src, proj, sid, cnt, tmin, tmax, topic in con.execute(sql, args):
        out.append({"source": src,
                    "project": (proj or "").replace(home, "~") if proj else "",
                    "session_id": sid or "",
                    "messages": cnt,
                    "first_ts": tmin or "",
                    "last_ts": tmax or "",
                    "topic": (topic or "")[:120].replace("\n", " ")})
    con.close()
    return out


def fmt_sessions(rows):
    if not rows:
        return "没有匹配的会话。"
    lines = [f"{len(rows)} 个会话："]
    for i, r in enumerate(rows, 1):
        lines.append(f"[{i}] {r['source']} · {r['messages']}条 · {r['last_ts']} · {r['project'] or '(未知项目)'}")
        lines.append(f"    主题: {r['topic'] or '(无用户消息)'}")
        lines.append(f"    session: {r['session_id']}")
    return "\n".join(lines)


# 注入预算（字符）：防止检索结果撑爆 agent 的上下文窗口。env 可覆盖。
MAX_OUTPUT_CHARS = int(os.environ.get("PAWRECALL_MAX_CHARS") or 6000)


def fmt_results(results, query):
    if not results:
        return f"没有找到包含「{query}」的历史记录。试试其他关键词，或先运行: python3 ~/.pawrecall/index.py 重新索引。"
    lines = [f"找到 {len(results)} 条与「{query}」相关的历史记录："]
    home = HOME.as_posix()
    used, shown = 0, 0
    for i, r in enumerate(results, 1):
        proj = (r.get("project") or "").replace(home, "~")
        if r.get("fact"):
            block = [f"\n[{i}] ★事实[{r.get('kind', 'fact')}] {r.get('subject', '')} · {r.get('ts', '')} · {proj}",
                     f"    {r.get('snippet', '')}",
                     f"    session: {r.get('session_id', '')}"]
            for p in (r.get("pending_conflicts") or [])[:1]:
                block.append(f"    ⚠ 与事实 #{p['other_fact_id']} 待确认矛盾：{p.get('reason') or '（模型未给理由）'}"
                             f"——向用户确认后用 paw_facts_resolve 裁决")
        else:
            block = [f"\n[{i}] {r['source']} · {r['role']} · {r['ts']} · {proj}",
                     f"    {r['snippet']}",
                     f"    session: {r['session_id']}  file: {r['file_path']}"]
        lines.extend(block)
        used += sum(len(x) for x in block)
        shown += 1
        if used > MAX_OUTPUT_CHARS and shown < len(results):
            lines.append(f"\n（已达注入预算 {MAX_OUTPUT_CHARS} 字符，剩余 {len(results) - shown} 条省略——"
                         f"用更具体关键词或调小 --limit 收窄）")
            break
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Index/search all local AI agent conversation logs")
    ap.add_argument("--db", default=str(DB_PATH))
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--source", choices=["claude", "codex", "opencode", "cursor", "qoder", "zcode"])
    sp.add_argument("--project", help="限定项目目录子串，如 'pet' 或完整路径")
    spp = sub.add_parser("sessions", help="按项目/源浏览会话分类目录")
    spp.add_argument("--source", choices=["claude", "codex", "opencode", "cursor", "qoder", "zcode"])
    spp.add_argument("--project")
    spp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--mode", choices=["auto", "exact", "semantic", "facts"], default="auto",
                    help="auto=混合（默认）；exact=仅精确；semantic=仅语义；facts=仅事实层")
    ep = sub.add_parser("embed", help="增量嵌入消息向量（语义层）")
    ep.add_argument("--limit", type=int, default=None, help="本次最多嵌入条数")
    ep.add_argument("--status", action="store_true", help="查看嵌入状态")
    ep.add_argument("--reset", action="store_true", help="清空向量（换模型后重建用）")
    fp = sub.add_parser("facts", help="事实层：run 提取 / status 状态 / conflicts 冲突 / resolve 裁决 / reset 清空")
    fp.add_argument("facts_cmd", nargs="?", choices=["run", "status", "reset", "conflicts", "resolve"], default="status")
    fp.add_argument("--sessions", type=int, default=None, help="本次最多提取的会话数")
    fp.add_argument("--state", default="pending", help="conflicts：按状态过滤（pending/confirmed/rejected/stale/all）")
    fp.add_argument("--conflict", type=int, help="resolve：冲突 id")
    fp.add_argument("--decision", choices=["new_wins", "old_wins", "both"], help="resolve：裁决结果")
    fp.add_argument("--by", default="cli", help="resolve：裁决人标识")
    args = ap.parse_args()
    if args.cmd == "search":
        results, note = search(args.query, args.source, getattr(args, "project", None), args.limit,
                               getattr(args, "mode", "auto"))
        print(f"[{note}]")
        print(fmt_results(results, args.query))
    elif args.cmd == "embed":
        import embed as em
        if getattr(args, "status", False):
            cfg, done, pending, model = em.vec_status()
            print(f"provider: {(cfg['provider'] + '/' + cfg['model']) if cfg else '未启用'}")
            print(f"模型记录: {model or '(无)'}  已嵌入: {done}  待嵌入: {pending}")
        elif getattr(args, "reset", False):
            con = sqlite3.connect(DB_PATH)
            em.ensure_tables(con)
            con.execute("DELETE FROM vecs")
            con.execute("DELETE FROM embed_meta")
            con.commit()
            con.close()
            print("已清空全部向量。")
        else:
            em.backfill(limit=args.limit)
    elif args.cmd == "sessions":
        print(fmt_sessions(list_sessions(getattr(args, "source", None), getattr(args, "project", None), getattr(args, "limit", 50))))
    elif args.cmd == "facts":
        import facts
        if args.facts_cmd == "status":
            cfg, total, superseded, pending, vecs, conflicts = facts.status()
            print(f"chat provider: {(cfg['provider'] + '/' + cfg['model']) if cfg else '未配置（事实层禁用，见 ~/.pawrecall/facts.json.example）'}"
                  f"  冲突策略: {facts.conflict_mode(cfg)}")
            print(f"事实总数: {total}（被推翻 {superseded}）  已向量化: {vecs}  "
                  f"待提取会话: {pending}  待裁决矛盾: {conflicts}")
        elif args.facts_cmd == "reset":
            facts.reset()
            print("已清空全部事实。")
        elif args.facts_cmd == "conflicts":
            state = None if args.state == "all" else args.state
            rows = []
            for st in (["pending", "confirmed", "rejected", "stale"] if state is None else [state]):
                rows.extend(facts.list_conflicts(project=getattr(args, "project", None), state=st))
            print(facts.fmt_conflicts(rows))
        elif args.facts_cmd == "resolve":
            if not args.conflict or not args.decision:
                sys.exit("resolve 需要 --conflict ID --decision new_wins|old_wins|both")
            print(facts.resolve_conflict(args.conflict, args.decision, by=args.by))
        else:
            n = facts.backfill(limit_sessions=args.sessions)
            print(f"本次提取 {n} 条事实。" if n >= 0 else "事实层未启用。")
    else:
        reindex()


if __name__ == "__main__":
    main()
