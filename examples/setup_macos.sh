#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This helper is for macOS only." >&2
  exit 1
fi

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
TRUST_DIR="$(pwd)"
CONFIGURE_CODEX=1
CONFIGURE_GEMINI=1
SKIP_INSTALL=0
OPEN_SETTINGS=0

usage() {
  cat <<'USAGE'
Usage: ./examples/setup_macos.sh [options]

Options:
  --trust-dir <path>     Project path to mark as TRUST_FOLDER for Gemini.
                         Default: current working directory.
  --no-codex             Do not edit ~/.codex/config.toml.
  --no-gemini            Do not edit ~/.gemini/settings.json and trustedFolders.
  --skip-install         Skip package/dependency installation.
  --open-settings        Open macOS Notification settings at the end.
  -h, --help             Show this help.
USAGE
}

log() {
  printf '[setup] %s\n' "$*"
}

warn() {
  printf '[setup:warn] %s\n' "$*" >&2
}

resolve_bin() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
    return 0
  fi

  for candidate in \
    "$HOME/.local/bin/$name" \
    "$HOME/miniconda3/bin/$name" \
    "$HOME/anaconda3/bin/$name"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --trust-dir)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --trust-dir" >&2
        exit 1
      fi
      TRUST_DIR="$2"
      shift 2
      ;;
    --no-codex)
      CONFIGURE_CODEX=0
      shift
      ;;
    --no-gemini)
      CONFIGURE_GEMINI=0
      shift
      ;;
    --skip-install)
      SKIP_INSTALL=1
      shift
      ;;
    --open-settings)
      OPEN_SETTINGS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  if command -v brew >/dev/null 2>&1; then
    if ! command -v terminal-notifier >/dev/null 2>&1; then
      log "Installing terminal-notifier via Homebrew"
      brew install terminal-notifier
    else
      log "terminal-notifier already installed"
    fi
  else
    warn "Homebrew not found; skipping terminal-notifier install (osascript fallback still works)."
  fi

  if ! command -v pipx >/dev/null 2>&1; then
    log "Installing pipx (user site)"
    python3 -m pip install --user pipx
    python3 -m pipx ensurepath || true
    export PATH="$HOME/.local/bin:$PATH"
  fi

  if command -v pipx >/dev/null 2>&1; then
    log "Installing/updating agent-notifier from local source checkout"
    pipx install --force "$PROJECT_ROOT"
  else
    warn "pipx still unavailable; falling back to pip user install"
    python3 -m pip install --user --upgrade "$PROJECT_ROOT"
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

if ! AGENT_NOTIFIER_BIN="$(resolve_bin agent-notifier)"; then
  echo "Could not find agent-notifier on PATH after install." >&2
  exit 1
fi
if ! CODEX_HOOK_BIN="$(resolve_bin agent-notifier-codex-hook)"; then
  echo "Could not find agent-notifier-codex-hook on PATH after install." >&2
  exit 1
fi
if ! GEMINI_HOOK_BIN="$(resolve_bin agent-notifier-gemini-hook)"; then
  echo "Could not find agent-notifier-gemini-hook on PATH after install." >&2
  exit 1
fi

log "agent-notifier binary: $AGENT_NOTIFIER_BIN"
log "codex hook: $CODEX_HOOK_BIN"
log "gemini hook: $GEMINI_HOOK_BIN"

if [[ "$CONFIGURE_CODEX" -eq 1 ]]; then
  log "Updating ~/.codex/config.toml notify hook"
  "$AGENT_NOTIFIER_BIN" setup codex
fi

if [[ "$CONFIGURE_GEMINI" -eq 1 ]]; then
  log "Updating Gemini hook config and trusted folder"
  "$AGENT_NOTIFIER_BIN" setup gemini --project "$TRUST_DIR"
fi

log "Running notifier self-test"
if ! "$AGENT_NOTIFIER_BIN" test-notifier --channel both --verbose; then
  warn "Self-test failed. Run with --channel console to validate basic install."
fi

log "Running environment doctor"
if ! "$AGENT_NOTIFIER_BIN" doctor --project "$TRUST_DIR"; then
  warn "Environment doctor reported issues. Review the output before shipping this setup."
fi

log "Running Codex bridge smoke test"
if ! "$CODEX_HOOK_BIN" --channel both --verbose type=agent-turn-complete turn-id=setup-check; then
  warn "Codex bridge smoke test failed. Check your Python environment and PATH."
fi

if [[ "$OPEN_SETTINGS" -eq 1 ]]; then
  open "x-apple.systempreferences:com.apple.preference.notifications" || true
fi

cat <<'NEXT_STEPS'

Next steps:
1. Restart Codex and Gemini so they pick up updated hook config.
2. Run one real prompt in each tool to validate end-to-end behavior.
3. If chime works but popup is missing, manually allow notifications for your terminal app in System Settings -> Notifications.

Note: macOS does not allow command-line tools to auto-grant notification permission for apps. That permission step is always manual.
NEXT_STEPS
