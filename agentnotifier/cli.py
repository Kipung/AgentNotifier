"""Click command-line interface for agent-notifier."""

from __future__ import annotations

import json
import platform
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from agentnotifier.config.config import AppConfig, load_config
from agentnotifier.core.doctor import collect_doctor_report, render_doctor_report
from agentnotifier.core.notifications import (
    build_title,
    notifier_run_completion,
    notifier_watch_completion,
)
from agentnotifier.core.procinfo import get_process_name
from agentnotifier.core.result import RunResult, WatchResult
from agentnotifier.core.runner import ProcessRunner
from agentnotifier.core.setup import (
    SetupError,
    ensure_claude_hooks,
    ensure_codex_notify_hook,
    ensure_gemini_hook,
    resolve_claude_hook_command,
    resolve_codex_hook_command,
    resolve_gemini_hook_command,
)
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


def _read_stdin_text(*, source_label: str, verbose: bool) -> str | None:
    try:
        if sys.stdin.isatty():
            if verbose:
                _warn(
                    f"{source_label} payload missing. "
                    "No piped stdin detected; skipping notification."
                )
            return None
    except Exception:
        pass

    try:
        return sys.stdin.read()
    except Exception as exc:
        if verbose:
            _warn(f"Unable to read {source_label} payload from stdin: {exc}")
        return None


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
    payload_text = _read_stdin_text(source_label="Gemini hook", verbose=verbose)
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
    payload_text = _read_stdin_text(source_label="Claude hook", verbose=verbose)
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
        payload_text = _read_stdin_text(source_label="Codex hook", verbose=verbose)
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
    payload_text = _read_stdin_text(source_label="Ollama hook", verbose=verbose)
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


@app.group("setup", help="Configure agent-notifier integrations.")
def setup_group() -> None:
    """Integration setup commands."""


def _render_setup_path(path: Path) -> str:
    return str(path.expanduser())


def _emit_setup_file_result(label: str, *, path: Path, changed: bool, backup_path: Path | None) -> None:
    if backup_path is not None:
        click.echo(f"{label} backup: {_render_setup_path(backup_path)}")
    status = "updated" if changed else "already configured"
    click.echo(f"{label}: {status} ({_render_setup_path(path)})")


def _emit_macos_notification_reminder() -> None:
    """Print a macOS-specific notification permission reminder after setup.

    On macOS, ``osascript`` always exits with code 0 — even when macOS
    silently drops the notification because the calling app has not been
    granted notification permission.  There is no way for agent-notifier to
    detect or fix this automatically; it requires a one-time manual step in
    System Settings.  We print this reminder after every setup command so
    users have the information immediately rather than discovering the gap
    after a long agent run produces no popup.
    """
    if platform.system() != "Darwin":
        return
    click.echo("")
    click.echo("NOTE (macOS — action required):")
    click.echo(
        "  Desktop notifications need permission for your terminal app in System Settings."
    )
    click.echo(
        "  macOS silently drops notifications without permission and osascript still"
    )
    click.echo("  exits 0, so there is no error even when nothing appears.")
    click.echo("")
    click.echo("  Steps:")
    click.echo(
        "    1. Open System Settings -> Notifications"
    )
    click.echo(
        "    2. Find your terminal app (Terminal / iTerm2 / Warp / WezTerm)"
    )
    click.echo(
        "    3. Set 'Allow Notifications' ON and choose 'Banners' or 'Alerts'"
    )
    click.echo(
        "    4. If terminal-notifier is installed, allow it too"
    )
    click.echo(
        "    5. Confirm with: agent-notifier test-notifier --channel desktop"
    )
    click.echo("")


