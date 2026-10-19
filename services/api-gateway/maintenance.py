"""Planned maintenance, read from Alertmanager silences.

A silence both mutes the alerts and, when its comment starts with
"[maintenance]", tells the status page. Other silences (someone muting a noisy
alert) stay private. Only the comment and the times are ever shown publicly.
"""

from __future__ import annotations

import re
from datetime import datetime

MARKER = "[maintenance]"


def _parse_time(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _matches(matcher: dict, value: str) -> bool:
    expected = matcher.get("value", "")
    if matcher.get("isRegex"):
        try:
            ok = re.fullmatch(expected, value) is not None  # Alertmanager regexes are anchored
        except re.error:
            return False
    else:
        ok = expected == value
    return ok if matcher.get("isEqual", True) else not ok


def windows_from_silences(silences: list[dict]) -> list[dict]:
    """Active and upcoming maintenance windows with their target matchers,
    active ones first, then by start time."""
    windows = []
    for silence in silences:
        state = (silence.get("status") or {}).get("state")
        comment = (silence.get("comment") or "").strip()
        if state not in ("active", "pending") or not comment.lower().startswith(MARKER):
            continue
        matchers = [m for m in silence.get("matchers", []) if m.get("name") == "target"]
        starts, ends = _parse_time(silence.get("startsAt", "")), _parse_time(silence.get("endsAt", ""))
        if not matchers or starts is None or ends is None:
            continue
        windows.append(
            {
                "matchers": matchers,
                "active": state == "active",
                "starts_at": starts,
                "ends_at": ends,
                "comment": comment[len(MARKER):].strip(),
            }
        )
    return sorted(windows, key=lambda w: (not w["active"], w["starts_at"]))


def applies_to(window: dict, target: str) -> bool:
    return all(_matches(m, target) for m in window["matchers"])


def maintenance_for(target: str, windows: list[dict]) -> dict | None:
    """The window that matters most for a target: an active one, else the next."""
    return next((w for w in windows if applies_to(w, target)), None)
