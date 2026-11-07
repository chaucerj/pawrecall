#!/bin/sh
# pawrecall uninstaller — removes the installed files and registrations.
set -e
DEST="$HOME/.pawrecall"

# 兼容历史两种 LaunchAgent 标签
for label in com.pawrecall.index com.chaucer.pawrecall-index; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
done

command -v claude >/dev/null 2>&1 && claude mcp remove --scope user pawrecall >/dev/null 2>&1 || true

rm -f "$HOME/.claude/skills/pawrecall" "$HOME/.agents/skills/pawrecall"
rm -rf "$DEST"

echo "pawrecall removed. Your original agent transcripts were never modified."
