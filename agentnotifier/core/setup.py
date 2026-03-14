"""Helpers for wiring agent-notifier into external CLI configs."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

_NOTIFY_ARRAY_PATTERN = re.compile(r"(?ms)^([ \t]*)notify\s*=\s*(\[[^\]]*\])")
_NOTIFY_SCALAR_PATTERN = re.compile(r"(?m)^([ \t]*)notify\s*=\s*(.+)$")


class SetupError(RuntimeError):
    """Raised when agent-notifier cannot safely edit an integration config."""


@dataclass(slots=True)
class CodexSetupResult:
    """Result of ensuring the Codex notify hook is configured."""

    config_path: Path
    hook_command: str
    changed: bool
    backup_path: Path | None


@dataclass(slots=True)
class FileUpdateResult:
    """Generic result for an updated config file."""

    path: Path
    changed: bool
    backup_path: Path | None


@dataclass(slots=True)
class GeminiSetupResult:
    """Result of ensuring Gemini hook and trust configuration."""

    settings: FileUpdateResult
    trusted_folders: FileUpdateResult
    hook_command: str
    project_dir: Path


@dataclass(slots=True)
class ClaudeSetupResult:
    """Result of ensuring Claude Code hook configuration."""

    settings: FileUpdateResult
    hook_command: str
    stop_command: str
    subagent_stop_command: str


def resolve_codex_hook_command(explicit: str | None = None) -> str:
    """Return the command Codex should invoke for notifications.

    When no explicit command is given, resolve the absolute path of the
    installed ``agent-notifier-codex-hook`` entry-point via ``shutil.which``.
    Using an absolute path is important because Codex spawns hook subprocesses
    with a restricted PATH that often omits the pipx/pip scripts directory,
    which would cause a bare name to silently fail on machines other than the
    developer's own.  Falls back to the bare name only when the binary cannot
    be found on the current PATH (e.g. during a source-checkout dev session).
    """
    if explicit:
        return explicit
    return shutil.which("agent-notifier-codex-hook") or "agent-notifier-codex-hook"


def resolve_gemini_hook_command(explicit: str | None = None) -> str:
    """Return the command Gemini should invoke for notifications.

    See ``resolve_codex_hook_command`` for the rationale behind absolute-path
    resolution.
    """
    if explicit:
        return explicit
    return shutil.which("agent-notifier-gemini-hook") or "agent-notifier-gemini-hook"


def resolve_claude_hook_command(explicit: str | None = None) -> str:
    """Return the command Claude Code should invoke for notifications.

    See ``resolve_codex_hook_command`` for the rationale behind absolute-path
    resolution.
    """
    if explicit:
        return explicit
    return shutil.which("agent-notifier-claude-hook") or "agent-notifier-claude-hook"


def ensure_codex_notify_hook(
    *,
    config_path: Path | None = None,
    hook_command: str,
    backup: bool = True,
) -> CodexSetupResult:
    """Create or update Codex config so notify calls agent-notifier."""
    target_path = (config_path or (Path.home() / ".codex" / "config.toml")).expanduser()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    original_text = ""
    if target_path.exists():
        original_text = target_path.read_text(encoding="utf-8")

    updated_text, changed = _upsert_notify_hook(original_text, hook_command)
    backup_path: Path | None = None

    if changed:
        if backup and target_path.exists():
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup_path = target_path.with_name(f"{target_path.name}.bak.{stamp}")
            backup_path.write_text(original_text, encoding="utf-8")
        target_path.write_text(updated_text, encoding="utf-8")

    return CodexSetupResult(
        config_path=target_path,
        hook_command=hook_command,
        changed=changed,
        backup_path=backup_path,
    )


def ensure_gemini_hook(
    *,
    project_dir: Path,
    hook_command: str,
    settings_path: Path | None = None,
    trusted_path: Path | None = None,
    backup: bool = True,
) -> GeminiSetupResult:
    """Create or update Gemini hook config and trust for a project."""
    resolved_project = project_dir.expanduser().resolve()
    target_settings_path = (settings_path or (Path.home() / ".gemini" / "settings.json")).expanduser()
    target_trusted_path = (
        trusted_path or (Path.home() / ".gemini" / "trustedFolders.json")
    ).expanduser()

    settings = _load_json_object(
        target_settings_path,
        missing_default={},
        error_message=(
            "Gemini settings must be a JSON object. "
            "Fix the file manually before running setup again."
        ),
    )
    hooks_config = _ensure_json_object(
        settings,
        "hooksConfig",
        owner=str(target_settings_path),
    )
    hooks_config["enabled"] = True

    hooks = _ensure_json_object(settings, "hooks", owner=str(target_settings_path))
    after_agent = hooks.setdefault("AfterAgent", [])
    if not isinstance(after_agent, list):
        raise SetupError(
            f"{target_settings_path}: `hooks.AfterAgent` must be a JSON array. "
            "Fix the file manually."
        )
    _upsert_gemini_after_agent_hook(after_agent, hook_command)

    settings_result = _write_json_object(
        path=target_settings_path,
        data=settings,
        backup=backup,
    )

    trusted_folders = _load_json_object(
        target_trusted_path,
        missing_default={},
        error_message=(
            "Gemini trusted folders must be a JSON object. "
            "Fix the file manually before running setup again."
        ),
    )
    trusted_folders[str(resolved_project)] = "TRUST_FOLDER"
    trusted_result = _write_json_object(
        path=target_trusted_path,
        data=trusted_folders,
        backup=backup,
    )

    return GeminiSetupResult(
        settings=settings_result,
        trusted_folders=trusted_result,
        hook_command=hook_command,
        project_dir=resolved_project,
    )


def ensure_claude_hooks(
    *,
    project_dir: Path,
    hook_command: str,
    settings_path: Path | None = None,
    use_user_settings: bool = False,
    backup: bool = True,
) -> ClaudeSetupResult:
    """Create or update Claude Code hook config."""
    resolved_project = project_dir.expanduser().resolve()
    target_settings_path = (
        settings_path
        or (
            Path.home() / ".claude" / "settings.json"
            if use_user_settings
            else resolved_project / ".claude" / "settings.local.json"
        )
    ).expanduser()

    settings = _load_json_object(
        target_settings_path,
        missing_default={},
        error_message=(
            "Claude settings must be a JSON object. "
            "Fix the file manually before running setup again."
        ),
    )
    hooks = _ensure_json_object(settings, "hooks", owner=str(target_settings_path))

    stop_entries = hooks.setdefault("Stop", [])
    if not isinstance(stop_entries, list):
        raise SetupError(
            f"{target_settings_path}: `hooks.Stop` must be a JSON array. "
            "Fix the file manually."
        )
    stop_command = _compose_shell_command(hook_command, "--event", "Stop")
    _upsert_claude_event_hook(stop_entries, event="Stop", hook_command=stop_command)

    subagent_stop_entries = hooks.setdefault("SubagentStop", [])
    if not isinstance(subagent_stop_entries, list):
        raise SetupError(
            f"{target_settings_path}: `hooks.SubagentStop` must be a JSON array. "
            "Fix the file manually."
        )
    subagent_stop_command = _compose_shell_command(
        hook_command,
        "--event",
        "SubagentStop",
    )
    _upsert_claude_event_hook(
        subagent_stop_entries,
        event="SubagentStop",
        hook_command=subagent_stop_command,
    )

    settings_result = _write_json_object(
        path=target_settings_path,
        data=settings,
        backup=backup,
    )

    return ClaudeSetupResult(
        settings=settings_result,
        hook_command=hook_command,
        stop_command=stop_command,
        subagent_stop_command=subagent_stop_command,
    )


def _upsert_notify_hook(text: str, hook_command: str) -> tuple[str, bool]:
    array_match = _NOTIFY_ARRAY_PATTERN.search(text)
    if array_match is not None:
        return _replace_notify_match(text, array_match, hook_command)

    scalar_match = _NOTIFY_SCALAR_PATTERN.search(text)
    if scalar_match is not None:
        return _replace_notify_match(text, scalar_match, hook_command)

    replacement = _render_notify_assignment(hook_values=[hook_command], indent="")
    # Insert before the first [section] header so the key stays at the top level.
    # Appending to the end would place it inside the last TOML table.
    section_match = re.search(r"(?m)^\[", text)
    if section_match:
        insert_pos = section_match.start()
        prefix = text[:insert_pos].rstrip("\n")
        suffix = text[insert_pos:]
        updated = f"{prefix}\n\n{replacement}\n\n{suffix}" if prefix else f"{replacement}\n\n{suffix}"
        return updated, True

    updated = text
    if updated.strip():
        if not updated.endswith("\n"):
            updated += "\n"
        updated += "\n"
    updated += f"{replacement}\n"
    return updated, True


def _replace_notify_match(text: str, match: re.Match[str], hook_command: str) -> tuple[str, bool]:
    indent = match.group(1)
    raw_notify = match.group(2)
    current_values = _parse_notify_value(raw_notify)
    if current_values is None:
        raise SetupError(
            "Existing Codex `notify` entry could not be parsed safely. "
            "Edit the file manually."
        )

    updated_values = _merge_codex_hook_values(current_values=current_values, hook_command=hook_command)
    if updated_values == current_values:
        return text, False

    replacement = _render_notify_assignment(
        hook_values=updated_values,
        indent=indent,
    )
    updated = f"{text[: match.start()]}{replacement}{text[match.end() :]}"
    return updated, True


def _parse_notify_value(raw_notify: str) -> list[str] | None:
    try:
        parsed = tomllib.loads(f"notify = {raw_notify}")
    except tomllib.TOMLDecodeError:
        return None

    notify_value = parsed.get("notify")
    if isinstance(notify_value, str):
        return [notify_value]
    if isinstance(notify_value, list) and all(isinstance(item, str) for item in notify_value):
        return list(notify_value)
    return None


def _render_notify_assignment(*, hook_values: list[str], indent: str) -> str:
    quoted_values = ", ".join(_quote_toml_string(value) for value in hook_values)
    return f"{indent}notify = [{quoted_values}]"


def _quote_toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _merge_codex_hook_values(*, current_values: list[str], hook_command: str) -> list[str]:
    updated_values: list[str] = []
    replaced = False

    for value in current_values:
        if _is_agent_notifier_codex_command(value):
            if not replaced:
                updated_values.append(hook_command)
                replaced = True
            continue
        updated_values.append(value)

    if not replaced:
        updated_values.append(hook_command)
    return updated_values


def _is_agent_notifier_codex_command(value: str) -> bool:
    normalized = value.strip()
    return (
        "agent-notifier-codex-hook" in normalized
        or normalized.endswith("codex_notifier_bridge.sh")
        or normalized.endswith("codex_notify_bridge.sh")
    )


def _load_json_object(
    path: Path,
    *,
    missing_default: dict[str, Any],
    error_message: str,
) -> dict[str, Any]:
    if not path.exists():
        return dict(missing_default)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise SetupError(f"Unable to read {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{error_message} ({path}: {exc})") from exc

    if not isinstance(data, dict):
        raise SetupError(f"{error_message} ({path})")
    return data


def _ensure_json_object(data: dict[str, Any], key: str, *, owner: str) -> dict[str, Any]:
    value = data.setdefault(key, {})
    if not isinstance(value, dict):
        raise SetupError(f"{owner}: `{key}` must be a JSON object. Fix the file manually.")
    return value


def _write_json_object(
    *,
    path: Path,
    data: dict[str, Any],
    backup: bool,
) -> FileUpdateResult:
    path.parent.mkdir(parents=True, exist_ok=True)

    original_text = ""
    if path.exists():
        original_text = path.read_text(encoding="utf-8")

    updated_text = json.dumps(data, indent=2) + "\n"
    backup_path: Path | None = None
    changed = updated_text != original_text

    if changed:
        if backup and path.exists():
            backup_path = _write_backup(path, original_text)
        path.write_text(updated_text, encoding="utf-8")

    return FileUpdateResult(
        path=path,
        changed=changed,
        backup_path=backup_path,
    )


def _write_backup(path: Path, original_text: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak.{stamp}")
    backup_path.write_text(original_text, encoding="utf-8")
    return backup_path


def _upsert_gemini_after_agent_hook(after_agent: list[Any], hook_command: str) -> None:
    desired_hook = {
        "type": "command",
        "name": "agent-notifier-gemini-task",
        "description": "Send notification when Gemini finishes an agent cycle.",
        "command": hook_command,
        "timeout": 10000,
    }

    for entry in after_agent:
        if not isinstance(entry, dict):
            continue
        nested_hooks = entry.get("hooks")
        if not isinstance(nested_hooks, list):
            continue
        for index, hook in enumerate(nested_hooks):
            if not isinstance(hook, dict):
                continue
            if _is_agent_notifier_gemini_hook(hook):
                nested_hooks[index] = desired_hook.copy()
                return

    after_agent.append(
        {
            "matcher": "*",
            "hooks": [desired_hook],
        }
    )


def _upsert_claude_event_hook(
    event_entries: list[Any],
    *,
    event: str,
    hook_command: str,
) -> None:
    desired_hook = {
        "type": "command",
        "command": hook_command,
    }

    for entry in event_entries:
        if not isinstance(entry, dict):
            continue
        nested_hooks = entry.get("hooks")
        if not isinstance(nested_hooks, list):
            continue
        for index, hook in enumerate(nested_hooks):
            if not isinstance(hook, dict):
                continue
            if _is_agent_notifier_claude_hook(hook, event=event):
                nested_hooks[index] = desired_hook.copy()
                return

    event_entries.append(
        {
            "matcher": "*",
            "hooks": [desired_hook],
        }
    )


def _is_agent_notifier_gemini_hook(hook: dict[str, Any]) -> bool:
    name = hook.get("name")
    command = hook.get("command")
    return name == "agent-notifier-gemini-task" or (
        isinstance(command, str) and "agent-notifier-gemini-hook" in command
    )


def _is_agent_notifier_claude_hook(hook: dict[str, Any], *, event: str) -> bool:
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    return "agent-notifier-claude-hook" in command and f"--event {event}" in command


def _compose_shell_command(command: str, *args: str) -> str:
    return " ".join(shlex.quote(part) for part in (command, *args))
