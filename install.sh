#!/bin/bash
# Install agent-session-restore for the current user.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.agent-session-restore"
LABEL="com.agent-session-restore.keeper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ "$(uname)" = "Darwin" ] || { echo "macOS only"; exit 1; }
PY=/usr/bin/python3
if ! "$PY" -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null; then
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] && "$PY" -c 'import sys; assert sys.version_info >= (3, 9)' \
    || { echo "Need Python 3.9+ (xcode-select --install)"; exit 1; }
fi

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
[ -f "$DIR/config.json" ] || cat > "$DIR/config.json" <<JSON
{
  "auto_trust": true,
  "notify_cmd": null
}
JSON

cat > "$DIR/launcher.command" <<SH
#!/bin/zsh
# Started by launchd (keeper) through Terminal so the daemon runs with
# Terminal's own permissions. The daemon closes this window.
nohup "$PY" "$REPO/agent_session_restore.py" daemon "\$(tty)" >> "$DIR/daemon.log" 2>&1 &!
SH
chmod +x "$DIR/launcher.command"

cat > "$PLIST" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$REPO/agent_session_restore.py</string><string>keeper</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$DIR/keeper.log</string>
  <key>StandardErrorPath</key><string>$DIR/keeper.log</string>
</dict>
</plist>
XML

# The current boot needs no restore; start snapshotting right away.
"$PY" "$REPO/agent_session_restore.py" mark
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed. A Terminal window opens briefly to start the daemon."
echo "Check with: $PY $REPO/agent_session_restore.py status"
