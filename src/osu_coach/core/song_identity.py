"""Conservative song identities, independent of difficulty, mapper and mods."""
import json
import unicodedata


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def song_tokens(beatmap):
    tokens = set()
    try:
        identifier = int(beatmap.get("set_id") or 0)
        if identifier > 0:
            tokens.add(f"set:{identifier}")
    except (ValueError, TypeError, OverflowError):
        pass
    titles = {normalized(beatmap.get(key)) for key in ("title", "title_romanized", "title_unicode")} - {""}
    artists = {normalized(beatmap.get(key)) for key in ("artist", "artist_romanized", "artist_unicode")} - {""}
    tokens.update("song:" + json.dumps([artist, title], ensure_ascii=False, separators=(",", ":"))
                  for artist in artists for title in titles)
    return tokens


def song_owner(profile):
    # Music preferences follow the player across clients, mods and recalibration.
    return normalized(json.loads(profile)[0]) if profile else ""
