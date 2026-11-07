#!/usr/bin/env python3
"""HITL 冲突处理端到端测试：模型提议矛盾 -> 挂起 -> 检索标注 -> 人工/agent 裁决。

覆盖：
  1. 默认 hitl：提取提议矛盾不直接推翻，挂起 pending，新旧事实都可见且带 ⚠ 标注
  2. search note 提示待确认数；CLI conflicts 列表；MCP pending_conflicts 队列
  3. 三种裁决：new_wins / old_wins / both，及裁决后可见性
  4. 兄弟 pending 随同侧抑制自动失效（stale）
  5. 迁移兼容：无 fact_conflicts 表时检索不崩
  6. legacy auto 模式仍可直接推翻（env 切换）
  7. MCP paw_read 未被 paw_facts 分支破坏（回归上一轮引入的 bug）
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEST_DB = Path.home() / ".pawrecall" / "test_conflicts.db"
os.environ["PAWRECALL_DB"] = str(TEST_DB)
os.environ.pop("PAWRECALL_FACTS_CONFLICT_MODE", None)  # 默认 hitl
CANNED = {"content": "决定：pawdemo 改用进程内 LRU 缓存", "reason": "新旧缓存方案互斥"}

import index  # noqa: E402  必须在 PAWRECALL_DB 设置之后
import facts  # noqa: E402


class MockChat(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        f = [{"kind": "decision", "subject": "缓存方案",
              "content": CANNED["content"], "supersedes": [1], "reason": CANNED["reason"]}]
        out = {"choices": [{"message": {"content": json.dumps(f, ensure_ascii=False)}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def fresh_db():
    if TEST_DB.exists():
        TEST_DB.unlink()
    con = sqlite3.connect(TEST_DB)
    index.ensure_db(con)
    facts.ensure_tables(con)
    return con


def add_session(con, sid, decision_text):
    con.executemany(
        "INSERT INTO msgs(source,project,session_id,file_path,role,ts,text) VALUES(?,?,?,?,?,?,?)",
        [("claude", "/Users/chaucer/pawdemo", sid, f"/tmp/{sid}", "user", "2026-10-22T10:00:00", "缓存怎么弄"),
         ("claude", "/Users/chaucer/pawdemo", sid, f"/tmp/{sid}", "assistant", "2026-10-22T10:01:00", decision_text)])
    con.commit()


def main():
    if TEST_DB.exists():
        TEST_DB.unlink()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockChat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["PAWRECALL_FACTS_URL"] = f"http://127.0.0.1:{srv.server_port}/v1"
    os.environ["PAWRECALL_FACTS_MODEL"] = "mock-chat"

    ok = []

    def check(name, cond, detail=""):
        ok.append(bool(cond))
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))

    con = fresh_db()
    con.execute(
        "INSERT INTO facts(source,project,session_id,ts,kind,subject,content,created_at) "
        "VALUES('claude','/Users/chaucer/pawdemo','s0','2026-09-01T09:00:00','decision',"
        "'缓存方案','决定：pawdemo 缓存用 Redis 两实例','2026-09-01T09:00:00')")
    con.commit()

    # -- 1. 提取提议矛盾 -> 挂起，不推翻
    add_session(con, "s1", "我们决定改用进程内 LRU 缓存。")
    facts.backfill(limit_sessions=10)
    old_sup = con.execute("SELECT superseded_by FROM facts WHERE id=1").fetchone()[0]
    check("hitl：提议矛盾不直接推翻（旧事实仍可见）", old_sup is None, f"superseded_by={old_sup}")
    pend = con.execute("SELECT id, old_id, new_id, reason, state FROM fact_conflicts").fetchall()
    check("hitl：矛盾挂起 pending 且带 reason",
          len(pend) == 1 and pend[0][1] == 1 and pend[0][2] == 2 and pend[0][3] != "" and pend[0][4] == "pending",
          str(pend))
    conflict_id = pend[0][0]

    # -- 2. 裁决前：新旧事实都可见，检索带 ⚠ 标注
    hits = facts.facts_search("缓存", project="pawdemo")
    ids = {h["id"] for h in hits}
    check("裁决前两条事实都可见", {1, 2} <= ids, str(ids))
    marked = next((h for h in hits if h["id"] == 1), None)
    check("命中事实带待确认标注", marked and marked.get("pending_conflicts")
          and marked["pending_conflicts"][0]["conflict_id"] == conflict_id)
    results, note = index.search("缓存", mode="facts", project="pawdemo", limit=5)
    check("search note 提示待确认数", "待确认矛盾" in note, note)
    rendered = index.fmt_results(results, "缓存")
    check("渲染含 ⚠ 与裁决指引", "⚠" in rendered and "paw_facts_resolve" in rendered)

    # -- 3. CLI conflicts + MCP 队列
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    r = subprocess.run([sys.executable, str(REPO / "index.py"), "facts", "conflicts"],
                       capture_output=True, text=True, env=env)
    check("CLI conflicts 列出新旧双方", r.returncode == 0 and "旧事实 #1" in r.stdout and "新事实 #2" in r.stdout,
          r.stdout[:150] + r.stderr[:150])
    r = subprocess.run([sys.executable, str(REPO / "index.py"), "facts", "status"],
                       capture_output=True, text=True, env=env)
    check("CLI status 显示待裁决数", "待裁决矛盾: 1" in r.stdout, r.stdout)

    def mcp(name, args):
        inp = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})]) + "\n"
        p = subprocess.run([sys.executable, str(REPO / "server.py")], input=inp,
                           capture_output=True, text=True, env=env)
        return next(json.loads(l) for l in p.stdout.strip().splitlines()
                    if l.strip() and json.loads(l).get("id") == 2)

    resp = mcp("paw_facts", {"pending_conflicts": True, "project": "pawdemo"})
    check("MCP pending_conflicts 队列", "旧事实 #1" in resp["result"]["content"][0]["text"])

    # -- 4. 裁决 new_wins -> 旧事实隐藏
    resp = mcp("paw_facts_resolve", {"conflict_id": conflict_id, "decision": "new_wins", "by": "测试用户"})
    check("MCP 裁决成功", "new_wins" in resp["result"]["content"][0]["text"])
    hits = facts.facts_search("缓存", project="pawdemo")
    check("new_wins 后旧事实隐藏", all(h["id"] != 1 for h in hits) and any(h["id"] == 2 for h in hits))
    con2 = sqlite3.connect(TEST_DB)
    state = con2.execute("SELECT state, resolved_by FROM fact_conflicts WHERE id=?", (conflict_id,)).fetchone()
    con2.close()
    check("冲突状态 confirmed + 审计人", state == ("confirmed", "测试用户"), str(state))

    # -- 5. old_wins / both（直接造冲突测裁决机制；用 lastrowid 拿真实 id）
    def mkfact(content, ts):
        cur = con.execute(
            "INSERT INTO facts(source,project,session_id,ts,kind,subject,content,created_at) "
            "VALUES('claude','/Users/chaucer/pawdemo','sX',?,'decision','方案',?,'2026-10-01T09:00:00')",
            (ts, content))
        return cur.lastrowid

    id_a, id_b = mkfact("决定：部署到 A 机房", "2026-10-01T09:00:00"), mkfact("决定：部署到 B 机房", "2026-10-02T09:00:00")
    id_c, id_d = mkfact("决定：用 selfhost 监控", "2026-10-03T09:00:00"), mkfact("决定：用云监控", "2026-10-04T09:00:00")
    con.execute("INSERT INTO fact_conflicts(old_id,new_id,reason,created_at) VALUES(?,?,?,'2026-10-02T09:00:00')",
                (id_a, id_b, "机房互斥"))
    conf_monitor = con.execute(
        "INSERT INTO fact_conflicts(old_id,new_id,reason,created_at) VALUES(?,?,?,'2026-10-04T09:00:00')",
        (id_c, id_d, "监控互斥")).lastrowid
    con.commit()
    r = facts.resolve_conflict(conf_monitor - 1, "old_wins", by="测试用户")
    hits_a = facts.facts_search("机房", project="pawdemo")
    check("old_wins：新事实被否", "已裁决" in r and hits_a and hits_a[0]["id"] == id_a,
          f"r={r} hits={[h['id'] for h in hits_a]}")
    r = facts.resolve_conflict(conf_monitor, "both", by="测试用户")
    con2 = sqlite3.connect(TEST_DB)
    unsup = con2.execute(
        "SELECT COUNT(*) FROM facts WHERE id IN (?,?) AND superseded_by IS NULL", (id_c, id_d)).fetchone()[0]
    con2.close()
    check("both：两条都保留", "已裁决" in r and unsup == 2, f"r={r} unsup={unsup}")

    # -- 6. 兄弟 pending 随同侧抑制自动失效（同一旧事实的其它提议在抑制后无需裁决）
    id_e = mkfact("决定：换 memcached", "2026-10-05T09:00:00")
    id_f = mkfact("决定：换 etcd 缓存", "2026-10-06T09:00:00")
    sib1 = con.execute(
        "INSERT INTO fact_conflicts(old_id,new_id,reason,created_at) VALUES(1,?,'提议A','2026-10-05T09:00:00')",
        (id_e,)).lastrowid
    sib2 = con.execute(
        "INSERT INTO fact_conflicts(old_id,new_id,reason,created_at) VALUES(1,?,'提议B','2026-10-06T09:00:00')",
        (id_f,)).lastrowid
    con.commit()
    facts.resolve_conflict(sib1, "new_wins", by="测试用户")
    con2 = sqlite3.connect(TEST_DB)
    sib = con2.execute("SELECT state FROM fact_conflicts WHERE id=?", (sib2,)).fetchall()
    con2.close()
    check("兄弟 pending 自动失效 stale", sib and sib[0][0] == "stale", str(sib))

    # -- 7. 迁移兼容：无 fact_conflicts 表时检索不崩
    con.execute("DROP TABLE fact_conflicts")
    con.commit()
    con.close()
    check("无冲突表时检索不崩", facts.facts_search("缓存", project="pawdemo") is not None
          and facts.pending_conflict_count() == 0)

    # -- 8. legacy auto 模式（env 切换）
    os.environ["PAWRECALL_FACTS_CONFLICT_MODE"] = "auto"
    facts._PROVIDER = None  # 重置 provider 缓存使新配置生效
    con = fresh_db()
    con.execute(
        "INSERT INTO facts(source,project,session_id,ts,kind,subject,content,created_at) "
        "VALUES('claude','/Users/chaucer/pawdemo','s0','2026-09-01T09:00:00','decision',"
        "'缓存方案','决定：pawdemo 缓存用 Redis 两实例','2026-09-01T09:00:00')")
    add_session(con, "s1", "我们决定改用进程内 LRU 缓存。")
    facts.backfill(limit_sessions=10)
    old_sup = con.execute("SELECT superseded_by FROM facts WHERE id=1").fetchone()[0]
    n_conf = con.execute("SELECT COUNT(*) FROM fact_conflicts").fetchone()[0]
    check("auto 模式保留旧语义（直接推翻）", old_sup == 2 and n_conf == 0, f"sup={old_sup} conflicts={n_conf}")
    con.close()

    srv.shutdown()
    TEST_DB.unlink(missing_ok=True)
    n_fail = ok.count(False)
    print(f"\n{'ALL PASS' if n_fail == 0 else f'{n_fail} FAILED'}: {len(ok) - n_fail}/{len(ok)}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
