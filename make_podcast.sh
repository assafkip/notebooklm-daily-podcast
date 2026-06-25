#!/bin/bash
# Build one daily NotebookLM podcast from a digest file, fully headless.
# Chain (all via notebooklm-mcp-cli `nlm`, internal-API, no UI clicking):
#   check_auth -> notebook create -> source add (text) -> audio create
#   (short/brief) -> poll studio status -> download .m4a
#
# Usage:
#   make_podcast.sh --digest <file> [--name "<title>"] [--out <dir>]
#                   [--length short|default|long] [--format brief|deep_dive|critique]
# Prints "PODCAST_FILE=<path>" on success; non-zero exit on any failure.
# Why a script, not prose: deterministic, cron-safe, single place to repair if
# nlm's output shape changes.
set -u

DIGEST=""; NAME=""; OUT=""; LENGTH="short"; FORMAT="brief"
while [ $# -gt 0 ]; do
  case "$1" in
    --digest) DIGEST="$2"; shift 2 ;;
    --name)   NAME="$2"; shift 2 ;;
    --out)    OUT="$2"; shift 2 ;;
    --length) LENGTH="$2"; shift 2 ;;
    --format) FORMAT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
TODAY="$(date +%F)"
[ -z "$NAME" ] && NAME="${PODCAST_SHOW_NAME:-AI News Daily} $TODAY"
[ -z "$OUT" ] && OUT="$SCRIPT_DIR/episodes"
mkdir -p "$OUT"

if [ -z "$DIGEST" ] || [ ! -f "$DIGEST" ]; then
  echo "ERROR: --digest <file> required and must exist" >&2; exit 64
fi

# 1) Auth gate (alerts you on Slack if expired, then we stop).
if ! bash "$SCRIPT_DIR/check_auth.sh"; then
  echo "ERROR: NotebookLM auth expired; alerted founder; aborting run." >&2; exit 75
fi

# 2) Create notebook -> notebook_id
NB="$(nlm notebook create "$NAME" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['notebook_id'])" 2>/dev/null)"
if [ -z "$NB" ]; then echo "ERROR: notebook create failed" >&2; exit 1; fi
echo "NOTEBOOK_ID=$NB"

# 3) Add the digest as a text source
if ! nlm source add "$NB" --text "$(cat "$DIGEST")" >/dev/null 2>&1; then
  echo "ERROR: source add failed" >&2; exit 1; fi

# 4) Generate audio (short/brief) -> artifact id
GEN="$(nlm audio create "$NB" --length "$LENGTH" --format "$FORMAT" --confirm 2>&1)"
ART="$(printf '%s\n' "$GEN" | sed -n 's/.*Artifact ID: *\([0-9a-f-]\{8,\}\).*/\1/p' | head -1)"
if [ -z "$ART" ]; then echo "ERROR: audio create failed: $GEN" >&2; exit 1; fi
echo "ARTIFACT_ID=$ART"

# 5) Poll until completed (up to ~15 min)
STATUS=""
for i in $(seq 1 30); do
  STATUS="$(nlm studio status "$NB" 2>/dev/null | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin); print([x['status'] for x in d if x.get('id')=='$ART'][0])
except Exception: print('unknown')" 2>/dev/null)"
  echo "[poll $i] $STATUS"
  case "$STATUS" in ready|completed|complete) break ;; esac
  sleep 30
done
case "$STATUS" in ready|completed|complete) : ;; *) echo "ERROR: audio not ready (last=$STATUS)" >&2; exit 1 ;; esac

# 6) Download
FILE="$OUT/ai-news-$TODAY.m4a"
if ! nlm download audio "$NB" --id "$ART" -o "$FILE" --no-progress >/dev/null 2>&1; then
  echo "ERROR: download failed" >&2; exit 1; fi
[ -s "$FILE" ] || { echo "ERROR: downloaded file empty" >&2; exit 1; }
echo "PODCAST_FILE=$FILE"
