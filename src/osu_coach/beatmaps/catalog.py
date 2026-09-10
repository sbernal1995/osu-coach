"""Catálogo local de osu!standard. Solo lee archivos de mapas del juego.

Acepta tanto Songs de stable como el almacén ``files`` de lazer. No consulta
cuentas, puntuaciones ni bases de datos del juego. La dificultad se calcula con
el motor de osu!lazer; ``length`` son segundos entre el primer objeto y el final del último.
"""

from __future__ import annotations

from bisect import bisect_right
import hashlib
import math
import os
from pathlib import Path
import re
from typing import Any, Callable

from osu_coach.integrations.lazer_calculator import calculator


MAX_MAP_BYTES = 8 * 1024 * 1024
MAX_OBJECTS = 100_000
_HASH_NAME = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_HEADER = re.compile(rb"osu file format v[0-9]+(?:\r?\n|$)")


class InvalidBeatmap(ValueError):
    """Archivo incompatible, vacío o demasiado complejo para el catálogo."""


def _rosu() -> Any:
    try:
        import rosu_pp_py
    except ImportError as exc:
        raise RuntimeError(
            "Falta rosu-pp-py para calcular la dificultad de los mapas. "
            "Ejecutá el instalador o instalá las dependencias del proyecto."
        ) from exc
    return rosu_pp_py


def _read_map(path: Path) -> bytes:
    """Descarta recursos binarios tras leer como máximo 128 bytes."""
    if path.is_symlink() or path.stat().st_size > MAX_MAP_BYTES:
        raise InvalidBeatmap("El archivo supera el tamaño permitido o es un enlace.")
    with path.open("rb") as handle:
        if os.fstat(handle.fileno()).st_size > MAX_MAP_BYTES:
            raise InvalidBeatmap("El archivo supera los 8 MB.")
        prefix = handle.read(128)
        header = prefix.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n")
        if not _HEADER.match(header):
            raise InvalidBeatmap("El archivo no tiene una cabecera de mapa osu!.")
        content = prefix + handle.read(MAX_MAP_BYTES + 1 - len(prefix))
        if len(content) > MAX_MAP_BYTES:
            raise InvalidBeatmap("El archivo supera los 8 MB.")
    return content


