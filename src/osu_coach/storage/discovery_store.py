"""Daily discovery cache; downloads metadata only, never imports songs into osu!."""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import json
import math
from pathlib import Path
import threading
import time

from osu_coach.integrations.discovery_source import candidate_quality_ok, MIN_RATING, MIN_RATING_VOTES, MIN_PLAY_COUNT, MIN_REQUEST_INTERVAL
from osu_coach.core.mod_policy import public_requirements
from osu_coach.settings import DEFAULTS, coach_settings, settings_context, get_setting


INTERVAL = 24 * 3600
RETRY_DELAY = 60
ERROR_RETRY_DELAY = 15 * 60
EXHAUSTED_RETRY_DELAY = 3600
QUALITY_POLICY = {"min_rating": MIN_RATING, "min_votes": MIN_RATING_VOTES,
                  "min_play_count": MIN_PLAY_COUNT, "scope": "beatmapset"}


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def compatible(sample):
    """Public stars are for the unmodified map; don't mix them into mod profiles."""
    if not sample:
        return True
    try:
        settings = json.loads(sample.get("mod_key", "{}"))
        return not sample.get("mods") and not settings.get("mods") and settings.get("rate") in (None, 1)
    except (ValueError, TypeError, AttributeError):
        return False


def _identifier(beatmap):
    try:
        value = int(beatmap.get("id", 0))
        return value if value > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _valid_maps(value):
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


