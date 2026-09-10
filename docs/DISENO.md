# Diseño del panel

El panel prioriza elegir una misión, conocer sus objetivos y jugarla. Su estructura conserva las funciones existentes y separa las consultas más extensas por vista.

| Vista | Información y acciones |
| --- | --- |
| Entrenar | Misiones por etapa, metas resumidas, evaluación completa desplegable, biblioteca y búsqueda de mapas nuevos. |
| Perfil | Radar, fortalezas, prioridades, evidencia y resultados por tags. |
| Progreso | Rango ganado, próximo rango, curva de referencia, reglas de ascenso y explicación de la progresión. |
| Historial | Partidas recientes, misiones completadas, retiradas y tandas anteriores. |
| Ajustes | Todos los parámetros existentes, validación y guardado. |

El resumen de referencia, foco y calibración sigue disponible en todas las vistas. En móvil comienza plegado y muestra la referencia y el foco. La navegación y los desplegables funcionan con teclado. Las actualizaciones conservan la misión abierta y su foco, así como los valores que se están editando en Ajustes.

## Jerarquía y densidad

Se redujeron el tamaño del encabezado, los márgenes, las tarjetas y los textos repetidos en la lectura principal. El rosa destaca la acción de copiar una búsqueda; los estados combinan texto y color. El título, la dificultad y el mapper pueden ocupar varias líneas para conservar nombres largos y caracteres japoneses.

Los objetivos visibles expresan el grado mínimo, la precisión, el máximo de misses y el combo real requerido. **Objetivos y detalles** contiene las comprobaciones por requisito, la razón de la meta, los tags y las demás formas de buscar. El combo se expresa en cantidad de notas del combo, igual que la misión, y no se sustituye por un porcentaje aproximado.

La información secundaria se agrupa siguiendo la [divulgación progresiva de Nielsen Norman Group](https://www.nngroup.com/articles/progressive-disclosure/). La separación por tareas y la reducción de elementos decorativos aplican sus [pautas para aplicaciones complejas](https://www.nngroup.com/articles/complex-application-design/).

## Telaraña del perfil

**Control general** compara cada valor con una referencia, de 0 a 100:

| Eje | Transformación visual |
| --- | --- |
| Precisión | Precisión observada / umbral configurado de precisión × 100. |
| Control de misses | Umbral configurado de misses / porcentaje observado de misses × 100; sin misses, 100. Si el umbral es cero, cualquier miss queda por debajo del objetivo. |
| Combo | Porcentaje observado del combo máximo / umbral configurado de combo × 100. |
| Completar mapas | Porcentaje de mapas completados. |
| Consistencia | 1 punto porcentual / dispersión observada × 100; dispersión cero, 100. La dispersión es la desviación estándar entre las precisiones medias de los mapas. |

El dibujo limita los valores al intervalo 0–100. Un resultado mejor que la referencia se mantiene en el borde, y su valor original se muestra en la lista. La forma permite localizar qué referencias de control faltan; las etiquetas de fortaleza o práctica conservan la evaluación del perfil. Por ejemplo, la consistencia también requiere precisión suficiente para clasificarse como fortaleza, aunque tenga poca dispersión.

**Por tipo de mapa** usa directamente la precisión media entre 0 y 100 % para los tags `skillset/jumps`, `skillset/streams`, `skillset/alt`, `skillset/tech`, `skillset/precision`, `skillset/reading` y `skillset/gimmick`. El orden es fijo. Los otros tags siguen en el análisis completo. Solo se dibujan observaciones del rango comparable; cada partida puede aportar a varios tags.

Los datos ausentes no se convierten en ceros ni se conectan con una línea que invente valores. Los puntos huecos y las líneas discontinuas indican evidencia inicial. Las mediciones y los conteos aparecen también como texto, siguiendo las [pautas de W3C para gráficos complejos](https://www.w3.org/WAI/tutorials/images/complex/). El radar no modifica el cálculo de progresión.

## Accesibilidad y verificación

Las pestañas usan nombres, estados y navegación con flechas, Inicio y Fin conforme al [patrón de pestañas WAI-ARIA](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/). El foco es visible, los botones principales tienen áreas de pulsación amplias y las tablas pueden desplazarse dentro de su región en pantallas pequeñas. El espaciado y los controles consideran el [tamaño mínimo de objetivos de WCAG 2.2](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html).

La revisión incluyó las cinco vistas a 1440, 1024, 768, 390 y 320 píxeles, navegación por teclado, despliegue de misiones durante una actualización, copia de búsqueda, edición y descarte de ajustes y estados vacíos o parciales del radar. Esto documenta las comprobaciones realizadas y no constituye una certificación integral de accesibilidad.
