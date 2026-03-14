# Release Checklist

Use this checklist before publishing a new `agent-notifier` release.

## Package checks

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest -q
python -m build
twine check dist/*
```

## Install smoke tests

1. Verify the current checkout:

```bash
agent-notifier doctor --project "$PWD"
agent-notifier test-notifier --channel console
```

2. Verify the packaged install in a clean environment:

```bash
pipx install dist/agent_notifier-*.whl
agent-notifier --help
agent-notifier doctor --project "$PWD"
```

3. For macOS, also verify:

```bash
./examples/setup_macos.sh --trust-dir "$PWD"
```

4. For Windows, verify the desktop fallback path:

```bash
pipx install "agent-notifier[windows]"
agent-notifier test-notifier --channel both --verbose
```

5. For Linux, verify desktop prerequisites:

```bash
command -v notify-send
agent-notifier test-notifier --channel both --verbose
```

## Release notes

1. Update `agentnotifier.__version__`.
2. Update `CHANGELOG.md`.
3. Rebuild `dist/`.
4. Upload only after the smoke tests above pass on real macOS, Windows, and Linux machines.