def _number(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise InvalidBeatmap("El mapa contiene un número inválido.")
    return result


def _metadata(content: bytes) -> dict[str, Any]:
    section = ""
    values: dict[str, str] = {}
    timing: list[tuple[float, float, bool]] = []
    objects: list[list[str]] = []
    for raw in content.decode("utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section in {"General", "Metadata", "Difficulty"} and ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
        elif section == "TimingPoints":
            parts = line.split(",")
            if len(parts) >= 2:
                beat_length = _number(parts[1])
                inherited = len(parts) >= 7 and parts[6].strip() == "0"
                timing.append((_number(parts[0]), beat_length, not inherited))
        elif section == "HitObjects":
            parts = line.split(",")
            if len(parts) < 5:
                raise InvalidBeatmap("El mapa contiene un objeto incompleto.")
            objects.append(parts)
            if len(objects) > MAX_OBJECTS:
                raise InvalidBeatmap("El mapa tiene demasiados objetos.")

    if int(values.get("Mode", "0")) != 0:
        raise InvalidBeatmap("Por ahora se admiten mapas de osu!standard.")
    if not objects:
        raise InvalidBeatmap("El mapa no tiene objetos jugables.")

    # Las líneas rojas fijan el tempo; las verdes ajustan la velocidad del slider.
    timing.sort(key=lambda item: item[0])
    times: list[float] = []
    states: list[tuple[float, float]] = []
    beat_length, velocity = 500.0, 1.0
    for timestamp, new_length, uninherited in timing:
        if uninherited and new_length > 0:
            beat_length, velocity = new_length, 1.0
        elif not uninherited and new_length < 0:
            velocity = min(10.0, max(0.1, -100.0 / new_length))
        times.append(timestamp)
        states.append((beat_length, velocity))

    multiplier = _number(values.get("SliderMultiplier", "1.4"))
    if multiplier <= 0:
        raise InvalidBeatmap("La velocidad de los sliders es inválida.")
    first_start = math.inf
    final_end = -math.inf
    for parts in objects:
        start = _number(parts[2])
        kind = int(parts[3])
        end = start
        if kind & 2:
            if len(parts) < 8:
                raise InvalidBeatmap("El mapa contiene un slider incompleto.")
            repeats, distance = int(parts[6]), _number(parts[7])
            if not 1 <= repeats <= 10_000 or not 0 <= distance <= 1_000_000:
                raise InvalidBeatmap("El mapa contiene un slider demasiado complejo.")
            index = bisect_right(times, start) - 1
            beat, speed = states[index] if index >= 0 else (500.0, 1.0)
            end += distance * repeats * beat / (multiplier * 100.0 * speed)
        elif kind & 8:
            if len(parts) < 6:
                raise InvalidBeatmap("El mapa contiene un spinner incompleto.")
            end = max(start, _number(parts[5]))
        first_start = min(first_start, start)
        final_end = max(final_end, end)

    duration = max(0.0, (final_end - first_start) / 1000.0)
    if not math.isfinite(duration) or duration > 6 * 60 * 60:
        raise InvalidBeatmap("La duración del mapa es demasiado larga.")
    return {
        "id": max(0, int(values.get("BeatmapID", "0") or "0")),
        "set_id": max(0, int(values.get("BeatmapSetID", "0") or "0")),
        "title": values.get("TitleUnicode") or values.get("Title") or "Sin título",
        "artist": values.get("ArtistUnicode") or values.get("Artist") or "Sin artista",
        "title_romanized": values.get("Title") or "",
        "artist_romanized": values.get("Artist") or "",
        "version": values.get("Version") or "Sin dificultad",
        "creator": values.get("Creator") or "",
        # Mapper metadata commonly describes the music. Keep it separate from
        # community skill tags, which are attached to an individual difficulty.
        "mapper_tags": list(dict.fromkeys(values.get("Tags", "").split()))[:256],
        "length": duration,
    }


def _attributes(content: bytes, metadata: dict[str, Any], rosu: Any,
                mods: Any = None, *, lazer: bool = True,
                clock_rate: float | None = None) -> dict[str, Any]:
    beatmap = rosu.Beatmap(bytes=content)
    if beatmap.mode != rosu.GameMode.Osu or beatmap.n_objects == 0:
        raise InvalidBeatmap("El mapa no contiene objetos de osu!standard.")
    if beatmap.n_objects > MAX_OBJECTS or beatmap.is_suspicious():
        raise InvalidBeatmap("El mapa es demasiado complejo para calcularlo.")
    options = {"mods": mods} if mods is not None else {}
    if clock_rate is not None:
        clock_rate = float(clock_rate)
        if not math.isfinite(clock_rate) or not 0.5 <= clock_rate <= 2:
            raise InvalidBeatmap("La velocidad de reproducción es inválida.")
        options["clock_rate"] = clock_rate
    difficulty = calculator.calculate(content, mods, lazer=lazer, clock_rate=clock_rate)
    builder = rosu.BeatmapAttributesBuilder(**options)
    builder.set_map(beatmap)
    attributes = builder.build()
    clock_rate = float(attributes.clock_rate)
    result = {
        "stars": float(difficulty["stars"]),
        "calculator": difficulty["calculator"],
        "bpm": float(beatmap.bpm) * clock_rate,
        "length": int(round(metadata["length"] / clock_rate)),
        "ar": float(difficulty["ar"]),
        "od": float(difficulty["od"]),
        "cs": float(difficulty["cs"]),
        "max_combo": int(difficulty["max_combo"]),
        "object_count": int(difficulty["object_count"]),
        "mode": 0,
        "aim": float(difficulty["aim"]),
        "speed": float(difficulty["speed"]),
        "reading": float(difficulty["reading"]),
        "clock_rate": clock_rate,
    }
    if any(isinstance(value, float) and not math.isfinite(value)
           for value in result.values()):
        raise InvalidBeatmap("No se pudo calcular una dificultad válida.")
    return result


def difficulty_for(path: str | Path, mods: Any = None, *,
                   lazer: bool = True,
                   clock_rate: float | None = None) -> dict[str, Any]:
    """Calcula SR, atributos y duración con los mods indicados.

    ``mods`` acepta el formato de rosu: entero, "HDDT", lista de acrónimos o
    lista de diccionarios ``{"acronym": "DT", "settings": {"speed_change": 1.2}}``.
    Los valores de AR, OD, CS y BPM reflejan los mods. ``lazer=False`` permite
    solicitar las reglas de stable. ``clock_rate`` aplica la velocidad observada
    en el juego y tiene prioridad sobre la indicada por los mods.
    Los archivos inválidos producen ValueError;
    un archivo inaccesible produce OSError.
    """
    rosu = _rosu()
    content = _read_map(Path(path))
    return _attributes(content, _metadata(content), rosu, mods,
                       lazer=lazer, clock_rate=clock_rate)


def scan_catalog(root: str | Path,
                 progress: Callable[[int], Any] | None = None) -> list[dict[str, Any]]:
    """Lee los mapas locales sin modificar archivos del juego.

    ``root`` es Songs (stable) o files (lazer). El MD5 de cada mapa es su ``key``;
    se omiten duplicados, otros modos y archivos dañados o inaccesibles.
    ``progress(cantidad)`` recibe la cantidad de mapas válidos encontrados, con
    una llamada inicial a cero. La falta de rosu-pp-py genera RuntimeError.
    """
    rosu = _rosu()
    directory = Path(root).expanduser()
    if not directory.is_dir():
        raise FileNotFoundError(f"No se encontró la carpeta de mapas: {directory}")
    maps: list[dict[str, Any]] = []
    seen: set[str] = set()
    if callable(progress):
        progress(0)
    for current, directories, names in os.walk(directory, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if not (Path(current) / name).is_symlink()
            and not (hasattr(Path, "is_junction") and (Path(current) / name).is_junction())
        )
        for name in sorted(names):
            if not (name.lower().endswith(".osu") or _HASH_NAME.fullmatch(name)):
                continue
            path = Path(current) / name
            try:
                content = _read_map(path)
                key = hashlib.md5(content, usedforsecurity=False).hexdigest()
                if key in seen:
                    continue
                metadata = _metadata(content)
                attributes = _attributes(content, metadata, rosu)
                attributes.pop("clock_rate")
                entry = {"key": key, **metadata, **attributes,
                         "path": str(path.resolve()), "source": "local"}
            except (OSError, ValueError, OverflowError, rosu.ParseError, rosu.ArgsError):
                continue
            seen.add(key)
            maps.append(entry)
            if callable(progress):
                progress(len(maps))
    return maps
