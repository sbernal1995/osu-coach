"""Observed skill references, spaced benchmarks and like-for-like progress.

These are transparent training heuristics, not validated skill diagnoses.
Historical rank/PP are never inputs. Accepted results remain the source of truth.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
import math

from osu_coach.core.grades import target_grade
from osu_coach.core.evidence import play_time, session_ids, recency_weight, weighted_mean
from osu_coach.core.mod_policy import conditions_match, play_context, identity, context
from osu_coach.core.quest_rules import _same_map
from osu_coach.core.tag_analysis import skill_tags
from osu_coach.settings import get_setting

MODEL_VERSION = 1
SKILLS = {
    "aim": ("Apuntado y saltos", ("skillset/jumps", "skillset/precision", "jumps/", "tech/aim control")),
    "tapping": ("Pulsaciones y streams", ("skillset/streams", "streams/", "skillset/alt")),
    "finger_control": ("Control de dedos", ("tech/finger control", "skillset/alt", "streams/doubles", "streams/quads")),
    "reading": ("Lectura", ("skillset/reading", "reading/")),
    "tech": ("Técnica y sliders", ("skillset/tech", "tech/slider tech", "sliders/")),
    "stamina": ("Resistencia", ("streams/stamina", "jumps/stamina")),
}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def skills_for(beatmap):
    names = [item['name'] for item in skill_tags(beatmap)]
    return [key for key, (_, tags) in SKILLS.items()
            if any(name == tag or (tag.endswith('/') and name.startswith(tag)) for name in names for tag in tags)]


def valid(play):
    return (not play.get('excluded') and not play.get('needs_confirmation')
            and play.get('mode', 0) == 0 and play.get('id') is not None and play_time(play) is not None)


def completed(play):
    return play.get('passed') is True and (number(play.get('completion')) or 0) >= .98


def conditions(beatmap):
    try:
        return beatmap.get('play_conditions') or play_context(beatmap)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def timing_comparable(beatmap, play, *, exact=False):
    """Unknown OD cannot establish cross-map timing comparability."""
    expected = conditions(beatmap)
    if expected is None or not conditions_match(expected, play):
        return False
    left, right = number(beatmap.get('od')), number(play.get('od'))
    if left is None or right is None:
        return exact and left is None and right is None
    return abs(left - right) <= (.05 if exact else get_setting('comparable_od_band')) + 1e-8


class EvidenceIndex:
    def __init__(self, plays):
        self.rows = defaultdict(list)
        seen = set()
        for play in sorted(plays, key=lambda p: play_time(p) or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            if not valid(play) or play['id'] in seen:
                continue
            seen.add(play['id'])
            for token in self.tokens(play, play=True):
                self.rows[token].append(play)

    @staticmethod
    def tokens(value, play=False):
        key = str(value.get('beatmap_key') or value.get('key') or '').casefold()
        identifier = value.get('beatmap_id') if play else value.get('id', value.get('beatmap_id'))
        return ([('key', key)] if key else []) + ([('id', str(identifier))] if identifier else [])

    def attempts(self, beatmap):
        found = {p['id']: p for token in self.tokens(beatmap) for p in self.rows.get(token, [])}
        return sorted((p for p in found.values() if _same_map(beatmap, p)), key=play_time, reverse=True)


def benchmark_candidates(maps, history, *, now=None):
    if not get_setting('benchmark_enabled'):
        return []
    now = play_time(now) or datetime.now(timezone.utc)
    index = EvidenceIndex(history)
    result = []
    for beatmap in maps:
        # Download discovery remains dedicated to new maps; reference repeats
        # are selected only from installed maps with an equivalent full result.
        if beatmap.get('source') == 'online' or beatmap.get('local') is False:
            continue
        attempts = index.attempts(beatmap)
        if not attempts or now - play_time(attempts[0]) < timedelta(days=get_setting('benchmark_cooldown_days')):
            continue
        reference = next((p for p in attempts if completed(p) and timing_comparable(beatmap, p, exact=True)), None)
        if reference is None:
            continue
        result.append({**beatmap, 'benchmark': {
            'play_id': reference['id'], 'played_at': reference['played_at'],
            'accuracy': None if reference.get('accuracy_rounded') else number(reference.get('accuracy')), 'misses': number(reference.get('misses')),
            'max_combo': number(reference.get('max_combo')), 'completion': reference.get('completion'),
            'od': reference.get('od'), 'label': 'Mapa de referencia',
        }})
    return result


def skill_references(plays, baseline, now=None):
    now = play_time(now) or datetime.now(timezone.utc)
    assignments = session_ids(plays)
    result = []
    for key, (label, _) in SKILLS.items():
        selected, counts = [], defaultdict(int)
        for play in sorted(plays, key=play_time, reverse=True):
            map_key = str(play.get('beatmap_key') or play.get('beatmap_id'))
            if key not in skills_for(play) or counts[map_key] >= get_setting('max_attempts_per_map'):
                continue
            counts[map_key] += 1
            selected.append(play)
        grouped = defaultdict(list)
        for play in selected:
            sr, acc = number(play.get('stars')), number(play.get('accuracy'))
            if sr is None or acc is None:
                continue
            # No FC prerequisite: a controlled completion is evidence too.
            misses, objects = number(play.get('misses')), number(play.get('judged_objects') or play.get('object_count'))
            controlled = completed(play) and acc >= get_setting('challenge_accuracy') and misses is not None and objects and misses / objects <= get_setting('challenge_miss_percent') / 100
            estimate = sr if controlled else sr - get_setting('recovery_drop')
            grouped[str(play.get('beatmap_key') or play.get('beatmap_id'))].append((estimate, recency_weight(play, now)))
        sessions = len({assignments[p['id']] for p in selected if p['id'] in assignments})
        enough = (len(selected) >= get_setting('profile_min_plays') and len(grouped) >= get_setting('profile_min_maps')
                  and sessions >= get_setting('profile_min_sessions'))
        values = [(weighted_mean(rows), max(w for _, w in rows)) for rows in grouped.values()]
        reference = weighted_mean(values) if values else None
        result.append({'key': key, 'label': label, 'reference': round(max(.5, reference), 2) if enough else None,
                       'samples': len(selected), 'distinct_maps': len(grouped), 'sessions': sessions,
                       'status': ('practice' if reference < baseline - .15 else 'strength' if reference > baseline + .15 else 'steady') if enough else 'learning',
                       'note': 'Referencia de dificultad en mapas con estas etiquetas; no identifica la causa de cada fallo.'})
    return result


def skill_target(beatmap, player, fallback, baseline=None):
    keys = skills_for(beatmap)
    refs = [row['reference'] for row in (player or {}).get('skill_levels', [])
            if row['key'] in keys and row.get('reference') is not None]
    if not refs:
        return fallback
    # Correlated tags never add difficulty. Use the lower supported reference,
    # bounded to avoid a sparse specialist sample dominating the whole plan.
    offset = min(refs) - (fallback if baseline is None else baseline)
    return max(.5, fallback + max(-get_setting('comparable_star_band'), min(get_setting('comparable_star_band'), offset)))


def evolution(plays, now=None):
    """Compare first/latest full results on exact maps in different sessions.

    One pair per map/mod/revision. A failed last attempt stays visible as a
    completion setback; a partial accuracy cannot claim an improvement.
    """
    groups = defaultdict(list)
    assignments = session_ids(plays)
    for play in plays:
        if not valid(play):
            continue
        try:
            key = (str(play.get('beatmap_key') or play.get('beatmap_id')), identity(play_context(play)))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        groups[key].append(play)
    comparisons = []
    for rows in groups.values():
        rows.sort(key=play_time)
        latest = rows[-1]
        first = next((p for p in rows[:-1] if completed(p)
                      and assignments.get(p['id']) != assignments.get(latest['id'])
                      and timing_comparable(p, latest, exact=True)
                      and abs((number(p.get('stars')) or 0) - (number(latest.get('stars')) or 0)) <= .05), None)
        if first is None:
            continue
        changes = []
        for field, label, direction in [('accuracy', 'Precisión', 1), ('misses', 'Misses', -1), ('max_combo', 'Combo', 1)]:
            a, b = number(first.get(field)), number(latest.get(field))
            if not completed(latest) or a is None or b is None or (field == 'accuracy' and (first.get('accuracy_rounded') or latest.get('accuracy_rounded'))):
                continue
            delta = round(b - a, 3)
            changes.append({'key': field, 'label': label, 'before': a, 'after': b, 'delta': delta,
                            'improved': delta * direction > .0001})
        comparisons.append({'map_key': latest.get('beatmap_key'), 'title': latest.get('title') or 'Mapa',
                            'version': latest.get('version'), 'mods': conditions(latest),
                            'before_at': first['played_at'], 'played_at': latest['played_at'],
                            'before_id': first['id'], 'play_id': latest['id'], 'completed': completed(latest),
                            'changes': changes, 'skills': skills_for(latest),
                            'improved': any(c['improved'] for c in changes),
                            'has_setback': not completed(latest) or any(c['delta'] * (-1 if c['key'] == 'misses' else 1) < -.0001 for c in changes)})
    comparisons.sort(key=lambda row: row['played_at'], reverse=True)
    return {'plays': len(plays), 'window_plays': get_setting('trend_plays'), 'window_days': get_setting('trend_days'),
            'comparisons': comparisons[:30], 'compared_maps': len(comparisons),
            'improved_maps': sum(c['improved'] for c in comparisons),
            'method': 'Primera y última partida comparables de la ventana, en sesiones distintas, misma dificultad, revisión, mods y OD. Cada indicador conserva sus mejoras y retrocesos; no se mezclan récords.'}


def training_goal(expected, beatmap, profile, stage, focus=None):
    """Separate the one practice objective from optional score indicators."""
    result = dict(expected)
    primary = {'misses': 'misses', 'combo': 'combo', 'completion': 'complete'}.get((focus or {}).get('key'), 'accuracy')
    if stage == 'challenge':
        primary = 'misses' if result.get('misses_max') is not None else 'accuracy'
    benchmark = beatmap.get('benchmark')
    reference_metrics = result.get('reference_metrics') or {}
    if stage == 'practice' and not benchmark:
        if primary == 'accuracy' and number(reference_metrics.get('accuracy')) is not None:
            result['accuracy_min'] = round(min(99.5, reference_metrics['accuracy'] + get_setting('goal_accuracy_step')), 2)
        if primary == 'misses' and number(reference_metrics.get('miss_rate')) is not None and number(beatmap.get('object_count')):
            result['misses_max'] = max(0, math.floor(reference_metrics['miss_rate'] * beatmap['object_count'] * (1 - get_setting('goal_miss_reduction_percent') / 100)))
        if primary == 'combo' and number(reference_metrics.get('combo')) is not None and number(beatmap.get('max_combo')):
            result['combo_min'] = math.ceil(min(1, reference_metrics['combo'] + get_setting('goal_combo_step') / 100) * beatmap['max_combo'])
    if benchmark:
        if (number(benchmark.get('misses')) or 0) > 0:
            primary = 'misses'
            result['misses_max'] = max(0, math.floor(benchmark['misses'] * (1 - get_setting('goal_miss_reduction_percent') / 100)))
        elif number(benchmark.get('accuracy')) is not None and benchmark['accuracy'] < 100:
            primary = 'accuracy'
            result['accuracy_min'] = round(min(100, benchmark['accuracy'] + get_setting('goal_accuracy_step')), 2)
        elif number(benchmark.get('max_combo')) is not None and number(beatmap.get('max_combo')) and benchmark['max_combo'] < beatmap['max_combo']:
            primary = 'combo'
            result['combo_min'] = min(beatmap['max_combo'], math.ceil(benchmark['max_combo'] + beatmap['max_combo'] * get_setting('goal_combo_step') / 100))
        else:
            primary = 'accuracy'
            result['accuracy_min'] = get_setting('strong_accuracy')
        result['benchmark_reference'] = dict(benchmark)
        result['basis'] = 'Comparación con tu partida del ' + benchmark['played_at'][:10] + ', en la misma dificultad y con los mismos mods.'
    elif stage == 'challenge':
        # Zero misses is a consolidation target. An unfamiliar challenge keeps
        # a bounded miss budget and does not require S/FC as a gateway.
        objects = number(beatmap.get('object_count'))
        if objects:
            result['misses_max'] = max(result.get('misses_max') or 0, math.floor(objects * get_setting('challenge_miss_percent') / 100))
        result['accuracy_min'] = min(result['accuracy_min'], get_setting('challenge_accuracy'))
    required = ['complete']
    if stage == 'consolidate' and not benchmark:
        required += [key for key, field in [('accuracy', 'accuracy_min'), ('misses', 'misses_max')] if result.get(field) is not None]
    elif primary != 'complete':
        required.append(primary)
    # A miss/combo target must not reward uncontrolled tapping. This explicit
    # guard stays below or at the player's attainable accuracy target.
    if primary in {'misses', 'combo'} and stage != 'consolidate':
        result['accuracy_min'] = min(result['accuracy_min'], get_setting('challenge_accuracy'))
        required.append('accuracy')
    field_for = {'accuracy': 'accuracy_min', 'misses': 'misses_max', 'combo': 'combo_min'}
    if primary != 'complete' and result.get(field_for[primary]) is None:
        primary = 'accuracy'
        required = ['complete', 'accuracy']
    grade = target_grade((conditions(beatmap) or {}).get('client', 'lazer'), result['accuracy_min'], result.get('misses_max'), (conditions(beatmap) or {}).get('mods', []))
    result.update(grade_min=grade.get('grade'), grade_label=grade.get('label', 'Completar'),
                  grade_requirements=grade.get('requirements', []), grade_note=grade.get('note', ''),
                  grade_is_conditional=grade.get('grade_is_conditional', False))
    result.update(required_keys=required, primary_metric=primary, model_version=MODEL_VERSION,
                  training_role='benchmark' if benchmark else stage,
                  note='La meta principal y el control indicado completan la misión. El grado y los demás indicadores son orientativos; todos los requisitos se evalúan en una misma partida.')
    if focus:
        result['focus'] = {k: focus[k] for k in ('key', 'label', 'action', 'tag') if k in focus}
    return result
