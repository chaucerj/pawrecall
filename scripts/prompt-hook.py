#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook (deterministic trigger).

When the user's message refers to past conversations, inject a "search first"
reminder into context. Silent (zero tokens) when no keyword matches.

Install: see install.sh, or wire manually in ~/.claude/settings.json:
  hooks.UserPromptSubmit -> python3 /path/to/prompt-hook.py
"""
import json
import sys

KEYWORDS = ("之前", "上次", "上回", "记得", "聊过", "讨论过", "历史对话", "前几天", "以前",
            "we discussed", "i remember", "last time", "previously", "前回", "以前話した")

try:
    data = json.load(sys.stdin)
    prompt = str(data.get("prompt", ""))
except Exception:
    sys.exit(0)

low = prompt.lower()
if any(k in prompt for k in KEYWORDS) or any(k in low for k in KEYWORDS):
    print(
        "[pawrecall] This message refers to past conversations. You MUST call "
        "paw_search (pawrecall server) first to search the local history index — "
        "prefer passing the current project as 'project'. Use paw_read for the "
        "full thread. Never invent or assume prior conclusions without searching."
    )

sys.exit(0)
