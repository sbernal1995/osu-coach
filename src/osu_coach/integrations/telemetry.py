"""Read local tosu snapshots and keep only newly observed attempts.

Schema checked against the upstream implementation on 2026-09-08:
https://github.com/tosuapp/tosu/blob/master/packages/tosu/src/api/utils/buildResultV2.ts
https://github.com/tosuapp/tosu/blob/master/packages/tosu/src/memory/stable.ts
https://github.com/tosuapp/tosu/blob/master/packages/tosu/src/memory/lazer.ts
https://github.com/tosuapp/tosu/blob/master/packages/server/router/v2.ts

tosu exposes resultsScreen.createdAt in UTC, scoreId, accuracy as a percentage,
hits['0'], maxCombo, and beatmap.stats.stars.total. State 2 is gameplay and 7 is
the result screen. Results have no passed field: use observed failures and map
completion. play.failed itself means health <= 0, including No Fail gameplay.
If failure happens between polls, a freshly dated partial result that remains
identical for at least 0.75 seconds is treated as a failed attempt. This assumes
the settled result counters describe the finished attempt, not a loading frame;
changing counters restart that settling interval.

The current public v2 schema does NOT expose an authoritative replay flag.
replayUIVisible is only UI visibility and must not be used as that flag. Explicit
flags, watching status, player identity and score dates reject known replays;
an undated result or a failure without a result date needs user confirmation.
Such attempts are returned with needs_confirmation=True, never silently trusted.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import ipaddress
import json
import math
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from osu_coach.core.grades import normalize_grade


DEFAULT_URL = "http://127.0.0.1:24050/json/v2"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RESULT_SETTLE_SECONDS = 0.75
_ASSISTED = {"AT", "AUTOPLAY", "RX", "RELAX", "AP", "AUTOPILOT", "CN", "CINEMA"}
_LEGACY_MODS = {
    1: "NF", 2: "EZ", 4: "TD", 8: "HD", 16: "HR", 32: "SD", 64: "DT",
    128: "RX", 256: "HT", 512: "NC", 1024: "FL", 2048: "AT", 4096: "SO",
    8192: "AP", 16384: "PF", 32768: "4K", 65536: "5K", 131072: "6K",
    262144: "7K", 524288: "8K", 1048576: "FI", 2097152: "RD",
    4194304: "CN", 8388608: "TP", 16777216: "9K", 33554432: "CO",
    67108864: "1K", 134217728: "3K", 268435456: "2K",
    536870912: "V2", 1073741824: "MR",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("El servidor local no debe redirigir a otra dirección.")


def read_snapshot(url: str = DEFAULT_URL, timeout: float = 2) -> dict:
    """Read at most 2 MiB from a loopback URL; never follow HTTP redirects."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if (not local or parsed.scheme not in {"http", "https"}
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment):
        raise ValueError("La dirección de tosu debe ser local (127.0.0.1 o localhost).")
    request = Request(url, headers={"Accept": "application/json"})
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("La respuesta de tosu supera el límite de 2 MB.")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("tosu devolvió una respuesta con formato inesperado.")
    if value.get("error"):
        raise ValueError(f"tosu todavía no está listo: {value['error']}")
    return value


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value, minimum=None):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return None
    return number


def _integer(value, minimum=0):
    number = _number(value, minimum)
    return int(number) if number is not None and number.is_integer() else None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None
        return date.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _mode(score: dict):
    return _integer(_dict(score.get("mode")).get("number"))


def _hits(score: dict, mode: int):
    hits = _dict(score.get("hits"))
    fields = {
        0: ("300", "100", "50", "0"),
        1: ("300", "100", "0"),
        2: ("300", "100", "50", "katu", "0"),
        3: ("300", "100", "50", "geki", "katu", "0"),
    }.get(mode)
    if fields is None:
        return None
    values = [_integer(hits.get(key)) for key in fields]
    if any(value is None for value in values):
        return None
    return sum(values)


def _mods(score: dict):
    raw = score.get("mods")
    if not isinstance(raw, dict):
        return None
    result = []
    array = raw.get("array")
    if isinstance(array, list) and array:
        for item in array:
            if isinstance(item, str):
                mod = {"acronym": item.upper()}
            elif isinstance(item, dict) and isinstance(item.get("acronym"), str):
                mod = copy.deepcopy(item)
                mod["acronym"] = mod["acronym"].upper()
            else:
                return None
            if mod["acronym"] != "NM":
                result.append(mod)
    else:
        number = _integer(raw.get("number"))
        if number is not None:
            result = [{"acronym": name} for bit, name in _LEGACY_MODS.items() if number & bit]
            if number & 512:
                result = [mod for mod in result if mod["acronym"] != "DT"]
            if number & 16384:
                result = [mod for mod in result if mod["acronym"] != "SD"]
            unknown = number & ~sum(_LEGACY_MODS)
            if unknown:
                result.append({"acronym": f"LEGACY_{unknown}"})
        elif array == [] or str(raw.get("name", "")).upper() == "NM":
            result = []
        else:
            return None
    result.sort(key=lambda mod: (mod["acronym"], json.dumps(mod, sort_keys=True)))
    rate = _number(raw.get("rate"), minimum=0.01)
    # The key includes every lazer setting and rate, not just acronym/legacy bits.
    key = json.dumps({"mods": result, "rate": rate}, sort_keys=True, separators=(",", ":"))
    return result, key


