#!/bin/bash
# A fully-autonomous daily AI-news podcast. Runs on a local cron/launchd timer.
# Chain: auth gate -> curate (headless claude researches 48h, writes candidates)
# -> dedup (no story repeats across episodes) -> build source brief -> make_podcast
# (two-host NotebookLM brief) -> optional delivery -> commit ledger. Any failure
# pings you on Slack (if SLACK_WEBHOOK is set) instead of dying silent.
#
# What it covers and who it is for live in config.json (audience, host persona,
# show name). The dedup ledger guarantees every episode is fresh, never a repeat.
#
# Must run LOCALLY on your machine: the NotebookLM cookies (~/.notebooklm-mcp-cli),
# the `nlm` binary, and your API keys (./.env) all live here.
# A cloud routine cannot reach them.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
NOTIFY="$SCRIPT_DIR/notify.sh"
TODAY="$(date +%F)"
WORK_DIR="$SCRIPT_DIR/work"
DIGEST="$SCRIPT_DIR/digests/$TODAY.txt"
CANDIDATES="$WORK_DIR/candidates-$TODAY.json"
POOL="$WORK_DIR/pool-$TODAY.json"
KEPT="$WORK_DIR/kept-$TODAY.json"
LEDGER="$SCRIPT_DIR/covered-log.jsonl"
LOG_DIR="$SCRIPT_DIR/logs"; mkdir -p "$LOG_DIR" "$SCRIPT_DIR/digests" "$WORK_DIR"
LOG="$LOG_DIR/run-$TODAY.log"

