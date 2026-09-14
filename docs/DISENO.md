# Diseño del panel

El panel prioriza elegir una misión, conocer sus objetivos y jugarla. Su estructura conserva las funciones existentes y separa las consultas más extensas por vista.

| Vista | Información y acciones |
| --- | --- |
| Entrenar | Misiones por etapa, metas resumidas, evaluación completa desplegable, biblioteca y búsqueda de mapas nuevos. |
| Perfil | Radar, fortalezas, prioridades, evidencia y resultados por tags. |
| Progreso | Rango ganado, próximo rango, curva de referencia, reglas de ascenso y explicación de la progresión. |
| Historial | Partidas recientes, misiones completadas, retiradas y tandas anteriores. |
| Ajustes | Todos los parámetros existentes, validación y guardado. |

La franja superior muestra referencia, foco, precisión de la sesión y calibración en todas las vistas. Al desplegarla aparecen el cálculo, la identidad del perfil, el contexto de la sesión y los controles de calibración. En móvil los cuatro indicadores se distribuyen en dos filas. La navegación y los desplegables funcionan con teclado. Las actualizaciones conservan la misión abierta y su foco, así como los valores que se están editando en Ajustes.

## Jerarquía y densidad

La composición sigue la imagen de referencia compartida por el usuario: encabezado de 64 px, resumen de unos 100 px, panel de misiones con tres etapas y una ficha lateral de 374 px en escritorio. Las tarjetas muestran nombre, dificultad, artista y mapper, estadísticas y objetivo. El rosa destaca **Ver mapa / descargar**; **Copiar búsqueda** usa un botón con borde. Los nombres largos se abrevian en la tarjeta y se conservan completos en su descripción y en los detalles desplegables. La dificultad ocupa la insignia superior; el catálogo no aporta géneros musicales verificables para imitar las etiquetas del ejemplo.

Los objetivos visibles muestran solo los requisitos que completan la misión. La práctica se centra en un indicador, con control explícito cuando corresponde; la consolidación pide precisión y control de misses. El grado y los demás indicadores se conservan como orientaciones dentro del desplegable. La etiqueta de rol distingue una práctica, un desafío o una repetición de referencia. **Ver objetivos y detalles** contiene las comprobaciones por requisito, la razón de la meta, los tags y las demás formas de buscar. El combo se expresa con el multiplicador × y conserva la cantidad exacta exigida por la misión. La explicación desplegada identifica el requisito completo. El veto **No me gusta esta canción** también está dentro de ese desplegable.

La información secundaria se agrupa siguiendo la [divulgación progresiva de Nielsen Norman Group](https://www.nngroup.com/articles/progressive-disclosure/). La separación por tareas y la reducción de elementos decorativos aplican sus [pautas para aplicaciones complejas](https://www.nngroup.com/articles/complex-application-design/).

## Telaraña del perfil

**Control general** muestra mediciones recientes en el rango comparable del jugador. Usa escalas lineales ampliadas para distinguir resultados cercanos al control perfecto. El radio se calcula como `100 × (valor − centro) / (borde − centro)`, limitado a 0–100:

| Eje | Centro (radio 0) | Borde (radio 100) |
| --- | --- | --- |
| Precisión | ≤90 % | 100 % |
| Control de misses | ≥2 % de misses | 0 % de misses |
| Combo | ≤50 % del combo máximo | 100 % del combo máximo |
| Completar mapas | 0 % de partidas completadas | 100 % |
| Consistencia | ≥5 pp de dispersión | 0 pp de dispersión |

Estos límites son una elección de presentación del coach, no umbrales universales de habilidad. Son fijos: cambiar los objetivos de entrenamiento no altera la posición del jugador. La línea gris discontinua transforma las referencias configuradas con la misma escala (precisión, misses y combo; completar usa 100 % y consistencia, 1 pp). Se dibuja en ambas vistas y puede quedar dentro del perfil. Superar una referencia ya no lleva automáticamente al borde.

Cada eje muestra su valor real incluso si queda recortado por el límite interior. La vista completa explica los extremos de cada escala. Por ejemplo, con 97,45 % de precisión, 0,26 % de misses, 92,14 % de combo, 100 % de partidas completadas y 3,43 pp de dispersión, los radios son 74,5 / 87 / 84,28 / 100 / 31,4. Alcanzar el borde describe esa medición en los mapas observados, no dominio global de osu!. La dificultad comparable aparece junto al gráfico. Las etiquetas de fortaleza conservan la evaluación del perfil; poca dispersión también requiere precisión suficiente para considerarse una fortaleza.

**Por tipo de mapa** usa la misma escala ampliada de precisión (90–100 %) para los tags `skillset/jumps`, `skillset/streams`, `skillset/alt`, `skillset/tech`, `skillset/precision`, `skillset/reading` y `skillset/gimmick`. El orden es fijo. La referencia gris corresponde al umbral configurado de precisión. Los otros tags siguen en el análisis completo. Solo se dibujan observaciones del rango comparable; cada partida puede aportar a varios tags. Es precisión asociada a un tag, no una medición aislada de cada habilidad.

Los datos ausentes no se convierten en ceros ni se conectan con una línea que invente valores. Los puntos huecos y las líneas discontinuas indican evidencia inicial. Las mediciones y los conteos aparecen también como texto, siguiendo las [pautas de W3C para gráficos complejos](https://www.w3.org/WAI/tutorials/images/complex/). El radar no modifica el cálculo de progresión.

## Accesibilidad y verificación

Las pestañas usan nombres, estados y navegación con flechas, Inicio y Fin conforme al [patrón de pestañas WAI-ARIA](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/). El foco es visible, los botones principales tienen áreas de pulsación amplias y las tablas pueden desplazarse dentro de su región en pantallas pequeñas. El espaciado y los controles consideran el [tamaño mínimo de objetivos de WCAG 2.2](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html).

La revisión del ajuste a la referencia incluyó las cinco vistas a 1505, 1280, 1024, 768, 390 y 320 píxeles, navegación por teclado, despliegue de misiones durante una actualización, copia de búsqueda, edición y descarte de ajustes y estados vacíos o parciales del radar. Esto documenta las comprobaciones realizadas y no constituye una certificación integral de accesibilidad.

Los estilos de la composición de entrenamiento viven en `web/training.css`; `compact.css` conserva las vistas de análisis, progreso, historial y ajustes. Los iconos son SVG locales y la tipografía usa fuentes del sistema. El diseño no necesita peticiones externas para cargarlos.

## Evidencia de mejora

Perfil incorpora referencias por habilidad, con muestras, mapas y sesiones. Progreso compara partidas equivalentes de sesiones distintas y presenta los cambios por indicador; el verde lleva una marca textual y no oculta los retrocesos en otros indicadores. Una partida parcial se identifica expresamente y su precisión queda fuera de la comparación. Las misiones conservan la meta asignada; los cambios de reglas renuevan solo las pendientes sin intentos.
