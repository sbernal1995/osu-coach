"""Clearly fictional fixtures, isolated from real training data."""
from datetime import datetime, timedelta, timezone


def demo_data():
    maps = []
    for index in range(24):
        maps.append({"key": f"demo-map-{index}", "id": 0, "set_id": index + 1,
                     "title": f"Mapa de ejemplo {index + 1:02}", "artist": "Biblioteca simulada",
                     "version": "Práctica", "creator": "Demo", "stars": round(2.6 + index * .05, 2),
                     "bpm": 160, "length": 110 + index, "ar": 8, "od": 7, "cs": 4,
                     "max_combo": 550, "object_count": 400, "mode": 0, "source": "demo"})
        maps[-1]["tags"] = [{"name": "skillset/jumps" if index % 2 == 0 else "streams/bursts", "source": "manual"}]
    now = datetime.now(timezone.utc)
    plays = []
    for i, acc in enumerate([95.2, 96.1, 97.4, 97.8, 98.1]):
        m = maps[12 + i]
        plays.append({"id": f"demo-score-{i}", "played_at": (now - timedelta(minutes=12 - i * 2)).isoformat(),
                      "player": "Jugador de ejemplo", "client": "lazer", "mode": 0,
                      "beatmap_key": m["key"], "beatmap_id": 0, "stars": m["stars"],
                      "title": m["title"], "version": m["version"], "accuracy": acc,
                      "misses": 0 if i > 1 else 5 - i, "max_combo": 520, "map_max_combo": 550,
                      "object_count": 400, "judged_objects": 400, "passed": True,
                      "completion": 1, "mods": [], "mod_key": '{"mods":[],"rate":1.0}',
                      "bpm": 160, "ar": 8, "length": m["length"], "source": "demo"})
    return maps, plays
