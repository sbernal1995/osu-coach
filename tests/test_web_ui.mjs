import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
// Import this browser module without adding a Node build dependency to the app.
const source = await readFile(
  new URL("../src/osu_coach/web/profile-radar.js", import.meta.url),
  "utf8",
);
const { buildRadarModel } = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);
const state = (key, value, extra = {}) => ({
  player_profile: {
    dimensions: [
      {
        key,
        value,
        samples: 10,
        distinct_maps: 6,
        sessions: 2,
        status: "steady",
        confidence: "medium",
        ...extra,
      },
    ],
  },
});
const axis = (s, key) => buildRadarModel(s).find((item) => item.key === key);

test("missing values remain missing, including empty and invalid telemetry", () => {
  for (const value of [undefined, null, "", true, NaN, Infinity, -1])
    assert.equal(axis(state("accuracy", value), "accuracy").score, null);
  assert.ok(buildRadarModel({}).every((item) => item.score === null));
});
test("no observations cannot produce a polygon point", () => {
  assert.equal(
    axis(state("accuracy", 99, { samples: 0 }), "accuracy").score,
    null,
  );
});
test("real zero values are represented, with direction appropriate to the metric", () => {
  assert.equal(axis(state("accuracy", 0), "accuracy").score, 0);
  assert.equal(axis(state("misses", 0), "misses").score, 100);
  assert.equal(axis(state("consistency", 0), "consistency").score, 100);
});
test("less misses and less dispersion move outward", () => {
  assert.ok(
    axis(state("misses", 0.8), "misses").score >
      axis(state("misses", 2), "misses").score,
  );
  assert.equal(axis(state("consistency", 2), "consistency").score, 50);
});
test("configured thresholds change the displayed reference", () => {
  const s = state("combo", 80);
  s.settings = { values: { strong_combo_percent: 100 } };
  assert.equal(axis(s, "combo").score, 80);
  s.settings.values.strong_combo_percent = 80;
  assert.equal(axis(s, "combo").score, 100);
});
test("a zero-miss reference is achieved only with zero misses", () => {
  const s = state("misses", 0.1);
  s.settings = { values: { strong_miss_percent: 0 } };
  assert.equal(axis(s, "misses").score, 0);
  s.player_profile.dimensions[0].value = 0;
  assert.equal(axis(s, "misses").score, 100);
});
test("values above the reference are capped but the real value is retained", () => {
  const a = axis(state("combo", 95), "combo");
  assert.equal(a.score, 100);
  assert.equal(a.raw, "95 %");
});
test("provisional results stay identifiable without becoming zero", () => {
  const a = axis(
    state("accuracy", 95, { confidence: "low", status: "learning" }),
    "accuracy",
  );
  assert.equal(a.provisional, true);
  assert.ok(a.score > 0);
  assert.equal(a.status, "learning");
});
test("tag axes keep their order and do not invent missing skills", () => {
  const items = [
    {
      tag: "skillset/jumps",
      accuracy: 98,
      stats_scope: "comparable",
      comparable_plays: 10,
      confidence: "medium",
      status: "strength",
    },
  ];
  const a = buildRadarModel({ tag_analysis: { items } }, "tags");
  assert.equal(a.length, 7);
  assert.equal(a[0].key, "skillset/jumps");
  assert.equal(a[0].score, 98);
  assert.ok(a.slice(1).every((x) => x.score === null));
  assert.deepEqual(
    a.map((x) => x.key),
    buildRadarModel({}, "tags").map((x) => x.key),
  );
});
test("tag results outside the comparable band cannot masquerade as a skill", () => {
  const items = [
    {
      tag: "skillset/jumps",
      accuracy: 99,
      stats_scope: "all",
      comparable_plays: 0,
    },
  ];
  assert.equal(
    buildRadarModel({ tag_analysis: { items } }, "tags")[0].score,
    null,
  );
});
test("visualization never mutates the profile or settings", () => {
  const s = state("accuracy", 98);
  const before = JSON.stringify(s);
  buildRadarModel(s);
  assert.equal(JSON.stringify(s), before);
});
