#!/usr/bin/env python3
"""Build the per-episode web page that the email "Listen" button points at.

Why this exists: Resend open/click tracking is inert on this domain (verified
2026-06-24 -- two real clicks produced zero events; the API flags are cosmetic).
For a podcast the metric that matters is LISTENS, not email opens. So the email's
"Listen" button no longer links straight to the raw .m4a; it links here, to a
hosted episode page that plays the same audio AND fires PostHog events. We get
listens + click-through + per-recipient attribution, independent of Resend.

The page reuses build_email_html.build_html for the brief body, so the web page
and the email can never drift apart (same deterministic renderer).
It adds a real <audio> player above the brief and a PostHog snippet that captures:
  episode_opened   - page load (click-through from the email)
  episode_play     - first play
  episode_progress - 25 / 50 / 75 percent listened
  episode_complete - reached the end

Per-recipient data rides in the query string (?e=<date>&r=<hash>), read client
side, so one cached HTML file serves the whole list. r = sha256(email)[:16],
which is non-sensitive and NOT the unsubscribe token.

The PostHog project key is a PUBLIC ingestion key -- it is designed to ship in
client HTML, so it lives here as an overridable default, not in a secret file.

Usage:
  build_episode_page.py build --kept work/kept-DATE.json --date DATE \
      --audio-url <m4a-url> [--posthog-key phc_... --posthog-host https://...] > page.html
  build_episode_page.py selftest        # exit 0 = PASS, 1 = FAIL
"""
import argparse
import html
import json
import sys

import build_email_html

# Optional listen-tracking. Off by default (empty key = no analytics injected).
# Set your own PostHog project key via PODCAST_POSTHOG_KEY / _HOST in .env.
DEFAULT_POSTHOG_KEY = ""
DEFAULT_POSTHOG_HOST = "https://us.i.posthog.com"

PAPER = build_email_html.PAPER
MAT = build_email_html.MAT
INK = build_email_html.INK

# Official PostHog loader snippet (array stub -> async-loads array.js). __PH_KEY__
# / __PH_HOST__ are replaced at build time (.replace, not .format -- the snippet is
# full of braces).
_PH_SNIPPET = (
    "!function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a)"
    "{function g(t,e){var o=e.split(\".\");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function()"
    "{t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement(\"script\"))"
    ".type=\"text/javascript\",p.async=!0,p.src=s.api_host.replace(\".i.posthog.com\","
    "\"-assets.i.posthog.com\")+\"/static/array.js\",(r=t.getElementsByTagName(\"script\")[0])"
    ".parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a=\"posthog\",u.people="
    "u.people||[],u.toString=function(t){var e=\"posthog\";return\"posthog\"!==a&&(e+=\".\"+a),"
    "t||(e+=\" (stub)\"),e},u.people.toString=function(){return u.toString(1)+\".people (stub)\"},"
    "o=\"capture identify alias people.set people.set_once set_config register register_once "
    "unregister opt_out_capturing has_opted_out_capturing opt_in_capturing reset isFeatureEnabled "
    "onFeatureFlags getFeatureFlag getFeatureFlagPayload reloadFeatureFlags group updateEarlyAccessFeatureEnrollment "
    "getEarlyAccessFeatures getActiveMatchingSurveys getSurveys getNextSurveyStep onSessionId\".split(\" \"),"
    "n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);"
    "posthog.init(\"__PH_KEY__\",{api_host:\"__PH_HOST__\",autocapture:!1,capture_pageview:!1});"
)

# Event wiring: identify the recipient, fire opened/play/progress/complete. Kept
# separate from the loader so it reads plainly.
_EVENT_JS = """
(function () {
  var q = new URLSearchParams(location.search);
  var ep = q.get('e') || 'unknown';
  var r = q.get('r');
  if (r) { try { posthog.identify(r); } catch (e) {} }
  var base = { product: 'ai-news-podcast', episode: ep };
  try { posthog.capture('episode_opened', base); } catch (e) {}
  var audio = document.getElementById('player');
  if (!audio) return;
  var played = false, marks = { 25: false, 50: false, 75: false };
  audio.addEventListener('play', function () {
    if (played) return;
    played = true;
    try { posthog.capture('episode_play', base); } catch (e) {}
  });
  audio.addEventListener('timeupdate', function () {
    if (!audio.duration) return;
    var pct = (audio.currentTime / audio.duration) * 100;
    [25, 50, 75].forEach(function (m) {
      if (pct >= m && !marks[m]) {
        marks[m] = true;
        try { posthog.capture('episode_progress', { product: base.product, episode: base.episode, percent: m }); } catch (e) {}
      }
    });
  });
  audio.addEventListener('ended', function () {
    try { posthog.capture('episode_complete', base); } catch (e) {}
  });
})();
"""


