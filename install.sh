#!/bin/sh
# pawrecall installer — copies the runtime files, indexes history, registers MCP + skill.
# Usage: ./install.sh [--with-scheduler]   (scheduler = 30-min auto-reindex, macOS only)
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.pawrecall"
PY="$(command -v python3)"

echo "==> pawrecall installer"

if [ -z "$PY" ]; then
  echo "ERROR: python3 not found on PATH (3.9+ required)." >&2
  exit 1
fi
echo "    python3: $PY ($($PY --version 2>&1))"

mkdir -p "$DEST"
for f in index.py embed.py facts.py server.py eval.py standards.py prompt-hook.py \
         eval_cases.example.json embed.json.example facts.json.example; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DEST/$f"
done
chmod 600 "$DEST"/index.py "$DEST"/server.py 2>/dev/null || true

echo "==> Building index (incremental; first run reads all transcripts)..."
"$PY" "$DEST/index.py" index

# --- MCP registration -------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  echo "==> Registering MCP server (Claude Code, user scope)..."
  claude mcp remove --scope user pawrecall >/dev/null 2>&1 || true
  claude mcp add --scope user pawrecall -- "$PY" "$DEST/server.py" \
    && echo "    OK — restart Claude Code to load it."
else
  echo "==> 'claude' CLI not found; register manually with any MCP client:"
  echo "    command: $PY  args: [$DEST/server.py]"
fi

# --- Skill installation -----------------------------------------------------
# Codex (0.38+), Cursor, Trae, Qoder and ZCode all read the same SKILL.md convention.
# ZCode never creates its skills dir until first use — make it when ZCode itself is present.
if [ -d "$HOME/.zcode" ]; then
  mkdir -p "$HOME/.zcode/skills"
fi
for d in "$HOME/.claude/skills" "$HOME/.agents/skills" "$HOME/.codex/skills" \
         "$HOME/.config/opencode/skills" "$HOME/.cursor/skills" \
         "$HOME/.trae/skills" "$HOME/.qoder/skills" "$HOME/.zcode/skills"; do
  if [ -d "$d" ]; then
    ln -sfn "$SRC/skill" "$d/pawrecall"
    echo "==> Skill linked: $d/pawrecall"
    if [ -d "$SRC/skills/paw-standards" ]; then
      ln -sfn "$SRC/skills/paw-standards" "$d/paw-standards"
      echo "==> Skill linked: $d/paw-standards"
    fi
  fi
done

# --- Optional scheduler (macOS LaunchAgent, every 30 min) -------------------
if [ "$1" = "--with-scheduler" ] && [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.pawrecall.index.plist"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.pawrecall.index</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$DEST/index.py</string>
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

# --- Deterministic trigger hook (Claude Code, optional but recommended) -----
"$PY" - "$PY" "$DEST" <<'EOF'
import json, os, sys
py, dest = sys.argv[1], sys.argv[2]
p = os.path.expanduser("~/.claude/settings.json")
cfg = {}
if os.path.exists(p):
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
hooks = cfg.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
entry = {"hooks": [{"type": "command", "command": py + " " + dest + "/prompt-hook.py"}]}
if entry not in hooks:
    hooks.append(entry)
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("==> Hook installed (UserPromptSubmit: says '之前/上次/we discussed' -> forced search reminder)")
EOF

cat <<EOF

==> Done.
    Database : $DEST/history.db  (local only, chmod 600)
    MCP      : pawrecall  (restart your agent to load)
    CLI      : python3 $DEST/index.py search "keyword"
    Optional : cp $DEST/facts.json.example $DEST/facts.json  (enable the fact layer)
    Try it   : tell any agent "find where we discussed X before"
EOF