class DiscoveryStore:
    def __init__(self, directory, stop=None, fetcher=None, batch_fetcher=None, settings=None):
        self.settings = dict(DEFAULTS if settings is None else settings)
        self.revision = 0
        self.path = Path(directory) / "discovery.json"
        self.stop = stop or threading.Event()
        self.lock = threading.RLock()
        self.changed = threading.Event()
        self.fetcher = fetcher
        self.batch_fetcher = batch_fetcher
        self.search = {}
        self.demand_retry_at = 0
        self.thread = None
        self.maps = []
        self.fetched_epoch = 0
        self.baseline = None
        self.next_retry_at = 0
        self.retry_path = self.path.with_name("discovery-retry.json")
        self.status = {"state": "ready", "message": "Se explora el catálogo público, también mapas antiguos, mientras el entrenador está abierto."}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if value.get("version") != 1 or not _valid_maps(value.get("maps")):
                    raise ValueError("Formato desconocido")
                fetched_epoch = float(value.get("fetched_epoch", 0))
                baseline = float(value["baseline"]) if value.get("baseline") is not None else None
                if (not math.isfinite(fetched_epoch) or fetched_epoch < 0
                        or (baseline is not None and not math.isfinite(baseline))):
                    raise ValueError("Fecha o dificultad inválida")
                stamp(fetched_epoch)
                self.maps, self.fetched_epoch, self.baseline = value["maps"], fetched_epoch, baseline
                search = value.get("search", {})
                if isinstance(search, dict):
                    retry = float(search.get("next_retry_at", 0))
                    if math.isfinite(retry) and retry >= 0:
                        self.search, self.demand_retry_at = search, retry
                if value.get("quality_policy") != self.quality_policy():
                    # Revisit old discoveries whose saved evidence lacks play counts.
                    self.search, self.demand_retry_at, self.fetched_epoch = {}, 0, 0
            except (ValueError, TypeError, OSError, AttributeError, OverflowError):
                self.status.update(state="error", message="La búsqueda de mapas necesita actualizar su caché.")

        if self.retry_path.exists():
            try:
                retry = json.loads(self.retry_path.read_text(encoding="utf-8"))
                retry_at = float(retry.get("next_retry_at", 0))
                if math.isfinite(retry_at) and retry_at > time.time():
                    self.next_retry_at = retry_at
                    self.status.update(state="error", message=str(retry.get("message") or "Se reintentará la búsqueda automáticamente."))
            except (ValueError, TypeError, OSError, AttributeError):
                pass

    def quality_policy(self):
        return {"min_rating": self.settings["quality_min_rating"], "min_votes": self.settings["quality_min_votes"],
                "min_play_count": self.settings["quality_min_plays"], "scope": "beatmapset"}

    def set_settings(self, values):
        with self.lock:
            if self.settings == values:
                return
            self.settings = dict(values)
            self.revision += 1
            self.search, self.demand_retry_at, self.fetched_epoch = {}, 0, 0
            if self.status["state"] != "error":
                self.next_retry_at = 0
            if self.status["state"] != "loading":
                self.status.update(state="ready", message="Ajustes actualizados. La próxima búsqueda usará tus nuevos criterios.")
            self.changed.set()

    def _save_retry(self, *, clear=False):
        try:
            temp = self.retry_path.with_suffix(".tmp")
            temp.write_text(json.dumps({"next_retry_at": 0 if clear else self.next_retry_at,
                                        "message": self.status["message"]}, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.retry_path)
        except OSError:
            pass

    @coach_settings
    def candidates(self, local_maps, sample=None):
        if not compatible(sample):
            return []
        local_ids = {_identifier(m) for m in local_maps} - {None}
        lazer = not sample or str(sample.get("client", "")).strip().lower() in {"lazer", "osu!lazer", "osu!lazer standard"}
        with self.lock:
            return copy.deepcopy([m for m in self.maps if _identifier(m) not in local_ids
                                  and candidate_quality_ok(m) and (not m.get("lazer_only") or lazer)])

    @coach_settings
    def snapshot(self, local_maps, sample=None, needs=None):
        needs = copy.deepcopy(needs or [])
        with self.lock:
            result = dict(self.status)
            result.update(last_updated=stamp(self.fetched_epoch) if self.fetched_epoch else None,
                          next_update=stamp(self.fetched_epoch + get_setting("discovery_interval_hours") * 3600) if self.fetched_epoch else None,
                          interval_hours=get_setting("discovery_interval_hours"), automatic=get_setting("discovery_enabled"), manual_allowed=compatible(sample), needs=needs, quality_policy=self.quality_policy(),
                          exhausted=bool(self.search.get("exhausted")),
                          batches_completed=self.search.get("pass_batches", 0),
                          batch_limit=get_setting("discovery_batches_per_pass"),
                          continuing=bool(needs and self.search.get("fast_continue") and self.status["state"] == "ready"),
                          next_retry=stamp(max(time.time(), self.next_retry_at, self.demand_retry_at)) if needs else None)
        result["candidate_count"] = len(self.candidates(local_maps, sample))
        if not get_setting("discovery_enabled"):
            result.update(state="paused", next_retry=None, next_update=None,
                          message="La búsqueda automática está desactivada en Configuración. " +
                                  ("Podés buscar manualmente." if compatible(sample) else "La búsqueda manual necesita jugar sin mods y a velocidad normal."))
        elif not compatible(sample):
            result.update(state="paused", next_retry=None,
                          message="La búsqueda automática se reanudará al jugar sin mods y a velocidad normal, para comparar la dificultad correctamente.")
        return result

    @staticmethod
    def _profile(sample):
        sample = sample or {}
        return json.dumps([str(sample.get("player", "Local")).casefold(), sample.get("client"),
                           sample.get("mode", 0), sample.get("mod_key")], sort_keys=True)

    @coach_settings
    def sync(self, baseline, sample=None, force=False, *, needs=None, exclude_ids=(), excluded_songs=(), envelope=None):
        if not compatible(sample) or (not force and not get_setting("discovery_enabled")):
            return False
        needs = copy.deepcopy(needs or [])
        demand = bool(needs)
        excluded_songs = tuple(excluded_songs)
        requirements = [{key: item[key] for key in ("min_stars", "max_stars", "max_bpm", "max_ar", "min_length", "max_length")
                         if key in item} for item in (envelope if envelope is not None else needs)]
        identity = self._profile(sample)
        now = time.time()
        with self.lock:
            if (self.stop.is_set() or self.status["state"] == "loading" or now < self.next_retry_at
                    or (self.thread is not None and self.thread.is_alive())):
                return False
            level_changed = self.baseline is not None and abs(baseline - self.baseline) >= .5 - 1e-8
            if demand or force:
                if now < self.demand_retry_at:
                    return False
            elif not force and now - self.fetched_epoch < get_setting("discovery_interval_hours") * 3600 and not (level_changed and now - self.fetched_epoch >= 3600):
                return False
            search = copy.deepcopy(self.search)
            previous_base = search.get("baseline")
            restart = (search.get("profile") != identity or previous_base is None
                       or abs(baseline - previous_base) >= .25 or search.get("exhausted"))
            # Resume the feed for minor changes. A materially wider practice
            # envelope can uncover maps skipped by the previous requirements.
            previous = search.get("requirements") or []
            old_lower = [r["min_stars"] for r in previous if r.get("min_stars") is not None]
            new_lower = [r["min_stars"] for r in requirements if r.get("min_stars") is not None]
            old_upper = [r["max_stars"] for r in previous if r.get("max_stars") is not None]
            new_upper = [r["max_stars"] for r in requirements if r.get("max_stars") is not None]
            if old_lower and new_lower and min(new_lower) < min(old_lower) - .1:
                restart = True
            if old_upper and new_upper and max(new_upper) > max(old_upper) + .1:
                restart = True
            for field, tolerance in (("max_bpm", 10), ("max_ar", .5), ("max_length", 20)):
                old_values = [r.get(field) for r in previous if r.get(field) is not None]
                new_values = [r.get(field) for r in requirements if r.get(field) is not None]
                if old_values and (not new_values or max(new_values) >= max(old_values) + tolerance):
                    restart = True
            old_min = [r.get("min_length", 0) or 0 for r in previous]
            new_min = [r.get("min_length", 0) or 0 for r in requirements]
            if old_min and (not new_min or min(new_min) < min(old_min)):
                restart = True
            cursor = None if restart else search.get("cursor")
            pass_limit = get_setting("discovery_batches_per_pass")
            pass_batches = search.get("pass_batches", 0)
            if (type(pass_batches) is not int or not 0 <= pass_batches < pass_limit
                    or restart or not demand
                    or now - self.fetched_epoch >= get_setting("discovery_retry_minutes") * 60):
                pass_batches = 0
            excluded = tuple(set(exclude_ids) | {_identifier(m) for m in self.maps if candidate_quality_ok(m)} - {None})
            self.status.update(state="loading", active_batch=pass_batches + 1, message=("Buscando automáticamente mapas adecuados para las misiones y su reserva…"
                                                        if demand else "Explorando el catálogo de osu!, también canciones antiguas, con buena valoración y suficientes partidas…"))
            self.next_retry_at = now + get_setting("discovery_retry_minutes") * 60
            revision = self.revision
            request_settings = dict(self.settings)

        def work():
            try:
                lower = max(.1, baseline - max(.7, max(get_setting("warmup_offset"), get_setting("recovery_drop")) + get_setting("star_tolerance_below")))
                upper = baseline + max(get_setting("star_tolerance_above") + get_setting("challenge_increment"), .4)
                effective_requirements = public_requirements(requirements)
                requested_lower = [item["min_stars"] for item in effective_requirements if item.get("min_stars") is not None]
                requested_upper = [item["max_stars"] for item in effective_requirements if item.get("max_stars") is not None]
                if requested_lower:
                    lower = max(.1, min(lower, min(requested_lower)))
                if requested_upper:
                    upper = max(upper, max(requested_upper))
                next_cursor, exhausted = None, False
                if demand or self.batch_fetcher is not None or self.fetcher is None:
                    if self.batch_fetcher is not None:
                        fetch = self.batch_fetcher
                    elif self.fetcher is None:
                        from osu_coach.integrations.discovery_source import fetch_candidate_batch
                        fetch = fetch_candidate_batch
                    else:
                        fetch = None
                    if fetch is None:
                        batch = {"maps": self.fetcher(lower, upper), "next_cursor": None, "exhausted": True}
                    else:
                        batch = fetch(lower, upper, cursor=cursor, exclude_ids=excluded, requirements=effective_requirements,
                                      **({"excluded_songs": excluded_songs} if excluded_songs else {}))
                    if (not isinstance(batch, dict) or not _valid_maps(batch.get("maps"))
                            or not isinstance(batch.get("exhausted"), bool)):
                        raise ValueError("La búsqueda devolvió un lote inválido.")
                    maps, next_cursor, exhausted = batch["maps"], batch.get("next_cursor"), batch["exhausted"]
                    json.dumps(next_cursor)
                    if not exhausted and next_cursor is None:
                        raise ValueError("La búsqueda no indicó cómo continuar el recorrido.")
                else:
                    if self.fetcher is None:
                        from osu_coach.integrations.discovery_source import fetch_candidates
                        fetch = fetch_candidates
                    else:
                        fetch = self.fetcher
                    maps = fetch(lower, upper)
                if not _valid_maps(maps):
                    raise ValueError("La búsqueda devolvió mapas inválidos.")
                maps = copy.deepcopy([m for m in maps if candidate_quality_ok(m)])
                if self.stop.is_set():
                    return
                fetched = time.time()
                with self.lock:
                    if revision != self.revision:
                        return
                    # Keep earlier useful candidates while visiting later pages.
                    merged = {_identifier(m): m for m in self.maps if _identifier(m) is not None and candidate_quality_ok(m)}
                    merged.update({_identifier(m): m for m in maps if _identifier(m) is not None})
                    combined = list(merged.values())[-2000:]
                    if demand or self.batch_fetcher is not None or self.fetcher is None:
                        completed = pass_batches + 1 if demand else 0
                        fast_continue = bool(demand and get_setting("discovery_enabled") and not exhausted and completed < pass_limit)
                        delay = (EXHAUSTED_RETRY_DELAY if exhausted else MIN_REQUEST_INTERVAL if fast_continue
                                 else get_setting("discovery_retry_minutes") * 60)
                        retry = fetched + delay
                        search = {"profile": identity, "baseline": baseline if restart else previous_base, "cursor": next_cursor,
                                  "exhausted": exhausted, "requirements": requirements if restart else previous, "next_retry_at": retry,
                                  "pass_batches": completed, "fast_continue": fast_continue}
                    else:
                        search = self.search
                    value = {"version": 1, "fetched_epoch": fetched, "baseline": baseline,
                             "maps": combined, "search": search, "quality_policy": self.quality_policy()}
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temp = self.path.with_suffix(".tmp")
                    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                    temp.replace(self.path)
                    self.maps, self.fetched_epoch, self.baseline = combined, fetched, baseline
                    if demand or self.batch_fetcher is not None or self.fetcher is None:
                        self.search, self.demand_retry_at, self.next_retry_at = search, retry, retry
                        found = "Se encontró 1 dificultad candidata." if len(maps) == 1 else f"Se encontraron {len(maps)} dificultades candidatas."
                        message = (found + " Las etapas se rellenan automáticamente si cumplen sus límites."
                                   if maps else "Este lote no aportó dificultades adecuadas. La búsqueda continuará automáticamente.")
                        if exhausted:
                            message += " Se llegó al final de la fuente consultada; se volverá a buscar en una hora si siguen faltando mapas."
                    else:
                        message = (f"Búsqueda actualizada: {len(maps)} dificultades candidatas. Se comprueban los límites de cada etapa antes de recomendarlas."
                                   if maps else "Las páginas consultadas todavía no aportaron mapas con buena valoración y suficientes partidas registradas en tu rango. La búsqueda se ampliará automáticamente si faltan misiones.")
                    self.status.update(state="ready", message=message)
                    self._save_retry(clear=True)
            except Exception as error:
                with self.lock:
                    if revision != self.revision:
                        return
                    self.next_retry_at = time.time() + ERROR_RETRY_DELAY
                    self.status.update(state="error", message="No se pudo actualizar la búsqueda. Se conservan los mapas encontrados antes y se reintentará dentro de 15 minutos. " + str(error)[:140])
                    self._save_retry()

        def worker():
            try:
                with settings_context(request_settings):
                    work()
            finally:
                with self.lock:
                    if revision != self.revision:
                        self.status.update(state="ready", message="Ajustes actualizados. La próxima búsqueda usará tus nuevos criterios.")
                self.changed.set()

        self.thread = threading.Thread(target=worker, daemon=True, name="map-discovery")
        self.thread.start()
        return True
