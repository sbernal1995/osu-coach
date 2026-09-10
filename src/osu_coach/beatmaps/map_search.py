"""Queries for the in-game selector, separate from frozen training goals."""
from __future__ import annotations

import unicodedata


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _words(value):
    # Lazer treats [text] as a difficulty filter, even inside a song title.
    # Operators and punctuation in metadata must not become query syntax.
    return " ".join("".join(char if char.isalnum() or unicodedata.category(char).startswith("M")
                            else " " for char in _text(value)).split())


def search_details(beatmap):
    title = _text(beatmap.get("title_romanized")) or _text(beatmap.get("title"))
    creator = _text(beatmap.get("creator"))
    title_query = _words(" ".join(filter(None, [title, _text(beatmap.get("version"))])))
    mapper_query = _words(creator)
    # A numeric title/mapper must not accidentally become an online ID lookup.
    if title_query.isdecimal():
        title_query = "title=" + title_query
    if mapper_query.isdecimal():
        mapper_query = "creator=" + mapper_query
    identifier = beatmap.get("id")
    valid_id = (isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0
                or isinstance(identifier, str) and identifier.isascii() and identifier.isdecimal()
                and int(identifier) > 0)
    if valid_id:
        query, method = str(int(identifier)), "id"
    elif title_query:
        query, method = title_query, "title"
    elif mapper_query:
        query, method = mapper_query, "mapper"
    else:
        query, method = "", "unavailable"
    return {"search_text": query, "search_method": method,
            "search_title_text": title_query, "search_mapper_text": mapper_query,
            "creator": creator, "title_romanized": _text(beatmap.get("title_romanized")),
            "artist_romanized": _text(beatmap.get("artist_romanized"))}
