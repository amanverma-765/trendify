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


def parse_trending(html_text):
    """Parse GitHub trending HTML markup into repository records.
    Raises RuntimeError if no repositories can be parsed."""
    soup = BeautifulSoup(html_text, "html.parser")
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


def fetch_trending(url=TRENDING_URL):
    """Fetch the trending page and parse repositories. Raises if network fails
    or markup cannot be parsed."""
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return parse_trending(resp.text)


class TrendingState:
    """Encapsulates sliding-window trending repository state persistence,
    deduplication, sliding-window updates, and TTL-based pruning."""

    def __init__(self, entries=None, path=None):
        self._entries = dict(entries) if entries is not None else {}
        self.path = Path(path) if path is not None else None

    @classmethod
    def load(cls, path=STATE_FILE):
        """Load state from disk, recovering gracefully from missing or corrupt files."""
        target = Path(path)
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return cls(entries=data, path=target)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return cls(entries={}, path=target)

    def is_seen(self, name):
        """Check if repository was already recorded within the sliding window."""
        return name in self._entries

    def touch(self, name, timestamp=None):
        """Update or record the last_seen timestamp for a repository."""
        ts = (timestamp or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        self._entries[name] = ts

    def prune(self, ttl_hours=TTL_HOURS, now=None):
        """Remove entries older than ttl_hours or with invalid timestamps.
        Returns the number of pruned entries."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=ttl_hours)
        pruned = 0
        for name, last_seen in list(self._entries.items()):
            try:
                expired = datetime.fromisoformat(last_seen) < cutoff
            except (ValueError, TypeError):
                expired = True
            if expired:
                del self._entries[name]
                pruned += 1
        return pruned

    def save(self, path=None):
        """Persist state to disk in deterministic sorted JSON format."""
        target = Path(path) if path is not None else self.path
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(self._entries.items())), f, indent=2)
            f.write("\n")

    def __len__(self):
        return len(self._entries)

    def __contains__(self, name):
        return self.is_seen(name)


def load_state(path=STATE_FILE):
    """Backward-compatible helper for loading state dictionary."""
    return TrendingState.load(path)._entries


def save_state(state, path=STATE_FILE):
    """Backward-compatible helper for saving state dictionary."""
    TrendingState(state, path=path).save()


def format_message(repo):
    """Format a trending repository record as an HTML message body."""
    url = f"https://github.com/{repo['name']}"
    lines = [
        "<b>New Trending Repo</b>",
        "",
        f'<b><a href="{url}">{html.escape(repo["name"])}</a></b>',
        "",
    ]
    if repo.get("description"):
        lines += [html.escape(repo["description"]), ""]
    if repo.get("language"):
        lines.append(f"<b>Language:</b> {html.escape(repo['language'])}")
    lines.append(f"<b>Stars:</b> ⭐ {html.escape(repo.get('stars', '?'))}")
    return "\n".join(lines)


class Notifier:
    """Delivery interface seam for repository notifications."""

    def send(self, repo):
        raise NotImplementedError


class DryRunNotifier(Notifier):
    """Dry-run delivery adapter: formats message and prints preview to stdout."""

    def send(self, repo):
        text = format_message(repo)
        repo_url = f"https://github.com/{repo['name']}"
        print(
            f"--- DRY RUN message ---\n{text}\n"
            f"[Button: ↗ View on GitHub -> {repo_url}]\n"
        )
        return True


class TelegramNotifier(Notifier):
    """Telegram delivery adapter: handles card fetching, photo upload with inline button,
    fallback to text message, and HTTP 429 rate limit backoff."""

    def __init__(
        self,
        token,
        chat_id,
        timeout=REQUEST_TIMEOUT,
        user_agent=USER_AGENT,
    ):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.user_agent = user_agent

    def send(self, repo):
        text = format_message(repo)
        repo_url = f"https://github.com/{repo['name']}"
        image_url = f"https://opengraph.githubassets.com/trendify/{repo['name']}"
        return self._deliver(text, image_url, repo_url)

    def _deliver(self, text, image_url, repo_url):
        reply_markup = {
            "inline_keyboard": [[{"text": "↗ View on GitHub", "url": repo_url}]]
        }

        image = self._fetch_card_image(image_url)
        if image is not None:
            resp = self._telegram_call(
                "sendPhoto",
                {
                    "chat_id": self.chat_id,
                    "caption": text,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(reply_markup),
                },
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

        resp = self._telegram_call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            },
        )
        if resp is not None and resp.ok:
            return True
        if resp is not None:
            print(
                f"warning: telegram send failed ({resp.status_code}): {resp.text[:200]}",
                file=sys.stderr,
            )
        return False

    def _fetch_card_image(self, url):
        """Download the social-card image ourselves. Telegram's fetcher gets
        rate-limited by GitHub (429 -> 'failed to get HTTP URL content'), so we
        fetch with retries and upload the bytes instead of passing the URL."""
        for attempt in (1, 2, 3):
            try:
                resp = requests.get(
                    url, headers={"User-Agent": self.user_agent}, timeout=self.timeout
                )
            except requests.RequestException as exc:
                print(f"warning: card image fetch failed: {exc}", file=sys.stderr)
                return None
            if resp.ok and resp.headers.get("Content-Type", "").startswith("image/"):
                return resp.content
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * attempt)
                continue
            print(
                f"warning: card image fetch failed ({resp.status_code}) for {url}",
                file=sys.stderr,
            )
            return None
        return None

    def _telegram_call(self, method, payload, files=None):
        """One API call with a single retry on 429."""
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in (1, 2):
            try:
                if files:
                    resp = requests.post(
                        url, data=payload, files=files, timeout=self.timeout
                    )
                else:
                    resp = requests.post(url, json=payload, timeout=self.timeout)
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


def fetch_card_image(url):
    """Backward-compatible helper for fetching card image."""
    return TelegramNotifier("", "")._fetch_card_image(url)


def _telegram_call(token, method, payload, files=None):
    """Backward-compatible helper for Telegram API calls."""
    return TelegramNotifier(token, "")._telegram_call(method, payload, files=files)


def send_telegram(token, chat_id, text, image_url, repo_url, dry_run):
    """Backward-compatible wrapper for sending Telegram messages."""
    if dry_run:
        print(
            f"--- DRY RUN message ---\n{text}\n[Button: ↗ View on GitHub -> {repo_url}]\n"
        )
        return True
    return TelegramNotifier(token, chat_id)._deliver(text, image_url, repo_url)


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not dry_run and not (token and chat_id):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use DRY_RUN=1")

    notifier = DryRunNotifier() if dry_run else TelegramNotifier(token, chat_id)
    repos = fetch_trending()
    state = TrendingState.load(STATE_FILE)

    notified = 0
    failed = 0
    for repo in repos:
        name = repo["name"]
        if state.is_seen(name):
            # Sliding window: still trending, refresh without notifying.
            state.touch(name)
            continue
        if notified and not dry_run:
            time.sleep(1)  # Telegram allows ~1 msg/sec per chat
        if notifier.send(repo):
            state.touch(name)
            notified += 1
        else:
            failed += 1  # not added to state, so it retries next run

    pruned = state.prune(ttl_hours=TTL_HOURS)
    state.save()

    # Expose the new-repo count so CI can determine the commit message:
    # "feat: caught..." for new trending repos, "chore: refresh..." otherwise.
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
