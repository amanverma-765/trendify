#!/usr/bin/env python3
"""Trendify: notify a Telegram chat about newly trending GitHub repos.

Scrapes https://github.com/trending (daily window, all languages), diffs
against a sliding-window state file, and sends one Telegram message per
newly trending repo. A repo re-notifies only after it has been absent
from trending for TTL_HOURS.

Env vars:
    TELEGRAM_BOT_TOKEN  bot token from @BotFather
    TELEGRAM_CHAT_ID    target chat ID
    DRY_RUN=1           print messages instead of sending (no secrets needed)
"""

import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

TRENDING_URL = "https://github.com/trending?since=daily"
STATE_FILE = Path(__file__).resolve().parent / "state" / "seen.json"
TTL_HOURS = 24
REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def fetch_trending():
    """Scrape the trending page. Raises if the page can't be parsed."""
    resp = requests.get(
        TRENDING_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    repos = []
    for row in soup.select("article.Box-row"):
        link = row.select_one("h2 a")
        if not link or not link.get("href"):
            continue
        desc = row.select_one("p")
        lang = row.select_one('span[itemprop="programmingLanguage"]')
        stars = row.select_one('a[href$="/stargazers"]')
        today = row.select_one("span.d-inline-block.float-sm-right")
        repos.append(
            {
                "name": link["href"].strip("/"),
                "description": desc.get_text(strip=True) if desc else "",
                "language": lang.get_text(strip=True) if lang else "",
                "stars": stars.get_text(strip=True) if stars else "?",
                "stars_today": today.get_text(strip=True) if today else "",
            }
        )

    if not repos:
        raise RuntimeError(
            "Parsed 0 repos from the trending page — GitHub markup may have changed"
        )
    return repos


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        if isinstance(state, dict):
            return state
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(dict(sorted(state.items())), f, indent=2)
        f.write("\n")


def format_message(repo):
    url = f"https://github.com/{repo['name']}"
    lines = [
        "<b>New Trending Repo</b>",
        "",
        f'<b><a href="{url}">{html.escape(repo["name"])}</a></b>',
        "",
    ]
    if repo["description"]:
        lines += [html.escape(repo["description"]), ""]
    if repo["language"]:
        lines.append(f"<b>Language:</b> {html.escape(repo['language'])}")
    lines.append(f"<b>Stars:</b> ⭐ {html.escape(repo['stars'])}")
    lines.append("")
    lines.append(f'<a href="{url}">↗ View on GitHub</a>')
    return "\n".join(lines)


def fetch_card_image(url):
    """Download the social-card image ourselves. Telegram's fetcher gets
    rate-limited by GitHub (429 -> "failed to get HTTP URL content"), so we
    fetch with retries and upload the bytes instead of passing the URL.
    Returns the image bytes, or None if it can't be fetched."""
    for attempt in (1, 2, 3):
        try:
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            print(f"warning: card image fetch failed: {exc}", file=sys.stderr)
            return None
        if resp.ok and resp.headers.get("Content-Type", "").startswith("image/"):
            return resp.content
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 * attempt)  # GitHub generates cards on demand; back off
            continue
        print(
            f"warning: card image fetch failed ({resp.status_code}) for {url}",
            file=sys.stderr,
        )
        return None
    return None


def _telegram_call(token, method, payload, files=None):
    """One API call with a single retry on 429. Returns the response or None."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in (1, 2):
        try:
            if files:
                resp = requests.post(
                    url, data=payload, files=files, timeout=REQUEST_TIMEOUT
                )
            else:
                resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            print(f"warning: telegram request failed: {exc}", file=sys.stderr)
            return None
        if resp.status_code == 429 and attempt == 1:
            retry_after = 5
            try:
                retry_after = resp.json()["parameters"]["retry_after"]
            except (ValueError, KeyError):
                pass
            print(f"rate limited, retrying in {retry_after}s", file=sys.stderr)
            time.sleep(retry_after)
            continue
        return resp
    return None


def send_telegram(token, chat_id, text, image_url, dry_run):
    """Send the repo card image with the text as caption; fall back to a
    plain text message if the image can't be fetched or the photo send
    fails. Returns True on success."""
    if dry_run:
        print(f"--- DRY RUN message ---\n{text}\n")
        return True

    image = fetch_card_image(image_url)
    if image is not None:
        resp = _telegram_call(
            token,
            "sendPhoto",
            {"chat_id": chat_id, "caption": text, "parse_mode": "HTML"},
            files={"photo": ("card.png", image)},
        )
        if resp is not None and resp.ok:
            return True
        if resp is not None:
            print(
                f"warning: sendPhoto failed ({resp.status_code}): {resp.text[:200]}, "
                "falling back to text message",
                file=sys.stderr,
            )
    resp = _telegram_call(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )
    if resp is not None and resp.ok:
        return True
    if resp is not None:
        print(
            f"warning: telegram send failed ({resp.status_code}): {resp.text[:200]}",
            file=sys.stderr,
        )
    return False


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not dry_run and not (token and chat_id):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use DRY_RUN=1")

    repos = fetch_trending()
    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    notified = 0
    failed = 0
    for repo in repos:
        if repo["name"] in state:
            # Sliding window: still trending, refresh without notifying.
            state[repo["name"]] = now_iso
            continue
        if notified and not dry_run:
            time.sleep(1)  # Telegram allows ~1 msg/sec per chat
        # GitHub's social-card image; the first path segment is an
        # arbitrary cache key, only the owner/repo suffix matters.
        image_url = f"https://opengraph.githubassets.com/trendify/{repo['name']}"
        if send_telegram(token, chat_id, format_message(repo), image_url, dry_run):
            state[repo["name"]] = now_iso
            notified += 1
        else:
            failed += 1  # not added to state, so it retries next run

    cutoff = now - timedelta(hours=TTL_HOURS)
    pruned = 0
    for name, last_seen in list(state.items()):
        try:
            expired = datetime.fromisoformat(last_seen) < cutoff
        except ValueError:
            expired = True
        if expired:
            del state[name]
            pruned += 1

    save_state(state)

    # Expose the new-repo count so CI can attribute the commit: real finds
    # commit as the repo owner (counts on the heatmap), routine state
    # refreshes commit as the bot (do not).
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"new_count={notified}\n")

    print(
        f"trending={len(repos)} new={notified} failed={failed} "
        f"pruned={pruned} tracked={len(state)}"
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
