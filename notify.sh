#!/bin/bash
# Optional failure-alert channel. Posts one line to a Slack Incoming Webhook if
# $SLACK_WEBHOOK is set; silent no-op otherwise, so the pipeline never breaks on
# a missing webhook. This replaces any project-specific notifier so the repo is
# self-contained.
#
# Usage: notify.sh "one concise line"
set -u
MSG="${1:-}"
[ -z "$MSG" ] && exit 0
[ -z "${SLACK_WEBHOOK:-}" ] && exit 0
# Minimal JSON escaping for the message text.
ESC="$(printf '%s' "$MSG" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"text\": $ESC}" "$SLACK_WEBHOOK" >/dev/null 2>&1 || true
exit 0
