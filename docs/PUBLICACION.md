# Archivos de la distribución

La distribución pública contiene el código necesario para crear su propio estado local al ejecutarse. Se prepara a partir de una selección explícita de archivos.

## Manifiesto publicable

| Ruta | Contenido |
| --- | --- |
| `src/osu_coach/` | Paquete Python: aplicación, reglas, catálogo, integraciones y almacenamiento |
| `src/osu_coach/web/index.html` | Panel local incluido en el paquete |
| `tests/` | Paquete de pruebas con fixtures sintéticas |
| `pyproject.toml` | Instalación del paquete, metadatos, licencia y comando de inicio |
| `requirements.txt` | Dependencia Python fijada |
| `iniciar.cmd` | Lanzador de Windows |
| `README.md` | Instalación y uso |
| `LICENSE` | Licencia MIT del coach |
| `.gitignore` | Exclusiones del estado privado y archivos generados |
| `.github/workflows/tests.yml` | Comprobaciones automáticas |
| `docs/*.md` | Documentación |
| `docs/images/` | Sólo imágenes de demostración ficticias, si se agregan después de revisarlas |

Los nombres de jugadores y los resultados de las fixtures deben ser inventados. Los IDs de mapas públicos utilizados en casos de búsqueda son ejemplos y no constituyen un historial del usuario.

## Archivos que quedan fuera

- `data/` y cualquier directorio elegido con `--data-dir`.
- `vendor/`, `.venv/`, otros entornos y binarios de terceros.
- `build/`, `dist/` y los directorios `*.egg-info/` que genera el empaquetado.
- Mapas, canciones, replays, paquetes de canciones y archivos multimedia del juego.
- `output/`, capturas de sesiones, perfiles del navegador y `.playwright-cli/`.
- Tokens, credenciales, configuraciones locales, bases de datos y logs.
- Accesos directos del escritorio y archivos de herramientas o editores personales.

`.gitignore` evita añadirlos accidentalmente, pero no retira archivos ya versionados. Antes de publicar, revisá la lista de archivos preparada para el commit y su contenido.

## Verificación de una copia limpia

1. Copiá únicamente el manifiesto publicable a una carpeta vacía.
2. Creá un entorno con Python 3.11+ y ejecutá `python -m pip install -e .` desde la raíz de esa copia.
3. Ejecutá `python -m unittest discover -s tests -v`.
4. Iniciá `python -m osu_coach --demo --no-browser --port 8766` y abrí el panel local.
5. Comprobá que la demo genera sus propios datos y muestra nombres y mapas de ejemplo.
6. Para comprobar la lectura real, instalá tosu desde su fuente oficial y seguí el README. Ese programa y los datos generados continúan fuera de la distribución.

La demo genera su propio catálogo y sus resultados ficticios. La instalación manual de tosu permite usar un clon nuevo con una carpeta `vendor/` propia.

`data/` y `vendor/` pertenecen a la carpeta desde donde se inicia el programa. `iniciar.cmd` usa la raíz del repositorio. La [guía de arquitectura](ARQUITECTURA.md) describe la estructura y la separación de los datos locales.

## Licencias

La licencia MIT cubre el código del coach. No concede derechos sobre osu!, tosu, canciones, mapas o replays. Las dependencias se instalan desde sus fuentes y conservan sus licencias propias.
