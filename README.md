# agent-notifier

`agent-notifier` sends notifications when long-running CLI/agent tasks finish.

It supports two usage styles:

1. Wrap a command (`agent-notifier run -- ...`)
2. Receive task-level events from interactive agents (Codex, Claude Code, Gemini, Ollama pipelines)

## Why People Use This

- You can keep coding in one window and get notified when a background task completes.
- Notifications include success/failure, duration, and exit code.
- Works on macOS, Windows, and Linux (with console fallback when desktop notifications are unavailable).

## Install

Recommended:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install agent-notifier
```

Alternative:

```bash
pip install agent-notifier
```

If `pipx install agent-notifier` fails because the package is not yet on PyPI, install from this repo checkout:

```bash
cd /path/to/AgentPulse/packages/agent-notifier
pipx install .
```

From source:

```bash
python -m pip install -e .
```

Confirm install:

```bash
agent-notifier --help
agent-notifier test-notifier --channel console
```

## Zero-To-Working Setup (macOS, Verified)

Validated on March 2, 2026 with:
- macOS 26.2
- iTerm2
- `codex-cli 0.107.0`
- Gemini CLI (`AfterAgent` hook)

Run the helper from the repo checkout:

```bash
cd /path/to/AgentPulse/packages/agent-notifier
./examples/setup_macos.sh --trust-dir "/absolute/path/to/your/project" --open-settings
```

What this script does:
- Installs `terminal-notifier` with Homebrew when available.
- Installs/updates `agent-notifier` from local source checkout.
- Configures Codex hook in `~/.codex/config.toml` using `notify = [...]`.
- Configures Gemini `AfterAgent` hook in `~/.gemini/settings.json`.
- Marks your project as trusted in `~/.gemini/trustedFolders.json`.
- Creates timestamped backups before editing existing Codex/Gemini config files.
- Runs `test-notifier` and a Codex bridge smoke test.

Important limitation:
- macOS notification permissions cannot be auto-granted by CLI tools.
- You must manually allow notifications for your terminal app (Terminal/iTerm) in System Settings -> Notifications.

## 2-Minute Quickstart

Run any long command through the wrapper:

```bash
agent-notifier run -- python3 -c "import time; time.sleep(8)"
```

If the command fails, the notification title changes to `Failed`.

## Behavior Model

`agent-notifier` is task-level first for interactive tools.
It notifies when hook events fire (Codex/Claude/Gemini integrations), not when you exit your shell.

## Interactive Tool Setup

### Codex CLI

Codex provides a notification hook.

Recommended setup (automatic, idempotent):

```bash
agent-notifier setup-codex
```

This updates `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`) with a top-level `notify = [...]` command.
On macOS/Linux it points directly at `agent-notifier-codex-hook`; on Windows it writes a PowerShell wrapper command that calls the installed hook executable.

```toml
notify = ["/absolute/path/to/agent-notifier-codex-hook"]
```

Then restart Codex and run:

```bash
agent-notifier test-notifier --channel both --verbose
```

Preferred (works great with `pipx`/`pip` installs):

```bash
BRIDGE_PATH="$(command -v agent-notifier-codex-hook)"
echo "$BRIDGE_PATH"
```

If you're running from a source checkout, use the included script bridge:

```bash
chmod +x examples/codex_notifier_bridge.sh
BRIDGE_PATH="$(realpath examples/codex_notifier_bridge.sh)"
echo "$BRIDGE_PATH"
```

Add to `~/.codex/config.toml`:

```toml
notify = [
  "/absolute/path/to/agent-notifier-codex-hook-or-script"
]
```

Codex key naming:
- `notify` is the Codex config key (official in current Codex CLI builds).
- It is unrelated to this package's internal naming (`agent-notifier`).

Legacy filename `examples/codex_notify_bridge.sh` is also supported.

Optional debug logs:

```bash
export AGENT_NOTIFIER_DEBUG=1
```

Log location:
`~/.agentnotifier/logs/codex_notifier.log`

#### Manual Verified Flow: macOS + iTerm + Codex

Use this exact flow if you do not use `examples/setup_macos.sh`:

1. Set Codex config key to `notify` (not `notifier`) in `~/.codex/config.toml`:

```toml
notify = [
  "/absolute/path/to/examples/codex_notifier_bridge.sh"
]
```

2. Ensure the bridge is executable:

```bash
chmod +x examples/codex_notifier_bridge.sh
```

3. Restart Codex fully (fresh process), then complete one prompt.

4. If you still do not see a popup, enable debug and inspect hook calls:

```bash
export AGENT_NOTIFIER_DEBUG=1
tail -n 80 ~/.agentnotifier/logs/codex_notifier.log
```

5. Manual bridge test (copy-paste safe):

```bash
./examples/codex_notifier_bridge.sh --channel both --verbose type=agent-turn-complete turn-id=manual-check
```

Expected behavior:
- You should hear a chime and/or see a desktop notification.
- The debug log should show `type=agent-turn-complete` payloads and `exit=0`.

Common causes when chime works but popup does not:
- macOS notification permissions disabled for your terminal app.
- Focus / Do Not Disturb enabled.
- Banner style disabled in System Settings -> Notifications.

### Claude Code

Preferred bridge command (installed via `pipx`/`pip`):

```bash
command -v agent-notifier-claude-hook
```

Configure hooks in `.claude/settings.local.json` (or user settings):

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "agent-notifier-claude-hook --event Stop"
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "agent-notifier-claude-hook --event SubagentStop"
          }
        ]
      }
    ]
  }
}
```

