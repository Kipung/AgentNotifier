from __future__ import annotations

import json
import subprocess

from click.testing import CliRunner

from agentnotifier.cli import app


def test_doctor_command_reports_configured_integrations(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".codex").mkdir(parents=True)
    (home / ".gemini").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    project.mkdir(exist_ok=True)

    (home / ".codex" / "config.toml").write_text(
        'notify = ["agent-notifier-codex-hook"]\n',
        encoding="utf-8",
    )
    (home / ".gemini" / "settings.json").write_text(
        json.dumps(
            {
                "hooksConfig": {"enabled": True},
                "hooks": {
                    "AfterAgent": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "agent-notifier-gemini-hook",
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (home / ".gemini" / "trustedFolders.json").write_text(
        json.dumps({str(project.resolve()): "TRUST_FOLDER"}),
        encoding="utf-8",
    )
    (project / ".claude" / "settings.local.json").write_text(
        '{"hooks":{"Stop":[{"hooks":[{"command":"agent-notifier-claude-hook --event Stop"}]}]}}',
        encoding="utf-8",
    )

    monkeypatch.setattr("agentnotifier.core.doctor.Path.home", lambda: home)
    monkeypatch.setattr("agentnotifier.core.doctor.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "agentnotifier.core.doctor.shutil.which",
        lambda name: {
            "agent-notifier": "/tmp/bin/agent-notifier",
            "agent-notifier-codex-hook": "/tmp/bin/agent-notifier-codex-hook",
            "agent-notifier-gemini-hook": "/tmp/bin/agent-notifier-gemini-hook",
            "agent-notifier-claude-hook": "/tmp/bin/agent-notifier-claude-hook",
            "terminal-notifier": "/opt/homebrew/bin/terminal-notifier",
            "osascript": "/usr/bin/osascript",
        }.get(name),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--project", str(project), "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["summary"]["platform"] == "Darwin"
    assert report["sections"]["codex"][0]["status"] == "ok"
    assert report["sections"]["gemini"][0]["status"] == "ok"
    assert report["sections"]["gemini"][1]["status"] == "ok"
    assert report["sections"]["gemini"][2]["status"] == "ok"
    assert report["sections"]["claude"][0]["status"] == "ok"


def test_doctor_command_warns_about_missing_windows_backend(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()

    monkeypatch.setattr("agentnotifier.core.doctor.Path.home", lambda: home)
    monkeypatch.setattr("agentnotifier.core.doctor.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "agentnotifier.core.doctor.shutil.which",
        lambda name: {
            "agent-notifier": "C:/bin/agent-notifier.exe",
            "agent-notifier-codex-hook": "C:/bin/agent-notifier-codex-hook.exe",
            "agent-notifier-gemini-hook": "C:/bin/agent-notifier-gemini-hook.exe",
            "agent-notifier-claude-hook": "C:/bin/agent-notifier-claude-hook.exe",
            "powershell": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        }.get(name),
    )
    monkeypatch.setattr(
        "agentnotifier.core.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="",
        ),
    )
    monkeypatch.setattr("agentnotifier.core.doctor.importlib.util.find_spec", lambda name: None)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--project", str(project), "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["summary"]["platform"] == "Windows"
    assert report["sections"]["backend"][1]["status"] == "warn"
    assert 'agent-notifier[windows]' in report["sections"]["backend"][1]["fix"]
