#!/usr/bin/env python3
"""Deliver the daily episode via Resend (off Gmail), one email per subscriber.

Flow: upload the m4a to Supabase Storage (public link, no attachment) -> fetch
active subscribers + their unsubscribe_token -> render the Editorial brief with
the play link and a PER-RECIPIENT unsubscribe link -> POST each to Resend with a
List-Unsubscribe header (the native one-click button in Gmail/Apple Mail).

Per-recipient sends, not one blast: each gets their own unsubscribe link, and no
one sees the rest of the list. The unsubscribe link points at the Supabase Edge
Function, which writes straight to the subscribers table.

One secret reused: SUPABASE_ACCESS_TOKEN (PAT) -> fetches the service_role key at
runtime for the Storage upload (never persisted). Plus RESEND_API_KEY for sending.
Both from .env. curl everywhere (Cloudflare blocks
urllib on api.supabase.com).

Usage:
  send_resend.py --audio <m4a> --kept <kept.json> --date YYYY-MM-DD [--subject S]
  send_resend.py selftest
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

import build_email_html
import build_episode_page
import subscribers

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = os.environ.get("PODCAST_FROM", "AI News Daily <onboarding@resend.dev>")
TIMEOUT = 90


def _recipient_id(email):
    """Non-sensitive, stable per-person id for PostHog (NOT the unsubscribe token)."""
    return hashlib.sha256((email or "").encode("utf-8")).hexdigest()[:16]


def _curl(args, payload=None, payload_file=None):
    """Run curl, return (rc, stdout). payload via stdin, payload_file via --data-binary."""
    cmd = ["curl", "-s", "-w", "\n%{http_code}"] + args
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                          timeout=TIMEOUT)
    return proc.returncode, proc.stdout, proc.stderr


def service_key(ref, token):
    """Fetch the project's service_role key via the Management API (PAT)."""
    rc, out, err = _curl([
        "-H", f"Authorization: Bearer {token}",
        f"https://api.supabase.com/v1/projects/{ref}/api-keys",
    ])
    body = out.rsplit("\n", 1)[0]
    keys = json.loads(body)
    for k in keys:
        if k.get("name") == "service_role":
            return k.get("api_key") or k.get("secret") or ""
    raise RuntimeError("service_role key not found")


BUCKET = "podcast-episodes"  # single source for the Storage bucket name + URL shape


def upload_object(ref, key, obj, content_type, *, path=None, html_str=None):
    """Upsert one object into the public Storage bucket; return its public URL.

    One uploader for every episode artifact. Pass exactly one of path (a file,
    streamed via --data-binary @file) or html_str (an in-memory body via stdin).
    """
    src = ["--data-binary", f"@{path}"] if path is not None else ["--data-binary", "@-"]
    rc, out, err = _curl([
        "-X", "POST",
        f"https://{ref}.supabase.co/storage/v1/object/{BUCKET}/{obj}",
        "-H", f"Authorization: Bearer {key}",
        "-H", "x-upsert: true",
        "-H", f"Content-Type: {content_type}",
    ] + src, payload=html_str)
    code = out.rsplit("\n", 1)[-1].strip()
    if code not in ("200", "201"):
        raise RuntimeError(f"upload failed ({obj}, HTTP {code}): {out[:200]}")
    return f"https://{ref}.supabase.co/storage/v1/object/public/{BUCKET}/{obj}"


def upload_audio(ref, key, path, date):
    """Upload the m4a to the public bucket; return its public URL."""
    return upload_object(ref, key, f"ai-news-{date}.m4a", "audio/mp4", path=path)


def upload_page(ref, key, html_str, date):
    """Upload the tracked episode HTML page (text/html so it renders, not
    downloads). The email "Listen" button points here."""
    return upload_object(ref, key, f"ai-news-{date}.html",
                         "text/html; charset=utf-8", html_str=html_str)


# The episode player page is served from VERCEL, not Supabase. WHY: the whole
# *.supabase.co domain force-serves ALL HTML as text/plain + nosniff (anti-XSS on
# their shared domain), so NEITHER storage NOR an edge function can render the page;
# a browser shows the source instead of playing it (scar 2026-06-25). The Vercel
# endpoint (your own deploy) fetches the page the pipeline stored in Supabase
# Storage and re-serves it as real text/html. Override via PODCAST_EPISODE_BASE.
EPISODE_BASE = (os.environ.get("PODCAST_EPISODE_BASE")
                or "")  # set PODCAST_EPISODE_BASE to your player-page deploy


def listen_link(ref, date, email):
    """Per-recipient tracked link to the episode player page (served from Vercel).
    The page JS reads ?e (episode) + ?r (recipient); ?d tells the endpoint which page
    to serve. ref is kept for signature compatibility."""
    return f"{EPISODE_BASE}?d={date}&e={date}&r={_recipient_id(email)}"


def unsubscribe_url(ref, token):
    return f"https://{ref}.supabase.co/functions/v1/unsubscribe?token={token}"


