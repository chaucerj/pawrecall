#!/usr/bin/env python3
"""Optional semantic layer for pawrecall.

Embedding providers (auto-detected in this order):
  1. Explicit config  ~/.pawrecall/embed.json  or PAWRECALL_EMBED_* env vars
     {"provider": "openai", "url": "https://ark.cn-beijing.volces.com/api/v3",
      "key": "...", "model": "doubao-embedding-text-240515"}
     url 也可以指向 Ollama 的 OpenAI 兼容端点 http://localhost:11434/v1
  2. fastembed (pip install fastembed，完全离线，首次运行下载 ~90MB 模型)
  3. Ollama native (http://localhost:11434，需要先 ollama pull bge-m3)

都没有 -> provider 为 None，语义层自动禁用，检索回落到精确匹配（原行为）。

向量存储：history.db 的 vecs 表（float32 BLOB），与消息同库。
检索：numpy 余弦暴力扫全量 + RRF 与精确匹配融合（见 index.hybrid_search）。
"""
import http.client
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path.home()
AI_DIR = HOME / ".pawrecall"
# PAWRECALL_DB：指向副本库跑评测/基准，绝不碰生产库（与 index.py 保持一致）
DB_PATH = Path(os.environ.get("PAWRECALL_DB") or AI_DIR / "history.db")
CONFIG_PATH = AI_DIR / "embed.json"

MIN_EMBED_CHARS = 6       # 太短的消息没有语义价值，跳过嵌入
MAX_EMBED_CHARS = 2000    # 超长截断，避免触发模型 token 上限
EMBED_BATCH = 64          # 本地 Ollama 无请求数限制，批大吞吐高；云端 API 可用 embed.json "batch" 覆盖
RRF_K = 60                # Reciprocal Rank Fusion 常数

OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
_KNOWN_OLLAMA_EMBED_MODELS = (
    "bge-m3", "qwen3-embedding", "nomic-embed-text", "mxbai-embed-large",
    "snowflake-arctic-embed", "bge-large", "bge-small",
)


def log(msg):
    print(msg, file=sys.stderr)


# ---------------- provider detection ----------------

def load_config():
    """显式配置：环境变量覆盖 embed.json。返回 normalized dict 或 None。"""
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"! embed.json 解析失败，忽略：{e}")
            cfg = {}
    # 环境变量覆盖（CI / 临时切换方便）
    env_map = {
        "PAWRECALL_EMBED_URL": "url",
        "PAWRECALL_EMBED_KEY": "key",
        "PAWRECALL_EMBED_MODEL": "model",
        "PAWRECALL_EMBED_PROVIDER": "provider",
    }
    for k, v in env_map.items():
        if os.environ.get(k):
            cfg[v] = os.environ[k]
    if not cfg:
        return None
    provider = (cfg.get("provider") or "").strip().lower()
    url = (cfg.get("url") or "").strip().rstrip("/")
    if not provider:
        provider = "ollama" if "localhost:11434" in url else "openai"
    if provider == "openai" and not url:
        return None
    cfg["provider"] = provider
    cfg["url"] = url
    cfg.setdefault("model", "")
    cfg.setdefault("key", "")
    return cfg


def detect_provider():
    """返回 {"provider":..., "model":...} 或 None（语义层禁用）。"""
    cfg = load_config()
    if cfg:
        return cfg
    try:  # fastembed：完全离线，优先于需要后台进程的 Ollama
        import fastembed  # noqa: F401
        return {"provider": "fastembed", "model": "BAAI/bge-small-zh-v1.5", "key": "", "url": ""}
    except ImportError:
        pass
    try:  # Ollama：本机有 embedding 模型就自动启用
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=1) as r:
            names = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
        for base in _KNOWN_OLLAMA_EMBED_MODELS:
            for n in names:
                if n.startswith(base):
                    return {"provider": "ollama", "model": n, "key": "", "url": "http://localhost:11434"}
        return {"provider": "ollama", "model": "bge-m3", "key": "", "url": "http://localhost:11434"}
    except Exception:
        return None


