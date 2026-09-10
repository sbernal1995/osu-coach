"""Read community user tags from public, per-difficulty osu! pages.

No account, cookie, OAuth token or game database is used. The public page
contains ``json-beatmapset`` with ``beatmaps[].top_tag_ids`` and the matching
``related_tags`` definitions. ``Tags`` in an .osu file is different metadata.

The caller owns the persistent cache. Fetch once per beatmapset, since one
response includes all its difficulties. Counts below five are returned as
evidence too; the caller must distinguish them from publicly displayed tags.
Official tag documentation: https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tags
"""

from __future__ import annotations

from html.parser import HTMLParser
import json
import math
import threading
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROVENANCE = "community_user_tags"
OFFICIAL_HOST = "osu.ppy.sh"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MIN_REQUEST_INTERVAL = 1.1
MAX_TAGS = 1024
MAX_DIFFICULTIES = 1024


class TagSourceError(RuntimeError):
    """Public tags could not be fetched or the upstream schema changed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _positive_id(value: Any) -> int | None:
    return value if type(value) is int and 0 < value < 2**63 else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The requested public endpoint needs no redirect. In particular, do
        # not follow an authentication redirect or send traffic to other hosts.
        raise TagSourceError("La fuente de etiquetas devolvió una redirección.", code)


class _SetJSON(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.collecting = False
        self.found = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") == "json-beatmapset":
            if attrs.get("type") != "application/json":
                raise TagSourceError("El contenido de etiquetas cambió de formato.")
            self.found += 1
            self.collecting = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.collecting = False

    def handle_data(self, data):
        if self.collecting:
            self.parts.append(data)


def parse_set_tags(page: str, expected_set_id: int) -> dict[int, list[dict[str, Any]]]:
    """Extract only tag metadata, maintaining each difficulty's own votes."""
    if _positive_id(expected_set_id) is None:
        raise ValueError("El identificador del set debe ser un entero positivo.")
    if not isinstance(page, str) or len(page.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise TagSourceError("La página de etiquetas supera el tamaño permitido.")
    parser = _SetJSON()
    parser.feed(page)
    parser.close()
    if parser.found != 1 or parser.collecting:
        raise TagSourceError("La página no contiene los datos públicos de etiquetas.")
    try:
        payload = json.loads("".join(parser.parts))
    except (ValueError, RecursionError) as exc:
        raise TagSourceError("La página contiene datos de etiquetas inválidos.") from exc
    if (not isinstance(payload, dict) or _positive_id(payload.get("id")) is None
            or payload.get("id") != expected_set_id):
        raise TagSourceError("Las etiquetas no corresponden al set solicitado.")
    maps, definitions = payload.get("beatmaps"), payload.get("related_tags")
    if not isinstance(maps, list) or not isinstance(definitions, list):
        raise TagSourceError("La fuente no incluyó los campos de etiquetas esperados.")
    if len(maps) > MAX_DIFFICULTIES or len(definitions) > MAX_TAGS:
        raise TagSourceError("La fuente devolvió demasiados mapas o etiquetas.")
    by_id: dict[int, dict[str, Any]] = {}
    for item in definitions:
        if not isinstance(item, dict) or _positive_id(item.get("id")) is None:
            raise TagSourceError("Hay una definición de etiqueta inválida.")
        name, description = item.get("name"), item.get("description")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise TagSourceError("Hay un nombre de etiqueta inválido.")
        tag = {"id": item["id"], "name": name, "source": PROVENANCE}
        if isinstance(description, str):
            tag["description"] = description[:4000]
        ruleset = item.get("ruleset_id")
        if ruleset is None or (type(ruleset) is int and 0 <= ruleset <= 3):
            tag["ruleset_id"] = ruleset
        if tag["id"] in by_id:
            raise TagSourceError("La fuente repitió una definición de etiqueta.")
        by_id[tag["id"]] = tag
    result: dict[int, list[dict[str, Any]]] = {}
    for beatmap in maps:
        if not isinstance(beatmap, dict) or _positive_id(beatmap.get("id")) is None:
            raise TagSourceError("Hay una dificultad inválida en la fuente.")
        if (_positive_id(beatmap.get("beatmapset_id")) is None
                or beatmap.get("beatmapset_id") != expected_set_id):
            raise TagSourceError("La dificultad pertenece a otro set.")
        votes = beatmap.get("top_tag_ids")
        if not isinstance(votes, list) or len(votes) > MAX_TAGS:
            raise TagSourceError("Faltan los votos de etiquetas de una dificultad.")
        tags: list[dict[str, Any]] = []
        seen = set()
        for vote in votes:
            if not isinstance(vote, dict):
                raise TagSourceError("Hay un voto de etiqueta inválido.")
            tag_id, count = vote.get("tag_id"), vote.get("count")
            if _positive_id(tag_id) is None or type(count) is not int or count < 0:
                raise TagSourceError("Hay un identificador o recuento de votos inválido.")
            if tag_id not in by_id or tag_id in seen:
                raise TagSourceError("Los votos no tienen una definición única.")
            seen.add(tag_id)
            tags.append({**by_id[tag_id], "count": count})
        if beatmap["id"] in result:
            raise TagSourceError("La fuente repitió una dificultad.")
        result[beatmap["id"]] = tags
    return result


class TagSourceClient:
    """Unauthenticated GET client with bounded reads and serial rate limiting."""

    def __init__(self, *, interval: float = MIN_REQUEST_INTERVAL, timeout: float = 12,
                 opener=None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if not math.isfinite(interval) or interval < 1:
            raise ValueError("Las consultas deben espaciarse al menos un segundo.")
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("El tiempo de espera debe estar entre 0 y 30 segundos.")
        self.interval = interval
        self.timeout = timeout
        self._opener = opener or build_opener(_NoRedirect())
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request: float | None = None

    def fetch_set_tags(self, set_id: int) -> dict[int, list[dict[str, Any]]]:
        if _positive_id(set_id) is None:
            raise ValueError("El identificador del set debe ser un entero positivo.")
        url = f"https://{OFFICIAL_HOST}/beatmapsets/{set_id}"
        with self._lock:
            if self._last_request is not None:
                remaining = self.interval - (self._clock() - self._last_request)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request = self._clock()
            request = Request(url, headers={
                "User-Agent": "osu-coach-local/1.0 (public beatmap user tags)",
                "Accept": "text/html",
                "Accept-Encoding": "identity",
            }, method="GET")
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    final = urlsplit(response.geturl())
                    if (final.scheme != "https" or final.hostname != OFFICIAL_HOST
                            or final.port not in (None, 443) or final.username or final.password
                            or final.path != f"/beatmapsets/{set_id}"):
                        raise TagSourceError("La respuesta salió de la fuente oficial permitida.")
                    if response.status != 200:
                        raise TagSourceError("La fuente de etiquetas no respondió correctamente.", response.status)
                    if response.headers.get_content_type() != "text/html":
                        raise TagSourceError("La fuente devolvió un tipo de contenido inesperado.")
                    encoding = response.headers.get("Content-Encoding", "identity")
                    if encoding.lower() not in ("", "identity"):
                        raise TagSourceError("La fuente devolvió una compresión inesperada.")
                    data = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise TagSourceError("La página de etiquetas supera el tamaño permitido.")
                page = data.decode("utf-8")
            except HTTPError as exc:
                raise TagSourceError(f"La fuente de etiquetas respondió HTTP {exc.code}.", exc.code) from exc
            except (URLError, TimeoutError, OSError, UnicodeError) as exc:
                raise TagSourceError("No se pudieron consultar las etiquetas públicas de osu!.") from exc
        return parse_set_tags(page, set_id)

    def fetch_sets_tags(self, set_ids: Iterable[int]) -> dict[int, dict[int, list[dict[str, Any]]]]:
        """Request each distinct set once; persistent caching belongs to caller."""
        values = list(set_ids)
        if any(_positive_id(set_id) is None for set_id in values):
            raise ValueError("Los identificadores deben ser enteros positivos.")
        ids = list(dict.fromkeys(values))
        return {set_id: self.fetch_set_tags(set_id) for set_id in ids}


_default_client = TagSourceClient()


def fetch_set_tags(set_id: int) -> dict[int, list[dict[str, Any]]]:
    return _default_client.fetch_set_tags(set_id)


def fetch_sets_tags(set_ids: Iterable[int]) -> dict[int, dict[int, list[dict[str, Any]]]]:
    return _default_client.fetch_sets_tags(set_ids)
