"""Validated, persistent coach preferences and isolated per-request settings."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import copy
import math


def field(key, label, default, minimum, maximum, step, group, description):
    kind = "boolean" if isinstance(default, bool) else "integer" if isinstance(default, int) else "number"
    return {"key": key, "label": label, "default": default, "min": minimum, "max": maximum,
            "step": step, "type": kind, "group": group, "description": description}


def choice(key, label, default, options, description):
    return {"key": key, "label": label, "default": default, "type": "choice",
            "options": [{"value": value, "label": text} for value, text in options],
            "group": "Recomendaciones: duración y mods", "description": description}


SCHEMA = [
    field("recommendation_min_seconds", "Duración mínima (segundos)", 0, 0, 7200, 1, "Recomendaciones: duración y mods", "Duración al jugar, después de aplicar la velocidad del mod. 0 no exige un mínimo."),
    field("recommendation_max_seconds", "Duración máxima (segundos)", 0, 0, 7200, 1, "Recomendaciones: duración y mods", "Se aplica a mapas locales y por descargar, en todas las etapas. 0 deja la duración sin límite."),
    choice("recommendation_mods", "Mods de las recomendaciones", "profile", [
        ("profile", "Mantener los mods del perfil"), ("free", "Libre: el coach elige"),
        ("NM", "Forzar Sin mods"), ("HD", "Forzar Hidden (HD)"), ("HR", "Forzar Hard Rock (HR)"),
        ("DT", "Forzar Double Time (DT · ×1,5)"), ("HT", "Forzar Half Time (HT · ×0,75)"),
        ("HDHR", "Forzar HD + HR"), ("HDDT", "Forzar HD + DT (×1,5)"), ("HDHT", "Forzar HD + HT (×0,75)")],
        "Libre compara Sin mods, HD, HR, DT, HT y combinaciones con HD. Cada misión fija sus mods y usa sus estrellas reales. Los cambios renuevan misiones sin intentos; las que empezaste conservan su objetivo."),

    field("reference_plays", "Partidas para calcular tu nivel", 100, 5, 2000, 1, "Memoria y calibración", "Máximo de partidas recientes que forman la referencia; el historial completo se conserva."),
    field("reference_days", "Antigüedad máxima de la referencia (días)", 30, 1, 365, 1, "Memoria y calibración", "Sólo entran resultados posteriores a la última recalibración y dentro de esta ventana."),
    field("session_plays", "Partidas para evaluar la sesión", 20, 3, 2000, 1, "Memoria y calibración", "Ventana corta que detecta cansancio o recuperación y ajusta la práctica del momento."),
    field("session_days", "Antigüedad máxima de la sesión (días)", 7, 1, 365, 1, "Memoria y calibración", "La sesión debe quedar dentro de la ventana de referencia."),
    field("calibration_plays", "Partidas para completar la calibración", 5, 3, 500, 1, "Memoria y calibración", "Cantidad mínima de resultados necesarios para pasar a entrenamiento."),
    field("calibration_maps", "Mapas distintos para calibrar", 3, 1, 100, 1, "Memoria y calibración", "Repetir una dificultad no aporta variedad a este mínimo."),
    field("initial_stars", "Referencia inicial (estrellas)", 2.5, .5, 10.5, .05, "Memoria y calibración", "Punto de partida provisional cuando todavía no hay partidas elegibles."),
    field("half_life_days", "Días para que una partida pese la mitad", 10.0, .5, 365.0, .5, "Memoria y calibración", "Un valor bajo hace que los resultados recientes pesen más."),
    field("max_attempts_per_map", "Intentos recientes por dificultad", 2, 1, 10, 1, "Memoria y calibración", "Limita cuánto influye repetir el mismo mapa en tu referencia y perfil."),
    field("trim_percent", "Recorte de cada extremo (%)", 20.0, 0.0, 40.0, 1, "Memoria y calibración", "Reduce la influencia de resultados extremos al calcular la referencia."),
    field("profile_min_plays", "Mediciones mínimas del perfil", 8, 3, 500, 1, "Perfil del jugador", "Evidencia necesaria para describir fortalezas y aspectos a practicar."),
    field("profile_min_maps", "Mapas mínimos del perfil", 5, 2, 100, 1, "Perfil del jugador", "Variedad mínima para sostener una conclusión sobre tu rendimiento."),
    field("profile_min_sessions", "Sesiones mínimas del perfil", 2, 1, 30, 1, "Perfil del jugador", "Cantidad de sesiones con resultados comparables."),
    field("session_gap_minutes", "Pausa que separa sesiones (minutos)", 60, 5, 1440, 5, "Perfil del jugador", "Una pausa igual o mayor inicia otra sesión para evaluar evidencia."),
    field("comparable_star_band", "Margen de comparación del perfil (estrellas)", .5, .1, 2.0, .05, "Perfil del jugador", "Distancia máxima a la referencia para comparar mapas, tags y metas."),
    field("trend_plays", "Partidas para medir evolución", 1000, 10, 10000, 10, "Evolución y repeticiones", "Historial amplio para comparar resultados equivalentes; no cambia la exigencia de la sesión."),
    field("trend_days", "Antigüedad de la evolución (días)", 90, 7, 730, 1, "Evolución y repeticiones", "Se respeta la última recalibración. Los resultados más antiguos siguen guardados."),
    field("benchmark_enabled", "Repetir mapas de referencia", True, None, None, None, "Evolución y repeticiones", "Permite una repetición espaciada para comparar tu avance en la misma dificultad y con los mismos mods. Respeta las canciones excluidas."),
    field("benchmark_cooldown_days", "Días antes de repetir una referencia", 7, 1, 90, 1, "Evolución y repeticiones", "Espera desde el último intento, incluso si fue fallido. Como máximo una misión de referencia activa."),
    field("goal_accuracy_step", "Paso de precisión por misión (puntos)", .5, .1, 2.0, .1, "Metas de práctica", "Mejora sobre una referencia comparable; es una meta ajustable, no una predicción."),
    field("goal_miss_reduction_percent", "Reducción de misses por misión (%)", 20.0, 5.0, 50.0, 5, "Metas de práctica", "Por ejemplo: de 10 a 8 misses con un paso del 20 %."),
    field("goal_combo_step", "Paso de combo por misión (%)", 5.0, 1.0, 20.0, 1, "Metas de práctica", "Aumento del porcentaje del combo máximo al trabajar continuidad."),
    field("comparable_od_band", "Margen de OD para comparar precisión", 1.0, .1, 3.0, .1, "Perfil del jugador", "Compara precisión en mapas con ventanas de acierto cercanas y los mismos mods. Si falta OD, la estimación queda provisional."),
    field("bpm_hard_limit", "Usar un límite estricto de BPM", False, None, None, None, "Dificultad y progresión", "Por defecto el tempo no descarta mapas. Se prioriza la exigencia de pulsaciones y patrones; activalo si preferís el límite antiguo."),
    field("training_progress_enabled", "Subir la práctica al cumplir misiones", True, None, None, None, "Progresión del entrenamiento", "Guarda un nivel de práctica por perfil y calibración. Los calentamientos no lo bajan."),
    field("training_step", "Subida de práctica (estrellas)", .10, .05, .25, .05, "Progresión del entrenamiento", "Aumento al completar un paso. Los cambios se aplican al siguiente paso."),
    field("training_required_maps", "Dificultades por paso", 3, 1, 12, 1, "Progresión del entrenamiento", "Misiones distintas que indiquen que cuentan para subir. Repetir la misma dificultad no suma."),
    field("training_required_sessions", "Sesiones por paso", 2, 1, 5, 1, "Progresión del entrenamiento", "Reparte los resultados entre sesiones. Las reglas del paso iniciado se conservan."),
    field("warmup_offset", "Reducción para entrar en ritmo (estrellas)", .35, 0.0, 1.5, .05, "Dificultad y progresión", "Cuánto más accesible será el objetivo de la entrada en ritmo."),
    field("challenge_increment", "Aumento del desafío (estrellas)", .15, .05, 1.0, .05, "Dificultad y progresión", "Paso por encima de tu referencia cuando estás listo para avanzar."),
    field("consolidate_increment", "Aumento del desafío al afianzar (estrellas)", .10, 0.0, 1.0, .05, "Dificultad y progresión", "Paso más moderado mientras el perfil pide afianzar."),
    field("recovery_drop", "Reducción por una sesión difícil (estrellas)", .25, .05, 1.0, .05, "Dificultad y progresión", "Baja de exigencia temporal para recuperar control."),
    field("star_tolerance_below", "Margen por debajo del objetivo (estrellas)", .30, .05, 2.0, .05, "Dificultad y progresión", "Límite inferior de dificultad para los mapas de cada etapa."),
    field("star_tolerance_above", "Margen por encima del objetivo (estrellas)", .18, .01, 2.0, .01, "Dificultad y progresión", "Límite superior de dificultad, también aplicado a las búsquedas online."),
    field("bpm_margin", "Margen de BPM si activás el límite estricto", 15.0, 0.0, 100.0, 1, "Dificultad y progresión", "Incremento sobre la velocidad de tus mapas recientes de referencia."),
    field("ar_margin", "Aumento máximo de AR", .7, 0.0, 3.0, .1, "Dificultad y progresión", "Limita el salto en la rapidez de lectura exigida."),
    field("warmup_preferred_seconds", "Duración preferida para entrar en ritmo (segundos)", 150, 0, 900, 15, "Dificultad y progresión", "Prioridad suave para mapas de hasta esta duración al calentar; los más largos también pueden aparecer. No limita la búsqueda ni afecta las otras etapas. 0 desactiva la preferencia."),
    field("challenge_maps", "Partidas distintas para habilitar el desafío", 3, 1, 20, 1, "Dificultad y progresión", "Las últimas partidas deben ser de dificultades distintas y cumplir precisión y misses."),
    field("challenge_accuracy", "Precisión mínima para el desafío (%)", 94.0, 80.0, 100.0, .5, "Dificultad y progresión", "Se comprueba en cada una de las últimas partidas necesarias."),
    field("challenge_miss_percent", "Máximo de misses para el desafío (%)", 2.0, 0.0, 10.0, .1, "Dificultad y progresión", "Porcentaje de misses sobre los objetos juzgados de cada partida."),
    field("rank_step", "Paso entre rangos personales (estrellas)", .25, .05, 1.0, .05, "Rangos personales", "Los rangos ya ganados se conservan al cambiar el paso."),
    field("rank_required_maps", "Mapas distintos para ganar un rango", 3, 1, 30, 1, "Rangos personales", "Cambiar ajustes no otorga rangos; se evalúan al aceptar partidas nuevas."),
    field("strong_accuracy", "Precisión de un resultado sólido (%)", 97.0, 80.0, 100.0, .5, "Rangos personales", "Se usa para reconocer resultados sólidos y demostrar rangos."),
    field("strong_miss_percent", "Misses máximos de un resultado sólido (%)", .5, 0.0, 10.0, .1, "Rangos personales", "Proporción máxima de misses sobre objetos juzgados."),
    field("strong_combo_percent", "Combo mínimo de un resultado sólido (%)", 80.0, 0.0, 100.0, 1, "Rangos personales", "Porcentaje del combo máximo necesario cuando el dato está disponible."),
    field("quality_min_rating", "Valoración mínima para descargar", 8.0, 0.0, 10.0, .1, "Mapas para descargar", "Nota del conjunto de dificultades, calculada a partir de votos verificables."),
    field("quality_min_votes", "Votos mínimos de valoración", 10, 1, 100000, 1, "Mapas para descargar", "Debe cumplirse junto con la nota mínima y el conteo de partidas."),
    field("quality_min_plays", "Partidas jugadas mínimas del conjunto", 10000, 0, 1000000000, 1000, "Mapas para descargar", "Total de partidas de todas las dificultades; no representa jugadores únicos."),
    field("discovery_reserve_per_stage", "Canciones online de reserva por etapa", 6, 0, 30, 1, "Mapas para descargar", "Candidatas para descargar adicionales por etapa, fuera de las misiones actuales. El coach sigue buscando hasta cubrir esta reserva; 0 busca solo si faltan misiones."),
    field("discovery_enabled", "Buscar mapas automáticamente", True, None, None, None, "Mapas para descargar", "Completa las misiones, prepara una reserva y sigue explorando mapas de cualquier antigüedad."),
    field("discovery_interval_hours", "Revisión habitual del catálogo (horas)", 24.0, 1.0, 168.0, 1, "Mapas para descargar", "Intervalo de búsqueda cuando las etapas y su reserva ya están cubiertas."),
    field("discovery_batches_per_pass", "Lotes seguidos para completar mapas y reserva", 10, 1, 50, 1, "Mapas para descargar", "Revisa hasta esta cantidad de lotes seguidos y comprueba lo que falta después de cada uno. Cada lote consulta hasta 3 páginas y verifica hasta 8 conjuntos; se detiene antes si ya hay suficientes opciones."),
    field("discovery_retry_minutes", "Pausa entre grupos de lotes (minutos)", 1, 1, 60, 1, "Mapas para descargar", "Si al terminar el grupo todavía faltan misiones o reserva, espera este tiempo y continúa desde donde quedó. Las consultas individuales mantienen una separación breve."),
]
DEFAULTS = {item["key"]: item["default"] for item in SCHEMA}
_FIELDS = {item["key"]: item for item in SCHEMA}
_current = ContextVar("osu_coach_settings", default=None)


def validate_settings(updates, base=None):
    if not isinstance(updates, dict):
        raise ValueError("Los ajustes deben ser un objeto de valores.")
    unknown = set(updates) - set(DEFAULTS)
    if unknown:
        raise ValueError("Ajuste desconocido: " + ", ".join(sorted(map(str, unknown))))
    result = dict(DEFAULTS)
    if base is not None:
        result.update(base)
    result.update(updates)
    for key, spec in _FIELDS.items():
        value = result[key]
        if spec["type"] == "choice":
            if not isinstance(value, str) or value not in {item["value"] for item in spec["options"]}:
                raise ValueError(spec["label"] + ": elegí una opción de la lista.")
            continue
        if spec["type"] == "boolean":
            if type(value) is not bool:
                raise ValueError(spec["label"] + ": elegí activado o desactivado.")
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not spec["min"] <= value <= spec["max"] or not math.isfinite(value)
                or (spec["type"] == "integer" and int(value) != value)):
            raise ValueError(f"{spec['label']}: usá un valor entre {spec['min']:g} y {spec['max']:g}.")
        result[key] = int(value) if spec["type"] == "integer" else float(value)
    if result["recommendation_max_seconds"] and result["recommendation_min_seconds"] > result["recommendation_max_seconds"]:
        raise ValueError("La duración mínima no puede superar la duración máxima.")
    relations = [("session_plays", "reference_plays"), ("session_days", "reference_days"),
                 ("calibration_maps", "calibration_plays"), ("calibration_plays", "reference_plays"),
                 ("profile_min_maps", "profile_min_plays"), ("profile_min_sessions", "profile_min_plays"),
                 ("profile_min_plays", "reference_plays"), ("rank_required_maps", "reference_plays"),
                 ("challenge_maps", "session_plays"), ("consolidate_increment", "challenge_increment")]
    for lower, upper in relations:
        if result[lower] > result[upper]:
            raise ValueError(_FIELDS[lower]["label"] + " no puede superar " + _FIELDS[upper]["label"].lower() + ".")
    return result


def load_settings(values):
    """Retire the old duration ceiling without resetting other saved preferences."""
    if isinstance(values, dict):
        values = {key: value for key, value in values.items()
                  if key not in {"length_multiplier", "length_extra_seconds"}}
    return validate_settings(values)


def get_setting(key):
    values = _current.get()
    return (values or DEFAULTS)[key]


@contextmanager
def settings_context(values):
    token = _current.set({**DEFAULTS, **(values or {})})
    try:
        yield
    finally:
        _current.reset(token)


def coach_settings(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            with settings_context(self.settings):
                return method(self, *args, **kwargs)
    return wrapped


def settings_snapshot(values):
    return {"values": dict(values), "defaults": dict(DEFAULTS), "schema": copy.deepcopy(SCHEMA)}
