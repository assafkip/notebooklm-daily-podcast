#!/usr/bin/env python3
"""Deterministic dedup gate for the daily NotebookLM AI-news podcast.

Pairs with run_daily.sh: the curate step is judgment (Claude researches the
48h window); THIS file is the deterministic slice -- it decides what counts as
a repeat, so the LLM can't talk its way past "you already covered that."
Proven across hundreds of episodes: no story repeats across days.

Why a separate gate (scar): an LLM asked to "not repeat yourself" will
happily re-run the same OpenAI headline two days straight because each day's
context is fresh. The ledger is the only thing that remembers across days.
Single-writer rule: ONLY `commit` writes the ledger. The research step calls
`filter` (read-only) to get its keep-list, builds the episode, then calls
`commit` once to record exactly what aired.

Commands:
  filter  --ledger L --candidates C [--days N] [--jaccard T] [--date D]
          read candidates JSON (list of items), print KEPT items as JSON to
          stdout, drop anything covered within the trailing N days (exact key
          match OR fuzzy title match) and any in-batch duplicate.
  commit  --ledger L --items I [--date D]
          append the aired items to the ledger, stamped with the date.
  selftest
          run the built-in reproducer: prove a repeat is blocked and a fresh
          item passes. exit 0 = PASS, 1 = FAIL.

Item shape (JSON object): {"title": str, "url": str?, "summary": str?,
"source": str?, "topic": str?}. Only `title` is required.
"""
import argparse
import datetime as dt
import json
import re
import sys
from urllib.parse import urlsplit

DEFAULT_DAYS = 7          # "2 days in a row or more" + margin; a topic rests a week
DEFAULT_JACCARD = 0.6     # title token overlap above this == same story

_STOP = {
    "a", "an", "the", "to", "of", "in", "on", "and", "for", "is", "are",
    "with", "as", "at", "by", "from", "how", "why", "what", "new", "now",
    "this", "that", "its", "it", "you", "your", "ai", "model", "models",
}


def canonical_url(url: str) -> str:
    """Lowercase host, drop www/query/fragment/trailing slash. Same article
    shared with different ?utm= tags collapses to one key."""
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")
    if not host:
        return url.strip().lower()
    return f"{host}{path}".lower()


def norm_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_tokens(title: str) -> set:
    return {w for w in norm_title(title).split() if w not in _STOP and len(w) > 2}


