"""Shared recency and session rules for reset-filtered player evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import hashlib
import json
from osu_coach.settings import get_setting

REFERENCE_PLAYS = 100
REFERENCE_DAYS = 30
SESSION_PLAYS = 20
SESSION_DAYS = 7
HALF_LIFE_DAYS = 10
SESSION_GAP_SECONDS = 60 * 60
MIN_EVIDENCE_PLAYS = 8
MIN_EVIDENCE_MAPS = 5
MIN_EVIDENCE_SESSIONS = 2


def play_time(value):
    if isinstance(value, dict):
        value = value.get("played_at")
    try:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result = datetime.fromtimestamp(value, timezone.utc)
        else:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def recency_weight(play, now=None):
    instant = play_time(play)
    if instant is None:
        return 0.0
    reference = play_time(now) or datetime.now(timezone.utc)
    age = max(0.0, (reference - instant).total_seconds() / 86400)
    return 2.0 ** (-age / get_setting('half_life_days'))


def weighted_mean(values_weights):
    pairs = [(float(value), float(weight)) for value, weight in values_weights
             if weight is not None and weight > 0 and math.isfinite(value) and math.isfinite(weight)]
    total = sum(weight for _, weight in pairs)
    return sum(value * weight for value, weight in pairs) / total if total else None


def trimmed_weighted_mean(values_weights, trim=None):
    """Trim a fraction of total weight at each tail, including partial weights."""
    trim = get_setting("trim_percent") / 100 if trim is None else trim
    pairs = sorted((float(value), float(weight)) for value, weight in values_weights
                   if weight > 0 and math.isfinite(value) and math.isfinite(weight))
    total = sum(weight for _, weight in pairs)
    if not total:
        return None
    lower, upper, position, kept = total * trim, total * (1 - trim), 0.0, []
    for value, weight in pairs:
        retained = max(0.0, min(position + weight, upper) - max(position, lower))
        if retained:
            kept.append((value, retained))
        position += weight
    return weighted_mean(kept)


def session_ids(plays):
    """Assign sessions on the full window before filtering maps/tags/metrics."""
    ordered = sorted(((play_time(play), play) for play in plays if play_time(play) is not None),
                     key=lambda pair: pair[0])
    result, previous, session = {}, None, None
    for instant, play in ordered:
        if previous is None or (instant - previous).total_seconds() >= get_setting('session_gap_minutes') * 60:
            session = instant.isoformat()
        identifier = play.get("id")
        if identifier is not None:
            result[identifier] = session
        previous = instant
    return result


def session_count(plays, sessions=None):
    plays = list(plays)
    sessions = session_ids(plays) if sessions is None else sessions
    return len({sessions[play.get("id")] for play in plays if play.get("id") in sessions})


# Historical method-2 points were recorded with these known parameters. This
# fingerprint classifies old observations; current calculations always read settings.
LEGACY_REFERENCE_SETTINGS = {
    "reference_plays": 100, "reference_days": 30, "calibration_plays": 5,
    "calibration_maps": 3, "initial_stars": 2.5, "half_life_days": 10,
    "max_attempts_per_map": 2, "trim_percent": 20, "strong_accuracy": 97,
    "strong_miss_percent": .5, "strong_combo_percent": 80,
}


def reference_settings_signature():
    values = {key: get_setting(key) for key in LEGACY_REFERENCE_SETTINGS}
    if values == LEGACY_REFERENCE_SETTINGS:
        return "default-v2"
    encoded = json.dumps({key: float(value) for key, value in values.items()},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def setting_text(key, decimals=None):
    value = get_setting(key)
    return (f"{value:g}" if decimals is None else f"{value:.{decimals}f}").replace(".", ",")
