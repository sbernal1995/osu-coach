"""Discover osu!standard maps through bounded, unauthenticated public reads.

Verified on 2026-09-08 against the public endpoint and osu-web source:
https://github.com/ppy/osu-web/blob/master/app/Libraries/Search/BeatmapsetSearchRequestParams.php
https://github.com/ppy/osu-web/blob/master/app/Http/Controllers/BeatmapsetsController.php
https://github.com/ppy/osu-web/blob/master/app/Models/Beatmapset.php

Guest search ignores advanced filters and sorting. Therefore this client reads
at most three pages per batch of the public feed, resuming into older maps, and applies the star range,
mode and quality filters locally. It does not claim an exhaustive search.
At most eight matching sets receive a second read to verify rating vote counts.
Unverified set IDs remain queued in the opaque cursor for subsequent batches.
Ratings and play_count describe the whole set, not an individual difficulty.
Candidates require both a verified rating with votes and sufficient plays.
Ranked status alone never qualifies a map. No cookie or OAuth token is used.
The caller owns persistence, periodic refresh and library deduplication.
"""
from __future__ import annotations

from hashlib import sha256
from html.parser import HTMLParser
import json
import math
import threading
import time

from osu_coach.core.song_identity import song_tokens
from osu_coach.settings import get_setting
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


OFFICIAL_HOST = "osu.ppy.sh"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 3
MAX_VERIFIED_SETS = 8
MAX_SETS_PER_PAGE = 100
MAX_MAPS_PER_SET = 1024
MIN_REQUEST_INTERVAL = 1.1
MIN_RATING = 8.0
MIN_RATING_VOTES = 10
MIN_PLAY_COUNT = 10_000
SCOPE_NOTE = "Catálogo público de osu!: incluye mapas antiguos; la dificultad y la calidad se filtran en el coach."


class DiscoverySourceError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _integer(value, minimum=0):
    return value if type(value) is int and minimum <= value < 2**63 else None


def _number(value, minimum=0, maximum=math.inf):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and minimum <= value <= maximum else None
    except OverflowError:
        return None


def candidate_quality_ok(candidate) -> bool:
    """Validate cached set evidence; favourites and labels cannot qualify it."""
    if not isinstance(candidate, dict) or not isinstance(candidate.get("popularity"), dict):
        return False
    evidence = candidate["popularity"]
    rating = _number(evidence.get("rating"), get_setting("quality_min_rating"), 10)
    votes = _integer(evidence.get("votes"), get_setting("quality_min_votes"))
    plays = _integer(evidence.get("play_count"), get_setting("quality_min_plays"))
    if evidence.get("scope") != "beatmapset" or rating is None or votes is None or plays is None:
        return False
    if "rating_votes" in evidence and _integer(evidence["rating_votes"], get_setting("quality_min_votes")) != votes:
        return False
    return True


def _range(min_stars, max_stars):
    lower, upper = _number(min_stars, .01, 30), _number(max_stars, .01, 30)
    if lower is None or upper is None or lower > upper:
        raise ValueError("El rango de estrellas debe ser válido y estar ordenado.")
    return lower, upper