### Gemini CLI

Config files:
- `~/.gemini/settings.json` (hooks)
- `~/.gemini/trustedFolders.json` (folder trust)

Preferred bridge command (installed via `pipx`/`pip`):

```bash
command -v agent-notifier-gemini-hook
```

Configure `AfterAgent` hook:

```json
{
  "hooksConfig": {
    "enabled": true
  },
  "hooks": {
    "AfterAgent": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "agent-notifier-gemini-hook",
            "timeout": 10000
          }
        ]
      }
    ]
  }
}
```

Gemini trust requirement:
- Hooks may not run in untrusted project folders.
- If `AfterAgent` does not fire, confirm the current project path is trusted in `~/.gemini/trustedFolders.json`.

Quick check (`$PROJECT_DIR` is the folder where you run Gemini):

```bash
PROJECT_DIR="/absolute/path/to/your/project"
rg -n "\"$PROJECT_DIR\"|TRUST_FOLDER" ~/.gemini/trustedFolders.json
```

Example update:

```bash
PROJECT_DIR="/absolute/path/to/your/project"
jq --arg p "$PROJECT_DIR" '. + {($p):"TRUST_FOLDER"}' ~/.gemini/trustedFolders.json > /tmp/trustedFolders.json && mv /tmp/trustedFolders.json ~/.gemini/trustedFolders.json
```

Restart Gemini after changing trust settings.

Temporary hook-fire debug:

```json
{
  "type": "command",
  "command": "agent-notifier-gemini-hook >> /tmp/gemini_afteragent.log 2>&1",
  "timeout": 10000
}
```

Then run:

```bash
tail -n 20 /tmp/gemini_afteragent.log
```

### Ollama

- Pure interactive `ollama run` currently has no native per-turn completion hook.
- If you use `ollama launch codex` or `ollama launch claude`, configure Codex/Claude hooks above.
- For non-interactive JSON output (`--format json`), pipe into `agent-notifier ollama-hook`.

Example:

```bash
ollama run llama3 --format json | agent-notifier ollama-hook --name ollama --channel both
```

## Core Commands

`agent-notifier run -- <cmd...>`
- Run and notify on completion.
- Wrapper exits with the same exit code as the wrapped command.

`agent-notifier watch --pid <pid>`
- Watch an existing process ID until exit.

`agent-notifier test-notifier`
- Send a sample notification.

`agent-notifier tail --file <path> --pattern <text>`
- Notify when a log pattern appears.

Hook bridge commands used by integrations:
- `agent-notifier-codex-hook`
- `agent-notifier-gemini-hook`
- `agent-notifier-claude-hook`
- `agent-notifier codex-hook` (direct mode)
- `agent-notifier gemini-hook` (direct mode)
- `agent-notifier claude-hook` (direct mode)
- `agent-notifier ollama-hook`

## Common Customizations

Suppress notifications while terminal is focused (macOS):

```bash
agent-notifier gemini-hook --quiet-when-focused
```

Note: `--quiet-when-focused` is optional. If you copied older hook examples that included it by default and notifications seem missing, remove that flag.

Add sound:

```bash
agent-notifier claude-hook --chime ping
```

Force console output:

```bash
agent-notifier run --channel console -- your-command
```

## Configuration

Environment variables:

- `AGENT_NOTIFIER_TITLE_PREFIX="Agent"`
- `AGENT_NOTIFIER_CHANNELS="desktop,console"`
- `AGENT_NOTIFIER_TAIL_LINES=20`
- `AGENT_NOTIFIER_POLL_INTERVAL=1.0`

Optional TOML config at `~/.agentnotifier/config.toml`:

```toml
title_prefix = "Agent"
channels = ["desktop"]
tail_lines = 20
poll_interval = 1.0
```

Environment variables override file values.

## Troubleshooting

### I only get notifications when I exit the CLI