@setup_group.command("codex", help="Wire the Codex notify hook to agent-notifier.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Codex config file to update. Defaults to ~/.codex/config.toml.",
)
@click.option(
    "--hook-command",
    type=str,
    default=None,
    help="Exact command/path that Codex should run for notifications.",
)
@click.option(
    "--backup/--no-backup",
    default=True,
    show_default=True,
    help="Create a timestamped backup before changing an existing config file.",
)
def setup_codex_command(
    config_path: Path | None,
    hook_command: str | None,
    backup: bool,
) -> None:
    resolved_hook_command = resolve_codex_hook_command(hook_command)
    try:
        result = ensure_codex_notify_hook(
            config_path=config_path,
            hook_command=resolved_hook_command,
            backup=backup,
        )
    except SetupError as exc:
        raise click.ClickException(str(exc)) from exc

    if result.backup_path is not None:
        click.echo(f"Backed up {result.config_path} -> {result.backup_path}")

    if result.changed:
        click.echo(f"Updated {result.config_path}")
    else:
        click.echo(f"No changes needed in {result.config_path}")

    click.echo(f"Hook command: {result.hook_command}")
    click.echo("Restart Codex to pick up the new notify hook.")
    _emit_macos_notification_reminder()


@setup_group.command("gemini", help="Wire Gemini AfterAgent hooks to agent-notifier.")
@click.option(
    "--project",
    "project_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project directory to trust in Gemini. Defaults to the current directory.",
)
@click.option(
    "--settings",
    "settings_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Gemini settings file to update. Defaults to ~/.gemini/settings.json.",
)
@click.option(
    "--trusted-folders",
    "trusted_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Gemini trustedFolders file to update. Defaults to ~/.gemini/trustedFolders.json.",
)
@click.option(
    "--hook-command",
    type=str,
    default=None,
    help="Exact command/path that Gemini should run for notifications.",
)
@click.option(
    "--backup/--no-backup",
    default=True,
    show_default=True,
    help="Create timestamped backups before changing existing config files.",
)
def setup_gemini_command(
    project_dir: Path | None,
    settings_path: Path | None,
    trusted_path: Path | None,
    hook_command: str | None,
    backup: bool,
) -> None:
    resolved_project = (project_dir or Path.cwd()).expanduser().resolve()
    resolved_hook_command = resolve_gemini_hook_command(hook_command)
    try:
        result = ensure_gemini_hook(
            project_dir=resolved_project,
            hook_command=resolved_hook_command,
            settings_path=settings_path,
            trusted_path=trusted_path,
            backup=backup,
        )
    except SetupError as exc:
        raise click.ClickException(str(exc)) from exc

    _emit_setup_file_result(
        "Gemini settings",
        path=result.settings.path,
        changed=result.settings.changed,
        backup_path=result.settings.backup_path,
    )
    _emit_setup_file_result(
        "Gemini trust",
        path=result.trusted_folders.path,
        changed=result.trusted_folders.changed,
        backup_path=result.trusted_folders.backup_path,
    )
    click.echo(f"Hook command: {result.hook_command}")
    click.echo(f"Trusted project: {_render_setup_path(result.project_dir)}")
    click.echo("Restart Gemini to pick up the new hook config.")
    _emit_macos_notification_reminder()


@setup_group.command("claude", help="Wire Claude Code hooks to agent-notifier.")
@click.option(
    "--project",
    "project_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project directory for project-scoped Claude settings. Defaults to the current directory.",
)
@click.option(
    "--scope",
    type=click.Choice(["project", "user"], case_sensitive=False),
    default="project",
    show_default=True,
    help="Write project-local settings or user-level Claude settings.",
)
@click.option(
    "--config",
    "settings_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Exact Claude settings file to update. Overrides --scope.",
)
@click.option(
    "--hook-command",
    type=str,
    default=None,
    help="Exact command/path that Claude Code should run for notifications.",
)
@click.option(
    "--backup/--no-backup",
    default=True,
    show_default=True,
    help="Create a timestamped backup before changing an existing config file.",
)
def setup_claude_command(
    project_dir: Path | None,
    scope: str,
    settings_path: Path | None,
    hook_command: str | None,
    backup: bool,
) -> None:
    resolved_project = (project_dir or Path.cwd()).expanduser().resolve()
    resolved_hook_command = resolve_claude_hook_command(hook_command)
    try:
        result = ensure_claude_hooks(
            project_dir=resolved_project,
            hook_command=resolved_hook_command,
            settings_path=settings_path,
            use_user_settings=scope.lower() == "user",
            backup=backup,
        )
    except SetupError as exc:
        raise click.ClickException(str(exc)) from exc

    _emit_setup_file_result(
        "Claude settings",
        path=result.settings.path,
        changed=result.settings.changed,
        backup_path=result.settings.backup_path,
    )
    click.echo(f"Hook command: {result.hook_command}")
    click.echo(f"Stop command: {result.stop_command}")
    click.echo(f"SubagentStop command: {result.subagent_stop_command}")
    click.echo("Restart Claude Code to pick up the new hook config.")
    _emit_macos_notification_reminder()


