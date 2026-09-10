"""osu!standard grade rules, kept separate from the training heuristic.

Checked against primary sources on 2026-09-08:
https://osu.ppy.sh/wiki/en/Gameplay/Grade
https://github.com/ppy/osu/blob/master/osu.Game/Rulesets/Scoring/ScoreProcessor.cs
https://github.com/ppy/osu/blob/master/osu.Game.Rulesets.Osu/Scoring/OsuScoreProcessor.cs
https://github.com/ppy/osu/blob/master/osu.Game.Rulesets.Osu/Mods/OsuModClassic.cs
https://github.com/ppy/osu/blob/master/osu.Game/Rulesets/Mods/ModHidden.cs
https://github.com/ppy/osu/blob/master/osu.Game/Rulesets/Mods/ModFlashlight.cs

Lazer uses inclusive accuracy cutoffs; Miss downgrades S/SS to A. Slider
judgements already affect the game's accuracy where applicable, and there is
no separate slider-break, tail-hit or full-combo requirement for S. Classic
changes slider judgement behaviour but leaves lazer's grade rules in place.
Stable uses the ratio of basic judgements, not an accuracy percentage cutoff.
HD/FL change the colour of S/SS without changing their requirements.

All accuracy inputs use percentages (0..100), as in the local play store.
Targets describe a minimum training goal, never an exact predicted result.
"""
from __future__ import annotations

import math


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _count(value):
    result = _number(value)
    return int(result) if result is not None and result >= 0 and result.is_integer() else None


def _client(value):
    value = str(value or "").strip().lower()
    if value in {"lazer", "osu!lazer", "osu!lazer standard"}:
        return "lazer"
    if value in {"stable", "osu!stable", "osu!stable standard"}:
        return "stable"
    return None


def _silver(mods):
    if isinstance(mods, str):
        mods = mods.replace(",", " ").replace("+", " ").split()
    if not isinstance(mods, (list, tuple, set)):
        return False
    for mod in mods:
        acronym = mod.get("acronym") if isinstance(mod, dict) else mod
        if str(acronym).strip().upper() in {"HD", "FL", "HIDDEN", "FLASHLIGHT"}:
            return True
    return False


def normalize_grade(value):
    """Accept letter grades only; a player's numeric ranking is never a grade."""
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    value = {"X": "SS", "XH": "SSH"}.get(value, value)
    return value if value in {"SS", "SSH", "S", "SH", "A", "B", "C", "D", "F"} else None


def target_grade(client, accuracy_min: float, misses_max: int | None, mods=()) -> dict:
    """Select a minimum grade goal and expose any extra stable conditions.

For stable, the choice of letter is a training heuristic and its judgement
requirements are part of the goal. ``grade_is_conditional`` tells the caller
to display those requirements: accuracy/misses alone do not define that goal.
Unknown clients and invalid target data return no grade instead of guessing.
``misses_max=None`` means unspecified, and therefore cannot support an S goal.
"""
    accuracy = _number(accuracy_min)
    misses = _count(misses_max)
    client = _client(client)
    if client is None or accuracy is None or not 0 <= accuracy <= 100 or (misses_max is not None and misses is None):
        return {
            "grade": None, "label": "Completar el mapa", "requirements": ["Completar el mapa"],
            "note": "El grado objetivo requiere un cliente identificado y una meta de precisión válida.",
            "grade_is_conditional": True,
        }

    if accuracy >= 95 and misses == 0:
        grade = "S"
    elif accuracy >= 90:
        grade = "A"
    elif accuracy >= 80:
        grade = "B"
    elif accuracy >= 70:
        grade = "C"
    else:
        grade = "D"

    requirements = ["Completar el mapa"]
    if client == "stable":
        extra = {
            "S": "Más del 90 % de juicios 300, como máximo 1 % de juicios 50 y 0 misses.",
            "A": "Más del 80 % de juicios 300 con 0 misses, o más del 90 % de juicios 300.",
            "B": "Más del 70 % de juicios 300 con 0 misses, o más del 80 % de juicios 300.",
            "C": "Más del 60 % de juicios 300.",
        }.get(grade)
        if extra:
            requirements.append(extra)
        note = "En stable, el grado depende también de la proporción de juicios 300 y 50."
    else:
        extra = {
            "S": "Al menos 95 % de precisión y 0 misses.",
            "A": "Al menos 90 % de precisión.",
            "B": "Al menos 80 % de precisión.",
            "C": "Al menos 70 % de precisión.",
        }.get(grade)
        if extra:
            requirements.append(extra)
        note = "El grado indicado es el mínimo objetivo."

    if _silver(mods) and grade == "S":
        note += " Con HD o FL, la S se muestra plateada."
    return {
        "grade": grade, "label": f"{grade} o mejor", "requirements": requirements,
        "note": note, "grade_is_conditional": client == "stable" and grade != "D",
    }


def evaluate_grade(play: dict) -> str | None:
    """Read an actual grade, or calculate it only from sufficient final data.

Prefer the stored grade from the game. Raw stable fallback requires explicit
n300/n100/n50/misses, with missing counters treated as unknown. Lazer fallback
requires the game's full-precision accuracy percentage and an explicit miss
count; do not pass a rounded UI value. Its accuracy already includes sliders.
``accuracy_rounded=True`` disables that fallback. Incomplete/unknown results
return None, while an observed failed attempt is F.
"""
    if not isinstance(play, dict) or play.get("mode", 0) != 0:
        return None
    if play.get("passed") is False:
        return "F"
    for field in ("grade", "score_grade", "rank"):
        grade = normalize_grade(play.get(field))
        if grade is not None:
            return grade
    if play.get("passed") is not True:
        return None
    completion = _number(play.get("completion", 1))
    if completion is None or completion < 1:
        return None

    client = _client(play.get("client"))
    misses = _count(play.get("misses"))
    if client is None or misses is None:
        return None
    if client == "stable":
        counts = [_count(play.get(field)) for field in ("n300", "n100", "n50")]
        if any(count is None for count in counts):
            return None
        n300, n100, n50 = counts
        total = n300 + n100 + n50 + misses
        if total <= 0:
            return None
        if n300 == total:
            grade = "SS"
        elif n300 * 10 > total * 9 and n50 * 100 <= total and misses == 0:
            grade = "S"
        elif (n300 * 10 > total * 8 and misses == 0) or n300 * 10 > total * 9:
            grade = "A"
        elif (n300 * 10 > total * 7 and misses == 0) or n300 * 10 > total * 8:
            grade = "B"
        elif n300 * 10 > total * 6:
            grade = "C"
        else:
            grade = "D"
    else:
        accuracy = _number(play.get("accuracy"))
        if accuracy is None or not 0 <= accuracy <= 100 or play.get("accuracy_rounded"):
            return None
        if accuracy == 100 and misses == 0:
            grade = "SS"
        elif accuracy >= 95 and misses == 0:
            grade = "S"
        elif accuracy >= 90:
            grade = "A"
        elif accuracy >= 80:
            grade = "B"
        elif accuracy >= 70:
            grade = "C"
        else:
            grade = "D"
    return grade + "H" if grade in {"S", "SS"} and _silver(play.get("mods")) else grade