Check your tool hooks configuration. This project intentionally focuses on task-level notifications.
If notifications only appear on session exit, verify your CLI is calling `codex-hook`, `claude-hook`, or `gemini-hook` on task completion events.

### Desktop notifications do not appear

1. Test fallback path:
   - `agent-notifier test-notifier --channel console`
2. Test desktop + diagnostics:
   - `agent-notifier test-notifier --channel both --verbose`
3. Verify platform backend:
   - macOS uses `terminal-notifier` first, then `osascript`
   - Windows uses PowerShell/BurntToast (with `win10toast` fallback)
   - Linux uses `notify-send` (package `libnotify-bin` on Debian/Ubuntu)
4. macOS permission gate:
   - Notifications must be allowed manually for your terminal app in System Settings -> Notifications.

### Codex notifications not firing

1. Confirm `notify` is configured in `~/.codex/config.toml`.
2. Confirm bridge command/path resolves:
   - `command -v agent-notifier-codex-hook` (preferred), or
   - `chmod +x examples/codex_notifier_bridge.sh` (source checkout)
3. Restart Codex after config changes (required).
4. Enable bridge debug logs with `AGENT_NOTIFIER_DEBUG=1` and inspect `~/.agentnotifier/logs/codex_notifier.log`.
5. Do not use the `notifier` key in Codex config; use `notify`.

### Scenario Matrix (Quick Diagnosis)

Use this section to map a symptom to a concrete next action.

| Symptom | What it usually means | Verify | Fix |
| --- | --- | --- | --- |
| No notifications from Codex and log file never updates | Codex hook not wired | `tail -n 20 ~/.agentnotifier/logs/codex_notifier.log` after a completed turn | Use `notify = [...]` in `~/.codex/config.toml` and restart Codex |
| Codex log updates with `agent-turn-complete` payloads but no popup | Hook runs, desktop backend or OS UI is blocking | `agent-notifier test-notifier --channel both --verbose` | Enable notifications for your terminal app, disable Focus/Do Not Disturb, enable banners |
| Chime plays but no popup | Sound path works, visual notifications blocked by OS settings | Run `agent-notifier test-notifier --channel both --verbose` | In System Settings -> Notifications, allow alerts for terminal/iTerm and `terminal-notifier` (if listed) |
| `zsh: command not found: agent-notifier` | Installed in a different Python env or not on `PATH` | `command -v agent-notifier` | Use absolute binary path (for example `~/miniconda3/bin/agent-notifier-codex-hook`) or update `PATH` |
| Manual bridge test prints `[codex] Done` but Codex turns show nothing | Bridge itself is healthy; Codex config/session issue | Run manual test and compare with Codex log updates | Restart Codex fully; confirm the same bridge path is in `notify = [...]` |
| Manual bridge command returns no output but exit code is `0` | Event did not match (often malformed payload text) | Use key-value payload form | Run: `./examples/codex_notifier_bridge.sh --channel both --verbose type=agent-turn-complete turn-id=manual-check` |
| Gemini `AfterAgent` hook never runs | Project path is untrusted in Gemini | Set hook command to write `/tmp/gemini_afteragent.log` and verify file updates | Add project path as `TRUST_FOLDER` in `~/.gemini/trustedFolders.json`, restart Gemini |
| Desktop backend failure mentions `terminal-notifier` | `terminal-notifier` present but failing/crashing | `agent-notifier test-notifier --channel both --verbose` | Reinstall/fix `terminal-notifier` or rely on `osascript` fallback |
| Linux desktop popup missing | `notify-send` missing/unavailable | `command -v notify-send` | Install `libnotify-bin` (Debian/Ubuntu) or use `--channel console` |
| Windows desktop popup missing | BurntToast/PowerShell path unavailable | `agent-notifier test-notifier --channel both --verbose` | Install optional extras: `pip install "agent-notifier[windows]"` |

## Platform Notes

macOS:
- Desktop notifications via `terminal-notifier` (preferred, third-party) or Notification Center (`osascript` fallback).
- `terminal-notifier` is not an Apple-official binary; it is a widely used open-source tool.
- Install with Homebrew: `brew install terminal-notifier`
- You can still work without it because `osascript` fallback is built in.

Windows:
- Primary backend: PowerShell + BurntToast.
- Optional fallback dependency: `pip install "agent-notifier[windows]"`.

Linux:
- Desktop notifications via `notify-send`.
- Debian/Ubuntu install: `sudo apt install libnotify-bin`.

## For Maintainers

Development checks:

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest -q
python -m build
twine check dist/*
```

Release checklist:
- `docs/release_checklist.md`

Project/process docs:
- `docs/project_charter.md`
- `docs/scrum_working_agreement.md`
- `docs/assumptions.md`
- `docs/commit_plan.md`
- `SECURITY.md`

## License

MIT (`LICENSE`)
