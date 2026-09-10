"""Recorded coach references and earned personal ranks, independent of matchmaking.

All calls run under Coach.lock. Mutations participate in the caller's SQLite
transaction. Existing references are retained when a delayed result is accepted.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import math

from engine import assess, number, timestamp
from progress_rules import rank_evidence
from evidence import reference_settings_signature, setting_text
from settings import get_setting


METHOD_VERSION = 2


class ProgressStore:
    def __init__(self, db):
        self.db = db
        self._history_cache = {}
        db.execute("""CREATE TABLE IF NOT EXISTS coach_progress_points (
            profile TEXT NOT NULL, epoch TEXT NOT NULL, play_id TEXT NOT NULL,
            played_at TEXT NOT NULL, reference REAL NOT NULL, calibrated INTEGER NOT NULL,
            source TEXT NOT NULL, method_version INTEGER NOT NULL DEFAULT 2, settings_signature TEXT,
            PRIMARY KEY (profile, epoch, play_id))""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(coach_progress_points)")}
        if "method_version" not in columns:
            # Classify existing observations without changing their values,
            # dates or earned ranks. A recalculation is never a played result.
            db.execute("ALTER TABLE coach_progress_points ADD COLUMN method_version INTEGER NOT NULL DEFAULT 1")
        if "settings_signature" not in columns:
            db.execute("ALTER TABLE coach_progress_points ADD COLUMN settings_signature TEXT")
        db.execute("""CREATE INDEX IF NOT EXISTS coach_progress_dates
            ON coach_progress_points(profile, epoch, played_at)""")
        db.execute("""CREATE TABLE IF NOT EXISTS coach_rank_milestones (
            profile TEXT NOT NULL, stars REAL NOT NULL, earned_at TEXT NOT NULL,
            source TEXT NOT NULL, PRIMARY KEY (profile, stars))""")

    def _rank(self, profile):
        row = self.db.execute("""SELECT stars, earned_at FROM coach_rank_milestones
            WHERE profile=? ORDER BY stars DESC LIMIT 1""", (profile,)).fetchone()
        return {"stars": row[0], "earned_at": row[1],
                "label": f"{row[0]:.2f}".replace(".", ",") + " ★ consolidadas"} if row else {
                    "stars": None, "earned_at": None, "label": "Rango por consolidar"}

    def _award(self, profile, assessment, earned_at, source):
        earned = self._rank(profile)["stars"]
        candidate = rank_evidence(assessment, earned)["candidate_stars"]
        if candidate is not None and (earned is None or candidate > earned):
            self.db.execute("INSERT OR IGNORE INTO coach_rank_milestones VALUES (?, ?, ?, ?)",
                            (profile, candidate, earned_at, source))

    def sync(self, profile, since, plays, initial=None, observed_id=None):
        """Record accepted results; reconstruct only a never-tracked profile.

        A reread or method migration cannot fill holes in an existing curve or
        earn another rank. Initial imports with no progress history are labelled
        reconstructed and evaluated at each historical play's own timestamp.
        """
        now = datetime.now(timezone.utc)
        cutoff = timestamp(since)
        ordered = []
        for play in plays:
            date = timestamp(play.get("played_at"))
            if (date is None or not isinstance(play.get("id"), str) or not play["id"]
                    or date > now + timedelta(seconds=5) or (cutoff is not None and date < cutoff)
                    or play.get("mode", 0) != 0 or number(play.get("stars")) <= 0
                    or play.get("needs_confirmation") or play.get("excluded")):
                continue
            ordered.append((date, play))
        ordered.sort(key=lambda item: (item[0], item[1]["id"]))
        existing = {row[0] for row in self.db.execute(
            "SELECT play_id FROM coach_progress_points WHERE profile=? AND epoch=?", (profile, since))}
        has_history = self.db.execute("SELECT 1 FROM coach_progress_points WHERE profile=? LIMIT 1",
                                      (profile,)).fetchone() is not None
        reconstruct = observed_id is None and not has_history
        prefix = []
        accepted_window = None
        for date, play in ordered:
            prefix.append(play)
            if play["id"] in existing:
                continue
            if play["id"] != observed_id and not reconstruct:
                continue
            source = "recorded" if play["id"] == observed_id else "reconstructed"
            if source == "recorded":
                # Confirmations can arrive after newer scores. Record the form
                # observed at acceptance, leaving all earlier points intact.
                if accepted_window is None:
                    accepted_window = assess([item[1] for item in ordered], now=now, since=since, initial=initial)
                assessment, recorded_at = accepted_window, now.isoformat()
            else:
                assessment = assess(prefix, now=date, since=since, initial=initial)
                recorded_at = date.isoformat()
            inserted = self.db.execute("""INSERT OR IGNORE INTO coach_progress_points
                (profile, epoch, play_id, played_at, reference, calibrated, source, method_version, settings_signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (profile, since, play["id"], recorded_at, assessment["baseline"],
                             int(assessment["phase"] == "training"), source, METHOD_VERSION,
                             assessment.get("settings_signature") or reference_settings_signature())).rowcount
            if not inserted:
                continue
            existing.add(play["id"])
            for cache_key in list(self._history_cache):
                if cache_key[:2] == (profile, since):
                    del self._history_cache[cache_key]
            # An expired late confirmation cannot grant a rank from a window
            # to which that accepted result contributes no evidence.
            if any(item.get("id") == play["id"] for item in assessment.get("window", [])):
                self._award(profile, assessment, recorded_at, source)

    def _history(self, profile, since):
        signature = reference_settings_signature()
        scope = (profile, since)
        key = (*scope, signature)
        if key in self._history_cache:
            return copy.deepcopy(self._history_cache[key])
        total = self.db.execute("SELECT COUNT(*) FROM coach_progress_points WHERE profile=? AND epoch=?",
                                scope).fetchone()[0]
        # NULL signatures from the original method are its documented defaults.
        # Reading them never rewrites their values or invents new observations.
        first = self.db.execute("""SELECT reference FROM coach_progress_points
            WHERE profile=? AND epoch=? AND calibrated=1 AND method_version=?
            AND COALESCE(settings_signature, 'default-v2')=?
            ORDER BY played_at, play_id LIMIT 1""", (*scope, METHOD_VERSION, signature)).fetchone()
        best = self.db.execute("""SELECT MAX(reference) FROM coach_progress_points
            WHERE profile=? AND epoch=? AND calibrated=1 AND method_version=?
            AND COALESCE(settings_signature, 'default-v2')=?""",
                               (*scope, METHOD_VERSION, signature)).fetchone()[0]
        has_legacy = self.db.execute("""SELECT 1 FROM coach_progress_points
            WHERE profile=? AND epoch=? AND method_version<>? LIMIT 1""",
                                    (*scope, METHOD_VERSION)).fetchone() is not None
        has_other_settings = self.db.execute("""SELECT 1 FROM coach_progress_points
            WHERE profile=? AND epoch=? AND method_version=?
            AND COALESCE(settings_signature, 'default-v2')<>? LIMIT 1""",
                                            (*scope, METHOD_VERSION, signature)).fetchone() is not None
        # Keep the beginning and the latest point when a long history is sampled.
        stride = max(1, math.ceil(max(0, total - 1) / 119))
        rows = self.db.execute("""WITH ordered AS (
            SELECT played_at, reference, calibrated, source, method_version, settings_signature,
                   ROW_NUMBER() OVER (ORDER BY played_at, play_id) AS position
            FROM coach_progress_points WHERE profile=? AND epoch=?)
            SELECT played_at, reference, calibrated, source, method_version, settings_signature FROM ordered
            WHERE (position-1) % ? = 0 OR position=? ORDER BY position""", (*scope, stride, total)).fetchall()
        result = {"tracked_plays": total, "initial_reference": first[0] if first else None,
                  "best_reference": best, "has_legacy_history": has_legacy,
                  "has_other_settings": has_other_settings,
                  "history": [{"played_at": row[0], "reference": row[1], "calibrated": bool(row[2]),
                               "source": row[3], "method_version": row[4],
                               "settings_signature": row[5] or ("default-v2" if row[4] == 2 else "legacy-v1")}
                              for row in rows]}
        self._history_cache[key] = result
        return copy.deepcopy(result)

    def snapshot(self, profile, since, assessment):
        if profile is None:
            return None
        rank = self._rank(profile)
        evidence = rank_evidence(assessment, rank["stars"])
        history = self._history(profile, since)
        calibrated = assessment["phase"] == "training"
        change = (round(assessment["baseline"] - history["initial_reference"], 2)
                  if calibrated and history["initial_reference"] is not None else None)
        milestones = self.db.execute("""SELECT stars, earned_at, source FROM coach_rank_milestones
            WHERE profile=? ORDER BY stars DESC LIMIT 12""", (profile,)).fetchall()
        window_text = f"{get_setting('reference_plays')} partidas de {get_setting('reference_days')} días"
        method_note = (
            "La curva conserva las referencias anteriores, calculadas con otros métodos o ajustes. "
            f"La comparación actual empieza con el primer resultado registrado con estos ajustes de {window_text}. "
            "El cambio de ajustes no cuenta como mejora y los rangos ganados se conservan."
            if history["has_legacy_history"] or history["has_other_settings"] else
            f"La comparación usa referencias registradas con el mismo método y ajustes de {window_text}.")
        return {**history, "current_reference": assessment["baseline"],
                "method_version": METHOD_VERSION, "settings_signature": reference_settings_signature(), "method_note": method_note,
                "status": "ready" if calibrated else "learning", "change": change,
                "rank": rank,
                "next_rank": {"stars": evidence["next_stars"],
                              **{field: evidence[field] for field in (
                                  "completed_maps", "required_maps", "qualifying_maps", "calibrated",
                                  "requirements", "window_plays", "window_days")}},
                "milestones": [{"stars": row[0], "earned_at": row[1], "source": row[2]} for row in milestones],
                "since": since,
                "method": (f"La referencia actual usa hasta {get_setting('reference_plays')} partidas de los últimos "
                           f"{get_setting('reference_days')} días, con mayor peso para las recientes. "
                           f"El rango personal avanza en escalones de {setting_text('rank_step')} ★ al demostrar resultados sólidos "
                           f"en {get_setting('rank_required_maps')} dificultades distintas de ese rango o hasta "
                           f"{setting_text('comparable_star_band')} ★ por encima, durante esa ventana. "
                           "Los rangos ganados se conservan para este jugador, cliente y mods, incluso al recalibrar. "
                           "La evolución de referencia se muestra desde la última calibración y conserva sus puntos "
                           "aunque salgan de la ventana reciente. Los puntos nuevos se registran al aceptar "
                           "el resultado; los reconstruidos usan la fecha de la partida. "
                           "Las comparaciones de mejora solo usan puntos del mismo método y ajustes. "
                           "Las recomendaciones siguen tu rendimiento actual.")}
