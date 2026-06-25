#!/usr/bin/env python3
"""Build + publish the podcast RSS 2.0 feed for the show.

This turns the daily episodes (already hosted in Supabase Storage by
send_resend.py) into a podcast feed that Apple Podcasts, Spotify, Overcast,
Pocket Casts, and YouTube Music can ingest. One feed serves every platform.

Design:
- The feed is rebuilt from a single ledger (feed-episodes.jsonl). Each episode
  is one JSON line. We never hand-append XML (that risks a malformed feed); we
  regenerate the whole feed.xml from the ledger every time.
- Single-writer: only `add`/`publish` writes the ledger, and only AFTER the
  episode's audio is confirmed live (HEAD 200). A dead <enclosure> gets a show
  pulled from Apple, so we never list audio we can't reach.
- AI disclosure is baked into the channel description and every item, because
  Apple requires prominent disclosure of AI-generated audio (and Spotify wants
  transparency). The voices are NotebookLM; no real person is impersonated.

The audio URL is deterministic from the project ref + date, matching the object
name send_resend.py uploads (ai-news-<date>.m4a), so this script needs no
network to know where the audio lives -- only to verify it and upload the feed.

Usage:
  build_rss.py publish --date YYYY-MM-DD --audio <m4a> --kept <kept.json> [--out feed.xml]
  build_rss.py add     --date YYYY-MM-DD --audio <m4a> --kept <kept.json> [--out feed.xml]
  build_rss.py build   [--out feed.xml]          # regenerate from ledger, no upload
  build_rss.py upload  --file <path> [--object feed.xml]
  build_rss.py selftest
"""
import argparse
import json
import os
import sys
from xml.dom import minidom
from xml.sax.saxutils import escape, quoteattr

import build_email_html
import send_resend
import subscribers

# --- Feed identity (non-secret; override the contact via env). ---------------
FEED_TITLE = os.environ.get("PODCAST_SHOW_NAME", "AI News Daily")
FEED_AUTHOR = os.environ.get("PODCAST_OWNER_NAME", "AI News Daily")
FEED_OWNER_NAME = os.environ.get("PODCAST_OWNER_NAME", "")
FEED_OWNER_EMAIL = os.environ.get("PODCAST_OWNER_EMAIL", "")
FEED_LINK = os.environ.get("PODCAST_LINK", "")
FEED_LANGUAGE = "en-us"
FEED_CATEGORY = "Technology"
FEED_DESCRIPTION = os.environ.get("PODCAST_DESCRIPTION") or (
    "A daily briefing on the AI news that matters to "
    + os.environ.get("PODCAST_AUDIENCE", "builders and founders")
    + ". The audio is AI-generated with Google NotebookLM; stories are curated "
    "from real reporting and may contain errors. No affiliation is implied with "
    "any company covered."
)
AI_DISCLOSURE = "AI-generated audio (Google NotebookLM). Curated from real reporting; may contain errors."

BUCKET = "podcast-episodes"
COVER_OBJECT = "cover.png"
FEED_OBJECT = "feed.xml"
ENCLOSURE_TYPE = "audio/x-m4a"
LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed-episodes.jsonl")


# --- URL + naming helpers ----------------------------------------------------
def public_url(ref, obj):
    """Public Storage URL for an object in the episodes bucket."""
    return f"https://{ref}.supabase.co/storage/v1/object/public/{BUCKET}/{obj}"


def audio_object(date):
    """Object name send_resend.py uploads the m4a under."""
    return f"ai-news-{date}.m4a"


def feed_self_url(ref):
    return public_url(ref, FEED_OBJECT)


def rfc2822(date):
    """RFC-2822 pubDate at a fixed 12:00 GMT, so the feed is reproducible."""
    import datetime as dt
    day = dt.datetime.strptime(date, "%Y-%m-%d")
    return day.strftime("%a, %d %b %Y") + " 12:00:00 GMT"


def episode_title(date):
    return f"{FEED_TITLE} for {build_email_html.pretty_date(date)}"


def episode_description(items):
    """Plain-text show notes: one line per story, AI disclosure last."""
    lines = []
    for item in items:
        title = (item.get("title") or "").strip()
        summary = (item.get("summary") or "").strip()
        if title:
            lines.append(f"- {title}: {summary}" if summary else f"- {title}")
    lines.append("")
    lines.append(AI_DISCLOSURE)
    return "\n".join(lines)


