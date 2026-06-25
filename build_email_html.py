#!/usr/bin/env python3
"""Build the HTML email body: a styled, quick-scan visual brief of the episode.

Pairs with run_daily.sh. The same kept stories that feed the
audio also render as a premium "Editorial" brief: warm paper, serif headlines, a color-coded topic kicker per story,
hairline rules. A reader who would rather skim than listen gets it in seconds.
Deterministic (scripts over prompts) and built from the same JSON,
so the visual and the audio can never drift apart.

Inline styles + a table layout only (email clients strip <style>/external CSS and
many ignore flexbox/grid). System serif (Georgia) so no web-font dependency.

Usage:
  build_email_html.py build --kept work/kept-DATE.json --date DATE > body.html
  build_email_html.py selftest        # exit 0 = PASS, 1 = FAIL

Item shape: {"title": str, "url": str?, "summary": str?, "source": str?, "topic": str?}.
"""
import argparse
import datetime as dt
import html
import json
import os
import sys

PODCAST_NAME = os.environ.get("PODCAST_SHOW_NAME", "AI News Daily")
AUDIENCE = os.environ.get(
    "PODCAST_AUDIENCE",
    "AI builders, developers, and startup founders",
)

# Bg/paper/ink for the editorial palette.
PAPER = "#faf8f3"
MAT = "#efece4"
INK = "#1c1917"
MUTED = "#57534e"
FAINT = "#a8a29e"
RULE = "#e7e2d9"


def topic_style(topic):
    """Map the free-text topic to a (label, accent-color) for the kicker."""
    t = (topic or "").lower()
    if any(k in t for k in ("trust", "safety", "fraud", "threat")):
        return ("Threat Intel", "#b91c1c")
    if any(k in t for k in ("regulation", "policy", "governance", "law")):
        return ("Policy", "#b45309")
    if any(k in t for k in ("fund", "m&a", "acquisition", "raise")):
        return ("Funding", "#047857")
    if any(k in t for k in ("workflow", "agent", "tooling", "mcp")):
        return ("Workflow", "#6d28d9")
    if any(k in t for k in ("model", "release", "capability", "open")):
        return ("Model", "#1d4ed8")
    return ("Industry", "#475569")


def pretty_date(date):
    """YYYY-MM-DD -> 'June 23, 2026'. Falls back to the raw string."""
    try:
        return dt.datetime.strptime(date, "%Y-%m-%d").strftime("%B %-d, %Y")
    except (ValueError, TypeError):
        return date


def esc(text):
    return html.escape((text or "").strip())


def _story_row(item):
    title = esc(item.get("title"))
    if not title:
        return ""
    label, color = topic_style(item.get("topic"))
    summary = esc(item.get("summary"))
    source = esc(item.get("source"))
    url = (item.get("url") or "").strip()

    summary_html = ""
    if summary:
        summary_html = (
            f'<div style="font-size:16px;line-height:1.65;color:{MUTED};'
            f'margin-top:10px;">{summary}</div>'
        )
    source_html = ""
    if source and url:
        safe_url = html.escape(url, quote=True)
        source_html = (
            f'<a href="{safe_url}" style="font-size:13px;color:{INK};'
            f'text-decoration:underline;text-underline-offset:3px;'
            f'display:inline-block;margin-top:12px;">{source}</a>'
        )
    elif source:
        source_html = (
            f'<div style="font-size:13px;color:{FAINT};margin-top:12px;">'
            f'{source}</div>'
        )

    return (
        f'<tr><td style="padding:26px 0;border-bottom:1px solid {RULE};">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:.14em;'
        f'text-transform:uppercase;color:{color};">{label}</div>'
        f'<div style="font-family:Georgia,\'Times New Roman\',serif;font-size:23px;'
        f'font-weight:700;line-height:1.3;color:{INK};margin-top:8px;">{title}</div>'
        f"{summary_html}{source_html}"
        "</td></tr>"
    )


def _listen_button(audio_url):
    if not audio_url:
        return ""
    safe = html.escape(audio_url, quote=True)
    return (
        f'<div style="margin-top:16px;"><a href="{safe}" '
        f'style="display:inline-block;background:{INK};color:#faf8f3;'
        f'font-size:14px;font-weight:600;text-decoration:none;padding:11px 20px;'
        f'border-radius:6px;">&#9654;&nbsp; Listen to today\'s brief</a></div>'
    )


