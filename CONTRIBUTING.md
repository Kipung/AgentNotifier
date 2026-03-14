# Contributing

Thanks for contributing to `agent-notifier`.

## Setup

```bash
python -m pip install -e ".[dev]"
```

## Local checks

```bash
ruff check .
pytest -q
python -m build
twine check dist/*
```

## Pull requests

- Keep changes focused and small.
- Add or update tests for behavior changes.
- Update `CHANGELOG.md` for user-visible changes.
- Run `agent-notifier doctor --project "$PWD"` when you touch install or hook setup behavior.
- Ensure CI passes on macOS and Windows.

## Commit style

Use clear, imperative commit messages.
