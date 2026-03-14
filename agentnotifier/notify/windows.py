"""Windows toast notification backend.

Re-exports ``WindowsNotifier`` from the canonical
``agentnotifier.notifier.windows`` implementation.  See ``notify/macos.py``
for the full rationale.
"""

from __future__ import annotations

from agentnotifier.notifier.windows import WindowsNotifier

__all__ = ["WindowsNotifier"]