def build_html(items, date, audio_url=None, unsubscribe_url=None):
    """Return the styled Editorial HTML email body for the episode.

    audio_url -> a "Listen" button (hosted link, no attachment).
    unsubscribe_url -> a per-recipient unsubscribe link in the footer.
    """
    safe_date = esc(pretty_date(date))
    intro_line = ("Audio is up top, the read is below." if audio_url
                  else "Audio attached, the read is below.")
    header = (
        f'<tr><td style="padding:40px 40px 20px;border-bottom:2px solid {INK};">'
        f'<div style="font-size:12px;font-weight:700;letter-spacing:.2em;'
        f'text-transform:uppercase;color:{FAINT};">{esc(PODCAST_NAME)}</div>'
        f'<div style="font-family:Georgia,\'Times New Roman\',serif;font-size:40px;'
        f'font-weight:800;color:{INK};margin-top:4px;letter-spacing:-.02em;">{safe_date}</div>'
        f'<div style="font-size:13px;color:#78716c;margin-top:10px;font-style:italic;'
        f'line-height:1.5;">The last 48 hours for {esc(AUDIENCE)}. {intro_line}</div>'
        f'{_listen_button(audio_url)}</td></tr>'
    )

    if not items:
        rows = (
            f'<tr><td style="padding:30px 0;font-family:Georgia,serif;font-size:18px;'
            f'line-height:1.6;color:{MUTED};">Quiet stretch. Nothing in the last 48 '
            f'hours cleared the bar for this audience that has not already been '
            f'covered.</td></tr>'
        )
    else:
        rows = "".join(_story_row(item) for item in items)

    unsub_html = ""
    if unsubscribe_url:
        safe_unsub = html.escape(unsubscribe_url, quote=True)
        unsub_html = (
            f' &middot; <a href="{safe_unsub}" style="color:{FAINT};'
            f'text-decoration:underline;">Unsubscribe</a>'
        )
    footer = (
        f'<tr><td style="padding:22px 40px 36px;">'
        f'<div style="font-size:12px;color:{FAINT};font-style:italic;">'
        f'Generated automatically. No repeats from prior episodes.{unsub_html}</div></td></tr>'
    )

    return (
        f'<div style="margin:0;padding:0;background:{MAT};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{MAT};"><tr><td align="center" style="padding:36px 16px;">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;width:100%;background:{PAPER};">'
        f'{header}'
        f'<tr><td style="padding:0 40px;"><table role="presentation" width="100%" '
        f'cellpadding="0" cellspacing="0">{rows}</table></td></tr>'
        f'{footer}'
        f'</table></td></tr></table></div>'
    )


def cmd_build(args):
    with open(args.kept, "r", encoding="utf-8") as fh:
        items = json.load(fh)
    if not isinstance(items, list):
        print("kept file must be a JSON array", file=sys.stderr)
        return 2
    sys.stdout.write(build_html(items, args.date, args.audio_url, args.unsubscribe_url))
    return 0


def cmd_selftest(_args):
    """Reproducer: a real story renders a serif title + linked source + topic
    kicker; an empty list renders the quiet-day card; titles are HTML-escaped."""
    ok = True

    items = [{
        "title": "Anthropic ships a governed-agent control plane",
        "summary": "Scope what autonomous agents may do in production.",
        "source": "Anthropic",
        "url": "https://anthropic.com/news/agents",
        "topic": "Trust and safety",
    }]
    out = build_html(items, "2026-06-23")
    if "Anthropic ships a governed-agent control plane" not in out:
        print("FAIL: story title missing", file=sys.stderr); ok = False
    if 'href="https://anthropic.com/news/agents"' not in out:
        print("FAIL: source link missing", file=sys.stderr); ok = False
    if "Threat Intel" not in out:
        print("FAIL: topic kicker missing/miscolored", file=sys.stderr); ok = False
    if "June 23, 2026" not in out:
        print("FAIL: date not prettified", file=sys.stderr); ok = False
    if "Georgia" not in out:
        print("FAIL: serif styling missing", file=sys.stderr); ok = False
    if "AI builders" not in out:
        print("FAIL: audience header missing", file=sys.stderr); ok = False

    danger = build_html([{"title": "<script>alert(1)</script>", "source": "x"}],
                        "2026-06-23")
    if "<script>alert(1)</script>" in danger:
        print("FAIL: title not HTML-escaped", file=sys.stderr); ok = False

    # audio link + unsubscribe link render when provided
    linked = build_html(items, "2026-06-23",
                        audio_url="https://x.supabase.co/audio.m4a",
                        unsubscribe_url="https://x.functions.supabase.co/unsubscribe?token=abc")
    if "Listen to today" not in linked or "audio.m4a" not in linked:
        print("FAIL: listen button missing", file=sys.stderr); ok = False
    if "Unsubscribe" not in linked or "token=abc" not in linked:
        print("FAIL: unsubscribe link missing", file=sys.stderr); ok = False
    # and absent when not provided (no fake unsubscribe link)
    if "Unsubscribe" in build_html(items, "2026-06-23"):
        print("FAIL: unsubscribe link present without url", file=sys.stderr); ok = False

    empty = build_html([], "2026-06-23")
    if "Quiet stretch" not in empty:
        print("FAIL: quiet-day card missing", file=sys.stderr); ok = False
    # negative self-test: quiet-day must not render a story rule row
    if f"border-bottom:1px solid {RULE}" in empty:
        print("FAIL: quiet-day fabricated a story row", file=sys.stderr); ok = False

    print("PASS" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="build styled HTML email brief")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build")
    build.add_argument("--kept", required=True)
    build.add_argument("--date", required=True)
    build.add_argument("--audio-url", default=None)
    build.add_argument("--unsubscribe-url", default=None)
    build.set_defaults(func=cmd_build)

    selftest = sub.add_parser("selftest")
    selftest.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
