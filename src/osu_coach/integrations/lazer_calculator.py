"""Versioned local osu!lazer calculator, shared by scans and mod calculations.

The native library is installed separately with npm's locked integrity checks.
There is no fallback to old star formulas if the runtime is missing or fails.
"""
from __future__ import annotations

import atexit
import base64
import copy
import json
import math
import os
import platform
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading

VERSION = "0.6.1-20260729-main.0"
CALCULATOR_ID = "osu!lazer " + VERSION
ASSETS = Path(__file__).resolve().parents[1] / "calculator"
RUNTIME = Path(os.environ.get("OSU_COACH_CALCULATOR_DIR", Path.cwd() / "data/runtime/calculator")).resolve()
PACKAGE = "@tosuapp/lazer-calculator-prebuilt"
PROCESS_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def node_path():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Falta Node.js con npm. Instalá Node.js y volvé a abrir el coach.")
    return node


def native_package():
    if platform.system() not in {"Windows", "Linux"} or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("El motor de dificultad requiere Windows o Linux de 64 bits (x64). La demo sigue disponible.")
    return "@tosuapp/lazer-calculator-" + ("win32" if os.name == "nt" else "linux") + "-x64"


def installed():
    try:
        metadata = json.loads((RUNTIME / "node_modules" / PACKAGE / "package.json").read_text(encoding="utf-8"))
        return ((RUNTIME / "node_modules" / native_package() / "binding.node").is_file()
                and metadata.get("version") == VERSION
                and (RUNTIME / "package-lock.json").read_bytes() == (ASSETS / "package-lock.json").read_bytes())
    except (OSError, ValueError):
        return False


