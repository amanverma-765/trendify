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
    name = html.escape(repo["name"])
    lines = [f"\U0001f525 <b>{name}</b>"]
    meta = f"⭐ {repo['stars']}"
    if repo["stars_today"]:
        meta += f" ({repo['stars_today']})"
    if repo["language"]:
        meta += f" · {repo['language']}"
    lines.append(html.escape(meta))
    if repo["description"]:
        lines.append(html.escape(repo["description"]))
    lines.append(f"https://github.com/{repo['name']}")
    return "\n".join(lines)


def send_telegram(token, chat_id, text, dry_run):
    """Send one message. Returns True on success. Retries once on 429."""
    if dry_run:
        print(f"--- DRY RUN message ---\n{text}\n")
        return True

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    for attempt in (1, 2):
        try:
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            print(f"warning: telegram request failed: {exc}", file=sys.stderr)
            return False
        if resp.status_code == 429 and attempt == 1:
            retry_after = 5
            try:
                retry_after = resp.json()["parameters"]["retry_after"]
            except (ValueError, KeyError):
                pass
            print(f"rate limited, retrying in {retry_after}s", file=sys.stderr)
            time.sleep(retry_after)
            continue
        if resp.ok:
            return True
        print(
            f"warning: telegram send failed ({resp.status_code}): {resp.text[:200]}",
            file=sys.stderr,
        )
        return False
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
        if send_telegram(token, chat_id, format_message(repo), dry_run):
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
    print(
        f"trending={len(repos)} new={notified} failed={failed} "
        f"pruned={pruned} tracked={len(state)}"
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
