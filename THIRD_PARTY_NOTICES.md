# Componentes de terceros

El código propio de osu! coach se distribuye bajo la licencia MIT del archivo `LICENSE`.

El cálculo de dificultad utiliza **@tosuapp/lazer-calculator-prebuilt 0.6.1-20260729-main.0**, de storycraft y los contribuidores de tosu, publicado bajo **LGPL-3.0-only**. Incluye su distribución nativa correspondiente a Windows o Linux x64.

- [Código fuente y avisos del proyecto](https://github.com/tosuapp/lazer-calculator)
- [Licencia LGPL del motor](https://github.com/tosuapp/lazer-calculator/blob/main/LICENSE)
- [Paquete y versiones publicadas](https://www.npmjs.com/package/@tosuapp/lazer-calculator-prebuilt)
- [Proyecto osu!lazer utilizado por el motor](https://github.com/ppy/osu)

El motor se descarga por separado con npm y se carga en un proceso local de Node.js. Sus binarios no se incluyen en este repositorio ni en el paquete Python. La versión y la integridad de cada archivo descargado se fijan en `src/osu_coach/calculator/package-lock.json`. Los avisos originales de las dependencias permanecen en la instalación local.

También se utiliza [rosu-pp-py 4.0.2](https://github.com/MaxOhn/rosu-pp-py), bajo licencia MIT, para leer mapas y atributos de tiempo. tosu y osu! se instalan por separado y conservan sus licencias respectivas.
