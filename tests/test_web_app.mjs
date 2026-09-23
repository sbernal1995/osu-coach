// Loads src/osu_coach/web/app.js in a minimal DOM/fetch environment and drives
// a full refresh(), so a bug in the page script (load-time listeners, render
// paths such as the favorites toggle/panel) fails the suite instead of leaving
// the user staring at "El coach está desconectado".
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const appUrl = new URL("../src/osu_coach/web/app.js", import.meta.url);
const radarUrl = new URL("../src/osu_coach/web/profile-radar.js", import.meta.url);
const indexUrl = new URL("../src/osu_coach/web/index.html", import.meta.url);
const appSource = await readFile(appUrl, "utf8");
const radarSource = await readFile(radarUrl, "utf8");
const indexSource = await readFile(indexUrl, "utf8");

const staticIds = new Set(
  [...indexSource.matchAll(/id="([^"]+)"/g)].map((match) => match[1]),
);
const jsReferencedIds = new Set();
for (const match of appSource.matchAll(
  /\$\s*\(\s*["']([^"']+)["']\s*\)|text\(\s*["']([^"']+)["']\s*/g,
))
  jsReferencedIds.add(match[1] || match[2]);
const knownIds = new Set([...staticIds, ...jsReferencedIds]);
const favoritesIds = [
  "favorites-toggle-button",
  "favorites-panel",
  "favorites-count",
  "favorites-list",
  "favorites-import",
  "favorites-import-text",
  "favorites-import-button",
  "favorites-import-result",
];

async function loadRealState(path) {
  const text = await readFile(path, "utf8");
  const parsed = JSON.parse(text.replace(/^\uFEFF/, ""));
  return parsed.state ?? parsed;
}

const realState = process.env.OSU_COACH_STATE_FILE
  ? await loadRealState(process.env.OSU_COACH_STATE_FILE)
  : null;

const VOID_ELEMENTS = new Set([
  "area",
  "base",
  "br",
  "col",
  "embed",
  "hr",
  "img",
  "input",
  "link",
  "meta",
  "param",
  "source",
  "track",
  "wbr",
]);
const IMPLICIT_CLOSE = new Set([
  "p",
  "li",
  "dt",
  "dd",
  "tr",
  "td",
  "th",
  "option",
  "thead",
  "tbody",
  "tfoot",
]);

function parseStaticNodes(html) {
  const clean = html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<script(\s[^<>]*)?>[\s\S]*?<\/script\s*>/gi, "")
    .replace(/<style(\s[^<>]*)?>[\s\S]*?<\/style\s*>/gi, "");
  const nodes = [];
  const stack = [];
  const tagPattern = /<\/?([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)\/?>/g;
  let match;
  while ((match = tagPattern.exec(clean))) {
    const [full, rawTag, rawAttrs] = match;
    const tag = rawTag.toLowerCase();
    if (full.startsWith("</")) {
      const index = stack.map((node) => node.tag).lastIndexOf(tag);
      if (index >= 0) stack.length = index;
      continue;
    }
    if (IMPLICIT_CLOSE.has(tag)) {
      const index = stack.map((node) => node.tag).lastIndexOf(tag);
      if (index >= 0) stack.length = index;
    }
    const attrs = rawAttrs || "";
    const id = (attrs.match(/\sid="([^"]*)"/) || [])[1] || null;
    const cls = (attrs.match(/\sclass="([^"]*)"/) || [])[1] || "";
    const node = {
      tag,
      id,
      class: cls,
      parent: stack.length ? stack[stack.length - 1] : null,
    };
    nodes.push(node);
    if (!full.endsWith("/>") && !VOID_ELEMENTS.has(tag)) stack.push(node);
  }
  return nodes;
}

function matchesSimpleSelector(el, part) {
  if (!(el instanceof El)) return false;
  if (part.startsWith("#")) return el._id === part.slice(1);
  if (part.startsWith(".")) return el.classList.contains(part.slice(1));
  const dot = part.indexOf(".");
  if (dot >= 0)
    return (
      el.tagName.toLowerCase() === part.slice(0, dot).toLowerCase() &&
      el.classList.contains(part.slice(dot + 1))
    );
  return el.tagName.toLowerCase() === part.toLowerCase();
}

function isSimpleSelector(selector) {
  const groups = String(selector).split(",");
  if (groups.length !== 1) return false;
  const text = groups[0].trim();
  if (!text || /[[>:+~*]/.test(text)) return false;
  return text
    .split(/\s+/)
    .every(
      (part) =>
        /^[a-zA-Z][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9_-]+)?$/.test(part) ||
        /^[#.][a-zA-Z0-9_-]+$/.test(part),
    );
}

function querySelectorAllIn(root, selector) {
  const results = [];
  const seen = new Set();
  const parts = String(selector).trim().split(/\s+/);
  function collect(node, partIndex) {
    for (const child of node.children || []) {
      if (
        child instanceof El &&
        matchesSimpleSelector(child, parts[partIndex])
      ) {
        if (partIndex === parts.length - 1) {
          if (!seen.has(child)) {
            seen.add(child);
            results.push(child);
          }
        } else collect(child, partIndex + 1);
      }
      collect(child, partIndex);
    }
  }
  collect(root, 0);
  return results;
}

class El {
  constructor(tag, id = null) {
    this.tagName = String(tag || "div").toUpperCase();
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle: (name, force) => {
        const state = force === undefined ? !classes.has(name) : Boolean(force);
        if (state) classes.add(name);
        else classes.delete(name);
        return state;
      },
      contains: (name) => classes.has(name),
      [Symbol.iterator]: () => classes.values(),
    };
    this.children = [];
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.tabIndex = -1;
    this.isConnected = false;
    this.value = "";
    this.type = "";
    this.options = [];
    this.checked = false;
    this.selectedIndex = -1;
    this.lastChild = null;
    this.parentNode = null;
    this.namespaceURI = "http://www.w3.org/2000/svg";
    this._id = null;
    if (id) this._id = id;
  }
  get id() {
    return this._id;
  }
  set id(value) {
    const doc = globalThis.document;
    if (
      this._id != null &&
      doc &&
      typeof doc._unregisterId === "function" &&
      doc.byId?.get(this._id) === this
    )
      doc._unregisterId(this._id);
    this._id = value ?? null;
    if (this._id != null && doc && typeof doc._registerId === "function")
      doc._registerId(this._id, this);
  }
  set className(value) {
    this.classList.remove(...this.classList);
    this.classList.add(...String(value ?? "").split(/\s+/).filter(Boolean));
  }
  get className() {
    return [...this.classList].join(" ");
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === "class") this.className = value;
  }
  getAttribute(key) {
    return key in this.attributes ? this.attributes[key] : null;
  }
  removeAttribute(key) {
    delete this.attributes[key];
  }
  _push(node) {
    if (node == null) return;
    if (typeof node === "string" || typeof node === "number") {
      this.appendChild({
        textContent: String(node),
        children: [],
        isConnected: false,
      });
      return;
    }
    node.parentNode = this;
    node.isConnected = true;
    this.children.push(node);
    this.lastChild = node;
    globalThis.document?._registerNode?.(node);
  }
  append(...nodes) {
    for (const node of nodes) this._push(node);
  }
  appendChild(node) {
    this._push(node);
    return node;
  }
  prepend(...nodes) {
    for (const node of nodes) this._push(node);
  }
  replaceChildren(...nodes) {
    const doc = globalThis.document;
    for (const child of this.children) {
      child.parentNode = null;
      if (doc && typeof doc._unregisterNode === "function")
        doc._unregisterNode(child);
    }
    this.children = [];
    for (const node of nodes) this._push(node);
    if (nodes.length) this.lastChild = this.children.at(-1);
    else this.lastChild = null;
  }
  insertBefore(node, ref) {
    this._push(node);
    return node;
  }
  before(node) {
    node.parentNode = this.parentNode;
    return node;
  }
  querySelector(selector) {
    if (!isSimpleSelector(selector)) return new El("div");
    const found = querySelectorAllIn(this, selector);
    return found.length ? found[0] : null;
  }
  querySelectorAll(selector) {
    if (!isSimpleSelector(selector)) return [];
    return querySelectorAllIn(this, selector);
  }
  addEventListener() {}
  removeEventListener() {}
  focus() {}
  click() {
    if (this.listeners?.click) this.listeners.click({ preventDefault() {} });
  }
  showModal() {}
  close() {}
  scrollIntoView() {}
  matches() {
    return true;
  }
  contains(node) {
    return this.children.includes(node);
  }
  cloneNode() {
    return new El(this.tagName);
  }
  get parentElement() {
    return this.parentNode;
  }
}

