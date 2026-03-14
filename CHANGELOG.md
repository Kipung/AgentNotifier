# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.1.5] - 2026-03-14

### Fixed

- **Codex `notify` written inside wrong TOML section.**
  When the Codex config contained section headers (e.g. `[notice.model_migrations]`)
  at the end of the file, the new `notify` key was appended after the last header,
  making TOML parse it as `notice.model_migrations.notify` (a string, not an array).
  Codex then errored with `invalid type: sequence, expected a string`.
  The key is now inserted before the first `[section]` header so it always lands
  at the top level.

## [0.1.4] - 2026-03-14

### Fixed

- **Hook subprocess PATH failure (the main cross-machine reliability fix).**
  `setup codex`, `setup gemini`, and `setup claude` now resolve the absolute
  install path of each hook entry-point via `shutil.which` and write that
  into the config file.  Previously, a bare name like
  `agent-notifier-claude-hook` was written, which works in a full login
  shell but fails silently when Claude Code / Codex / Gemini spawn hook
  subprocesses with a restricted PATH (typically `/usr/bin:/bin`).  This
  was the root cause of notifications working on the developer's machine but
  not on any other user's machine.  Falls back to the bare name only when
  the binary cannot be located during setup (e.g. source-checkout dev
  sessions without a pip install).

- **`notify/` module duplication and missing text-normalisation bug.**
  `agentnotifier/notify/*.py` previously contained separate copies of the
  platform backends.  `notify/macos.py` was missing the
  `_normalize_osascript_text` call that strips newlines before handing a
  string to `osascript`; multiline notification bodies would cause
  `osascript` to silently fail (exit 0, no popup).  All five `notify/*.py`
  files now re-export from the canonical `agentnotifier/notifier/` backends,
  eliminating the duplication and the divergence risk.

### Changed

- Doctor (`agent-notifier doctor`) now reports the macOS notification
  permission check as `[WARN]` instead of `[INFO]`, with a precise fix path.
  `osascript` returns exit code 0 even when macOS silently drops a
  notification because the terminal app has not been granted permission;
  there is no programmatic way to detect this, so an explicit warning is the
  only reliable signal.

- `setup codex`, `setup gemini`, `setup claude`, and `setup all` now print a
  macOS notification permission reminder after completing on macOS, listing
  the exact System Settings path and terminal apps to allow.

## [0.1.3] - 2026-03-11

### Added

- New `agent-notifier doctor` command for install, backend, and hook diagnostics across Codex, Gemini, and Claude Code setups.
- New maintainer release checklist at `docs/release_checklist.md`.

### Changed

- Package version is now single-sourced from `agentnotifier.__version__` to avoid PyPI metadata drift.
- Packaging metadata now uses SPDX-style license declaration and includes a Linux operating-system classifier.
- README and setup docs now reflect the verified `pipx`/PyPI install flow and the Windows extra needed for reliable desktop toasts.

## [0.1.2] - 2026-03-02

- Linux desktop notification backend using `notify-send` with console fallback when unavailable.
- Cross-platform notifier backend tests for macOS, Windows, and Linux code paths.
- New packaged Codex bridge entrypoint: `agent-notifier-codex-hook`.
- New packaged Gemini and Claude bridge entrypoints:
  - `agent-notifier-gemini-hook`
  - `agent-notifier-claude-hook`
- New macOS bootstrap helper: `examples/setup_macos.sh` to automate local install, Codex/Gemini hook wiring, and Gemini trust setup.

### Changed

- Removed `shell-init` process-exit notification mode from the CLI.
- Removed shell hook generation internals/tests and aligned project docs to task-level notifications only.
- Updated Windows PID existence checks to avoid signal-based probing during watch mode.
- Gemini hook payload parsing now accepts both snake_case and camelCase keys (`hook_event_name`/`eventName`, etc.).
- macOS desktop backend now tries `terminal-notifier` before `osascript`.
- macOS `osascript` fallback now flattens newline/tab characters for safer payload rendering.
- `--channel both --verbose` now explicitly reports desktop backend failure even when console delivery succeeds.
- Config loader now supports documented `AGENT_NOTIFIER_*` env vars (with compatibility for legacy `AGENT_NOTIFY_*` names).
- README now includes a scenario matrix with verified Codex/macOS/iTerm diagnostics and fix paths.
- README now includes a verified zero-to-working macOS onboarding flow, explicit `terminal-notifier` guidance, and clarified Gemini trust-folder requirements.

## [0.1.1] - 2026-02-20

### Changed

- Reworked README onboarding flow with clearer mode selection, per-tool setup recipes, and troubleshooting guidance.
- Clarified task-level vs shell-exit notification behavior to reduce setup confusion.
- Added `.release-venv/` to `.gitignore` to keep local release environments out of commits.

## [0.1.0] - 2026-02-20

### Added

- Initial release of `agent-notifier`.
- `run` command to wrap long-running commands and notifier on completion.
- `watch` command to monitor an existing PID.
- `test-notifier` command for backend verification.
- Optional `tail` command for log pattern notification.
- macOS notifier using `osascript`.
- Windows notifier using PowerShell/BurntToast with `win10toast` fallback.
- Console and null notifier implementations.
- Unit and integration tests with mocked notifier coverage.
- GitHub Actions CI for macOS + Windows.
