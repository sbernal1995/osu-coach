"""Persistent, reversible music preferences; call under Coach.lock and a transaction."""
from datetime import datetime, timezone
import json
import uuid

from osu_coach.core.song_identity import song_tokens


class SongBanStore:
    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS song_bans (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS song_ban_owner ON song_bans(owner)")

    def items(self, owner):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM song_bans WHERE owner=? ORDER BY rowid DESC", (owner,))]

    def tokens(self, owner):
        return {token for item in self.items(owner) for token in item["tokens"]}

    def ban(self, owner, beatmap, aliases=()):
        tokens = song_tokens(beatmap)
        if not owner or not tokens:
            raise ValueError("La canción no tiene datos suficientes para excluirla.")
        # Enrich the selected song with known local/online spelling aliases only.
        for candidate in aliases:
            if tokens & song_tokens(candidate):
                tokens.update(song_tokens(candidate))
        existing = [item for item in self.items(owner) if tokens & set(item["tokens"])]
        for item in existing:
            tokens.update(item["tokens"])
        record = {"id": existing[0]["id"] if existing else uuid.uuid4().hex,
                  "title": beatmap.get("title") or "Canción sin título",
                  "artist": beatmap.get("artist") or "Artista sin datos",
                  "created_at": existing[0]["created_at"] if existing else datetime.now(timezone.utc).isoformat(),
                  "tokens": sorted(tokens)}
        for item in existing:
            self.db.execute("DELETE FROM song_bans WHERE id=? AND owner=?", (item["id"], owner))
        self.db.execute("INSERT INTO song_bans VALUES (?, ?, ?)",
                        (record["id"], owner, json.dumps(record, ensure_ascii=False)))
        return record

    def unban(self, owner, identifier):
        # Idempotent, including a retried request after a successful response was lost.
        self.db.execute("DELETE FROM song_bans WHERE id=? AND owner=?", (identifier, owner))

    def snapshot(self, owner):
        items = [{k: v for k, v in item.items() if k != "tokens"} for item in self.items(owner)]
        return {"total": len(items), "items": items}