def _posthog_head(posthog_key, posthog_host):
    """Return the <script> block that loads + inits PostHog, or '' if no key."""
    if not posthog_key:
        return ""
    snippet = (_PH_SNIPPET
               .replace("__PH_KEY__", posthog_key)
               .replace("__PH_HOST__", posthog_host or DEFAULT_POSTHOG_HOST))
    return f"<script>{snippet}</script>"


def _player(audio_url):
    """Return the <audio> player block, or '' if no audio."""
    if not audio_url:
        return ""
    safe = html.escape(audio_url, quote=True)
    return (
        f'<div style="max-width:600px;margin:0 auto;padding:24px 16px 0;">'
        f'<audio id="player" controls preload="none" style="width:100%;" '
        f'src="{safe}"></audio></div>'
    )


def build_page(items, date, audio_url=None,
               posthog_key=DEFAULT_POSTHOG_KEY, posthog_host=DEFAULT_POSTHOG_HOST):
    """Return the full standalone HTML episode page.

    Reuses build_email_html.build_html for the brief body (audio_url=None there so
    it does not render a second listen button) and wraps it with an audio player
    and the PostHog tracking snippet.
    """
    brief = build_email_html.build_html(items, date, audio_url=None,
                                        unsubscribe_url=None)
    safe_date = html.escape(build_email_html.pretty_date(date))
    return (
        "<!doctype html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{safe_date} - {html.escape(build_email_html.PODCAST_NAME)}</title>"
        f"{_posthog_head(posthog_key, posthog_host)}"
        f"</head><body style=\"margin:0;background:{MAT};\">"
        f"{_player(audio_url)}"
        f"{brief}"
        f"<script>{_EVENT_JS}</script>"
        "</body></html>"
    )


def cmd_build(args):
    with open(args.kept, "r", encoding="utf-8") as fh:
        items = json.load(fh)
    if not isinstance(items, list):
        print("kept file must be a JSON array", file=sys.stderr)
        return 2
    sys.stdout.write(build_page(items, args.date, args.audio_url,
                                args.posthog_key, args.posthog_host))
    return 0


def cmd_selftest(_args):
    """Reproducer: the page embeds the audio player, the PostHog key, and the
    play-event wiring; titles stay HTML-escaped; with no audio_url there is no
    player element (negative self-test)."""
    ok = True
    items = [{
        "title": "Anthropic ships a governed-agent control plane",
        "summary": "Scope what autonomous agents may do in production.",
        "source": "Anthropic",
        "url": "https://anthropic.com/news/agents",
        "topic": "Trust and safety",
    }]
    page = build_page(items, "2026-06-24",
                      audio_url="https://x.supabase.co/public/ai-news-2026-06-24.m4a",
                      posthog_key="phc_TESTKEY", posthog_host="https://us.i.posthog.com")

    if '<audio id="player"' not in page:
        print("FAIL: audio player missing", file=sys.stderr); ok = False
    if "ai-news-2026-06-24.m4a" not in page:
        print("FAIL: audio src missing", file=sys.stderr); ok = False
    if "phc_TESTKEY" not in page:
        print("FAIL: posthog key not embedded", file=sys.stderr); ok = False
    if "episode_play" not in page or "episode_opened" not in page:
        print("FAIL: tracking events missing", file=sys.stderr); ok = False
    if "posthog.init" not in page:
        print("FAIL: posthog init missing", file=sys.stderr); ok = False
    if "Anthropic ships a governed-agent control plane" not in page:
        print("FAIL: brief body missing", file=sys.stderr); ok = False

    danger = build_page([{"title": "<script>alert(1)</script>", "source": "x"}],
                        "2026-06-24", audio_url="https://x/y.m4a")
    if "<script>alert(1)</script>" in danger:
        print("FAIL: title not HTML-escaped", file=sys.stderr); ok = False

    # negative self-test: no audio_url -> no player element, no tracking key fabricated
    noaudio = build_page(items, "2026-06-24", audio_url=None, posthog_key="")
    if '<audio id="player"' in noaudio:
        print("FAIL: player rendered without audio_url", file=sys.stderr); ok = False
    if "posthog.init" in noaudio:
        print("FAIL: posthog init rendered without a key", file=sys.stderr); ok = False

    print("PASS" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="build the tracked episode page")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build")
    build.add_argument("--kept", required=True)
    build.add_argument("--date", required=True)
    build.add_argument("--audio-url", default=None)
    build.add_argument("--posthog-key", default=DEFAULT_POSTHOG_KEY)
    build.add_argument("--posthog-host", default=DEFAULT_POSTHOG_HOST)
    build.set_defaults(func=cmd_build)

    sub.add_parser("selftest").set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