_PROVIDER_INFO = None


def provider_info():
    global _PROVIDER_INFO
    if _PROVIDER_INFO is None:
        _PROVIDER_INFO = detect_provider()
    return _PROVIDER_INFO


# ---------------- embedding calls ----------------

# ---------------- HTTP（keep-alive 连接复用，省去每次 TLS 握手） ----------------

_CONN = {}  # (scheme, host) -> http.client connection


def _request(method, url, payload, key="", timeout=120):
    """带 keep-alive 的 POST；连接失效自动重建重试一次。"""
    u = urlsplit(url)
    ckey = (u.scheme, u.netloc)
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    for attempt in (1, 2):
        conn = _CONN.get(ckey)
        try:
            if conn is None:
                conn = (http.client.HTTPSConnection(u.netloc, timeout=timeout)
                        if u.scheme == "https" else http.client.HTTPConnection(u.netloc, timeout=timeout))
                _CONN[ckey] = conn
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, data)
            return json.loads(data.decode("utf-8"))
        except (http.client.HTTPException, OSError) as e:
            # 连接可能被服务端关闭：重建后再试一次；HTTPError(4xx/5xx) 不在此列，直接抛
            _CONN.pop(ckey, None)
            conn = None
            if attempt == 2 or isinstance(e, urllib.error.HTTPError):
                raise
            time.sleep(0.3)


def _post_json(url, payload, key="", timeout=120):
    return _request("POST", url, payload, key, timeout)


def _embed_openai(texts, cfg):
    """OpenAI 兼容 /embeddings，自动分批。"""
    url = cfg["url"] + "/embeddings"
    batch = int(cfg.get("batch") or EMBED_BATCH)
    out = []
    for i in range(0, len(texts), batch):
        resp = _post_json(url, {"model": cfg["model"], "input": texts[i:i + batch]}, cfg["key"])
        out.extend(d["embedding"] for d in resp["data"])
    return out


def _embed_ollama(texts, cfg):
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        resp = _post_json(OLLAMA_EMBED_URL, {"model": cfg["model"], "input": texts[i:i + EMBED_BATCH]})
        out.extend(resp["embeddings"])
    return out


_FE_MODEL = None


def _embed_fastembed(texts, cfg):
    global _FE_MODEL
    if _FE_MODEL is None:
        from fastembed import TextEmbedding
        model_name = cfg.get("model") or "BAAI/bge-small-zh-v1.5"
        _FE_MODEL = TextEmbedding(model_name=model_name)
    return [v.tolist() for v in _FE_MODEL.embed(texts)]


def embed_texts(texts, cfg=None, patient=True):
    """texts -> list[list[float]]。失败抛异常，由调用方决定降级。

    patient=True  （回填）429 长退避 5s→120s，尽量不丢批次
    patient=False （在线查询）429 最多等 0.8s，快速失败让调用方回落
    """
    cfg = cfg or provider_info()
    if not cfg or not texts:
        return []
    fn = {"openai": _embed_openai, "ollama": _embed_ollama, "fastembed": _embed_fastembed}[cfg["provider"]]
    last = None
    waits = (5, 15, 30, 60, 90, 120, 120) if patient else (0.8,)
    for attempt in range(len(waits) + 1):
        try:
            return fn(texts, cfg)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < len(waits):
                time.sleep(waits[attempt])
                continue
            break
        except Exception as e:
            last = e
            if attempt >= (1 if patient else 0):
                break
            time.sleep(0.3)
    raise RuntimeError(f"embedding 调用失败（{cfg['provider']}/{cfg['model']}）：{last}")


# ---------------- vector storage ----------------

