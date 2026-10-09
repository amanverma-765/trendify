import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import main


class TestTrendingParser(unittest.TestCase):
    def test_parse_trending_success(self) -> None:
        html_doc = """
        <html>
          <body>
            <article class="Box-row">
              <h2><a href="/owner/cool-repo">owner / cool-repo</a></h2>
              <p>An awesome project</p>
              <span itemprop="programmingLanguage">Python</span>
              <a href="/owner/cool-repo/stargazers">1,234</a>
              <span class="d-inline-block float-sm-right">150 stars today</span>
            </article>
          </body>
        </html>
        """
        repos = main.parse_trending(html_doc)
        self.assertEqual(len(repos), 1)
        self.assertEqual(
            repos[0],
            {
                "name": "owner/cool-repo",
                "description": "An awesome project",
                "language": "Python",
                "stars": "1,234",
                "stars_today": "150 stars today",
            },
        )

    def test_parse_trending_missing_optional_fields(self) -> None:
        html_doc = """
        <article class="Box-row">
          <h2><a href="/minimal/repo">minimal / repo</a></h2>
        </article>
        """
        repos = main.parse_trending(html_doc)
        self.assertEqual(len(repos), 1)
        self.assertEqual(
            repos[0],
            {
                "name": "minimal/repo",
                "description": "",
                "language": "",
                "stars": "?",
                "stars_today": "",
            },
        )

    def test_parse_trending_empty_markup_raises_runtime_error(self) -> None:
        html_doc = "<html><body><div>Empty page</div></body></html>"
        with self.assertRaises(RuntimeError) as ctx:
            main.parse_trending(html_doc)
        self.assertIn("Parsed 0 repos", str(ctx.exception))

    @patch("main.requests.get")
    def test_fetch_trending_delegates_to_parser(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.text = """
        <article class="Box-row">
          <h2><a href="/test/repo">test / repo</a></h2>
        </article>
        """
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        repos = main.fetch_trending()
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["name"], "test/repo")


class TestTrendingState(unittest.TestCase):
    def test_is_seen_and_touch(self) -> None:
        state = main.TrendingState()
        self.assertFalse(state.is_seen("org/repo"))
        self.assertNotIn("org/repo", state)
        self.assertEqual(len(state), 0)

        fixed_time = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        state.touch("org/repo", timestamp=fixed_time)

        self.assertTrue(state.is_seen("org/repo"))
        self.assertIn("org/repo", state)
        self.assertEqual(len(state), 1)
        self.assertEqual(state._entries["org/repo"], "2026-10-09T12:00:00+00:00")

    def test_sliding_window_refresh(self) -> None:
        old_time = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc)
        new_time = datetime(2026, 10, 9, 10, 0, 0, tzinfo=timezone.utc)
        state = main.TrendingState(entries={"org/repo": old_time.isoformat()})

        state.touch("org/repo", timestamp=new_time)
        self.assertEqual(state._entries["org/repo"], new_time.isoformat())

    def test_prune_expired_and_corrupt_entries(self) -> None:
        now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        fresh_time = (now - timedelta(hours=5)).isoformat()
        expired_time = (now - timedelta(hours=25)).isoformat()

        state = main.TrendingState(
            entries={
                "fresh/repo": fresh_time,
                "old/repo": expired_time,
                "broken/repo": "not-a-valid-iso-date",
            }
        )
        pruned = state.prune(ttl_hours=24, now=now)
        self.assertEqual(pruned, 2)
        self.assertTrue(state.is_seen("fresh/repo"))
        self.assertFalse(state.is_seen("old/repo"))
        self.assertFalse(state.is_seen("broken/repo"))
        self.assertEqual(len(state), 1)

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "sub" / "seen.json"
            state = main.TrendingState(
                entries={
                    "b/repo": "2026-10-09T12:00:00+00:00",
                    "a/repo": "2026-10-09T11:00:00+00:00",
                },
                path=file_path,
            )
            state.save()

            # Ensure saved file is sorted alphabetically with indentation
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertTrue(content.startswith('{\n  "a/repo":'))

            loaded = main.TrendingState.load(file_path)
            self.assertEqual(len(loaded), 2)
            self.assertTrue(loaded.is_seen("a/repo"))
            self.assertTrue(loaded.is_seen("b/repo"))

    def test_load_recovery_from_missing_and_corrupt_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "missing.json"
            state = main.TrendingState.load(nonexistent)
            self.assertEqual(len(state), 0)

            corrupt_json = Path(tmpdir) / "corrupt.json"
            corrupt_json.write_text("{broken", encoding="utf-8")
            state = main.TrendingState.load(corrupt_json)
            self.assertEqual(len(state), 0)

            list_json = Path(tmpdir) / "list.json"
            list_json.write_text("[1, 2, 3]", encoding="utf-8")
            state = main.TrendingState.load(list_json)
            self.assertEqual(len(state), 0)


