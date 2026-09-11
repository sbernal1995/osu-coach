"""Conservative, transparent training heuristics. Never uses rank, pp or best scores."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
import math
from statistics import mean
from urllib.parse import urlencode

from osu_coach.core.mod_policy import duration_ok, stamp as stamp_mods, play_context, context as mod_context
from osu_coach.core.expectations import expectation_for
from osu_coach.core.training import training_goal, skill_target, skills_for
from osu_coach.settings import get_setting
from osu_coach.beatmaps.map_search import search_details
from osu_coach.core.evidence import (recency_weight, weighted_mean, trimmed_weighted_mean, play_time,
                      setting_text, reference_settings_signature)


def timestamp(value):
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value, timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


def map_key(play):
    return str(play.get("beatmap_key") or play.get("beatmap_id") or play.get("key") or "unknown")


def rates(play):
    # Hits from incomplete plays use judged_objects when telemetry supplies it.
    objects = max(1, number(play.get("judged_objects"), number(play.get("object_count"), 1)))
    miss_rate = number(play.get("misses")) / objects
    maximum = number(play.get("map_max_combo"))
    combo = min(1.0, number(play.get("max_combo")) / maximum) if maximum > 0 else None
    return miss_rate, combo


def strong(play):
    misses, combo = rates(play)
    return (play.get("passed") is True and number(play.get("completion"), 1) >= .98
            and number(play.get("accuracy")) >= get_setting('strong_accuracy') and misses <= get_setting('strong_miss_percent') / 100
            and (combo is None or combo >= get_setting('strong_combo_percent') / 100))


def struggling(play):
    return (play.get("passed") is False or number(play.get("accuracy")) < 90
            or rates(play)[0] > .035)


def recent_window(plays, now=None, since=None, *, limit=None, days=None):
    now = play_time(now) or datetime.now(timezone.utc)
    limit = get_setting('reference_plays') if limit is None else limit
    days = get_setting('reference_days') if days is None else days
    cutoff = now - timedelta(days=days)
    if timestamp(since):
        cutoff = max(cutoff, timestamp(since))
    seen, selected = set(), []
    for play in sorted(plays, key=lambda p: timestamp(p.get("played_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        dt = timestamp(play.get("played_at"))
        identifier = play.get("id")
        if (not dt or not cutoff <= dt <= now + timedelta(seconds=5)
                or identifier in seen or play.get("mode", 0) != 0
                or number(play.get("stars")) <= 0 or play.get("excluded")
                or play.get("needs_confirmation")):
            continue
        seen.add(identifier)
        selected.append(play)
        if len(selected) == limit:
            break
    return list(reversed(selected))


def assess(plays, now=None, since=None, initial=None):
    now = play_time(now) or datetime.now(timezone.utc)
    plays = list(plays)
    initial = get_setting('initial_stars') if initial is None else initial
    window = recent_window(plays, now, since)
    session_window = recent_window(plays, now, since, limit=get_setting('session_plays'), days=get_setting('session_days'))
    base = max(.5, min(10.5, number(initial, get_setting('initial_stars'))))
    unique = len({map_key(play) for play in window})
    phase = "training" if len(window) >= get_setting('calibration_plays') and unique >= get_setting('calibration_maps') else "calibrating"
    def adjusted_level(play):
        correction = ((-.8 if number(play.get("completion"), 1) < .5 else -.45) if struggling(play)
                      else -.15 if not strong(play) else .15 if phase == "training" else 0)
        return number(play["stars"]) + correction
    grouped = defaultdict(list)
    for play in reversed(window):
        key = map_key(play)
        if len(grouped[key]) < get_setting('max_attempts_per_map'):
            grouped[key].append(play)
    evidence, accuracies = [], []
    for attempts in grouped.values():
        weights = [recency_weight(play, now) for play in attempts]
        map_weight = max(weights)
        estimate = weighted_mean((adjusted_level(play), weight) for play, weight in zip(attempts, weights))
        accuracy = weighted_mean((number(play.get("accuracy")), weight) for play, weight in zip(attempts, weights))
        evidence.append((estimate, map_weight))
        accuracies.append((accuracy, map_weight))
    if evidence:
        # Per-map recency weights and trimmed tails keep one outlier or repeated
        # song from dominating. The oldest surviving play is never a seed.
        base = max(.5, min(10.5, trimmed_weighted_mean(evidence)))
    reference_accuracy = weighted_mean(accuracies)

    # The short window changes session demands, independently of the longer
    # measured reference. Repeated attempts cannot stand for multiple maps.
    counts, sample = Counter(), []
    for play in reversed(session_window):
        key = map_key(play)
        if counts[key] < get_setting('max_attempts_per_map'):
            sample.append(play)
            counts[key] += 1
    accuracy = mean(number(p.get("accuracy")) for p in sample) if sample else None
    misses = mean(rates(p)[0] for p in sample) if sample else 0
    clean_maps = {map_key(p) for p in session_window[-max(5, get_setting('challenge_maps')):]
                  if strong(p) and base - get_setting('warmup_offset') - 1e-8 <= number(p["stars"]) <= base + .4 + 1e-8}
    hard = [p for p in session_window[-3:] if struggling(p) and number(p["stars"]) <= base + .6]
    trend = "down" if len(hard) >= 2 else "up" if phase == "training" and len(clean_maps) >= get_setting('challenge_maps') else "steady"
    if not session_window:
        focus = "Retomar el ritmo"
        message = "Jugá un mapa cómodo para actualizar el estado de tu sesión."
    elif misses > .02 or any(not p.get("passed") for p in session_window[-2:]):
        focus = "Completar con control"
        message = "Buscá terminar el mapa con menos misses. La práctica sugerida mantiene una dificultad accesible."
    elif accuracy < 96:
        focus = "Precisión"
        message = "Repetí una dificultad manejable e intentá llegar al 96 % de precisión antes de aumentar."
    else:
        focus = "Consistencia"
        message = f"Buscá {setting_text('strong_accuracy')} % de precisión y pocos misses en mapas distintos. {get_setting('challenge_maps')} resultados sólidos habilitan un paso pequeño."
    if phase == "calibrating":
        message = f"Calibración: {min(get_setting('calibration_plays'), len(window))}/{get_setting('calibration_plays')} partidas y {min(get_setting('calibration_maps'), unique)}/{get_setting('calibration_maps')} mapas distintos. " + message
    if not window:
        focus = "Calibración"
        message = "Jugá un mapa que hoy te resulte cómodo. La primera partida ajustará este punto de partida."
    challenge_window = session_window[-get_setting('challenge_maps'):]
    unlocked = (phase == "training" and len(challenge_window) == get_setting('challenge_maps')
                and all(p.get("passed") for p in challenge_window)
                and all(number(p.get("accuracy")) >= get_setting('challenge_accuracy')
                        and rates(p)[0] <= get_setting('challenge_miss_percent') / 100 for p in challenge_window)
                and len({map_key(p) for p in challenge_window}) == get_setting('challenge_maps'))
    session = {"attempts": len(session_window), "distinct_maps": len({map_key(p) for p in session_window}),
               "accuracy": round(accuracy, 2) if accuracy is not None else None,
               "trend": trend, "focus": focus, "message": message,
               "adjustment": -get_setting('recovery_drop') if trend == "down" else 0,
               "window_plays": get_setting('session_plays'), "window_days": get_setting('session_days')}
    return {"phase": phase, "baseline": round(base, 2), "attempts": len(window),
            "distinct_maps": unique, "calibration_needed": get_setting('calibration_plays'), "calibration_maps_needed": get_setting('calibration_maps'), "trend": trend,
            "focus": focus, "message": message,
            "accuracy": round(reference_accuracy, 2) if reference_accuracy is not None else None,
            "challenge_unlocked": unlocked, "window": window, "session_window": session_window,
            "session": session, "window_plays": get_setting('reference_plays'), "window_days": get_setting('reference_days'),
            "evaluated_at": now.isoformat(), "method_version": 2, "settings_signature": reference_settings_signature(),
            "reference_method": f"Hasta {get_setting('reference_plays')} partidas de los últimos {get_setting('reference_days')} días; máximo {get_setting('max_attempts_per_map')} intentos por mapa. "
                f"El peso se reduce a la mitad cada {setting_text('half_life_days')} días. Las estimaciones por mapa corrigen las estrellas "
                f"según el resultado y recortan el {setting_text('trim_percent')} % del peso de cada extremo antes de promediar."}


def select_candidates(candidates, limit, used_songs, *, fill_online=False):
    """Reserve at most one slot for a downloadable map after all fit filters."""
    ordered = sorted(candidates, key=lambda item: (item[0], item[1]))
    local = [row for row in ordered if row[2].get("source") != "online" and row[2].get("local") is not False]
    online = [row for row in ordered if row not in local]
    chosen, keys, sets, songs = [], set(), set(), set(used_songs)

    def take(pool, count):
        added = 0
        for row in pool:
            _, key, beatmap = row
            song = (str(beatmap.get("artist", "")).casefold(), str(beatmap.get("title", "")).casefold())
            set_id = beatmap.get("set_id")
            if key in keys or song in songs or (set_id and set_id in sets) or (beatmap.get("benchmark") and any(row[2].get("benchmark") for row in chosen)):
                continue
            chosen.append(row)
            keys.add(key)
            songs.add(song)
            if set_id:
                sets.add(set_id)
            added += 1
            if added >= count:
                break

    if limit <= 0:
        return []
    take(local, max(1, limit - bool(online)))
    if len(chosen) < limit:
        take(online, 1)
    if len(chosen) < limit:
        take(local, limit - len(chosen))
    if fill_online and len(chosen) < limit:
        take(online, limit - len(chosen))
    return chosen


def apply_player_profile(profile, player):
    """Adapt this session while retaining the measured recent skill reference."""
    result = dict(profile)
    policy = (player or {}).get("progression", {})
    mode = policy.get("mode", "calibrate")
    practice_offset, challenge_increment = 0, get_setting('challenge_increment')
    if mode == "recover":
        practice_offset = -min(get_setting('challenge_increment'), get_setting('recovery_drop'))
    elif mode == "consolidate" and player.get("status") == "ready":
        practice_offset, challenge_increment = -get_setting('consolidate_increment'), get_setting('consolidate_increment')
    session_offset = max(-get_setting('recovery_drop'), min(0, number(profile.get("session", {}).get("adjustment"))))
    if session_offset < 0:
        mode, practice_offset = "recover", min(practice_offset, session_offset)
    base_unlocked = profile.get("base_challenge_unlocked", profile.get("challenge_unlocked", False))
    result["base_challenge_unlocked"] = base_unlocked
    result["challenge_unlocked"] = bool(base_unlocked and mode != "recover" and policy.get("allow_challenge", True))
    reasons = policy.get("reasons", [])
    result["training_adjustments"] = {
        "mode": mode, "practice_offset": practice_offset,
        "challenge_increment": challenge_increment,
        "reason": " ".join(reason for reason in reasons if isinstance(reason, str)),
    }
    priorities = (player or {}).get("priorities", [])
    focus = next((item for item in priorities if item.get("key") == policy.get("focus_key")), None)
    if focus and profile.get("phase") == "training":
        result["focus"] = focus.get("label") or profile.get("focus")
        result["message"] = focus.get("action") or profile.get("message")
    if session_offset < 0:
        result["focus"] = profile["session"]["focus"]
        result["message"] = profile["session"]["message"]
        result["training_adjustments"]["reason"] = f"La sesión reciente pide bajar {setting_text('recovery_drop')} ★ para recuperar el control."
    return result


def profile_tag_analysis(analysis, player):
    """Replace the existing tag focus; a correlated signal never adds a bonus."""
    if not analysis or not player:
        return analysis
    focus_key = player.get("progression", {}).get("focus_key")
    focus = next((item for item in player.get("priorities", []) if item.get("key") == focus_key), {})
    tag = focus.get("tag")
    supported = any(item.get("tag") == tag and item.get("status") == "practice"
                    and item.get("confidence") == "medium" and item.get("stats_scope") == "comparable"
                    and number(item.get("comparable_plays")) >= get_setting('profile_min_plays')
                    and number(item.get("comparable_maps")) >= get_setting('profile_min_maps')
                    and number(item.get("comparable_sessions")) >= get_setting('profile_min_sessions')
                    for item in analysis.get("items", []))
    return {**analysis, "focus_tag": tag} if tag and supported else analysis


def physical_reference(profile):
    window = profile.get("session_window", profile.get("window", []))
    comfortable = [p for p in window if p.get("passed") and number(p.get("accuracy")) >= get_setting('challenge_accuracy')][-5:]
    ref = comfortable or window[-5:]
    result = {}
    for field in ("bpm", "ar"):
        values = [number(p.get(field)) for p in ref if number(p.get(field)) > 0]
        result[field] = mean(values) if values else None
    return result


def physical_limits(profile):
    anchors = physical_reference(profile)
    # Duration is not a proxy for skill difficulty and must not close the pool
    # around the short maps already recommended to the player.
    return {"bpm": anchors["bpm"] + get_setting('bpm_margin') if anchors["bpm"] and get_setting('bpm_hard_limit') else None,
            "ar": anchors["ar"] + get_setting('ar_margin') if anchors["ar"] else None}


def recommend(catalog, profile, limit=3, tag_analysis=None, player_profile=None, stages=None, *, fill_online=False):
    base, window = profile["baseline"], profile.get("session_window", profile["window"])
    tag_analysis = profile_tag_analysis(tag_analysis, player_profile)
    adjustments = profile.get("training_adjustments", {})
    practice_target = max(.5, base + max(-get_setting('recovery_drop'), min(0, number(adjustments.get("practice_offset")))))
    challenge_increment = max(0, min(get_setting('challenge_increment'),
                                      number(adjustments.get("challenge_increment"), get_setting('challenge_increment'))))
    focus_key = (player_profile or {}).get("progression", {}).get("focus_key")
    focus = next((item for item in (player_profile or {}).get("priorities", []) if item.get("key") == focus_key), None)
    anchors = physical_reference(profile)
    limits = physical_limits(profile)
    def reference(field):
        return anchors.get(field)
    recent_keys = [map_key(p) for p in window[-4:]]
    used, used_songs, groups = set(), set(), []
    benchmark_used = False
    unlocked = profile["challenge_unlocked"]
    consolidation_reason = "Repetí resultados sólidos en mapas distintos de una dificultad que ya podés controlar."
    if profile.get("phase") == "calibrating":
        consolidation_reason = "Terminá la calibración jugando mapas cómodos y variados."
    if adjustments.get("mode") == "recover":
        consolidation_reason = "Hoy priorizá recuperar el control; tu rango ganado se conserva."
    practice_description = "Dedicá la mayor parte de la sesión a las metas de estos mapas."
    if focus:
        practice_description = focus.get("action") or practice_description
    if practice_target < base:
        difference = f"{base - practice_target:.2f}".replace(".", ",")
        practice_description += f" Esta práctica baja {difference} ★ respecto de tu referencia para trabajar el foco actual."
    steps = [("warmup", "Entrar en ritmo", max(.5, base - get_setting('warmup_offset')), "Empezá con un mapa cómodo para recuperar el ritmo."),
             ("practice", "Práctica principal", practice_target, practice_description),
             ("challenge", "Consolidar", practice_target if adjustments.get("mode") == "recover" else base,
              consolidation_reason)]
    for stage, label, target, goal in steps:
        if stages is not None and stage not in stages:
            continue
        effective_stage = "consolidate" if stage == "challenge" else stage
        candidates, challenges = [], []
        skill_margin = get_setting('comparable_star_band') if any(row.get('reference') is not None for row in (player_profile or {}).get('skill_levels', [])) else 0
        range_lower = max(.1, target - skill_margin - get_setting('star_tolerance_below'))
        range_upper = target + skill_margin + get_setting('star_tolerance_above') + (challenge_increment if stage == 'practice' and unlocked else 0)
        for m in catalog:
            key = str(m.get("key"))
            sr = number(m.get("stars"))
            personal_target = skill_target(m, player_profile, target, base)
            in_range = max(.1, personal_target - get_setting('star_tolerance_below')) - 1e-9 <= sr <= personal_target + get_setting('star_tolerance_above') + 1e-9
            challenge_target = personal_target + challenge_increment
            in_challenge = (stage == 'practice' and unlocked and not m.get('benchmark') and sr > personal_target + 1e-8
                            and challenge_target - get_setting('star_tolerance_below') <= sr <= challenge_target + get_setting('star_tolerance_above'))
            song = (str(m.get("artist", "")).casefold(), str(m.get("title", "")).casefold())
            if (m.get("mode", 0) != 0 or not duration_ok(m) or key in used or song in used_songs or sr <= 0
                    or (m.get('benchmark') and (stage == 'warmup' or benchmark_used))
                    or not (in_range or in_challenge)):
                continue
            # Keep simultaneous jumps in reading and speed bounded.
            if any(ceiling is not None and number(m.get(field)) > ceiling for field, ceiling in limits.items()):
                continue
            penalty = abs(sr - personal_target) * 5 + (.5 if key in recent_keys else 0)
            # Actual note density, when available, is a soft preference. BPM
            # alone never substitutes for tapping demand.
            densities = [number(p.get('note_density')) for p in window if number(p.get('note_density')) > 0]
            if densities and number(m.get('note_density')) > 0:
                penalty += min(.3, abs(number(m['note_density']) - mean(densities)) / 20)
            preferred = (player_profile or {}).get('training_skill')
            if stage == 'practice' and preferred and preferred in skills_for(m):
                penalty -= .4
            # A bounded preference helps keep warmups brief without excluding
            # longer maps or influencing practice and consolidation difficulty.
            warmup_seconds = get_setting("warmup_preferred_seconds")
            if stage == "warmup" and warmup_seconds:
                penalty += min(.2, max(0, number(m.get("length")) - warmup_seconds) / warmup_seconds * .2)
            if tag_analysis:
                from osu_coach.core.tag_analysis import tag_priority
                adjustment, _ = tag_priority(m, tag_analysis, effective_stage)
                penalty += adjustment
            candidate = {**m, 'training_target': round(personal_target, 2)}
            if in_range:
                candidates.append((penalty - (.5 if m.get('benchmark') else 0), key, candidate))
            if in_challenge:
                challenges.append((penalty + (abs(sr - challenge_target) - abs(sr - personal_target)) * 5, key,
                                   {**candidate, 'training_role': 'challenge', 'training_target': round(challenge_target, 2)}))
        maps = []
        chosen_sets = set()
        chosen = select_candidates(candidates, limit, used_songs, fill_online=fill_online)
        if challenges and limit:
            # At most one controlled challenge in main practice; retain the
            # third column for consolidation even when challenges are unlocked.
            first = select_candidates(challenges, 1, used_songs, fill_online=True)
            if first:
                special_song = (str(first[0][2].get('artist', '')).casefold(), str(first[0][2].get('title', '')).casefold())
                rest = [row for row in candidates if row[1] != first[0][1] and (not first[0][2].get('set_id') or row[2].get('set_id') != first[0][2].get('set_id'))]
                chosen = select_candidates(rest, limit - 1, used_songs | {special_song}, fill_online=fill_online) + first
        for _, key, m in chosen:
            if m.get('benchmark') and benchmark_used:
                continue
            set_id = m.get("set_id")
            song = (str(m.get("artist", "")).casefold(), str(m.get("title", "")).casefold())
            if (set_id and set_id in chosen_sets) or song in used_songs:
                continue
            if not m.get("play_conditions"):
                m = stamp_mods(m, play_context(profile["window"][-1]) if profile.get("window") else mod_context())
            result = {k: v for k, v in m.items() if k not in {"path"}}
            role = m.get('training_role') or effective_stage
            expected = expectation_for(m, profile, role, tag_analysis)
            expected = training_goal(expected, m, profile, role, focus if stage == 'practice' else None)
            if focus and stage == "practice":
                expected["focus"] = {key: focus[key] for key in ("key", "label", "action", "tag") if key in focus}
            labels = {'accuracy': f"≥{expected['accuracy_min']:g} % de precisión",
                      'misses': f"≤{expected['misses_max']} misses", 'combo': f"≥{expected['combo_min']}× combo"}
            personal_goal = "Completar · " + " · ".join(labels[k] for k in expected['required_keys'] if k in labels)
            result.update(reason=f"{number(m['stars']):.2f} ★, objetivo ajustado de {m['training_target']:.2f} ★ para este tipo de mapa.",
                          expectation=expected, training_role=expected['training_role'], goal=personal_goal + ".", **search_details(m))
            if m.get('benchmark'):
                benchmark_used = True
            if tag_analysis:
                from osu_coach.core.tag_analysis import tag_priority
                _, tag_reason = tag_priority(m, tag_analysis, effective_stage)
                if tag_reason:
                    result["reason"] += " " + tag_reason
            identifier = int(number(m.get("id")))
            result["url"] = f"https://osu.ppy.sh/beatmaps/{identifier}" if identifier > 0 else ""
            maps.append(result)
            used.add(key)
            used_songs.add(song)
            if set_id:
                chosen_sets.add(set_id)
            if len(maps) == limit:
                break
        query = f"stars>={range_lower:.2f} stars<={range_upper:.2f}"
        groups.append({"stage": stage, "label": label, "target": round(target, 2), "min_stars": range_lower, "max_stars": range_upper, "maps": maps,
                       "max_online": max(1, 3 - sum(m.get("source") != "online" and m.get("local") is not False for m in maps)) if fill_online else 1,
                       "description": goal,
                       "search_url": "https://osu.ppy.sh/beatmapsets?" + urlencode({"m": 0, "q": query})})
    return groups
