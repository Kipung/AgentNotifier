"""Linux desktop notification backend.

Re-exports ``LinuxNotifier`` from the canonical
``agentnotifier.notifier.linux`` implementation.  See ``notify/macos.py`` for
the full rationale.
"""

from __future__ import annotations

from agentnotifier.notifier.linux import LinuxNotifier

__all__ = ["LinuxNotifier"]
