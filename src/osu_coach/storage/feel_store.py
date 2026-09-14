"""Persistent, reversible per-player difficulty feel. Call under Coach.lock and a transaction."""
from __future__ import annotations

from datetime import datetime, timezone
import math


class FeelStore:
    """One offset per player and difficulty; survives clients, mods and recalibration."""

    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS feel_adjustments (
            owner TEXT NOT NULL, beatmap_key TEXT NOT NULL, offset REAL NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (owner, beatmap_key))""")
        db.execute("CREATE INDEX IF NOT EXISTS feel_owner ON feel_adjustments(owner)")

    def map(self, owner):
        return {row[0]: row[1] for row in self.db.execute(
            "SELECT beatmap_key, offset FROM feel_adjustments WHERE owner=?", (owner,))}

    def set(self, owner, beatmap_key, offset):
        if not owner:
            raise ValueError("Jugá una primera partida para guardar la sensación de dificultad.")
        if not isinstance(beatmap_key, str) or not beatmap_key.strip() or len(beatmap_key) > 100:
            raise ValueError("La dificultad no tiene una clave identificadora suficiente.")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not math.isfinite(offset):
            raise ValueError("Elegí una de las opciones de sensación de dificultad.")
        key = beatmap_key.strip()
        if offset == 0:
            self.clear(owner, key)
            return
        with self.db:
            self.db.execute(
                "INSERT INTO feel_adjustments (owner, beatmap_key, offset, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(owner, beatmap_key) DO UPDATE SET offset=excluded.offset, updated_at=excluded.updated_at",
                (owner, key, float(offset), datetime.now(timezone.utc).isoformat()))

    def clear(self, owner, beatmap_key):
        self.db.execute("DELETE FROM feel_adjustments WHERE owner=? AND beatmap_key=?",
                        (owner, beatmap_key))

    def snapshot(self, owner):
        values = self.map(owner)
        return {"total": len(values),
                "items": [{"beatmap_key": key, "offset": value} for key, value in values.items()]}