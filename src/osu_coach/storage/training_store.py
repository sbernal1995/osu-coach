"""Persistent practice steps, earned only by newly assigned completed missions.

Call under Coach.lock and a transaction. Scope includes player/client/mods and
calibration epoch. Each cycle freezes its rules; reading old victories cannot
create progress, and a completion can be spent only in its assigned cycle.
"""
import copy
import json
import uuid

from osu_coach.core.evidence import session_ids
from osu_coach.core.engine import struggling
from osu_coach.settings import get_setting
from osu_coach.storage.quest_store import map_tokens


class TrainingStore:
    def __init__(self, db):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS training_levels (scope TEXT PRIMARY KEY, data TEXT NOT NULL)")

    @staticmethod
    def _cycle(level, previous=None):
        return {"stars": round(level, 2), "cycle": uuid.uuid4().hex,
                "step": get_setting('training_step'),
                "required_maps": get_setting('training_required_maps'),
                "required_sessions": get_setting('training_required_sessions'),
                "credits": [], "history": (previous or {}).get('history', [])}

    def sync(self, scope, profile, completions, plays):
        if not scope or not get_setting('training_progress_enabled') or profile.get('phase') != 'training':
            return None
        row = self.db.execute("SELECT data FROM training_levels WHERE scope=?", (scope,)).fetchone()
        data = json.loads(row[0]) if row else self._cycle(profile['baseline'])
        assignments = session_ids(plays)
        accepted = {p.get('id'): p for p in plays if not p.get('excluded') and not p.get('needs_confirmation')}
        used = {token for item in data['credits'] for token in item['tokens']}
        play_ids = {item['play_id'] for item in data['credits']}
        changed = False
        for quest in sorted(completions, key=lambda q: (q.get('completed_at') or '', q.get('id') or '')):
            beatmap = quest.get('map') or {}
            stamp = beatmap.get('training_progress') or {}
            play_id = quest.get('completed_play_id')
            play = accepted.get(play_id)
            tokens = map_tokens(beatmap)
            if (quest.get('status') != 'completed' or stamp.get('cycle') != data['cycle']
                    or not stamp.get('eligible') or not tokens or tokens & used
                    or play_id in play_ids or play is None or play.get('passed') is not True
                    or play_id not in assignments):
                continue
            data['credits'].append({'quest_id': quest['id'], 'play_id': play_id,
                                    'tokens': sorted(tokens), 'title': beatmap.get('title', 'Mapa'),
                                    'version': beatmap.get('version', ''), 'stars': beatmap.get('stars'),
                                    'played_at': play['played_at'], 'session': assignments[play_id]})
            used.update(tokens)
            play_ids.add(play_id)
            changed = True
        # Refresh session identities using the complete accepted log: intervening
        # attempts and delayed confirmations must not fabricate a second session.
        for credit in data['credits']:
            credit['session'] = assignments.get(credit['play_id'], credit['session'])
        sessions = len({c['session'] for c in data['credits']})
        if (changed and data['stars'] < 10.5 and len(data['credits']) >= data['required_maps']
                and sessions >= data['required_sessions']
                and profile.get('training_adjustments', {}).get('mode') != 'recover'
                and sum(struggling(p) for p in profile.get('session_window', [])[-3:]) < 2):
            next_level = round(min(10.5, data['stars'] + data['step']), 2)
            data['history'].append({'from': data['stars'], 'to': next_level,
                                    'completed_at': max(c['played_at'] for c in data['credits']),
                                    'maps': len(data['credits']), 'sessions': sessions})
            data = self._cycle(next_level, data)
        encoded = json.dumps(data)
        if row is None or row[0] != encoded:
            self.db.execute("INSERT OR REPLACE INTO training_levels VALUES (?, ?)", (scope, encoded))
        result = copy.deepcopy(data)
        result['completed_maps'] = len(data['credits'])
        result['completed_sessions'] = len({c['session'] for c in data['credits']})
        result['next_stars'] = round(min(10.5, data['stars'] + data['step']), 2) if data['stars'] < 10.5 else None
        return result
