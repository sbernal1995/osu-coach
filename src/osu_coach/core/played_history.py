"""Exact difficulty identities from profile-filtered accepted play history.

No time window, performance threshold or global ranking is used. Catalog rows
link local/online keys and revisions of the same beatmap ID. Song titles,
artists, difficulty names and set IDs never establish difficulty identity.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re


_REMOTE_KEY = re.compile(r"(?:remote|osu):([0-9]+)\Z")


def _key(value):
    if not isinstance(value, str):
        return ""
    return value.strip().casefold()


def _positive_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value > 0 and value.is_integer() else None
    if isinstance(value, str):
        value = value.strip()
        if value.isascii() and value.isdecimal():
            result = int(value)
            return result if result > 0 else None
    return None


def _date(value):
    """Only explicit, timezone-aware observation times prove temporal order."""
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _completed_at(item):
    value = _date(item.get("completed_at"))
    last_attempt = item.get("last_attempt")
    if value is None and isinstance(last_attempt, dict):
        value = _date(last_attempt.get("played_at"))
    return value


def _nodes(beatmap, *, play=False):
    if not isinstance(beatmap, dict):
        return set()
    result = set()
    for field in ("key", "beatmap_key", "checksum"):
        key = _key(beatmap.get(field))
        if not key:
            continue
        result.add(("key", key))
        match = _REMOTE_KEY.fullmatch(key)
        if match and (identifier := _positive_id(match[1])) is not None:
            result.add(("id", identifier))
    # An attempt's id is not the difficulty's id, even if it looks numeric.
    for field in (("beatmap_id",) if play else ("id", "beatmap_id")):
        identifier = _positive_id(beatmap.get(field))
        if identifier is not None:
            result.add(("id", identifier))
    return result


class PlayedHistory:
    """Read-only index of exact difficulties played by one active profile.

    ``plays`` must contain that profile's full accepted history. Failed accepted
    attempts count, while pending/excluded records do not. ``completed`` accepts
    beatmap dictionaries or completed quest entries containing a ``map``.

    Catalog identities are joined before played components are marked, so an
    online difficulty can match its local key or another revision of that ID.
    Other difficulties of the same song remain separate, even if their titles,
    artists and difficulty names are identical. Text alone proves no identity.
    ``contains(before=...)`` additionally needs a dated observation at or before
    the cutoff. Undated history blocks new candidates but cannot establish when
    a frozen mission became stale. Queries never modify the index or its input.
    """

    def __init__(self, plays, catalog=(), completed=()):
        self._parents = {}
        self._sizes = {}
        played_nodes = set()
        observed_dates = {}

        def add(beatmap, *, played=False, play=False, observed_at=None):
            nodes = _nodes(beatmap, play=play)
            if not nodes:
                return
            for node in nodes:
                self._parents.setdefault(node, node)
                self._sizes.setdefault(node, 1)
            first = next(iter(nodes))
            for node in nodes:
                self._union(first, node)
            if played:
                played_nodes.update(nodes)
                date = _date(observed_at)
                if date is not None:
                    for node in nodes:
                        previous = observed_dates.get(node)
                        if previous is None or date < previous:
                            observed_dates[node] = date

        for beatmap in catalog or ():
            add(beatmap)
        for play in plays or ():
            if (not isinstance(play, dict) or play.get("excluded") or play.get("needs_confirmation")
                    or play.get("status") in {"pending", "rejected", "excluded"}):
                continue
            add(play, played=True, play=True, observed_at=play.get("played_at"))
        for item in completed or ():
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("map"), dict):
                if item.get("status", "completed") != "completed":
                    continue
                add(item["map"], played=True, observed_at=_completed_at(item))
            else:
                add(item, played=True, observed_at=_completed_at(item))
        # Compress after all unions, including bridges from played records.
        self._played = {self._find(node) for node in played_nodes}
        self._earliest = {}
        for node, date in observed_dates.items():
            component = self._find(node)
            previous = self._earliest.get(component)
            if previous is None or date < previous:
                self._earliest[component] = date
        for node in self._parents:
            self._parents[node] = self._find(node)
        self._sizes.clear()

    def _find(self, node):
        parent = self._parents[node]
        while parent != self._parents[parent]:
            parent = self._parents[parent]
        return parent

    def _union(self, left, right):
        left, right = self._find(left), self._find(right)
        if left == right:
            return
        if self._sizes[left] < self._sizes[right]:
            left, right = right, left
        self._parents[right] = left
        self._sizes[left] += self._sizes[right]

    def contains(self, beatmap, before=None) -> bool:
        """Match the played difficulty, optionally no later than an ISO/datetime.

        A cutoff must contain a timezone; missing or invalid observation dates
        cannot satisfy it. Omitting the cutoff includes undated accepted plays.
        """
        components = {self._parents[node] for node in _nodes(beatmap) if node in self._parents}
        if before is None:
            return bool(components & self._played)
        cutoff = _date(before)
        if cutoff is None:
            return False
        return any(component in self._earliest and self._earliest[component] <= cutoff
                   for component in components)
