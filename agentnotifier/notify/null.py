"""Null notifier (test stub).

Re-exports from the canonical ``agentnotifier.notifier.null`` implementation.
See ``notify/macos.py`` for the full rationale.
"""

from __future__ import annotations

from agentnotifier.notifier.null import NullNotifier, SentNotification

__all__ = ["NullNotifier", "SentNotification"]
