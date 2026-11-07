#!/usr/bin/env python3
"""facts + budget 冒烟测试：在副本库上验证事实层全部新行为。

覆盖：
  1. 无 chat 配置时 facts 层禁用、search 行为不变
  2. 合成事实的 LIKE 检索 + auto 模式前置
  3. 冲突语义：superseded_by 排除 + include_superseded 审计
  4. fmt_results 字符预算封顶
  5. MCP server 的 paw_facts 工具协议
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["PAWRECALL_DB"] = str(Path.home() / ".pawrecall" / "test_history.db")
os.environ.pop("PAWRECALL_FACTS_URL", None)  # 确保默认禁用
os.environ.pop("PAWRECALL_FACTS_MODEL", None)

import index  # noqa: E402  PAWRECALL_DB 已设置
import facts  # noqa: E402

DB = Path(os.environ["PAWRECALL_DB"])
PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


con = sqlite3.connect(DB)
index.ensure_db(con)
facts.ensure_tables(con)
con.execute("DELETE FROM facts")
con.execute("DELETE FROM fact_vecs")
now = time.strftime("%Y-%m-%dT%H:%M:%S")

# -- 合成事实：两条缓存方案事实（后者推翻前者）+ 一条偏好
con.executemany(
    "INSERT INTO facts(source,project,session_id,ts,kind,subject,content,created_at) VALUES(?,?,?,?,?,?,?,?)",
    [
        ("claude", "/Users/chaucer/demo", "sess-cache-old", "2026-09-01T10:00:00", "decision",
         "缓存方案", "决定：pawdemo 缓存层用 Redis，部署两实例", now),
        ("claude", "/Users/chaucer/demo", "sess-cache-new", "2026-10-20T10:00:00", "decision",
         "缓存方案", "决定：pawdemo 缓存层改用进程内 LRU，因为部署环境不允许守护进程", now),
        ("codex", "/Users/chaucer/demo", "sess-pref", "2026-10-21T10:00:00", "preference",
         "代码风格", "偏好：提交信息用中文，正文解释为什么", now),
    ])
con.execute("UPDATE facts SET superseded_by=(SELECT id FROM facts WHERE session_id='sess-cache-new') "
            "WHERE session_id='sess-cache-old'")
con.commit()

# -- 1. 禁用状态
cfg = facts.provider_info()
check("默认无 chat 配置 -> 提取层禁用", cfg is None, f"cfg={cfg}")

# -- 2. facts_search 关键词
rows = facts.facts_search("缓存层 LRU", project="demo")
check("facts_search 命中缓存决定", len(rows) >= 1 and "LRU" in rows[0]["snippet"])
check("facts_search 默认排除被推翻事实", all(r["session_id"] != "sess-cache-old" for r in rows))

# -- 3. auto 模式事实前置
results, note = index.search("缓存层 改成什么了", mode="auto", project="demo", limit=5)
fact_first = results and results[0].get("fact")
check("auto 模式事实层前置", bool(fact_first), f"note={note}")
check("note 标注事实层", "事实层" in note)

# -- 4. fmt 渲染与预算
text = index.fmt_results(results, "缓存")
check("★事实 前缀渲染", "★事实" in text)
big = [{"source": "claude", "project": "p", "session_id": f"s{i}", "file_path": "f",
        "role": "user", "ts": "", "snippet": "x" * 500} for i in range(50)]
budget_text = index.fmt_results(big, "q")
check("字符预算封顶生效", len(budget_text) < index.MAX_OUTPUT_CHARS + 300 and "注入预算" in budget_text,
      f"len={len(budget_text)}")

# -- 5. list_facts 审计模式
sup = facts.list_facts(project="demo", include_superseded=True)
check("include_superseded 可见被推翻事实", any(r["session_id"] == "sess-cache-old" for r in sup))

# -- 6. facts CLI（禁用态 status 不崩）
r = subprocess.run([sys.executable, str(REPO / "index.py"), "facts", "status"],
                   capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO)})
check("CLI facts status 正常输出", r.returncode == 0 and "事实总数" in r.stdout, r.stderr[:200])

# -- 7. MCP server: paw_facts 协议
srv_input = "\n".join([
    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}}),
    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "paw_facts", "arguments": {"query": "缓存", "project": "demo"}}}),
]) + "\n"
r = subprocess.run([sys.executable, str(REPO / "server.py")], input=srv_input,
                   capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO)})
out_lines = [json.loads(l) for l in r.stdout.strip().splitlines() if l.strip()]
tools = next(o for o in out_lines if o.get("id") == 2)["result"]["tools"]
tool_names = [t["name"] for t in tools]
check("MCP tools/list 含 paw_facts", "paw_facts" in tool_names, str(tool_names))
resp = next(o for o in out_lines if o.get("id") == 3)
check("MCP paw_facts 返回事实", "LRU" in resp["result"]["content"][0]["text"])

con.close()
n_fail = sum(1 for _, ok in PASS if not ok)
print(f"\n{'ALL PASS' if n_fail == 0 else f'{n_fail} FAILED'}: {len(PASS) - n_fail}/{len(PASS)}")
sys.exit(1 if n_fail else 0)
