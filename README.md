# Trendify

Watches the [GitHub trending page](https://github.com/trending) every 3 hours and
sends a Telegram message for each **newly trending** repo.

## How it works

- A GitHub Actions cron job (`17 */3 * * *` UTC) scrapes
  `github.com/trending?since=daily` (all languages).
- Seen repos are tracked in [state/seen.json](state/seen.json) as a
  **sliding 24-hour window**: every time a repo appears, its timestamp is
  refreshed. A repo only notifies again after it has been **absent from
  trending for a full day**.
- The workflow commits the updated state back to this repo as
  `github-actions[bot]`. Those commits also keep the repo "active," so
  GitHub never auto-disables the scheduled workflow.
- A repo is only recorded as seen after its Telegram message sends
  successfully — failed sends retry on the next run.

## Setup

1. Create a bot: message [@BotFather](https://t.me/BotFather) → `/newbot` →
   copy the **bot token**.
2. Get your **chat ID**: send your bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `message.chat.id`.
3. Push this project to a GitHub repository.
4. In the repo: **Settings → Secrets and variables → Actions**, add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. Trigger the **trendify** workflow manually once (Actions tab →
   trendify → Run workflow) to confirm it works.

> **Note:** the very first run notifies all ~25 currently trending repos.
> That flood is one-time; later runs only report genuinely new entries.

## Run locally

```sh
pip install -r requirements.txt

# no Telegram needed — prints messages instead of sending
DRY_RUN=1 python main.py

# real send
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python main.py
```

## Notes

- GitHub has no official trending API, so this scrapes HTML. If GitHub
  changes its markup, the script fails loudly (red run) instead of
  silently reporting nothing.
- Actions cron is best-effort; runs can drift by 15–30 minutes.
- Messages are spaced 1 s apart and honor Telegram's `retry_after` on 429.
