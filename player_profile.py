"""Observable recent strengths and practice priorities for one active profile.

The caller supplies the reset-aware thirty-day/hundred-play player/client/mod
window. Conclusions need eight observations on five comparable maps in two sessions.
Map tags remain associations; accuracy never becomes an inferred aim skill.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math

from tag_analysis import TAG_NAMES
from evidence import recency_weight, session_ids, weighted_mean, setting_text
from settings import get_setting




def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _count(value):
    value = _number(value)
    return int(value) if value is not None and value >= 0 and value.is_integer() else None


def _time(play):
    value = play.get("played_at")
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value, timezone.utc)
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _key(play):
    return str(play.get("beatmap_key") or play.get("key") or play.get("beatmap_id") or "")


def _completed(play):
    completion = _number(play.get("completion"))
    if play.get("passed") is False or (completion is not None and completion < .98):
        return False
    if play.get("passed") is True:
        return True
    return None


def _select(window, baseline):
    all_plays, comparable, seen, counts = [], [], set(), Counter()
    for play in sorted(window, key=_time, reverse=True):
        stars, key = _number(play.get("stars")), _key(play)
        if (not key or stars is None or stars <= 0 or play.get("mode", 0) != 0
                or _time(play) == datetime.min.replace(tzinfo=timezone.utc)
                or play.get("excluded") or play.get("needs_confirmation")):
            continue
        identifier = play.get("id")
        if identifier is not None:
            if identifier in seen:
                continue
            seen.add(identifier)
        all_plays.append(play)
        # Count the latest attempts before checking individual measurements;
        # absent telemetry must not resurrect older successful attempts.
        if abs(stars - baseline) <= get_setting('comparable_star_band') + 1e-8 and counts[key] < get_setting('max_attempts_per_map'):
            comparable.append(play)
            counts[key] += 1
    return all_plays, comparable


def _values(plays, key):
    rows = []
    for play in plays:
        completed = _completed(play)
        value = None
        if key == "completion" and completed is not None:
            value = 100.0 if completed else 0.0
        elif key == "accuracy" and completed is True:
            accuracy = _number(play.get("accuracy"))
            if accuracy is not None and 0 <= accuracy <= 100:
                value = accuracy
        elif key == "misses":
            misses, judged = _count(play.get("misses")), _count(play.get("judged_objects"))
            if judged is None and completed is True:
                judged = _count(play.get("object_count"))
            if misses is not None and judged is not None and judged > 0 and misses <= judged:
                value = misses / judged * 100
        elif key == "combo" and completed is True:
            combo, maximum = _count(play.get("max_combo")), _count(play.get("map_max_combo"))
            if combo is not None and maximum is not None and maximum > 0 and combo <= maximum:
                value = combo / maximum * 100
        if value is not None:
            rows.append((_key(play), value, play))
    return rows


def _per_map(rows, now):
    grouped = defaultdict(list)
    for key, value, play in rows:
        grouped[key].append((value, recency_weight(play, now)))
    return [(weighted_mean(values), max(weight for _, weight in values)) for values in grouped.values()]


def _sessions(plays, assignments):
    return len({assignments[play.get("id")] for play in plays if play.get("id") in assignments})


def _evidence(samples, maps, sessions, kind="datos"):
    word = "mapa" if maps == 1 else "mapas"
    if kind == "partidas completas":
        kind = "partida completa" if samples == 1 else kind
    elif kind == "datos":
        kind = "dato" if samples == 1 else kind
    session_word = "sesión" if sessions == 1 else "sesiones"
    return f"{samples} {kind} en {maps} {word} y {sessions} {session_word} dentro de ±{setting_text('comparable_star_band')} ★ de tu referencia."


DIMENSIONS = {
    "accuracy": ("Precisión", "Trabajá hacia el 97 % de precisión; seguí la meta de cada mapa como próximo paso."),
    "misses": ("Control de misses", "Buscá como máximo 1 miss cada 200 objetos juzgados."),
    "combo": ("Combo", "Trabajá en sostener el combo para acercarte al 80 %; usá la meta de cada mapa como próximo paso."),
    "completion": ("Completar mapas", "Elegí un mapa más cómodo y priorizá terminarlo con control."),
    "consistency": ("Consistencia entre mapas", "Repetí resultados de precisión parecidos en canciones distintas del mismo rango."),
}


def _dimension(key, rows, assignments, now, mean_accuracy=None):
    values = _per_map(rows, now)
    samples, maps = len(rows), len(values)
    sessions = _sessions([row[2] for row in rows], assignments)
    average = weighted_mean(values) if values else None
    value = (math.sqrt(weighted_mean([((item - average) ** 2, weight) for item, weight in values]))
             if key == "consistency" and len(values) >= 2
             else average if key != "consistency" else None)
    enough = samples >= get_setting('profile_min_plays') and maps >= get_setting('profile_min_maps') and sessions >= get_setting('profile_min_sessions') and value is not None
    status = "learning"
    if enough:
        if key == "accuracy":
            status = "strength" if value >= get_setting('strong_accuracy') - 1e-8 else "practice" if value < 96 - 1e-8 else "steady"
        elif key == "misses":
            status = "strength" if value <= get_setting('strong_miss_percent') + 1e-8 else "practice" if value > 1 + 1e-8 else "steady"
        elif key == "combo":
            status = "strength" if value >= get_setting('strong_combo_percent') - 1e-8 else "practice" if value < 65 - 1e-8 else "steady"
        elif key == "completion":
            status = "strength" if value >= 100 - 1e-8 else "practice" if value < 80 else "steady"
        else:
            status = ("practice" if value > 2.5 else "strength"
                      if value <= 1 and mean_accuracy is not None and mean_accuracy >= 96 else "steady")
    label, action = DIMENSIONS[key]
    action = {
        "accuracy": f"Trabajá hacia el {setting_text('strong_accuracy')} % de precisión; seguí la meta de cada mapa como próximo paso.",
        "misses": f"Buscá como máximo {setting_text('strong_miss_percent')} % de misses sobre objetos juzgados.",
        "combo": f"Trabajá en sostener el combo para acercarte al {setting_text('strong_combo_percent')} %; usá la meta de cada mapa como próximo paso.",
    }.get(key, action)
    evidence = _evidence(samples, maps, sessions, "partidas completas" if key in {"accuracy", "combo", "consistency"} else "datos")
    if key == "consistency":
        evidence += " Dispersión: desviación estándar de la precisión media de cada mapa, en puntos porcentuales."
    if not enough:
        action = f"Reuní {get_setting('profile_min_plays')} partidas con este dato en al menos {get_setting('profile_min_maps')} mapas del rango y {get_setting('profile_min_sessions')} sesiones para definir una tendencia."
    elif status == "strength":
        action = {
            "accuracy": f"Mantené al menos {setting_text('strong_accuracy')} % de precisión al probar otros mapas del rango.",
            "misses": "Mantené esta proporción baja de misses al variar las canciones.",
            "combo": f"Mantené al menos el {setting_text('strong_combo_percent')} % del combo al practicar mapas distintos.",
            "completion": "Usá esta regularidad para practicar otros mapas de dificultad cercana.",
            "consistency": "Sostené resultados parecidos al cambiar de canción.",
        }[key]
    return {"key": key, "label": label, "status": status,
            "confidence": "medium" if enough else "low", "value": round(value, 2) if value is not None else None,
            "unit": "pp" if key == "consistency" else "%", "evidence": evidence,
            "action": action, "samples": samples, "distinct_maps": maps, "sessions": sessions}


def _tags(analysis):
    findings, unknown, observed_enough = [], [], False
    for item in (analysis or {}).get("items", []):
        tag = item.get("tag")
        if tag not in TAG_NAMES:
            continue
        samples, maps = _count(item.get("comparable_plays")), _count(item.get("comparable_maps"))
        sessions = _count(item.get("comparable_sessions"))
        enough = (item.get("confidence") == "medium" and item.get("stats_scope") == "comparable"
                  and samples is not None and samples >= get_setting('profile_min_plays') and maps is not None and maps >= get_setting('profile_min_maps')
                  and sessions is not None and sessions >= get_setting('profile_min_sessions'))
        if not enough:
            if item.get("plays", 0):
                unknown.append(f"{TAG_NAMES[tag]}: faltan resultados comparables para definir una fortaleza o un aspecto a practicar.")
            continue
        observed_enough = True
        if item.get("status") not in {"strength", "practice"}:
            continue
        status, label = item["status"], f"Mapas con {TAG_NAMES[tag].lower()}"
        action = (f"Practicá mapas con {TAG_NAMES[tag].lower()} cerca de tu dificultad actual."
                  if status == "practice" else f"Usá mapas con {TAG_NAMES[tag].lower()} para entrar en ritmo.")
        evidence = _evidence(samples, maps, sessions)
        if item.get("cooccurs"):
            evidence += " Estas partidas también cuentan para otras etiquetas del mapa."
        findings.append({"key": "tag:" + tag, "tag": tag, "label": label,
                         "status": status, "confidence": "medium", "value": _number(item.get("accuracy")),
                         "unit": "%", "evidence": evidence, "action": action,
                         "samples": samples, "distinct_maps": maps, "sessions": sessions})
    return findings, unknown, observed_enough


def build_player_profile(profile, tag_analysis=None):
    """Describe current observations and suggest a bounded progression policy."""
    baseline = _number(profile.get("baseline"))
    baseline = baseline if baseline is not None and baseline > 0 else get_setting('initial_stars')
    all_plays, comparable = _select(profile.get("window") or [], baseline)
    assignments = session_ids(profile.get("window") or [])
    now = _time({"played_at": profile.get("evaluated_at")})
    if now == datetime.min.replace(tzinfo=timezone.utc):
        now = datetime.now(timezone.utc)
    maps = len({_key(play) for play in comparable})
    sessions = _sessions(comparable, assignments)
    ready = len(comparable) >= get_setting('profile_min_plays') and maps >= get_setting('profile_min_maps') and sessions >= get_setting('profile_min_sessions')
    accuracy_rows = _values(comparable, "accuracy")
    accuracy_means = _per_map(accuracy_rows, now)
    dimensions = [_dimension(key, accuracy_rows if key in {"accuracy", "consistency"} else _values(comparable, key),
                             assignments, now, weighted_mean(accuracy_means) if accuracy_means else None)
                  for key in DIMENSIONS]
    by_key = {item["key"]: item for item in dimensions}
    tag_findings, tag_unknowns, tag_evidence = _tags(tag_analysis)
    findings = dimensions + tag_findings
    strengths = [dict(item) for item in findings if item["status"] == "strength"]
    weaknesses = [dict(item) for item in findings if item["status"] == "practice"]
    order = {"completion": 0, "misses": 1, "accuracy": 2, "combo": 3, "consistency": 4}
    weaknesses.sort(key=lambda item: (order.get(item["key"], 5), item["key"]))
    steady_findings = [item for item in dimensions
                       if item["status"] == "steady" and item["key"] in {"accuracy", "misses", "combo", "consistency"}]
    # Once accuracy/miss control is manageable, a sub-80 % combo is a concrete
    # next target. It remains an intermediate result, never a diagnosed weakness.
    if (by_key["combo"]["status"] == "steady" and by_key["accuracy"]["value"] is not None
            and by_key["accuracy"]["value"] >= 96 and by_key["misses"]["value"] is not None
            and by_key["misses"]["value"] <= 1):
        steady_findings = [by_key["combo"]] + [item for item in steady_findings if item["key"] != "combo"]
    priority_findings = weaknesses or steady_findings[:1]
    priorities = [{"key": item["key"], "label": item["label"],
                   "reason": ("Aspecto para practicar según tus partidas recientes. " if item["status"] == "practice"
                              else "Resultado intermedio: podés afianzarlo con la siguiente meta. ") + item["evidence"],
                   "action": item["action"], **({"tag": item["tag"]} if "tag" in item else {})}
                  for item in priority_findings]
    unknowns = [f"{item['label']}: la tendencia requiere al menos {get_setting('profile_min_plays')} datos en {get_setting('profile_min_maps')} mapas comparables y {get_setting('profile_min_sessions')} sesiones."
                for item in dimensions if item["status"] == "learning"]
    unknowns.extend(tag_unknowns)
    if not tag_evidence:
        unknowns.append("Los tipos de patrones se describen cuando hay etiquetas de habilidades con evidencia suficiente.")

    session_plays, _ = _select(profile.get("session_window", profile.get("window")) or [], baseline)
    latest_comparable = [play for play in session_plays if abs(_number(play["stars"]) - baseline) <= get_setting('comparable_star_band') + 1e-8][:3]
    recent_failures = sum(_completed(play) is False for play in latest_comparable)
    recover = recent_failures >= 2
    allowed = bool(profile.get("challenge_unlocked"))
    focus_key, focus_label = ((priorities[0]["key"], priorities[0]["label"]) if priorities else ("calibration", "Reunir evidencia"))
    if recover:
        mode, allowed, focus_key, focus_label = "recover", False, "completion", DIMENSIONS["completion"][0]
        reasons = [f"La práctica principal baja hasta {min(get_setting('challenge_increment'), get_setting('recovery_drop')):g} ★ y el desafío queda en pausa para recuperar el control."]
        reasons.append("Hay al menos 2 intentos incompletos en los últimos 3 intentos comparables de la ventana de sesión.")
        if not any(item["key"] == "completion" for item in priorities):
            priorities.insert(0, {"key": "completion", "label": focus_label,
                                  "reason": reasons[-1], "action": DIMENSIONS["completion"][1]})
    elif not ready or not any(by_key[key]["confidence"] == "medium" for key in ("accuracy", "misses", "combo")):
        mode = "calibrate"
        reasons = ["La progresión conserva sus límites actuales mientras se reúnen datos comparables."]
    elif weaknesses:
        mode = "consolidate"
        reasons = [f"La práctica principal baja hasta {setting_text('consolidate_increment', 2)} ★ para trabajar estas prioridades.",
                   f"Si el desafío ya está habilitado, el paso se limita a +{setting_text('consolidate_increment', 2)} ★."]
    elif (all(by_key[key]["status"] == "strength" for key in ("accuracy", "misses", "completion"))
          and by_key["combo"]["status"] in {"strength", "learning"}):
        mode, focus_key, focus_label = "advance", "consistency", "Sostener resultados en otros mapas"
        reasons = ["La progresión mantiene sus pasos habituales al sostener buenos resultados en mapas distintos."]
        if not priorities:
            priorities.append({"key": focus_key, "label": focus_label, "reason": reasons[0],
                               "action": "Sostené tus buenos resultados al cambiar de canción, siguiendo la meta de cada mapa."})
    else:
        mode = "consolidate"
        reasons = [f"La práctica principal baja hasta {setting_text('consolidate_increment', 2)} ★ para afianzar resultados intermedios.",
                   f"Si el desafío ya está habilitado, el paso se limita a +{setting_text('consolidate_increment', 2)} ★."]

    status = "ready" if ready else "learning"
    if not ready:
        summary = "El perfil se está formando con tus partidas recientes en mapas cercanos a tu dificultad actual."
    elif recover:
        summary = "Tu prioridad actual es terminar mapas con más control antes de aumentar la dificultad."
    elif weaknesses:
        summary = f"Tu prioridad actual es practicar {weaknesses[0]['label'].lower()} en una dificultad manejable."
    elif priorities:
        summary = f"Podés seguir mejorando {priorities[0]['label'].lower()} para afianzar tu nivel actual."
    elif strengths:
        summary = "Tus resultados actuales permiten sostener una progresión gradual en mapas distintos."
    else:
        summary = "Hay partidas suficientes en el rango; faltan mediciones para definir algunas tendencias."
    return {"status": status, "summary": summary,
            "evidence": {"plays": len(all_plays), "distinct_maps": len({_key(play) for play in all_plays}),
                         "comparable_plays": len(comparable), "comparable_maps": maps,
                         "sessions": _sessions(all_plays, assignments), "comparable_sessions": sessions,
                         "min_plays": get_setting('profile_min_plays'), "min_maps": get_setting('profile_min_maps'), "min_sessions": get_setting('profile_min_sessions'),
                         "days": get_setting('reference_days'), "max_plays": get_setting('reference_plays'), "max_attempts_per_map": get_setting('max_attempts_per_map'),
                         "star_band": {"min": round(max(.1, baseline - get_setting('comparable_star_band')), 2), "max": round(baseline + get_setting('comparable_star_band'), 2)}},
            "dimensions": dimensions, "strengths": strengths, "weaknesses": weaknesses,
            "priorities": priorities, "unknowns": unknowns,
            "progression": {"mode": mode, "focus_key": focus_key, "focus_label": focus_label,
                            "reasons": reasons, "allow_challenge": allowed},
            "method": (f"Hasta {get_setting('reference_plays')} partidas de los últimos {get_setting('reference_days')} días del perfil activo. Hasta {get_setting('max_attempts_per_map')} intentos recientes por mapa dentro de ±{setting_text('comparable_star_band')} ★. "
                       f"La recencia pesa con una semivida de {setting_text('half_life_days')} días; cada mapa aporta una media y el peso de su intento más reciente. "
                       f"Las conclusiones requieren {get_setting('profile_min_plays')} mediciones válidas en {get_setting('profile_min_maps')} mapas y {get_setting('profile_min_sessions')} sesiones, separadas por al menos {setting_text('session_gap_minutes')} minutos. "
                       "La recuperación inmediata usa solamente los últimos intentos comparables de la ventana de sesión."),
            "limitations": "Las tendencias de tags describen resultados en mapas con esas etiquetas. El análisis de fallos puntuales requiere estudiar los patrones de la partida."}
