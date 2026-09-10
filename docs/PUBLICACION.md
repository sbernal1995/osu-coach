# Archivos de la distribución

La distribución pública contiene el código necesario para crear su propio estado local al ejecutarse. Se prepara a partir de una selección explícita de archivos.

## Manifiesto publicable

| Ruta | Contenido |
| --- | --- |
| `*.py` en la raíz | Aplicación, cálculo, almacenamiento, conectores públicos, configuración y demo ficticia |
| `web/index.html` | Panel local |
| `tests/test_*.py` | Pruebas con fixtures sintéticas |
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
- Mapas, canciones, replays, paquetes de canciones y archivos multimedia del juego.
- `output/`, capturas de sesiones, perfiles del navegador y `.playwright-cli/`.
- Tokens, credenciales, configuraciones locales, bases de datos y logs.
- Accesos directos del escritorio y archivos de herramientas o editores personales.

`.gitignore` evita añadirlos accidentalmente, pero no retira archivos ya versionados. Antes de publicar, revisá la lista de archivos preparada para el commit y su contenido.

## Verificación de una copia limpia

1. Copiá únicamente el manifiesto publicable a una carpeta vacía.
2. Creá un entorno con Python 3.11+ e instalá `requirements.txt`.
3. Ejecutá `python -m unittest discover -s tests -v`.
4. Iniciá `python app.py --demo --no-browser --port 8766` y abrí el panel local.
5. Comprobá que la demo genera sus propios datos y muestra nombres y mapas de ejemplo.
6. Para comprobar la lectura real, instalá tosu desde su fuente oficial y seguí el README. Ese programa y los datos generados continúan fuera de la distribución.

No se necesitan el catálogo ni las partidas de otra persona para abrir la demo. La instalación manual de tosu permite usar un clon nuevo sin incluir `vendor/`.

## Licencias

La licencia MIT cubre el código del coach. No concede derechos sobre osu!, tosu, canciones, mapas o replays. Las dependencias se instalan desde sus fuentes y conservan sus licencias propias.
