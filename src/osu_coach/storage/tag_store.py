"""Bounded background enrichment of local maps with public community tags."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

TTL_SECONDS = 7 * 24 * 3600
MAX_SETS_PER_SYNC = 40
MIN_VOTES = 5


def _stamp():
    return datetime.now(timezone.utc).isoformat()


class TagStore:
    def __init__(self, directory, stop=None, fetcher=None):
        self.path = Path(directory) / "community-tags.json"
        self.stop = stop or threading.Event()
        self.lock = threading.RLock()
        self.fetcher = fetcher
        self.sets = {}
        self.thread = None
        self.next_retry_at = 0
        self.status = {"state": "ready", "message": "Los tags se consultan para tus partidas y mapas cercanos a tu dificultad.",
                       "processed": 0, "total": 0, "last_updated": None}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if value.get("version") != 1 or not isinstance(value.get("sets"), dict):
                    raise ValueError("Formato de caché desconocido")
                self.sets = value["sets"]
                self.status["last_updated"] = value.get("updated_at")
            except (ValueError, OSError, AttributeError):
                self.status.update(state="error", message="La caché de tags no se pudo leer. Podés volver a consultar las etiquetas.")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 1, "updated_at": _stamp(), "sets": self.sets}, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)

    def _index(self):
        index = {}
        for record in self.sets.values():
            for beatmap_id, votes in record.get("maps", {}).items():
                index[str(beatmap_id)] = (votes, record.get("fetched_at"))
        return index

    def enrich(self, maps):
        with self.lock:
            index = self._index()
        enriched = []
        for beatmap in maps:
            result = dict(beatmap)
            identifier = beatmap.get("beatmap_id") if "beatmap_id" in beatmap else beatmap.get("id")
            match = index.get(str(identifier or 0))
            if match is None:
                # Demo/manual fixtures can bring explicit, attributed tags.
                result.setdefault("tags", [])
                result["tag_status"] = ("known" if result["tags"] else
                                        result.get("tag_status") if result.get("tag_status") in {"none", "weak"} else "missing")
            else:
                votes, fetched_at = match
                result["tags"] = [{**tag, "source": "community", "provenance": "community_user_tags"} for tag in votes
                                  if isinstance(tag, dict) and isinstance(tag.get("count"), int)
                                  and tag["count"] >= MIN_VOTES and isinstance(tag.get("name"), str)]
                result["tag_status"] = "known" if result["tags"] else ("weak" if votes else "none")
                result["tags_checked_at"] = fetched_at
            enriched.append(result)
        return enriched

    def snapshot(self, maps):
        enriched = self.enrich(maps)
        with self.lock:
            state = dict(self.status)
        from osu_coach.core.tag_analysis import skill_tags
        state.update(tagged_maps=sum(bool(skill_tags(m)) for m in enriched),
                     known_maps=sum(m.get("tag_status") != "missing" for m in enriched),
                     weak_votes_maps=sum(m.get("tag_status") == "weak" for m in enriched),
                     catalog_maps=len(enriched))
        return state

    def plan(self, maps, plays, baseline, force=False):
        by_key = {m.get("key"): m for m in maps}
        by_id = {m.get("id"): m for m in maps if m.get("id")}
        ordered = []
        for play in reversed(sorted(plays, key=lambda p: p.get("played_at", ""))):
            match = by_key.get(play.get("beatmap_key")) or by_id.get(play.get("beatmap_id"))
            if match:
                ordered.append(match)
        ordered += sorted((m for m in maps if abs(float(m.get("stars", 0)) - baseline) <= .85),
                          key=lambda m: abs(float(m.get("stars", 0)) - baseline))
        selected, seen = [], set()
        with self.lock:
            records = dict(self.sets)
        for beatmap in ordered:
            set_id = beatmap.get("set_id")
            if not isinstance(set_id, int) or set_id <= 0 or set_id in seen:
                continue
            seen.add(set_id)
            cached = records.get(str(set_id), {})
            if not force and time.time() - cached.get("fetched_epoch", 0) < TTL_SECONDS:
                continue
            selected.append(set_id)
            if len(selected) == MAX_SETS_PER_SYNC:
                break
        return selected

    def sync(self, maps, plays, baseline, force=False):
        with self.lock:
            if self.status["state"] == "loading" or self.stop.is_set() or time.time() < self.next_retry_at:
                return False
            selected = self.plan(maps, plays, baseline, force)
            if not selected:
                self.status.update(state="ready", processed=0, total=0,
                                   message="Los tags de los mapas elegidos están actualizados.")
                return False
            self.status.update(state="loading", processed=0, total=len(selected),
                               message="Consultando tags comunitarios de tus partidas y mapas cercanos…")

        def worker():
            failures = []
            if self.fetcher is None:
                from osu_coach.integrations.tag_source import fetch_set_tags
                fetch = fetch_set_tags
            else:
                fetch = self.fetcher
            for index, set_id in enumerate(selected):
                if self.stop.is_set():
                    break
                try:
                    values = fetch(set_id)
                    record = {"fetched_at": _stamp(), "fetched_epoch": time.time(),
                              "maps": {str(k): v for k, v in values.items()}}
                    with self.lock:
                        self.sets[str(set_id)] = record
                        self._save()
                        self.status["last_updated"] = record["fetched_at"]
                except Exception as error:
                    failures.append(str(error)[:160])
                with self.lock:
                    self.status["processed"] = index + 1
                # Back off quickly on upstream failures, keeping usable cached tags.
                if len(failures) >= 3:
                    break
                if index < len(selected) - 1 and self.stop.wait(1.1):
                    break
            with self.lock:
                if failures:
                    self.next_retry_at = time.time() + 60
                    self.status.update(state="error", message="Algunos tags no se pudieron actualizar. Se conservan los datos anteriores. " + failures[-1])
                else:
                    self.status.update(state="ready", message="Tags comunitarios actualizados. La cobertura crece con tus partidas.")
        self.thread = threading.Thread(target=worker, daemon=True, name="community-tags")
        self.thread.start()
        return True
