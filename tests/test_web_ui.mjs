import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
// Import this browser module without adding a Node build dependency to the app.
const source = await readFile(
  new URL("../src/osu_coach/web/profile-radar.js", import.meta.url),
  "utf8",
);
const { buildRadarModel, renderRadar } = await import(
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
  assert.equal(axis(state("consistency", 2), "consistency").score, 60);
});
test("configured thresholds move only the reference, not the observed result", () => {
  for (const [key, value, setting, first, second] of [
    ["combo", 92, "strong_combo_percent", 100, 80],
    ["accuracy", 98, "strong_accuracy", 99, 97],
    ["misses", 0.2, "strong_miss_percent", 0, 0.5],
  ]) {
    const s = state(key, value);
    s.settings = { values: { [setting]: first } };
    const before = axis(s, key);
    s.settings.values[setting] = second;
    const after = axis(s, key);
    assert.equal(after.score, before.score);
    assert.notEqual(after.referenceScore, before.referenceScore);
    assert.equal(after.raw, before.raw);
  }
});
test("a zero-miss reference is achieved only with zero misses", () => {
  const s = state("misses", 0.1);
  s.settings = { values: { strong_miss_percent: 0 } };
  assert.equal(axis(s, "misses").score, 95);
  assert.equal(axis(s, "misses").referenceScore, 100);
  s.player_profile.dimensions[0].value = 0;
  assert.equal(axis(s, "misses").score, 100);
});
test("exceeding a solid-result reference leaves room for further improvement", () => {
  const a = axis(state("combo", 95), "combo");
  assert.equal(a.score, 90);
  assert.equal(a.referenceScore, 60);
  assert.equal(a.raw, "95 %");
  assert.ok(a.score > a.referenceScore);
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
  assert.equal(a[0].score, 80);
  assert.equal(a[0].raw, "98 %");
  assert.equal(a[0].referenceScore, 70);
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

test("the reported player profile no longer saturates four axes", () => {
  const values = {
    accuracy: 97.45,
    misses: 0.26,
    combo: 92.14,
    completion: 100,
    consistency: 3.43,
  };
  const expected = {
    accuracy: 74.5,
    misses: 87,
    combo: 84.28,
    completion: 100,
    consistency: 31.4,
  };
  for (const [key, value] of Object.entries(values)) {
    const a = axis(state(key, value), key);
    assert.ok(Math.abs(a.score - expected[key]) < 1e-8, key);
    assert.equal(a.score === 100, key === "completion");
    assert.ok(a.scale.includes("borde"));
    assert.ok(a.reading);
  }
});

test("only a perfect measurement reaches the perimeter", () => {
  for (const [key, almost, perfect] of [
    ["accuracy", 99.99, 100],
    ["misses", 0.01, 0],
    ["combo", 99.99, 100],
    ["completion", 99.99, 100],
    ["consistency", 0.01, 0],
  ]) {
    assert.ok(axis(state(key, almost), key).score < 100, key);
    assert.equal(axis(state(key, perfect), key).score, 100, key);
  }
});

test("zoom bounds preserve low raw results and distinguish them from missing data", () => {
  for (const [key, value] of [
    ["accuracy", 85],
    ["misses", 3],
    ["combo", 40],
    ["consistency", 6],
  ]) {
    const a = axis(state(key, value), key);
    assert.equal(a.score, 0, key);
    assert.ok(a.raw.startsWith(String(value)), key);
    assert.notEqual(a.reading, "Sin datos");
  }
  assert.equal(axis(state("accuracy", 101), "accuracy").score, null);
});

test("renderer uses per-axis references in both views and does not fill missing observations", () => {
  const node = () => ({
    attributes: {},
    children: [],
    setAttribute(key, value) {
      this.attributes[key] = value;
    },
    append(...children) {
      this.children.push(...children);
    },
    replaceChildren(...children) {
      this.children = children;
    },
  });
  const originalDocument = globalThis.document;
  globalThis.document = { createElementNS: node, createElement: node };
  try {
    const axes = buildRadarModel(state("accuracy", 97.45));
    for (const compact of [true, false]) {
      const host = node();
      renderRadar(host, axes, { id: "test-radar", compact });
      const svg = host.children[0];
      const reference = svg.children.find(
        (n) => n.attributes.class === "radar-reference",
      );
      const perimeter = svg.children
        .filter((n) => n.attributes.class === "radar-grid")
        .at(-1);
      assert.ok(reference);
      assert.notEqual(reference.attributes.points, perimeter.attributes.points);
      const firstPoint = reference.attributes.points
        .split(" ")[0]
        .split(",")
        .map(Number);
      assert.ok(
        Math.abs((155 - firstPoint[1]) / 88 - axes[0].referenceScore / 100) <
          1e-8,
      );
      assert.equal(
        svg.children.filter((n) => n.attributes.class === "radar-point").length,
        1,
      );
      assert.ok(!svg.children.some((n) => n.attributes.class === "radar-fill"));
      const labels = svg.children.filter(
        (n) => n.attributes.class === "radar-label",
      );
      assert.equal(labels[0].children.at(-1).textContent, "97,45 %");
      assert.equal(labels[1].children.at(-1).textContent, "Sin datos");
    }
  } finally {
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  }
});
