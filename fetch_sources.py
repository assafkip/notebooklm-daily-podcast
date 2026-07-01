#!/usr/bin/env python3
"""Deterministic source harvester for the daily AI-news podcast.

Pairs with run_daily.sh step 2. Replaces the old source-blind WebSearch with a
chosen-source pool: it reads sources.json, fetches each source, normalizes every
item to one shape, drops anything outside the freshness window, then pre-ranks
and caps per source for diversity. The curate LLM then SELECTS from this pool
instead of guessing what exists.

Source list + fetch/curate logic adapted from treesoop/ai-news-mcp, retargeted
to this podcast's audience -- their curation excludes some topics this one
keeps, so we steal the plumbing not the filter.

Why stdlib only (scar): this runs under a launchd cron that sources a minimal
env; the host has no pyyaml/feedparser. A missing import would kill the run
silently. urllib + xml.etree + email.utils are always present.

Why each source is wrapped in try/except (their failsafe rule): one flaky feed
must never blank the whole episode. A dead source skips with a stderr note.

Usage:
  fetch_sources.py build --config sources.json --date YYYY-MM-DD --out work/pool-DATE.json
  fetch_sources.py selftest        # exit 0 = PASS, 1 = FAIL (offline, pure logic)

Normalized item: {"title","url","source","summary","published_at","score","topic"}.
"""
import argparse
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

USER_AGENT = "notebooklm-daily-podcast/1.0 (+https://github.com)"
ARCTIC_BASE = "https://arctic-shift.photon-reddit.com"
PULLPUSH_BASE = "https://api.pullpush.io"
HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Pure logic (offline, selftested) -- normalize / window / rank / cap
# ---------------------------------------------------------------------------

