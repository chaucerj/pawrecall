#!/bin/sh
# omnirecall installer — copies one file, indexes history, registers MCP + skill.
# Usage: ./install.sh [--with-scheduler]   (scheduler = 30-min auto-reindex, macOS only)
set -e

SRC="$(cd "$(dirname "$0")" && pwd)/omnirecall.py"
DEST="$HOME/.omnirecall"
PY="$(command -v python3)"

echo "==> omnirecall installer"

if [ -z "$PY" ]; then
  echo "ERROR: python3 not found on PATH (3.9+ required)." >&2
  exit 1
fi
echo "    python3: $PY ($($PY --version 2>&1))"

mkdir -p "$DEST"
cp "$SRC" "$DEST/omnirecall.py"
chmod 600 "$DEST/omnirecall.py" 2>/dev/null || true

echo "==> Building index (incremental; first run reads all transcripts)..."
"$PY" "$DEST/omnirecall.py" index

# --- MCP registration -------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  echo "==> Registering MCP server (Claude Code, user scope)..."
  claude mcp remove --scope user omnirecall >/dev/null 2>&1 || true
  claude mcp add --scope user omnirecall -- "$PY" "$DEST/omnirecall.py" serve \
    && echo "    OK — restart Claude Code to load it."
else
  echo "==> 'claude' CLI not found; register manually with any MCP client:"
  echo "    command: $PY  args: [$DEST/omnirecall.py, serve]"
fi

# --- Skill installation -----------------------------------------------------
# Codex (0.38+) and OpenCode read the same SKILL.md convention natively.
for d in "$HOME/.claude/skills" "$HOME/.agents/skills" "$HOME/.codex/skills" \
         "$HOME/.config/opencode/skills"; do
  if [ -d "$d" ]; then
    ln -sfn "$(cd "$(dirname "$0")" && pwd)/skill" "$d/omnirecall"
    echo "==> Skill linked: $d/omnirecall"
  fi
done

# --- Optional scheduler (macOS LaunchAgent, every 30 min) -------------------
if [ "$1" = "--with-scheduler" ] && [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.omnirecall.index.plist"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.omnirecall.index</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$DEST/omnirecall.py</string>
        <string>index</string>
    </array>
    <key>StartInterval</key><integer>1800</integer>
    <key>RunAtLoad</key><true/>
    <key>LowPriorityIO</key><true/>
    <key>StandardOutPath</key><string>$DEST/index.log</string>
    <key>StandardErrorPath</key><string>$DEST/index.log</string>
</dict>
</plist>
PLIST
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
  echo "==> Scheduler installed (every 30 min). Log: $DEST/index.log"
fi

cat <<EOF

==> Done.
    Database : $DEST/history.db  (local only, chmod 600)
    MCP      : omnirecall  (restart your agent to load)
    CLI      : python3 $DEST/omnirecall.py search "keyword"
    Try it   : tell any agent "find where we discussed X before"
EOF
