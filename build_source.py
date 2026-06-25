#!/usr/bin/env python3
"""Build the NotebookLM source brief from the deduped kept items.

Pairs with run_daily.sh: after dedup.py drops anything already aired, this turns
the surviving items (JSON array) into the single text source NotebookLM reads to
generate the two-host (deep_dive) audio. Deterministic on purpose -- the founder
rule is scripts over prompts, so assembling the brief is code, not a second LLM
call that could drift or re-introduce noise.

Audience framing is baked into the header so the two hosts discuss why each story
matters to the people this podcast serves.

Usage:
  build_source.py --kept work/kept-DATE.json --date DATE > digests/DATE.txt
  build_source.py selftest        # exit 0 = PASS, 1 = FAIL

Item shape (JSON object): {"title": str, "summary": str?, "source": str?}.
Only `title` is required; missing fields are skipped cleanly.
"""
import argparse
import json
import os
import sys

# Overridden per run from config.json via $PODCAST_AUDIENCE (run_daily.sh).
AUDIENCE = os.environ.get(
    "PODCAST_AUDIENCE",
    "AI builders, developers, and startup founders",
)


def build_brief(items, date):
    """Return the source text NotebookLM turns into the two-host episode."""
    header = (
        f"AI news brief for {date}. "
        f"These are the developments from the last 48 hours that matter to "
        f"{AUDIENCE}. Discuss what happened and why it matters to those people."
    )
    if not items:
        return (
            header
            + "\n\nQuiet stretch. Nothing in the last 48 hours clears the bar "
            "for this audience that has not already been covered. Keep it short "
            "and honest rather than padding it with filler."
        )
    lines = [header, ""]
    for index, item in enumerate(items, start=1):
        title = (item.get("title") or "").strip()
        if not title:
            continue
        summary = (item.get("summary") or "").strip()
        source = (item.get("source") or "").strip()
        lines.append(f"{index}. {title}")
        if summary:
            lines.append(summary)
        if source:
            lines.append(f"Source: {source}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_build(args):
    with open(args.kept, "r", encoding="utf-8") as fh:
        items = json.load(fh)
    if not isinstance(items, list):
        print("kept file must be a JSON array", file=sys.stderr)
        return 2
    sys.stdout.write(build_brief(items, args.date))
    return 0


def cmd_selftest(_args):
    """Reproducer: a real item produces a numbered brief with its source, and an
    empty list produces the honest quiet-day brief. PASS proves both shapes."""
    ok = True

    items = [{
        "title": "Anthropic ships a governed-agent control plane",
        "summary": "A new way to scope what autonomous agents may do in prod.",
        "source": "Anthropic",
    }]
    brief = build_brief(items, "2026-06-22")
    if "1. Anthropic ships a governed-agent control plane" not in brief:
        print("FAIL: numbered story missing from brief", file=sys.stderr)
        ok = False
    if "Source: Anthropic" not in brief:
        print("FAIL: source line missing from brief", file=sys.stderr)
        ok = False
    if "AI builders" not in brief:
        print("FAIL: audience framing missing from brief", file=sys.stderr)
        ok = False

    empty = build_brief([], "2026-06-22")
    # negative self-test: the quiet-day brief must NOT invent a numbered story
    if "1." in empty:
        print("FAIL: quiet-day brief fabricated a story", file=sys.stderr)
        ok = False
    if "Quiet stretch" not in empty:
        print("FAIL: quiet-day brief missing honest fallback", file=sys.stderr)
        ok = False

    print("PASS" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="build NotebookLM source brief")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build")
    build.add_argument("--kept", required=True)
    build.add_argument("--date", required=True)
    build.set_defaults(func=cmd_build)

    selftest = sub.add_parser("selftest")
    selftest.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
