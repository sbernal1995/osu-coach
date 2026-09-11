# osu! coach

Entrenador local para **osu!standard**, pensado para retomar el juego y progresar desde tu rendimiento actual. Registra partidas nuevas con tosu, propone mapas con metas personales y conserva tus misiones y ascensos.

El cálculo usa las partidas registradas por el entrenador. Tu ranking global, los PP y tus mejores puntuaciones históricas de osu! quedan fuera de la referencia de entrenamiento.

**Proyecto comunitario independiente.** No está afiliado a osu!, ppy ni tosu.

## Qué ofrece

- Recomendaciones por etapas: entrar en ritmo, práctica principal y consolidación o desafío.
- Metas por dificultad: grado mínimo, precisión, misses y combo cuando se puede verificar.
- Perfil de fortalezas y prioridades, con evidencia por tipo de mapa y tags comunitarios.
- Misiones que conservan sus objetivos mientras las intentás y se renuevan al completarlas.
- Rango personal e historial de mejora separados de la referencia de rendimiento reciente.
- Búsqueda automática de mapas por descargar cuando faltan opciones apropiadas.
- Criterios ajustables desde **Configuración**, sin editar el código.

## Requisitos

Para jugar y registrar partidas, la configuración preparada es **Windows x64 con Python 3.11 o posterior y Node.js 22 con npm**, osu! stable o lazer y tosu ejecutándose localmente. Python de 64 bits es la opción habitual.

