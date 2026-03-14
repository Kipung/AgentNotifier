"""Install and integration diagnostics for agent-notifier."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agentnotifier import __version__

Check = dict[str, str | None]


def collect_doctor_report(*, project_dir: Path | None = None) -> dict[str, Any]:
    resolved_project = (project_dir or Path.cwd()).expanduser().resolve()
    return {
        "summary": {
            "version": __version__,
            "platform": platform.system(),
            "python": sys.executable,
            "current_executable": sys.argv[0],
            "project_dir": str(resolved_project),
        },
        "sections": {
            "install": _collect_install_checks(),
            "backend": _collect_backend_checks(),
            "codex": _collect_codex_checks(),
            "gemini": _collect_gemini_checks(project_dir=resolved_project),
            "claude": _collect_claude_checks(project_dir=resolved_project),
        },
    }


def render_doctor_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    sections = report["sections"]

    lines = [
        f"agent-notifier {summary['version']}",
        f"Platform: {summary['platform']}",
        f"Python: {summary['python']}",
        f"Current executable: {summary['current_executable']}",
        f"Project: {summary['project_dir']}",
    ]

    for section_name, checks in sections.items():
        lines.append("")
        lines.append(f"{section_name.title()}:")
        for check in checks:
            prefix = {
                "ok": "OK",
                "warn": "WARN",
                "info": "INFO",
            }.get(str(check["status"]), "INFO")
            lines.append(f"- [{prefix}] {check['name']}: {check['detail']}")
            if check.get("fix"):
                lines.append(f"  fix: {check['fix']}")

    return "\n".join(lines)


def _check(
    status: str,
    name: str,
    detail: str,
    *,
    fix: str | None = None,
) -> Check:
    return {
        "status": status,
        "name": name,
        "detail": detail,
        "fix": fix,
    }


def _collect_install_checks() -> list[Check]:
    checks: list[Check] = []
    for script_name in (
        "agent-notifier",
        "agent-notifier-codex-hook",
        "agent-notifier-gemini-hook",
        "agent-notifier-claude-hook",
    ):
        resolved = shutil.which(script_name)
        if resolved:
            checks.append(_check("ok", script_name, resolved))
            continue
        checks.append(
            _check(
                "warn",
                script_name,
                "Not found on PATH.",
                fix=(
                    "Run `pipx ensurepath`, open a new shell, or add your Python scripts "
                    "directory to PATH."
                ),
            )
        )
    return checks


def _collect_backend_checks() -> list[Check]:
    system = platform.system()
    if system == "Darwin":
        return _collect_macos_backend_checks()
    if system == "Linux":
        return _collect_linux_backend_checks()
    if system == "Windows":
        return _collect_windows_backend_checks()
    return [
        _check(
            "warn",
            "desktop backend",
            f"No desktop notifier is implemented for {system}.",
            fix="Use `--channel console` on unsupported platforms.",
        )
    ]


def _collect_macos_backend_checks() -> list[Check]:
    terminal_notifier = shutil.which("terminal-notifier")
    osascript = shutil.which("osascript")
    checks: list[Check] = []

    if terminal_notifier or osascript:
        detail = (
            f"terminal-notifier={terminal_notifier or 'missing'}, "
            f"osascript={osascript or 'missing'}"
        )
        checks.append(_check("ok", "desktop backend", detail))
    else:
        checks.append(
            _check(
                "warn",
                "desktop backend",
                "Neither `terminal-notifier` nor `osascript` is available.",
                fix="Install `terminal-notifier` or use a standard macOS shell with `osascript`.",
            )
        )

    # macOS silently drops notifications when the calling app lacks permission.
    # osascript always exits 0, so we cannot detect this programmatically.
    # Marking as WARN ensures users see this step — it is mandatory, not optional.
    checks.append(
        _check(
            "warn",
            "notification permission",
            (
                "macOS requires manual notification permission for the app that runs "
                "the hook (your terminal: Terminal, iTerm2, Warp, etc.).  "
                "osascript exits 0 even when macOS silently drops the notification, "
                "so there is no way to detect this failure programmatically."
            ),
            fix=(
                "Open System Settings -> Notifications.  "
                "Find your terminal app (Terminal / iTerm2 / Warp / WezTerm) "
                "and set 'Allow Notifications' to ON with alert style 'Banners' or 'Alerts'.  "
                "If terminal-notifier is installed, also allow it.  "
                "Then run: agent-notifier test-notifier --channel desktop  "
                "and confirm the popup appears."
            ),
        )
    )
    return checks


def _collect_linux_backend_checks() -> list[Check]:
    checks: list[Check] = []
    notify_send = shutil.which("notify-send")
    if notify_send:
        checks.append(_check("ok", "notify-send", notify_send))
    else:
        checks.append(
            _check(
                "warn",
                "notify-send",
                "`notify-send` is not available.",
                fix=(
                    "Install `libnotify-bin` on Debian/Ubuntu or the equivalent "
                    "package for your distro."
                ),
            )
        )

    if any(os.getenv(key) for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")):
        checks.append(
            _check(
                "ok",
                "graphical session",
                "Graphical session environment variables are present.",
            )
        )
    else:
        checks.append(
            _check(
                "warn",
                "graphical session",
                "No DISPLAY, WAYLAND_DISPLAY, or DBUS_SESSION_BUS_ADDRESS was detected.",
                fix=(
                    "Desktop popups usually require an active graphical session. "
                    "Use `--channel console` in headless shells."
                ),
            )
        )
    return checks


def _collect_windows_backend_checks() -> list[Check]:
    checks: list[Check] = []
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if powershell:
        checks.append(_check("ok", "powershell", powershell))
    else:
        checks.append(
            _check(
                "warn",
                "powershell",
                "PowerShell is not available on PATH.",
                fix="Use a standard Windows PowerShell install or rely on the Python fallback.",
            )
        )

    burnttoast_available = _burnttoast_available(powershell=powershell)
    win10toast_available = importlib.util.find_spec("win10toast") is not None
    if burnttoast_available or win10toast_available:
        detail = (
            f"BurntToast={'yes' if burnttoast_available else 'no'}, "
            f"win10toast={'yes' if win10toast_available else 'no'}"
        )
        checks.append(_check("ok", "desktop backend", detail))
    else:
        checks.append(
            _check(
                "warn",
                "desktop backend",
                "No Windows desktop notification backend is available.",
                fix=(
                    'Install a backend with `pip install "agent-notifier[windows]"` '
                    "or install the BurntToast PowerShell module."
                ),
            )
        )
    return checks


def _burnttoast_available(*, powershell: str | None) -> bool:
    if not powershell:
        return False

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "if (Get-Module -ListAvailable -Name BurntToast) { exit 0 } "
        "exit 1"
    )
    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _collect_codex_checks() -> list[Check]:
    config_path = Path.home() / ".codex" / "config.toml"
    text = _read_text(config_path)
    if text is None:
        return [
            _check(
                "warn",
                "Codex config",
                f"{config_path} does not exist.",
                fix="Run `agent-notifier setup codex`, then restart Codex.",
            )
        ]

    notify_match = re.search(r"(?ms)^\s*notify\s*=\s*(\[[^\]]*\]|.+)$", text)
    if notify_match and any(
        token in notify_match.group(1)
        for token in (
            "agent-notifier-codex-hook",
            "codex_notifier_bridge.sh",
            "codex_notify_bridge.sh",
        )
    ):
        return [
            _check(
                "ok",
                "Codex notify hook",
                f"{config_path}: {notify_match.group(1).strip()}",
            )
        ]

    if notify_match:
        return [
            _check(
                "warn",
                "Codex notify hook",
                f"{config_path} has a `notify` key, but it does not point at agent-notifier.",
                fix="Run `agent-notifier setup codex`, then restart Codex.",
            )
        ]

    return [
        _check(
            "warn",
            "Codex notify hook",
            f"{config_path} is missing a `notify` entry.",
            fix="Run `agent-notifier setup codex`, then restart Codex.",
        )
    ]


def _collect_gemini_checks(*, project_dir: Path) -> list[Check]:
    checks: list[Check] = []
    settings_path = Path.home() / ".gemini" / "settings.json"
    settings = _read_json(settings_path)
    if settings is None:
        checks.append(
            _check(
                "warn",
                "Gemini settings",
                f"{settings_path} is missing or invalid JSON.",
                fix="Run `agent-notifier setup gemini --project <path>`, then restart Gemini.",
            )
        )
    else:
        hooks_enabled = bool(
            isinstance(settings.get("hooksConfig"), dict)
            and settings["hooksConfig"].get("enabled") is True
        )
        checks.append(
            _check(
                "ok" if hooks_enabled else "warn",
                "Gemini hooks enabled",
                f"{settings_path}: {'enabled' if hooks_enabled else 'disabled'}",
                fix=(
                    None
                    if hooks_enabled
                    else "Run `agent-notifier setup gemini --project <path>`, then restart Gemini."
                ),
            )
        )

        after_agent = _gemini_after_agent_configured(settings)
        checks.append(
            _check(
                "ok" if after_agent else "warn",
                "Gemini AfterAgent hook",
                f"{settings_path}: {'configured' if after_agent else 'missing'}",
                fix=(
                    None
                    if after_agent
                    else "Run `agent-notifier setup gemini --project <path>`, then restart Gemini."
                ),
            )
        )

    trusted_path = Path.home() / ".gemini" / "trustedFolders.json"
    trusted_folders = _read_json(trusted_path)
    if (
        isinstance(trusted_folders, dict)
        and trusted_folders.get(str(project_dir)) == "TRUST_FOLDER"
    ):
        checks.append(
            _check(
                "ok",
                "Gemini trust",
                f"{project_dir} is trusted in {trusted_path}.",
            )
        )
    else:
        checks.append(
            _check(
                "warn",
                "Gemini trust",
                f"{project_dir} is not trusted in {trusted_path}.",
                fix="Run `agent-notifier setup gemini --project <path>`, then restart Gemini.",
            )
        )
    return checks


def _collect_claude_checks(*, project_dir: Path) -> list[Check]:
    candidate_paths = [
        project_dir / ".claude" / "settings.local.json",
        Path.home() / ".claude" / "settings.json",
    ]

    found_match: Path | None = None
    existing_paths: list[Path] = []
    for candidate in candidate_paths:
        text = _read_text(candidate)
        if text is None:
            continue
        existing_paths.append(candidate)
        if "agent-notifier-claude-hook" in text:
            found_match = candidate
            break

    if found_match is not None:
        return [
            _check(
                "ok",
                "Claude hook",
                f"Found `agent-notifier-claude-hook` in {found_match}.",
            )
        ]

    if existing_paths:
        rendered_paths = ", ".join(str(path) for path in existing_paths)
        return [
            _check(
                "warn",
                "Claude hook",
                (
                    "Claude settings exist, but none reference "
                    f"`agent-notifier-claude-hook`: {rendered_paths}"
                ),
                fix="Run `agent-notifier setup claude --project <path>`, then restart Claude Code.",
            )
        ]

    return [
        _check(
            "info",
            "Claude hook",
            "No Claude settings file was found in the project or user home.",
            fix="If you use Claude Code, run `agent-notifier setup claude --project <path>`.",
        )
    ]


def _gemini_after_agent_configured(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    after_agent = hooks.get("AfterAgent")
    if not isinstance(after_agent, list):
        return False

    for entry in after_agent:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if not isinstance(nested, list):
            continue
        for hook in nested:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command")
            if isinstance(command, str) and "agent-notifier-gemini-hook" in command:
                return True
    return False


def _read_json(path: Path) -> dict[str, Any] | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _read_text(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