def _text(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _requirements(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("Los requisitos de las etapas deben ser una lista.")
    result = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Cada etapa debe indicar un rango de estrellas válido.")
        lower, upper = _range(item.get("min_stars"), item.get("max_stars"))
        requirement = {"min_stars": lower, "max_stars": upper}
        for key in ("max_bpm", "max_ar", "max_length", "min_length"):
            if item.get(key) is not None:
                value = _number(item[key])
                if value is None or value <= 0:
                    raise ValueError("Los límites de las etapas deben ser números positivos y finitos.")
                requirement[key] = value
        result.append(requirement)
    return result


def _eligible(item, excluded, requirements, excluded_songs=()):
    return item["id"] not in excluded and not song_tokens(item).intersection(excluded_songs) and (not requirements or any(
        requirement["min_stars"] <= item["stars"] <= requirement["max_stars"]
        and ("min_length" not in requirement or item["length"] >= requirement["min_length"])
        and all(item[field] <= requirement[limit] for field, limit in (
            ("bpm", "max_bpm"), ("ar", "max_ar"), ("length", "max_length")) if limit in requirement)
        for requirement in requirements))


def _position(value):
    if (isinstance(value, dict) and _integer(value.get("approved_date")) is not None
            and _integer(value.get("id"), 1) is not None):
        return (value["approved_date"], value["id"])
    return None


def _read_cursor(value):
    """The caller persists this versioned value without interpreting its fields."""
    if value is None:
        return {"version": 1, "page": 1, "cursor_string": None, "position": None, "last_page": None}
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("El cursor de mapas no es válido.")
    token, position, fingerprint = value.get("cursor_string"), value.get("position"), value.get("last_page")
    if (_integer(value.get("page"), 1) is None
            or (token is not None and (not isinstance(token, str) or not token or len(token) > 4096
                                      or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-=" for c in token)))
            or (position is not None and _position(position) is None)
            or (token is None) != (position is None)
            or (fingerprint is not None and (not isinstance(fingerprint, str) or len(fingerprint) != 64
                                            or any(c not in "0123456789abcdef" for c in fingerprint)))):
        raise ValueError("El cursor de mapas no es válido.")
    return {"version": 1, "page": value["page"], "cursor_string": token,
            "position": dict(position) if position is not None else None, "last_page": fingerprint}


def _page_cursor(payload, current, fingerprint):
    # Both page and searchAfter are public even though guests cannot use the
    # advanced search filters. Older envelopes without cursor fields use page.
    token, position = payload.get("cursor_string"), payload.get("cursor")
    if "cursor_string" not in payload and "cursor" not in payload:
        return {"version": 1, "page": current["page"] + 1, "cursor_string": None,
                "position": None, "last_page": fingerprint}
    if token is None and position is None:
        return None
    try:
        next_cursor = _read_cursor({"version": 1, "page": current["page"] + 1,
                                    "cursor_string": token, "position": position, "last_page": fingerprint})
    except ValueError as exc:
        raise DiscoverySourceError("La búsqueda pública devolvió un cursor inválido.") from exc
    previous_position = _position(current["position"])
    if (token == current["cursor_string"]
            or (previous_position is not None and _position(position) >= previous_position)):
        return None
    return next_cursor


def parse_search_page(payload: dict) -> list[dict]:
    """Validate the feed envelope without trusting its server-side filters."""
    if not isinstance(payload, dict) or payload.get("error"):
        raise DiscoverySourceError("La búsqueda pública devolvió un error.")
    sets = payload.get("beatmapsets")
    if not isinstance(sets, list) or len(sets) > MAX_SETS_PER_PAGE:
        raise DiscoverySourceError("La búsqueda pública cambió de formato.")
    if any(not isinstance(item, dict) or _integer(item.get("id"), 1) is None for item in sets):
        raise DiscoverySourceError("La búsqueda pública contiene un set inválido.")
    return sets


class _SetJSON(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.collecting = False
        self.found = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") == "json-beatmapset":
            if attrs.get("type") != "application/json":
                raise DiscoverySourceError("La página del mapa cambió de formato.")
            self.collecting = True
            self.found += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self.collecting = False

    def handle_data(self, data):
        if self.collecting:
            self.parts.append(data)


def parse_set_page(page: str, expected_set_id: int) -> dict:
    if _integer(expected_set_id, 1) is None:
        raise ValueError("El identificador del set debe ser un entero positivo.")
    if not isinstance(page, str) or len(page.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise DiscoverySourceError("La página del mapa supera el tamaño permitido.")
    parser = _SetJSON()
    parser.feed(page)
    parser.close()
    if parser.found != 1 or parser.collecting:
        raise DiscoverySourceError("La página del mapa carece de datos públicos verificables.")
    try:
        payload = json.loads("".join(parser.parts))
    except (ValueError, RecursionError) as exc:
        raise DiscoverySourceError("La página del mapa contiene datos inválidos.") from exc
    if (not isinstance(payload, dict) or _integer(payload.get("id"), 1) is None
            or payload.get("id") != expected_set_id):
        raise DiscoverySourceError("La página corresponde a otro set.")
    return payload


def popularity_evidence(beatmapset: dict) -> dict:
    """Require actual rating votes AND the set's public play count.

    osu-web exposes Beatmapset.play_count directly in its compact transformer:
    https://github.com/ppy/osu-web/blob/master/app/Transformers/BeatmapsetCompactTransformer.php
    This is set evidence; it is not a count of unique players or audio previews.
    """
    favourites = _integer(beatmapset.get("favourite_count"))
    play_count = _integer(beatmapset.get("play_count"))
    rating, votes = None, None
    histogram = beatmapset.get("ratings")
    if isinstance(histogram, list) and len(histogram) == 11 and all(_integer(n) is not None for n in histogram):
        votes = sum(histogram)
        if votes:
            rating = sum(value * count for value, count in enumerate(histogram)) / votes
    evidence = {"rating": rating, "rating_votes": votes, "votes": votes, "play_count": play_count,
                "favourites": favourites, "scope": "beatmapset", "source": "osu_public",
                "url": f"https://{OFFICIAL_HOST}/beatmapsets/{beatmapset.get('id')}"}
    qualified = candidate_quality_ok({"popularity": evidence})
    evidence["qualification"] = "rated" if qualified else None
    if qualified:
        rating_label = f"{rating:.2f}".replace(".", ",")
        votes_label = f"{votes:,}".replace(",", ".")
        plays_label = f"{play_count:,}".replace(",", ".")
        evidence["label"] = f"Set: {rating_label}/10 ({votes_label} votos) · {plays_label} reproducciones"
    else:
        evidence["label"] = "El set necesita buena valoración con suficientes votos y reproducciones"
    return evidence


def parse_candidates(beatmapset: dict, min_stars, max_stars, *, require_qualification=True) -> list[dict]:
    """Normalize only native standard difficulties with usable physical data."""
    lower, upper = _range(min_stars, max_stars)
    if not isinstance(beatmapset, dict):
        return []
    set_id = _integer(beatmapset.get("id"), 1)
    availability = beatmapset.get("availability") or {}
    if (set_id is None or beatmapset.get("deleted_at") or beatmapset.get("download_disabled") is True
            or (isinstance(availability, dict) and availability.get("download_disabled") is True)):
        return []
    title = _text(beatmapset.get("title_unicode")) or _text(beatmapset.get("title"))
    artist = _text(beatmapset.get("artist_unicode")) or _text(beatmapset.get("artist"))
    maps = beatmapset.get("beatmaps")
    if not title or not artist or not isinstance(maps, list) or len(maps) > MAX_MAPS_PER_SET:
        return []
    evidence = popularity_evidence(beatmapset)
    if require_qualification and evidence["qualification"] is None:
        return []
    result, seen = [], set()
    for item in maps:
        if not isinstance(item, dict):
            continue
        map_id = _integer(item.get("id"), 1)
        mode = item.get("mode_int")
        stars = _number(item.get("difficulty_rating"), lower, upper)
        bpm = _number(item.get("bpm"), .01, 2000)
        ar = _number(item.get("ar"), .01, 12)
        length = _integer(item.get("total_length"), 1)
        version = _text(item.get("version"))
        counts = [_integer(item.get(field)) for field in ("count_circles", "count_sliders", "count_spinners")]
        if (map_id is None or map_id in seen or item.get("beatmapset_id") != set_id
                or type(mode) is not int or mode != 0 or item.get("mode") != "osu"
                or item.get("convert") is not False or item.get("deleted_at")
                or item.get("status") not in {"ranked", "approved", "loved"}
                or stars is None or bpm is None or ar is None or length is None or length > 21600
                or not version or any(count is None for count in counts) or not 0 < sum(counts) <= 100000):
            continue
        entry = {
            "key": f"remote:{map_id}", "id": map_id, "set_id": set_id,
            "title": title, "artist": artist, "version": version,
            "title_romanized": _text(beatmapset.get("title")) or "",
            "artist_romanized": _text(beatmapset.get("artist")) or "",
            "creator": _text(beatmapset.get("creator")) or "",
            "stars": stars, "bpm": bpm, "ar": ar, "length": length,
            "object_count": sum(counts), "mode": 0, "source": "online", "local": False,
            "url": f"https://{OFFICIAL_HOST}/beatmapsets/{set_id}#osu/{map_id}",
            "download_url": f"https://{OFFICIAL_HOST}/beatmapsets/{set_id}/download",
            "popularity": dict(evidence), "status": item["status"],
            "discovery_scope": "public_catalog", "difficulty_source": "osu_public_nomod",
            "lazer_only": item.get("lazer_only") is True,
        }
        for key, upstream in (("od", "accuracy"), ("cs", "cs")):
            value = _number(item.get(upstream), 0, 12)
            if value is not None:
                entry[key] = value
        max_combo = _integer(item.get("max_combo"), 1)
        if max_combo is not None:
            entry["max_combo"] = max_combo
        checksum = item.get("checksum")
        if isinstance(checksum, str) and len(checksum) == 32 and all(c in "0123456789abcdefABCDEF" for c in checksum):
            entry["checksum"] = checksum.lower()
        seen.add(map_id)
        result.append(entry)
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DiscoverySourceError("La fuente de mapas devolvió una redirección.", code)


class DiscoverySourceClient:
    def __init__(self, *, interval=MIN_REQUEST_INTERVAL, timeout=12, opener=None,
                 clock=time.monotonic, sleep=time.sleep):
        if _number(interval, 1, 30) is None or _number(timeout, .1, 30) is None:
            raise ValueError("Las consultas requieren un intervalo de al menos un segundo y una espera limitada.")
        self.interval, self.timeout = interval, timeout
        self._opener = opener or build_opener(_NoRedirect())
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._last_request = None

    def _get(self, path, content_type, query=None):
        url = f"https://{OFFICIAL_HOST}{path}" + ("?" + urlencode(query) if query else "")
        with self._lock:
            if self._last_request is not None:
                remaining = self.interval - (self._clock() - self._last_request)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request = self._clock()
            request = Request(url, headers={"User-Agent": "osu-coach-local/1.0 (public map discovery)",
                                           "Accept": content_type, "Accept-Encoding": "identity"}, method="GET")
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    final = urlsplit(response.geturl())
                    if (final.scheme != "https" or final.hostname != OFFICIAL_HOST or final.port not in (None, 443)
                            or final.username or final.password or final.path != path):
                        raise DiscoverySourceError("La respuesta salió de la fuente oficial permitida.")
                    if response.status != 200:
                        raise DiscoverySourceError("La fuente de mapas respondió con un error.", response.status)
                    if response.headers.get_content_type() != content_type:
                        raise DiscoverySourceError("La fuente de mapas cambió de tipo de contenido.")
                    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                        raise DiscoverySourceError("La fuente de mapas devolvió una compresión inesperada.")
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise DiscoverySourceError("La respuesta de mapas supera el tamaño permitido.")
                    return raw.decode("utf-8")
            except HTTPError as exc:
                raise DiscoverySourceError(f"La fuente de mapas respondió HTTP {exc.code}.", exc.code) from exc
            except (URLError, TimeoutError, OSError, UnicodeError) as exc:
                raise DiscoverySourceError("No se pudieron consultar los mapas públicos de osu!.") from exc

    def fetch_candidate_batch(self, min_stars, max_stars, *, cursor=None, exclude_ids=(), requirements=(), excluded_songs=()) -> dict:
        """Read one bounded batch, continuing the public descending-ranked feed.

        next_cursor is JSON serializable and must be stored opaquely. Empty
        results can still have a next cursor. A repeated/backward cursor or page
        ends the feed safely. Stage requirements are alternatives (logical OR).
        Verification checks eight sets per batch; remaining IDs stay in the cursor.
        """
        lower, upper = _range(min_stars, max_stars)
        stage_requirements = _requirements(requirements)
        pending, feed_exhausted = [], False
        if isinstance(cursor, dict) and cursor.get("version") == 2:
            pending = cursor.get("pending")
            feed_exhausted = cursor.get("feed_exhausted")
            if (not isinstance(pending, list) or len(pending) > MAX_PAGES * MAX_SETS_PER_PAGE
                    or any(_integer(value, 1) is None for value in pending)
                    or len(set(pending)) != len(pending) or type(feed_exhausted) is not bool
                    or (not feed_exhausted and cursor.get("feed") is None)):
                raise ValueError("La continuación del catálogo es inválida.")
            cursor = cursor.get("feed")
        current = _read_cursor(cursor)
        excluded = {value for value in exclude_ids if _integer(value, 1) is not None}
        candidates = {}
        seen_sets, seen_pages = set(), set()
        if current["last_page"] is not None:
            seen_pages.add(current["last_page"])
        next_cursor = cursor
        exhausted = feed_exhausted
        for _ in range(0 if pending or feed_exhausted else MAX_PAGES):
            query = ({"cursor_string": current["cursor_string"]} if current["cursor_string"] is not None
                     else {"page": current["page"]})
            text = self._get("/beatmapsets/search", "application/json", query)
            try:
                payload = json.loads(text)
            except (ValueError, RecursionError) as exc:
                raise DiscoverySourceError("La búsqueda de mapas devolvió datos inválidos.") from exc
            sets = parse_search_page(payload)
            if not sets:
                exhausted, next_cursor = True, None
                break
            fingerprint = sha256(json.dumps(sorted({item["id"] for item in sets})).encode()).hexdigest()
            if fingerprint in seen_pages:
                exhausted, next_cursor = True, None
                break
            seen_pages.add(fingerprint)
            new_sets = 0
            for beatmapset in sets:
                set_id = beatmapset["id"]
                if set_id in seen_sets:
                    continue
                seen_sets.add(set_id)
                new_sets += 1
                # The public listing already includes the set play count.
                # Reject insufficient/unknown counts before spending a set read.
                if (_integer(beatmapset.get("play_count"), get_setting("quality_min_plays")) is not None
                        and any(_eligible(item, excluded, stage_requirements, excluded_songs) for item in
                                parse_candidates(beatmapset, lower, upper, require_qualification=False))):
                    candidates[set_id] = beatmapset
            if new_sets == 0:
                exhausted, next_cursor = True, None
                break
            next_cursor = _page_cursor(payload, current, fingerprint)
            if next_cursor is None:
                exhausted = True
                break
            current = next_cursor
        # Favourites are only used to prioritise the bounded verification work.
        # The full page provides both the current maps and the rating histogram.
        queued = pending or [item["id"] for item in sorted(candidates.values(), key=lambda item: (
            -(_integer(item.get("favourite_count")) or 0), item["id"]))]
        selected, remaining = queued[:MAX_VERIFIED_SETS], queued[MAX_VERIFIED_SETS:]
        result, seen_maps = [], set()
        for set_id in selected:
            page = self._get(f"/beatmapsets/{set_id}", "text/html")
            verified = parse_set_page(page, set_id)
            # The same public page also carries per-difficulty community tags.
            # Their absence should not discard otherwise verified rating data.
            from osu_coach.integrations.tag_source import TagSourceError, parse_set_tags
            try:
                tags = parse_set_tags(page, set_id)
            except (TagSourceError, ValueError):
                tags = None
            for item in parse_candidates(verified, lower, upper):
                if item["id"] not in seen_maps and _eligible(item, excluded, stage_requirements, excluded_songs):
                    if tags is not None:
                        item["tags"] = [{**tag, "source": "community", "provenance": "community_user_tags"}
                                        for tag in tags.get(item["id"], []) if tag["count"] >= 5]
                        item["tag_status"] = "known" if item["tags"] else ("weak" if tags.get(item["id"]) else "none")
                    result.append(item)
                    seen_maps.add(item["id"])
        if remaining:
            next_cursor = {"version": 2, "feed": next_cursor, "feed_exhausted": exhausted,
                           "pending": remaining}
            exhausted = False
        return {"maps": result, "next_cursor": next_cursor, "exhausted": exhausted}

    def fetch_candidates(self, min_stars, max_stars) -> list[dict]:
        return self.fetch_candidate_batch(min_stars, max_stars)["maps"]


_default_client = DiscoverySourceClient()


def fetch_candidates(min_stars, max_stars) -> list[dict]:
    return _default_client.fetch_candidates(min_stars, max_stars)


def fetch_candidate_batch(min_stars, max_stars, *, cursor=None, exclude_ids=(), requirements=(), excluded_songs=()) -> dict:
    return _default_client.fetch_candidate_batch(min_stars, max_stars, cursor=cursor,
                                                 exclude_ids=exclude_ids, requirements=requirements,
                                                 **({"excluded_songs": excluded_songs} if excluded_songs else {}))