def parse_ts(value):
    """Return a UTC epoch float for an epoch / ISO8601 / RFC822 value, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.isdigit():
        return float(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return _to_utc_epoch(dt)
    except ValueError:
        pass
    try:
        return _to_utc_epoch(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        return None


def _to_utc_epoch(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def freshness_boost(published_ts, now_ts):
    """Their freshness signal: <24h boosts, 24-72h neutral, >72h discounts.
    Missing date is neutral -- the trending signal already carried it here."""
    if published_ts is None:
        return 0.0
    hours = (now_ts - published_ts) / 3600.0
    if hours < 24:
        return 3.0
    if hours <= 72:
        return 0.0
    return -3.0


def within_window(items, now_ts, window_hours):
    """Keep ONLY items we can confirm fall inside the window. This is a 48h news
    product, so an item we cannot date is dropped, not kept -- absence of a date
    is not evidence of freshness (scar 2026-06-23: dateless HuggingFace/Anthropic
    items leaked a 7-day-old and a 3-month-old story into a 'last 48h' brief)."""
    kept = []
    for item in items:
        ts = parse_ts(item.get("published_at"))
        if ts is None:
            continue
        if (now_ts - ts) / 3600.0 > window_hours:
            continue
        kept.append(item)
    return kept


def dedup_items(items):
    """Drop exact repeats within this run by canonical url, else lowered title."""
    seen = set()
    out = []
    for item in items:
        url = (item.get("url") or "").split("?")[0].rstrip("/").lower()
        key = url or (item.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def score_item(item, now_ts):
    """Final rank = source weight + freshness boost + a small popularity tiebreak."""
    weight = float(item.get("_weight", 0))
    boost = freshness_boost(parse_ts(item.get("published_at")), now_ts)
    popularity = math.log1p(max(float(item.get("score") or 0), 0)) * 0.5
    return weight + boost + popularity


def rank_and_cap(items, now_ts, default_cap, pool_size):
    """Sort by score desc, enforce the per-source cap, then take the pool_size top."""
    ranked = sorted(items, key=lambda it: score_item(it, now_ts), reverse=True)
    per_source = {}
    capped = []
    for item in ranked:
        source = item.get("source", "")
        cap = int(item.get("_cap", default_cap))
        used = per_source.get(source, 0)
        if used >= cap:
            continue
        per_source[source] = used + 1
        capped.append(item)
    return capped[:pool_size]


def normalize(title, url, source, summary, published_at, score):
    """One shape for every source. Trims summary; topic is the source name."""
    return {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "source": source,
        "summary": re.sub(r"\s+", " ", (summary or "")).strip()[:400],
        "published_at": published_at or "",
        "score": int(score or 0),
        "topic": source,
    }


# ---------------------------------------------------------------------------
# Network IO (not exercised by selftest)
# ---------------------------------------------------------------------------

def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _get_json(url, headers=None):
    return json.loads(_get(url, headers))


def fetch_hackernews(src):
    """HN via Algolia search-by-date; min_points filters out chatter."""
    query = urllib.parse.quote(src.get("query", "AI"))
    url = (f"https://hn.algolia.com/api/v1/search_by_date?tags=story"
           f"&query={query}&hitsPerPage=40")
    data = _get_json(url)
    out = []
    for hit in data.get("hits", []):
        points = hit.get("points") or 0
        if points < src.get("min_points", 0):
            continue
        link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        out.append(normalize(hit.get("title"), link, src["name"], hit.get("story_text"),
                             hit.get("created_at"), points))
    return out


def fetch_reddit(src):
    subs = _reddit_subreddits(src)
    if not subs:
        return []
    return _fetch_reddit_archive(src, subs)


def _reddit_subreddits(src):
    subs = src.get("subreddits")
    if not subs and src.get("subreddit"):
        subs = [src["subreddit"]]
    return [s.lstrip("/").removeprefix("r/").strip() for s in (subs or []) if str(s).strip()]


def _rotate_subs(subs, rotate):
    if not rotate or rotate >= len(subs):
        return subs
    offset = datetime.now(timezone.utc).date().toordinal()
    return [subs[(offset + i) % len(subs)] for i in range(rotate)]


def _fetch_reddit_archive(src, subs):
    subs = _rotate_subs(subs, src.get("rotate"))
    limit = int(src.get("posts_per_sub") or src.get("per_sub") or src.get("limit") or 25)
    lookback_days = int(src.get("lookback_days") or 35)
    after = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    out = []
    for sub in subs:
        for item in _reddit_archive_items(sub, after, limit):
            title = (item.get("title") or "").strip()
            if not title or item.get("stickied"):
                continue
            permalink = item.get("permalink") or ""
            link = f"https://www.reddit.com{permalink}" if permalink else item.get("url", "")
            out.append(normalize(title, link, src["name"], item.get("selftext"),
                                 item.get("created_utc"), item.get("score") or item.get("ups")))
    return out


def _reddit_archive_items(subreddit, after, limit):
    try:
        return _archive_items(_get_json(_arctic_posts_url(subreddit, after, limit)))
    except Exception:
        return _archive_items(_get_json(_pullpush_posts_url(subreddit, limit)))


def _archive_items(payload):
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    return payload if isinstance(payload, list) else []


def _arctic_posts_url(subreddit, after, limit):
    query = urllib.parse.urlencode({"subreddit": subreddit, "after": after, "limit": limit, "sort": "desc"})
    return f"{ARCTIC_BASE}/api/posts/search?{query}"


def _pullpush_posts_url(subreddit, limit):
    query = urllib.parse.urlencode({"subreddit": subreddit, "size": limit, "sort": "desc"})
    return f"{PULLPUSH_BASE}/reddit/search/submission?{query}"


def fetch_rss(src):
    """Generic RSS/Atom via stdlib xml. Handles both <item> and Atom <entry>."""
    root = ElementTree.fromstring(_get(src["url"]))
    out = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        out.append(_rss_node(node, src["name"]))
    return [it for it in out if it["title"]]


def _rss_node(node, source_name):
    def text(*tags):
        for tag in tags:
            for child in node:
                if child.tag.split("}")[-1] == tag and (child.text or child.get("href")):
                    return child.text or child.get("href")
        return ""
    link = text("link")
    return normalize(text("title"), link, source_name,
                     text("description", "summary", "content"),
                     text("pubDate", "published", "updated"), 0)


def fetch_lobsters(src):
    data = _get_json("https://lobste.rs/hottest.json")
    return [normalize(it.get("title"), it.get("url"), src["name"], it.get("description"),
                      it.get("created_at"), it.get("score")) for it in data]


def fetch_huggingface(src):
    data = _get_json("https://huggingface.co/api/models?sort=trendingScore&limit=20")
    out = []
    for m in data:
        mid = m.get("modelId") or m.get("id") or ""
        if not mid:
            continue
        # Use real createdAt so the 48h window gates it: a model trending today
        # but created months ago is NOT 48h news (scar: GLM-5.2 was 7d old and
        # LocateAnything-3B 3mo old, yet both leaked into a "last 48h" brief).
        # Only a genuinely new + trending release survives the window now.
        out.append(normalize(f"Trending model: {mid}", f"https://huggingface.co/{mid}",
                             src["name"], (m.get("pipeline_tag") or ""),
                             m.get("createdAt"), m.get("likes")))
    return out


def fetch_github_trending(src):
    """Best-effort HTML scrape; on any markup change it just returns less."""
    html = _get(f"https://github.com/trending?since={src.get('since', 'daily')}")
    out = []
    for m in re.finditer(r'<h2 class="h3 lh-condensed">\s*<a href="/([^"]+)"', html):
        repo = m.group(1)
        out.append(normalize(f"Trending repo: {repo}", f"https://github.com/{repo}",
                             src["name"], "", "", 0))
    return out


def fetch_apify_x(src):
    """X/Twitter via Apify actor apidojo/tweet-scraper. Gated on APIFY_TOKEN.

    Prefers handle-following (twitter_handles) over broad search_terms -- broad
    terms attract reply-bot spam, named voices do not. A `start` date keeps the
    run cheap by only pulling the recency window, not a handle's whole history.
    """
    import os
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print(f"  skip {src['name']}: APIFY_TOKEN not set", file=sys.stderr)
        return []

    handles = [h.lstrip("@").strip() for h in src.get("twitter_handles", []) if h.strip()]
    days_back = src.get("days_back", 2)
    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    actor_input = {
        "maxItems": src.get("max_items", 60),
        "sort": "Latest",
        "start": start_date,
    }
    if handles:
        actor_input["twitterHandles"] = handles
    if src.get("search_terms"):
        actor_input["searchTerms"] = src["search_terms"]

    url = ("https://api.apify.com/v2/acts/apidojo~tweet-scraper/"
           f"run-sync-get-dataset-items?token={token}")
    req = urllib.request.Request(url, data=json.dumps(actor_input).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        tweets = json.loads(resp.read().decode("utf-8", "replace"))

    # Drop retweets (no original signal) and cap per author so one chatty
    # account can't flood the X slot -- scar: jackfriks took 5 of 6 slots with
    # RTs and outage jokes on the first handle-based run (2026-06-23).
    per_author_cap = src.get("per_author_cap", 2)
    out = []
    author_counts = {}
    for t in tweets:
        text = (t.get("text") or "").strip()
        if not text or text.startswith("RT @") or t.get("isRetweet"):
            continue
        author = ((t.get("author") or {}).get("userName")) or t.get("authorUsername") or ""
        if author_counts.get(author, 0) >= per_author_cap:
            continue
        author_counts[author] = author_counts.get(author, 0) + 1
        likes = (t.get("likeCount") or 0) + (t.get("retweetCount") or 0)
        title = (f"@{author}: " if author else "") + text[:120]
        summary = (f"via @{author}: " if author else "") + text
        out.append(normalize(title, t.get("url"), src["name"], summary,
                             t.get("createdAt"), likes))
    return out


def fetch_reddit_apify(src):
    """Backward-compatible source type.

    The old Apify actor path timed out or required proxy credentials. Keep the
    config key working, but route it through Arctic Shift with PullPush fallback.
    """
    subs = _reddit_subreddits(src)
    if not subs:
        return []
    return _fetch_reddit_archive(src, subs)


def fetch_anthropic(src):
    """Anthropic has no RSS; its /news page is server-rendered with post slugs in
    document order (newest first). Scrape the first N slugs, then fetch each
    article for its real datePublished so the 48h window gates it like every
    other source (scar: a no-date source bypassed the window entirely)."""
    html = _get("https://www.anthropic.com/news")
    seen = []
    for m in re.finditer(r'/news/([a-z0-9][a-z0-9-]+)', html):
        slug = m.group(1)
        if slug not in seen:
            seen.append(slug)
        if len(seen) >= src.get("max_items", 6):
            break
    out = []
    for slug in seen:
        url = f"https://www.anthropic.com/news/{slug}"
        out.append(normalize("Anthropic: " + slug.replace("-", " "), url,
                             src["name"], "", _anthropic_date(url), 0))
    return out


def _anthropic_date(url):
    """Pull the article's publish date (JSON-LD datePublished, else a bare date).
    Returns '' if the page can't be read -- the window then drops it (no date)."""
    try:
        page = _get(url)
    except Exception:
        return ""
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page) or re.search(r'(20\d\d-\d\d-\d\d)', page)
    return m.group(1) if m else ""


