#!/bin/sh
# pawrecall uninstaller — removes the installed files and registrations.
set -e
DEST="$HOME/.pawrecall"

launchctl bootout "gui/$(id -u)/com.pawrecall.index" 2>/dev/null || \
  launchctl unload "$HOME/Library/LaunchAgents/com.pawrecall.index.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.pawrecall.index.plist"

command -v claude >/dev/null 2>&1 && claude mcp remove --scope user pawrecall >/dev/null 2>&1 || true

rm -f "$HOME/.claude/skills/pawrecall" "$HOME/.agents/skills/pawrecall"
rm -rf "$DEST"

echo "pawrecall removed. Your original agent transcripts were never modified."
