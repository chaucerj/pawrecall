#!/usr/bin/env python3
"""LoCoMo 公开基准 harness — 用公开长对话数据集度量 pawrecall 的检索质量.

数据集: LoCoMo (snap-research/locomo) — 10 段超长多会话对话 + 5 类 QA
  (1 单跳 / 2 多跳 / 3 时间推理 / 4 开放域 / 5 对抗性"未提及")

指标:
  - evidence recall@k: 检索结果里是否出现标注证据（文本重叠判定，不需要 LLM）
  - MRR: 首条证据命中的平均倒数名次
  - (可选 --judge) answer accuracy: 用 chat 模型判回答是否与标准答案语义一致
    —— judge 才能体现事实层（蒸馏改写）对答题质量的增益；纯证据召回只看原文命中

用法:
  python3 bench/bench_locomo.py --data locomo10.json                 # 本地数据文件
  python3 bench/bench_locomo.py --hf                                 # 尝试从 HuggingFace 下载
  python3 bench/bench_locomo.py --data locomo10.json --mode semantic # 指定检索模式
  python3 bench/bench_locomo.py --data locomo10.json --judge --limit 50
  python3 bench/bench_locomo.py --selftest                           # 内置合成数据自检（离线可跑）

 bench 全程使用独立副本库（默认 ~/.pawrecall/bench_locomo.db），绝不碰生产库；
 换 --mode 重跑前建议 --rebuild 重建副本库。
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH_DB = Path.home() / ".pawrecall" / "bench_locomo.db"
sys.path.insert(0, str(REPO))  # ingest/评测要 import index
HF_URLS = [
    "https://huggingface.co/datasets/snap-research/locomo/resolve/main/data/locomo10.json",
    "https://huggingface.co/datasets/snap-research/locomo/resolve/main/locomo10.json",
    "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
]

SCOPED_CATS = (1, 2, 3, 4)   # 默认评测类别（5 = 对抗性，单独开关）
TOP_K = 5


def set_db(path):
    os.environ["PAWRECALL_DB"] = str(path)  # 必须在 import index 之前设置


# ---------------- 数据加载 ----------------

def load_locomo(path=None, use_hf=False):
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if use_hf:
        for url in HF_URLS:
            try:
                print(f"下载 {url} …")
                with urllib.request.urlopen(url, timeout=120) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                print(f"  失败：{e}")
        sys.exit("下载失败。请手动下载 locomo10.json 后用 --data 指定。")
    sys.exit("需要 --data <locomo.json> 或 --hf。")


def conversations(data):
    """兼容几种顶层包装：list / {"conversations": [...]} / 单个 dict。"""
    if isinstance(data, list):
        return data
    for k in ("conversations", "data"):
        if isinstance(data, dict) and isinstance(data.get(k), list):
            return data[k]
    if isinstance(data, dict) and "conversation" in data:
        return [data]
    sys.exit("无法识别的 LoCoMo 顶层结构（期望 conversation 列表）。")


def turns_of(conv):
    """兼容两种形态：LoCoMo v1 的 {session_N: [turn...]} 字典展平为有序列表；或平铺列表。"""
    raw = conv.get("conversation") or conv.get("turns") or {}
    if isinstance(raw, list):
        return raw

    def skey(k):
        m = re.match(r"session_(\d+)$", k or "")
        return int(m.group(1)) if m else -1

    turns = []
    for k in sorted(raw, key=skey):
        if skey(k) < 0 or not isinstance(raw[k], list):
            continue  # speaker_a / session_N_date_time 等元数据键
        for t in raw[k]:
            if isinstance(t, dict):
                t = dict(t)
                t["_session"] = skey(k)
                turns.append(t)
    return turns


def qas_of(conv):
    return conv.get("qa") or conv.get("qas") or conv.get("questions") or []


def turn_text(t):
    if not isinstance(t, dict):
        return str(t)
    return t.get("text") or t.get("content") or t.get("utterance") or ""


def turn_speaker(t):
    return (t.get("speaker") or t.get("speaker_role") or t.get("role") or "") if isinstance(t, dict) else ""


def turn_session(t, i):
    if isinstance(t, dict) and t.get("_session") is not None:
        return f"s{t.get('_session')}"
    if isinstance(t, dict) and t.get("session_id") is not None:
        return f"s{t.get('session_id')}"
    return f"s{i // 10}"  # 每 10 轮近似一个会话，仅供聚合展示


def evidence_texts(qa, turns):
    """把 LoCoMo 的 evidence 标注归一成文本列表。

    LoCoMo v1 的 evidence 是 dia_id 引用码（如 "D1:3"），按 dia_id 映射回原文；
    其他变体（原文片段 / 下标 / dict）一并兜住。"""
    dia = {}
    for t in turns:
        if isinstance(t, dict) and t.get("dia_id"):
            dia[t["dia_id"]] = turn_text(t)
    ev = qa.get("evidence") or qa.get("evidences") or []
    out = []
    for item in ev:
        if isinstance(item, str):
            out.append(dia.get(item, item))
        elif isinstance(item, dict):
            for k in ("text", "evidence", "answer"):
                v = item.get(k)
                if isinstance(v, str):
                    out.append(dia.get(v, v))
                elif isinstance(v, list):
                    out.extend(dia.get(x, x) if isinstance(x, str) else "" for x in v)
        elif isinstance(item, (list, tuple)):
            for sub in item:
                if isinstance(sub, str):
                    out.append(dia.get(sub, sub))
                elif isinstance(sub, (list, tuple)):
                    out.extend(dia.get(x, x) if isinstance(x, str) else "" for x in sub)
                elif isinstance(sub, int) and 0 <= sub < len(turns):
                    txt = turn_text(turns[sub])
                    if txt:
                        out.append(txt)
        elif isinstance(item, int) and 0 <= item < len(turns):
            txt = turn_text(turns[item])
            if txt:
                out.append(txt)
    return [t for t in out if t and t.strip() and not re.fullmatch(r"D\d+:\d+", t.strip())]


# ---------------- 文本匹配（证据判定，无 LLM） ----------------

_NORM_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def norm(s):
    return _NORM_RE.sub("", (s or "").lower())


def bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def is_hit(retrieved, evidences):
    """归一化后双向包含，或字符二元组 Dice >= 0.5。"""
    r = norm(retrieved)
    if not r:
        return False
    rb = bigrams(r)
    for ev in evidences:
        e = norm(ev)
        if not e:
            continue
        if e in r or r in e:
            return True
        eb = bigrams(e)
        dice = 2 * len(rb & eb) / max(len(rb) + len(eb), 1)
        if dice >= 0.5:
            return True
    return False


# ---------------- 数据入库 ----------------

def ingest(data, db_path, rebuild=False):
    import sqlite3
    if rebuild and db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    have = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='msgs'").fetchone()[0]
    con.close()
    if have and not rebuild:
        print(f"副本库已存在 {db_path}（--rebuild 可重建）")
        return
    from index import ensure_db, insert_rows
    con = sqlite3.connect(db_path)
    ensure_db(con)
    rows = []
    for conv in conversations(data):
        cid = conv.get("conversation_id", conv.get("sample_id", "x"))
        project = f"locomo-{cid}"
        for i, t in enumerate(turns_of(conv)):
            txt = turn_text(t)
            if not txt or not txt.strip():
                continue
            rows.append(("locomo", project, f"locomo-{cid}-{turn_session(t, i)}",
                         f"locomo-{cid}", turn_speaker(t), str(t.get("ts", "") or ""),
                         txt))
    insert_rows(con, rows)
    con.commit()
    con.close()
    print(f"已入库 {len(rows)} 条消息 -> {db_path}")


# ---------------- 评测 ----------------

def run_retrieval_eval(data, mode="auto", k=TOP_K, limit=None, cats=SCOPED_CATS):
    from index import search
    cases = []
    for conv in conversations(data):
        cid = conv.get("conversation_id", conv.get("sample_id", "x"))
        project = f"locomo-{cid}"
        turns = turns_of(conv)
        for qa in qas_of(conv):
            cat = qa.get("category")
            if cats is not None and cat not in cats:
                continue
            evs = evidence_texts(qa, turns)
            if not evs or not qa.get("question"):
                continue
            cases.append({"project": project, "question": qa["question"],
                          "answer": qa.get("answer", ""), "category": cat, "evidence": evs})
    if limit:
        cases = cases[:int(limit)]
    print(f"评测 {len(cases)} 题（类别 {cats or '全部'}，mode={mode}，k={k}）")
    hits, mrr, per_cat = 0, 0.0, {}
    for i, c in enumerate(cases, 1):
        results, note = search(c["question"], project=c["project"], mode=mode, limit=k)
        rank = None
        for j, r in enumerate(results, 1):
            if is_hit(r.get("snippet", ""), c["evidence"]):
                rank = j
                break
        ok = rank is not None
        hits += ok
        mrr += (1.0 / rank) if ok else 0.0
        st = per_cat.setdefault(c["category"], [0, 0])
        st[0] += ok
        st[1] += 1
        if not ok or i % 20 == 0:
            mark = "HIT " if ok else f"miss(rank={rank})"
            print(f"  [{i}/{len(cases)}] {mark} cat{c['category']} {c['question'][:40]}")
    recall = hits / max(len(cases), 1)
    print(f"\n== evidence recall@{k}: {recall:.1%} ({hits}/{len(cases)})  MRR: {mrr / max(len(cases), 1):.3f}")
    for cat in sorted(per_cat):
        h, n = per_cat[cat]
        print(f"   category {cat}: {h}/{n} = {h / max(n, 1):.1%}")
    return {"recall": recall, "mrr": mrr / max(len(cases), 1), "n": len(cases),
            "per_category": {c: per_cat[c] for c in sorted(per_cat)}}


def run_judge(data, mode="auto", limit=None, cats=SCOPED_CATS, k=3):
    """用 chat 模型判「检索内容能否答对题」——事实层的蒸馏增益在这里才可见。"""
    from index import search
    from facts import provider_info, chat
    cfg = provider_info()
    if not cfg:
        sys.exit("judge 需要 chat 模型：配置 facts.json（见 facts.json.example）或 Ollama 跑一个 instruct 模型。")
    cases = []
    for conv in conversations(data):
        cid = conv.get("conversation_id", conv.get("sample_id", "x"))
        project = f"locomo-{cid}"
        for qa in qas_of(conv):
            if qa.get("category") in cats and qa.get("question") and qa.get("answer"):
                cases.append({"project": project, "question": qa["question"],
                              "answer": qa["answer"], "category": qa.get("category")})
    if limit:
        cases = cases[:int(limit)]
    print(f"judge 评测 {len(cases)} 题（{cfg['provider']}/{cfg['model']}，mode={mode}）")
    correct, total = 0, 0
    for i, c in enumerate(cases, 1):
        results, _ = search(c["question"], project=c["project"], mode=mode, limit=k)
        ctx = "\n".join(f"- {r.get('snippet', '')[:300]}" for r in results)[:1500] or "（无检索结果）"
        prompt = (f"仅依据以下检索内容判断：对问题「{c['question']}」，检索内容能否支持标准答案「{c['answer']}」？\n"
                  f"检索内容：\n{ctx}\n只回答 是 或 否。")
        try:
            out = chat([{"role": "user", "content": prompt}], cfg)
            ok = any(w in out for w in ("是", "yes", "Yes", "true", "TRUE"))
        except Exception as e:
            print(f"  judge 失败：{e}")
            break
        correct += ok
        total += 1
        if i % 10 == 0:
            print(f"  [{i}/{len(cases)}] 累计 accuracy {correct}/{total}")
    print(f"\n== answer accuracy: {correct}/{max(total, 1)} = {correct / max(total, 1):.1%}")
    return {"accuracy": correct / max(total, 1), "n": total}


# ---------------- 自检（离线合成数据，验证 harness 本身） ----------------

def selftest():
    data = [
        {"conversation_id": 1,
         "conversation": [
             {"speaker": "Alice", "text": "我决定把缓存层从 Redis 换成内存 LRU，因为部署环境不允许起守护进程。", "ts": 1},
             {"speaker": "Bob", "text": "好，那重试策略就定为指数退避，最多三次。", "ts": 2},
             {"speaker": "Alice", "text": "另外以后我们所有服务都用 UTC 时间戳。", "ts": 3},
         ],
         "qa": [
             {"question": "缓存层 内存 LRU", "answer": "内存 LRU，因为部署环境不允许起守护进程",
              "category": 1, "evidence": ["我决定把缓存层从 Redis 换成内存 LRU，因为部署环境不允许起守护进程。"]},
             {"question": "重试策略 指数退避", "answer": "指数退避，最多三次",
              "category": 1, "evidence": ["重试策略就定为指数退避，最多三次"]},
             {"question": "UTC 时间戳", "answer": "UTC", "category": 2,
              "evidence": ["以后我们所有服务都用 UTC 时间戳"]},
             {"question": "会议定在几点？", "answer": "对话中未提及", "category": 5,
              "evidence": [], "adversarial_answer": "对话中未提及"},
         ]},
    ]
    set_db(Path.home() / ".pawrecall" / "bench_selftest.db")
    p = Path(os.environ["PAWRECALL_DB"])
    if p.exists():
        p.unlink()
    ingest(data, p, rebuild=True)
    res = run_retrieval_eval(data, mode="exact", k=TOP_K)
    assert res["n"] == 3, f"应评测 3 题（cat5 无证据自动跳过），实际 {res['n']}"
    assert res["recall"] == 1.0, f"合成数据应 100% 召回，实际 {res['recall']}"
    print("\nSELFTEST PASS — harness 数据链路（入库→检索→证据判定→指标）工作正常")


def main():
    ap = argparse.ArgumentParser(description="LoCoMo benchmark for pawrecall")
    ap.add_argument("--data", help="locomo json 文件路径")
    ap.add_argument("--hf", action="store_true", help="从 HuggingFace 下载")
    ap.add_argument("--db", default=str(BENCH_DB), help="基准副本库路径")
    ap.add_argument("--rebuild", action="store_true", help="重建副本库")
    ap.add_argument("--mode", choices=["auto", "exact", "semantic", "facts"], default="auto")
    ap.add_argument("--k", type=int, default=TOP_K)
    ap.add_argument("--limit", type=int, default=None, help="只评测前 N 题")
    ap.add_argument("--cats", default="1,2,3,4", help="评测类别，逗号分隔；all=全部")
    ap.add_argument("--judge", action="store_true", help="附加 LLM 判答 accuracy")
    ap.add_argument("--include-adv", action="store_true", help="judge 时含 category 5 对抗题")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        return
    set_db(Path(a.db))
    data = load_locomo(a.data, a.hf)
    ingest(data, Path(a.db), rebuild=a.rebuild)
    cats = None if a.cats == "all" else tuple(int(x) for x in a.cats.split(",") if x.strip())
    res = run_retrieval_eval(data, mode=a.mode, k=a.k, limit=a.limit, cats=cats)
    if a.judge:
        jcats = None if a.include_adv else SCOPED_CATS
        res["judge"] = run_judge(data, mode=a.mode, limit=a.limit, cats=jcats, k=a.k)
    out = Path(a.db).with_suffix(".results.json")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    main()
