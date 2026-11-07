#!/usr/bin/env python3
"""语义层召回效果对比测试：exact vs semantic vs auto(hybrid)。

三组 case:
  A. 原 eval_cases(精确召回基线)
  B. 改述查询(exact 必挂,考语义):query 与原文无共同关键词
  C. 跨语言改述(中文 query 搜英文对话)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from index import search  # noqa: E402

# B 组:改述查询。expect = 期望命中的 session 子串 + 判定依据说明
PARAPHRASE_CASES = [
    # 真实历史:讨论过简历"定位行"排版、HR 6 秒扫简历(ef5bed91 / 08439d25 等 myResume 会话)
    {"query": "简历第一行应该怎么写才能让招聘方快速抓住重点", "expect": "myResume", "why": "原文讲'定位行/HR 6秒扫'"},
    # 真实历史:O2O 洗车业务投递定制简历(ef5bed91)
    {"query": "投递汽车后市场岗位时简历要怎么调整业务侧重点", "expect": "myResume", "why": "原文讲'O2O洗车业务定制版'"},
    # pawrecall 本身的设计讨论
    {"query": "做一个能搜历史聊天记录的工具要怎么设计", "expect": "", "why": "考能否搜到 pawrecall 相关会话"},
    # OpenViking 调研(当前会话之前的对话)
    {"query": "对比两个项目的架构差异", "expect": "", "why": "考最近项目对比讨论"},
    # HCIA 认证学习
    {"query": "人工智能职业认证考试准备", "expect": "0501f6cc", "why": "原文讲'HCIA-AI'"},
    # RAG 课程
    {"query": "检索增强生成课程的评价指标", "expect": "0501f6cc", "why": "原文讲'RAGAS'"},
]

A_CASES = json.load(open(Path(__file__).parent / "eval_cases.json", encoding="utf-8"))


def hit(results, expect):
    if not expect:
        return None  # 无客观判定,只展示 top1
    return any(expect in (r.get("file_path") or "") or expect in (r.get("session_id") or "") for r in results)


def run_group(name, cases, mode, limit=30):
    ok = shown = 0
    n_judge = 0
    for c in cases:
        t0 = time.time()
        results, note = search(c["query"], mode=mode, limit=limit)
        dt = (time.time() - t0) * 1000
        expect = c.get("expect") or c.get("expect_file_contains") or ""
        h = hit(results, expect)
        if h is not None:
            n_judge += 1
            ok += h
        mark = {True: "PASS", False: "FAIL", None: "    "}[h]
        top = results[0] if results else None
        print(f"{mark} [{mode:8s}] {c['query'][:28]:28s} {dt:5.0f}ms")
        if top:
            print(f"       top1: {top['source']}/{(top['project'] or '?')[-30:]} · {top['ts'][:10]} · {top['snippet'][:60]}")
        shown += 1
    return ok, n_judge


if __name__ == "__main__":
    modes = sys.argv[1:] or ["exact", "semantic", "auto"]
    print(f"向量覆盖: 见 embed --status(未覆盖的消息只可能被 exact 命中)\n")
    score = {}
    for mode in modes:
        print(f"—— A组 原eval基线({len(A_CASES)}个,含真实关键词)——")
        ok, n = run_group("A", A_CASES, mode)
        print(f"—— B组 改述查询({len(PARAPHRASE_CASES)}个,无共同关键词)——")
        ok2, n2 = run_group("B", PARAPHRASE_CASES, mode)
        score[mode] = (ok + ok2, n + n2)
        print(f"== {mode}: {ok + ok2}/{n + n2} ==\n")
    print("汇总(仅可判定 case):")
    for m, (ok, n) in score.items():
        print(f"  {m:9s} {ok}/{n}")
