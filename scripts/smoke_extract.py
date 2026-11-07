#!/usr/bin/env python3
"""事实提取管线端到端测试：mock chat 模型 -> facts.backfill -> 冲突/游标/溯源/向量。

不依赖真实 LLM；chat 返回带 ```json 围栏，顺带验证解析鲁棒性。
向量环节若本机有 embedding provider（Ollama）则真实走一遍 fact_vecs。
"""
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEST_DB = Path.home() / ".pawrecall" / "test_extract.db"
os.environ["PAWRECALL_FACTS_CONFLICT_MODE"] = "auto"  # 本测试覆盖旧版"急切推翻"语义
CANNED = json.dumps([
    {"kind": "decision", "subject": "缓存方案", "content": "决定：pawdemo 改用进程内 LRU 缓存，因部署环境不允许守护进程", "supersedes": [1]},
], ensure_ascii=False)


class MockChat(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        req = json.loads(body)
        assert req["messages"][0]["role"] == "system"
        out = {"choices": [{"message": {"content": f"```json\n{CANNED}\n```"}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    if TEST_DB.exists():
        TEST_DB.unlink()
    os.environ["PAWRECALL_DB"] = str(TEST_DB)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockChat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["PAWRECALL_FACTS_URL"] = f"http://127.0.0.1:{srv.server_port}/v1"
    os.environ["PAWRECALL_FACTS_MODEL"] = "mock-chat"
    os.environ.pop("PAWRECALL_FACTS_KEY", None)

    import index
    import facts

    con = sqlite3.connect(TEST_DB)
    index.ensure_db(con)
    facts.ensure_tables(con)
    con.executemany(
        "INSERT INTO msgs(source,project,session_id,file_path,role,ts,text) VALUES(?,?,?,?,?,?,?)",
        [
            ("claude", "/Users/chaucer/pawdemo", "s1", "/tmp/f1", "user", "2026-10-22T10:00:00", "缓存老是被 Redis 拖累，部署环境不允许起守护进程怎么办"),
            ("claude", "/Users/chaucer/pawdemo", "s1", "/tmp/f1", "assistant", "2026-10-22T10:01:00", "那就改用进程内 LRU 缓存，不需要守护进程。我们决定就这么办。"),
        ])
    con.execute(
        "INSERT INTO facts(source,project,session_id,ts,kind,subject,content,created_at) "
        "VALUES('claude','/Users/chaucer/pawdemo','s0','2026-09-01T09:00:00','decision','缓存方案','决定：pawdemo 缓存用 Redis 两实例','2026-09-01T09:00:00')")
    con.commit()

    ok = []

    def check(name, cond, detail=""):
        ok.append(bool(cond))
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))

    cfg = facts.provider_info()
    check("env 配置探测生效", cfg and cfg["model"] == "mock-chat")
    n = facts.backfill(limit_sessions=10)
    check("backfill 提取 1 条事实", n == 1, f"n={n}")

    row = con.execute(
        "SELECT source,project,session_id,ts,kind,content,superseded_by FROM facts WHERE id>1").fetchone()
    check("溯源字段完整", row and row[0] == "claude" and row[1] == "/Users/chaucer/pawdemo"
          and row[2] == "s1" and row[3] == "2026-10-22T10:01:00" and row[4] == "decision", str(row))
    check("冲突推翻生效（旧 Redis 事实被指向 LRU）",
          con.execute("SELECT superseded_by FROM facts WHERE id=1").fetchone()[0] == 2)
    cur = con.execute("SELECT last_msg_id FROM facts_cursor WHERE session_id='s1'").fetchone()
    max_id = con.execute("SELECT MAX(id) FROM msgs").fetchone()[0]
    check("游标推进到最新消息", cur and cur[0] == max_id, f"cursor={cur} max={max_id}")

    n2 = facts.backfill(limit_sessions=10)
    check("游标幂等（二次提取 0 条）", n2 == 0, f"n2={n2}")

    con.commit()
    con.close()
    hits = facts.facts_search("缓存 LRU", project="pawdemo")
    check("事实检索只见未被推翻项", len(hits) == 1 and "LRU" in hits[0]["snippet"])
    allrows = facts.list_facts(project="pawdemo", include_superseded=True)
    check("审计模式可见 2 条", len(allrows) == 2)

    vecs = sqlite3.connect(TEST_DB).execute("SELECT COUNT(*) FROM fact_vecs").fetchone()[0]
    check("事实向量化回填" + (f"（{vecs} 条）" if vecs else "（本机无 embedding provider，跳过属正常）"),
          vecs >= 1 or facts.embed.provider_info() is None)

    srv.shutdown()
    TEST_DB.unlink(missing_ok=True)
    n_fail = ok.count(False)
    print(f"\n{'ALL PASS' if n_fail == 0 else f'{n_fail} FAILED'}: {len(ok) - n_fail}/{len(ok)}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
