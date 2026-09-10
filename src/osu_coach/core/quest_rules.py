"""Read-only evaluation of one accepted attempt against one frozen map mission.

The store selects the active player/client/mod context and deduplicates attempt
IDs. This module never combines attempts, updates missions or reads a database.
``now`` is optional and injectable for deterministic tests of timestamp limits.
Numeric accuracy uses the unrounded percentage observed in telemetry.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re

from osu_coach.core.grades import evaluate_grade, normalize_grade


_HASH = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REMOTE_KEY = re.compile(r"(?:remote|osu):([1-9][0-9]*)\Z")
_GRADE_ORDER = {"F": 0, "D": 1, "C": 2, "B": 3, "A": 4, "S": 5, "SS": 6}


def _number(value, minimum=0, maximum=math.inf):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and minimum <= result <= maximum else None
    except (TypeError, ValueError, OverflowError):
        return None


def _count(value):
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _keys(value):
    return {value[field].strip().lower() for field in ("beatmap_key", "key", "checksum")
            if isinstance(value.get(field), str) and value[field].strip()}


def _map_id(value):
    for field in ("beatmap_id", "id"):
        number = _count(value.get(field))
        if number is not None and number > 0:
            return number
    for key in _keys(value):
        match = _REMOTE_KEY.fullmatch(key)
        if match:
            return int(match[1])
    return None


def _same_map(beatmap, play):
    map_keys, play_keys = _keys(beatmap), _keys(play)
    map_hashes = {key for key in map_keys if _HASH.fullmatch(key)}
    play_hashes = {key for key in play_keys if _HASH.fullmatch(key)}
    # A positive online ID must not hide a different local revision/checksum.
    if map_hashes and play_hashes and map_hashes.isdisjoint(play_hashes):
        return False
    if map_keys & play_keys:
        return True
    map_id = _map_id(beatmap)
    # play.id is the attempt UUID, not the beatmap ID.
    play_identity = {key: value for key, value in play.items() if key != "id"}
    play_id = _map_id(play_identity)
    return map_id is not None and play_id is not None and map_id == play_id


def _base_grade(value):
    normalized = normalize_grade(value)
    return {"SH": "S", "SSH": "SS"}.get(normalized, normalized)


def evaluate_attempt(quest: dict, play: dict, *, now: datetime | None = None) -> dict | None:
    """Return checks for this attempt, or None when it cannot belong to a quest.

Missing measurements stay unknown. Every included check must be met in this
same play for ``completed`` to become true. A passed flag alone is insufficient
without an explicit completion fraction of at least 0.98.
"""
    if not isinstance(quest, dict) or not isinstance(play, dict):
        return None
    beatmap = quest.get("map")
    if (not isinstance(beatmap, dict) or not isinstance(play.get("id"), str) or not play["id"].strip()
            or play.get("needs_confirmation") or play.get("excluded")
            or type(play.get("mode")) is not int or play.get("mode") != 0
            or beatmap.get("mode", 0) != 0 or not _same_map(beatmap, play)):
        return None
    created, played = _date(quest.get("created_at")), _date(play.get("played_at"))
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None:
        return None
    if created is None or played is None or played < created or (played - now).total_seconds() > 5:
        return None
    if play.get("started_at") is not None:
        started = _date(play.get("started_at"))
        if started is None or started < created or started > played:
            return None

    expectation = beatmap.get("expectation")
    if not isinstance(expectation, dict):
        return None
    passed = play.get("passed")
    completion = _number(play.get("completion"), 0, 1)
    complete = passed and completion >= .98 if type(passed) is bool and completion is not None else None
    checks = [{"key": "complete", "label": "Completar el mapa", "target": True, "actual": complete,
               "status": "unknown" if complete is None else "met" if complete else "unmet"}]

    if expectation.get("grade_min") is not None:
        target = _base_grade(expectation["grade_min"])
        # A stored letter can describe a partial view; trust it only when the
        # completed result is explicit. An observed failure is always F.
        grade = evaluate_grade(play) if complete is True or (passed is False and completion is not None) else None
        actual = _base_grade(grade)
        status = "unknown"
        if target in _GRADE_ORDER and actual in _GRADE_ORDER:
            status = "met" if _GRADE_ORDER[actual] >= _GRADE_ORDER[target] else "unmet"
        checks.append({"key": "grade", "label": "Grado mínimo", "target": target,
                       "actual": grade, "status": status})

    numeric_goals = (("accuracy_min", "accuracy", "accuracy", "Precisión mínima", False),
                     ("misses_max", "misses", "misses", "Misses máximos", True),
                     ("combo_min", "max_combo", "combo", "Combo mínimo", False))
    for goal_key, value_key, key, label, maximum in numeric_goals:
        if expectation.get(goal_key) is None:
            continue
        if key == "accuracy":
            target = _number(expectation[goal_key], 0, 100)
            actual = None if play.get("accuracy_rounded") else _number(play.get(value_key), 0, 100)
        else:
            target, actual = _count(expectation[goal_key]), _count(play.get(value_key))
        status = "unknown"
        if target is not None and actual is not None:
            met = actual <= target if maximum else actual >= target
            status = "met" if met else "unmet"
        checks.append({"key": key, "label": label, "target": target, "actual": actual, "status": status})
    return {"play_id": play["id"], "played_at": play["played_at"],
            "completed": all(check["status"] == "met" for check in checks), "checks": checks}