def item_key(item: dict) -> str:
    """Stable identity for an item: canonical URL if present, else title hash."""
    u = canonical_url(item.get("url", ""))
    if u:
        return f"url:{u}"
    return f"title:{norm_title(item.get('title', ''))}"


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def load_recent(ledger_path: str, today: dt.date, days: int):
    """Return (keys, token_sets) covered within the trailing `days` window."""
    cutoff = today - dt.timedelta(days=days)
    keys = set()
    token_sets = []
    try:
        with open(ledger_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # never let one bad line blind the whole gate
                d = rec.get("date")
                try:
                    rec_date = _parse_date(d) if d else None
                except ValueError:
                    rec_date = None
                if rec_date is not None and rec_date < cutoff:
                    continue
                keys.add(rec.get("key") or item_key(rec))
                token_sets.append(title_tokens(rec.get("title", "")))
    except FileNotFoundError:
        pass
    return keys, token_sets


def filter_items(candidates, ledger_path, today, days, jac_threshold):
    recent_keys, recent_tokens = load_recent(ledger_path, today, days)
    kept, dropped = [], []
    seen_keys = set()
    seen_tokens = []
    for item in candidates:
        key = item_key(item)
        toks = title_tokens(item.get("title", ""))
        # in-batch exact dupe
        if key in seen_keys:
            dropped.append((item, "dupe-in-batch (exact)"))
            continue
        # in-batch near dupe
        if any(jaccard(toks, t) >= jac_threshold for t in seen_tokens):
            dropped.append((item, "dupe-in-batch (near)"))
            continue
        # covered recently (exact)
        if key in recent_keys:
            dropped.append((item, f"covered in last {days}d (exact)"))
            continue
        # covered recently (near)
        if any(jaccard(toks, t) >= jac_threshold for t in recent_tokens):
            dropped.append((item, f"covered in last {days}d (near)"))
            continue
        kept.append(item)
        seen_keys.add(key)
        seen_tokens.append(toks)
    return kept, dropped


def cmd_filter(args):
    today = _parse_date(args.date) if args.date else dt.date.today()
    with open(args.candidates, "r", encoding="utf-8") as fh:
        candidates = json.load(fh)
    if not isinstance(candidates, list):
        print("candidates file must be a JSON array", file=sys.stderr)
        return 2
    kept, dropped = filter_items(
        candidates, args.ledger, today, args.days, args.jaccard
    )
    for item, reason in dropped:
        print(f"DROP  {reason:32s} | {item.get('title','')[:80]}", file=sys.stderr)
    print(f"-- kept {len(kept)} / {len(candidates)} candidates --", file=sys.stderr)
    json.dump(kept, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_commit(args):
    today = args.date if args.date else dt.date.today().isoformat()
    with open(args.items, "r", encoding="utf-8") as fh:
        items = json.load(fh)
    if not isinstance(items, list):
        print("items file must be a JSON array", file=sys.stderr)
        return 2
    n = 0
    with open(args.ledger, "a", encoding="utf-8") as fh:
        for item in items:
            rec = {
                "date": today,
                "key": item_key(item),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source": item.get("source", ""),
                "topic": item.get("topic", ""),
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"committed {n} items to {args.ledger} (date {today})", file=sys.stderr)
    return 0


def cmd_selftest(_args):
    """Reproducer: yesterday we aired 'OpenAI ships X'. Today the candidates
    include that same story (different URL/wording) plus a genuinely new one.
    PASS = old story dropped, new story kept."""
    import os
    import tempfile

    today = dt.date(2026, 6, 17)
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    fd, ledger = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    ok = True
    try:
        with open(ledger, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "date": yesterday,
                "key": item_key({"title": "OpenAI ships new agent SDK",
                                 "url": "https://openai.com/blog/agent-sdk"}),
                "title": "OpenAI ships new agent SDK",
                "url": "https://openai.com/blog/agent-sdk",
            }) + "\n")

        candidates = [
            # same story, reworded headline + utm tag -> must DROP (fuzzy)
            {"title": "OpenAI ships its new agent SDK for developers",
             "url": "https://openai.com/blog/agent-sdk?utm_source=x"},
            # genuinely new -> must KEEP
            {"title": "Anthropic releases Claude memory tool for long context",
             "url": "https://anthropic.com/news/memory"},
            # in-batch dupe of the kept one -> must DROP
            {"title": "Anthropic releases a Claude memory tool for long context",
             "url": "https://anthropic.com/news/memory?ref=feed"},
        ]
        kept, dropped = filter_items(candidates, ledger, today,
                                     DEFAULT_DAYS, DEFAULT_JACCARD)
        kept_titles = [k["title"] for k in kept]

        if any("OpenAI" in t for t in kept_titles):
            print("FAIL: yesterday's OpenAI story was not blocked", file=sys.stderr)
            ok = False
        if not any("memory tool" in t for t in kept_titles):
            print("FAIL: the new Anthropic story was dropped", file=sys.stderr)
            ok = False
        if len(kept) != 1:
            print(f"FAIL: expected exactly 1 kept, got {len(kept)}", file=sys.stderr)
            ok = False
        # negative self-test: prove the gate is not a no-op (it MUST drop some)
        if len(dropped) != 2:
            print(f"FAIL: expected 2 dropped, got {len(dropped)}", file=sys.stderr)
            ok = False
    finally:
        os.unlink(ledger)

    print("PASS" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="dedup gate for ai-news-podcast")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("filter")
    f.add_argument("--ledger", required=True)
    f.add_argument("--candidates", required=True)
    f.add_argument("--days", type=int, default=DEFAULT_DAYS)
    f.add_argument("--jaccard", type=float, default=DEFAULT_JACCARD)
    f.add_argument("--date", default="")
    f.set_defaults(func=cmd_filter)

    c = sub.add_parser("commit")
    c.add_argument("--ledger", required=True)
    c.add_argument("--items", required=True)
    c.add_argument("--date", default="")
    c.set_defaults(func=cmd_commit)

    s = sub.add_parser("selftest")
    s.set_defaults(func=cmd_selftest)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
