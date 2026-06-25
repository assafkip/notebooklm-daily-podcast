#!/bin/bash
# Run every script's built-in offline selftest. No creds, no network, no config.
# These are the deterministic gates the daily run depends on: the 48h freshness
# filter, the no-repeat dedup ledger, the brief builder, the email/page renderers,
# and the subscriber-store logic. If any fails, the pipeline is not safe to run.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
fail=0
for mod in fetch_sources dedup build_source subscribers \
           build_email_html build_episode_page send_resend build_rss; do
  if python3 "$mod.py" selftest >/dev/null 2>&1; then
    printf "  PASS  %s\n" "$mod"
  else
    printf "  FAIL  %s\n" "$mod"; fail=1
  fi
done
[ "$fail" = 0 ] && echo "all selftests passed" || echo "SELFTESTS FAILED"
exit $fail