Las estrellas se calculan localmente con el [motor nativo de osu!lazer utilizado por tosu](https://github.com/tosuapp/lazer-calculator), fijado en la versión 0.6.1-20260729-main.0. El inicio prepara esta dependencia en una carpeta local mediante npm; los mapas no se envían a un servidor.

Para leer archivos y obtener su BPM también se usa `rosu-pp-py==4.0.2`, cuyo [paquete oficial requiere Python >=3.11](https://pypi.org/project/rosu-pp-py/4.0.2/). Cuando hay una distribución compilada para tu sistema, su instalación con pip no necesita Rust; otras plataformas pueden requerir compilar la biblioteca.

La demo puede ejecutarse solo con Python. Las pruebas completas necesitan también Node.js y el motor de dificultad, sin tener osu! o tosu instalados. El motor nativo tiene distribuciones para Windows y Linux x64; macOS y ARM pueden usar la demo. El lanzador `iniciar.cmd` es para Windows; en otros sistemas se usan los comandos de Python. La detección automática de carpetas está preparada para Windows.

## Instalación

1. Instalá [Python desde su sitio oficial](https://www.python.org/downloads/). En Windows, habilitá el acceso a Python desde la terminal o instalá su lanzador `py`. Instalá también [Node.js con npm](https://nodejs.org/en/download) y abrí otra terminal.
2. Descargá el código del repositorio con **Code → Download ZIP** y extraelo en una carpeta donde puedas guardar archivos. También podés usar Git:

   ```powershell
   git clone https://github.com/sbernal1995/osu-coach.git
   cd osu-coach
   ```

3. En Windows, abrí `iniciar.cmd`. Crea un entorno `.venv`, instala este proyecto en modo editable, prepara el motor de dificultad y abre el panel en [127.0.0.1:8765](http://127.0.0.1:8765/). La primera instalación necesita Internet.

Para preparar el entorno manualmente:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

En Linux o macOS, el equivalente es `python3 -m venv .venv` y `.venv/bin/python -m pip install -e .`. La [documentación de Python explica los entornos virtuales](https://docs.python.org/3/library/venv.html).

Ejecutá la instalación desde la carpeta del repositorio. El comando del programa es **`python -m osu_coach`** con el Python del entorno instalado; también se crea el comando `osu-coach`. La instalación editable permite aplicar los cambios de `src/osu_coach/` al volver a abrir el programa.

### Probar primero la demo

```powershell
.\iniciar.cmd --demo --port 8766
```

O, con el entorno ya instalado:

```powershell
.\.venv\Scripts\python.exe -m osu_coach --demo --port 8766
```

En Linux o macOS: `.venv/bin/python -m osu_coach --demo --port 8766`.

Abrí [127.0.0.1:8766](http://127.0.0.1:8766/). La demo crea mapas y partidas ficticios en `data/demo/`, separados de `data/live/`. No inicia tosu ni las consultas automáticas de mapas y tags. Las demostraciones no incluyen canciones, replays ni puntuaciones personales.

## Conectar tus partidas

1. Descargá tosu desde sus [releases oficiales](https://github.com/tosuapp/tosu/releases/latest), extraelo en su propia carpeta y ejecutá `tosu.exe`.
2. Abrí [127.0.0.1:24050](http://127.0.0.1:24050/) para comprobar que aparece su panel. Su [guía oficial](https://github.com/tosuapp/tosu#installation-guide) admite stable y lazer.
3. En la configuración de tosu, conservá el servidor local `127.0.0.1`, puerto `24050`, y el cálculo habilitado (`CALCULATE_PP=true`). El coach utiliza las estrellas calculadas; su progresión no usa PP. No necesita los overlays.
4. Abrí osu! y ejecutá:

   ```powershell
   .\iniciar.cmd --no-tosu
   ```

5. Jugá una partida nueva y dejá aparecer la pantalla de resultados. El panel del coach mostrará el resultado y las misiones.

El proyecto no distribuye tosu ni descarga sus binarios. Si preferís que el coach inicie una copia portátil, podés colocar por tu cuenta los archivos oficiales de tosu en `vendor/tosu/`, con `tosu.exe` dentro. Esa carpeta permanece excluida del repositorio. Al cerrar el coach se cierra únicamente la instancia de tosu que haya iniciado.

### Elegir la carpeta de mapas

La detección automática prueba estas ubicaciones:

| Cliente | Carpeta habitual |
| --- | --- |
| osu! lazer | `%APPDATA%\osu\files` |
| osu! stable | `%LOCALAPPDATA%\osu!\Songs` |

Si ambas existen, se elige lazer. Si tu instalación está en otra carpeta o querés usar stable, indicá su ubicación:

```powershell
.\iniciar.cmd --no-tosu --maps "D:\Juegos\osu!\Songs"
```

Para lazer, elegí su carpeta `files`; para stable, `Songs`. El lector abre archivos de mapas y crea su propio índice. Después de importar mapas nuevos, pulsá **Volver a leer mapas**.

### Opciones útiles

```powershell
# Otro puerto para el panel
.\iniciar.cmd --port 8766

# Guardar el entrenamiento en una carpeta propia
.\iniciar.cmd --no-tosu --data-dir "D:\Entrenamiento-osu"

# Otro puerto local de tosu
.\iniciar.cmd --no-tosu --tosu-url "http://127.0.0.1:24051/json/v2"

# Consultar todas las opciones
.\iniciar.cmd --help
```

El servidor de tosu debe ser local. Las carpetas y la conexión se eligen mediante estas opciones de inicio.

`data/` y `vendor/` se resuelven desde la carpeta donde iniciás el programa. `iniciar.cmd` se sitúa siempre en la carpeta del repositorio, por lo que conserva las ubicaciones de las versiones anteriores. Con `python -m osu_coach`, ejecutá desde esa misma carpeta para recuperar tus datos; `--data-dir` permite elegir una ubicación explícita.

## Configurar el entrenamiento

En el panel, abrí la vista **Ajustes** o el enlace **Configuración**. Cada criterio muestra su explicación, valor actual y límites admitidos. Ajustá los valores y pulsá **Guardar ajustes**. **Restaurar valores iniciales** recupera la configuración de partida.

Los controles permiten adaptar la memoria de resultados, la evidencia necesaria para el perfil y los tags, la progresión y los filtros de descubrimiento. Los valores vigentes que muestra el panel son la referencia; los ejemplos de esta guía describen la configuración inicial.

Por ejemplo, inicialmente la referencia considera hasta **100 partidas en 30 días**, mientras que la sesión usa hasta **20 partidas en 7 días**. Los resultados recientes pesan más y la repetición de un mismo mapa tiene un peso limitado. La calibración inicial requiere **5 partidas en 3 dificultades distintas**.

Las metas de las misiones que ya tenés asignadas permanecen fijas. Los criterios vigentes se aplican al preparar nuevas propuestas. Recalibrar establece un nuevo comienzo para estimar tu rendimiento; conserva las partidas guardadas y los rangos ganados.

### Duración y mods

En **Ajustes → Recomendaciones: duración y mods** podés configurar:

- **Duración mínima y máxima**, en formato **mm:ss**. `00:00` deja ese extremo sin límite. Por ejemplo, `01:30` y `04:00` admiten mapas entre un minuto y medio y cuatro minutos. La duración es el tiempo jugable y refleja la velocidad del mod: DT ×1,5 acorta un mapa de 3 minutos a 2 minutos; HT ×0,75 lo lleva a 4 minutos.
- **Mantener los mods del perfil**, el comportamiento inicial.
- **Libre**, para comparar Sin mods, HD, HR, DT, HT, HD+HR, HD+DT y HD+HT.
- **Forzar una combinación**, para que las nuevas misiones usen exclusivamente esa opción. DT y HT usan su velocidad estándar.

Las tarjetas indican los mods requeridos. Las estrellas, BPM, AR, duración y combo se calculan con esas condiciones. Las metas usan evidencia de la misma combinación; si todavía no hay resultados, se presentan como provisionales. El primer cálculo de variantes de una biblioteca grande puede tardar unos minutos, funciona en segundo plano y se guarda para las próximas sesiones.

Cambiar estos filtros renueva las misiones pendientes que dejaron de cumplirlos y que todavía no intentaste. Las misiones en práctica conservan sus condiciones, objetivos e intentos, aunque queden fuera del filtro nuevo. La exclusión de dificultades ya jugadas se mantiene: Libre no propone repetir un mismo mapa cambiándole los mods.

## Cómo usar las recomendaciones

Podés empezar con un mapa de **Entrar en ritmo**, continuar con dos o tres de **Práctica principal** y cerrar con **Consolidar**. El desafío aparece cuando los resultados recientes cumplen las condiciones que indica el panel.

Cada tarjeta explica la meta para ese mapa. Es un objetivo de entrenamiento, no una predicción del resultado. Para completar una misión, una misma partida nueva debe cumplir todos los requisitos y terminar el mapa. Si falta información, el coach deja esa comprobación pendiente.

**Copiar búsqueda** copia el ID de la dificultad cuando está disponible; pegalo en la búsqueda del juego. osu! [admite buscar mapas por ID](https://osu.ppy.sh/wiki/en/Beatmap_search). Las alternativas por título o mapper pueden devolver varios resultados: elegí la dificultad de la tarjeta.

Las nuevas misiones evitan dificultades exactas ya jugadas en ese perfil. Otras dificultades de la misma canción pueden seguir apareciendo. El perfil se separa por jugador, cliente y configuración de mods. Las partidas que cumplen las condiciones de una misión con mods recomendados se vinculan a la progresión que propuso esa misión; se conservan los mods y las estrellas observados. Jugar una combinación distinta fuera de una misión mantiene su perfil separado.

Si una canción no te gusta, abrí **Ver objetivos y detalles** de su misión y usá **No me gusta esta canción**. Se excluyen todas sus dificultades y otros sets con el mismo artista y título, incluidas las variantes romanizadas conocidas. La misión se reemplaza automáticamente cuando hay otra candidata. Si estás jugando ese mapa o falta confirmar su resultado, el reemplazo espera para conservar el intento. Podés deshacerlo en **Ajustes → Canciones excluidas**. El veto se guarda por jugador, persiste entre clientes, mods y recalibraciones, y conserva partidas y logros. Títulos de remixes o versiones distintos se tratan como canciones distintas.

Los tags describen tendencias observadas en mapas comparables. Una asociación con un tag no identifica por sí sola el patrón concreto donde fallaste. El panel muestra cuánta evidencia respalda cada conclusión.

La referencia reciente puede subir o bajar. El rango personal ganado se conserva y exige demostrar resultados sólidos en varias dificultades distintas, según los criterios configurados.

## Mapas por descargar

El coach busca canciones **antiguas y recientes**: nuevas para tus recomendaciones no significa recién publicadas. La [búsqueda pública de osu! sin iniciar sesión](https://github.com/ppy/osu-web/blob/master/app/Libraries/Search/BeatmapsetSearchRequestParams.php) ordena por fecha de clasificación y limita los filtros disponibles. Por eso el coach recorre páginas hacia mapas más antiguos y aplica localmente tus límites de estrellas, AR y BPM, junto con tus exclusiones. La duración solo excluye mapas si configurás un mínimo o un máximo; no modifica tu dificultad de referencia. En «Entrar en ritmo» se da una preferencia suave a los mapas de hasta 02:30, configurable en Ajustes (00:00 la desactiva); todas las etapas respetan los límites de duración que elijas. Los antiguos ajustes de factor y margen de duración se retiran automáticamente al actualizar, conservando las demás preferencias.

La pantalla mantiene hasta **9 misiones** y prepara una **reserva de 6 canciones online por etapa** fuera de las misiones actuales. La reserva evita que la búsqueda se detenga apenas se llena el tablero. Podés configurar su tamaño en **Ajustes → Mapas para descargar**, o ponerlo en 0 para buscar solo cuando falten misiones. Los filtros y los conteos de reserva aparecen en **Filtros y frecuencia de búsqueda**.

Cuando faltan misiones o reserva, el coach recorre **hasta 10 lotes seguidos**, recalculando lo que falta después de cada lote. Se detiene antes si ya hay suficientes opciones. Si todavía faltan, espera **1 minuto** y continúa con otro grupo; cuando completa las opciones, vuelve a la revisión habitual de **24 horas**. La cantidad de lotes, la pausa entre grupos y la frecuencia habitual se configuran en **Ajustes → Mapas para descargar**. Funciona con el panel cerrado mientras el servicio del coach siga abierto.

Cada lote lee hasta tres páginas y verifica hasta ocho conjuntos. Las consultas individuales mantienen una separación mínima de 1,1 segundos. Los conjuntos que quedan pendientes se guardan para el siguiente lote. El panel muestra el lote actual, la continuación y las pausas. Al llegar al final de la fuente consultada se espera una hora; ante un error de red, 15 minutos. Las búsquedas automáticas, periódicas y manuales conservan el punto de continuación, incluso al cerrar el coach. Un lote vacío no termina el recorrido. Cambiar de perfil o ampliar sustancialmente los filtros puede reiniciarlo para revisar mapas antes descartados.

Con la configuración inicial, un conjunto descargable necesita cumplir **a la vez**:

- Valoración de al menos **8/10**.
- Al menos **10 votos** que respalden esa nota.
- Al menos **10.000 partidas registradas en el conjunto**.

Los favoritos se muestran como información adicional y no sustituyen esos requisitos. Los umbrales se pueden consultar y ajustar en **Configuración**. Los datos de valoración y reproducciones pertenecen al conjunto de dificultades; no prueban la calidad específica de cada dificultad.

El botón **Ver / descargar** abre osu!. La descarga e importación se realizan desde el juego o el sitio oficial. En modo Libre o al forzar un mod, se obtiene el archivo público de definición de la dificultad para calcular sus estrellas antes de recomendarla. Esto no descarga la canción ni importa el mapa al juego. Si mantenés los mods del perfil y no hay estrellas online comparables, la búsqueda se pausa.

La fuente pública tiene cobertura limitada y puede cambiar de formato. El panel informa los errores y conserva los candidatos válidos guardados.

## Datos y privacidad

El coach guarda localmente la configuración, el catálogo, las partidas aceptadas, las misiones y el progreso. La carpeta habitual es `data/live/`, relativa a la carpeta de inicio; copiá esa carpeta para hacer una copia de seguridad con la aplicación cerrada.

Las consultas públicas de mapas y tags envían identificadores públicos de conjuntos y parámetros de paginación. El rendimiento, el nombre del jugador, las listas de exclusión y los criterios de entrenamiento se procesan localmente. El coach no pide credenciales de osu! ni usa un servicio de análisis externo.

`data/`, `.venv/`, `vendor/`, registros, capturas y replays están excluidos del repositorio. Si elegís una carpeta propia con `--data-dir`, mantenela fuera del código que compartís. Consultá [Privacidad y archivos locales](docs/PRIVACIDAD.md) antes de adjuntar archivos a un reporte.

## Limitaciones

- El entrenamiento está implementado para **osu!standard**.
- tosu debe observar la partida y el resultado. Una pantalla histórica al iniciar no cuenta como una partida nueva.
- Si falta una fecha verificable, el panel puede pedirte confirmar el intento. Las transiciones muy rápidas pueden perderse entre lecturas.
- El grado de stable depende también de los juicios; la precisión por sí sola no permite prometer una S. Los datos ausentes permanecen pendientes.
- El catálogo utiliza una versión fijada del cálculo de osu!lazer. Al actualizar ese motor, se descarta la caché de estrellas anterior y se recalcula la biblioteca. Las misiones conservan sus objetivos; las tarjetas muestran las estrellas actualizadas de la misma dificultad. Las partidas y los ascensos ya registrados se conservan. Una versión futura del juego puede volver a cambiar la fórmula.
- Las dificultades aún no descargadas usan las estrellas publicadas por osu!; al importarlas se calculan localmente. Las variantes con mods solo se recomiendan después de calcularlas. Una variante que no pudo calcularse queda fuera hasta reintentarse.
- Los criterios son heurísticas de práctica; todavía no constituyen un método de entrenamiento validado.

## Problemas frecuentes

| Situación | Qué comprobar |
| --- | --- |
| Python no se encuentra o es demasiado antiguo | Instalá Python 3.11+ y abrí otra terminal. El lanzador prueba `py` y después `python`. |
| Falla la instalación de rosu-pp-py | Revisá la versión y arquitectura de Python. Consultá la [instalación de la biblioteca](https://github.com/MaxOhn/rosu-pp-py#installing-rosu-pp-py). |
| Falta Node.js o falla el motor de dificultad | Instalá Node.js con npm y abrí otra terminal. El primer inicio necesita Internet para preparar el motor. |
| El panel espera a tosu | Abrí osu! y tosu; verificá su panel local y el puerto configurado. |
| No hay mapas en la biblioteca | Elegí `files` o `Songs` con `--maps` y volvé a leer los mapas. |
| Una dificultad importada sigue figurando como descargable | Pulsá **Volver a leer mapas** cuando termine la importación. |
| El panel anterior sigue abierto | Cerrá el entrenador desde su panel antes de iniciar otra versión o usá otro puerto. |
| No aparecen nuevos candidatos | Revisá los límites de la etapa y el estado de búsqueda; puede faltar una opción que cumpla todos los criterios. |

## Desarrollo y pruebas

El código está organizado en `src/osu_coach/`. La [guía de arquitectura](docs/ARQUITECTURA.md) explica los paquetes y sus responsabilidades. Instalá el proyecto antes de ejecutar las pruebas:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m osu_coach.integrations.lazer_calculator --install
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Las pruebas usan datos ficticios, carpetas temporales y respuestas públicas simuladas. Algunas pruebas de integración abren un servidor HTTP local temporal. No requieren osu!, tosu ni credenciales. El flujo de GitHub Actions instala el proyecto y ejecuta las pruebas en Windows y Linux con Python 3.11 y 3.12, Node.js 22 y el motor fijado.

Para contribuir, describí el comportamiento esperado y un caso reproducible con datos ficticios. Evitá adjuntar bases de datos, capturas con nombres personales o logs completos. La [guía de publicación](docs/PUBLICACION.md) detalla los archivos que forman parte del proyecto.

## Licencia y fuentes

El código del coach se distribuye bajo [licencia MIT](LICENSE). Los [componentes de terceros](THIRD_PARTY_NOTICES.md) y los programas externos conservan sus propias licencias; los mapas, canciones y replays no forman parte de esta distribución.

Fuentes principales: [tosu](https://github.com/tosuapp/tosu), [rosu-pp-py](https://github.com/MaxOhn/rosu-pp-py), [almacenamiento de lazer](https://github.com/ppy/osu/wiki/User-file-storage), [tags de mapas](https://osu.ppy.sh/wiki/en/Beatmap/Beatmap_tags) y [grados de osu!](https://osu.ppy.sh/wiki/en/Gameplay/Grade).

## Panel compacto y perfil visual

La navegación separa **Entrenar**, **Perfil**, **Progreso**, **Historial** y **Ajustes**. Las misiones muestran sus objetivos principales y conservan la evaluación completa, los tags y las alternativas de búsqueda en **Objetivos y detalles**. En pantallas pequeñas, **Tu sesión** despliega la referencia y la calibración.

El perfil incluye una telaraña con dos lecturas:

- **Control general:** precisión, control de misses, combo, completar mapas y consistencia, respecto de las referencias del coach. El borde significa alcanzar la referencia; los valores reales se conservan debajo.
- **Por tipo de mapa:** precisión en mapas comparables con siete tags de habilidades, en un orden fijo. Los puntos huecos señalan evidencia inicial y los ejes sin datos quedan sin punto.

La telaraña solo representa resultados existentes. No añade puntos ni cambia recomendaciones, metas o rangos. Los umbrales configurados de precisión, misses y combo se reflejan en el gráfico. [Criterios de diseño y lectura de las escalas](docs/DISENO.md).

Para comprobar las transformaciones del radar durante el desarrollo, con Node.js 22:

```console
node --test tests/test_web_ui.mjs
```

Node.js ejecuta tanto estas pruebas como el motor local de dificultad. Después de preparar el motor, el cálculo de estrellas funciona sin conexión.
