#!/usr/bin/env python3
"""pawrecall evaluation harness.

Measures three things against a built index:

  A. Indexing fidelity  — sample verbatim messages from the RAW transcripts and
                          verify each one exists in the DB (exact string match).
  B. Query recall       — cut random substrings out of indexed messages, run the
                          real search path, and verify they are findable. Also
                          probes escaping edge cases (%, _, ', non-ASCII).
  C. Labeled precision@3 — optional. Provide ~/.pawrecall/eval_cases.json:
                          [{"query": "...", "expect_file_contains": "session-id-or-path"}]
                          Each case passes if the expected file appears in the top 3.
                          Keep this file local: session ids are semi-sensitive.

Usage:
  python3 scripts/eval.py [--db PATH] [--samples 60] [--seed 7]
"""

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude" / "projects"
CODEX_DIR = HOME / ".codex" / "sessions"
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"
CASES_FILE = HOME / ".pawrecall" / "eval_cases.json"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pawrecall import search, clean_text  # noqa: E402  (use the real search path)


def texts_from_claude(fp):
    out = []
    for line in open(fp, encoding="utf-8", errors="replace"):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") not in ("user", "assistant") or obj.get("isMeta"):
            continue
        msg = obj.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out += [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
    return [t for t in out if t and len(t) >= 40]


def texts_from_codex(fp):
    out = []
    for line in open(fp, encoding="utf-8", errors="replace"):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        p = obj.get("payload") or {}
        if obj.get("type") == "response_item" and p.get("type") == "message":
            out += [c.get("text", "") for c in (p.get("content") or [])
                    if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")]
    return [t for t in out if t and len(t) >= 40]


def texts_from_opencode(n, rnd):
    if not OPENCODE_DB.exists():
        return []
    con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT json_extract(p.data,'$.text') FROM part p "
        "WHERE json_extract(p.data,'$.type')='text' ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
    con.close()
    return [r[0] for r in rows if r[0] and len(r[0]) >= 40]


def sample_raw_texts(source, n, rnd):
    if source == "claude":
        files = list(CLAUDE_DIR.glob("*/*.jsonl"))
        pool = []
        for fp in rnd.sample(files, min(len(files), max(8, n // 8))):
            pool += texts_from_claude(fp)
    elif source == "codex":
        files = list(CODEX_DIR.rglob("*.jsonl"))
        pool = []
        for fp in rnd.sample(files, min(len(files), max(8, n // 8))):
            pool += texts_from_codex(fp)
    else:
        pool = texts_from_opencode(n, rnd)
    return rnd.sample(pool, min(n, len(pool))) if pool else []


def exists_exact(db, text):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    hit = con.execute("SELECT 1 FROM msgs WHERE text=? LIMIT 1", (text,)).fetchone()
    con.close()
    return hit is not None


def test_fidelity(db, samples, seed):
    print(f"\n── A. 索引保真度（原文逐字比对，每源 {samples} 条）──")
    rnd = random.Random(seed)
    ok_all, total_all = 0, 0
    for src in ("claude", "codex", "opencode"):
        texts = sample_raw_texts(src, samples, rnd)
        if not texts:
            print(f"  {src:9s} 无原始样本（未安装或为空），跳过")
            continue
        ok = lost = bydesign = 0
        for t in texts:
            t2 = clean_text(t)  # apply the same by-design filtering as the indexer
            if not t2:
                bydesign += 1
            elif exists_exact(db, t2):
                ok += 1
            else:
                lost += 1
        ok_all += ok
        total_all += ok + lost
        mark = "✓" if lost == 0 else "✗"
        print(f"  {src:9s} 命中 {ok}  真丢失 {lost} {mark}  设计过滤 {bydesign}")
    rate = ok_all / total_all * 100 if total_all else 0
    print(f"  真实保真率: {rate:.1f}%  （设计过滤 = command 标记、system-reminder 等不应入库的内容）")
    return rate


def test_query_recall(db, n, seed):
    print(f"\n── B. 查询召回（随机子串 {n} 次 + 边界字符用例）──")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rnd = random.Random(seed + 1)
    ids = [r[0] for r in con.execute("SELECT id FROM msgs").fetchall()]
    rows = [con.execute("SELECT file_path, text FROM msgs WHERE id=?", (i,)).fetchone()
            for i in rnd.sample(ids, min(n, len(ids)))]
    con.close()
    hit = strict = 0
    for fpath, text in rows:
        if len(text) < 12:
            continue
        i = rnd.randint(0, len(text) - 8)
        q = text[i:i + rnd.randint(4, 14)]
        res = search(db, q, limit=20)
        if res:
            hit += 1
            if any(r["file_path"] == fpath for r in res):
                strict += 1
    print(f"  随机子串可寻回: {hit}/{len(rows)} ({hit / len(rows) * 100:.1f}%)")
    print(f"  原文件出现在结果中: {strict}/{len(rows)} ({strict / len(rows) * 100:.1f}%)")

    edges = ["100%", "a_b", "it's", "——", "『』", "xkcd-quantum-unicorn-42"]
    print("  边界用例:")
    for q in edges:
        res = search(db, q, limit=5)
        status = f"{len(res)} 条" if res else "0 条"
        print(f"    {q!r:28s} → {status}")
    return hit / len(rows) * 100 if rows else 0


def test_labeled(db):
    print("\n── C. 标注 precision@3（人工核对的真实会话用例）──")
    if not CASES_FILE.exists():
        print("  未找到 ~/.pawrecall/eval_cases.json，跳过（格式见 scripts/eval.py 头注释）")
        return None
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    passed = 0
    for c in cases:
        res = search(db, c["query"], limit=3)
        top3_files = [r["file_path"] for r in res]
        ok = any(c["expect_file_contains"] in f for f in top3_files)
        passed += ok
        print(f"  {'✓' if ok else '✗'}  {c['query']!r:24s} → 期望 {c['expect_file_contains'][:24]}… "
              f"实际 top3: {[f.split('/')[-1][:20] for f in top3_files] or '空'}")
    print(f"  precision@3: {passed}/{len(cases)} ({passed / len(cases) * 100:.0f}%)")
    return passed / len(cases)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=HOME / ".pawrecall" / "history.db")
    ap.add_argument("--samples", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if not args.db.exists():
        sys.exit(f"索引不存在: {args.db} — 先运行 python3 pawrecall.py index")
    print(f"pawrecall 评测 · db={args.db} · samples={args.samples} · seed={args.seed}")
    a = test_fidelity(args.db, args.samples, args.seed)
    b = test_query_recall(args.db, args.samples, args.seed)
    c = test_labeled(args.db)
    print("\n════════ 汇总 ════════")
    print(f"  索引保真率      : {a:.1f}%")
    print(f"  随机子串召回率  : {b:.1f}%")
    print(f"  标注 precision@3: {f'{c*100:.0f}%' if c is not None else '无本地用例'}")
    print("  注: 随机子串召回在纯子串检索架构下理论上应为 100%，此测试验证整条管线")
    print("      （解析→入库→LIKE 转义→排序）无静默丢失。语义相关性需人工标注。")


if __name__ == "__main__":
    main()