FETCHERS = {
    "hackernews": fetch_hackernews,
    "reddit": fetch_reddit,
    "reddit_apify": fetch_reddit_apify,
    "anthropic": fetch_anthropic,
    "rss": fetch_rss,
    "lobsters": fetch_lobsters,
    "huggingface": fetch_huggingface,
    "github_trending": fetch_github_trending,
    "apify_x": fetch_apify_x,
}


def collect(config, now_ts):
    """Fetch every enabled source, tag items with rank inputs, never let one die."""
    items = []
    for src in config.get("sources", []):
        if not src.get("enabled"):
            continue
        fetcher = FETCHERS.get(src.get("type"))
        if fetcher is None:
            print(f"  skip {src.get('name')}: unknown type {src.get('type')}", file=sys.stderr)
            continue
        try:
            got = fetcher(src)
        except Exception as exc:  # failsafe: a dead source is skipped, not fatal
            print(f"  skip {src.get('name')}: {exc}", file=sys.stderr)
            continue
        for it in got:
            it["_weight"] = src.get("weight", 0)
            it["_cap"] = src.get("cap", config.get("default_cap", 5))
        print(f"  {src['name']}: {len(got)} items", file=sys.stderr)
        items.extend(got)
    return items


def build_pool(config, now_ts):
    window_hours = config.get("window_hours", 48)
    pool_size = config.get("pool_size", 30)
    default_cap = config.get("default_cap", 5)
    items = collect(config, now_ts)
    items = dedup_items(items)
    items = within_window(items, now_ts, window_hours)
    pool = rank_and_cap(items, now_ts, default_cap, pool_size)
    for it in pool:  # drop internal rank fields before handing off
        it.pop("_weight", None)
        it.pop("_cap", None)
    return pool


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_build(args):
    with open(args.config, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    # The 48h window MUST anchor to real now, never to a passed date. Scar
    # 2026-06-23: anchoring to noon-of-DATE measured the window up to a full day
    # behind reality, so a >48h story slipped into a "last 48h" brief. --date is
    # a label only (the brief header / filenames), it never moves the window.
    now_ts = datetime.now(timezone.utc).timestamp()
    pool = build_pool(config, now_ts)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(pool, fh, ensure_ascii=False, indent=2)
    print(f"-- pool: {len(pool)} items -> {args.out} --", file=sys.stderr)
    return 0 if pool else 1


def cmd_selftest(_args):
    """Reproducer for the pure ranking logic, fully offline.
    PASS proves: a >window item is dropped, the per-source cap holds, and a
    fresh item outranks a stale one. Negative self-test: it MUST drop something."""
    now = 1_000_000.0  # fixed 'now' epoch
    hour = 3600.0
    ok = True

    items = [
        {"title": "fresh A", "url": "u1", "source": "src1", "published_at": now - 2 * hour,
         "score": 10, "_weight": 5, "_cap": 2},
        {"title": "fresh B", "url": "u2", "source": "src1", "published_at": now - 3 * hour,
         "score": 5, "_weight": 5, "_cap": 2},
        {"title": "fresh C", "url": "u3", "source": "src1", "published_at": now - 4 * hour,
         "score": 1, "_weight": 5, "_cap": 2},
        {"title": "popular-older D", "url": "u4", "source": "src2", "published_at": now - 40 * hour,
         "score": 99, "_weight": 5, "_cap": 2},
        {"title": "old-window E", "url": "u5", "source": "src2", "published_at": now - 50 * hour,
         "score": 1, "_weight": 5, "_cap": 2},
        {"title": "no-date F", "url": "u6", "source": "src2", "published_at": "",
         "score": 50, "_weight": 5, "_cap": 2},
    ]

    windowed = within_window(items, now, window_hours=48)
    titles = [it["title"] for it in windowed]
    if "old-window E" in titles:
        print("FAIL: a >48h item survived the window", file=sys.stderr)
        ok = False
    if "no-date F" in titles:
        print("FAIL: an undateable item survived the window (must drop)", file=sys.stderr)
        ok = False
    if "fresh A" not in titles:
        print("FAIL: a fresh item was dropped by the window", file=sys.stderr)
        ok = False

    pool = rank_and_cap(windowed, now, default_cap=5, pool_size=10)
    src1_count = sum(1 for it in pool if it["source"] == "src1")
    if src1_count > 2:
        print(f"FAIL: per-source cap broken (src1={src1_count}, want <=2)", file=sys.stderr)
        ok = False

    # freshness must outrank raw popularity: fresh A (2h, score 10) beats
    # popular-older D (40h, score 99) within the same window
    order = [it["title"] for it in pool]
    if "popular-older D" in order and order.index("fresh A") > order.index("popular-older D"):
        print("FAIL: popular-but-older outranked fresh", file=sys.stderr)
        ok = False

    # negative self-test: the pipeline MUST have removed items (not a no-op)
    if len(pool) >= len(items):
        print("FAIL: pipeline dropped nothing (no-op)", file=sys.stderr)
        ok = False

    # date parsing across formats
    if parse_ts("2026-06-23T10:00:00Z") is None:
        print("FAIL: ISO8601 date did not parse", file=sys.stderr)
        ok = False
    if parse_ts("Mon, 23 Jun 2026 10:00:00 GMT") is None:
        print("FAIL: RFC822 date did not parse", file=sys.stderr)
        ok = False

    original_get_json = globals()["_get_json"]
    requested_urls = []

    def fake_get_json(url, headers=None):
        requested_urls.append(url)
        if "arctic-shift.photon-reddit.com/api/posts/search" in url:
            raise RuntimeError("arctic blocked")
        if "api.pullpush.io/reddit/search/submission" not in url:
            raise AssertionError(f"unexpected Reddit URL: {url}")
        return {
            "data": [
                {
                    "id": "rd1",
                    "subreddit": "ClaudeCode",
                    "author": "builder",
                    "title": "MCP eval harness launch",
                    "selftext": "New agent eval harness pattern.",
                    "permalink": "/r/ClaudeCode/comments/rd1/mcp_eval_harness/",
                    "score": 42,
                    "num_comments": 7,
                    "created_utc": now,
                }
            ]
        }

    try:
        globals()["_get_json"] = fake_get_json
        reddit_items = fetch_reddit({"name": "reddit-tools", "subreddit": "ClaudeCode", "limit": 2})
        if not reddit_items or reddit_items[0]["title"] != "MCP eval harness launch":
            print("FAIL: Reddit Arctic/PullPush collector returned no normalized items", file=sys.stderr)
            ok = False
        if not requested_urls or "arctic-shift.photon-reddit.com/api/posts/search" not in requested_urls[0]:
            print("FAIL: Reddit collector did not try Arctic Shift first", file=sys.stderr)
            ok = False
        if len(requested_urls) < 2 or "api.pullpush.io/reddit/search/submission" not in requested_urls[1]:
            print("FAIL: Reddit collector did not fall back to PullPush", file=sys.stderr)
            ok = False
    except Exception as exc:
        print(f"FAIL: Reddit Arctic/PullPush selftest failed: {exc}", file=sys.stderr)
        ok = False
    finally:
        globals()["_get_json"] = original_get_json

    print("PASS" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="harvest podcast source pool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build")
    build.add_argument("--config", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--date", default="")
    build.set_defaults(func=cmd_build)

    selftest = sub.add_parser("selftest")
    selftest.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
