"""macOS notification backend.

This module re-exports ``MacOSNotifier`` from the canonical
``agentnotifier.notifier.macos`` implementation so that code importing from
either ``agentnotifier.notify`` or ``agentnotifier.notifier`` gets the same,
correctly-maintained class.

Previously this file contained a separate copy of the macOS backend that was
missing the ``_normalize_osascript_text`` call, which meant any multiline
notification body would cause ``osascript`` to fail silently (exit 0 but no
visible popup).  The duplication has been removed in favour of a single
authoritative implementation.
"""

from __future__ import annotations

from agentnotifier.notifier.macos import MacOSNotifier

__all__ = ["MacOSNotifier"]
