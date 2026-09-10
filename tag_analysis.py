"""Recent map-tag associations for osu!standard, without rank or historical scores.

Callers select the current player/client/mod profile and recent play window first.
Only explicit gameplay tags are interpreted. The allowlist follows the official
wiki (checked 2026-09-08): https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tags

Each map contributes its two latest attempts, with age weighting. Trends
require eight measurements across five maps and two sessions within +/- 0.5 stars.
These are map-level training heuristics, not identification of failed patterns.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
from evidence import recency_weight, session_ids, weighted_mean, setting_text
from settings import get_setting


MAX_ITEMS = 25

# Restricting full names also excludes similarly named tags for other rulesets.
TAG_NAMES = {
    "skillset/jumps": "Saltos",
    "skillset/streams": "Streams",
    "skillset/alt": "Alternancia",
    "skillset/tech": "Técnica",
    "skillset/precision": "Precisión de apuntado",
    "skillset/reading": "Lectura",
    "skillset/gimmick": "Mecánicas especiales",
    "jumps/sharp": "Saltos con ángulos cerrados",
    "jumps/wide": "Saltos con ángulos abiertos",
    "jumps/linear": "Saltos en línea",
    "jumps/triangles": "Saltos en triángulos",
    "jumps/squares": "Saltos en cuadrados",
    "jumps/stars": "Saltos en estrellas",
    "jumps/back and forth": "Saltos de ida y vuelta",
    "jumps/freeform": "Saltos de forma libre",
    "jumps/cross-screen": "Saltos de pantalla completa",
    "jumps/stamina": "Resistencia en saltos",
    "streams/doubles": "Dobles",
    "streams/quads": "Grupos de cuatro notas",
    "streams/bursts": "Ráfagas",
    "streams/stamina": "Resistencia en streams",
    "streams/speed": "Velocidad en streams",
    "streams/flow aim": "Apuntado continuo",
    "streams/spaced streams": "Streams espaciados",
    "streams/cutstreams": "Streams con cambios de espaciado",
    "tech/slider tech": "Sliders técnicos",
    "tech/aim control": "Control del apuntado",
    "tech/finger control": "Control de los dedos",
    "reading/overlaps": "Lectura de notas superpuestas",
    "reading/perfect stacks": "Lectura de notas apiladas",
    "reading/visually dense": "Lectura de alta densidad",
    "sliders/low sv": "Sliders lentos",
    "sliders/high sv": "Sliders rápidos",
    "sliders/complex sv": "Cambios de velocidad en sliders",
    "sliders/complex slidershapes": "Sliders de formas complejas",
}

LABELS = {
    "strength": "Te está yendo bien",
    "practice": "Para practicar",
    "learning": "Reunir evidencia",
    "explore": "Por explorar",
}


def _number(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _canonical(value):
    if not isinstance(value, str):
        return None
    name = " ".join(value.casefold().strip().split())
    return name if name in TAG_NAMES else None


def skill_tags(beatmap):
    """Return unique explicit, standard-compatible gameplay tag dictionaries.

    Free-form mapper metadata such as artist names and anime titles is ignored.
    A mapper/manual entry must use an exact canonical gameplay tag to qualify.
    Community votes below the site's display threshold do not qualify either.
    """
    if beatmap.get("mode", 0) != 0:
        return []
    raw = beatmap.get("tags") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (tuple, list)):
        return []
    selected = {}
    source_order = {"mapper": 0, "community": 1, "manual": 2}
    for value in raw:
        entry = value if isinstance(value, dict) else {"name": value, "source": "mapper"}
        name = _canonical(entry.get("name"))
        if not name:
            continue
        source = entry.get("source", "mapper")
        if source not in source_order:
            continue
        count = _number(entry.get("count"))
        if source == "community" and count is not None and count < 5:
            continue
        normalized = {"name": name, "id": entry.get("id"), "source": source}
        if name not in selected or source_order[source] > source_order[selected[name]["source"]]:
            selected[name] = normalized
    return [selected[name] for name in sorted(selected)]


def _played_at(play):
    value = play.get("played_at")
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _map_key(value):
    return str(value.get("beatmap_key") or value.get("key") or value.get("beatmap_id") or "")


def _rates(play):
    completion = _number(play.get("completion"))
    passed = (False if play.get("passed") is False else
              completion >= .98 if play.get("passed") is True and completion is not None and 0 <= completion <= 1 else None)
    judged = _number(play.get("judged_objects"))
    if judged is None and passed is True:
        judged = _number(play.get("object_count"))
    misses = _number(play.get("misses"))
    valid_counts = (judged is not None and judged > 0 and judged.is_integer()
                    and misses is not None and 0 <= misses <= judged and misses.is_integer())
    miss_rate = misses / judged if valid_counts else None
    maximum, count = _number(play.get("map_max_combo")), _number(play.get("max_combo"))
    combo = (count / maximum if maximum is not None and maximum > 0 and maximum.is_integer()
             and count is not None and 0 <= count <= maximum and count.is_integer() else None)
    accuracy = _number(play.get("accuracy"))
    return {"accuracy": accuracy if accuracy is not None and 0 <= accuracy <= 100 else None,
            "miss_rate": miss_rate, "pass_rate": float(passed) if passed is not None else None, "combo": combo}


def _summarize(rows, now):
    """Two attempts on one map may refine that map, never double its weight."""
    grouped = defaultdict(list)
    for key, play in rows:
        grouped[key].append((_rates(play), recency_weight(play, now)))
    per_map = []
    for attempts in grouped.values():
        item = {}
        for field in ("accuracy", "miss_rate", "pass_rate", "combo"):
            values = [(rates[field], weight) for rates, weight in attempts if rates[field] is not None]
            item[field] = ((weighted_mean(values), max(weight for _, weight in values)) if values else None)
        per_map.append(item)
    if not per_map:
        return {"accuracy": None, "miss_rate": None, "pass_rate": None,
                "combo": None, "distinct_maps": 0}
    result = {}
    for field in ("accuracy", "miss_rate", "pass_rate", "combo"):
        values = [item[field] for item in per_map if item[field] is not None]
        result[field] = weighted_mean(values) if values else None
    result["distinct_maps"] = len(per_map)
    return result


def _sessions(rows, assignments):
    return len({assignments[play.get("id")] for _, play in rows if play.get("id") in assignments})


def analyze_tags(plays, catalog, baseline, now=None):
    """Describe recent performance on tagged maps, with conservative evidence.

    ``plays`` must already belong to one player/client/mod profile and the current
    rolling window/reset. Stars on these plays must include the active mods.
    Out-of-band results remain visible but cannot create a strength or weakness.
    Summary rates are fractions; accuracy is a percentage from 0 to 100.
    """
    base = _number(baseline, 0)
    assignments = session_ids(plays)
    now = _played_at({"played_at": now}) if now is not None else datetime.now(timezone.utc)
    lower, upper = max(.1, base - get_setting('comparable_star_band')), base + get_setting('comparable_star_band')
    by_key, by_id, known, available, nearby = {}, {}, {}, Counter(), Counter()
    for beatmap in catalog:
        if beatmap.get("mode", 0) != 0:
            continue
        if beatmap.get("key"):
            by_key[str(beatmap["key"])] = beatmap
        identifier = _number(beatmap.get("id"), 0)
        if identifier > 0:
            by_id[str(int(identifier))] = beatmap
        for tag in skill_tags(beatmap):
            known.setdefault(tag["name"], set()).add(tag["source"])
            available[tag["name"]] += 1
            stars = _number(beatmap.get("stars"), 0)
            if lower <= stars <= upper:
                nearby[tag["name"]] += 1

    tagged_count, total_count, selected, counts, seen = 0, 0, [], Counter(), set()
    for play in sorted(plays, key=_played_at, reverse=True):
        stars = _number(play.get("stars"))
        if (play.get("mode", 0) != 0 or play.get("excluded") or play.get("needs_confirmation")
                or _played_at(play) == datetime.min.replace(tzinfo=timezone.utc)
                or stars is None or stars <= 0):
            continue
        identifier = play.get("id")
        if identifier is not None:
            if identifier in seen:
                continue
            seen.add(identifier)
        key = _map_key(play)
        beatmap = by_key.get(key)
        if beatmap is None:
            numeric_id = _number(play.get("beatmap_id"), 0)
            beatmap = by_id.get(str(int(numeric_id))) if numeric_id > 0 else None
        tags = skill_tags(beatmap if beatmap is not None else play)
        if beatmap is not None:
            key = str(beatmap.get("key") or play.get("beatmap_id") or key)
        total_count += 1
        tagged_count += bool(tags)
        # An unidentified map cannot establish independent evidence.
        if not key or counts[key] >= get_setting('max_attempts_per_map'):
            continue
        counts[key] += 1
        for tag in tags:
            known.setdefault(tag["name"], set()).add(tag["source"])
        selected.append((key, play, tags))

    all_rows, comparable, cooccurs = defaultdict(list), defaultdict(list), set()
    for key, play, tags in selected:
        for tag in tags:
            name = tag["name"]
            all_rows[name].append((key, play))
            if lower <= _number(play["stars"]) <= upper:
                comparable[name].append((key, play))
            if len(tags) > 1:
                cooccurs.add(name)

    items = []
    for name, sources in known.items():
        rows, current = all_rows[name], comparable[name]
        stats = _summarize(current or rows, now)
        valid = [(key, play) for key, play in current
                 if all(_rates(play)[field] is not None for field in ("accuracy", "miss_rate", "pass_rate"))]
        current_maps = len({key for key, _ in valid})
        current_sessions = _sessions(valid, assignments)
        enough = len(valid) >= get_setting('profile_min_plays') and current_maps >= get_setting('profile_min_maps') and current_sessions >= get_setting('profile_min_sessions')
        combo_rows = [(key, play) for key, play in valid if _rates(play)["combo"] is not None]
        combo_enough = (len(combo_rows) >= get_setting('profile_min_plays') and len({key for key, _ in combo_rows}) >= get_setting('profile_min_maps')
                        and _sessions(combo_rows, assignments) >= get_setting('profile_min_sessions'))
        state = "learning" if rows else "explore"
        if enough:
            if (stats["accuracy"] >= get_setting('strong_accuracy') - 1e-8 and stats["miss_rate"] <= get_setting('strong_miss_percent') / 100 + 1e-10
                    and stats["pass_rate"] >= 1 - 1e-8 and combo_enough and stats["combo"] >= get_setting('strong_combo_percent') / 100 - 1e-8):
                state = "strength"
            elif stats["accuracy"] < 96 - 1e-8 or stats["miss_rate"] > .01 + 1e-10 or stats["pass_rate"] < .8 - 1e-8:
                state = "practice"
        scope = f"{lower:.2f} a {upper:.2f} ★".replace(".", ",")
        if not rows:
            message = f"Disponible en {available[name]} mapas; todavía faltan partidas con esta etiqueta."
        elif not enough:
            play_word = "medición válida" if len(valid) == 1 else "mediciones válidas"
            map_word = "mapa" if current_maps == 1 else "mapas"
            message = (f"Hay {len(valid)} {play_word} en {current_maps} {map_word} y {current_sessions} sesiones dentro de {scope}. "
                       f"La tendencia requiere {get_setting('profile_min_plays')} mediciones en {get_setting('profile_min_maps')} mapas y {get_setting('profile_min_sessions')} sesiones de ese rango.")
        elif state == "strength":
            message = f"Resultados sólidos en mapas con esta etiqueta, dentro de {scope}."
        elif state == "practice":
            message = f"Conviene practicar mapas con esta etiqueta en {scope}, buscando más control."
        else:
            message = f"Resultados intermedios dentro de {scope}; reuní más partidas en mapas distintos."
        outside = len(rows) - len(current)
        if outside:
            message += (" 1 partida fuera del rango queda fuera de la tendencia." if outside == 1
                        else f" {outside} partidas fuera del rango quedan fuera de la tendencia.")
        if rows and not current:
            message += " Las cifras muestran esos resultados solo como referencia."
        severity = 0
        if state == "practice":
            severity = (max(0, get_setting('strong_accuracy') - stats["accuracy"]) / 10 + stats["miss_rate"] * 8
                        + (1 - stats["pass_rate"]) * .8)
        # Shrink priorities toward neutral when there are few independent maps.
        evidence_weight = current_maps / (current_maps + 2)
        items.append({
            "tag": name, "name": TAG_NAMES[name], "category": name.split("/")[0],
            "status": state, "label": LABELS[state], "plays": len(rows),
            "distinct_maps": len({key for key, _ in rows}),
            "comparable_plays": len(valid), "comparable_maps": current_maps,
            "sessions": _sessions(rows, assignments), "comparable_sessions": current_sessions,
            "outside_band_plays": outside,
            "accuracy": round(stats["accuracy"], 2) if stats["accuracy"] is not None else None,
            "miss_rate": round(stats["miss_rate"], 5) if stats["miss_rate"] is not None else None,
            "pass_rate": round(stats["pass_rate"], 4) if stats["pass_rate"] is not None else None,
            "stats_scope": "comparable" if current else ("outside_band" if rows else "none"),
            "confidence": "medium" if enough else "low", "message": message,
            "source": next(iter(sources)) if len(sources) == 1 else "mixed",
            "sources": sorted(sources), "cooccurs": name in cooccurs,
            "available_maps": available[name], "nearby_maps": nearby[name],
            "evidence_weight": round(evidence_weight, 4),
            "priority": round(min(2, severity) * evidence_weight, 4),
        })

    order = {"practice": 0, "strength": 1, "learning": 2, "explore": 3}
    items.sort(key=lambda item: (order[item["status"]], -item["priority"],
                                 -item["comparable_maps"], -item["plays"],
                                 -item["nearby_maps"], item["tag"]))
    focus = next((item for item in items if item["status"] == "practice"), None)
    if not tagged_count:
        message = "Las partidas recientes todavía carecen de etiquetas de habilidades reconocidas."
    elif focus:
        message = f"Práctica sugerida: {focus['name']}, manteniendo una dificultad manejable."
    else:
        message = "Jugá distintos mapas etiquetados de tu rango para conocer tus tendencias actuales."
    return {
        "items": items[:MAX_ITEMS], "focus_tag": focus["tag"] if focus else None,
        "focus_name": focus["name"] if focus else None,
        "tagged_plays": tagged_count, "total_plays": total_count,
        "message": message, "band": {"min": round(lower, 2), "max": round(upper, 2)},
        "evidence": {"days": get_setting('reference_days'), "max_plays": get_setting('reference_plays'), "min_plays": get_setting('profile_min_plays'), "min_maps": get_setting('profile_min_maps'),
                     "min_sessions": get_setting('profile_min_sessions'), "sessions": len(set(assignments.values())),
                     "comparable_sessions": _sessions([(key, play) for key, play, _ in selected
                                                       if lower <= _number(play["stars"]) <= upper], assignments)},
        "method": (f"Hasta {get_setting('reference_plays')} partidas de los últimos {get_setting('reference_days')} días del perfil activo. Máximo {get_setting('max_attempts_per_map')} intentos por mapa. "
                   f"Media ponderada por recencia; cada mapa pesa según su intento más reciente, con semivida de {setting_text('half_life_days')} días. "
                   f"Tendencias desde {get_setting('profile_min_plays')} mediciones en {get_setting('profile_min_maps')} mapas y {get_setting('profile_min_sessions')} sesiones dentro de ±{setting_text('comparable_star_band')} ★ de tu referencia. "
                   f"Las sesiones se separan por al menos {setting_text('session_gap_minutes')} minutos y se identifican antes de filtrar etiquetas. "
                   f"Buen resultado: {setting_text('strong_accuracy')} % de precisión, hasta {setting_text('strong_miss_percent')} % de misses, mapas completos y {setting_text('strong_combo_percent')} % de combo "
                   "con mediciones suficientes. Practicar: menos de 96 %, más de 1 % de misses o menos de 80 % completados."),
        "cooccurrence_message": ("Cada partida cuenta para todas las etiquetas del mapa. "
                                 "Las tendencias describen resultados en mapas con esas etiquetas; "
                                 "el análisis de fallos puntuales requiere estudiar los patrones de la partida."),
    }


def tag_priority(beatmap, analysis, stage):
    """Return a small additive ranking cost, after the engine's safety filters.

    Matching a known practice focus or a warmup strength breaks close ties.
    Missing tags are neutral; tags never change star/BPM/AR/length eligibility.
    Multiple matching tags cannot accumulate an unlimited bonus.
    """
    names = {tag["name"] for tag in skill_tags(beatmap)}
    if not names or not analysis:
        return 0.0, None
    entries = {item["tag"]: item for item in analysis.get("items", [])
               if item.get("confidence") == "medium" and item.get("stats_scope") == "comparable"
               and _number(item.get("comparable_plays"), 0) >= get_setting('profile_min_plays')
               and _number(item.get("comparable_maps"), 0) >= get_setting('profile_min_maps')
               and _number(item.get("comparable_sessions"), 0) >= get_setting('profile_min_sessions')}
    focus = entries.get(analysis.get("focus_tag"))
    if stage == "practice" and focus and focus.get("status") == "practice" and focus["tag"] in names:
        weight = max(0, min(1, _number(focus.get("evidence_weight"), .5)))
        return round(-.4 * weight, 4), f"Para practicar {focus['name'].lower()} según tus partidas recientes."
    if stage in {"warmup", "consolidate", "consolidation"}:
        strengths = [item for name, item in entries.items() if name in names and item.get("status") == "strength"]
        if strengths:
            strongest = max(strengths, key=lambda item: (_number(item.get("evidence_weight"), .5), item["tag"]))
            weight = max(0, min(1, _number(strongest.get("evidence_weight"), .5)))
            return round(-.3 * weight, 4), f"Te está yendo bien en mapas con {strongest['name'].lower()}."
    return 0.0, None
