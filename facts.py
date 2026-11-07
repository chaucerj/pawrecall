#!/usr/bin/env python3
"""Optional fact layer for pawrecall — 蒸馏式原子事实记忆.

在「原始对话检索」之上加一层轻量蒸馏：会话索引后异步提取决定/偏好/约束级
原子事实（Hy-Memory / mem0 的 L1 思路），检索时事实层前置、原文层兜底。

零依赖哲学（与 embed.py 一致）：
  - 没有可用的 chat 模型 -> 整层自动禁用，行为与纯检索完全一致
  - 表结构全部 CREATE TABLE IF NOT EXISTS，向后兼容
  - 提取异步且限量：挂在 index.py reindex() 之后，每次最多 N 个会话，
    不拖慢 LaunchAgent；原始消息永不被修改或删除

chat 来源探测顺序：
  1. 显式配置 ~/.pawrecall/facts.json 或 PAWRECALL_FACTS_* 环境变量
     {"url": "http://localhost:11434", "model": "qwen3:4b", "key": "", "sessions_per_run": 3}
     url 为 OpenAI 兼容基础地址（Ollama 填 http://localhost:11434 即可）
  2. Ollama 本地自动探测：/api/tags 中存在已知 instruct 模型（排除纯 embedding 模型）
  都没有 -> provider 为 None，事实层禁用。

冲突语义（HITL 默认）：模型判定新旧事实矛盾时只有提议权——挂起进 fact_conflicts
待人工裁决，裁决前新旧事实都保留可见并在检索结果中带 ⚠ 标注；
paw_facts_resolve / facts resolve 裁决（new_wins / old_wins / both），审计留痕。
conflict_mode=auto 回到直接信任模型的旧行为；auto_resolve_days 兜底超时冲突。
原始消息永不被修改或删除。
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import embed  # noqa: E402  复用其 HTTP/向量化/配置探测

HOME = Path.home()
AI_DIR = HOME / ".pawrecall"
CONFIG_PATH = AI_DIR / "facts.json"

MAX_SESSION_CHARS = 9000   # 单次提取喂给模型的对话窗口上限（字符，取尾部）
MAX_MSG_CHARS = 500        # 单条消息截断
MAX_EXISTING_FACTS = 20    # 喂给模型做冲突对比的旧事实条数
FACTS_PER_CALL = 5         # 每次调用最多提取条数
VALID_KINDS = ("decision", "preference", "constraint", "fact")

_CHAT_HINTS = ("qwen", "llama", "mistral", "gemma", "phi", "glm", "deepseek",
               "minimax", "internlm", "yi-", "granite")
_EMBED_EXCLUDE = ("embed", "bge", "nomic", "mxbai", "arctic", "minilm", "e5-")


def log(msg):
    print(msg, file=sys.stderr)


# ---------------- provider ----------------

def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"! facts.json 解析失败，忽略：{e}")
            cfg = {}
    env_map = {"PAWRECALL_FACTS_URL": "url", "PAWRECALL_FACTS_KEY": "key", "PAWRECALL_FACTS_MODEL": "model"}
    for k, v in env_map.items():
        if os.environ.get(k):
            cfg[v] = os.environ[k]
    if os.environ.get("PAWRECALL_FACTS_DISABLE"):
        return None
    if not cfg.get("url") and not cfg.get("model"):
        return None
    provider = (cfg.get("provider") or "").strip().lower()
    url = (cfg.get("url") or "").strip().rstrip("/")
    if not provider:
        provider = "ollama" if "localhost:11434" in url else "openai"
    cfg["provider"] = provider
    cfg["url"] = url
    cfg.setdefault("model", "")
    cfg.setdefault("key", "")
    cfg.setdefault("sessions_per_run", 3)
    # 矛盾处理策略：hitl（默认）= 挂起待人工裁决；auto = 直接信任模型推翻（旧行为）
    if os.environ.get("PAWRECALL_FACTS_CONFLICT_MODE"):
        cfg["conflict_mode"] = os.environ["PAWRECALL_FACTS_CONFLICT_MODE"]
    cfg.setdefault("conflict_mode", "hitl")
    cfg.setdefault("auto_resolve_days", 0)  # >0 时超过 N 天未裁决的冲突按 new_wins 自动收敛
    return cfg


def _detect_ollama_chat():
    """Ollama tags 里找已知 instruct 模型（排除纯 embedding 模型），体积小者优先。"""
    try:
        import urllib.request
        with urllib.request.urlopen(embed.OLLAMA_TAGS_URL, timeout=1) as r:
            models = json.loads(r.read()).get("models", [])
    except Exception:
        return None
    cands = []
    for m in models:
        name = (m.get("name") or "").lower()
        if not name or any(x in name for x in _EMBED_EXCLUDE):
            continue
        if any(h in name for h in _CHAT_HINTS):
            cands.append((m.get("size") or 1 << 40, m.get("name")))
    return {"provider": "ollama", "url": "http://localhost:11434", "model": cands[0][1],
            "key": "", "sessions_per_run": 3} if cands else None


_PROVIDER = None


def provider_info():
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = load_config() or _detect_ollama_chat()
    return _PROVIDER


def conflict_mode(cfg=None):
    """hitl（默认）：模型对矛盾只有提议权，挂起待人工裁决；auto：直接信任模型推翻（旧行为）。"""
    return ((cfg or provider_info() or {}).get("conflict_mode") or "hitl").lower()


def _chat_url(cfg):
    return cfg["url"] + ("/v1/chat/completions" if cfg["provider"] == "ollama" else "/chat/completions")


def chat(messages, cfg=None, patient=False):
    cfg = cfg or provider_info()
    if not cfg:
        raise RuntimeError("chat 来源未配置")
    resp = embed._post_json(_chat_url(cfg),
                            {"model": cfg["model"], "messages": messages,
                             "temperature": 0.1, "stream": False}, cfg["key"])
    return (resp["choices"][0]["message"]["content"] or "").strip()


# ---------------- storage ----------------

def ensure_tables(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS facts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT, project TEXT, session_id TEXT,
          ts TEXT,
          kind TEXT NOT NULL DEFAULT 'fact',
          subject TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL,
          superseded_by INTEGER REFERENCES facts(id),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_facts_project ON facts(project);
        CREATE INDEX IF NOT EXISTS idx_facts_session ON facts(session_id);
        CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);
        CREATE TABLE IF NOT EXISTS facts_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS facts_cursor(
          session_id TEXT PRIMARY KEY, last_msg_id INTEGER NOT NULL, extracted_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fact_vecs(
          fact_id INTEGER PRIMARY KEY REFERENCES facts(id) ON DELETE CASCADE,
          vec BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fact_conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_id INTEGER NOT NULL REFERENCES facts(id),
          new_id INTEGER NOT NULL REFERENCES facts(id),
          state TEXT NOT NULL DEFAULT 'pending',   -- pending|confirmed|rejected|stale
          reason TEXT NOT NULL DEFAULT '',         -- 模型给出的矛盾理由，供人裁决参考
          created_at TEXT NOT NULL,
          resolved_at TEXT,
          resolved_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_conflicts_state ON fact_conflicts(state);
        """
    )


