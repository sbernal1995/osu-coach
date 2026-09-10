# Privacidad y archivos locales

## Qué guarda el coach

El directorio de datos contiene la configuración elegida, las rutas del catálogo, los resultados que aceptaste, las misiones y sus intentos, los rangos ganados y las cachés de metadatos públicos. Los resultados pueden incluir el nombre del jugador, fechas, identidad del mapa, precisión, misses, combo y mods.

Por defecto se usa `data/live/`. La demo usa `data/demo/` con partidas y mapas inventados. Ambas rutas parten de la carpeta donde se inicia `python -m osu_coach`. El lanzador `iniciar.cmd` usa siempre la raíz del repositorio. La opción `--data-dir` permite elegir otra ubicación.

El código instalado y el panel están en el paquete `osu_coach`. La instalación del paquete deja los datos de entrenamiento en su carpeta de ejecución. Si actualizás desde una versión anterior, conservá `data/` y ejecutá el lanzador desde el mismo repositorio para seguir usando ese historial.

Para una copia de seguridad, cerrá el entrenador y copiá su carpeta de datos completa. Al actualizar el código, conservá esa carpeta. La base de datos y su configuración son privadas: no hace falta publicarlas para ejecutar o mejorar el programa.

## Lecturas locales

El coach consulta la API local de tosu y lee archivos del catálogo de mapas. tosu es un programa independiente que obtiene información del juego; sus opciones y su comportamiento se documentan en su [repositorio oficial](https://github.com/tosuapp/tosu).

El panel escucha en `127.0.0.1`. Las operaciones de escritura del panel usan un token temporal de la sesión local. Ese token no es una clave de osu! y no debe copiarse a reportes.

## Consultas de Internet

El coach consulta páginas públicas de osu! para obtener tags y candidatos de mapas. Las solicitudes usan identificadores públicos de conjuntos y parámetros de paginación. Los filtros de dificultad y los requisitos de entrenamiento se aplican dentro del coach. No se envían el nombre del jugador, sus puntuaciones, la lista de mapas excluidos ni la configuración personal.

El servidor remoto puede registrar las solicitudes y la dirección IP, como ocurre al visitar una página web. La búsqueda no necesita cookies, una cuenta ni una clave de API. No hay un servicio de analítica del coach.

Al preparar el entorno con `python -m pip install -e .`, pip instala el proyecto local y descarga las dependencias indicadas en `requirements.txt`, además de las herramientas de construcción declaradas en `pyproject.toml`. tosu se obtiene y ejecuta por separado. Abrir un enlace de descarga lleva al sitio oficial de osu!, donde se aplican sus propias condiciones.

La demo no inicia las búsquedas automáticas ni la lectura de tosu.

## Qué compartir al reportar un problema

Es suficiente indicar la versión de Python, el cliente de osu! usado, la acción realizada y el mensaje de error relevante. Podés reproducir errores del panel con `--demo`.

Antes de adjuntar una captura o fragmento de log, revisá nombres, rutas y tokens. Para una prueba automatizada, construí una fixture inventada en el propio test. El repositorio excluye carpetas de datos, replays, mapas, binarios, registros y capturas de la sesión.

No adjuntes `coach.sqlite3`, `config.json`, archivos de configuración de tosu ni un directorio `data/` completo. Si configuraste datos fuera de esa carpeta, comprobá también su ubicación antes de compartir el proyecto.