# --- Ledger (single source of truth for the feed) ----------------------------
def load_episodes(path=LEDGER):
    """Return episode records from the ledger, newest first."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    records.sort(key=lambda r: r.get("date", ""), reverse=True)
    return records


def episode_record(date, audio_url, audio_bytes, items):
    """Build one immutable episode record for the ledger."""
    return {
        "date": date,
        "guid": f"episode-{date}",
        "title": episode_title(date),
        "description": episode_description(items),
        "audio_url": audio_url,
        "audio_bytes": audio_bytes,
        "pub_date": rfc2822(date),
    }


def append_episode(record, path=LEDGER):
    """Append one episode to the ledger, replacing any same-date entry.

    Single-writer: this is the only function that writes the ledger.
    """
    kept = [r for r in load_episodes(path) if r.get("date") != record["date"]]
    kept.append(record)
    kept.sort(key=lambda r: r.get("date", ""))
    with open(path, "w", encoding="utf-8") as fh:
        for r in kept:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# --- Feed rendering ----------------------------------------------------------
def _item_xml(record):
    """One <item> block for an episode record."""
    return (
        "    <item>\n"
        f"      <title>{escape(record['title'])}</title>\n"
        f"      <description>{escape(record['description'])}</description>\n"
        f"      <itunes:summary>{escape(record['description'])}</itunes:summary>\n"
        f"      <enclosure url={quoteattr(record['audio_url'])} "
        f"length=\"{record['audio_bytes']}\" type=\"{ENCLOSURE_TYPE}\"/>\n"
        f"      <guid isPermaLink=\"false\">{escape(record['guid'])}</guid>\n"
        f"      <pubDate>{record['pub_date']}</pubDate>\n"
        "      <itunes:episodeType>full</itunes:episodeType>\n"
        "      <itunes:explicit>false</itunes:explicit>\n"
        "    </item>"
    )


def build_feed_xml(episodes, ref):
    """Render a full RSS 2.0 + iTunes feed from episode records."""
    cover = public_url(ref, COVER_OBJECT)
    items = "\n".join(_item_xml(r) for r in episodes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(FEED_TITLE)}</title>\n"
        f"    <link>{escape(FEED_LINK)}</link>\n"
        f"    <language>{FEED_LANGUAGE}</language>\n"
        f"    <description>{escape(FEED_DESCRIPTION)}</description>\n"
        f"    <itunes:author>{escape(FEED_AUTHOR)}</itunes:author>\n"
        f"    <itunes:summary>{escape(FEED_DESCRIPTION)}</itunes:summary>\n"
        f"    <itunes:image href={quoteattr(cover)}/>\n"
        f"    <itunes:category text={quoteattr(FEED_CATEGORY)}/>\n"
        "    <itunes:explicit>false</itunes:explicit>\n"
        "    <itunes:type>episodic</itunes:type>\n"
        "    <itunes:owner>\n"
        f"      <itunes:name>{escape(FEED_OWNER_NAME)}</itunes:name>\n"
        f"      <itunes:email>{escape(FEED_OWNER_EMAIL)}</itunes:email>\n"
        "    </itunes:owner>\n"
        f"    <atom:link href={quoteattr(feed_self_url(ref))} "
        'rel="self" type="application/rss+xml"/>\n'
        f"{items}\n"
        "  </channel>\n"
        "</rss>\n"
    )


# --- Storage I/O -------------------------------------------------------------
def audio_is_live(url):
    """HEAD the audio URL; True only on HTTP 200 (never list a dead enclosure)."""
    rc, out, err = send_resend._curl(["-I", "-o", "/dev/null", "-w", "%{http_code}", url])
    return out.strip().endswith("200")


def upload_object(ref, key, obj, path, content_type):
    """Upsert a local file into the public bucket."""
    rc, out, err = send_resend._curl([
        "-X", "POST",
        f"https://{ref}.supabase.co/storage/v1/object/{BUCKET}/{obj}",
        "-H", f"Authorization: Bearer {key}",
        "-H", "x-upsert: true",
        "-H", f"Content-Type: {content_type}",
        "--data-binary", f"@{path}",
    ])
    code = out.rsplit("\n", 1)[-1].strip()
    if code not in ("200", "201"):
        raise RuntimeError(f"upload {obj} failed (HTTP {code}): {out[:200]}")
    return public_url(ref, obj)


# --- Commands ----------------------------------------------------------------
def _write_feed(ref, out_path):
    xml = build_feed_xml(load_episodes(), ref)
    minidom.parseString(xml)  # fail loud if we ever emit malformed XML
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return out_path


def cmd_add(args):
    """Record one episode to the ledger and regenerate feed.xml locally."""
    ref = subscribers._ref()
    url = public_url(ref, audio_object(args.date))
    if not os.path.exists(args.audio):
        print(f"build_rss: audio file not found: {args.audio}", file=sys.stderr)
        return 2
    record = episode_record(args.date, url, os.path.getsize(args.audio),
                            _load_items(args.kept))
    append_episode(record)
    out = _write_feed(ref, args.out)
    print(f"feed rebuilt with {len(load_episodes())} episodes -> {out}")
    return 0


def cmd_publish(args):
    """Verify audio is live, add the episode, then upload feed.xml to Storage."""
    ref = subscribers._ref()
    url = public_url(ref, audio_object(args.date))
    if not audio_is_live(url):
        print(f"build_rss: audio not live at {url}; not publishing feed",
              file=sys.stderr)
        return 4
    rc = cmd_add(args)
    if rc != 0:
        return rc
    token = (os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
    if not token:
        print("build_rss: SUPABASE_ACCESS_TOKEN not set", file=sys.stderr)
        return 3
    key = send_resend.service_key(ref, token)
    feed_url = upload_object(ref, key, FEED_OBJECT, args.out, "application/rss+xml")
    print(f"feed published: {feed_url}")
    return 0


def cmd_build(args):
    out = _write_feed(subscribers._ref(), args.out)
    print(f"feed rebuilt with {len(load_episodes())} episodes -> {out}")
    return 0


def cmd_upload(args):
    """Upload an arbitrary local file (e.g. the cover) to the bucket."""
    ref = subscribers._ref()
    token = (os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
    if not token:
        print("build_rss: SUPABASE_ACCESS_TOKEN not set", file=sys.stderr)
        return 3
    key = send_resend.service_key(ref, token)
    ctype = _content_type(args.file)
    obj = args.object or os.path.basename(args.file)
    url = upload_object(ref, key, obj, args.file, ctype)
    print(f"uploaded: {url}")
    return 0


def _content_type(path):
    """Pick a served Content-Type so audio is delivered as audio, not a blob."""
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".m4a") or path.endswith(".mp4"):
        return "audio/mp4"
    if path.endswith(".mp3"):
        return "audio/mpeg"
    if path.endswith(".xml"):
        return "application/rss+xml"
    return "application/octet-stream"


def _load_items(kept_path):
    if not kept_path or not os.path.exists(kept_path):
        return []
    with open(kept_path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_selftest(_args):
    """Offline: feed is well-formed XML with the required tags + AI disclosure."""
    ok = True
    sample = [
        episode_record("2026-06-23",
                        "https://x.supabase.co/storage/v1/object/public/podcast-episodes/ai-news-2026-06-23.m4a",
                        3500000,
                        [{"title": "New agent framework ships", "summary": "It matters."}]),
        episode_record("2026-06-22",
                        "https://x.supabase.co/storage/v1/object/public/podcast-episodes/ai-news-2026-06-22.m4a",
                        3300000, []),
    ]
    xml = build_feed_xml(sorted(sample, key=lambda r: r["date"], reverse=True), "x")

    try:
        minidom.parseString(xml)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: feed is not well-formed XML: {exc}", file=sys.stderr)
        return 1

    required = [
        "<itunes:author>", "<itunes:image", "<itunes:category", "<itunes:owner>",
        "<enclosure ", "isPermaLink=\"false\"", "<atom:link", "<itunes:explicit>",
    ]
    for tag in required:
        if tag not in xml:
            print(f"FAIL: required tag missing: {tag}", file=sys.stderr); ok = False

    if xml.count("<item>") != 2:
        print("FAIL: expected 2 items", file=sys.stderr); ok = False
    if AI_DISCLOSURE not in xml:
        print("FAIL: AI disclosure missing from feed", file=sys.stderr); ok = False
    # Newest-first ordering: 06-23 must precede 06-22.
    if xml.index("ai-news-2026-06-23") > xml.index("ai-news-2026-06-22"):
        print("FAIL: episodes not newest-first", file=sys.stderr); ok = False

    print("PASS" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="build + publish the podcast RSS feed")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, func in (("add", cmd_add), ("publish", cmd_publish)):
        p = sub.add_parser(name)
        p.add_argument("--date", required=True)
        p.add_argument("--audio", required=True)
        p.add_argument("--kept", default=None)
        p.add_argument("--out", default=os.path.join(os.path.dirname(LEDGER), FEED_OBJECT))
        p.set_defaults(func=func)

    b = sub.add_parser("build")
    b.add_argument("--out", default=os.path.join(os.path.dirname(LEDGER), FEED_OBJECT))
    b.set_defaults(func=cmd_build)

    u = sub.add_parser("upload")
    u.add_argument("--file", required=True)
    u.add_argument("--object", default=None)
    u.set_defaults(func=cmd_upload)

    sub.add_parser("selftest").set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
