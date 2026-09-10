"""Per-map training goals grounded in the active profile's recent play window.

These are conservative coaching heuristics, not forecasts or probabilities.
The caller selects the active player/client/mod profile and thirty-day window.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
from statistics import median

from osu_coach.core.grades import target_grade
from osu_coach.core.tag_analysis import skill_tags
from osu_coach.core.evidence import setting_text
from osu_coach.settings import get_setting




def _number(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _map_key(play):
    return str(play.get("beatmap_key") or play.get("key") or play.get("beatmap_id") or "")


def _time(play):
    value = play.get("played_at")
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value, timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _comparable(window, stars):
    """Two latest attempts per map, including failures before selecting passes."""
    grouped, seen = defaultdict(list), set()
    for play in sorted(window, key=_time, reverse=True):
        sr = _number(play.get("stars"))
        accuracy = _number(play.get("accuracy"))
        key, identifier = _map_key(play), play.get("id")
        if (not key or sr is None or sr <= 0 or abs(sr - stars) > get_setting('comparable_star_band') + 1e-8
                or accuracy is None or not 0 <= accuracy <= 100 or play.get("mode", 0) != 0
                or play.get("excluded") or play.get("needs_confirmation")):
            continue
        if identifier is not None:
            if identifier in seen:
                continue
            seen.add(identifier)
        if len(grouped[key]) < get_setting('max_attempts_per_map'):
            grouped[key].append(play)
    return grouped


def _tag_adjustment(beatmap, analysis):
    names = {item["name"] for item in skill_tags(beatmap)}
    stars = _number(beatmap.get("stars"), 0)
    band = (analysis or {}).get("band", {})
    lower, upper = _number(band.get("min")), _number(band.get("max"))
    if lower is not None and upper is not None and not lower <= stars <= upper:
        return 0, None
    entries = [item for item in (analysis or {}).get("items", [])
               if item.get("tag") in names and item.get("confidence") == "medium"
               and item.get("stats_scope") == "comparable"
               and item.get("comparable_plays", 0) >= get_setting('profile_min_plays')
               and item.get("comparable_maps", 0) >= get_setting('profile_min_maps')
               and item.get("comparable_sessions", 0) >= get_setting('profile_min_sessions')
               and item.get("status") in {"practice", "strength"}]
    # Correlated tags describe the same maps: never add their adjustments.
    weaker = [item for item in entries if item.get("status") == "practice"]
    candidates = weaker or entries
    if not candidates:
        return 0, None
    item = max(candidates, key=lambda row: (row.get("comparable_maps", 0), row.get("tag", "")))
    if weaker:
        return -.5, f"La meta se suaviza por tus resultados en mapas con {item['name'].lower()}."
    return .5, f"Tus resultados en mapas con {item['name'].lower()} permiten pedir algo más de precisión."


def expectation_for(beatmap, profile, stage, tag_analysis=None):
    """Return a concrete, attainable minimum training target for one map.

    ``profile.window`` is already filtered to the active profile, reset and the
    latest 100 attempts in thirty days. Accuracy targets use complete passes only;
    incomplete high accuracy can never be mistaken for a strong complete play.
    """
    window = profile.get("window") or []
    if stage in {"consolidation", "consolidate"} or (stage == "challenge" and not profile.get("challenge_unlocked")):
        stage = "consolidate"
    if stage not in {"warmup", "practice", "consolidate", "challenge"}:
        stage = "practice"
    stars = _number(beatmap.get("stars"), _number(profile.get("baseline"), get_setting('initial_stars')))
    grouped = _comparable(window, stars)
    per_map, completed, failures = [], [], 0
    for attempts in grouped.values():
        passed = [p for p in attempts if p.get("passed") is True
                  and _number(p.get("completion"), 1) >= .98]
        failures += len(attempts) - len(passed)
        completed.extend(passed)
        if not passed:
            continue
        misses, combos = [], []
        for play in passed:
            objects = _number(play.get("judged_objects"), _number(play.get("object_count"), 0))
            miss_count = _number(play.get("misses"))
            if objects and objects > 0 and miss_count is not None and miss_count >= 0:
                misses.append(_clamp(miss_count / objects, 0, 1))
            maximum, combo = _number(play.get("map_max_combo")), _number(play.get("max_combo"))
            if maximum and maximum > 0 and combo is not None and combo >= 0:
                combos.append(_clamp(combo / maximum, 0, 1))
        per_map.append({"accuracy": median(_number(p["accuracy"]) for p in passed),
                        "stars": median(_number(p["stars"]) for p in passed),
                        "miss_rate": median(misses) if misses else None,
                        "combo": median(combos) if combos else None})

    samples, distinct_maps = len(completed), len(per_map)
    # Shrink small samples toward a nearby practice goal; one lucky play has
    # at most a one-third share. A returning player's low accuracy must also
    # lower the starting goal instead of inheriting an arbitrary 96 % demand.
    # Independent maps receive equal weight.
    weight = distinct_maps / (distinct_maps + 2)
    accuracy = max(0, get_setting('strong_accuracy') - 1)
    star_adjustment = 0
    miss_rate = get_setting('strong_miss_percent') / 100
    combo_rate = max(0, get_setting('strong_combo_percent') / 100 - .05)
    if per_map:
        observed_accuracy = median(item["accuracy"] for item in per_map)
        accuracy = min(accuracy, observed_accuracy + 3)
        accuracy += min(observed_accuracy - accuracy, 3) * weight
        reference_stars = median(item["stars"] for item in per_map)
        star_adjustment = _clamp((reference_stars - stars) * 2, -1, 1)
        observed_misses = [item["miss_rate"] for item in per_map if item["miss_rate"] is not None]
        observed_combos = [item["combo"] for item in per_map if item["combo"] is not None]
        if observed_misses:
            miss_rate += (_clamp(median(observed_misses), 0, .04) - miss_rate) * weight
        if observed_combos:
            combo_rate += (median(observed_combos) - combo_rate) * weight

    accuracy += star_adjustment + {"warmup": .5, "practice": 0, "consolidate": .5, "challenge": -1}[stage]
    tag_adjustment, tag_reason = _tag_adjustment(beatmap, tag_analysis)
    accuracy += tag_adjustment
    struggling = failures >= 2
    if struggling:
        accuracy -= min(1.0, failures / max(1, samples + failures))
    accuracy = math.floor(_clamp(accuracy, 0, 99) * 2 + .5) / 2

    # Limits are inclusive, including zero; short maps never get an automatic
    # one-miss allowance. Requiring fewer misses remains a progressive goal.
    multiplier = {"warmup": .5, "practice": .9, "consolidate": .5, "challenge": 1.5}[stage]
    rate_ceiling = {"warmup": .0075, "practice": .02, "consolidate": .0075, "challenge": .03}[stage]
    miss_rate = _clamp(miss_rate * multiplier + max(0, -star_adjustment) * .002, 0, rate_ceiling)
    if struggling:
        miss_rate = min(rate_ceiling, miss_rate + .003)
    objects = _number(beatmap.get("object_count"), 0)
    objects = int(objects) if objects > 0 else 0
    misses_max = math.floor(objects * miss_rate + 1e-8) if objects else None
    # Every miss costs at least 300 weighted accuracy points. Lazer sliders
    # add weighted judgements; the base-object bound is deliberately stricter.
    if misses_max is not None:
        misses_max = min(misses_max, math.floor(objects * (1 - accuracy / 100) + 1e-8))

    combo_shift = {"warmup": .05, "practice": 0, "consolidate": .05, "challenge": -.1}[stage]
    combo_rate = _clamp(combo_rate + combo_shift - (.05 if struggling else 0), .4, .95)
    map_combo = _number(beatmap.get("max_combo"), 0)
    combo_min = math.ceil(map_combo * combo_rate) if map_combo > 0 else None

    latest = max(window, key=_time) if window else {}
    client, mods = latest.get("client", "lazer"), latest.get("mods", [])
    grade = target_grade(client, accuracy, misses_max, mods)
    confidence = "provisional"
    if samples >= 3 and distinct_maps >= 2:
        confidence = "orientative"
    if samples >= get_setting('calibration_plays') and distinct_maps >= get_setting('calibration_maps') and profile.get("phase") == "training":
        confidence = "moderate"
    if samples:
        play_word = "partida completa" if samples == 1 else "partidas completas"
        map_word = "mapa cercano" if distinct_maps == 1 else "mapas cercanos"
        basis = (f"Basado en {samples} {play_word} de {distinct_maps} {map_word} "
                 f"(±{setting_text('comparable_star_band')} ★), con igual peso por mapa.")
    else:
        basis = f"Meta inicial: todavía faltan partidas completas en mapas de este rango (±{setting_text('comparable_star_band')} ★)."
    if tag_reason:
        basis += " " + tag_reason
    note = "Objetivo orientativo de entrenamiento; se ajusta con tus próximas partidas."
    if struggling:
        note += " Por tus intentos recientes, priorizá completar el mapa con control."
    return {
        "kind": "training_target", "stage": stage,
        "grade_min": grade.get("grade"), "grade_label": grade.get("label", "Completar"),
        "accuracy_min": accuracy, "misses_max": misses_max,
        "combo_min": combo_min, "complete_required": True,
        "confidence": confidence, "samples": samples, "distinct_maps": distinct_maps,
        "basis": basis, "note": note,
        "grade_requirements": grade.get("requirements", []), "grade_note": grade.get("note", ""),
        "grade_is_conditional": grade.get("grade_is_conditional", False),
        "evidence_star_band": {"min": round(max(.1, stars - get_setting('comparable_star_band')), 2), "max": round(stars + get_setting('comparable_star_band'), 2)},
        "failed_samples": failures,
        "method": (f"Últimos {get_setting('max_attempts_per_map')} intentos por mapa; precisión y misses de partidas completas. "
                   "Medianas con igual peso por mapa, ajuste gradual por dificultad y etapa. "
                   "Los tags con evidencia suficiente ajustan como máximo 0,5 puntos de precisión."),
    }