function makeDocument(ids) {
  const byId = new Map();
  const allowed = new Set(ids);
  const elements = new Map();
  let root = null;
  let body = null;
  for (const node of parseStaticNodes(indexSource)) {
    if (node.id && !allowed.has(node.id)) continue;
    const el = new El(node.tag);
    if (node.class) el.className = node.class;
    if (node.id) el._id = node.id;
    elements.set(node, el);
    if (node.id && !byId.has(node.id)) byId.set(node.id, el);
    if (node.tag === "html" && !root) root = el;
    if (node.tag === "body" && !body) body = el;
    let ancestor = node.parent;
    while (ancestor && !elements.has(ancestor)) ancestor = ancestor.parent;
    const parentEl = ancestor ? elements.get(ancestor) : null;
    if (parentEl) {
      el.parentNode = parentEl;
      el.isConnected = true;
      parentEl.children.push(el);
      parentEl.lastChild = el;
    } else if (!root) root = el;
  }
  if (!root) root = new El("html");
  if (!body) body = new El("body");
  for (const id of ids) {
    if (byId.has(id)) continue;
    const el = new El("div");
    el._id = id;
    byId.set(id, el);
  }
    function registerSubtree(node) {
    if (!node || typeof node !== "object") return;
    if (typeof node._id === "string" && node._id) byId.set(node._id, node);
    for (const child of node.children || []) registerSubtree(child);
  }
  function unregisterSubtree(node) {
    if (!node || typeof node !== "object") return;
    if (
      typeof node._id === "string" &&
      node._id &&
      byId.get(node._id) === node
    )
      byId.delete(node._id);
    for (const child of node.children || []) unregisterSubtree(child);
  }
  return {
    byId,
    _registerNode: registerSubtree,
    _unregisterNode: unregisterSubtree,
    _registerId(id, node) {
      byId.set(id, node);
    },
    _unregisterId(id) {
      byId.delete(id);
    },
    getElementById(id) {
      return byId.get(id) ?? null;
    },
    createElement(tag) {
      return new El(tag);
    },
    createElementNS(ns, tag) {
      const el = new El(tag);
      el.namespaceURI = ns || "http://www.w3.org/2000/svg";
      return el;
    },
    createTextNode(text) {
      const el = new El("#text");
      el.textContent = String(text);
      return el;
    },
    querySelector(selector) {
      if (!isSimpleSelector(selector)) return new El("div");
      const found = querySelectorAllIn(root, selector);
      return found.length ? found[0] : null;
    },
    querySelectorAll(selector) {
      if (!isSimpleSelector(selector)) return [];
      return querySelectorAllIn(root, selector);
    },
    addEventListener() {},
    removeEventListener() {},
    body,
    documentElement: root,
  };
}

