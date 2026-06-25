# notebooklm-daily-podcast

A daily AI-news podcast that runs itself. It harvests news from sources you
choose, has Claude pick the few stories worth airing, makes sure nothing repeats,
turns them into a two-host audio episode with Google NotebookLM, and (optionally)
emails subscribers and publishes a podcast RSS feed.

Clone it, point it at your own sources, and it is your show. Nothing here is tied
to anyone else's brand. Every choice that makes it *yours* lives in one config
file.

## What it does (the daily chain)

```
auth gate -> harvest sources -> Claude curates the best 4-5 -> dedup (no repeats)
          -> build a brief -> NotebookLM makes the audio -> [optional] deliver
          -> record what aired so tomorrow stays fresh
```

The default setup stops at the audio file. Delivery (email + podcast feed) is
opt-in, so your first run needs zero hosting and zero subscriber list.

## Requirements

- **macOS or Linux** with `python3` (stdlib only, no pip packages for the core),
  `bash`, and `curl`.
- **Claude Code** (the `claude` CLI) with an Anthropic API key. This is the
  curation brain. https://docs.claude.com/claude-code
- **NotebookLM CLI** (`nlm`, from notebooklm-mcp-cli) logged into a Google
  account. This is the audio engine.
- *(optional, for delivery)* a Supabase project + a Resend account.
- *(optional, for extra sources)* an Apify token for X/Twitter and some subreddits.

## Setup (about 10 minutes)

```bash
# 1. configuration
cp config.example.json config.json     # your show name, audience, host persona
cp sources.example.json sources.json   # where the news comes from
cp .env.example .env                   # your API keys

# 2. install the two engines
#    Claude Code:  https://docs.claude.com/claude-code
#    NotebookLM:   the `nlm` CLI, then `nlm login` (sign into your Google account)

# 3. prove it before you trust it (offline, no creds)
bash tests/run_selftests.sh

# 4. run it
bash run_daily.sh
```

The first run drops a `.m4a` episode in `episodes/`. That is the whole loop
working. Wire delivery only once you want it.

## Make it yours: `config.json`

| Key | What it controls |
|-----|------------------|
| `show_name` | Episode + email + feed title |
| `audience` | Who the hosts are talking to. Drives what Claude keeps and how the brief is framed. The single most important knob. |
| `host_persona` | The curator's voice in the selection prompt |
| `forbidden_terms` | Words/brands Claude must never mention (e.g. competitors) |
| `window_hours` | Freshness window. Default 48h. Anything older is dropped. |
| `delivery` | `none` (build only) or `resend` (also email + publish RSS) |
| `owner_name` / `owner_email` / `show_link` | Used in the podcast RSS feed |

## Choose your sources: `sources.json`

One JSON entry per source. Types: `hackernews`, `reddit`, `rss`, `lobsters`,
`huggingface`, `github_trending`, `anthropic`, plus `apify_x` (X/Twitter handles)
and `reddit_apify` (needs `APIFY_TOKEN`). A dead source skips silently; it never
blanks a run. The shipped default targets new AI-coding tools and agentic
workflows. Swap in feeds for your topic.

## Turning on delivery (optional)

Set `"delivery": "resend"` in `config.json` and fill the delivery keys in `.env`:

- **Supabase** hosts the audio and stores subscribers. Set `SUPABASE_ACCESS_TOKEN`
  and your project ref (`SUPABASE_PROJECT_REF` or `supabase.json`). You need a
  `subscribers` table (email, name, source, status, unsubscribe_token, created_at)
  and a public storage bucket for the audio.
- **Resend** sends the email. Set `RESEND_API_KEY` and `PODCAST_FROM`.
- Manage subscribers with `python3 subscribers.py add --email you@example.com`.
- **Listen analytics** are off by default. Set `PODCAST_POSTHOG_KEY` to turn them on.

The podcast RSS feed (`build_rss.py`) publishes automatically once delivery is on,
so Apple Podcasts / Spotify / Overcast can subscribe.

## Run it daily

Put `run_daily.sh` on a `cron` (Linux) or `launchd` (macOS) timer. It must run on
the machine where you ran `nlm login` (the NotebookLM cookies live there). Set
`SLACK_WEBHOOK` in `.env` and it pings you if a run fails or auth expires.

## Why it is built this way

- **Scripts over prompts.** You choose sources and audience in config files, not
  by editing a buried prompt. The only LLM step is curation; everything else is
  deterministic and testable.
- **Freshness + no-repeat are gates, not hopes.** A dated-and-within-window check
  runs on every harvested item, and a ledger blocks any story that already aired.
  Both have offline selftests.
- **Honest by default.** Episodes disclose AI-generated audio. Delivery and
  analytics are opt-in, not sprung on you.

## License

MIT. Take it and run.
