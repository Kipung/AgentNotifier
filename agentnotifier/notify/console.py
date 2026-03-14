"""Console notification backend.

Re-exports ``ConsoleNotifier`` from the canonical
``agentnotifier.notifier.console`` implementation.  See ``notify/macos.py``
for the full rationale.
"""

from __future__ import annotations

from agentnotifier.notifier.console import ConsoleNotifier

__all__ = ["ConsoleNotifier"]
