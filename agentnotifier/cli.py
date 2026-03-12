"""Click command-line interface for agent-notifier."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from agentnotifier.config.config import AppConfig, load_config
from agentnotifier.core.notifications import (
    build_title,
    notifier_run_completion,
    notifier_watch_completion,
)
from agentnotifier.core.procinfo import get_process_name
from agentnotifier.core.result import RunResult, WatchResult
from agentnotifier.core.runner import ProcessRunner
from agentnotifier.core.watcher import ProcessWatcher
from agentnotifier.notifier.base import (
    NotificationLevel,
    Notifier,
    NotifierUnavailable,
)
from agentnotifier.notifier.console import ConsoleNotifier
from agentnotifier.notifier.linux import LinuxNotifier
from agentnotifier.notifier.macos import MacOSNotifier
from agentnotifier.notifier.windows import WindowsNotifier

CHANNEL_CHOICES = ["desktop", "console", "both"]
CHIME_CHOICES = ["none", "bell", "ping"]
TERMINAL_APP_NAMES = {
    "Terminal",
    "iTerm2",
    "Warp",
    "WezTerm",
    "Alacritty",
    "kitty",
    "Ghostty",
    "Hyper",
    "Tabby",
}

HOOK_STDIN_READ_TIMEOUT_SECONDS = 0.2


def _toml_basic_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_array(items: list[str]) -> str:
    return f"[{', '.join(_toml_basic_string(item) for item in items)}]"


def _resolve_codex_config_path(override_path: str | None) -> Path:
    if override_path:
        return Path(override_path).expanduser()

    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _upsert_codex_notify_config(*, existing_text: str, notify_line: str) -> str:
    lines = existing_text.splitlines(keepends=True)
    rebuilt_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() != "[notify]":
            rebuilt_lines.append(line)
            index += 1
            continue

        # Remove legacy table header and common bridge fields.
        # Keep unknown keys instead of dropping user content.
        index += 1
        while index < len(lines):
            current = lines[index]
            stripped = current.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            if stripped == "" or stripped.startswith("#"):
                index += 1
                continue
            if re.match(r"^\s*program\s*=", current):
                index += 1
                continue
            if re.match(r"^\s*args\s*=", current):
                balance = current.count("[") - current.count("]")
                index += 1
                while balance > 0 and index < len(lines):
                    balance += lines[index].count("[") - lines[index].count("]")
                    index += 1
                continue
            rebuilt_lines.append(current)
            index += 1

    updated = "".join(rebuilt_lines)

    patterns = [
        re.compile(r"(?ms)^notify\s*=\s*\[[^\]]*\]\s*"),
        re.compile(r"(?m)^notify\s*=.*$"),
    ]
    for pattern in patterns:
        if pattern.search(updated):
            return pattern.sub(notify_line, updated, count=1)

    lines = updated.splitlines(keepends=True)
    insert_at = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            insert_at = index
            break

    prefix = "".join(lines[:insert_at])
    suffix = "".join(lines[insert_at:])
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return f"{prefix}{notify_line}{suffix}"


def _escape_powershell_single_quoted(value: str) -> str:
    return value.replace("'", "''")


def _render_windows_codex_wrapper(*, hook_path: Path) -> str:
    escaped_hook = _escape_powershell_single_quoted(str(hook_path))
    return "\n".join(
        [
            "param(",
            "  [Parameter(ValueFromRemainingArguments = $true)]",
            "  [string[]]$ArgsFromCodex",
            ")",
            "",
            f"$hook = '{escaped_hook}'",
            "& $hook @ArgsFromCodex",
            "exit $LASTEXITCODE",
            "",
        ]
    )


def _resolve_codex_notify_command(
    *,
    hook_path: Path,
    codex_config_path: Path,
) -> tuple[list[str], Path | None]:
    resolved_hook = hook_path.resolve()
    if platform.system() != "Windows":
        return [str(resolved_hook)], None

    wrapper_path = codex_config_path.with_name("agent-notifier-codex-wrapper.ps1")
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    if wrapper_path.exists():
        wrapper_path.unlink()
    wrapper_path.write_text(
        _render_windows_codex_wrapper(hook_path=resolved_hook),
        encoding="utf-8",
    )
    resolved_wrapper = wrapper_path.resolve()
    return [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(resolved_wrapper),
    ], resolved_wrapper


class _DesktopConsoleNotifier(Notifier):
    """Deliver console output even if desktop delivery fails."""

    def __init__(self, desktop: Notifier, console: Notifier, *, verbose: bool) -> None:
        self._desktop = desktop
        self._console = console
        self._verbose = verbose

    def notifier(
        self,
        title: str,
        message: str,
        level: NotificationLevel = NotificationLevel.INFO,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        desktop_error: Exception | None = None
        try:
            self._desktop.notifier(title=title, message=message, level=level, metadata=metadata)
        except Exception as exc:  # pragma: no cover - defensive integration path
            desktop_error = exc

        self._console.notifier(title=title, message=message, level=level, metadata=metadata)
        if desktop_error is not None and self._verbose:
            _warn(
                "Desktop notification failed while console delivery succeeded: "
                f"{desktop_error}"
            )


@click.group(help="Notifier when long-running agentic commands complete.")
def app() -> None:
    """Root CLI group."""


def _warn(message: str) -> None:
    click.secho(message, fg="yellow", err=True)


def _clip_for_notification(value: object, limit: int) -> str:
    if value is None:
        return ""

    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    if limit <= 3:
        return compact[:limit]
    return f"{compact[: limit - 3]}..."


def _read_json_payload(
    *,
    payload_text: str,
    verbose: bool,
    source_label: str,
) -> dict[str, object] | None:
    if not payload_text.strip():
        if verbose:
            _warn(f"{source_label} payload missing. Skipping notification.")
        return None

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        if verbose:
            _warn(f"{source_label} payload is not valid JSON. Skipping notification.")
        return None

    if not isinstance(payload, dict):
        if verbose:
            _warn(f"{source_label} payload must be a JSON object. Skipping notification.")
        return None
    return payload


def _try_parse_json_object(payload_text: str) -> dict[str, object] | None:
    text = payload_text.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _read_json_lines_payload(
    *,
    payload_text: str,
    verbose: bool,
    source_label: str,
) -> dict[str, object] | None:
    if not payload_text.strip():
        if verbose:
            _warn(f"{source_label} payload missing. Skipping notification.")
        return None

    parsed: dict[str, object] | None = None
    for line in payload_text.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate

    if parsed is None:
        if verbose:
            _warn(
                f"{source_label} payload did not include JSON object lines. "
                "Skipping notification."
            )
        return None
    return parsed


def _extract_payload_text(payload: dict[str, object], keys: tuple[str, ...], limit: int) -> str:
    for key in keys:
        if key not in payload:
            continue
        clipped = _clip_for_notification(payload.get(key), limit)
        if clipped:
            return clipped
    return ""


def _extract_payload_event(payload: dict[str, object]) -> str:
    return _extract_payload_text(
        payload,
        (
            "hook_event_name",
            "hookEventName",
            "event_name",
            "eventName",
            "event",
            "type",
        ),
        80,
    )


def _read_hook_stdin_text(*, verbose: bool, source_label: str) -> str | None:
    """Read hook payload text from stdin without blocking on interactive terminals."""
    try:
        stdin_is_tty = sys.stdin.isatty()
    except Exception:
        stdin_is_tty = False

    if stdin_is_tty:
        if verbose:
            _warn(
                f"{source_label} expects hook payload via stdin or arguments. "
                "Interactive stdin detected; skipping notification."
            )
        return None

    read_result: dict[str, str | Exception] = {}
    read_complete = threading.Event()

    def _reader() -> None:
        try:
            read_result["text"] = sys.stdin.read()
        except Exception as exc:  # pragma: no cover - defensive
            read_result["error"] = exc
        finally:
            read_complete.set()

    thread = threading.Thread(target=_reader, name="agent-notifier-hook-stdin", daemon=True)
    thread.start()
    if not read_complete.wait(HOOK_STDIN_READ_TIMEOUT_SECONDS):
        if verbose:
            _warn(
                f"{source_label} received no stdin payload before timeout; "
                "skipping notification."
            )
        return None

    error = read_result.get("error")
    if isinstance(error, Exception):
        if verbose:
            _warn(f"{source_label} could not read stdin payload: {error}")
        return None
    return read_result.get("text", "")


def _normalize_event_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _extract_codex_event(payload: dict[str, object]) -> str:
    explicit_event = _extract_payload_event(payload)
    if explicit_event:
        return explicit_event

    # Codex legacy notifier payload may omit an explicit event field.
    legacy_keys = ("thread-id", "turn-id", "input-messages", "last-assistant-message")
    if any(key in payload for key in legacy_keys):
        return "after_agent"
    return ""


def _parse_codex_payload_parts(parts: tuple[str, ...]) -> dict[str, object] | None:
    if not parts:
        return None

    joined = " ".join(parts).strip()
    parsed = _try_parse_json_object(joined)
    if parsed is not None:
        return parsed

    result: dict[str, object] = {}
    loose_parts: list[str] = []
    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip()
            if key:
                result[key] = value.strip()
            continue
        loose_parts.append(part)

    if loose_parts and len(loose_parts) % 2 == 0:
        for index in range(0, len(loose_parts), 2):
            key = loose_parts[index].strip()
            if key:
                result[key] = loose_parts[index + 1]
    elif loose_parts and not result:
        # Last-resort fallback for unknown positional payloads.
        result["raw_payload"] = joined

    return result or None


def _is_user_focused_on_terminal(*, verbose: bool) -> bool:
    if platform.system() != "Darwin":
        return False
    if shutil.which("osascript") is None:
        return False

    script = (
        'tell application "System Events" '
        "to get name of first application process whose frontmost is true"
    )
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        if verbose:
            _warn(f"Unable to detect frontmost app: {exc}")
        return False

    if completed.returncode != 0:
        if verbose:
            stderr = completed.stderr.strip() or "unknown osascript error"
            _warn(f"Unable to detect frontmost app: {stderr}")
        return False

    frontmost = completed.stdout.strip()
    return frontmost in TERMINAL_APP_NAMES


def _play_chime(chime: str, *, verbose: bool) -> None:
    selected = chime.lower()
    if selected == "none":
        return

    if selected == "bell":
        try:
            sys.stderr.write("\a")
            sys.stderr.flush()
        except Exception as exc:
            if verbose:
                _warn(f"Bell chime failed: {exc}")
        return

    if selected != "ping":
        return

    system = platform.system()
    if system == "Darwin":
        afplay = shutil.which("afplay")
        if not afplay:
            if verbose:
                _warn("afplay not found; falling back to terminal bell.")
            _play_chime("bell", verbose=verbose)
            return

        # Prefer system Ping sound; fallback to bell if unavailable.
        sound_candidates = [
            "/System/Library/Sounds/Ping.aiff",
            "/System/Library/Sounds/Glass.aiff",
        ]
        for sound_path in sound_candidates:
            if not Path(sound_path).exists():
                continue
            completed = subprocess.run(
                [afplay, sound_path],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                return

        if verbose:
            _warn("Unable to play macOS ping sound; falling back to terminal bell.")
        _play_chime("bell", verbose=verbose)
        return

    if system == "Windows":
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "[console]::beep(1000,220)",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 and verbose:
            _warn("Windows ping chime failed; falling back to terminal bell.")
        if completed.returncode != 0:
            _play_chime("bell", verbose=verbose)
        return

    # For Linux/other platforms, use terminal bell as a minimal fallback.
    _play_chime("bell", verbose=verbose)


def _build_desktop_notifier() -> Notifier:
    system = platform.system()
    if system == "Darwin":
        return MacOSNotifier()
    if system == "Windows":
        return WindowsNotifier()
    if system == "Linux":
        return LinuxNotifier()
    raise NotifierUnavailable(f"Desktop notifications are not implemented for {system}")


def _resolve_notifier(channel: str, verbose: bool) -> Notifier:
    if channel == "console":
        return ConsoleNotifier()

    if channel == "both":
        console = ConsoleNotifier()
        try:
            desktop = _build_desktop_notifier()
        except NotifierUnavailable as exc:
            if verbose:
                _warn(f"Desktop notifier unavailable: {exc}. Falling back to console only.")
            return console
        return _DesktopConsoleNotifier(desktop=desktop, console=console, verbose=verbose)

    try:
        return _build_desktop_notifier()
    except NotifierUnavailable as exc:
        _warn(f"Desktop notifier unavailable: {exc}. Falling back to console output.")
        return ConsoleNotifier()


def _default_channel(config: AppConfig) -> str:
    channels = [entry.lower() for entry in config.channels or []]
    if "console" in channels and "desktop" in channels:
        return "both"
    if "console" in channels:
        return "console"
    return "desktop"


def _infer_tool_name_from_command(command: tuple[str, ...]) -> str | None:
    if not command:
        return None

    executable = Path(command[0]).name.strip()
    if not executable:
        return None

    lowered = executable.lower()
    if lowered.endswith(".exe"):
        executable = executable[:-4]
        lowered = executable.lower()

    # Prefer module name for "python -m <module>" invocations.
    if lowered.startswith("python") and "-m" in command:
        module_index = command.index("-m") + 1
        if module_index < len(command):
            module_name = command[module_index].strip()
            if module_name:
                return module_name

    return executable


def _infer_tool_name_from_pid(pid: int) -> str | None:
    process_name = get_process_name(pid)
    if process_name:
        return process_name
    if pid > 0:
        return f"pid-{pid}"
    return None


def _notifier_run_with_fallback(
    *,
    notifier: Notifier,
    result: RunResult,
    tool_name: str | None,
    title_override: str | None,
    default_tool_name: str,
    verbose: bool,
) -> None:
    try:
        notifier_run_completion(
            notifier,
            result,
            tool_name=tool_name,
            title_override=title_override,
            default_tool_name=default_tool_name,
        )
    except Exception as exc:
        _warn(f"Desktop notification failed: {exc}. Falling back to console output.")
        if verbose:
            _warn("Use --channel console to avoid desktop notifier errors in headless sessions.")
        notifier_run_completion(
            ConsoleNotifier(),
            result,
            tool_name=tool_name,
            title_override=title_override,
            default_tool_name=default_tool_name,
        )


def _notifier_watch_with_fallback(
    *,
    notifier: Notifier,
    result: WatchResult,
    tool_name: str | None,
    title_override: str | None,
    default_tool_name: str,
    verbose: bool,
) -> None:
    try:
        notifier_watch_completion(
            notifier,
            result,
            tool_name=tool_name,
            title_override=title_override,
            default_tool_name=default_tool_name,
        )
    except Exception as exc:
        _warn(f"Desktop notification failed: {exc}. Falling back to console output.")
        if verbose:
            _warn("Use --channel console to avoid desktop notifier errors in headless sessions.")
        notifier_watch_completion(
            ConsoleNotifier(),
            result,
            tool_name=tool_name,
            title_override=title_override,
            default_tool_name=default_tool_name,
        )


@app.command(
    "setup-codex",
    help="Configure Codex notify hook in ~/.codex/config.toml (idempotent).",
)
@click.option(
    "--codex-config",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override Codex config.toml path. Default: $CODEX_HOME/config.toml or ~/.codex/config.toml",
)
@click.option(
    "--hook-path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Absolute path to agent-notifier-codex-hook executable.",
)
@click.option(
    "--backup/--no-backup",
    default=True,
    show_default=True,
    help="Create timestamped backup before updating existing config.",
)
def setup_codex_command(
    codex_config: Path | None,
    hook_path: Path | None,
    backup: bool,
) -> None:
    resolved_hook = hook_path.expanduser() if hook_path else None
    if resolved_hook is None:
        discovered = shutil.which("agent-notifier-codex-hook")
        if discovered is None:
            click.secho(
                "Could not find 'agent-notifier-codex-hook' on PATH. "
                "Install agent-notifier first or pass --hook-path.",
                fg="red",
                err=True,
            )
            raise SystemExit(1)
        resolved_hook = Path(discovered)
    if not resolved_hook.exists():
        click.secho(f"Hook path does not exist: {resolved_hook}", fg="red", err=True)
        raise SystemExit(1)

    config_path = _resolve_codex_config_path(str(codex_config) if codex_config else None)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    notify_command, wrapper_path = _resolve_codex_notify_command(
        hook_path=resolved_hook,
        codex_config_path=config_path,
    )

    existing_text = ""
    if config_path.exists():
        existing_text = config_path.read_text(encoding="utf-8")
        if backup:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup_path = config_path.with_name(f"{config_path.name}.bak.{stamp}")
            backup_path.write_text(existing_text, encoding="utf-8")
            click.echo(f"Backed up {config_path} -> {backup_path}")

    notify_line = f"notify = {_toml_array(notify_command)}\n"
    updated_text = _upsert_codex_notify_config(
        existing_text=existing_text,
        notify_line=notify_line,
    )
    config_path.write_text(updated_text, encoding="utf-8")

    click.echo(f"Codex config: {config_path}")
    click.echo(f"Hook path: {resolved_hook.resolve()}")
    if wrapper_path is not None:
        click.echo(f"Wrapper path: {wrapper_path}")
    click.echo("Updated top-level notify hook for Codex.")


@app.command(
    "run",
    context_settings={"ignore_unknown_options": True},
    help="Run a command, wait for completion, and send notification.",
)
@click.option("--name", type=str, default=None, help="Tool name shown in notification title.")
@click.option("--title", type=str, default=None, help="Override notification title.")
@click.option(
    "--tail-lines",
    type=click.IntRange(min=1),
    default=None,
    help="Include last N lines of combined stdout/stderr in notification body.",
)
@click.option("--no-capture", is_flag=True, help="Disable output capture.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
def run_command(
    name: str | None,
    title: str | None,
    tail_lines: int | None,
    no_capture: bool,
    channel: str | None,
    verbose: bool,
    command: tuple[str, ...],
) -> None:
    if not command:
        raise click.UsageError("Missing command. Usage: agent-notifier run -- <cmd...>")

    config = load_config()
    selected_channel = channel or _default_channel(config)
    notifier = _resolve_notifier(selected_channel, verbose=verbose)
    effective_name = name or _infer_tool_name_from_command(command)

    runner = ProcessRunner()
    effective_tail = tail_lines if tail_lines is not None else config.tail_lines

    try:
        result = runner.run(
            list(command),
            tool_name=effective_name,
            capture_output=not no_capture,
            tail_lines=effective_tail,
        )
    except FileNotFoundError as exc:
        click.secho(f"Command not found: {exc.filename}", fg="red", err=True)
        raise SystemExit(127) from exc
    except Exception as exc:
        click.secho(f"Failed to run command: {exc}", fg="red", err=True)
        raise SystemExit(1) from exc

    _notifier_run_with_fallback(
        notifier=notifier,
        result=result,
        tool_name=effective_name,
        title_override=title,
        default_tool_name=config.title_prefix,
        verbose=verbose,
    )

    raise SystemExit(result.exit_code)


@app.command("emit", help="Send a completion notification from external integrations.")
@click.option("--name", type=str, default=None, help="Tool name shown in notification title.")
@click.option("--title", type=str, default=None, help="Override notification title.")
@click.option(
    "--command",
    "command_text",
    type=str,
    required=True,
    help="Command string to include.",
)
@click.option(
    "--duration-seconds",
    type=click.FloatRange(min=0.0),
    required=True,
    help="Elapsed duration in seconds.",
)
@click.option("--exit-code", type=int, required=True, help="Command exit code.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
def emit_command(
    name: str | None,
    title: str | None,
    command_text: str,
    duration_seconds: float,
    exit_code: int,
    channel: str | None,
    verbose: bool,
) -> None:
    config = load_config()
    selected_channel = channel or _default_channel(config)
    notifier = _resolve_notifier(selected_channel, verbose=verbose)

    parsed_command = shlex.split(command_text) if command_text else []
    effective_name = name or _infer_tool_name_from_command(tuple(parsed_command))

    now = datetime.now(timezone.utc)
    result = RunResult(
        command=parsed_command or [command_text],
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        output_tail=[],
        started_at=now,
        ended_at=now,
        tool_name=effective_name,
    )

    _notifier_run_with_fallback(
        notifier=notifier,
        result=result,
        tool_name=effective_name,
        title_override=title,
        default_tool_name=config.title_prefix,
        verbose=verbose,
    )


def _notifier_task_hook(
    *,
    name: str,
    title: str | None,
    channel: str | None,
    quiet_when_focused: bool,
    chime: str,
    verbose: bool,
    body_lines: list[str],
    metadata: dict[str, object],
) -> None:
    if quiet_when_focused and _is_user_focused_on_terminal(verbose=verbose):
        if verbose:
            _warn("Skipping notification because your terminal is frontmost.")
        return

    config = load_config()
    selected_channel = channel or _default_channel(config)
    notifier = _resolve_notifier(selected_channel, verbose=verbose)

    rendered_lines = [line for line in body_lines if line.strip()]
    if not rendered_lines:
        rendered_lines = ["Task event received."]

    title_text = build_title(
        exit_code=0,
        tool_name=name,
        title_override=title,
        default_tool_name=config.title_prefix,
    )
    body_text = "\n".join(rendered_lines)

    try:
        notifier.notifier(
            title=title_text,
            message=body_text,
            level=NotificationLevel.SUCCESS,
            metadata=metadata,
        )
    except Exception as exc:
        _warn(f"Desktop notification failed: {exc}. Falling back to console output.")
        if verbose:
            _warn("Use --channel console to avoid desktop notifier errors in headless sessions.")
        ConsoleNotifier().notifier(
            title=title_text,
            message=body_text,
            level=NotificationLevel.SUCCESS,
            metadata=metadata,
        )

    _play_chime(chime, verbose=verbose)


def _payload_event_matches(hook_event: str, target_event: str) -> bool:
    actual = _normalize_event_name(hook_event)
    target = _normalize_event_name(target_event)
    if actual == target:
        return True

    # Backward/forward compatibility between Codex legacy and current names.
    aliases: dict[str, set[str]] = {
        "after-agent": {"after-agent", "agent-turn-complete"},
        "agent-turn-complete": {"after-agent", "agent-turn-complete"},
    }
    return target in aliases.get(actual, {actual})


@app.command("gemini-hook", help="Send notification from Gemini CLI hook events (task-level).")
@click.option(
    "--event",
    "target_event",
    type=str,
    default="AfterAgent",
    show_default=True,
    help="Gemini hook event name that should trigger a notification.",
)
@click.option("--name", type=str, default="gemini", show_default=True, help="Tool name in title.")
@click.option("--title", type=str, default=None, help="Override notification title.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option(
    "--max-prompt-chars",
    type=click.IntRange(min=20),
    default=160,
    show_default=True,
    help="Maximum prompt length to include in the notification body.",
)
@click.option(
    "--max-response-chars",
    type=click.IntRange(min=20),
    default=220,
    show_default=True,
    help="Maximum response length to include in the notification body.",
)
@click.option(
    "--quiet-when-focused/--notifier-when-focused",
    default=False,
    show_default=True,
    help="Skip notifications when your terminal app is frontmost (macOS).",
)
@click.option(
    "--chime",
    type=click.Choice(CHIME_CHOICES, case_sensitive=False),
    default="none",
    show_default=True,
    help="Optional sound: none, bell, or ping.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
def gemini_hook_command(
    target_event: str,
    name: str,
    title: str | None,
    channel: str | None,
    max_prompt_chars: int,
    max_response_chars: int,
    quiet_when_focused: bool,
    chime: str,
    verbose: bool,
) -> None:
    payload_text = _read_hook_stdin_text(verbose=verbose, source_label="Gemini hook")
    if payload_text is None:
        return

    payload = _read_json_payload(
        payload_text=payload_text,
        verbose=verbose,
        source_label="Gemini hook",
    )
    if payload is None:
        return

    hook_event = _extract_payload_event(payload)
    if not _payload_event_matches(hook_event, target_event):
        return

    prompt = _extract_payload_text(
        payload,
        (
            "prompt",
            "user_prompt",
            "userPrompt",
        ),
        max_prompt_chars,
    )
    response = _extract_payload_text(
        payload,
        (
            "prompt_response",
            "promptResponse",
            "response",
        ),
        max_response_chars,
    )
    session_id = _extract_payload_text(payload, ("session_id", "sessionId"), 64)

    body_lines: list[str] = [f"Event: {hook_event}"]
    if prompt:
        body_lines.append(f"Prompt: {prompt}")
    if response:
        body_lines.append(f"Response: {response}")
    if session_id:
        body_lines.append(f"Session: {session_id}")

    _notifier_task_hook(
        name=name,
        title=title,
        channel=channel,
        quiet_when_focused=quiet_when_focused,
        chime=chime,
        verbose=verbose,
        body_lines=body_lines,
        metadata={
            "event": hook_event,
            "session_id": payload.get("session_id"),
            "has_prompt": bool(prompt),
            "has_response": bool(response),
            "source": "gemini",
        },
    )


@app.command("claude-hook", help="Send notification from Claude Code hook events (task-level).")
@click.option(
    "--event",
    "target_event",
    type=str,
    default="Stop",
    show_default=True,
    help="Claude hook event name that should trigger a notification.",
)
@click.option(
    "--name",
    type=str,
    default="claude-code",
    show_default=True,
    help="Tool name in title.",
)
@click.option("--title", type=str, default=None, help="Override notification title.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option(
    "--max-user-prompt-chars",
    type=click.IntRange(min=20),
    default=160,
    show_default=True,
    help="Maximum user prompt length to include in the notification body.",
)
@click.option(
    "--max-result-chars",
    type=click.IntRange(min=20),
    default=220,
    show_default=True,
    help="Maximum tool/result text length to include in the notification body.",
)
@click.option(
    "--quiet-when-focused/--notifier-when-focused",
    default=False,
    show_default=True,
    help="Skip notifications when your terminal app is frontmost (macOS).",
)
@click.option(
    "--chime",
    type=click.Choice(CHIME_CHOICES, case_sensitive=False),
    default="none",
    show_default=True,
    help="Optional sound: none, bell, or ping.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
def claude_hook_command(
    target_event: str,
    name: str,
    title: str | None,
    channel: str | None,
    max_user_prompt_chars: int,
    max_result_chars: int,
    quiet_when_focused: bool,
    chime: str,
    verbose: bool,
) -> None:
    payload_text = _read_hook_stdin_text(verbose=verbose, source_label="Claude hook")
    if payload_text is None:
        return

    payload = _read_json_payload(
        payload_text=payload_text,
        verbose=verbose,
        source_label="Claude hook",
    )
    if payload is None:
        return

    hook_event = _extract_payload_event(payload)
    if not _payload_event_matches(hook_event, target_event):
        return

    user_prompt = _extract_payload_text(payload, ("user_prompt",), max_user_prompt_chars)
    reason = _extract_payload_text(payload, ("reason",), 120)
    tool_name = _extract_payload_text(payload, ("tool_name",), 80)
    tool_result = _extract_payload_text(payload, ("tool_result",), max_result_chars)
    session_id = _extract_payload_text(payload, ("session_id",), 64)

    body_lines: list[str] = [f"Event: {hook_event}"]
    if reason:
        body_lines.append(f"Reason: {reason}")
    if user_prompt:
        body_lines.append(f"Prompt: {user_prompt}")
    if tool_name:
        body_lines.append(f"Tool: {tool_name}")
    if tool_result:
        body_lines.append(f"Result: {tool_result}")
    if session_id:
        body_lines.append(f"Session: {session_id}")

    _notifier_task_hook(
        name=name,
        title=title,
        channel=channel,
        quiet_when_focused=quiet_when_focused,
        chime=chime,
        verbose=verbose,
        body_lines=body_lines,
        metadata={
            "event": hook_event,
            "session_id": payload.get("session_id"),
            "reason": payload.get("reason"),
            "tool_name": payload.get("tool_name"),
            "source": "claude",
        },
    )


@app.command("codex-hook", help="Send notification from Codex notifier hook payloads (task-level).")
@click.option(
    "--event",
    "target_event",
    type=str,
    default="agent-turn-complete",
    show_default=True,
    help="Codex event type that should trigger a notification.",
)
@click.option("--name", type=str, default="codex", show_default=True, help="Tool name in title.")
@click.option("--title", type=str, default=None, help="Override notification title.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option(
    "--max-input-chars",
    type=click.IntRange(min=20),
    default=140,
    show_default=True,
    help="Maximum input preview length in the notification body.",
)
@click.option(
    "--max-assistant-chars",
    type=click.IntRange(min=20),
    default=220,
    show_default=True,
    help="Maximum assistant message length in the notification body.",
)
@click.option(
    "--quiet-when-focused/--notifier-when-focused",
    default=False,
    show_default=True,
    help="Skip notifications when your terminal app is frontmost (macOS).",
)
@click.option(
    "--chime",
    type=click.Choice(CHIME_CHOICES, case_sensitive=False),
    default="none",
    show_default=True,
    help="Optional sound: none, bell, or ping.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
@click.argument("payload_parts", nargs=-1)
def codex_hook_command(
    target_event: str,
    name: str,
    title: str | None,
    channel: str | None,
    max_input_chars: int,
    max_assistant_chars: int,
    quiet_when_focused: bool,
    chime: str,
    verbose: bool,
    payload_parts: tuple[str, ...],
) -> None:
    payload_obj = _parse_codex_payload_parts(payload_parts)
    if payload_obj is None:
        payload_text = _read_hook_stdin_text(verbose=verbose, source_label="Codex hook")
        if payload_text is None:
            return
        payload_obj = _try_parse_json_object(payload_text)
    if payload_obj is None:
        if verbose:
            _warn("Codex hook payload missing. Skipping notification.")
        return

    hook_event = _extract_codex_event(payload_obj)
    if not hook_event:
        if verbose:
            _warn("Codex hook payload missing event details. Skipping notification.")
        return
    if not _payload_event_matches(hook_event, target_event):
        return

    input_preview = _extract_payload_text(
        payload_obj,
        ("input-messages", "input_messages"),
        max_input_chars,
    )
    assistant_preview = _extract_payload_text(
        payload_obj,
        ("last-assistant-message", "last_assistant_message"),
        max_assistant_chars,
    )
    cwd = _extract_payload_text(payload_obj, ("cwd",), 120)
    turn_id = _extract_payload_text(payload_obj, ("turn-id", "turn_id"), 64)

    body_lines: list[str] = [f"Event: {hook_event}"]
    if input_preview:
        body_lines.append(f"Input: {input_preview}")
    if assistant_preview:
        body_lines.append(f"Assistant: {assistant_preview}")
    if cwd:
        body_lines.append(f"CWD: {cwd}")
    if turn_id:
        body_lines.append(f"Turn: {turn_id}")

    _notifier_task_hook(
        name=name,
        title=title,
        channel=channel,
        quiet_when_focused=quiet_when_focused,
        chime=chime,
        verbose=verbose,
        body_lines=body_lines,
        metadata={
            "event": hook_event,
            "turn_id": payload_obj.get("turn-id") or payload_obj.get("turn_id"),
            "cwd": payload_obj.get("cwd"),
            "source": "codex",
        },
    )


@app.command(
    "ollama-hook",
    help="Send notification from Ollama JSON/JSONL output (use with --format json).",
)
@click.option("--name", type=str, default="ollama", show_default=True, help="Tool name in title.")
@click.option("--title", type=str, default=None, help="Override notification title.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option(
    "--max-response-chars",
    type=click.IntRange(min=20),
    default=220,
    show_default=True,
    help="Maximum response text length in the notification body.",
)
@click.option(
    "--quiet-when-focused/--notifier-when-focused",
    default=False,
    show_default=True,
    help="Skip notifications when your terminal app is frontmost (macOS).",
)
@click.option(
    "--chime",
    type=click.Choice(CHIME_CHOICES, case_sensitive=False),
    default="none",
    show_default=True,
    help="Optional sound: none, bell, or ping.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
def ollama_hook_command(
    name: str,
    title: str | None,
    channel: str | None,
    max_response_chars: int,
    quiet_when_focused: bool,
    chime: str,
    verbose: bool,
) -> None:
    payload_text = _read_hook_stdin_text(verbose=verbose, source_label="Ollama hook")
    if payload_text is None:
        return

    payload = _read_json_lines_payload(
        payload_text=payload_text,
        verbose=verbose,
        source_label="Ollama hook",
    )
    if payload is None:
        return

    # Ollama streams JSON lines and marks completion with done=true.
    done_flag = payload.get("done")
    if isinstance(done_flag, bool) and not done_flag:
        return

    model = _extract_payload_text(payload, ("model",), 80)
    response = _extract_payload_text(payload, ("response",), max_response_chars)
    done_reason = _extract_payload_text(payload, ("done_reason",), 80)

    body_lines: list[str] = ["Event: done"]
    if model:
        body_lines.append(f"Model: {model}")
    if response:
        body_lines.append(f"Response: {response}")
    if done_reason:
        body_lines.append(f"Reason: {done_reason}")

    _notifier_task_hook(
        name=name,
        title=title,
        channel=channel,
        quiet_when_focused=quiet_when_focused,
        chime=chime,
        verbose=verbose,
        body_lines=body_lines,
        metadata={
            "event": "done",
            "model": payload.get("model"),
            "done_reason": payload.get("done_reason"),
            "source": "ollama",
        },
    )


@app.command("watch", help="Watch an existing process ID until it exits.")
@click.option("--pid", type=click.IntRange(min=1), required=True, help="PID to monitor until exit.")
@click.option("--name", type=str, default=None, help="Tool name shown in notification title.")
@click.option("--title", type=str, default=None, help="Override notification title.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option(
    "--poll-interval",
    type=click.FloatRange(min=0.05),
    default=None,
    help="Polling interval in seconds.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
def watch_command(
    pid: int,
    name: str | None,
    title: str | None,
    channel: str | None,
    poll_interval: float | None,
    verbose: bool,
) -> None:
    config = load_config()
    selected_channel = channel or _default_channel(config)
    notifier = _resolve_notifier(selected_channel, verbose=verbose)
    effective_name = name or _infer_tool_name_from_pid(pid)

    watcher = ProcessWatcher(poll_interval=poll_interval or config.poll_interval)
    result = watcher.wait_for_exit(pid)

    _notifier_watch_with_fallback(
        notifier=notifier,
        result=result,
        tool_name=effective_name,
        title_override=title,
        default_tool_name=config.title_prefix,
        verbose=verbose,
    )

    if result.exit_code is not None:
        raise SystemExit(result.exit_code)
    raise SystemExit(0)


@app.command("test-notifier", help="Send a sample notification.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
def test_notifier_command(channel: str | None, verbose: bool) -> None:
    config = load_config()
    selected_channel = channel or _default_channel(config)
    notifier = _resolve_notifier(selected_channel, verbose=verbose)

    title_text = f"[{config.title_prefix}] Test Notification"
    body_text = "agent-notifier is installed and can send notifications."

    try:
        notifier.notifier(title=title_text, message=body_text)
    except Exception as exc:
        _warn(f"Desktop notification failed: {exc}. Falling back to console output.")
        ConsoleNotifier().notifier(title=title_text, message=body_text)


@app.command("tail", help="Watch a log file and notifier when a pattern appears.")
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    required=True,
)
@click.option("--pattern", type=str, required=True, help="Pattern that triggers notification.")
@click.option("--name", type=str, default=None, help="Tool name shown in notification title.")
@click.option("--title", type=str, default=None, help="Override notification title.")
@click.option(
    "--channel",
    type=click.Choice(CHANNEL_CHOICES, case_sensitive=False),
    default=None,
    help="Notification channel: desktop, console, or both.",
)
@click.option(
    "--poll-interval",
    type=click.FloatRange(min=0.05),
    default=0.5,
    show_default=True,
)
@click.option("--verbose", is_flag=True, help="Enable verbose logs.")
def tail_command(
    file_path: Path,
    pattern: str,
    name: str | None,
    title: str | None,
    channel: str | None,
    poll_interval: float,
    verbose: bool,
) -> None:
    config = load_config()
    selected_channel = channel or _default_channel(config)
    notifier = _resolve_notifier(selected_channel, verbose=verbose)

    started = time.monotonic()
    with file_path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline()
            if not line:
                time.sleep(poll_interval)
                continue

            if pattern in line:
                duration = time.monotonic() - started
                title_text = title or f"[{name or config.title_prefix}] Done"
                body_text = f"Pattern '{pattern}' detected in {duration:.2f}s\nFile: {file_path}"
                try:
                    notifier.notifier(title=title_text, message=body_text)
                except Exception as exc:
                    _warn(f"Desktop notification failed: {exc}. Falling back to console output.")
                    ConsoleNotifier().notifier(title=title_text, message=body_text)
                return


def main() -> None:
    app(prog_name="agent-notifier")


if __name__ == "__main__":
    main()