def install():
    """Explicit setup; never downloads from the calculation or HTTP request path."""
    native_package()
    node = node_path()
    if installed():
        return
    npm_cli = Path(node).parent / "node_modules/npm/bin/npm-cli.js"
    npm = [node, str(npm_cli)] if npm_cli.is_file() else [shutil.which("npm") or "npm"]
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(ASSETS / name, RUNTIME / name)
    print("Preparando el motor de dificultad de osu!lazer…", flush=True)
    try:
        subprocess.run([*npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=RUNTIME,
                       check=True, timeout=240, **PROCESS_FLAGS)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("No se pudo instalar el motor de dificultad. Revisá Node.js, npm y la conexión.") from error
    if not installed():
        raise RuntimeError("La instalación del motor de dificultad quedó incompleta.")


# Legacy flags accepted by the public catalog API. Non-standard/key-count mods
# are deliberately rejected rather than silently calculating different mods.
LEGACY = {1: "NF", 2: "EZ", 4: "TD", 8: "HD", 16: "HR", 32: "SD", 64: "DT",
          128: "RX", 256: "HT", 512: "NC", 1024: "FL", 2048: "AT", 4096: "SO",
          8192: "AP", 16384: "PF", 1048576: "FI", 536870912: "V2", 1073741824: "MR"}


SUPPORTED_MODS = (set(LEGACY.values()) - {"FI"}) | {"CL", "DA", "DC"}


def normalise_mods(mods, *, lazer=True, clock_rate=None):
    def expand(value):
        if value is None:
            return []
        if isinstance(value, bool):
            raise ValueError("Mods inválidos.")
        if isinstance(value, int):
            if value < 0 or value & ~sum(LEGACY):
                raise ValueError("Estos mods no se pueden calcular para osu!standard.")
            return [{"acronym": acronym} for flag, acronym in LEGACY.items() if value & flag]
        if isinstance(value, str):
            value = value.upper()
            if value in ("", "NM"):
                return []
            if not re.fullmatch(r"(?:[A-Z0-9]{2})+", value):
                raise ValueError("Mods inválidos.")
            return [{"acronym": value[i:i+2]} for i in range(0, len(value), 2)]
        if isinstance(value, dict):
            if not isinstance(value.get("acronym"), str) or not re.fullmatch(r"[A-Z0-9]{2,3}", value["acronym"]):
                raise ValueError("Mods inválidos.")
            settings = value.get("settings", {})
            if settings is None:
                settings = {}
            if not isinstance(settings, dict):
                raise ValueError("Ajustes de mods inválidos.")
            return [{"acronym": value["acronym"], "settings": copy.deepcopy(settings)}]
        if isinstance(value, (list, tuple)):
            return [mod for entry in value for mod in expand(entry)]
        raise ValueError("Mods inválidos.")
    result = expand(mods)
    if any(mod["acronym"] not in SUPPORTED_MODS for mod in result):
        raise ValueError("Estos mods todavía no se pueden calcular de forma comparable.")
    acronyms = {mod["acronym"] for mod in result}
    result = [m for m in result if not (m["acronym"] == "DT" and "NC" in acronyms)
              and not (m["acronym"] == "SD" and "PF" in acronyms)]
    if clock_rate is not None:
        if not math.isfinite(clock_rate) or not .5 <= clock_rate <= 2:
            raise ValueError("La velocidad de reproducción debe estar entre 0,5 y 2.")
        result = [m for m in result if m["acronym"] not in {"DT", "NC", "HT", "DC"}]
        if clock_rate != 1:
            result.append({"acronym": "DT" if clock_rate > 1 else "HT", "settings": {"speed_change": clock_rate}})
    speed_mods = [m for m in result if m["acronym"] in {"DT", "NC", "HT", "DC"}]
    if len(speed_mods) > 1:
        raise ValueError("No se pueden combinar mods con velocidades diferentes.")
    for mod in speed_mods:
        rate = mod.get("settings", {}).get("speed_change")
        lower, upper = (1.01, 2) if mod["acronym"] in {"DT", "NC"} else (.5, .99)
        if rate is not None and (isinstance(rate, bool) or not isinstance(rate, (int, float))
                                 or not math.isfinite(rate) or not lower <= rate <= upper):
            raise ValueError("La velocidad del mod está fuera del rango admitido por osu!lazer.")
    if not lazer and not any(m["acronym"] == "CL" for m in result):
        result.append({"acronym": "CL"})
    return result


class Calculator:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.messages = None
        self.calls = 0

    def close(self):
        with self.lock:
            process, self.process = self.process, None
            if process is None:
                return
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()

    def _read(self):
        try:
            message = self.messages.get(timeout=30)
        except queue.Empty as error:
            self.close()
            raise RuntimeError("El motor de dificultad tardó demasiado. Volvé a leer los mapas.") from error
        if message is None:
            self.close()
            raise RuntimeError("El motor de dificultad se cerró. Volvé a leer los mapas.")
        try:
            response = json.loads(message)
            if not isinstance(response, dict):
                raise ValueError("Expected a response object")
            return response
        except (ValueError, TypeError) as error:
            self.close()
            raise RuntimeError("El motor de dificultad devolvió una respuesta inválida.") from error

    def _start(self):
        if not installed():
            raise RuntimeError("Falta preparar el motor: ejecutá python -m osu_coach.integrations.lazer_calculator --install.")
        self.messages = messages = queue.Queue()
        self.process = process = subprocess.Popen(
            [node_path(), "--expose-gc", str(ASSETS / "worker.cjs"), str(RUNTIME)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", **PROCESS_FLAGS)
        def receive():
            try:
                for line in process.stdout:
                    messages.put(line)
            except (OSError, ValueError):
                pass
            finally:
                messages.put(None)
        threading.Thread(target=receive, daemon=True, name="difficulty-reader").start()
        if self._read().get("ready") != VERSION:
            self.close()
            raise RuntimeError("La versión del motor de dificultad no coincide con la del coach.")
        self.calls = 0

    def calculate(self, content, mods=None, *, lazer=True, clock_rate=None):
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("El mapa es demasiado grande.")
        request = {"content": base64.b64encode(content).decode("ascii"),
                   "mods": normalise_mods(mods, lazer=lazer, clock_rate=clock_rate)}
        with self.lock:
            if self.process is not None and (self.process.poll() is not None or self.calls >= 256):
                self.close()
            if self.process is None:
                self._start()
            try:
                self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as error:
                self.close()
                raise RuntimeError("No se pudo enviar el mapa al motor de dificultad.") from error
            response = self._read()
            self.calls += 1
            if "error" in response:
                raise ValueError(response["error"])
            result = response.get("result")
            if not isinstance(result, dict) or not all(isinstance(result.get(key), (int, float))
                    and math.isfinite(result[key]) for key in ("stars", "aim", "speed", "reading", "ar", "od", "cs", "max_combo", "object_count")):
                raise RuntimeError("El motor no entregó atributos de dificultad válidos.")
            return {**result, "calculator": CALCULATOR_ID}


calculator = Calculator()
atexit.register(calculator.close)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Preparar el motor local de osu!lazer")
    parser.add_argument("--install", action="store_true")
    if parser.parse_args().install:
        install()
