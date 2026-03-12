from __future__ import annotations

import json
import time
from pathlib import Path

from click.testing import CliRunner

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from agentnotifier import cli
from agentnotifier.cli import app
from agentnotifier.notifier.base import NotificationError, NotificationLevel, Notifier


def test_emit_command_auto_detects_tool_name() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "emit",
            "--command",
            "codex run fix-tests",
            "--duration-seconds",
            "11",
            "--exit-code",
            "0",
            "--channel",
            "console",
        ],
    )
    assert result.exit_code == 0
    assert "[codex] Done" in result.output
    assert "Exit code: 0" in result.output


def test_gemini_hook_command_after_agent_notifies_console() -> None:
    runner = CliRunner()
    payload = {
        "hook_event_name": "AfterAgent",
        "prompt": "Please summarize what changed in the repository today.",
        "prompt_response": "Completed summary with key changed files and outcomes.",
        "session_id": "session-123",
    }
    result = runner.invoke(
        app,
        [
            "gemini-hook",
            "--channel",
            "console",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0
    assert "[gemini] Done" in result.output
    assert "Event: AfterAgent" in result.output
    assert "Prompt:" in result.output
    assert "Response:" in result.output


def test_gemini_hook_command_accepts_camel_case_payload_keys() -> None:
    runner = CliRunner()
    payload = {
        "eventName": "AfterAgent",
        "userPrompt": "Please summarize what changed in the repository today.",
        "promptResponse": "Completed summary with key changed files and outcomes.",
        "sessionId": "session-123",
    }
    result = runner.invoke(
        app,
        [
            "gemini-hook",
            "--channel",
            "console",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0
    assert "[gemini] Done" in result.output
    assert "Event: AfterAgent" in result.output
    assert "Prompt:" in result.output
    assert "Response:" in result.output


def test_gemini_hook_command_ignores_non_matching_event() -> None:
    runner = CliRunner()
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "session-123",
    }
    result = runner.invoke(
        app,
        [
            "gemini-hook",
            "--channel",
            "console",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0
    assert result.output == ""


def test_gemini_hook_command_invalid_json_is_non_fatal() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "gemini-hook",
            "--channel",
            "console",
            "--verbose",
        ],
        input="not-json",
    )
    assert result.exit_code == 0
    assert "not valid JSON" in result.output


def test_gemini_hook_command_skips_when_focused(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    payload = {
        "hook_event_name": "AfterAgent",
        "prompt": "run tests",
        "prompt_response": "all green",
        "session_id": "session-123",
    }
    monkeypatch.setattr(
        "agentnotifier.cli._is_user_focused_on_terminal",
        lambda *, verbose: True,
    )
    result = runner.invoke(
        app,
        [
            "gemini-hook",
            "--channel",
            "console",
            "--quiet-when-focused",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0
    assert result.output == ""


def test_gemini_hook_command_can_play_chime(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    payload = {
        "hook_event_name": "AfterAgent",
        "prompt": "run tests",
        "prompt_response": "all green",
        "session_id": "session-123",
    }
    calls: list[str] = []

    def fake_play(chime: str, *, verbose: bool) -> None:
        del verbose
        calls.append(chime)

    monkeypatch.setattr("agentnotifier.cli._play_chime", fake_play)
    result = runner.invoke(
        app,
        [
            "gemini-hook",
            "--channel",
            "console",
            "--chime",
            "ping",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0
    assert calls == ["ping"]


def test_gemini_hook_command_warns_when_desktop_fails_in_both_mode(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    payload = {
        "hook_event_name": "AfterAgent",
        "prompt": "run tests",
        "prompt_response": "all green",
        "session_id": "session-123",
    }

    class FailingDesktopNotifier(Notifier):
        def notifier(  # type: ignore[override]
            self,
            title: str,
            message: str,
            level: NotificationLevel = NotificationLevel.INFO,
            metadata: dict[str, object] | None = None,
        ) -> None:
            del title, message, level, metadata
            raise NotificationError("desktop backend exploded")

    monkeypatch.setattr(
        "agentnotifier.cli._build_desktop_notifier",
        lambda: FailingDesktopNotifier(),
    )
    result = runner.invoke(
        app,
        [
            "gemini-hook",
            "--channel",
            "both",
            "--verbose",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0
    assert "[gemini] Done" in result.output
    assert "Desktop notification failed while console delivery succeeded" in result.output


def test_claude_hook_command_stop_notifies_console() -> None:
    runner = CliRunner()
    payload = {
        "hook_event_name": "Stop",
        "reason": "Task appears complete",
        "user_prompt": "Run the test suite",
        "session_id": "claude-session-1",
    }
    result = runner.invoke(
        app,
        [
            "claude-hook",
            "--channel",
            "console",
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0
    assert "[claude-code] Done" in result.output
    assert "Event: Stop" in result.output
    assert "Reason: Task appears complete" in result.output


def test_codex_hook_command_payload_argument_notifies_console() -> None:
    runner = CliRunner()
    payload = {
        "type": "agent-turn-complete",
        "input-messages": "Please summarize the latest diff.",
        "last-assistant-message": "Summary complete with key files and risk notes.",
        "turn-id": "turn-42",
    }
    result = runner.invoke(
        app,
        [
            "codex-hook",
            "--channel",
            "console",
            json.dumps(payload),
        ],
    )
    assert result.exit_code == 0
    assert "[codex] Done" in result.output
    assert "Event: agent-turn-complete" in result.output
    assert "Turn: turn-42" in result.output


def test_codex_hook_command_ignores_non_matching_event() -> None:
    runner = CliRunner()
    payload = {
        "type": "session-ended",
        "turn-id": "turn-42",
    }
    result = runner.invoke(
        app,
        [
            "codex-hook",
            "--channel",
            "console",
            json.dumps(payload),
        ],
    )
    assert result.exit_code == 0
    assert result.output == ""


def test_codex_hook_command_legacy_payload_without_event_notifies() -> None:
    runner = CliRunner()
    payload = {
        "thread-id": "thread-1",
        "turn-id": "turn-legacy-1",
        "input-messages": "Do the thing",
        "last-assistant-message": "Done.",
    }
    result = runner.invoke(
        app,
        [
            "codex-hook",
            "--channel",
            "console",
            json.dumps(payload),
        ],
    )
    assert result.exit_code == 0
    assert "[codex] Done" in result.output
    assert "Event: after_agent" in result.output
    assert "Turn: turn-legacy-1" in result.output


def test_codex_hook_command_key_value_parts_notifies() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "codex-hook",
            "--channel",
            "console",
            "thread-id",
            "thread-2",
            "turn-id",
            "turn-kv-1",
            "last-assistant-message",
            "Done from kv format.",
        ],
    )
    assert result.exit_code == 0
    assert "[codex] Done" in result.output
    assert "Event: after_agent" in result.output
    assert "Turn: turn-kv-1" in result.output


def test_codex_hook_command_without_payload_skips() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "codex-hook",
            "--channel",
            "console",
            "--verbose",
        ],
    )
    assert result.exit_code == 0
    assert "payload missing" in result.output


def test_codex_hook_command_interactive_stdin_skips_without_reading(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()

    class _TTYStdin:
        def isatty(self) -> bool:
            return True

        def read(self) -> str:
            raise AssertionError("read() should not be called for interactive stdin")

    monkeypatch.setattr("agentnotifier.cli.sys.stdin", _TTYStdin())
    result = runner.invoke(
        app,
        [
            "codex-hook",
            "--channel",
            "console",
            "--verbose",
        ],
    )
    assert result.exit_code == 0
    assert "payload missing" in result.output


def test_codex_hook_command_noninteractive_stdin_timeout_skips(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _BlockingStdin:
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            time.sleep(1)
            return ""

    monkeypatch.setattr("agentnotifier.cli.sys.stdin", _BlockingStdin())
    result = cli._read_hook_stdin_text(
        verbose=True,
        source_label="Codex hook",
    )
    assert result is None


def test_setup_codex_command_creates_notify_with_explicit_paths(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    monkeypatch.setattr("agentnotifier.cli.platform.system", lambda: "Linux")
    hook_path = tmp_path / "bin" / "agent-notifier-codex-hook"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("", encoding="utf-8")

    config_path = tmp_path / ".codex" / "config.toml"
    result = runner.invoke(
        app,
        [
            "setup-codex",
            "--codex-config",
            str(config_path),
            "--hook-path",
            str(hook_path),
            "--no-backup",
        ],
    )
    assert result.exit_code == 0
    text = config_path.read_text(encoding="utf-8")
    assert 'notify = ["' in text
    assert "agent-notifier-codex-hook" in text
    assert "[notify]" not in text
    assert "Updated top-level notify hook for Codex." in result.output


def test_setup_codex_command_replaces_notify_table_and_keeps_other_config(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    monkeypatch.setattr("agentnotifier.cli.platform.system", lambda: "Linux")
    hook_path = tmp_path / "bin" / "agent-notifier-codex-hook"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("", encoding="utf-8")

    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        (
            "[notify]\n"
            'program = "powershell.exe"\n'
            'args = ["-NoProfile"]\n'
            'model = "gpt-5"\n\n'
            "[projects.'C:\\\\Users\\\\asael']\n"
            'trust_level = "trusted"\n'
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "setup-codex",
            "--codex-config",
            str(config_path),
            "--hook-path",
            str(hook_path),
        ],
    )
    assert result.exit_code == 0
    text = config_path.read_text(encoding="utf-8")
    assert "[notify]" not in text
    assert 'notify = ["' in text
    assert 'model = "gpt-5"' in text
    assert "[projects.'C:\\\\Users\\\\asael']" in text
    assert 'trust_level = "trusted"' in text
    assert "Backed up" in result.output


def test_setup_codex_command_on_windows_writes_wrapper_notify_command(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    monkeypatch.setattr("agentnotifier.cli.platform.system", lambda: "Windows")

    hook_path = tmp_path / "bin" / "agent-notifier-codex-hook.exe"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("", encoding="utf-8")

    config_path = tmp_path / ".codex" / "config.toml"
    result = runner.invoke(
        app,
        [
            "setup-codex",
            "--codex-config",
            str(config_path),
            "--hook-path",
            str(hook_path),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    text = config_path.read_text(encoding="utf-8")
    assert 'notify = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",' in text
    assert "agent-notifier-codex-wrapper.ps1" in text
    wrapper_path = tmp_path / ".codex" / "agent-notifier-codex-wrapper.ps1"
    assert wrapper_path.exists()
    wrapper_text = wrapper_path.read_text(encoding="utf-8")
    assert str(hook_path.resolve()) in wrapper_text
    assert "Wrapper path:" in result.output


def test_setup_codex_command_on_windows_writes_valid_toml(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    monkeypatch.setattr("agentnotifier.cli.platform.system", lambda: "Windows")

    hook_path = tmp_path / "bin" / "agent-notifier-codex-hook.exe"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("", encoding="utf-8")

    config_path = tmp_path / ".codex" / "config.toml"
    result = runner.invoke(
        app,
        [
            "setup-codex",
            "--codex-config",
            str(config_path),
            "--hook-path",
            str(hook_path),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["notify"][0] == "powershell.exe"
    assert parsed["notify"][-1].endswith("agent-notifier-codex-wrapper.ps1")


def test_setup_codex_command_inserts_notify_before_existing_tables(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    monkeypatch.setattr("agentnotifier.cli.platform.system", lambda: "Windows")

    hook_path = tmp_path / "bin" / "agent-notifier-codex-hook.exe"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("", encoding="utf-8")

    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        (
            'model = "gpt-5.4"\n\n'
            "[projects.'C:\\\\Users\\\\asael']\n"
            'trust_level = "trusted"\n\n'
            "[notice.model_migrations]\n"
            '"gpt-5.3-codex" = "gpt-5.4"\n'
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "setup-codex",
            "--codex-config",
            str(config_path),
            "--hook-path",
            str(hook_path),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    text = config_path.read_text(encoding="utf-8")
    assert 'model = "gpt-5.4"\nnotify = [' in text
    parsed = tomllib.loads(text)
    assert parsed["notify"][0] == "powershell.exe"
    assert parsed["notice"]["model_migrations"]["gpt-5.3-codex"] == "gpt-5.4"


def test_setup_codex_command_fails_for_missing_hook_path(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / ".codex" / "config.toml"
    result = runner.invoke(
        app,
        [
            "setup-codex",
            "--codex-config",
            str(config_path),
            "--hook-path",
            str(tmp_path / "missing" / "agent-notifier-codex-hook"),
            "--no-backup",
        ],
    )
    assert result.exit_code == 1
    assert "Hook path does not exist" in result.output


def test_ollama_hook_command_notifies_on_done_jsonl() -> None:
    runner = CliRunner()
    payload = "\n".join(
        [
            json.dumps({"model": "llama3.1", "response": "working", "done": False}),
            json.dumps(
                {
                    "model": "llama3.1",
                    "response": "All steps complete.",
                    "done": True,
                    "done_reason": "stop",
                }
            ),
        ]
    )
    result = runner.invoke(
        app,
        [
            "ollama-hook",
            "--channel",
            "console",
        ],
        input=payload,
    )
    assert result.exit_code == 0
    assert "[ollama] Done" in result.output
    assert "Event: done" in result.output
    assert "Model: llama3.1" in result.output


def test_ollama_hook_command_skips_incomplete_payload() -> None:
    runner = CliRunner()
    payload = json.dumps({"model": "llama3.1", "response": "working", "done": False})
    result = runner.invoke(
        app,
        [
            "ollama-hook",
            "--channel",
            "console",
        ],
        input=payload,
    )
    assert result.exit_code == 0
    assert result.output == ""
