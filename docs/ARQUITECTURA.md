# Arquitectura del proyecto

El código instalable vive en `src/osu_coach/`. La raíz reúne la configuración del paquete y el lanzador, junto con la documentación y las pruebas.

```text
osu-coach/
├── pyproject.toml           Metadatos, paquetes y comando de inicio
├── requirements.txt        Dependencias de ejecución fijadas
├── iniciar.cmd             Preparación e inicio en Windows
├── README.md
├── LICENSE
├── src/
│   └── osu_coach/
│       ├── __init__.py
│       ├── __main__.py      Entrada para python -m osu_coach
│       ├── app.py           Coordinación y servidor del panel local
│       ├── settings.py      Criterios configurables y validación
│       ├── demo.py          Mapas y partidas ficticios
│       ├── core/            Reglas de entrenamiento y evaluación
│       ├── beatmaps/        Catálogo e identidad de dificultades
│       ├── integrations/    Lectura de tosu y fuentes públicas
│       ├── storage/         Persistencia del estado y cachés
│       └── web/
│           ├── index.html   Estructura y vistas del panel
│           ├── styles.css   Componentes y estados compartidos
│           ├── compact.css  Vistas de análisis y componentes compartidos
│           ├── training.css Composición del entrenamiento según la referencia
│           ├── app.js       Renderizado, navegación y acciones
│           └── profile-radar.js  Transformación visual y radar accesible
├── tests/                   Pruebas con datos ficticios
├── docs/
└── .github/workflows/       Comprobaciones automáticas
```

## Responsabilidades

| Área | Función |
| --- | --- |
| `app.py` | Recibe resultados, consulta las reglas y prepara el estado que consume el panel. También gestiona el servidor local y los trabajos en segundo plano. |
| `settings.py` | Define los ajustes permitidos. Cada entrenador aplica su propia configuración durante sus cálculos y búsquedas. |
| `core/` | Estima la referencia reciente, selecciona etapas y metas, analiza evidencia y evalúa misiones o ascensos. |
| `beatmaps/` | Lee el catálogo y compara la identidad de cada dificultad. También prepara las búsquedas que se copian al juego. |
| `integrations/` | Interpreta la telemetría de tosu y obtiene metadatos de páginas públicas. |
| `storage/` | Conserva misiones, progreso y cachés. Controla la continuidad del estado y de las búsquedas. |
| `web/` | Presenta las cinco vistas y envía acciones a la API local. El radar transforma datos únicamente para mostrarlos; las reglas de entrenamiento siguen en `core/`. |

Las reglas comparten los datos normalizados que coordina la aplicación. Los conectores externos y el almacenamiento tienen módulos separados para poder probar el entrenamiento con respuestas simuladas.

## Instalación e inicio

Desde la raíz del repositorio, con un entorno de Python 3.11+ preparado:

```console
python -m pip install -e .
python -m osu_coach --demo --port 8766
```

`pyproject.toml` define el paquete `osu-coach` y el comando `osu-coach`. El nombre que importa Python es `osu_coach`. La instalación editable enlaza el entorno con `src/`; una instalación convencional copia el paquete al entorno.

Las dependencias se leen de `requirements.txt`, que mantiene sus versiones fijadas. Los archivos HTML, CSS y JavaScript se declaran como datos del paquete para incluirlo también en una distribución wheel. La configuración sigue las [reglas de pyproject de PyPA](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/) y el [soporte de archivos de setuptools](https://setuptools.pypa.io/en/latest/userguide/datafiles.html).

## Código y archivos de ejecución

El programa busca el panel dentro del paquete instalado. Los datos propios del usuario se guardan en otro lugar:

| Ubicación | Uso |
| --- | --- |
| `data/live/` | Entrenamiento habitual |
| `data/demo/` | Demostración con datos ficticios |
| `vendor/tosu/` | Copia portátil de tosu instalada manualmente, si se usa |
| `.venv/` | Entorno de Python del lanzador |

Estas rutas se resuelven desde la carpeta donde se inicia el programa. `iniciar.cmd` cambia primero a la raíz del repositorio, manteniendo los datos existentes allí. Al ejecutar `python -m osu_coach` desde otra carpeta, esa carpeta pasa a ser la base; `--data-dir` permite fijar expresamente dónde guardar el entrenamiento.

El paquete distribuye código y el panel. Las carpetas de ejecución quedan excluidas del repositorio y de la instalación. Para actualizar desde la estructura anterior, conservá `data/` y la copia de tosu que hayas instalado, y ejecutá el lanzador actualizado o repetí `python -m pip install -e .` desde la raíz.

El servidor publica únicamente los cuatro recursos enumerados en `/assets/`. El navegador carga módulos nativos, sin compilación ni dependencias de frontend para usar el coach. Los criterios de presentación se explican en [Diseño del panel](DISENO.md).

## Preferencias de canciones y reserva

`core/song_identity.py` compara identificadores de conjuntos y pares de artista/título normalizados (Unicode y romanizados). `storage/song_ban_store.py` guarda exclusiones reversibles en SQLite por nombre del jugador; los mods, el cliente y la fecha de recalibración no cambian esa preferencia. La API valida la identidad de una misión actual antes de excluirla y mantiene la protección local de origen y token.

La exclusión se aplica antes de seleccionar recomendaciones o reservas. Las misiones retiradas por gusto musical quedan en `quest_skips` con motivo `song_banned`, conservando intentos y resultados anteriores. Un mapa en juego o con confirmación pendiente espera para retirarse. Permitir una canción otra vez no modifica dificultades ya jugadas ni el registro de misiones retiradas.

`DiscoveryStore` comparte la continuación entre las búsquedas por demanda, periódicas y manuales. El cursor del conector admite una cola de identificadores por verificar además de la posición en el catálogo. Así, el límite de ocho verificaciones por lote no descarta los conjuntos restantes. Las peticiones siguen acotadas y espaciadas. Los filtros de todas las etapas se envían al conector local y la reserva online se cuenta fuera de las canciones ya asignadas.
