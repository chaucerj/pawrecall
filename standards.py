#!/usr/bin/env python3
"""paw-standards: harvest improvement-type user feedback from the pawrecall index.

Scans user messages recorded in ~/.pawrecall/history.db for one workspace
(project path scope), keeping only "improvement-type" utterances — 纠正、要求、
偏好、流程约束 ("以后别用X"、"下次记得Y"、"going forward use Z") — so an agent
can distill them into a workspace-local design/development standards file
(the paw-standards skill writes an AGENTS.md managed block from this output).

Heuristic recall, deterministic: no model calls, stdlib only. Precision is the
caller's (the distilling agent's) job — this script only nominates candidates.

Usage:
  python3 standards.py harvest /path/to/workspace [--since 90] [--limit 200] [--json]
  python3 standards.py status
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
DB_PATH = Path(os.environ.get("PAWRECALL_DB") or AI_DIR / "history.db")

MAX_CHARS = int(os.environ.get("PAWRECALL_MAX_CHARS", "12000"))

# 与 index.py 同源的出现噪音前缀：命令回显、系统注入、中断标记一律跳过；
# 另外过滤 Claude Code 的会话续传摘要（"This session is being continued..."，
# 是 agent 自己写的总结挂在 user role 下，不是用户原声，蒸馏规范时全是噪音）
SKIP_PREFIXES = ("<command", "<local-command", "Caveat:", "[Request interrupted",
                 "<system-reminder>", "No response requested", "[Image",
                 "This session is being continued", "<task-notification",
                 "<task-output", "<system>", "# AGENTS.md instructions")

MIN_LEN = 8

# ---------------- 改进型信号（启发式，宁可召回、由蒸馏方把关精度） ----------------

# 强信号：明确的"今后怎么做"指令
ZH_DIRECTIVE = [
    "以后", "下次", "下回", "别再", "不要再", "再也不要", "以后都", "以后统一",
    "以后一律", "以后默认", "以后就用", "以后改为", "以后避免", "以后就按",
    "统一用", "固定用", "一律用", "记住", "记得要", "下次注意", "下不为例",
    "应该改成", "必须改成", "规范是", "以后每次",
]
EN_DIRECTIVE = [
    "going forward", "next time", "from now on", "always use", "never use",
    "don't use", "do not use", "stop using", "make sure", "remember to",
    "keep in mind", "instead of", "use ... instead", "going to require",
    "should always", "must always", "from here on",
]
# 修正信号：对已发生行为的纠正（隐含要求）
ZH_CORRECTION = [
    "我说过", "上次就说过", "已经说过", "怎么又", "又错", "还是不对", "还是错了",
    "搞错了", "弄错了", "不对，", "不对,", "为什么用", "为什么要用", "为什么不",
    "怎么没有", "别用", "不用这个", "换成", "改成",
]
EN_CORRECTION = [
    "i already told", "i told you", "why are you", "why not use", "why did you",
    "you should", "you shouldn't", "that's wrong", "this is wrong",
    "not what i asked", "i asked for", "wrong approach", "wrong way",
]


def _has_any(text, pats):
    low = text.lower()
    return next((p for p in pats if p in low or p in text), None)


def is_improvement_signal(text):
    """返回命中的信号标签，无命中返回 None。"""
    tag = _has_any(text, ZH_DIRECTIVE) or _has_any(text, EN_DIRECTIVE)
    if tag:
        return f"指令:{tag}" if tag in ZH_DIRECTIVE else f"directive:{tag}"
    tag = _has_any(text, ZH_CORRECTION) or _has_any(text, EN_CORRECTION)
    if tag:
        return f"纠正:{tag}" if tag in ZH_CORRECTION else f"correction:{tag}"
    return None


# ---------------- workspace 作用域 ----------------

def _norm(p):
    # ZCode 会在会话中途 workspace 改名时给 project 追加 " (was <旧路径>)" 装饰，匹配前剥掉
    return re.sub(r"\s*\(was .*\)$", "", str(p).rstrip("/").rstrip("\\"))


def project_matches(project, workspace):
    """会话归入 workspace：完全相等、workspace 是会话目录的子目录、
    或会话目录是 workspace 的子目录（monorepo 从根启动、从子目录提问都算）。
    家目录根启动的会话（project == HOME）不归属任何特定 workspace——
    它们是临时/全局会话，放进每个 workspace 的召回里只会造成污染。"""
    if not project or not workspace:
        return False
    p, w = _norm(project), _norm(workspace)
    if p == HOME.as_posix() and w != p:
        return False
    return p == w or p.startswith(w + "/") or w.startswith(p + "/")


def load_candidates(workspace, since_days=None, limit=200):
    if not DB_PATH.exists():
        sys.exit(f"索引不存在：{DB_PATH}（先运行 python3 ~/.pawrecall/index.py index）")
    since = None
    if since_days:
        since = (datetime.datetime.now() - datetime.timedelta(days=since_days)).isoformat(timespec="seconds")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    sql = ("SELECT source, project, session_id, ts, text FROM msgs "
           "WHERE role='user' AND project IS NOT NULL AND text != ''")
    if since:
        sql += " AND ts >= ?"
        rows = con.execute(sql + " ORDER BY ts DESC", (since,))
    else:
        rows = con.execute(sql + " ORDER BY ts DESC")
    seen_texts = set()
    out = []
    for src, proj, sid, ts, text in rows:
        if not project_matches(proj, workspace):
            continue
        text = text.strip()
        if len(text) < MIN_LEN or any(text.startswith(p) for p in SKIP_PREFIXES):
            continue
        if text in seen_texts:  # 不同会话重复说同一句话只留一条
            continue
        seen_texts.add(text)
        tag = is_improvement_signal(text)
        if not tag:
            continue
        out.append({"source": src, "project": proj, "session_id": sid,
                    "ts": ts, "signal": tag, "text": text[:600]})
        if len(out) >= limit:
            break
    con.close()
    return out


def fmt_candidates(ws, items):
    proj = _norm(ws)
    proj_short = proj.replace(HOME.as_posix(), "~")
    head = (f"# paw-standards 候选（workspace: {proj_short}，改进型用户反馈 {len(items)} 条）\n\n"
            "以下是启发式召回的候选，需要人工/agent 蒸馏成规范；引用来源时带上 source · session_id · 日期。\n")
    lines = []
    for it in items:
        ts = (it["ts"] or "")[:10]
        sid_short = (it["session_id"] or "")[:12]
        lines.append(f"- [{ts}] ({it['source']} · {sid_short} · {it['signal']})\n  {it['text']}")
    body = "\n".join(lines) if lines else "（0 条候选：该 workspace 的历史会话里没有召回改进型反馈；可放宽 --since 或检查作用域路径）"
    return head + "\n" + body


def status():
    if not DB_PATH.exists():
        print(f"索引不存在：{DB_PATH}")
        return
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    total = con.execute("SELECT COUNT(*) FROM msgs WHERE role='user' AND project IS NOT NULL").fetchone()[0]
    projects = con.execute("SELECT COUNT(DISTINCT project) FROM msgs WHERE role='user' AND project IS NOT NULL").fetchone()[0]
    con.close()
    print(f"库: {DB_PATH}")
    print(f"可扫描用户消息（带项目归属）: {total} 条，覆盖 {projects} 个 workspace/项目")
    print("改进型信号词表: 指令类 %d 中文 / %d 英文，纠正类 %d 中文 / %d 英文" % (
        len(ZH_DIRECTIVE), len(EN_DIRECTIVE), len(ZH_CORRECTION), len(EN_CORRECTION)))


def main():
    ap = argparse.ArgumentParser(description="paw-standards：从 pawrecall 索引召回改进型用户反馈候选")
    sub = ap.add_subparsers(dest="cmd", required=True)
    hp = sub.add_parser("harvest", help="按 workspace 召回改进型反馈候选")
    hp.add_argument("workspace", help="workspace 路径（会话启动路径按前缀/父子关系归入）")
    hp.add_argument("--since", type=int, default=None, help="只看最近 N 天")
    hp.add_argument("--limit", type=int, default=200, help="最多返回条数（默认 200）")
    hp.add_argument("--json", action="store_true", help="输出 JSON 数组（程序化消费）")
    sub.add_parser("status", help="查看索引规模与信号词表")
    args = ap.parse_args()
    if args.cmd == "status":
        status()
        return
    items = load_candidates(args.workspace, args.since, args.limit)
    if args.json:
        print(json.dumps({"workspace": _norm(args.workspace), "count": len(items), "items": items},
                         ensure_ascii=False, indent=2))
    else:
        out = fmt_candidates(args.workspace, items)
        if len(out) > MAX_CHARS:
            out = out[:MAX_CHARS] + "\n…（超出字符预算被截断，可用 --limit / --since 收窄）"
        print(out)


if __name__ == "__main__":
    main()