log()    { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
notify() { [ -x "$NOTIFY" ] && bash "$NOTIFY" "$1" >/dev/null 2>&1; }
fail()   { log "FAIL: $1"; notify "Daily podcast failed ($TODAY): $1"; exit 1; }

# Load API keys from .env (gitignored). See .env.example.
CREDS="$SCRIPT_DIR/.env"
[ -f "$CREDS" ] && { set -a; . "$CREDS"; set +a; }

log "=== daily podcast run $TODAY ==="

# 1) Auth gate (check_auth.sh Slack-pings the founder itself if expired).
bash "$SCRIPT_DIR/check_auth.sh" >>"$LOG" 2>&1 || { log "auth expired; check_auth alerted; aborting"; exit 1; }

# 1b) Freshness-gate self-check (deterministic): prove the 48h window still
#     drops undated AND stale items before we trust any harvest. Scar 2026-06-23:
#     dateless sources leaked a 7-day-old and a 3-month-old story into a "last
#     48h" brief. If this regresses, no episode ships -- that is the point.
python3 "$SCRIPT_DIR/fetch_sources.py" selftest >>"$LOG" 2>&1 || fail "fetch_sources selftest failed (48h freshness gate broken)"
python3 "$SCRIPT_DIR/dedup.py" selftest >>"$LOG" 2>&1 || fail "dedup selftest failed (repeat gate broken)"

# 2a) Harvest: deterministically pull the chosen sources (sources.json) into a
#     ranked candidate pool. Scripts over prompts -- the founder picks sources in
#     one JSON file, not a buried search prompt. X via Apify and any RSS/HN/etc.
#     source is a config line. A dead source skips; it never blanks the run.
# Load user config (config.json) and export it for the python scripts.
export PODCAST_SHOW_NAME="$(python3 "$SCRIPT_DIR/conf.py" show_name)"
export PODCAST_AUDIENCE="$(python3 "$SCRIPT_DIR/conf.py" audience)"
export PODCAST_OWNER_NAME="$(python3 "$SCRIPT_DIR/conf.py" owner_name)"
export PODCAST_OWNER_EMAIL="$(python3 "$SCRIPT_DIR/conf.py" owner_email)"
export PODCAST_LINK="$(python3 "$SCRIPT_DIR/conf.py" show_link)"
AUDIENCE="$PODCAST_AUDIENCE"
HOST_PERSONA="$(python3 "$SCRIPT_DIR/conf.py" host_persona)"
SHOW_NAME="$PODCAST_SHOW_NAME"
FORBIDDEN="$(python3 "$SCRIPT_DIR/conf.py" forbidden_terms)"
FORBIDDEN_CLAUSE=""
[ -n "$FORBIDDEN" ] && FORBIDDEN_CLAUSE="absolutely NO mention of $FORBIDDEN; "
# Source list: user copies sources.example.json -> sources.json (gitignored).
SOURCES="$SCRIPT_DIR/sources.json"; [ -f "$SOURCES" ] || SOURCES="$SCRIPT_DIR/sources.example.json"
log "harvesting sources..."
python3 "$SCRIPT_DIR/fetch_sources.py" build --config "$SOURCES" --date "$TODAY" --out "$POOL" >>"$LOG" 2>&1 || true
POOL_N="$(python3 -c "import json;print(len(json.load(open('$POOL'))))" 2>/dev/null || echo 0)"
log "pool: $POOL_N items from chosen sources"

# 2a-guard) Independent freshness assertion on the POOL itself (not just the gate
#   logic): every pooled item must carry a real date inside the window, measured
#   against real now. Catches an anchor/gate regression at runtime, every run.
#   Scar 2026-06-23: a noon-anchored window let a >48h story into a "48h" brief.
python3 - "$POOL" "$SCRIPT_DIR" >>"$LOG" 2>&1 <<'PYGUARD' || fail "pool freshness assertion failed (an item is undated or older than the window)"
import json, sys
sys.path.insert(0, sys.argv[2])
import fetch_sources as f
from datetime import timezone
pool = json.load(open(sys.argv[1]))
now = f.datetime.now(timezone.utc).timestamp()
window = 48
bad = []
for it in pool:
    ts = f.parse_ts(it.get("published_at"))
    if ts is None or (now - ts) / 3600.0 > window:
        bad.append(it.get("title", "")[:60])
if bad:
    print("STALE/UNDATED IN POOL:", bad)
    sys.exit(1)
print(f"freshness assertion OK: all {len(pool)} pooled items dated and <= {window}h")
PYGUARD

# 2b) Curate: headless Claude SELECTS from the pool (judgment lives here; the
#     dedup gate below is the deterministic slice). Falls back to a live web
#     sweep ONLY if the harvest came back empty (every source was down).
log "curating..."
if [ "$POOL_N" -ge 1 ]; then
  CURATE_PROMPT="You are ${HOST_PERSONA}. Read the candidate pool at $POOL: a JSON array of items already gathered from chosen sources, each with title, url, source, summary, published_at. SELECT the 4 to 5 MOST INTERESTING new tools worth trying: an open-source repo, an MCP server, a Claude Code skill or hook, an agentic-coding workflow, or a notable new model with real agent capabilities, the kind of thing a builder in this audience ($AUDIENCE) could install or steal today. Prefer something concrete and installable over commentary. DROP: vendor PR and funding/M&A, generic AI hype, anything with no actual tool to try, and any sponsored or ad content. You MAY use WebFetch on an item's url to sharpen it, but SELECT ONLY items present in the pool and keep their exact url. Write ONLY a JSON array to the file $CANDIDATES (overwrite if it exists). Each object MUST have: title, url, summary, source, topic. The summary is 2-3 plain sentences: name the tool/repo, what it does, and why a builder would want to try it. RULES: ${FORBIDDEN_CLAUSE}no self-promotion; no em-dashes. Output nothing else."
  CURATE_TOOLS=(Read WebFetch Write)
else
  log "WARN: pool empty (all sources down); falling back to a live web sweep"
  CURATE_PROMPT="You are ${HOST_PERSONA}. Use WebSearch and WebFetch to find the 4 to 5 MOST INTERESTING new tools from the LAST 48 HOURS only (anything older than 48h: drop it): an open-source repo, an MCP server, a Claude Code skill or hook, an agentic-coding workflow, or a notable new model with real agent capabilities, the kind a builder in this audience ($AUDIENCE) could install or steal today. Prefer concrete and installable over commentary. DROP: vendor PR and funding/M&A, generic AI hype, anything with no actual tool to try, and any sponsored or ad content. Write 4 to 5 candidates as a JSON array to the file $CANDIDATES. Each object MUST have: title, url, summary, source, topic. The summary is 2-3 plain sentences: name the tool/repo, what it does, and why a builder would want to try it. RULES: ${FORBIDDEN_CLAUSE}no self-promotion; no em-dashes. Use the Write tool to save ONLY the JSON array to $CANDIDATES (overwrite if it exists). Output nothing else."
  CURATE_TOOLS=(WebSearch WebFetch Write)
fi
claude -p "$CURATE_PROMPT" --allowedTools "${CURATE_TOOLS[@]}" --dangerously-skip-permissions --add-dir "$SCRIPT_DIR" >>"$LOG" 2>&1

# Guard: candidates file must exist and be a valid JSON array.
python3 -c "import json,sys; d=json.load(open('$CANDIDATES')); sys.exit(0 if isinstance(d,list) and d else 1)" 2>>"$LOG" \
  || fail "curation produced no valid candidates JSON"
log "candidates ready ($(python3 -c "import json;print(len(json.load(open('$CANDIDATES'))))" 2>/dev/null) items)"

# 3) Dedup gate (deterministic): drop anything already aired in a prior episode.
python3 "$SCRIPT_DIR/dedup.py" filter --ledger "$LEDGER" --candidates "$CANDIDATES" --date "$TODAY" >"$KEPT" 2>>"$LOG" \
  || fail "dedup filter failed"
KEPT_N="$(python3 -c "import json;print(len(json.load(open('$KEPT'))))" 2>/dev/null || echo 0)"
log "kept $KEPT_N fresh stories after dedup"
[ "$KEPT_N" -ge 1 ] || fail "every candidate was a repeat; nothing fresh to air"

# 4) Build the NotebookLM source brief from the kept (fresh) stories only.
python3 "$SCRIPT_DIR/build_source.py" build --kept "$KEPT" --date "$TODAY" >"$DIGEST" 2>>"$LOG" \
  || fail "source brief build failed"
[ -s "$DIGEST" ] || fail "source brief is empty"
log "source brief ready"

# 5) Build the podcast: tight tools rundown (NotebookLM brief, ~5 min, not the
#    long deep_dive chat -- founder wanted shorter episodes 2026-06-24).
OUT="$(bash "$SCRIPT_DIR/make_podcast.sh" --digest "$DIGEST" --length short --format brief 2>>"$LOG")"
FILE="$(printf '%s\n' "$OUT" | sed -n 's/^PODCAST_FILE=//p' | head -1)"
echo "$OUT" >>"$LOG"
[ -n "$FILE" ] && [ -s "$FILE" ] || fail "make_podcast produced no file"
log "podcast: $FILE"

# 6) Deliver. Default delivery=none: the .m4a is built and we stop here. Set
#    delivery="resend" in config.json (plus RESEND_API_KEY + SUPABASE_ACCESS_TOKEN
#    in .env) to host the audio, email subscribers, and publish the RSS feed.
DELIVERY="$(python3 "$SCRIPT_DIR/conf.py" delivery)"
if [ "$DELIVERY" = "resend" ] && [ -n "${RESEND_API_KEY:-}" ]; then
  python3 "$SCRIPT_DIR/send_resend.py" send --audio "$FILE" --kept "$KEPT" \
    --date "$TODAY" --subject "$SHOW_NAME - $TODAY" >>"$LOG" 2>&1 \
    || fail "Resend send failed"
  log "delivered via Resend"

  # 6b) Publish the episode to the podcast RSS feed (Apple/Spotify/etc.). The audio
  #     is already hosted by send_resend above; build_rss verifies it's live, adds
  #     the episode to the feed ledger, and uploads feed.xml. Non-fatal: a feed
  #     hiccup must not fail a run whose email already went out.
  python3 "$SCRIPT_DIR/build_rss.py" publish --date "$TODAY" --audio "$FILE" --kept "$KEPT" >>"$LOG" 2>&1 \
    && log "RSS feed published" || log "WARN: RSS feed publish failed (podcast apps won't see today's episode)"
else
  log "delivery=none: episode built at $FILE. Set delivery=resend in config.json to email it."
fi

# 8) Only after delivery: record what aired so tomorrow's dedup blocks it.
#    Single-writer rule -- this is the only place that writes the ledger.
python3 "$SCRIPT_DIR/dedup.py" commit --ledger "$LEDGER" --items "$KEPT" --date "$TODAY" >>"$LOG" 2>&1 \
  && log "ledger committed ($KEPT_N stories)" || log "WARN: ledger commit failed (tomorrow may repeat these)"

log "=== done: sent $TODAY ==="
