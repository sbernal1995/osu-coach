"""Recommendation variants and exact play conditions; no star multipliers."""
import copy
import json
import math
from osu_coach.settings import get_setting

PRESETS = {"NM": [], "HD": ["HD"], "HR": ["HR"], "DT": ["DT"], "HT": ["HT"],
           "HDHR": ["HD", "HR"], "HDDT": ["HD", "DT"], "HDHT": ["HD", "HT"]}


def context(mods=None, rate=None, client="lazer"):
    mods = copy.deepcopy(mods or [])
    if isinstance(mods, str):
        mods = PRESETS[mods]
    mods = [({"acronym": m} if isinstance(m, str) else m) for m in mods]
    mods = [m for m in mods if m.get("acronym") != "NM"]
    names = {m["acronym"] for m in mods}
    if "NC" in names:
        mods = [m for m in mods if m["acronym"] != "DT"]
    if "PF" in names:
        mods = [m for m in mods if m["acronym"] != "SD"]
    if rate is None:
        speed = next((m for m in mods if m["acronym"] in {"DT", "NC", "HT", "DC"}), {})
        rate = speed.get("settings", {}).get("speed_change", 1.5 if names & {"DT", "NC"} else .75 if names & {"HT", "DC"} else 1)
    rate = float(rate)
    if not math.isfinite(rate) or not .01 <= rate <= 100:
        raise ValueError("Velocidad de mods no comparable.")
    clean = []
    for mod in mods:
        settings = dict(mod.get("settings") or {})
        if mod["acronym"] in {"DT", "NC", "HT", "DC"} and settings.get("speed_change") == rate:
            settings.pop("speed_change")
        clean.append({"acronym": mod["acronym"], **({"settings": settings} if settings else {})})
    clean.sort(key=lambda m: m["acronym"])
    return {"mods": clean, "rate": rate, "client": client}


def play_context(play):
    saved = json.loads(play.get("mod_key") or "{}")
    return context(play.get("mods", saved.get("mods", [])), saved.get("rate"), play.get("client", "lazer"))


def identity(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def label(value):
    names = " + ".join(m["acronym"] for m in value["mods"]) or "Sin mods"
    return names + ((" · ×" + f'{value["rate"]:g}'.replace(".", ",")) if value["rate"] != 1 else "")


def options(sample=None):
    mode = get_setting("recommendation_mods")
    client = (sample or {}).get("client", "lazer")
    if mode == "profile":
        return [play_context(sample)] if sample else [context()]
    return [context(PRESETS[key], client=client) for key in (PRESETS if mode == "free" else [mode])]


def stamp(beatmap, value):
    return {**beatmap, "play_conditions": value, "mods_label": label(value)}


def duration_ok(beatmap):
    lower, upper = get_setting("recommendation_min_seconds"), get_setting("recommendation_max_seconds")
    if not lower and not upper:
        return True
    length = beatmap.get("length")
    return (isinstance(length, (int, float)) and not isinstance(length, bool) and math.isfinite(length)
            and length >= lower and (not upper or length <= upper))


def conditions_match(expected, play):
    try:
        return identity(expected) == identity(play_context(play))
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def preference_ok(beatmap, sample=None):
    value = beatmap.get("play_conditions") or (play_context(sample) if sample else context())
    return duration_ok(beatmap) and identity(value) in {identity(item) for item in options(sample)}


def public_sample(sample):
    if get_setting("recommendation_mods") == "profile":
        return sample
    return {**(sample or {}), "mods": [], "mod_key": '{"mods":[],"rate":1}'}


def public_requirements(requirements):
    # Published stars cannot predict modded stars. Query broadly, then calculate
    # the actual file and apply exact stage, BPM, AR and duration limits locally.
    mode = get_setting("recommendation_mods")
    if mode not in {"profile", "NM"}:
        requirements = requirements or [{"min_stars": .1, "max_stars": 30}]
        return [{**r, "min_stars": .1, "max_stars": 30, "max_bpm": None, "max_ar": None,
                 "min_length": None, "max_length": None} for r in requirements]
    return requirements


def describe_play(play):
    try:
        return label(play_context(play))
    except (TypeError, ValueError, KeyError, AttributeError):
        return "Mods no verificables"