function stateFixture(favoritesEnabled) {
  if (realState) {
    return {
      ...realState,
      settings: {
        ...(realState.settings || {}),
        values: {
          ...(realState.settings?.values || {}),
          favorites_enabled: favoritesEnabled,
        },
      },
      favorites: {
        items: realState.favorites?.items || [],
        total: realState.favorites?.total ?? 0,
        enabled: favoritesEnabled,
      },
    };
  }
  return {
    app: "osu-coach",
    token: "test-token",
    demo: false,
    settings: {
      values: { favorites_enabled: favoritesEnabled },
      schema: [
        {
          key: "favorites_enabled",
          label: "Modo favoritas",
          type: "boolean",
          group: "Evolución y repeticiones",
          description: "Muestra solo canciones favoritas.",
          default: false,
        },
        {
          key: "initial_stars",
          label: "Referencia inicial (estrellas)",
          type: "number",
          group: "Memoria y calibración",
          description: "Punto de partida provisional.",
          default: 2.5,
          min: 0.5,
          max: 10.5,
          step: 0.05,
        },
      ],
    },
    connection: { ok: false, message: "Esperando conexión con tosu…" },
    scanning: false,
    scan_count: 0,
    catalog_count: 1,
    mode_label: "osu!standard",
    profile_label: "Perfil nuevo · osu!standard · Sin mods",
    profile: {
      phase: "training",
      baseline: 3.2,
      evaluated_at: "2026-01-01T00:00:00+00:00",
      distinct_maps: 8,
      attempts: 40,
      focus: "Encontrar tu ritmo",
      message: "Jugá distintos mapas para orientar la práctica.",
      trend: "steady",
      session: { observations: 2, trend: "steady" },
      training_level: { stars: 3.4, cycle: "cycle-1" },
      ranges: [],
    },
    player_profile: {
      dimensions: [],
      notes: [],
      status: "Por calibrar",
    },
    recommendations: [
      {
        stage: "practice",
        label: "Práctica principal",
        target: "Ampliar control",
        min_stars: 3.0,
        max_stars: 3.8,
        maps: [
          {
            key: "beatmap-1",
            beatmap_id: 1,
            title: "Canción de test",
            artist: "Artista",
            version: "Insane",
            stars: 3.4,
            bpm: 180,
            duration: 150,
            calculator: "lazer",
            training_role: favoritesEnabled ? "favorite" : "practice",
            training_progress: {
              eligible: true,
              cycle: "cycle-1",
              stars: 3.4,
            },
            play_conditions: { mods: [], rate: 1.0 },
            expectation: {
              basis: "batí tu marca hasta el FC",
            },
          },
        ],
      },
    ],
    recent: [],
    pity: 0,
    quest_board: {
      id: "board-1",
      automatic_refresh: false,
      groups: [
        {
          stage: "practice",
          label: "Práctica principal",
          target: "Ampliar control",
          min_stars: 3.0,
          max_stars: 3.8,
          quests: [
            {
              id: "quest-1",
              status: "pending",
              map: {
                key: "beatmap-1",
                title: "Canción de test",
                artist: "Artista",
                version: "Insane",
                stars: 3.4,
                bpm: 180,
                duration: 150,
                calculator: "lazer",
                ...(favoritesEnabled ? { favorite: { reference: null } } : {}),
                play_conditions: { mods: [], rate: 1.0 },
                expectation: {
                  basis: favoritesEnabled
                    ? "batí tu marca hasta el FC"
                    : "práctica específica",
                },
              },
            },
          ],
        },
      ],
    },
    quest_history: [],
    quest_completions: { total: 0, items: [] },
    quest_skips: { total: 0, items: [] },
    song_bans: { items: [], total: 0 },
    favorites: favoritesEnabled
      ? {
          total: 1,
          items: [{ id: "fav-1", title: "Canción de test", artist: "Artista" }],
        }
      : { total: 0, items: [] },
    feel: { offsets: {}, rated: 0 },
    quest_availability: {
      "quest-1": {
        installed: true,
        mods_label: "Sin mods",
        favorite: favoritesEnabled,
        favorite_id: favoritesEnabled ? "fav-1" : null,
        search: {},
        popularity: null,
        difficulty: { stars: 3.4, calculator: "lazer" },
        difficulty_pending: false,
      },
    },
    recommendation_policy: {
      mode: favoritesEnabled ? "favorites" : "unplayed",
      unit: "difficulty",
      favorites_enabled: favoritesEnabled,
      history_plays: 0,
    },
    coach_progress: { sessions: [], completions: [] },
    tag_analysis: { items: [], scope: "all" },
    tag_sync: { status: "idle" },
    discovery: { state: "ready", automatic: true, needs: [], search_needs: [] },
    pending: [],
    warnings: [],
  };
}

