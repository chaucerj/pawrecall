#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook:
用户消息指涉过往对话时，向上下文注入"必须先检索"的提醒。
不匹配关键词时静默（零 token 开销）。
"""
import json
import sys

KEYWORDS = ("之前", "上次", "上回", "记得", "聊过", "讨论过", "历史对话", "前几天", "以前")

try:
    data = json.load(sys.stdin)
    prompt = str(data.get("prompt", ""))
except Exception:
    sys.exit(0)

if any(k in prompt for k in KEYWORDS):
    print(
        "[pawrecall] 该消息指涉过往对话。必须先调用 paw_search 工具"
        "（pawrecall server）检索本机全部历史会话，命中后可用 paw_read "
        "读取完整对话。禁止在未检索的情况下假设或编造之前的结论。"
    )

sys.exit(0)
