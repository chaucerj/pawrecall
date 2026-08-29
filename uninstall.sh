#!/bin/sh
# omnirecall uninstaller — removes the installed files and registrations.
set -e
DEST="$HOME/.omnirecall"

launchctl bootout "gui/$(id -u)/com.omnirecall.index" 2>/dev/null || \
  launchctl unload "$HOME/Library/LaunchAgents/com.omnirecall.index.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.omnirecall.index.plist"

command -v claude >/dev/null 2>&1 && claude mcp remove --scope user omnirecall >/dev/null 2>&1 || true

rm -f "$HOME/.claude/skills/omnirecall" "$HOME/.agents/skills/omnirecall"
rm -rf "$DEST"

echo "omnirecall removed. Your original agent transcripts were never modified."
