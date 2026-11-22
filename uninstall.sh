#!/bin/bash
# Remove agent-session-restore. Keeps ~/.agent-session-restore unless --purge.
set -uo pipefail
DIR="$HOME/.agent-session-restore"
LABEL="com.agent-session-restore.keeper"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pid="$(cat "$DIR/daemon.lock" 2>/dev/null)"
[ -n "$pid" ] && kill "$pid" 2>/dev/null
[ "${1:-}" = "--purge" ] && rm -rf "$DIR"
echo "Uninstalled."