@setup_group.command("all", help="Wire Codex, Gemini, and Claude Code to agent-notifier.")
@click.option(
    "--project",
    "project_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project directory used for Gemini trust and project-scoped Claude settings.",
)
@click.option(
    "--claude-scope",
    type=click.Choice(["project", "user"], case_sensitive=False),
    default="project",
    show_default=True,
    help="Write project-local or user-level Claude settings.",
)
@click.option(
    "--backup/--no-backup",
    default=True,
    show_default=True,
    help="Create timestamped backups before changing existing config files.",
)
def setup_all_command(project_dir: Path | None, claude_scope: str, backup: bool) -> None:
    resolved_project = (project_dir or Path.cwd()).expanduser().resolve()
    try:
        codex_result = ensure_codex_notify_hook(
            hook_command=resolve_codex_hook_command(),
            backup=backup,
        )
        gemini_result = ensure_gemini_hook(
            project_dir=resolved_project,
            hook_command=resolve_gemini_hook_command(),
            backup=backup,
        )
        claude_result = ensure_claude_hooks(
            project_dir=resolved_project,
            hook_command=resolve_claude_hook_command(),
            use_user_settings=claude_scope.lower() == "user",
            backup=backup,
        )
    except SetupError as exc:
        raise click.ClickException(str(exc)) from exc

    if codex_result.backup_path is not None:
        click.echo(f"Codex backup: {_render_setup_path(codex_result.backup_path)}")
    codex_status = "updated" if codex_result.changed else "already configured"
    click.echo(f"Codex: {codex_status} ({_render_setup_path(codex_result.config_path)})")
    click.echo(f"Codex hook command: {codex_result.hook_command}")
    _emit_setup_file_result(
        "Gemini settings",
        path=gemini_result.settings.path,
        changed=gemini_result.settings.changed,
        backup_path=gemini_result.settings.backup_path,
    )
    _emit_setup_file_result(
        "Gemini trust",
        path=gemini_result.trusted_folders.path,
        changed=gemini_result.trusted_folders.changed,
        backup_path=gemini_result.trusted_folders.backup_path,
    )
    click.echo(f"Gemini hook command: {gemini_result.hook_command}")
    click.echo(f"Trusted project: {_render_setup_path(gemini_result.project_dir)}")
    _emit_setup_file_result(
        "Claude settings",
        path=claude_result.settings.path,
        changed=claude_result.settings.changed,
        backup_path=claude_result.settings.backup_path,
    )
    click.echo(f"Claude hook command: {claude_result.hook_command}")
    click.echo("Restart Codex, Gemini, and Claude Code to pick up the new hook config.")
    _emit_macos_notification_reminder()


@app.command("doctor", help="Show install and integration diagnostics for this machine.")
@click.option(
    "--project",
    "project_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project directory used for Gemini and Claude checks. Defaults to the current directory.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def doctor_command(project_dir: Path | None, json_output: bool) -> None:
    report = collect_doctor_report(project_dir=project_dir)
    if json_output:
        click.echo(json.dumps(report, indent=2))
        return
    click.echo(render_doctor_report(report))


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
