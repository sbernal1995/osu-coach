"""Local osu! training companion. Run: python app.py [--demo] [--no-browser]."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import webbrowser

from engine import assess, recommend, number, apply_player_profile, timestamp, physical_limits
from player_profile import build_player_profile
from quest_store import QuestStore, scope_key, map_tokens, is_remote
from progress_store import ProgressStore
from discovery_store import DiscoveryStore
from discovery_source import candidate_quality_ok
from tag_analysis import analyze_tags, skill_tags
from tag_store import TagStore
from telemetry import TosuTracker, read_snapshot
from map_search import search_details
from played_history import PlayedHistory
from settings import validate_settings, settings_snapshot, coach_settings, get_setting, DEFAULTS

ROOT = Path(__file__).resolve().parent
DEFAULT_MOD_KEY = '{"mods":[],"rate":1.0}'


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_json(path, fallback):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise RuntimeError(f"Revisá el archivo local {path.name}: no se pudo leer.")
    return fallback


def detect_maps():
    roaming = Path(os.environ.get("APPDATA", str(Path.home() / ".local/share")))
    lazer = roaming / "osu" / "files"
    stable = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "osu!" / "Songs"
    return str(lazer if lazer.is_dir() else stable)


def profile_key(play):
    return json.dumps([str(play.get("player", "Local")).casefold(), play.get("client", "unknown"),
                       play.get("mode", 0), play.get("mod_key", DEFAULT_MOD_KEY)], separators=(",", ":"))


class Coach:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.token = secrets.token_urlsafe(32)
        self.data = Path(args.data_dir).resolve() if args.data_dir else ROOT / "data" / ("demo" if args.demo else "live")
        self.data.mkdir(parents=True, exist_ok=True)
        self.tag_store = TagStore(self.data, stop=self.stop)
        self.background_enabled = False
        self.config_path = self.data / "config.json"
        self.config = load_json(self.config_path, {"since": utcnow(), "maps_path": detect_maps(), "initial_stars": 2.5})
        if args.maps:
            self.config["maps_path"] = str(Path(args.maps).expanduser().resolve())
        self.settings = validate_settings(self.config.get("settings", {"initial_stars": self.config.get("initial_stars", 2.5)}))
        self.config["settings"] = dict(self.settings)
        self.config["initial_stars"] = self.settings["initial_stars"]
        save_json(self.config_path, self.config)
        self.discovery_store = DiscoveryStore(self.data, stop=self.stop, settings=self.settings)
        self.db = sqlite3.connect(self.data / "coach.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS plays (id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL)")
        self.quest_store = QuestStore(self.db)
        self.progress_store = ProgressStore(self.db)
        self.db.commit()
        self.catalog = []
        self.adjusted = {}
        self.scanning = False
        self.scan_count = 0
        self.scan_error = ""
        self.connection = {"ok": False, "message": "Esperando conexión con tosu…"}
        self.active = self.config.get("active")
        self.tracker = TosuTracker()
        self.last_snapshot = None
        self.child = None
        self.child_log = None
        if args.demo:
            from demo import demo_data
            self.catalog, samples = demo_data()
            for sample in samples:
                self.add_play(sample)
            self.config["since"] = samples[0]["played_at"]
            self.connection = {"ok": True, "message": "Demostración con partidas simuladas"}
        else:
            cache = load_json(self.data / "catalog.json", {})
            if isinstance(cache, dict) and cache.get("root") == self.config["maps_path"]:
                self.catalog = cache.get("maps", [])
        if self.active:
            with self.db:
                self.sync_progress(self.active)

    @coach_settings
    def add_play(self, play):
        if play.get("mode", 0) != 0:
            return
        with self.lock:
            # Complete metadata from the matching local beatmap when tosu lacks it.
            beatmap = next((m for m in self.catalog if m["key"] == play.get("beatmap_key")
                            or (m.get("id", 0) > 0 and m.get("id") == play.get("beatmap_id"))), None)
            if beatmap:
                if not play.get("beatmap_key"):
                    play["beatmap_key"] = beatmap["key"]
                for field in ("title", "artist", "version"):
                    if not play.get(field):
                        play[field] = beatmap.get(field)
            status = "pending" if play.get("needs_confirmation") else "accepted"
            with self.db:
                inserted = self.db.execute("INSERT OR IGNORE INTO plays VALUES (?, ?, ?)",
                                          (play["id"], json.dumps(play, ensure_ascii=False), status)).rowcount
                if inserted and status == "accepted":
                    self.quest_store.record_play(scope_key(profile_key(play), self.config["since"]), play)
                    self.sync_progress(profile_key(play), observed_id=play["id"])
            if inserted and status == "accepted":
                self.active = profile_key(play)
                self.config["active"] = self.active
                save_json(self.config_path, self.config)
                if self.background_enabled:
                    self.sync_tags()

    def plays(self, status="accepted", *, limit=500):
        query = "SELECT data FROM plays WHERE status=? ORDER BY rowid DESC"
        params = (status,) if limit is None else (status, limit)
        if limit is not None:
            query += " LIMIT ?"
        return [json.loads(row[0]) for row in self.db.execute(query, params)]

    def training_plays(self):
        # Other profiles and late confirmations must not displace this player's
        # 100 eligible results behind the old global 500-row display limit.
        return [play for row in self.db.execute("SELECT data FROM plays WHERE status='accepted' ORDER BY rowid DESC")
                if profile_key(play := json.loads(row[0])) == self.active]

    @coach_settings
    def sync_progress(self, active, observed_id=None):
        # Historical points use the complete accepted log for this profile.
        # Their existence never changes the short window used for recommendations.
        plays = [play for row in self.db.execute("SELECT data FROM plays WHERE status='accepted'")
                 if profile_key(play := json.loads(row[0])) == active]
        self.progress_store.sync(active, self.config["since"], plays,
                                 initial=self.config.get("initial_stars", 2.5), observed_id=observed_id)

    @coach_settings
    def confirm(self, identifier, accept):
        with self.lock:
            row = self.db.execute("SELECT data FROM plays WHERE id=? AND status='pending'", (identifier,)).fetchone()
            if not row:
                raise ValueError("Esa partida ya fue revisada.")
            play = json.loads(row[0])
            play["needs_confirmation"] = False
            play["evidence"] = "user_confirmed" if accept else "user_rejected"
            with self.db:
                self.db.execute("UPDATE plays SET data=?, status=? WHERE id=?", (json.dumps(play), "accepted" if accept else "rejected", identifier))
                if accept:
                    self.quest_store.record_play(scope_key(profile_key(play), self.config["since"]), play)
                    self.sync_progress(profile_key(play), observed_id=play["id"])
            if accept:
                self.active = profile_key(play)
                self.config["active"] = self.active
                save_json(self.config_path, self.config)
                if self.background_enabled:
                    self.sync_tags()

    @coach_settings
    def reset(self):
        with self.lock:
            self.config["since"] = utcnow()
            save_json(self.config_path, self.config)
            with self.db:
                self.db.execute("UPDATE plays SET status='rejected' WHERE status='pending'")
                self.quest_store.archive_all()
            self.tracker = TosuTracker()

    @coach_settings
    def new_quests(self, board_id):
        with self.lock:
            if not self.active:
                raise ValueError("Jugá una primera partida para preparar misiones para tu perfil.")
            state = self.state(ensure_quests=False)
            with self.db:
                return self.quest_store.replace(scope_key(self.active, self.config["since"]),
                    state["profile_label"], state["recommendations"], board_id)

    def quest_replacements(self, scope, board, maps, profile, analysis, player):
        visible = [quest["map"] for group in board["groups"] for quest in group["quests"]]
        occupied = PlayedHistory([], catalog=self.catalog + maps, completed=visible)
        # maps has already been filtered against the complete played history.
        available = [beatmap for beatmap in maps if not occupied.contains(beatmap)]
        replacements = []
        for group in board["groups"]:
            if len(group["quests"]) >= 3 and not any(quest["status"] in {"completed", "skipped"} for quest in group["quests"]):
                continue
            candidates = recommend(available, profile, limit=12, tag_analysis=analysis, player_profile=player,
                                   stages={group["stage"]}, fill_online=True)[0]
            local_count = sum(not is_remote(q["map"]) for q in group["quests"] if q["status"] in {"pending", "in_progress"})
            local_count += sum(not is_remote(m) for m in candidates["maps"])
            candidates["max_online"] = max(1, 3 - local_count)
            replacements.append(candidates)
        return replacements

    @staticmethod
    def installed_quest_map(beatmap, local_keys, local_ids):
        installed = local_keys.get(str(beatmap.get("key")))
        if installed is None and is_remote(beatmap):
            installed = local_ids.get(int(number(beatmap.get("id"))))
        return installed

    @staticmethod
    def download_evidence(beatmap, popularity):
        current = next((popularity[token] for token in map_tokens(beatmap) if token in popularity), None)
        return {**beatmap, "popularity": current} if current is not None else beatmap

    def quest_is_protected(self, quest, pending):
        assigned = timestamp(quest.get("created_at"))
        if not assigned:
            return True
        tokens = map_tokens(quest["map"])
        snapshot = self.last_snapshot or {}
        state = snapshot.get("state") or {}
        if state.get("number") == 2 or state.get("name") == "play":
            beatmap = snapshot.get("beatmap") or {}
            current = map_tokens({"key": beatmap.get("checksum"), "id": beatmap.get("id")})
            if not current or tokens & current:
                return True
        for play in pending:
            if profile_key(play) != self.active:
                continue
            played = timestamp(play.get("played_at"))
            if (played and played >= assigned
                    and tokens & map_tokens({"key": play.get("beatmap_key"), "id": play.get("beatmap_id")})):
                return True
        return False

    def can_skip_played_quest(self, quest, history, pending):
        return (not self.quest_is_protected(quest, pending)
                and history.contains(quest["map"], before=quest.get("created_at")))

    def can_skip_download_quality(self, quest, pending, local_keys, local_ids, popularity):
        beatmap = quest["map"]
        return (is_remote(beatmap)
                and self.installed_quest_map(beatmap, local_keys, local_ids) is None
                and not candidate_quality_ok(self.download_evidence(beatmap, popularity))
                and not self.quest_is_protected(quest, pending))

    def rescan(self):
        with self.lock:
            if self.scanning or self.args.demo:
                return
            self.scanning, self.scan_count, self.scan_error = True, 0, ""
        def work():
            try:
                from catalog import scan_catalog
                root = self.config["maps_path"]
                if not Path(root).is_dir():
                    raise ValueError("Elegí la carpeta de mapas con --maps. No encontré la carpeta habitual de osu!.")
                def progress(count):
                    self.scan_count = count
                result = scan_catalog(root, progress=progress)
                save_json(self.data / "catalog.json", {"root": root, "created_at": utcnow(), "calculator": "rosu-pp-py 4.0.2", "maps": result})
                with self.lock:
                    self.catalog = result
                    self.adjusted.clear()
            except Exception as error:
                with self.lock:
                    self.scan_error = f"No pude actualizar los mapas: {error}"
            finally:
                with self.lock:
                    self.scanning = False
                if self.background_enabled:
                    self.sync_tags()
        threading.Thread(target=work, daemon=True, name="map-catalog").start()

    @coach_settings
    def sync_tags(self, force=False):
        if self.args.demo:
            return
        with self.lock:
            active = self.training_plays()
            profile = assess(active, since=self.config["since"], initial=self.config.get("initial_stars", 2.5))
            maps, _ = self.catalog_for(active[0] if active else None)
            if not maps:
                # Keep exact played map identities while adjusted stars are pending.
                maps = [{**m, "stars": -10} for m in self.catalog]
            self.tag_store.sync(maps, profile["window"], profile["baseline"], force=force)

    @coach_settings
    def sync_discovery(self, force=False):
        if self.args.demo:
            return
        with self.lock:
            active = self.training_plays()
            profile = assess(active, since=self.config["since"], initial=self.config.get("initial_stars", 2.5))
            self.discovery_store.sync(profile["baseline"], active[0] if active else None, force=force)

    def discover_periodically(self):
        while not self.stop.is_set():
            state = self.state()
            if not state["discovery"]["needs"]:
                self.sync_discovery()
            self.stop.wait(60)

    def start_tosu(self):
        if self.args.demo or self.args.no_tosu:
            return
        try:
            read_snapshot(self.args.tosu_url, timeout=.5)
            return
        except Exception:
            pass
        executable = ROOT / "vendor" / "tosu" / "tosu.exe"
        if executable.exists() and os.name == "nt":
            self.child_log = (self.data / "tosu.log").open("a", encoding="utf-8")
            self.child = subprocess.Popen([str(executable)], cwd=executable.parent,
                                         creationflags=subprocess.CREATE_NO_WINDOW,
                                         stdout=self.child_log, stderr=subprocess.STDOUT)

    def poll(self):
        while not self.stop.is_set():
            try:
                snapshot = read_snapshot(self.args.tosu_url)
                with self.lock:
                    self.last_snapshot = snapshot
                    self.connection = {"ok": True, "message": "Conectado a tosu. Esperando una partida nueva de osu!standard."}
                    play = self.tracker.feed(snapshot)
                    if play:
                        self.add_play(play)
                        self.connection["message"] = "Partida recibida. Revisá tu próxima práctica."
            except Exception as error:
                with self.lock:
                    self.connection = {"ok": False, "message": "Abrí osu! y tosu para detectar las partidas. " + str(error)[:180]}
                    # Reconnect starts fresh, so a result opened during downtime is not imported.
                    self.tracker = TosuTracker()
            self.stop.wait(1)

    def catalog_for(self, sample):
        if not sample:
            return self.catalog, ""
        settings = json.loads(sample.get("mod_key", "{}"))
        rate = settings.get("rate")
        lazer = sample.get("client") == "lazer"
        if not sample.get("mods") and lazer and rate in (None, 1):
            return self.catalog, ""
        key = str(sample.get("client")) + ":" + sample.get("mod_key", "")
        unsupported = [m for m in sample.get("mods", []) if isinstance(m, dict)
                       and m.get("acronym") in {"DA", "WU", "WD", "AS"} and not m.get("settings")]
        if unsupported:
            return [], "tosu no entregó los ajustes de estos mods. Jugá Sin mods para obtener recomendaciones comparables."
        if key in self.adjusted:
            result = self.adjusted[key]
            if result is None:
                return [], "Calculando la dificultad de tus mapas con estos mods…"
            return result, ""
        self.adjusted[key] = None
        catalog_reference = self.catalog
        maps = copy.deepcopy(self.catalog)
        def adjust():
            from catalog import difficulty_for
            changed = []
            for beatmap in maps:
                if self.stop.is_set():
                    return
                try:
                    stats = difficulty_for(beatmap["path"], mods=sample["mods"], lazer=lazer, clock_rate=rate)
                    beatmap.update(stats)
                    changed.append(beatmap)
                except Exception:
                    continue
            with self.lock:
                if self.catalog is catalog_reference:
                    self.adjusted[key] = changed
                    if self.background_enabled:
                        self.sync_tags()
        threading.Thread(target=adjust, daemon=True, name="mod-difficulty").start()
        return [], "Calculando la dificultad de tus mapas con estos mods…"

    @coach_settings
    def state(self, *, ensure_quests=True):
        with self.lock:
            active = self.training_plays()
            sample = active[0] if active else None
            profile = assess(active, since=self.config["since"], initial=self.config.get("initial_stars", 2.5))
            maps, mod_warning = self.catalog_for(sample)
            maps = list(maps) + self.discovery_store.candidates(self.catalog, sample)
            local_keys = {str(m["key"]): m for m in self.catalog if m.get("key")}
            local_ids = {int(number(m["id"])): m for m in self.catalog if number(m.get("id")) > 0}
            with self.discovery_store.lock:
                quality_maps = copy.deepcopy(self.discovery_store.maps)
            popularity = {token: beatmap["popularity"] for beatmap in quality_maps
                          if isinstance(beatmap.get("popularity"), dict) for token in map_tokens(beatmap)}
            maps = [beatmap for beatmap in maps
                    if not is_remote(beatmap)
                    or self.installed_quest_map(beatmap, local_keys, local_ids) is not None
                    or candidate_quality_ok(self.download_evidence(beatmap, popularity))]
            tagged_catalog = self.tag_store.enrich(maps)
            tagged_plays = self.tag_store.enrich(profile["window"])
            analysis = analyze_tags(tagged_plays, tagged_catalog, profile["baseline"], now=profile["evaluated_at"])
            player = build_player_profile(profile, analysis)
            profile = apply_player_profile(profile, player)
            maps = self.tag_store.enrich(maps)
            for beatmap in maps:
                names = {tag["name"] for tag in skill_tags(beatmap)}
                beatmap["tags"] = [tag for tag in beatmap.get("tags", [])
                                   if isinstance(tag, dict) and " ".join(str(tag.get("name", "")).casefold().split()) in names]
            scope = scope_key(self.active, self.config["since"]) if self.active else None
            completed_history = self.quest_store.completions(scope, limit=None)["items"] if scope else []
            history = PlayedHistory(active, catalog=self.catalog + maps, completed=completed_history)
            unplayed = [beatmap for beatmap in maps if not history.contains(beatmap)]
            groups = recommend(unplayed, profile, tag_analysis=analysis, player_profile=player, fill_online=True)
            for group in groups:
                if not group["maps"]:
                    group["empty_reason"] = "no_unplayed_maps_in_range"
            coach_progress = self.progress_store.snapshot(self.active, self.config["since"], profile)
            if sample:
                mods = " + ".join(m["acronym"] if isinstance(m, dict) else str(m) for m in sample.get("mods", [])) or "Sin mods"
                profile_label = f"{sample.get('player', 'Local')} · {sample.get('client', 'osu!')} · {mods}"
                try:
                    settings = json.loads(sample.get("mod_key", "{}"))
                    rate = settings.get("rate")
                    if rate and rate != 1:
                        profile_label += f" · velocidad ×{rate}"
                except ValueError:
                    pass
            else:
                profile_label = "Perfil nuevo · osu!standard · Sin mods"
            quest_board, quest_history, quest_completions = None, [], {"total": 0, "items": []}
            quest_skips = {"total": 0, "items": []}
            pending = self.plays("pending", limit=None)
            if self.active:
                def skip_reason(quest):
                    if self.can_skip_played_quest(quest, history, pending):
                        return "played_before_assignment"
                    if self.can_skip_download_quality(quest, pending, local_keys, local_ids, popularity):
                        return "download_quality"
                    return None
                with self.db:
                    quest_board = (self.quest_store.ensure(scope, profile_label, groups,
                        replacement_provider=lambda board: self.quest_replacements(scope, board, unplayed, profile, analysis, player),
                        skip_predicate=skip_reason) if ensure_quests
                                   else self.quest_store.current(scope))
                quest_history = self.quest_store.history(scope)
                quest_completions = self.quest_store.completions(scope)
                quest_skips = self.quest_store.skips(scope)
            needs = []
            ceilings = physical_limits(profile)
            for group in groups:
                assigned = next((g for g in (quest_board or {}).get("groups", []) if g["stage"] == group["stage"]), None)
                count = (sum(q["status"] in {"pending", "in_progress"} for q in assigned["quests"])
                         if assigned is not None else len(group["maps"]))
                if count < 3:
                    needs.append({"stage": group["stage"], "label": group["label"], "target": group["target"],
                                  "missing": 3 - count, "min_stars": max(.1, group["target"] - get_setting("star_tolerance_below")),
                                  "max_stars": group["target"] + get_setting("star_tolerance_above"),
                                  **{"max_" + field: limit for field, limit in ceilings.items()}})
            if (needs and self.background_enabled and not self.args.demo
                    and not (self.scanning and not self.catalog)):
                excluded = {int(number(m.get("id"))) for m in self.catalog}
                excluded.update(int(number(p.get("beatmap_id"))) for p in active)
                excluded.update(int(number(q.get("map", {}).get("id"))) for q in completed_history)
                self.discovery_store.sync(profile["baseline"], sample, needs=needs, exclude_ids=excluded - {0})
            discovery = self.discovery_store.snapshot(self.catalog, sample, needs=needs)
            quest_availability = {}
            if quest_board:
                for group in quest_board["groups"]:
                    for quest in group["quests"]:
                        beatmap = quest["map"]
                        local_map = self.installed_quest_map(beatmap, local_keys, local_ids)
                        # Refresh only search/display metadata; the stored quest and
                        # its exact revision, targets and attempt feedback stay frozen.
                        lookup = dict(beatmap)
                        if local_map is not None:
                            for field in ("title", "artist", "version", "creator",
                                          "title_romanized", "artist_romanized"):
                                if local_map.get(field):
                                    lookup[field] = local_map[field]
                        quest_availability[quest["id"]] = {
                            "installed": local_map is not None, **search_details(lookup),
                            "popularity": copy.deepcopy(self.download_evidence(beatmap, popularity).get("popularity"))}
            recent = list(reversed(profile.pop("session_window")))
            profile.pop("window")
            warnings = [m for m in [self.scan_error, mod_warning] if m]
            if not self.catalog and not self.scanning:
                warnings.append("Todavía no hay mapas locales disponibles.")
            if sample and sample.get("mods") and not maps and not mod_warning:
                warnings.append("Estos mods todavía no se pudieron calcular. Elegí Sin mods para comenzar una calibración nueva.")
            for item in pending:
                item["reason"] = item.get("uncertain_reason", "Confirmá que acabás de jugar esta partida y que corresponde a tu entrenamiento.")
            return {"app": "osu-coach", "token": self.token, "demo": self.args.demo,
                    "settings": settings_snapshot(self.settings),
                    "connection": dict(self.connection), "scanning": self.scanning,
                    "scan_count": self.scan_count, "catalog_count": len(self.catalog),
                    "mode_label": "osu!standard", "profile_label": profile_label,
                    "profile": profile, "player_profile": player, "recommendations": groups, "recent": recent,
                    "quest_board": quest_board, "quest_history": quest_history,
                    "quest_completions": quest_completions,
                    "quest_skips": quest_skips,
                    "quest_availability": quest_availability,
                    "recommendation_policy": {"mode": "unplayed", "unit": "difficulty", "history_plays": len(active),
                        "message": "Las nuevas misiones evitan dificultades que ya jugaste. "
                                   "Pueden incluir otras dificultades de la misma canción. "
                                   "Las misiones en práctica conservan sus metas."},
                    "coach_progress": coach_progress,
                    "tag_analysis": analysis, "tag_sync": self.tag_store.snapshot(self.catalog),
                    "discovery": discovery,
                    "pending": pending[:500], "warnings": warnings}

    def update_settings(self, values=None, *, reset=False):
        with self.lock:
            if type(reset) is not bool or (reset and values is not None):
                raise ValueError("Indicá valores nuevos o restaurar los valores iniciales.")
            updated = validate_settings({} if reset else values, base=None if reset else self.settings)
            config = {**self.config, "settings": dict(updated), "initial_stars": updated["initial_stars"]}
            # Persist before publishing any state; a failed write leaves all settings unchanged.
            save_json(self.config_path, config)
            self.config, self.settings = config, updated
            self.discovery_store.set_settings(updated)
            return settings_snapshot(updated)

    def close(self):
        self.stop.set()
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.child.kill()
        if self.child_log:
            self.child_log.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "OsuCoach/1.0"

    def log_message(self, format, *args):
        pass

    def local_request(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.local_request():
            return self.send(403, {"error": "Acceso local requerido."})
        path = urlsplit(self.path).path
        if path == "/api/state":
            return self.send(200, self.server.coach.state())
        if path == "/":
            return self.send(200, (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
        return self.send(404, {"error": "Ruta desconocida."})

    def do_POST(self):
        if not self.local_request() or not secrets.compare_digest(self.headers.get("X-Coach-Token", ""), self.server.coach.token):
            return self.send(403, {"error": "Recargá el panel e intentá de nuevo."})
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            return self.send(403, {"error": "Origen inválido."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("Solicitud demasiado grande o vacía.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Se esperaba un objeto JSON.")
            if self.path == "/api/settings":
                if set(body) - {"values", "reset"}:
                    raise ValueError("Campos de configuración desconocidos.")
                result = self.server.coach.update_settings(body.get("values"), reset=body.get("reset", False))
                return self.send(200, {"ok": True, "settings": result})
            elif self.path == "/api/reset":
                self.server.coach.reset()
            elif self.path == "/api/rescan":
                self.server.coach.rescan()
            elif self.path == "/api/tags/sync":
                self.server.coach.sync_tags(force=True)
            elif self.path == "/api/discovery/sync":
                self.server.coach.sync_discovery(force=True)
            elif self.path == "/api/quests/new":
                if "board_id" not in body or (body["board_id"] is not None and not isinstance(body["board_id"], str)):
                    raise ValueError("Indicá la tanda que querés renovar.")
                self.server.coach.new_quests(body["board_id"])
            elif self.path == "/api/confirm":
                if not isinstance(body.get("accept"), bool):
                    raise ValueError("Indicá si la partida fue tuya.")
                self.server.coach.confirm(str(body.get("id", "")), body["accept"])
            elif self.path == "/api/stop":
                self.send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                return self.send(404, {"error": "Ruta desconocida."})
            return self.send(200, {"ok": True})
        except (ValueError, TypeError) as error:
            return self.send(400, {"error": str(error)})
        except OSError:
            return self.send(500, {"error": "No se pudieron guardar los cambios. Revisá el espacio disponible y los permisos de la carpeta del coach."})


def main():
    parser = argparse.ArgumentParser(description="Entrenador local de osu!standard basado en tus partidas nuevas.")
    parser.add_argument("--demo", action="store_true", help="Mostrar partidas simuladas en una base separada")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tosu", action="store_true", help="Usar una instancia de tosu ya abierta")
    parser.add_argument("--tosu-url", default="http://127.0.0.1:24050/json/v2")
    parser.add_argument("--maps", help="Carpeta Songs (stable) o files (lazer)")
    parser.add_argument("--data-dir", help="Carpeta propia para datos del entrenador")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # Reopening the launcher should return to the existing panel.
    if not args.maps and not args.data_dir:
        try:
            with urlopen(f"http://127.0.0.1:{args.port}/api/state", timeout=1) as response:
                running = json.loads(response.read(2 * 1024 * 1024))
            if running.get("app") == "osu-coach" and running.get("demo") == args.demo:
                if not args.no_browser:
                    webbrowser.open(f"http://127.0.0.1:{args.port}")
                print("El entrenador ya está abierto.")
                return
        except Exception:
            pass
    coach = Coach(args)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        coach.close()
        parser.exit(1, f"El puerto {args.port} está ocupado. Elegí otro con --port.\n")
    server.coach = coach
    coach.start_tosu()
    if not args.demo:
        coach.background_enabled = True
        coach.sync_tags()
        coach.rescan()
        threading.Thread(target=coach.poll, daemon=True, name="tosu-reader").start()
        threading.Thread(target=coach.discover_periodically, daemon=True, name="discovery-schedule").start()
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"osu! coach: {url}\nCerrá esta ventana o presioná Ctrl+C para terminar.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        coach.close()
        server.server_close()


if __name__ == "__main__":
    main()
