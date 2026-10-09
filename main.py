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
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, TypedDict

import requests
from bs4 import BeautifulSoup

TRENDING_URL: str = "https://github.com/trending?since=daily"
STATE_FILE: Path = Path(__file__).resolve().parent / "state" / "seen.json"
TTL_HOURS: int = 24
REQUEST_TIMEOUT: int = 30
USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class Repo(TypedDict):
    name: str
    description: str
    language: str
    stars: str
    stars_today: str


def parse_trending(html_text: str) -> list[Repo]:
    """Parse GitHub trending HTML markup into repository records.
    Raises RuntimeError if no repositories can be parsed."""
    soup = BeautifulSoup(html_text, "html.parser")
    repos: list[Repo] = []
    for row in soup.select("article.Box-row"):
        link = row.select_one("h2 a")
        if not link:
            continue
        href = link.get("href")
        if not isinstance(href, str) or not href:
            continue
        desc = row.select_one("p")
        lang = row.select_one('span[itemprop="programmingLanguage"]')
        stars = row.select_one('a[href$="/stargazers"]')
        today = row.select_one("span.d-inline-block.float-sm-right")
        repos.append(
            {
                "name": href.strip("/"),
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


def fetch_trending(url: str = TRENDING_URL) -> list[Repo]:
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

    def __init__(
        self,
        entries: Mapping[str, str] | None = None,
        path: Path | str | None = None,
    ) -> None:
        self._entries: dict[str, str] = dict(entries) if entries is not None else {}
        self.path: Path | None = Path(path) if path is not None else None

    @classmethod
    def load(cls, path: Path | str = STATE_FILE) -> "TrendingState":
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

    def is_seen(self, name: str) -> bool:
        """Check if repository was already recorded within the sliding window."""
        return name in self._entries

    def touch(self, name: str, timestamp: datetime | None = None) -> None:
        """Update or record the last_seen timestamp for a repository."""
        ts = (timestamp or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        self._entries[name] = ts

    def prune(self, ttl_hours: int = TTL_HOURS, now: datetime | None = None) -> int:
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

    def save(self, path: Path | str | None = None) -> None:
        """Persist state to disk in deterministic sorted JSON format."""
        target = Path(path) if path is not None else self.path
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(self._entries.items())), f, indent=2)
            f.write("\n")

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.is_seen(name)


def format_message(repo: Repo, max_caption_len: int | None = None) -> str:
    """Format a trending repository record as an HTML message body."""
    url = f"https://github.com/{repo['name']}"
    lines = [
        "<b>New Trending Repo</b>",
        "",
        f'<b><a href="{url}">{html.escape(repo["name"])}</a></b>',
        "",
    ]
    desc = repo.get("description", "")
    if desc:
        if max_caption_len is not None:
            # Estimate static parts length to keep caption under Telegram limits
            other_len = sum(len(l) + 1 for l in lines) + len(
                f"<b>Language:</b> {repo.get('language', '')}\n<b>Stars:</b> ⭐ {repo.get('stars', '')}"
            )
            budget = max(max_caption_len - other_len - 15, 20)
            if len(desc) > budget:
                desc = desc[:budget].rstrip() + "..."
        lines += [html.escape(desc), ""]
    if repo.get("language"):
        lines.append(f"<b>Language:</b> {html.escape(repo['language'])}")
    lines.append(f"<b>Stars:</b> ⭐ {html.escape(repo.get('stars', '?'))}")
    return "\n".join(lines)


class Notifier(Protocol):
    """Delivery interface seam for repository notifications."""

    def send(self, repo: Repo) -> bool: ...


class DryRunNotifier:
    """Dry-run delivery adapter: formats message and prints preview to stdout."""

    def send(self, repo: Repo) -> bool:
        text = format_message(repo)
        repo_url = f"https://github.com/{repo['name']}"
        print(
            f"--- DRY RUN message ---\n{text}\n"
            f"[Button: ↗ View on GitHub -> {repo_url}]\n"
        )
        return True


class TelegramNotifier:
    """Telegram delivery adapter: handles card fetching, photo upload with inline button,
    fallback to text message, and HTTP 429 rate limit backoff."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        timeout: int = REQUEST_TIMEOUT,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.user_agent = user_agent

    def send(self, repo: Repo) -> bool:
        caption = format_message(repo, max_caption_len=1000)
        full_text = format_message(repo)
        repo_url = f"https://github.com/{repo['name']}"
        return self._deliver(repo, caption, full_text, repo_url)

    def _deliver(self, repo: Repo, caption: str, full_text: str, repo_url: str) -> bool:
        reply_markup = {
            "inline_keyboard": [[{"text": "↗ View on GitHub", "url": repo_url}]]
        }

        image = self._fetch_repo_image(repo)
        if image is not None:
            resp = self._telegram_call(
                "sendPhoto",
                {
                    "chat_id": self.chat_id,
                    "caption": caption,
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
                "text": full_text,
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

    def _fetch_image_from_url(self, url: str) -> tuple[int, bytes | None]:
        """Fetch image bytes from a URL. Returns (status_code, image_bytes_or_none)."""
        try:
            resp = requests.get(
                url, headers={"User-Agent": self.user_agent}, timeout=self.timeout
            )
            if resp.ok and resp.headers.get("Content-Type", "").startswith("image/"):
                return resp.status_code, resp.content
            return resp.status_code, None
        except requests.RequestException as exc:
            print(f"warning: image fetch failed: {exc}", file=sys.stderr)
            return 0, None

    def _fetch_repo_image(self, repo: Repo) -> bytes | None:
        """Fetch repository preview image. Tries the social card first; on 429
        rate-limiting or failure, immediately falls back to the owner avatar."""
        card_url = f"https://opengraph.githubassets.com/trendify/{repo['name']}"
        _, image = self._fetch_image_from_url(card_url)
        if image is not None:
            return image

        # Fallback to owner avatar (statically cached on GitHub CDN, no 429 rate limit)
        owner = repo["name"].split("/")[0]
        avatar_url = f"https://github.com/{owner}.png"
        _, image = self._fetch_image_from_url(avatar_url)
        if image is not None:
            return image

        print(
            f"warning: card and avatar image fetch failed for {repo['name']}",
            file=sys.stderr,
        )
        return None

    def _telegram_call(
        self,
        method: str,
        payload: dict[str, Any],
        files: Mapping[str, Any] | None = None,
    ) -> requests.Response | None:
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


def main() -> None:
    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not dry_run and not (token and chat_id):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use DRY_RUN=1")

    notifier: Notifier = (
        DryRunNotifier() if dry_run else TelegramNotifier(token, chat_id)
    )
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
