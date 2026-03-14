"""Tests for the CLI setup commands and their underlying setup helpers.

Design note on shutil.which mocking
------------------------------------
``resolve_*_hook_command()`` now calls ``shutil.which()`` to write an absolute
path into config files instead of a bare binary name.  Hook subprocesses
launched by Claude Code / Codex / Gemini run with a restricted PATH that often
omits the pipx/pip scripts directory, so using the full path is required for
the hooks to work on machines other than the developer's own.

We mock ``agentnotifier.core.setup.shutil.which`` in each test that exercises
the default path-resolution behaviour so that:

  1. Tests are deterministic regardless of which Python environment is active.
  2. We can explicitly assert the absolute-path happy-path AND the bare-name
     fallback (when the binary is not on PATH during setup).
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from agentnotifier.cli import app

# ---------------------------------------------------------------------------
# Fake absolute paths used across tests
# ---------------------------------------------------------------------------
_CODEX_HOOK_PATH = "/test/bin/agent-notifier-codex-hook"
_GEMINI_HOOK_PATH = "/test/bin/agent-notifier-gemini-hook"
_CLAUDE_HOOK_PATH = "/test/bin/agent-notifier-claude-hook"


def _fake_which(name: str) -> str | None:
    """Simulate shutil.which resolving all agent-notifier entry-points."""
    return {
        "agent-notifier-codex-hook": _CODEX_HOOK_PATH,
        "agent-notifier-gemini-hook": _GEMINI_HOOK_PATH,
        "agent-notifier-claude-hook": _CLAUDE_HOOK_PATH,
    }.get(name)


# ---------------------------------------------------------------------------
# Codex setup
# ---------------------------------------------------------------------------


def test_setup_codex_command_creates_config(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """setup codex writes the resolved absolute path into config.toml.

    When agent-notifier-codex-hook is found on PATH (the common case after
    ``pipx install agent-notifier``), setup must write its absolute path so
    that Codex's hook subprocess — which runs with a restricted PATH — can
    locate the binary.
    """
    monkeypatch.setattr("agentnotifier.core.setup.shutil.which", _fake_which)
    config_path = tmp_path / ".codex" / "config.toml"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "codex",
            "--config",
            str(config_path),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    assert "Updated" in result.output
    assert config_path.read_text(encoding="utf-8") == (
        f'notify = ["{_CODEX_HOOK_PATH}"]\n'
    )


def test_setup_codex_command_falls_back_to_bare_name_when_not_found(
    tmp_path, monkeypatch  # noqa: ANN001
) -> None:
    """setup codex falls back to the bare name when shutil.which returns None.

    This covers the case where a user runs ``setup`` from a source checkout
    before the package has been installed into any discoverable PATH location.
    The bare name is no worse than the previous default behaviour and can
    always be overridden with ``--hook-command``.
    """
    monkeypatch.setattr("agentnotifier.core.setup.shutil.which", lambda _name: None)
    config_path = tmp_path / ".codex" / "config.toml"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "codex",
            "--config",
            str(config_path),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    assert config_path.read_text(encoding="utf-8") == 'notify = ["agent-notifier-codex-hook"]\n'


def test_setup_codex_command_appends_to_existing_notify_value(tmp_path) -> None:
    """--hook-command bypasses shutil.which; the explicit path is written as-is."""
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        'model = "gpt-5.4"\nnotify = "existing-hook"\n',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "codex",
            "--config",
            str(config_path),
            "--hook-command",
            "/tmp/bin/agent-notifier-codex-hook",
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    assert (
        config_path.read_text(encoding="utf-8")
        == 'model = "gpt-5.4"\nnotify = ["existing-hook", "/tmp/bin/agent-notifier-codex-hook"]\n'
    )


def test_setup_codex_command_is_idempotent(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        'notify = ["/tmp/bin/agent-notifier-codex-hook"]\n',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "codex",
            "--config",
            str(config_path),
            "--hook-command",
            "/tmp/bin/agent-notifier-codex-hook",
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    assert "No changes needed" in result.output
    assert (
        config_path.read_text(encoding="utf-8")
        == 'notify = ["/tmp/bin/agent-notifier-codex-hook"]\n'
    )


def test_setup_codex_command_replaces_legacy_codex_hook_entry(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        'notify = ["agent-notifier-codex-hook", "other-hook"]\n',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "codex",
            "--config",
            str(config_path),
            "--hook-command",
            "/tmp/bin/agent-notifier-codex-hook",
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    assert (
        config_path.read_text(encoding="utf-8")
        == 'notify = ["/tmp/bin/agent-notifier-codex-hook", "other-hook"]\n'
    )


# ---------------------------------------------------------------------------
# Gemini setup
# ---------------------------------------------------------------------------


def test_setup_gemini_command_creates_settings_and_trust(
    tmp_path, monkeypatch  # noqa: ANN001
) -> None:
    """setup gemini writes the resolved absolute hook path into settings.json."""
    monkeypatch.setattr("agentnotifier.core.setup.shutil.which", _fake_which)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    settings_path = tmp_path / ".gemini" / "settings.json"
    trusted_path = tmp_path / ".gemini" / "trustedFolders.json"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "gemini",
            "--project",
            str(project_dir),
            "--settings",
            str(settings_path),
            "--trusted-folders",
            str(trusted_path),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooksConfig"]["enabled"] is True
    assert settings["hooks"]["AfterAgent"][0]["hooks"][0]["command"] == _GEMINI_HOOK_PATH

    trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
    assert trusted[str(project_dir.resolve())] == "TRUST_FOLDER"


def test_setup_gemini_command_falls_back_to_bare_name_when_not_found(
    tmp_path, monkeypatch  # noqa: ANN001
) -> None:
    """setup gemini falls back to the bare name when shutil.which returns None."""
    monkeypatch.setattr("agentnotifier.core.setup.shutil.which", lambda _name: None)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    settings_path = tmp_path / ".gemini" / "settings.json"
    trusted_path = tmp_path / ".gemini" / "trustedFolders.json"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "gemini",
            "--project",
            str(project_dir),
            "--settings",
            str(settings_path),
            "--trusted-folders",
            str(trusted_path),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooks"]["AfterAgent"][0]["hooks"][0]["command"] == (
        "agent-notifier-gemini-hook"
    )


# ---------------------------------------------------------------------------
# Claude setup
# ---------------------------------------------------------------------------


def test_setup_claude_command_creates_project_settings(
    tmp_path, monkeypatch  # noqa: ANN001
) -> None:
    """setup claude writes absolute-path hook commands into settings.local.json.

    Both Stop and SubagentStop hooks must reference the full path so that
    Claude Code's hook subprocess can locate the binary even when running
    with a restricted PATH.
    """
    monkeypatch.setattr("agentnotifier.core.setup.shutil.which", _fake_which)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "claude",
            "--project",
            str(project_dir),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0

    settings_path = project_dir / ".claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    stop_command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    subagent_command = settings["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
    assert stop_command == f"{_CLAUDE_HOOK_PATH} --event Stop"
    assert subagent_command == f"{_CLAUDE_HOOK_PATH} --event SubagentStop"


def test_setup_claude_command_falls_back_to_bare_name_when_not_found(
    tmp_path, monkeypatch  # noqa: ANN001
) -> None:
    """setup claude falls back to the bare name when shutil.which returns None."""
    monkeypatch.setattr("agentnotifier.core.setup.shutil.which", lambda _name: None)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "claude",
            "--project",
            str(project_dir),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    settings_path = project_dir / ".claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    stop_command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert stop_command == "agent-notifier-claude-hook --event Stop"


# ---------------------------------------------------------------------------
# setup all
# ---------------------------------------------------------------------------


def test_setup_all_command_wires_codex_gemini_and_claude(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """setup all writes absolute hook paths for every integration in one pass."""
    home = tmp_path / "home"
    project_dir = tmp_path / "project"
    home.mkdir()
    project_dir.mkdir()

    monkeypatch.setattr("agentnotifier.core.setup.Path.home", lambda: home)
    monkeypatch.setattr("agentnotifier.core.setup.shutil.which", _fake_which)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "all",
            "--project",
            str(project_dir),
            "--no-backup",
        ],
    )

    assert result.exit_code == 0
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == (
        f'notify = ["{_CODEX_HOOK_PATH}"]\n'
    )

    gemini_settings = json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert gemini_settings["hooks"]["AfterAgent"][0]["hooks"][0]["command"] == _GEMINI_HOOK_PATH

    claude_settings = json.loads(
        (project_dir / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert claude_settings["hooks"]["Stop"][0]["hooks"][0]["command"] == (
        f"{_CLAUDE_HOOK_PATH} --event Stop"
    )
