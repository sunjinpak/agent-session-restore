#!/bin/bash
# Send the verify report to Telegram. Put TELEGRAM_BOT_TOKEN and
# TELEGRAM_CHAT_ID in ~/.agent-session-restore/telegram.env, then set
#   "notify_cmd": ["/path/to/telegram-notify.sh"]
# in ~/.agent-session-restore/config.json.
# Uses sendMessage only, so it never competes with a bot that polls updates.
source "$HOME/.agent-session-restore/telegram.env"
curl -s --max-time 20 "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
  --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" --data-urlencode "text=$1" >/dev/null