def ensure_tables(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS vecs(
          msg_id INTEGER PRIMARY KEY REFERENCES msgs(id) ON DELETE CASCADE,
          vec BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS embed_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS qcache(
          qkey TEXT PRIMARY KEY,   -- model + 归一化 query
          vec BLOB NOT NULL
        );
        """
    )


def vec_to_blob(v):
    import struct
    return struct.pack(f"{len(v)}f", *v)


def meta_get(con, key):
    row = con.execute("SELECT value FROM embed_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(con, key, value):
    con.execute("INSERT INTO embed_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def vec_status():
    """(provider_cfg, 已嵌入数, 待嵌入数, 当前模型)。"""
    cfg = provider_info()
    if not DB_PATH.exists():
        return cfg, 0, 0, ""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    done = con.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
    pending = con.execute(
        "SELECT COUNT(*) FROM msgs WHERE length(text) >= ? AND id NOT IN (SELECT msg_id FROM vecs)",
        (MIN_EMBED_CHARS,)).fetchone()[0]
    model = con.execute("SELECT value FROM embed_meta WHERE key='model'").fetchone()
    con.close()
    return cfg, done, pending, (model[0] if model else "")


# ---------------- backfill ----------------

def backfill(limit=None, batch=EMBED_BATCH, quiet=False):
    """为未嵌入的消息增量生成向量。返回本次嵌入条数（provider 缺失返回 -1）。"""
    cfg = provider_info()
    if not cfg:
        if not quiet:
            log("未检测到 embedding 来源（embed.json / fastembed / Ollama），语义层保持禁用。")
            log("启用方法见：~/.pawrecall/embed.json.example")
        return -1
    if not DB_PATH.exists():
        return -1
    con = sqlite3.connect(DB_PATH)
    ensure_tables(con)
    con.execute("DELETE FROM vecs WHERE msg_id NOT IN (SELECT id FROM msgs)")  # 清理悬空向量
    con.execute("DELETE FROM vecs WHERE msg_id IN (SELECT id FROM msgs WHERE length(text) < ?)",
                (MIN_EMBED_CHARS,))  # 清理超短消息向量（语义噪声）
    cur_model = f"{cfg['provider']}:{cfg['model']}"
    if meta_get(con, "model") not in (None, cur_model):
        con.execute("DELETE FROM vecs")  # 换模型：维度/语义空间变了，全部重来
        if not quiet:
            log(f"embedding 模型变更为 {cur_model}，已清空旧向量重新嵌入。")
    meta_set(con, "model", cur_model)

    sql = ("SELECT id, text FROM msgs WHERE length(text) >= ? AND id NOT IN (SELECT msg_id FROM vecs) ORDER BY id")
    args = [MIN_EMBED_CHARS]
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    todo = con.execute(sql, args).fetchall()
    n_total, n_ok = len(todo), 0
    if not todo:
        con.commit()
        con.close()
        return 0
    if not quiet:
        log(f"开始嵌入 {n_total} 条消息（{cur_model}）…")
    t0 = time.time()
    for i in range(0, n_total, batch):
        chunk = todo[i:i + batch]
        texts = [t[:MAX_EMBED_CHARS] for _, t in chunk]
        try:
            vecs = embed_texts(texts, cfg)
        except Exception as e:
            log(f"! 批次失败，中止本次回填（已嵌入 {n_ok} 条）：{e}")
            break
        if len(vecs) != len(chunk):
            log(f"! 返回向量数不符（{len(vecs)}/{len(chunk)}），中止。")
            break
        con.executemany(
            "INSERT OR REPLACE INTO vecs(msg_id, vec) VALUES(?, ?)",
            [(mid, vec_to_blob(v)) for (mid, _), v in zip(chunk, vecs)])
        con.commit()
        n_ok += len(chunk)
        time.sleep(0.05)  # 轻度限速：本地推理无需避 RPM，仅留缓冲
        if not quiet and (i // batch) % 50 == 0 and n_total > batch:
            rate = n_ok / max(time.time() - t0, 1e-6)
            log(f"  {n_ok}/{n_total}  ({rate:.0f} 条/秒)")
    meta_set(con, "last_backfill", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    con.commit()
    con.close()
    if not quiet:
        log(f"嵌入完成：{n_ok}/{n_total} 条，耗时 {time.time() - t0:.0f}s。")
    return n_ok


# ---------------- semantic search ----------------

_MATRIX_CACHE = {"key": None, "ids": None, "mat": None, "src": None, "proj": None, "ts": 0.0}
_MATRIX_TTL = 60.0  # 回填期间 count 不断变化，TTL 内沿用旧矩阵，避免每次查询都重载 300MB


def _load_matrix(con, cfg):
    """全量向量载入 numpy 矩阵并预归一化；按 (model, dim) 缓存 + TTL 刷新。"""
    import numpy as np
    model = meta_get(con, "model") or ""
    count, dim = con.execute("SELECT COUNT(*), MIN(LENGTH(vec))/4 FROM vecs").fetchone()
    count, dim = count or 0, dim or 0
    key = (model, dim, count)
    now = time.time()
    cache = _MATRIX_CACHE
    same_model = cache["key"] and cache["key"][:2] == key[:2]  # 同模型同维度
    if cache["mat"] is not None and same_model and now - cache["ts"] < _MATRIX_TTL:
        return cache  # TTL 内沿用（回填增量晚点再见）
    if count == 0:
        cache.update(key=key, ids=None, mat=None, src=None, proj=None, ts=now)
        return cache
    ids, srcs, projs, blobs = [], [], [], []
    for mid, src, proj, blob in con.execute(
            "SELECT v.msg_id, m.source, m.project, v.vec FROM vecs v JOIN msgs m ON m.id = v.msg_id"):
        ids.append(mid)
        srcs.append(src or "")
        projs.append(proj or "")
        blobs.append(blob)
    ids = np.asarray(ids, dtype=np.int64)
    mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), dim).copy()
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat /= norms
    _MATRIX_CACHE.update(key=key, ids=ids, mat=mat,
                         src=np.array(srcs), proj=np.array(projs), ts=time.time())
    return _MATRIX_CACHE


_QVEC_MEM = {}  # 进程内查询向量缓存 {qkey: list[float]}


def _query_vec(query, cfg):
    """查询向量化，带两层缓存（内存 + SQLite qcache）；失败抛异常。"""
    import struct
    qkey = f"{cfg['provider']}:{cfg['model']}:{' '.join(query.split()).lower()}"
    if qkey in _QVEC_MEM:
        return _QVEC_MEM[qkey]
    con = sqlite3.connect(DB_PATH)
    try:
        ensure_tables(con)
        row = con.execute("SELECT vec FROM qcache WHERE qkey=?", (qkey,)).fetchone()
        if row:
            v = list(struct.unpack(f"{len(row[0])//4}f", row[0]))
            _QVEC_MEM[qkey] = v
            return v
    finally:
        con.close()
    v = embed_texts([query[:MAX_EMBED_CHARS]], cfg, patient=False)[0]  # 在线路径：快速失败
    blob = struct.pack(f"{len(v)}f", *v)
    try:
        con = sqlite3.connect(DB_PATH)
        ensure_tables(con)
        con.execute("INSERT OR REPLACE INTO qcache(qkey, vec) VALUES(?, ?)", (qkey, blob))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()
    _QVEC_MEM[qkey] = v
    return v


def semantic_search(query, source=None, project=None, limit=10, cfg=None):
    """余弦相似度 Top-K。返回 (results, note)；不可用时 results=[] 并说明原因。"""
    import numpy as np
    cfg = cfg or provider_info()
    if not cfg:
        return [], "语义层未启用"
    if not DB_PATH.exists():
        return [], "索引库不存在"
    try:
        qvec = _query_vec(query, cfg)
    except Exception as e:
        return [], f"query 向量化失败：{e}"
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cache = _load_matrix(con, cfg)
        if cache["mat"] is None:
            return [], "向量索引为空（先运行: python3 ~/.pawrecall/index.py embed）"
        mask = np.ones(len(cache["ids"]), dtype=bool)
        if source:
            mask &= cache["src"] == source
        if project:
            mask &= np.array([project in p for p in cache["proj"]])
        idx = np.nonzero(mask)[0]
        if len(idx) == 0:
            return [], "过滤条件下无向量候选"
        sub = cache["mat"][idx]
        q = np.asarray(qvec, dtype=np.float32)
        qn = np.linalg.norm(q)
        if qn == 0:
            return [], "query 向量异常"
        q /= qn
        scores = sub @ q
        k = min(max(limit * 4, 40), len(idx))
        pos = np.argpartition(-scores, k - 1)[:k]   # 过滤子集内的无序 top-k（下标对齐 idx）
        pos = pos[np.argsort(-scores[pos])]         # 按分数降序
        top = idx[pos]                              # 映射回全量矩阵行号
        rows = {}
        for mid in cache["ids"][top]:
            rows[int(mid)] = None  # sqlite3 不认 np.int64，必须转原生 int
        placeholders = ",".join("?" * len(rows))
        for mid, src, proj, sid, fpath, role, ts, text in con.execute(
                f"SELECT id,source,project,session_id,file_path,role,ts,text FROM msgs WHERE id IN ({placeholders})",
                tuple(rows)):
            rows[mid] = (src, proj, sid, fpath, role, ts, text)
        score_by_id = {int(i): float(s) for i, s in zip(cache["ids"][top], scores[pos])}
        out = []
        for mid in sorted(rows, key=lambda m: -score_by_id.get(m, -1)):
            r = rows[mid]
            if not r:
                continue
            src, proj, sid, fpath, role, ts, text = r
            out.append({
                "id": mid,
                "source": src, "project": proj or "", "session_id": sid or "",
                "file_path": fpath or "", "role": role or "", "ts": ts or "",
                "snippet": text[:320].replace("\n", " ").strip(),
                "via": "semantic", "score": round(score_by_id.get(mid, 0.0), 4),
            })
            if len(out) >= limit:
                break
        return out, f"语义检索（{cfg['provider']}/{cfg['model']}）"
    finally:
        con.close()


# ---------------- hybrid fusion ----------------

def rrf_fuse(list_a, list_b, limit):
    """Reciprocal Rank Fusion：score = Σ 1/(RRF_K + rank)。按消息 id 去重合并。"""
    scores, by_key = {}, {}
    for rank, r in enumerate(list_a, 1):
        key = r.get("id") or (r["file_path"], r["snippet"][:64])
        by_key[key] = r
        scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    for rank, r in enumerate(list_b, 1):
        key = r.get("id") or (r["file_path"], r["snippet"][:64])
        if key in by_key:
            by_key[key]["via"] = "both"
        else:
            by_key[key] = r
        scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    out = [by_key[k] for k in sorted(scores, key=lambda k: -scores[k])]
    return out[:limit]


def group_sessions(fused, limit, fill_second=True):
    """按会话聚合融合结果：会话分 = 各消息名次分之和（聊得越深分越高）。

    每个会话只出最优 1 条消息（防单会话刷屏）；若名额有富余，用次优消息补齐。
    输出附带 session_hits 字段（该会话在候选池中的命中数）。"""
    sessions = {}  # skey -> {"score": float, "msgs": [msg...]}
    for rank, r in enumerate(fused, 1):
        skey = (r.get("source"), r.get("session_id") or r.get("file_path"))
        s = sessions.setdefault(skey, {"score": 0.0, "msgs": []})
        s["score"] += 1.0 / (RRF_K + rank)
        s["msgs"].append(r)
    ordered = sorted(sessions.values(), key=lambda s: -s["score"])
    out = []
    for s in ordered:  # 第一轮：每会话 1 条最优
        best = s["msgs"][0]
        best["session_hits"] = len(s["msgs"])
        out.append(best)
        if len(out) >= limit:
            return out
    if fill_second:  # 第二轮：名额富余时用各会话次优消息补
        for s in ordered:
            for r in s["msgs"][1:limit]:
                r["session_hits"] = len(s["msgs"])
                out.append(r)
                if len(out) >= limit:
                    return out
    return out


if __name__ == "__main__":
    cfg, done, pending, model = vec_status()
    print(f"provider: {cfg['provider'] + '/' + cfg['model'] if cfg else '未启用'}")
    print(f"model:    {model or '(无)'}  已嵌入: {done}  待嵌入: {pending}")