function nodeText(node) {
  return [
    node.textContent || "",
    ...node.children.reduce(
      (all, child) => all.concat(nodeText(child)),
      [],
    ),
  ].join("");
}

function stateFixtureEmptyFavorites() {
  const state = JSON.parse(JSON.stringify(stateFixture(true)));
  for (const group of state.quest_board?.groups || []) group.quests = [];
  state.recommendations = [];
  state.settings = state.settings || {};
  state.settings.values = { ...(state.settings.values || {}), favorites_enabled: true };
  state.recommendation_policy = {
    ...(state.recommendation_policy || {}),
    mode: "favorites",
    favorites_enabled: true,
  };
  return state;
}

function searchNotices(doc) {
  const found = [];
  function walk(node) {
    if (!node || typeof node !== "object") return;
    if (node.dataset && node.dataset.searchState !== undefined)
      found.push(node);
    for (const child of node.children || []) walk(child);
  }
  walk(doc.documentElement);
  return found;
}

function favoritesEmptyMessage(doc) {
  let found = false;
  function walk(node) {
    if (!node || typeof node !== "object" || found) return;
    if (
      node.classList?.contains?.("empty-stage") &&
      nodeText(node).includes("no tiene favoritas adecuadas")
    )
      found = true;
    for (const child of node.children || []) walk(child);
  }
  walk(doc.documentElement);
  return found;
}

