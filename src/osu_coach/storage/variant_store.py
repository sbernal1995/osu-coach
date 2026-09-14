"""Background, persistent exact difficulty calculations for recommendation mods."""
from collections import OrderedDict
import copy
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from osu_coach.core.mod_policy import context, identity, stamp
from osu_coach.integrations.lazer_calculator import CALCULATOR_ID
from osu_coach.beatmaps.catalog import difficulty_for, _metadata, MAX_MAP_BYTES


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class VariantStore:
    def __init__(self, directory, stop, calculate=None):
        self.directory = Path(directory)
        self.path = self.directory / "mod-difficulties.json"
        self.stop = stop
        self.calculate = calculate or self._calculate
        self.lock = threading.RLock()
        self.cache = {}
        self.pending = OrderedDict()
        self.failures = {}
        self.thread = None
        self.error = ""
        self.in_flight = None
        self.download_retry = {}
        self.next_download_at = 0
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("calculator") == CALCULATOR_ID:
                entries = saved["entries"]
                self.cache = {key: item for key, item in entries.items() if isinstance(item, dict)
                              and isinstance(item.get("stats"), dict) and item["stats"].get("calculator") == CALCULATOR_ID
                              and isinstance(item["stats"].get("stars"), (int, float))
                              and math.isfinite(item["stats"]["stars"]) and item["stats"]["stars"] > 0
                              and isinstance(item.get("calculated_at"), (int, float))}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    @staticmethod
    def key(beatmap, conditions):
        value = [CALCULATOR_ID, beatmap["key"], beatmap.get("updated_at"), conditions]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _calculate(self, beatmap, conditions):
        path = beatmap.get("path")
        if not path:
            identifier = int(beatmap["id"])
            if identifier <= 0:
                raise ValueError("El mapa no tiene un identificador válido.")
            path = self.directory / "map-files" / (str(identifier) + ".osu")
            if not path.exists() or time.time() - path.stat().st_mtime > 86400:
                now = time.time()
                if self.download_retry.get(identifier, 0) > now:
                    raise ValueError("La descarga de esta dificultad espera para reintentarse.")
                if self.stop.wait(max(0, self.next_download_at - now)):
                    raise RuntimeError("Cálculo detenido.")
                request = Request("https://osu.ppy.sh/osu/" + str(identifier),
                                  headers={"User-Agent": "osu-coach/0.1", "Accept": "text/plain"})
                try:
                    with build_opener(NoRedirect()).open(request, timeout=12) as response:
                        content = response.read(MAX_MAP_BYTES + 1)
                    if len(content) > MAX_MAP_BYTES or _metadata(content).get("id") != identifier:
                        raise ValueError("El archivo público no corresponde a la dificultad.")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_suffix(".tmp")
                    temp.write_bytes(content)
                    temp.replace(path)
                except Exception:
                    # All presets share the same file; a failure must not trigger
                    # another identical download for every different combination.
                    self.download_retry[identifier] = time.time() + 60
                    raise
                finally:
                    self.next_download_at = time.time() + 1
        stats = difficulty_for(path, conditions["mods"], lazer=conditions["client"] == "lazer",
                               clock_rate=conditions["rate"])
        return stats

    def variants(self, maps, choices):
        result, pending = [], OrderedDict()
        now = time.time()
        failed = 0
        with self.lock:
            for beatmap in maps:
                for conditions in choices:
                    key = self.key(beatmap, conditions)
                    # Local no-mod catalog already uses the current lazer formula;
                    # public no-mod candidates retain the official published stars.
                    plain = not conditions["mods"] and conditions["rate"] == 1
                    if plain and conditions["client"] == "lazer":
                        result.append(stamp(beatmap, conditions))
                        continue
                    item = self.cache.get(key)
                    if item and (beatmap.get("path") or now - item.get("calculated_at", 0) < 86400):
                        result.append(stamp({**beatmap, **item["stats"]}, conditions))
                    elif beatmap.get("path") or (beatmap.get("source") == "online" and beatmap.get("id")):
                        if self.failures.get(key, 0) > now:
                            failed += 1
                        elif key != self.in_flight:
                            pending[key] = (copy.deepcopy(beatmap), copy.deepcopy(conditions))
            self.pending = pending
            if self.pending and not self.stop.is_set() and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._work, daemon=True, name="recommendation-mods")
                self.thread.start()
            count = len(pending) + int(self.in_flight is not None)
            warning = (f"Calculando variantes con mods: quedan {count} combinaciones. Las opciones verificadas aparecen al terminar."
                       if count else ("Algunas variantes no se pudieron calcular. Se reintentarán automáticamente." if failed else self.error))
            return result, warning

    def _save(self):
        with self.lock:
            value = {"calculator": CALCULATOR_ID, "entries": self.cache}
            self.directory.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(value), encoding="utf-8")
            temp.replace(self.path)

    def _work(self):
        changed = 0
        while not self.stop.is_set():
            with self.lock:
                if not self.pending:
                    break
                key, (beatmap, conditions) = self.pending.popitem(last=False)
                self.in_flight = key
            try:
                stats = self.calculate(beatmap, conditions)
                if stats.get("calculator") != CALCULATOR_ID:
                    raise RuntimeError("El cálculo del mod no tiene una versión verificada.")
                with self.lock:
                    self.cache[key] = {"stats": stats, "calculated_at": time.time()}
                    self.error = ""
                changed += 1
                if changed % 32 == 0:
                    self._save()
            except Exception:
                with self.lock:
                    self.failures[key] = time.time() + 60
                    self.error = "Algunas variantes no se pudieron calcular. Se reintentarán; solo se recomiendan dificultades verificadas."
            finally:
                with self.lock:
                    self.in_flight = None
        if changed:
            try:
                self._save()
            except OSError:
                with self.lock:
                    self.error = "Los mods se calcularon, pero no se pudo guardar su caché."

    def close(self):
        if self.thread is not None:
            self.thread.join(timeout=3)