def _tables_ready(con):
    """只读连接上探测事实表是否已建（存在则无需 ensure_tables，避免 ro 写库报错）。"""
    n = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('facts','fact_vecs')").fetchone()[0]
    return n >= 2


def _meta_get(con, key):
    row = con.execute("SELECT value FROM facts_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(con, key, value):
    con.execute("INSERT INTO facts_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ---------------- extraction ----------------

_EXTRACTION_PROMPT = """你是记忆提取器。从下面的对话片段中提取 0-%d 条「原子事实」——决定、偏好、约束或稳定事实。

规则：
- 只提取对话中明确表达的内容，不要推测、不要总结对话流程
- 优先提取：技术决定及其理由、用户明确表达的偏好、任务约束、"我们决定/改成/定了/以后都"类结论
- kind 取值: "decision"(决定/结论) | "preference"(偏好/习惯) | "constraint"(约束/限制) | "fact"(其他稳定事实)
- subject: 事实主体，不超过16字，用于跨会话对比（如项目名、功能名、组件名）
- content: 一句话陈述，不超过100字，保留原话关键措辞和数字
- supersedes: 本条推翻或更新的旧事实 id 数组（只能从「已有事实」列表里选；没有填 []）。拿不准是否矛盾就填 []——宁可漏报不要误报
- reason: 当 supersedes 非空时必填，一句话说明新旧事实为何矛盾（≤30字），供人工裁决参考
- 语言跟随对话原文

只输出 JSON 数组（可能为空数组），不要输出任何其他文字。"""


def _parse_json_array(text):
    """从模型输出里鲁棒地抠出 JSON 数组。"""
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    l, r = text.find("["), text.rfind("]")
    if l < 0 or r <= l:
        m = re.search(r'"facts"\s*:\s*(\[.*?\])', text, re.S)
        if not m:
            return None
        l, r = m.start(1), m.end(1)
    try:
        arr = json.loads(text[l:r + 1])
        return arr if isinstance(arr, list) else None
    except Exception:
        return None


def _pending_sessions(con, limit):
    """新消息数 >0 的会话，最新的优先（近期决定最有价值；旧会话随每次运行逐步补上）。"""
    return con.execute(
        """
        SELECT m.source, m.project, m.session_id, MAX(m.id) AS max_id
        FROM msgs m
        LEFT JOIN facts_cursor c ON c.session_id = m.session_id
        WHERE c.session_id IS NULL OR m.id > c.last_msg_id
        GROUP BY m.session_id
        ORDER BY max_id DESC
        LIMIT ?
        """, (int(limit),)).fetchall()


def _window(con, source, sid, after_id):
    """会话内新消息按时间正序拼窗口；超长从尾部保留（近期内容权重高）。"""
    rows = con.execute(
        "SELECT id, role, ts, text FROM msgs WHERE session_id=? AND source=? AND id>? ORDER BY id ASC",
        (sid, source, after_id)).fetchall()
    lines, kept = [], []
    used = 0
    for mid, role, ts, text in reversed(rows):  # 从最新往回收
        block = f"[{role or 'msg'}] {(text or '')[:MAX_MSG_CHARS]}"
        if used + len(block) > MAX_SESSION_CHARS and kept:
            break
        lines.append(block)
        kept.append(mid)
        used += len(block)
    lines.reverse()
    return sorted(zip(kept, lines), key=lambda x: x[0])  # (msg_id, line) 按时间正序


def extract_session(con, source, project, sid, after_id, cfg=None):
    """对单个会话的新消息做一次提取。返回 (新增事实数, 已处理到的 msg_id)。"""
    cfg = cfg or provider_info()
    ordered = _window(con, source, sid, after_id)
    if not ordered:
        return 0, after_id
    max_id = ordered[-1][0]
    dialogue = "\n".join(ln for _, ln in ordered)

    existing = con.execute(
        "SELECT id, subject, content FROM facts WHERE superseded_by IS NULL "
        "AND (project IS ? OR ? IS NULL) ORDER BY id DESC LIMIT ?",
        (project, project, MAX_EXISTING_FACTS)).fetchall()
    existing_block = "\n".join(f'- id={i} [{s}] {c[:80]}' for i, s, c in existing) or "（无）"

    messages = [
        {"role": "system", "content": _EXTRACTION_PROMPT % FACTS_PER_CALL},
        {"role": "user", "content": f"【已有事实】\n{existing_block}\n\n【对话】\n{dialogue}"},
    ]
    raw = chat(messages, cfg)
    arr = _parse_json_array(raw)
    valid_old = {i for i, _, _ in existing}
    n = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    fact_ts = con.execute(
        "SELECT ts FROM msgs WHERE session_id=? AND source=? AND id<=? AND ts != '' ORDER BY id DESC LIMIT 1",
        (sid, source, max_id)).fetchone()
    fact_ts = (fact_ts[0] if fact_ts else "")
    if arr:
        for f in arr[:FACTS_PER_CALL]:
            if not isinstance(f, dict):
                continue
            content = (f.get("content") or "").strip()
            if not content:
                continue
            kind = (f.get("kind") or "fact").strip().lower()
            if kind not in VALID_KINDS:
                kind = "fact"
            subject = (f.get("subject") or "").strip()[:24]
            cur = con.execute(
                "INSERT INTO facts(source,project,session_id,ts,kind,subject,content,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (source, project, sid, fact_ts, kind, subject, content[:200], now))
            new_id = cur.lastrowid
            n += 1
            reason = (f.get("reason") or "").strip()[:120]
            for old in (f.get("supersedes") or []):
                try:
                    old = int(old)
                except (TypeError, ValueError):
                    continue
                if old not in valid_old or old == new_id:
                    continue
                if conflict_mode(cfg) == "auto":
                    # 旧行为：直接信任模型判定，立即推翻
                    con.execute("UPDATE facts SET superseded_by=? WHERE id=? AND superseded_by IS NULL",
                                (new_id, old))
                elif con.execute("SELECT superseded_by FROM facts WHERE id=?", (old,)).fetchone()[0] is None:
                    # HITL（默认）：模型只有提议权——挂起待人工裁决，新旧事实在裁决前都保留可见
                    con.execute(
                        "INSERT INTO fact_conflicts(old_id,new_id,reason,created_at) VALUES(?,?,?,?)",
                        (old, new_id, reason, now))
    con.execute("INSERT OR REPLACE INTO facts_cursor(session_id,last_msg_id,extracted_at) VALUES(?,?,?)",
                (sid, max_id, now))
    return n, max_id


def _embed_new_facts(con, quiet=False):
    """给还没有向量的事实补向量（复用 embed 层 provider；无 provider 时静默跳过）。"""
    ecfg = embed.provider_info()
    if not ecfg:
        return 0
    rows = con.execute("SELECT id, content FROM facts WHERE id NOT IN (SELECT fact_id FROM fact_vecs)").fetchall()
    if not rows:
        return 0
    cur_model = f"{ecfg['provider']}:{ecfg['model']}"
    if _meta_get(con, "vec_model") not in (None, cur_model):
        con.execute("DELETE FROM fact_vecs")
        if not quiet:
            log(f"embedding 模型变更为 {cur_model}，事实向量已清空重建。")
    _meta_set(con, "vec_model", cur_model)
    try:
        vecs = embed.embed_texts([r[1][:embed.MAX_EMBED_CHARS] for r in rows], ecfg)
    except Exception as e:
        if not quiet:
            log(f"(事实向量跳过：{e})")
        return 0
    con.executemany("INSERT OR REPLACE INTO fact_vecs(fact_id, vec) VALUES(?,?)",
                    [(rid, embed.vec_to_blob(v)) for (rid, _), v in zip(rows, vecs)])
    return len(rows)


def backfill(limit_sessions=None, quiet=False):
    """异步提取入口：每次处理 N 个会话（默认 sessions_per_run），供 reindex / CLI 调用。"""
    cfg = provider_info()
    if not cfg:
        if not quiet:
            log("事实层未启用（配置 facts.json 或让 Ollama 跑一个 instruct 模型）。")
        return -1
    if not embed.DB_PATH.exists():
        return -1
    con = sqlite3.connect(embed.DB_PATH)
    ensure_tables(con)
    n_sessions = int(limit_sessions or cfg.get("sessions_per_run") or 3)
    todo = _pending_sessions(con, n_sessions)
    if not todo:
        con.commit()
        con.close()
        return 0
    total = 0
    for source, project, sid, max_id in todo:
        try:
            n, done_id = extract_session(con, source, project, sid, 0, cfg)
            total += n
            if not quiet:
                log(f"  事实提取 {sid[:20]}… +{n} 条")
        except Exception as e:
            if not quiet:
                log(f"! 事实提取失败（{sid[:20]}）：{e}，跳过该会话")
    _embed_new_facts(con, quiet=quiet)
    con.commit()
    con.close()
    _auto_resolve_ttl(cfg)
    return total


# ---------------- search ----------------

def _esc_like(q):
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_result(r, via="facts"):
    fid, source, project, sid, ts, kind, subject, content, sup_by = r
    return {
        "id": fid, "fact": True, "kind": kind, "subject": subject,
        "superseded_by": sup_by,
        "source": source or "", "project": project or "", "session_id": sid or "",
        "file_path": "", "role": "fact", "ts": ts or "",
        "snippet": content, "via": via,
    }


def facts_search(query, source=None, project=None, limit=5, semantic=True):
    """关键词(LIKE) + 可选语义(向量) 双路事实检索，RRF 融合；被推翻的事实默认不出现。"""
    if not embed.DB_PATH.exists():
        return []
    con = sqlite3.connect(f"file:{embed.DB_PATH}?mode=ro", uri=True)
    try:
        if not _tables_ready(con):
            return []
        terms = [t for t in (query or "").split() if t] or [str(query or "")]
        # 事实是单句蒸馏文本，不做跨词 AND（自然语言问句很难全词命中）：
        # 任一词条命中即入选，按命中词条数降序、再按插入时间降序
        match_clauses, score_parts = [], []
        where_args, score_args = [], []
        for t in terms:
            pat = f"%{_esc_like(t)}%"
            match_clauses.append("(subject LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')")
            score_parts.append("((subject LIKE ? ESCAPE '\\') OR (content LIKE ? ESCAPE '\\'))")
            where_args += [pat, pat]
            score_args += [pat, pat]
        sql = ("SELECT id,source,project,session_id,ts,kind,subject,content,superseded_by FROM facts "
               "WHERE superseded_by IS NULL AND (" + " OR ".join(match_clauses) + ")")
        args = list(where_args)
        if source:
            sql += " AND source=?"
            args.append(source)
        if project:
            sql += " AND project LIKE ? ESCAPE '\\'"
            args.append(f"%{_esc_like(project)}%")
        sql += " ORDER BY (" + " + ".join(score_parts) + ") DESC, id DESC LIMIT ?"
        args += score_args + [min(max(limit * 10, 40), 400)]
        like_rows = con.execute(sql, args).fetchall()
        like_results = [_row_to_result(r, "facts-exact") for r in like_rows][:limit]

        sem_results = []
        if semantic:
            try:
                sem_results = _semantic_facts(con, query, source, project, limit)
            except Exception:
                sem_results = []
        if like_results and sem_results:
            fused = embed.rrf_fuse(like_results, sem_results, limit)
            out = fused[:limit]
        else:
            out = (sem_results or like_results)[:limit]
        _attach_pending(con, out)
        return out
    finally:
        con.close()


def _attach_pending(con, results):
    """给命中事实挂上待确认矛盾标注（HITL 呈现用；旧库无该表时静默跳过）。"""
    if not results:
        return
    try:
        ids = [int(r["id"]) for r in results]
        ph = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT id, old_id, new_id, reason FROM fact_conflicts "
            f"WHERE state='pending' AND (old_id IN ({ph}) OR new_id IN ({ph}))",
            ids + ids).fetchall()
    except sqlite3.OperationalError:
        return
    if not rows:
        return
    by_id = {}
    for cid, old_id, new_id, reason in rows:
        by_id.setdefault(old_id, []).append({"conflict_id": cid, "other_fact_id": new_id,
                                             "reason": reason, "role": "old"})
        by_id.setdefault(new_id, []).append({"conflict_id": cid, "other_fact_id": old_id,
                                             "reason": reason, "role": "new"})
    for r in results:
        pend = by_id.get(int(r["id"]))
        if pend:
            r["pending_conflicts"] = pend


def _semantic_facts(con, query, source, project, limit):
    import numpy as np
    ecfg = embed.provider_info()
    if not ecfg:
        return []
    n = con.execute("SELECT COUNT(*) FROM fact_vecs").fetchone()[0]
    if not n:
        return []
    qvec = embed._query_vec(query, ecfg)
    sql = ("SELECT f.id,f.source,f.project,f.session_id,f.ts,f.kind,f.subject,f.content,f.superseded_by,v.vec "
           "FROM fact_vecs v JOIN facts f ON f.id = v.fact_id WHERE f.superseded_by IS NULL")
    args = []
    if source:
        sql += " AND f.source=?"
        args.append(source)
    if project:
        sql += " AND f.project LIKE ? ESCAPE '\\'"
        args.append(f"%{_esc_like(project)}%")
    rows = con.execute(sql, args).fetchall()
    if not rows:
        return []
    dim = len(rows[0][-1]) // 4
    mat = np.frombuffer(b"".join(r[-1] for r in rows), dtype=np.float32).reshape(len(rows), dim)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    q = np.asarray(qvec, dtype=np.float32)
    qn = np.linalg.norm(q)
    if qn == 0:
        return []
    scores = (mat / norms) @ (q / qn)
    k = min(limit, len(rows))
    top = np.argsort(-scores)[:k]
    import struct
    out = []
    for i in top:
        r = list(rows[i][:-1])
        res = _row_to_result(tuple(r), "facts-semantic")
        res["score"] = round(float(scores[i]), 4)
        out.append(res)
    return out


def list_facts(project=None, source=None, include_superseded=False, limit=50):
    if not embed.DB_PATH.exists():
        return []
    con = sqlite3.connect(f"file:{embed.DB_PATH}?mode=ro", uri=True)
    try:
        if not _tables_ready(con):
            return []
        sql = "SELECT id,source,project,session_id,ts,kind,subject,content,superseded_by FROM facts WHERE 1=1"
        args = []
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        if source:
            sql += " AND source=?"
            args.append(source)
        if project:
            sql += " AND project LIKE ? ESCAPE '\\'"
            args.append(f"%{_esc_like(project)}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [_row_to_result(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def fmt_facts(rows, title=None):
    if not rows:
        return "没有事实记录（事实层未启用或尚未提取；启用见 ~/.pawrecall/facts.json.example）。"
    lines = [title or f"{len(rows)} 条事实："]
    home = HOME.as_posix()
    for i, r in enumerate(rows, 1):
        proj = (r["project"] or "").replace(home, "~")
        lines.append(f"[{i}] [{r['kind']}] {r['subject']} — {r['snippet']}")
        lines.append(f"    {r['source']} · {r['ts']} · {proj} · session: {r['session_id']}"
                     + (f" · 已被 #{r['superseded_by']} 推翻" if r.get("superseded_by") else ""))
        for p in (r.get("pending_conflicts") or [])[:2]:
            lines.append(f"    ⚠ 与事实 #{p['other_fact_id']} 待确认矛盾：{p.get('reason') or '（模型未给理由）'}"
                         f"——向用户确认后用 paw_facts_resolve 裁决")
    return "\n".join(lines)


# ---------------- conflict resolution (HITL) ----------------

_DECISIONS = ("new_wins", "old_wins", "both")


def list_conflicts(project=None, state="pending", limit=50):
    """冲突清单（默认待裁决），每行带新旧双方内容与溯源。"""
    if not embed.DB_PATH.exists():
        return []
    con = sqlite3.connect(embed.DB_PATH)
    try:
        ensure_tables(con)
        sql = ("SELECT c.id, c.state, c.reason, c.created_at, c.resolved_at, c.resolved_by,"
               " o.id, o.content, o.ts, o.session_id, o.project, o.superseded_by,"
               " n.id, n.content, n.ts, n.session_id, n.project"
               " FROM fact_conflicts c"
               " JOIN facts o ON o.id = c.old_id JOIN facts n ON n.id = c.new_id"
               " WHERE c.state=?")
        args = [state]
        if project:
            sql += " AND (o.project LIKE ? ESCAPE '\\' OR n.project LIKE ? ESCAPE '\\')"
            args += [f"%{_esc_like(project)}%", f"%{_esc_like(project)}%"]
        sql += " ORDER BY c.id DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for r in con.execute(sql, args):
            out.append({
                "conflict_id": r[0], "state": r[1], "reason": r[2],
                "created_at": r[3], "resolved_at": r[4], "resolved_by": r[5],
                "old": {"id": r[6], "content": r[7], "ts": r[8], "session_id": r[9],
                        "project": r[10], "superseded_by": r[11]},
                "new": {"id": r[12], "content": r[13], "ts": r[14], "session_id": r[15],
                        "project": r[16]},
            })
        return out
    finally:
        con.close()


def fmt_conflicts(rows, title=None):
    if not rows:
        return "没有待确认的矛盾。"
    lines = [title or f"{len(rows)} 条待确认矛盾："]
    for c in rows:
        lines.append(f"\n[#{c['conflict_id']}] {c['reason'] or '（模型未给理由）'} · 提出于 {c['created_at']}")
        lines.append(f"    旧事实 #{c['old']['id']} ({c['old']['ts']}): {c['old']['content'][:90]}")
        lines.append(f"    新事实 #{c['new']['id']} ({c['new']['ts']}): {c['new']['content'][:90]}")
        if c["state"] != "pending":
            lines.append(f"    状态: {c['state']} by {c['resolved_by']} @ {c['resolved_at']}")
        else:
            lines.append("    裁决: facts resolve --conflict %d --decision new_wins|old_wins|both"
                         % c["conflict_id"])
    return "\n".join(lines)


def resolve_conflict(conflict_id, decision, by="user"):
    """人工裁决一条矛盾。new_wins=旧事实被新事实取代；old_wins=新事实被否（隐藏）；
    both=矛盾判定不成立，两条都保留。返回结果描述字符串。"""
    if decision not in _DECISIONS:
        return f"无效裁决: {decision}（可选 {', '.join(_DECISIONS)}）"
    con = sqlite3.connect(embed.DB_PATH)
    try:
        ensure_tables(con)
        row = con.execute("SELECT old_id, new_id, state FROM fact_conflicts WHERE id=?",
                          (int(conflict_id),)).fetchone()
        if not row:
            return f"冲突 #{conflict_id} 不存在。"
        old_id, new_id, state = row
        if state != "pending":
            return f"冲突 #{conflict_id} 已处理过（{state}），无需重复裁决。"
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        new_state = "confirmed" if decision in ("new_wins", "old_wins") else "rejected"
        if decision == "new_wins":
            con.execute("UPDATE facts SET superseded_by=? WHERE id=? AND superseded_by IS NULL",
                        (new_id, old_id))
        elif decision == "old_wins":
            con.execute("UPDATE facts SET superseded_by=? WHERE id=? AND superseded_by IS NULL",
                        (old_id, new_id))
        con.execute("UPDATE fact_conflicts SET state=?, resolved_at=?, resolved_by=? WHERE id=?",
                    (new_state, now, by, int(conflict_id)))
        # 同侧已被抑制的其它 pending 冲突随之失效（旧事实已隐藏，无需再裁决）
        suppressed = old_id if decision == "new_wins" else (new_id if decision == "old_wins" else None)
        if suppressed:
            con.execute(
                "UPDATE fact_conflicts SET state='stale', resolved_at=?, resolved_by=? "
                "WHERE state='pending' AND id!=? AND (old_id=? OR new_id=?)",
                (now, f"stale-of-{conflict_id}", int(conflict_id), suppressed, suppressed))
        con.commit()
        return f"冲突 #{conflict_id} 已裁决: {decision}（by {by}）"
    finally:
        con.close()


def pending_conflict_count(project=None, source=None):
    """作用域内待裁决矛盾数（检索 note 提示用；旧库无表时返回 0）。"""
    if not embed.DB_PATH.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{embed.DB_PATH}?mode=ro", uri=True)
        try:
            sql = ("SELECT COUNT(*) FROM fact_conflicts c"
                   " JOIN facts o ON o.id=c.old_id JOIN facts n ON n.id=c.new_id"
                   " WHERE c.state='pending'")
            args = []
            if project:
                sql += " AND (o.project LIKE ? ESCAPE '\\' OR n.project LIKE ? ESCAPE '\\')"
                args += [f"%{_esc_like(project)}%", f"%{_esc_like(project)}%"]
            if source:
                sql += " AND (o.source=? OR n.source=?)"
                args += [source, source]
            return con.execute(sql, args).fetchone()[0]
        finally:
            con.close()
    except sqlite3.OperationalError:
        return 0


def _auto_resolve_ttl(cfg):
    """可选兜底：超过 auto_resolve_days 未裁决的冲突按 new_wins 收敛（默认关闭）。"""
    days = int((cfg or {}).get("auto_resolve_days") or 0)
    if days <= 0:
        return 0
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - days * 86400))
    con = sqlite3.connect(embed.DB_PATH)
    try:
        ensure_tables(con)
        rows = con.execute(
            "SELECT id FROM fact_conflicts WHERE state='pending' AND created_at < ?",
            (cutoff,)).fetchall()
    finally:
        con.close()
    n = 0
    for (cid,) in rows:
        resolve_conflict(cid, "new_wins", by=f"auto-ttl-{days}d")
        n += 1
    if n:
        log(f"自动收敛 {n} 条超时未裁决的矛盾（new_wins，auto_resolve_days={days}）。")
    return n


def status():
    cfg = provider_info()
    if not embed.DB_PATH.exists():
        return cfg, 0, 0, 0, 0, 0
    con = sqlite3.connect(f"file:{embed.DB_PATH}?mode=ro", uri=True)
    if not _tables_ready(con):
        con.close()
        return cfg, 0, 0, 0, 0, 0
    total = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    superseded = con.execute("SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL").fetchone()[0]
    pending = con.execute(
        """SELECT COUNT(*) FROM (
             SELECT m.session_id FROM msgs m
             LEFT JOIN facts_cursor c ON c.session_id = m.session_id
             WHERE c.session_id IS NULL OR m.id > c.last_msg_id
             GROUP BY m.session_id)""").fetchone()[0]
    vecs = con.execute("SELECT COUNT(*) FROM fact_vecs").fetchone()[0]
    try:
        conflicts = con.execute("SELECT COUNT(*) FROM fact_conflicts WHERE state='pending'").fetchone()[0]
    except sqlite3.OperationalError:
        conflicts = 0
    con.close()
    return cfg, total, superseded, pending, vecs, conflicts


def reset():
    con = sqlite3.connect(embed.DB_PATH)
    ensure_tables(con)
    for t in ("fact_vecs", "facts", "facts_cursor", "facts_meta"):
        con.execute(f"DELETE FROM {t}")
    con.commit()
    con.close()


if __name__ == "__main__":
    ap = __import__("argparse").ArgumentParser(description="pawrecall fact layer")
    ap.add_argument("cmd", choices=["run", "status", "reset", "conflicts", "resolve"])
    ap.add_argument("--sessions", type=int, default=None)
    ap.add_argument("--state", default="pending", help="conflicts 列表的状态过滤")
    ap.add_argument("--conflict", type=int, help="resolve：冲突 id")
    ap.add_argument("--decision", choices=_DECISIONS, help="resolve：裁决")
    ap.add_argument("--by", default="cli", help="resolve：裁决人标识")
    a = ap.parse_args()
    if a.cmd == "status":
        cfg, total, superseded, pending, vecs, conflicts = status()
        print(f"chat provider: {(cfg['provider'] + '/' + cfg['model']) if cfg else '未配置（事实层禁用）'}"
              f"  冲突策略: {conflict_mode(cfg)}")
        print(f"事实总数: {total}（被推翻 {superseded}）  已向量化: {vecs}  "
              f"待提取会话: {pending}  待裁决矛盾: {conflicts}")
    elif a.cmd == "reset":
        reset()
        print("已清空全部事实。")
    elif a.cmd == "conflicts":
        print(fmt_conflicts(list_conflicts(state=a.state)))
    elif a.cmd == "resolve":
        if not a.conflict or not a.decision:
            __import__("sys").exit("resolve 需要 --conflict ID --decision new_wins|old_wins|both")
        print(resolve_conflict(a.conflict, a.decision, by=a.by))
    else:
        n = backfill(limit_sessions=a.sessions)
        print(f"本次提取 {n} 条事实。" if n >= 0 else "事实层未启用。")
