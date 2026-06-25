#!/bin/bash
# NotebookLM auth watchdog for the daily podcast pipeline.
#
# Why this exists: the podcast engine (notebooklm-mcp-cli, `nlm`) authenticates
# with browser cookies that expire roughly every 2-4 weeks. When they expire the
# daily generation silently fails. This script detects that and pings the founder
# on Slack with the exact one-command fix, so the only recurring human touch is a
# 30-second re-login. Deterministic check (`nlm login --check`), not a guess.
#
# Exit codes: 0 = auth valid (or --test). 1 = auth expired (caller should stop).
#
# Usage:
#   check_auth.sh                 # check default profile, alert on failure
#   check_auth.sh --profile work  # check a named profile
#   check_auth.sh --test          # send a labeled test ping, prove delivery
#
# State file dedupes alerts: it pings on the ok->expired transition (and once on
# recovery), not every run, so a daily cron does not spam while you are asleep.
set -u

PROFILE="default"
TEST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --test) TEST=1; shift ;;
    *) shift ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Load .env so a standalone --test run can reach SLACK_WEBHOOK too.
[ -f "$SCRIPT_DIR/.env" ] && { set -a; . "$SCRIPT_DIR/.env"; set +a; }
NOTIFY="$SCRIPT_DIR/notify.sh"
STATE_DIR="$HOME/.notebooklm-mcp-cli"
STATE_FILE="$STATE_DIR/.nlm_auth_alert_state_${PROFILE}"
mkdir -p "$STATE_DIR"

# Cron/headless shells have a minimal PATH; uv installs nlm here.
export PATH="$HOME/.local/bin:$PATH"

notify() { # $1 = message; silent no-op if webhook unconfigured (slack-notify handles that)
  if [ -x "$NOTIFY" ]; then bash "$NOTIFY" "$1"; fi
}

if [ "$TEST" = "1" ]; then
  notify "[test] Podcast auth alert is wired. If NotebookLM login expires you'll get a ping here with the fix."
  echo "test alert sent"
  exit 0
fi

LAST="ok"; [ -f "$STATE_FILE" ] && LAST="$(cat "$STATE_FILE" 2>/dev/null || echo ok)"

if nlm login --check --profile "$PROFILE" >/dev/null 2>&1; then
  # auth good
  if [ "$LAST" = "bad" ]; then
    notify "NotebookLM auth is back. Daily podcast resumes."
  fi
  echo "ok" > "$STATE_FILE"
  exit 0
else
  # auth expired
  if [ "$LAST" != "bad" ]; then
    notify "NotebookLM login expired - daily podcast is paused. Fix (~30s): run \`nlm login\` in your terminal, sign into your Google account. Then it auto-resumes."
  fi
  echo "bad" > "$STATE_FILE"
  exit 1
fi
