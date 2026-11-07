#!/usr/bin/env python3
"""pawrecall 检索回归评测。

用法:
  python3 eval.py                 # 全部模式
  python3 eval.py --mode exact    # 仅精确（验证原行为无回归）

case 格式 (eval_cases.json):
  {"query": "...", "expect_file_contains": "session_id 或 file_path 子串"}
命中判定：结果里任一条的 session_id/file_path 包含期望子串。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from index import search  # noqa: E402


def run(mode):
    base = Path(__file__).parent
    cases_path = base / "eval_cases.json"
    if not cases_path.exists():  # 仓库里只有样例；部署目录用真实用例
        cases_path = base / "eval_cases.example.json"
    cases = json.load(open(cases_path, encoding="utf-8"))
    ok = 0
    for c in cases:
        results, note = search(c["query"], mode=mode, limit=50)
        hit = any(
            c["expect_file_contains"] in (r.get("file_path") or "") or c["expect_file_contains"] in (r.get("session_id") or "")
            for r in results
        )
        ok += hit
        print(f"{'PASS' if hit else 'FAIL'} [{mode}] {c['query']}  ({note})")
    print(f"{mode}: {ok}/{len(cases)} passed\n")
    return ok, len(cases)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "exact", "semantic", "all"], default="all")
    a = ap.parse_args()
    modes = ["exact", "semantic", "auto"] if a.mode == "all" else [a.mode]
    total_ok = total = 0
    for m in modes:
        ok, n = run(m)
        total_ok += ok
        total += n
    print(f"TOTAL: {total_ok}/{total}")
