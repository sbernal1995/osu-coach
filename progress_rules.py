"""Evidence for configurable personal ranks from an already filtered window.

The caller supplies assess()['window'] for one player/client/mod context (configured
play count and age). Persistence and the monotonic earned rank belong to the store.
All measurements must belong to a single attempt; repeated maps add no credit.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

from evidence import setting_text
from settings import get_setting


MIN_RANK = Decimal("0.5")
MAX_RANK = Decimal("10.5")


def _step():
    return Decimal(str(get_setting("rank_step")))


def _requirements():
    return [
        "Terminá la calibración para empezar a ganar rangos.",
        f"Demostrá el nivel en {get_setting('rank_required_maps')} dificultades distintas, entre el rango y "
        f"{setting_text('comparable_star_band')} ★ por encima.",
        f"Cuenta tu mejor resultado válido de cada mapa en las últimas {get_setting('reference_plays')} "
        f"partidas de los últimos {get_setting('reference_days')} días.",
        f"En una misma partida: mapa aprobado y al menos 98 % completado, precisión de "
        f"{setting_text('strong_accuracy')} % o más, hasta {setting_text('strong_miss_percent')} % de misses "
        f"sobre objetos juzgados y al menos {setting_text('strong_combo_percent')} % del combo máximo.",
    ]


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _count(value):
    value = _number(value)
    return int(value) if value is not None and value >= 0 and value == value.to_integral_value() else None


def _rung(value):
    """Clamp a finite level to the preceding configured rung, anchored at the minimum."""
    value = _number(value)
    if value is None:
        return None
    value = max(MIN_RANK, min(MAX_RANK, value))
    step = _step()
    return MIN_RANK + ((value - MIN_RANK) / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _attempt(play):
    """Return a qualifying whole attempt with its identity and quality order."""
    if (not isinstance(play, dict) or play.get("passed") is not True
            or play.get("excluded") or play.get("needs_confirmation")
            or play.get("accuracy_rounded") is True
            or play.get("mode", 0) != 0 or isinstance(play.get("mode"), bool)):
        return None
    completion, accuracy, stars = (_number(play.get(key)) for key in ("completion", "accuracy", "stars"))
    misses, judged, combo, maximum = (_count(play.get(key)) for key in
                                      ("misses", "judged_objects", "max_combo", "map_max_combo"))
    if (completion is None or not Decimal(".98") <= completion <= 1
            or accuracy is None or not Decimal(str(get_setting("strong_accuracy"))) <= accuracy <= 100
            or stars is None or not 0 < stars <= 100
            or any(value is None for value in (misses, judged, combo, maximum))
            or judged <= 0 or maximum <= 0 or misses > judged or combo > maximum
            or Decimal(misses) * 100 > Decimal(judged) * Decimal(str(get_setting("strong_miss_percent")))
            or Decimal(combo) * 100 < Decimal(maximum) * Decimal(str(get_setting("strong_combo_percent")))):
        return None

    beatmap_id = _count(play.get("beatmap_id"))
    key = next((play[field].strip() for field in ("beatmap_key", "key")
                if isinstance(play.get(field), str) and play[field].strip()), None)
    identity = ("id", beatmap_id) if beatmap_id else ("key", key.casefold()) if key else None
    if identity is None:
        return None
    stars = stars.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    combo_ratio = Decimal(combo) / maximum
    miss_ratio = Decimal(misses) / judged
    played_at = play.get("played_at") if isinstance(play.get("played_at"), str) else None
    result = {
        "key": key or f"osu:{beatmap_id}",
        "title": play.get("title") or "Mapa sin título",
        "version": play.get("version") or "",
        "stars": float(stars), "accuracy": float(accuracy), "misses": misses,
        "combo_ratio": float(combo_ratio), "played_at": played_at,
    }
    # A later weaker attempt does not erase earlier proof in the same window.
    # Stars break exact quality ties only; they do not replace playing quality.
    quality = (accuracy, -miss_ratio, combo_ratio, completion, stars, played_at or "", result["key"])
    return identity, stars, quality, result


def rank_evidence(profile: dict, earned_stars=None) -> dict:
    """Describe current proof and the next rung, without granting/storing ranks.

``candidate_stars`` is the highest rung backed by the configured number of distinct maps now.
``qualifying_maps`` and ``completed_maps`` refer only to ``next_stars``, not to
an earlier earned rank. At the 10.5-star cap there is no next rung: its value is
None and its evidence list/count are empty/zero. An unknown baseline starts the
displayed goal at 0.5; it never creates evidence or grants a rank.
"""
    profile = profile if isinstance(profile, dict) else {}
    calibrated = profile.get("phase") == "training"
    earned = _number(earned_stars)
    step = _step()
    required_maps = get_setting("rank_required_maps")
    if earned is None:
        next_rank = _rung(profile.get("baseline")) or MIN_RANK
    else:
        next_rank = _rung(earned) + step if earned < MAX_RANK else None
        if next_rank is not None and next_rank > MAX_RANK:
            next_rank = None
    result = {
        "candidate_stars": None,
        "next_stars": float(next_rank) if next_rank is not None else None,
        "qualifying_maps": [], "completed_maps": 0, "required_maps": required_maps,
        "calibrated": calibrated, "requirements": _requirements(),
        "window_plays": get_setting("reference_plays"), "window_days": get_setting("reference_days"),
    }
    if not calibrated:
        return result

    best = {}
    window = profile.get("window")
    for play in window if isinstance(window, (list, tuple)) else ():
        attempt = _attempt(play)
        if attempt is not None:
            identity, _, quality, _ = attempt
            if identity not in best or quality > best[identity][2]:
                best[identity] = attempt
    attempts = sorted(best.values(), key=lambda attempt: (attempt[1], attempt[2]), reverse=True)

    def in_band(rank):
        return [attempt[3] for attempt in attempts if rank <= attempt[1] <= rank + Decimal(str(get_setting("comparable_star_band")))]

    for index in range(int((MAX_RANK - MIN_RANK) / step), -1, -1):
        rank = MIN_RANK + step * index
        if len(in_band(rank)) >= required_maps:
            result["candidate_stars"] = float(rank)
            break
    if next_rank is not None:
        result["qualifying_maps"] = in_band(next_rank)[:required_maps]
        result["completed_maps"] = len(result["qualifying_maps"])
    return result
