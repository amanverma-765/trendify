<div align="center">

# Trendify

**A GitHub trending → Telegram notifier that runs itself.**

Trendify watches the [GitHub trending page](https://github.com/trending) every 30 minutes
and posts a clean, card-style Telegram message the moment a new repository starts trending.

<br>

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/Runs%20on-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)
![Telegram](https://img.shields.io/badge/Delivers%20to-Telegram-26A5E4?logo=telegram&logoColor=white)
![No server](https://img.shields.io/badge/Infra-Serverless-success)

<br>

### 📢 Want the updates without running anything?

**[Join the Trendify channel on Telegram →](https://t.me/git_trendify)**

A live feed of new trending repositories, posted automatically.

</div>

---

## What it does

Every 30 minutes, a GitHub Actions cron job scrapes the trending page, figures out which
repositories are **newly** trending, and sends each one to your Telegram channel as a
rich photo card — the repository's own GitHub preview image on top, with the details
below it.

Each notification looks like this:

> **[ card image of the repository ]**
>
> **New Trending Repo**
>
> **owner/repo** ← bold, tappable link
>
> The repository's description
>
> **Language:** TypeScript
> **Stars:** ⭐ 69,011
> **Stars Today:** ⭐ +4,349
>
> ↗ View on GitHub

---

## Why it's reliable

- **No API, but never silent.** GitHub has no official trending API, so Trendify scrapes
  the HTML. If GitHub ever changes its markup, the run **fails loudly** (a red build)
  instead of quietly reporting nothing.
- **No duplicate spam.** Seen repositories are tracked in [`state/seen.json`](state/seen.json)
  as a **sliding 24-hour window** — every time a repo appears, its timestamp is refreshed.
  A repo only notifies again after it has been **off the trending list for a full day**, so
  the same project won't ping you twice for one trending streak.
- **Self-hosting state.** After each run the workflow commits the updated state back to the
  repository. Those commits double as activity, so GitHub never auto-disables the scheduled
  workflow for inactivity.
- **Never loses a notification.** A repo is only recorded as *seen* after its message sends
  successfully. A failed send simply retries on the next run.
- **Graceful image fallback.** Trendify downloads each repository's preview image itself
  (retrying if GitHub rate-limits) and uploads it to Telegram directly — Telegram's own
  URL fetcher is easily rate-limited by GitHub. If the image still can't be fetched,
  it falls back to a plain text message so the alert always goes out.

---

## How it works

```
┌─────────────────────────┐
│  GitHub Actions (cron)   │   every 30 minutes
│      7,37 * * * *        │
└────────────┬────────────┘
             │
             ▼
   scrape github.com/trending           →  ~15–25 repos
             │
             ▼
   diff against state/seen.json         →  which are new?
             │
             ▼
   send Telegram photo card per new repo
             │
             ▼
   refresh + prune state (24h window)
             │
             ▼
   commit state/seen.json back to repo
```

---

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Follow the prompts and copy the **bot token** it gives you.

### 2. Get your chat ID

- **For a channel:** add the bot to your channel as an admin, then read the channel's
  numeric ID (e.g. `-1001234567890`).
- **For a direct chat:** send your bot any message, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`.

### 3. Deploy

1. Push this project to a GitHub repository.
2. In the repo, go to **Settings → Secrets and variables → Actions** and add:

   | Secret | Value |
   | ------ | ----- |
   | `TELEGRAM_BOT_TOKEN` | your BotFather token |
   | `TELEGRAM_CHAT_ID`   | your channel / chat ID |

3. Open the **Actions** tab → **trendify** → **Run workflow** to trigger the first run
   manually and confirm it works. After that, the cron takes over automatically.

---

## Run locally

```sh
pip install -r requirements.txt

# preview messages in the terminal — no Telegram, no secrets needed
DRY_RUN=1 python main.py

# send for real
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python main.py
```

---

## Configuration

All settings are constants at the top of [`main.py`](main.py):

| Constant | Default | Meaning |
| -------- | ------- | ------- |
| `TTL_HOURS` | `24` | How long a repo must be absent from trending before it can notify again |
| `TRENDING_URL` | `…/trending?since=daily` | Which trending page to watch (all languages, daily) |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout in seconds |

The cron cadence lives in [`.github/workflows/trendify.yml`](.github/workflows/trendify.yml)
(`7,37 * * * *` — every 30 minutes, offset off :00/:30 because those slots are the most
delayed on GitHub's shared schedulers).

---

## Project structure

```
trendify/
├── main.py                        # scrape → diff → notify → persist state
├── state/seen.json                # sliding-window state, committed by CI
├── requirements.txt               # requests, beautifulsoup4
├── .github/workflows/trendify.yml # the half-hourly cron + commit-back
└── README.md
```

---

## Good to know

- **First run notifies everything currently trending** (~15–25 repos). That flood is
  one-time; later runs only report genuinely new entries.
- **GitHub Actions cron is best-effort** — scheduled runs can be delayed, and under heavy
  load some ticks are skipped entirely. At a 30-minute cadence that just means the
  occasional check is late or missed; the next run catches up, so nothing is lost.
- **Telegram etiquette** — messages are spaced one second apart and honor the API's
  `retry_after` value if rate-limited.

---

<div align="center">
<sub>Built to run quietly in the background and never bother you twice about the same repo.</sub>
</div>
