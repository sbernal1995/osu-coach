"""Persistent training missions. Call under Coach.lock and a DB transaction."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import uuid

from quest_rules import evaluate_attempt


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def scope_key(profile, since):
    return json.dumps([profile, since], separators=(",", ":"))


def map_tokens(beatmap):
    """Compare a downloaded difficulty with its former online recommendation."""
    tokens = set()
    key = str(beatmap.get("key") or "").strip().casefold()
    if key:
        tokens.add("key:" + key)
    try:
        identifier = int(beatmap.get("id", 0))
        if identifier > 0:
            tokens.add("id:" + str(identifier))
    except (TypeError, ValueError, OverflowError):
        pass
    if key.startswith(("remote:", "osu:")) and key.split(":", 1)[1].isdigit():
        tokens.add("id:" + str(int(key.split(":", 1)[1])))
    return tokens


def is_remote(beatmap):
    return beatmap.get("source") == "online" or beatmap.get("local") is False


class QuestStore:
    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS quest_boards (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, profile TEXT NOT NULL,
            status TEXT NOT NULL, data TEXT NOT NULL)""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS quest_active_scope
            ON quest_boards(scope) WHERE status='active'""")
        db.execute("""CREATE TABLE IF NOT EXISTS quest_attempts (
            board_id TEXT NOT NULL, quest_id TEXT NOT NULL, play_id TEXT NOT NULL,
            data TEXT NOT NULL, PRIMARY KEY (quest_id, play_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS quest_completions (
            quest_id TEXT PRIMARY KEY, board_id TEXT NOT NULL, profile TEXT NOT NULL,
            completed_at TEXT NOT NULL, data TEXT NOT NULL)""")
        db.execute("""CREATE INDEX IF NOT EXISTS quest_completion_profile
            ON quest_completions(profile, completed_at)""")
        db.execute("""CREATE TABLE IF NOT EXISTS quest_skips (
            quest_id TEXT PRIMARY KEY, board_id TEXT NOT NULL, profile TEXT NOT NULL,
            skipped_at TEXT NOT NULL, data TEXT NOT NULL)""")
        # Keep victories from boards created by versions before automatic renewal.
        for scope, data in db.execute("SELECT scope, data FROM quest_boards").fetchall():
            self._remember_completions(scope, json.loads(data))

    @staticmethod
    def _summary(board):
        quests = [q for group in board["groups"] for q in group["quests"]]
        retired = board.get("retired_count", 0)
        waiting = sum(q["status"] == "completed" for q in quests)
        skipped_waiting = sum(q["status"] == "skipped" for q in quests)
        skipped = board.get("skipped_count", 0)
        board["retired_count"] = retired
        board["skipped_count"] = skipped
        board["skipped_waiting_count"] = skipped_waiting
        board["total_count"] = len(quests) + retired + skipped - skipped_waiting
        board["completed_count"] = waiting + retired
        board["active_count"] = sum(q["status"] in {"pending", "in_progress"} for q in quests)
        board["waiting_count"] = waiting
        board["automatic_refresh"] = True
        board["all_completed"] = bool(quests) and waiting == len(quests)
        return board

    @staticmethod
    def _new_quest(beatmap, group, created_at):
        return {"id": uuid.uuid4().hex, "status": "pending", "map": copy.deepcopy(beatmap),
                "created_at": created_at, "attempt_count": 0, "last_attempt": None,
                "completed_at": None, "completed_play_id": None,
                "stage_label": group.get("label"), "stage_target": group.get("target")}

    @staticmethod
    def _online_limit(group):
        try:
            return max(0, min(3, int(group.get("max_online", 1))))
        except (TypeError, ValueError, OverflowError):
            return 1

    def current(self, scope):
        row = self.db.execute("SELECT data FROM quest_boards WHERE scope=? AND status='active'",
                              (scope,)).fetchone()
        return self._summary(json.loads(row[0])) if row else None

    def _save(self, board):
        self._summary(board)
        self.db.execute("UPDATE quest_boards SET status=?, data=? WHERE id=?",
                        (board["status"], json.dumps(board, ensure_ascii=False), board["id"]))

    def _create(self, scope, label, groups):
        created_at = utcnow()
        frozen_groups = []
        seen = set()
        for group in groups:
            quests = []
            remote_count = 0
            for beatmap in group.get("maps", []):
                identity = map_tokens(beatmap)
                if (not identity or identity & seen or not isinstance(beatmap.get("expectation"), dict)
                        or (is_remote(beatmap) and remote_count >= self._online_limit(group))):
                    continue
                seen.update(identity)
                quests.append(self._new_quest(beatmap, group, created_at))
                remote_count += int(is_remote(beatmap))
                if len(quests) == 3:
                    break
            frozen_groups.append({**{key: copy.deepcopy(value) for key, value in group.items() if key != "maps"},
                                  "quests": quests})
        if not any(g["quests"] for g in frozen_groups):
            return None
        board = self._summary({"id": uuid.uuid4().hex, "created_at": created_at,
                               "profile_label": label, "status": "active", "groups": frozen_groups})
        self.db.execute("INSERT INTO quest_boards VALUES (?, ?, ?, ?, ?)",
                        (board["id"], scope, json.loads(scope)[0], "active", json.dumps(board, ensure_ascii=False)))
        return board

    def ensure(self, scope, label, groups, replacement_provider=None, skip_predicate=None):
        board = self.current(scope)
        if board is None:
            return self._create(scope, label, groups)
        self._remember_completions(scope, board)
        if skip_predicate is not None:
            self._skip_previously_played(scope, board, skip_predicate)
        incomplete = any(len(group["quests"]) < 3 for group in board["groups"])
        if (board["waiting_count"] or board["skipped_waiting_count"] or incomplete) and replacement_provider is not None:
            self._renew_completed(board, replacement_provider(board))
        return board

    def _skip_previously_played(self, scope, board, predicate):
        changed = False
        for group in board["groups"]:
            for quest in group["quests"]:
                if (quest["status"] != "pending" or quest.get("attempt_count", 0)
                        or quest.get("last_attempt") is not None):
                    continue
                decision = predicate(quest)
                if not decision:
                    continue
                reason = "download_quality" if decision == "download_quality" else "played_before_assignment"
                quest.update(status="skipped", skipped_reason=reason, skipped_at=utcnow())
                saved = copy.deepcopy(quest)
                saved.setdefault("stage_label", group.get("label"))
                saved.setdefault("stage_target", group.get("target"))
                saved["board_id"] = board["id"]
                self.db.execute("INSERT OR IGNORE INTO quest_skips VALUES (?, ?, ?, ?, ?)",
                                (quest["id"], board["id"], json.loads(scope)[0], quest["skipped_at"],
                                 json.dumps(saved, ensure_ascii=False)))
                board["skipped_count"] = board.get("skipped_count", 0) + 1
                changed = True
        if changed:
            self._save(board)

    def _remember_completions(self, scope, board):
        for group in board["groups"]:
            for quest in group["quests"]:
                if quest["status"] != "completed":
                    continue
                saved = copy.deepcopy(quest)
                saved.setdefault("stage_label", group.get("label"))
                saved.setdefault("stage_target", group.get("target"))
                saved["board_id"] = board["id"]
                self.db.execute("INSERT OR IGNORE INTO quest_completions VALUES (?, ?, ?, ?, ?)",
                                (quest["id"], board["id"], json.loads(scope)[0],
                                 quest.get("completed_at") or quest["created_at"], json.dumps(saved, ensure_ascii=False)))

    def completions(self, scope, *, limit=30):
        profile = json.loads(scope)[0]
        total = self.db.execute("SELECT COUNT(*) FROM quest_completions WHERE profile=?", (profile,)).fetchone()[0]
        query = "SELECT data FROM quest_completions WHERE profile=? ORDER BY rowid DESC"
        rows = self.db.execute(query + (" LIMIT ?" if limit is not None else ""),
                               (profile, limit) if limit is not None else (profile,)).fetchall()
        return {"total": total, "items": [json.loads(row[0]) for row in rows]}

    def skips(self, scope):
        profile = json.loads(scope)[0]
        total = self.db.execute("SELECT COUNT(*) FROM quest_skips WHERE profile=?", (profile,)).fetchone()[0]
        rows = self.db.execute("SELECT data FROM quest_skips WHERE profile=? ORDER BY rowid DESC LIMIT 30",
                               (profile,)).fetchall()
        return {"total": total, "items": [json.loads(row[0]) for row in rows]}

    def _renew_completed(self, board, replacements):
        """Replace finished slots and fill unassigned capacity without moving active work."""
        visible = [quest["map"] for group in board["groups"] for quest in group["quests"]]
        occupied = set().union(*(map_tokens(beatmap) for beatmap in visible)) if visible else set()
        changed = False
        created_at = utcnow()
        for group in board["groups"]:
            fresh = next((item for item in replacements if item["stage"] == group["stage"]), None)
            if fresh is None:
                continue
            pending = [quest for quest in group["quests"] if quest["status"] in {"pending", "in_progress"}]
            remote_count = sum(is_remote(quest["map"]) for quest in pending)
            slots = [(index, quest) for index, quest in enumerate(group["quests"])
                     if quest["status"] in {"completed", "skipped"}]
            slots.extend((None, None) for _ in range(max(0, 3 - len(group["quests"]))))
            for index, quest in slots:
                eligible = [candidate for candidate in fresh.get("maps", [])
                            if isinstance(candidate.get("expectation"), dict) and map_tokens(candidate)
                            and not (map_tokens(candidate) & occupied)
                            and (not is_remote(candidate) or remote_count < self._online_limit(fresh))]
                # Keep the usual online option; stages with scarce local maps
                # can explicitly permit more downloads through max_online.
                beatmap = next((m for m in eligible if remote_count == 0 and is_remote(m)), None)
                beatmap = beatmap or next(iter(eligible), None)
                if beatmap is None:
                    continue
                replacement = self._new_quest(beatmap, fresh, created_at)
                if quest is None:
                    group["quests"].append(replacement)
                else:
                    replacement["replaces_quest_id"] = quest["id"]
                    group["quests"][index] = replacement
                occupied.update(map_tokens(beatmap))
                remote_count += int(is_remote(beatmap))
                if quest is not None and quest["status"] == "completed":
                    board["retired_count"] = board.get("retired_count", 0) + 1
                changed = True
        if changed:
            self._save(board)

    def replace(self, scope, label, groups, expected_id):
        current = self.current(scope)
        if (current["id"] if current else None) != expected_id:
            raise ValueError("La tanda ya cambió. Recargá el panel para ver las misiones actuales.")
        if not any(m.get("expectation") and (m.get("key") or m.get("id"))
                   for group in groups for m in group.get("maps", [])):
            raise ValueError("Todavía no hay mapas adecuados para una nueva tanda. Conservamos tus misiones.")
        if current:
            self._archive(current)
        board = self._create(scope, label, groups)
        if board is None:
            raise ValueError("No se pudieron preparar las misiones. Intentá de nuevo cuando haya mapas disponibles.")
        return board

    def _archive(self, board):
        board["status"] = "archived"
        board["archived_at"] = utcnow()
        self._save(board)

    def archive_all(self):
        rows = self.db.execute("SELECT data FROM quest_boards WHERE status='active'").fetchall()
        for row in rows:
            self._archive(json.loads(row[0]))

    def history(self, scope):
        rows = self.db.execute("""SELECT data FROM quest_boards WHERE profile=? AND status='archived'
            ORDER BY rowid DESC LIMIT 5""", (json.loads(scope)[0],)).fetchall()
        return [{key: board.get(key) for key in ("id", "created_at", "archived_at", "completed_count", "total_count")}
                for board in (self._summary(json.loads(row[0])) for row in rows)]

    def record_play(self, scope, play):
        board = self.current(scope)
        if not board:
            return
        changed = False
        for group in board["groups"]:
            for quest in group["quests"]:
                if quest["status"] not in {"pending", "in_progress"}:
                    continue
                attempt = evaluate_attempt(quest, play)
                if attempt is None:
                    continue
                inserted = self.db.execute("INSERT OR IGNORE INTO quest_attempts VALUES (?, ?, ?, ?)",
                    (board["id"], quest["id"], play["id"], json.dumps(attempt, ensure_ascii=False))).rowcount
                if not inserted:
                    continue
                quest["attempt_count"] += 1
                # A later confirmation of an earlier play must not hide newer feedback.
                previous = quest["last_attempt"]
                if (previous is None or attempt["completed"] or
                        datetime.fromisoformat(attempt["played_at"]) >= datetime.fromisoformat(previous["played_at"])):
                    quest["last_attempt"] = attempt
                quest["status"] = "completed" if attempt["completed"] else "in_progress"
                if attempt["completed"]:
                    quest["completed_at"] = attempt["played_at"]
                    quest["completed_play_id"] = play["id"]
                changed = True
        if changed:
            self._remember_completions(scope, board)
            self._save(board)
