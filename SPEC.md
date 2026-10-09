# SPEC.md: Telegram Inline URL Button, GitHub Actions Bot Commits & Architecture Deepening

## 1. Objective

Enhance the notification presentation in Trendify by replacing the text-based hyperlink at the bottom of Telegram messages with a native Telegram inline URL button ("↗ View on GitHub"). Standardize automated git commits in GitHub Actions so all automated state commits are authored by `github-actions[bot]` instead of personal credentials (`amanverma-765`).

Additionally, deepen the codebase architecture by establishing clear seams and deep modules:
1. Pure document parsing isolated from network transport (`parse_trending`).
2. Encapsulated sliding-window state management and persistence (`TrendingState`).
3. Delivery interface seam with swappable adapters (`Notifier`, `TelegramNotifier`, `DryRunNotifier`).

### Target Users
- Subscribers to the Trendify Telegram channel/chat receiving trending repository alerts.
- Maintainers managing GitHub repository commits, Actions runs, and codebase evolution.

---

## 2. Capabilities & Acceptance Criteria

### Capability 1: Telegram Inline URL Button
- **Message Body**: Remove `<a href="{url}">↗ View on GitHub</a>` from the HTML text body in `format_message(repo)`. The message body ends cleanly after the stars line.
- **Inline Keyboard**: Attach a Telegram `reply_markup` with an `inline_keyboard` containing a single button:
  - `text`: `"↗ View on GitHub"`
  - `url`: `https://github.com/{repo['name']}`
- **API Call Handling**:
  - `sendPhoto`: Supply `reply_markup` as a JSON-serialized string in form data payload (`json.dumps(...)`).
  - `sendMessage` (fallback): Supply `reply_markup` in the JSON payload dictionary.
- **Dry Run**: In `DRY_RUN=1` mode, print the inline button details alongside the text preview so it can be verified locally without Telegram credentials.
- **Documentation**: Update the sample message card in `README.md` to reflect the button element.

### Capability 2: Uniform GitHub Actions Bot Commits
- **Workflow Configuration**: In `.github/workflows/trendify.yml`, configure git user identity unconditionally:
  - `user.name`: `"github-actions[bot]"`
  - `user.email`: `"41898282+github-actions[bot]@users.noreply.github.com"`
- **Author Elimination**: Fully remove personal identity references (`amanverma-765`, `akverma4aman@gmail.com`).
- **Commit Messages**: Retain the dynamic commit message logic based on `NEW_COUNT`:
  - `feat: caught 1 new trending repo 🔥` (when count is 1)
  - `feat: caught ${NEW_COUNT} new trending repos 🔥` (when count > 1)
  - `chore: refresh trending state` (when count is 0)
- **Comments**: Update comments in `main.py` referencing user vs bot commit attribution.

### Capability 3: Architecture Deepening & Seams
- **Ingestion Seam (`parse_trending`)**:
  - Pure function `parse_trending(html_text: str) -> list[dict]` parses DOM without network I/O.
  - `fetch_trending(url: str)` handles HTTP transport and delegates to `parse_trending`.
- **Deep State Lifecycle (`TrendingState`)**:
  - Encapsulates JSON I/O, corrupted file recovery, sliding-window `touch()`, `is_seen()`, TTL `prune()`, and sorted serialization.
  - Replaces shallow `load_state` and `save_state` helpers (preserved as backward-compatibility shims).
- **Delivery Seam (`Notifier`)**:
  - Interface: `send(repo: dict) -> bool`.
  - Adapters: `TelegramNotifier` (production HTTP delivery, card image fetch with backoff, photo-to-text fallback, 429 retry) and `DryRunNotifier` (stdout preview with inline button).
- **Orchestration**: `main()` delegates to `TrendingState` and `Notifier`, reducing procedural plumbing.

---

## 3. Project Structure

Affected files in the repository:
```
trendify/
├── main.py                        # Deep modules: TrendingState, Notifier, parse_trending, and main()
├── test_trendify.py               # Unit tests covering state, notifiers, and parser
├── .github/workflows/trendify.yml # Standardized bot commit config
├── README.md                      # Updated sample message layout
└── SPEC.md                        # This specification file
```

---

## 4. Commands

### Local Development & Verification
- Run test suite:
  ```sh
  uv run --with-requirements requirements.txt python -m unittest test_trendify.py
  ```
- Run dry run locally:
  ```sh
  uv run --with-requirements requirements.txt env DRY_RUN=1 python main.py
  ```

---

## 5. Code Style & Architecture

- **Minimalist & Idiomatic Python**: Standard library tools (`json`, `html`, `sys`, `os`, `pathlib`, `datetime`) + existing dependencies (`requests`, `beautifulsoup4`).
- **Codebase Design Principles**:
  - Deep modules over shallow pass-throughs.
  - Clear seams with multiple adapters (`TelegramNotifier` vs `DryRunNotifier`).
  - Pure functions for parsing; test surfaces aligned with module interfaces.

---

## 6. Testing Strategy

1. **Parser Tests (`TestTrendingParser`)**:
   - Verify parsing of valid markup with all fields.
   - Verify fallback handling for missing descriptions and languages.
   - Verify exception on empty / altered markup.
2. **State Tests (`TestTrendingState`)**:
   - Test `is_seen`, `touch`, sliding-window timestamp refresh.
   - Test TTL expiration and corruption pruning.
   - Test disk persistence formatting (sorted keys, indentation) and corrupted file recovery.
3. **Notifier Tests (`TestNotifier`)**:
   - Test `DryRunNotifier` returns `True`.
   - Test `TelegramNotifier` photo upload with inline keyboard button.
   - Test `TelegramNotifier` photo failure falling back to text message.
   - Test failure reporting when both transport paths fail.
