from __future__ import annotations

import json

from click.testing import CliRunner

from agentnotifier.cli import _read_stdin_text, app
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


def test_read_stdin_text_skips_interactive_terminal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class TtyStdin:
        def __init__(self) -> None:
            self.read_called = False

        def isatty(self) -> bool:
            return True

        def read(self) -> str:
            self.read_called = True
            raise AssertionError("stdin.read() should not be called for interactive terminals")

    fake_stdin = TtyStdin()
    monkeypatch.setattr("agentnotifier.cli.sys.stdin", fake_stdin)

    result = _read_stdin_text(source_label="Codex hook", verbose=True)

    assert result is None
    assert fake_stdin.read_called is False


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