def send_one(resend_key, to_email, subject, html_body, unsub_link, sender):
    payload = json.dumps({
        "from": sender,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "headers": {
            "List-Unsubscribe": f"<{unsub_link}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    })
    rc, out, err = _curl([
        "-X", "POST", RESEND_URL,
        "-H", f"Authorization: Bearer {resend_key}",
        "-H", "Content-Type: application/json",
        "--data", "@-",
    ], payload=payload)
    code = out.rsplit("\n", 1)[-1].strip()
    body = out.rsplit("\n", 1)[0]
    return code, body


def cmd_send(args):
    resend_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not resend_key:
        print("send_resend: RESEND_API_KEY not set", file=sys.stderr)
        return 3
    token = (os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
    if not token:
        print("send_resend: SUPABASE_ACCESS_TOKEN not set", file=sys.stderr)
        return 3
    sender = (os.environ.get("PODCAST_FROM") or DEFAULT_FROM).strip()
    ref = subscribers._ref()

    with open(args.kept, encoding="utf-8") as fh:
        items = json.load(fh)

    key = service_key(ref, token)
    audio_url = upload_audio(ref, key, args.audio, args.date)
    print(f"audio hosted: {audio_url}", file=sys.stderr)

    # Build + host the tracked episode page; the email "Listen" button points here
    # (not at the raw m4a) so PostHog can record listens. Public ingestion key,
    # overridable via env.
    ph_key = os.environ.get("PODCAST_POSTHOG_KEY", build_episode_page.DEFAULT_POSTHOG_KEY).strip()
    ph_host = os.environ.get("PODCAST_POSTHOG_HOST", build_episode_page.DEFAULT_POSTHOG_HOST).strip()
    page_html = build_episode_page.build_page(items, args.date, audio_url=audio_url,
                                              posthog_key=ph_key, posthog_host=ph_host)
    page_url = upload_page(ref, key, page_html, args.date)
    print(f"page hosted: {page_url}", file=sys.stderr)

    rows = subscribers.run_query(
        "select email, unsubscribe_token from subscribers "
        "where status = 'active' order by created_at asc"
    )
    if not rows:
        print("send_resend: no active subscribers", file=sys.stderr)
        return 4

    sent, failed = 0, 0
    for row in rows:
        email = row.get("email")
        unsub = unsubscribe_url(ref, row.get("unsubscribe_token"))
        html_body = build_email_html.build_html(items, args.date,
                                                 audio_url=listen_link(ref, args.date, email),
                                                 unsubscribe_url=unsub)
        code, body = send_one(resend_key, email, args.subject, html_body, unsub, sender)
        if code in ("200", "201"):
            sent += 1
            print(f"sent -> {email}", file=sys.stderr)
        else:
            failed += 1
            print(f"FAIL -> {email} (HTTP {code}): {body[:200]}", file=sys.stderr)

    print(f"resend: {sent} sent, {failed} failed")
    return 0 if failed == 0 else 4


def cmd_selftest(_args):
    """Offline: URL building, payload shape, missing-key handling."""
    ok = True

    u = unsubscribe_url("abc123", "tok-xyz")
    if u != "https://abc123.supabase.co/functions/v1/unsubscribe?token=tok-xyz":
        print(f"FAIL: unsub url: {u}", file=sys.stderr); ok = False

    # per-recipient tracked link: episode served via the edge function (text/html),
    # carrying episode + stable recipient hash, NOT the unsubscribe token
    link = listen_link("abc123", "2026-06-24", "reader@example.com")
    rid = _recipient_id("reader@example.com")
    expect = f"{EPISODE_BASE}?d=2026-06-24&e=2026-06-24&r={rid}"
    if link != expect:
        print(f"FAIL: listen link: {link}", file=sys.stderr); ok = False
    if len(rid) != 16 or rid == "reader@example.com":
        print(f"FAIL: recipient id not a 16-char hash: {rid}", file=sys.stderr); ok = False
    if _recipient_id("reader@example.com") != rid:
        print("FAIL: recipient id not stable", file=sys.stderr); ok = False
    if _recipient_id("other@example.com") == rid:
        print("FAIL: recipient id collides across emails", file=sys.stderr); ok = False

    saved = os.environ.pop("RESEND_API_KEY", None)
    try:
        if cmd_send(argparse.Namespace(audio="x", kept="x", date="2026-06-23",
                                       subject="s")) != 3:
            print("FAIL: missing RESEND_API_KEY should return 3", file=sys.stderr)
            ok = False
    finally:
        if saved is not None:
            os.environ["RESEND_API_KEY"] = saved

    print("PASS" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="send daily episode via Resend")
    sub = parser.add_subparsers(dest="cmd", required=True)

    send = sub.add_parser("send")
    send.add_argument("--audio", required=True)
    send.add_argument("--kept", required=True)
    send.add_argument("--date", required=True)
    send.add_argument("--subject", default="Daily AI News Pod")
    send.set_defaults(func=cmd_send)

    sub.add_parser("selftest").set_defaults(func=cmd_selftest)

    # default subcommand "send" if flags passed without it
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