def _key(beatmap: dict) -> str:
    checksum = beatmap.get("checksum")
    if isinstance(checksum, str) and checksum.strip():
        return checksum.strip().lower()
    map_id = _integer(beatmap.get("id"), minimum=1)
    return f"osu:{map_id}" if map_id else ""


def _blocked(snapshot: dict) -> bool:
    containers = [snapshot, _dict(snapshot.get("game")), _dict(snapshot.get("play"))]
    for container in containers:
        if any(container.get(flag) is True for flag in (
                "isWatchingReplay", "isReplay", "isSpectating", "spectating", "watchingReplay")):
            return True
    profile = _dict(snapshot.get("profile"))
    status = _dict(profile.get("banchoStatus"))
    if status.get("number") == 6 or str(status.get("name", "")).lower() in {
            "watching", "spectating", "watchingreplay"}:
        return True
    play = _dict(snapshot.get("play"))
    mods = _mods(play)
    if mods and any(mod["acronym"] in _ASSISTED for mod in mods[0]):
        return True
    own = str(profile.get("name") or "").strip().casefold()
    player = str(play.get("playerName") or "").strip().casefold()
    return bool(own and player and own != player)


class TosuTracker:
    """Convert transitions into one event per attempt, ignoring historical views.

    feed() is intentionally independent of networking and persistence. Every real
    attempt receives a UUID, so two identical new scores remain distinct. Replay
    ambiguity is exposed via needs_confirmation/evidence/uncertain_reason.
    """

    def __init__(self):
        self._attempt = None
        self._previous_state = None

    def _begin(self, snapshot: dict, now: float):
        self._attempt = {
            "id": str(uuid.uuid4()), "started": now,
            "snapshot": copy.deepcopy(snapshot), "last": now,
        }

    def feed(self, snapshot: dict, now: float | None = None) -> dict | None:
        if not isinstance(snapshot, dict):
            return None
        now = time.time() if now is None else now
        state = _integer(_dict(snapshot.get("state")).get("number"))
        if state is None or snapshot.get("error"):
            return None
        if _blocked(snapshot):
            self._attempt = None
            self._previous_state = state
            return None

        event = None
        if state == 2:
            if self._attempt is None or self._previous_state != 2:
                self._begin(snapshot, now)
            else:
                previous = self._attempt["snapshot"]
                old_map = _dict(previous.get("beatmap"))
                new_map = _dict(snapshot.get("beatmap"))
                old_play = _dict(previous.get("play"))
                new_play = _dict(snapshot.get("play"))
                old_hits = _hits(old_play, _mode(old_play))
                new_hits = _hits(new_play, _mode(new_play))
                old_time = _number(_dict(old_map.get("time")).get("live"))
                new_time = _number(_dict(new_map.get("time")).get("live"))
                rewound = (old_time is not None and new_time is not None
                           and old_time - new_time > 1500)
                restart = (_key(old_map) != _key(new_map) or
                           (old_hits is not None and new_hits is not None
                            and new_hits < old_hits and (rewound or new_hits == 0)))
                if restart:
                    if old_play.get("failed") is True:
                        event = self._finish(None, now)
                    self._begin(snapshot, now)
                else:
                    self._attempt["snapshot"] = copy.deepcopy(snapshot)
                    self._attempt["last"] = now
        elif state == 7 and self._attempt is not None:
            event = self._finish(snapshot, now)
            if event is not None:
                self._attempt = None
        elif self._attempt is not None:
            if _dict(self._attempt["snapshot"].get("play")).get("failed") is True:
                event = self._finish(None, now)
            self._attempt = None
        self._previous_state = state
        return event

    def _finish(self, result_snapshot: dict | None, now: float):
        attempt = self._attempt
        live = attempt["snapshot"]
        beatmap = _dict(live.get("beatmap"))
        play = _dict(live.get("play"))
        stats = _dict(beatmap.get("stats"))
        score = play if result_snapshot is None else _dict(result_snapshot.get("resultsScreen"))
        if result_snapshot is not None:
            result_map = _dict(result_snapshot.get("beatmap"))
            if _key(result_map) != _key(beatmap):
                return None
        mode = _mode(score)
        if mode not in {0, 1, 2, 3} or mode != _mode(play):
            return None
        hit_count = _hits(score, mode)
        if hit_count is None or hit_count <= 0:
            return None
        live_hits = _hits(play, mode)
        if result_snapshot is not None and live_hits is not None and hit_count < live_hits:
            return None  # tosu may briefly expose the preceding result.
        player = str(score.get("playerName") or score.get("name") or "").strip()
        if not player or player.casefold() != str(play.get("playerName") or "").strip().casefold():
            return None
        mods = _mods(score)
        if mods is None or any(mod["acronym"] in _ASSISTED for mod in mods[0]):
            return None
        live_mods = _mods(play)
        if live_mods is None or mods[1] != live_mods[1]:
            return None
        created = _date(score.get("createdAt")) if result_snapshot is not None else None
        if created is not None and (created < attempt["started"] - 2 or created > now + 120):
            return None

        accuracy = _number(score.get("accuracy"), minimum=0)
        stars = _number(_dict(stats.get("stars")).get("total"), minimum=0.001)
        misses = _integer(_dict(score.get("hits")).get("0"))
        max_combo = _integer(score.get("maxCombo") if result_snapshot is not None
                             else _dict(score.get("combo")).get("max"))
        map_max_combo = _integer(stats.get("maxCombo"), minimum=1)
        object_count = _integer(_dict(stats.get("objects")).get("total"), minimum=1)
        if (accuracy is None or accuracy > 100 or stars is None or misses is None
                or max_combo is None or map_max_combo is None or object_count is None
                or not _key(beatmap)):
            return None
        completion = min(1.0, hit_count / object_count)
        # A No Fail play can reach zero HP mid-map yet complete every object.
        passed = result_snapshot is not None and completion >= 1.0
        inferred_failure = False
        if result_snapshot is not None and not passed and play.get("failed") is not True:
            # The HP=0 frame can occur entirely between HTTP polls. A partial
            # result with a fresh score date is evidence of that failure once
            # its counters settle. Undated/stale data cannot use this inference.
            if created is None:
                return None
            signature = json.dumps({
                "date": created, "score_id": score.get("scoreId"),
                "score": score.get("score"), "hits": score.get("hits"),
                "accuracy": accuracy, "combo": max_combo, "mods": mods[1],
            }, sort_keys=True, separators=(",", ":"))
            candidate = attempt.get("partial_result")
            if candidate is None or candidate["signature"] != signature:
                attempt["partial_result"] = {"signature": signature, "since": now}
                return None
            if now - candidate["since"] < RESULT_SETTLE_SECONDS:
                return None
            inferred_failure = True
        timing = _dict(beatmap.get("time"))
        first = _number(timing.get("firstObject"), minimum=0)
        last = _number(timing.get("lastObject"), minimum=0)
        length = (last - first) / 1000 if first is not None and last is not None and last >= first else None
        uncertain_reason = None
        if created is None:
            uncertain_reason = ("tosu no aporta una fecha de resultado verificable para este intento; "
                                "confirmá que lo acabás de jugar y que no era una repetición.")
        event = {
            "id": attempt["id"], "played_at": _iso(created if created is not None else now),
            "player": player, "mode": mode, "beatmap_key": _key(beatmap),
            "beatmap_id": _integer(beatmap.get("id")) or 0,
            "stars": stars, "accuracy": accuracy, "misses": misses,
            "max_combo": max_combo, "map_max_combo": map_max_combo,
            "object_count": object_count, "judged_objects": hit_count, "passed": passed, "completion": completion,
            "mods": mods[0], "mod_key": mods[1],
            "bpm": _number(_dict(stats.get("bpm")).get("common"), minimum=0),
            "length": length,
            "ar": _number(_dict(stats.get("ar")).get("converted"), minimum=0),
            "od": _number(_dict(stats.get("od")).get("converted"), minimum=0),
            "cs": _number(_dict(stats.get("cs")).get("converted"), minimum=0),
            "title": str(beatmap.get("title") or ""), "artist": str(beatmap.get("artist") or ""),
            "version": str(beatmap.get("version") or ""), "source": "tosu",
            "client": live.get("client"),
            "score_id": _integer(score.get("scoreId")) if result_snapshot is not None else None,
            "needs_confirmation": created is None,
            "evidence": "dated_live_result" if created is not None else "uncertain",
            "failure_inferred_from_result": inferred_failure,
            "uncertain_reason": uncertain_reason,
        }
        # Preserve observed basic judgments for stable's grade rules. Missing
        # optional fields remain absent instead of becoming zero.
        observed_hits = _dict(score.get("hits"))
        for field, source_field in (("n300", "300"), ("n100", "100"), ("n50", "50")):
            value = _integer(observed_hits.get(source_field))
            if value is not None:
                event[field] = value
        observed_rank = score.get("rank") if result_snapshot is not None else _dict(score.get("rank")).get("current")
        grade = normalize_grade(observed_rank)
        if grade is not None:
            event["grade"] = grade
        # This records when this attempt was first observed, not an estimate
        # obtained by subtracting map time. Retries receive a fresh timestamp.
        started = _number(attempt.get("started"), minimum=0)
        if started is not None and started <= (created if created is not None else now):
            event["started_at"] = _iso(started)
        return event
