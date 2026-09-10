// Local, line-delimited JSON worker. Map contents never leave this process.
const readline = require("node:readline");
const path = require("node:path");
const engine = require(
  path.join(process.argv[2], "node_modules/@tosuapp/lazer-calculator-prebuilt"),
);
const version = require(
  path.join(
    process.argv[2],
    "node_modules/@tosuapp/lazer-calculator-prebuilt/package.json",
  ),
).version;
console.log(JSON.stringify({ ready: version }));
let count = 0;
readline
  .createInterface({ input: process.stdin, crlfDelay: Infinity })
  .on("line", (line) => {
    try {
      const request = JSON.parse(line);
      const map = engine.PlayBeatmap.parse(
        Buffer.from(request.content, "base64").toString("utf8"),
      );
      if (map.mode !== 0) throw new Error("El mapa no es de osu!standard.");
      map.applyMods(
        request.mods.map((mod) => ({
          acronym: mod.acronym,
          settings: new Map(Object.entries(mod.settings || {})),
        })),
      );
      const gradual = map.createGradualDifficulty();
      gradual.skipToEnd();
      const attrs = gradual.createDifficultyAttrs().getData();
      const diff = map.getBeatmapDifficulty();
      const result = {
        stars: attrs.stars,
        aim: attrs.aim,
        speed: attrs.speed,
        reading: attrs.reading,
        max_combo: attrs.maxCombo,
        object_count: attrs.nCircles + attrs.nSliders + attrs.nSpinners,
        ar: diff.approachRate,
        od: diff.overallDifficulty,
        cs: diff.circleSize,
      };
      if (Object.values(result).some((value) => !Number.isFinite(value)))
        throw new Error("Atributos inválidos.");
      console.log(JSON.stringify({ result }));
    } catch (error) {
      console.log(
        JSON.stringify({
          error:
            "No se pudo calcular el mapa: " +
            String(error.message).slice(0, 300),
        }),
      );
    }
    // Let the native binding release objects between requests in large libraries.
    if (++count % 32 === 0 && global.gc) global.gc();
  });