class TestNotifier(unittest.TestCase):
    def setUp(self) -> None:
        self.repo: main.Repo = {
            "name": "test-owner/test-repo",
            "description": "Test description",
            "language": "Python",
            "stars": "1,234",
            "stars_today": "100",
        }

    def test_format_message(self) -> None:
        msg = main.format_message(self.repo)
        self.assertNotIn("↗ View on GitHub</a>", msg)
        self.assertIn("<b>Stars:</b> ⭐ 1,234", msg)
        self.assertTrue(msg.endswith("<b>Stars:</b> ⭐ 1,234"))

    def test_format_message_caption_truncation(self) -> None:
        long_repo: main.Repo = {
            "name": "test-owner/test-repo",
            "description": "Very long description " * 100,
            "language": "Python",
            "stars": "1,234",
            "stars_today": "100",
        }
        caption = main.format_message(long_repo, max_caption_len=1000)
        self.assertLessEqual(len(caption), 1000)
        self.assertTrue(caption.endswith("<b>Stars:</b> ⭐ 1,234"))
        self.assertIn("...", caption)

    def test_dry_run_notifier(self) -> None:
        notifier = main.DryRunNotifier()
        self.assertTrue(notifier.send(self.repo))

    @patch("main.requests.get")
    @patch("main.requests.post")
    def test_telegram_notifier_photo_success(
        self, mock_post: MagicMock, mock_get: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            ok=True,
            status_code=200,
            headers={"Content-Type": "image/png"},
            content=b"fake-image",
        )
        mock_post.return_value = MagicMock(ok=True, status_code=200)

        notifier = main.TelegramNotifier("token", "123")
        self.assertTrue(notifier.send(self.repo))

        mock_get.assert_called_once_with(
            "https://opengraph.githubassets.com/trendify/test-owner/test-repo",
            headers={"User-Agent": main.USER_AGENT},
            timeout=main.REQUEST_TIMEOUT,
        )
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.telegram.org/bottoken/sendPhoto")
        self.assertEqual(kwargs["data"]["chat_id"], "123")
        reply_markup = json.loads(kwargs["data"]["reply_markup"])
        self.assertEqual(
            reply_markup["inline_keyboard"],
            [
                [
                    {
                        "text": "↗ View on GitHub",
                        "url": "https://github.com/test-owner/test-repo",
                    }
                ]
            ],
        )
        self.assertIn("photo", kwargs["files"])

    @patch("main.requests.get")
    @patch("main.requests.post")
    def test_telegram_notifier_card_429_falls_back_to_avatar(
        self, mock_post: MagicMock, mock_get: MagicMock
    ) -> None:
        # First call (opengraph) returns 429; second call (avatar) returns 200 image
        mock_card = MagicMock(ok=False, status_code=429, headers={})
        mock_avatar = MagicMock(
            ok=True,
            status_code=200,
            headers={"Content-Type": "image/png"},
            content=b"avatar-bytes",
        )
        mock_get.side_effect = [mock_card, mock_avatar]
        mock_post.return_value = MagicMock(ok=True, status_code=200)

        notifier = main.TelegramNotifier("token", "123")
        self.assertTrue(notifier.send(self.repo))

        self.assertEqual(mock_get.call_count, 2)
        mock_get.assert_any_call(
            "https://opengraph.githubassets.com/trendify/test-owner/test-repo",
            headers={"User-Agent": main.USER_AGENT},
            timeout=main.REQUEST_TIMEOUT,
        )
        mock_get.assert_any_call(
            "https://github.com/test-owner.png",
            headers={"User-Agent": main.USER_AGENT},
            timeout=main.REQUEST_TIMEOUT,
        )
        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args[0][0], "https://api.telegram.org/bottoken/sendPhoto"
        )
        self.assertEqual(
            mock_post.call_args[1]["files"]["photo"],
            ("card.png", b"avatar-bytes"),
        )

    @patch("main.requests.get")
    @patch("main.requests.post")
    def test_telegram_notifier_photo_fails_text_fallback(
        self, mock_post: MagicMock, mock_get: MagicMock
    ) -> None:
        # Both image fetches fail (e.g. 404)
        mock_get.return_value = MagicMock(ok=False, status_code=404, headers={})
        mock_post.return_value = MagicMock(ok=True, status_code=200)

        notifier = main.TelegramNotifier("token", "123")
        self.assertTrue(notifier.send(self.repo))

        self.assertEqual(mock_get.call_count, 2)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.telegram.org/bottoken/sendMessage")
        self.assertEqual(kwargs["json"]["chat_id"], "123")
        self.assertEqual(
            kwargs["json"]["reply_markup"]["inline_keyboard"],
            [
                [
                    {
                        "text": "↗ View on GitHub",
                        "url": "https://github.com/test-owner/test-repo",
                    }
                ]
            ],
        )

    @patch("main.requests.get")
    @patch("main.requests.post")
    def test_telegram_notifier_both_fail_returns_false(
        self, mock_post: MagicMock, mock_get: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(ok=False, status_code=404, headers={})
        mock_post.return_value = MagicMock(
            ok=False, status_code=500, text="Internal error"
        )

        notifier = main.TelegramNotifier("token", "123")
        self.assertFalse(notifier.send(self.repo))


if __name__ == "__main__":
    unittest.main()