function assertMissions(doc, message) {
  const root = doc.getElementById("recommendations");
  assert.equal(
    root.getAttribute("aria-busy"),
    "false",
    message + ": el área de misiones sale del estado de carga",
  );
  const stage = root.children.find((child) =>
    child.classList.contains("stage"),
  );
  assert.ok(stage, message + ": hay un grupo de misiones");
  const card = stage.children.find((child) =>
    child.classList.contains("map-card"),
  );
  assert.ok(card, message + ": el grupo muestra tarjetas de misión");
  assert.ok(
    nodeText(card).includes("★"),
    message + ": las tarjetas muestran sus estrellas",
  );
}

async function runScenario(ids, favoritesExpected) {
  const saved = {};
  for (const key of [
    "window",
    "document",
    "navigator",
    "location",
    "history",
    "fetch",
    "setTimeout",
    "clearTimeout",
  ]) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, key);
    saved[key] = descriptor
      ? descriptor
      : { value: globalThis[key], writable: true, configurable: true };
    delete globalThis[key];
  }
  const errors = [];
  const savedConsoleError = console.error;
  try {
    let pendingState = stateFixture(false);
    const doc = makeDocument(ids);
    const windowStub = {
      isSecureContext: false,
      matchMedia: () => ({ matches: false, addEventListener() {} }),
      addEventListener() {},
      removeEventListener() {},
      confirm: () => true,
      location: { hash: "" },
      history: { pushState() {}, replaceState() {} },
      scrollTo() {},
    };
    globalThis.window = windowStub;
    globalThis.location = windowStub.location;
    globalThis.history = windowStub.history;
    globalThis.document = doc;
    globalThis.navigator = { clipboard: { writeText: async () => {} } };
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => JSON.parse(JSON.stringify(pendingState)),
    });
    const timers = [];
    globalThis.setTimeout = (fn) => {
      timers.push(fn);
      return timers.length;
    };
    globalThis.clearTimeout = () => {};

    console.error = (...args) =>
      errors.push(
        args
          .map((value) =>
            value instanceof Error ? value.stack : String(value),
          )
          .join("\n"),
      );

    const radarImport = `const { buildRadarModel, renderRadar, renderRadarValues } = await import(${JSON.stringify(
      "data:text/javascript;base64," +
        Buffer.from(radarSource).toString("base64"),
    )});\n`;
    const nonce = "/*" + Math.random().toString(36).slice(2) + "*/";
    const source =
      nonce +
      radarImport +
      appSource.replace(/^import[\s\S]*?from "\.\/profile-radar\.js";\n?/, "");
    await import(
      "data:text/javascript;base64," + Buffer.from(source).toString("base64")
    );
    for (let i = 0; i < 6; i++) await new Promise(setImmediate);

    const connection = doc.getElementById("connection");
    assert.deepEqual(
      errors,
      [],
      "el panel no debe fallar al renderizar el estado inicial",
    );
    assert.equal(connection.dataset.offline, "false");
    assert.equal(doc.getElementById("initial-error").hidden, true);
    assert.equal(
      doc.getElementById("connection-text").textContent,
      pendingState.connection?.message || "Script conectado",
    );
    if (favoritesExpected) {
      assert.equal(
        doc.getElementById("favorites-toggle-button").textContent,
        "☆ Modo favoritas desactivado",
      );
      const list = doc.getElementById("favorites-list");
      assert.equal(list.children.length, 1);
      assert.ok(
        list.children[0].className.includes("favorites-empty"),
        "sin favoritas se muestra el aviso del panel",
      );
      assert.ok(doc.getElementById("favorites-import-text"));
      assert.ok(doc.getElementById("favorites-import-button"));
      assert.ok(doc.getElementById("favorites-import-result"));
    } else {
      assert.equal(doc.getElementById("favorites-toggle-button"), null);
      assert.equal(doc.getElementById("favorites-list"), null);
      assert.equal(doc.getElementById("favorites-panel"), null);
      assert.equal(doc.getElementById("favorites-import"), null);
      assert.equal(doc.getElementById("favorites-import-text"), null);
      assert.equal(doc.getElementById("favorites-import-button"), null);
      assert.equal(doc.getElementById("favorites-import-result"), null);
    }
    assertMissions(doc, "estado inicial");

    pendingState = stateFixture(true);
    timers.splice(0).forEach((fn) => fn());
    for (let i = 0; i < 6; i++) await new Promise(setImmediate);

    assert.deepEqual(
      errors,
      [],
      "el panel no debe fallar al renderizar el estado con favoritas activas",
    );
    assert.equal(connection.dataset.offline, "false");
    if (favoritesExpected) {
      const toggle = doc.getElementById("favorites-toggle-button");
      assert.equal(toggle.textContent, "★ Modo favoritas activado");
      assert.ok([...toggle.classList].includes("favorites-on"));
      assert.equal(
        doc.getElementById("favorites-list").children.length,
        1,
      );
    }
    assertMissions(doc, "estado con favoritas activas");

    pendingState = stateFixtureEmptyFavorites();
    timers.splice(0).forEach((fn) => fn());
    for (let i = 0; i < 6; i++) await new Promise(setImmediate);

    assert.deepEqual(
      errors,
      [],
      "el panel no debe fallar al renderizar el tablero vacío de favoritas",
    );
    assert.equal(connection.dataset.offline, "false");
    assert.deepEqual(
      searchNotices(doc).map((node) => nodeText(node)),
      [],
      "en modo favoritas no hay avisos de búsqueda automática",
    );
    assert.ok(
      favoritesEmptyMessage(doc),
      "el tablero vacío de favoritas explica cómo marcar canciones",
    );
  } finally {
    console.error = savedConsoleError;
    for (const [key, descriptor] of Object.entries(saved)) {
      delete globalThis[key];
      Object.defineProperty(globalThis, key, descriptor);
    }
  }
}

test("pagina carga, refresh queda online y las misiones se renderizan con y sin elementos de favoritas", async () => {
  await runScenario([...knownIds], true);
  await runScenario(
    [...knownIds].filter((id) => !favoritesIds.includes(id)),
    false,
  );
});