/** Presentation-only transforms. No score here feeds recommendations or progression. */
const LABELS = {
  accuracy: "Precisión",
  misses: "Control de misses",
  combo: "Combo",
  completion: "Completar mapas",
  consistency: "Consistencia",
};
const TAG_AXES = [
  ["skillset/jumps", "Saltos"],
  ["skillset/streams", "Streams"],
  ["skillset/alt", "Alternancia"],
  ["skillset/tech", "Técnica"],
  ["skillset/precision", "Apuntado"],
  ["skillset/reading", "Lectura"],
  ["skillset/gimmick", "Mecánicas"],
];
const number = (value) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
const clamp = (value) => Math.min(100, Math.max(0, value));
const format = (value) =>
  new Intl.NumberFormat("es-AR", { maximumFractionDigits: 2 }).format(value);
const setting = (state, key, fallback) =>
  number(state?.settings?.values?.[key]) ?? fallback;
const statusText = (status) =>
  ({
    strength: "Fortaleza",
    practice: "A practicar",
    steady: "Estable",
    learning: "Evidencia inicial",
    explore: "Sin datos",
  })[status] || "Evidencia inicial";

export function buildRadarModel(state, mode = "control") {
  if (mode === "tags") {
    const items = new Map(
      (state?.tag_analysis?.items || []).map((item) => [item.tag, item]),
    );
    return TAG_AXES.map(([key, label]) => {
      const item = items.get(key) || {};
      const value = number(item.accuracy);
      const valid =
        item.stats_scope === "comparable" &&
        (number(item.comparable_plays) ?? 0) > 0 &&
        value !== null &&
        value >= 0 &&
        value <= 100;
      return {
        key,
        label,
        score: valid ? value : null,
        raw: valid ? format(value) + " %" : "Sin datos",
        status: valid ? item.status || "learning" : "explore",
        provisional: item.confidence !== "medium",
        samples: item.comparable_plays || 0,
        maps: item.comparable_maps || 0,
        sessions: item.comparable_sessions || 0,
        target: "Precisión en mapas con este tag",
        evidence:
          item.message || "Todavía faltan partidas comparables con este tag.",
      };
    });
  }
  const dimensions = new Map(
    (state?.player_profile?.dimensions || []).map((item) => [item.key, item]),
  );
  const targets = {
    accuracy: setting(state, "strong_accuracy", 97),
    misses: setting(state, "strong_miss_percent", 0.5),
    combo: setting(state, "strong_combo_percent", 80),
    completion: 100,
    consistency: 1,
  };
  return Object.entries(LABELS).map(([key, label]) => {
    const item = dimensions.get(key) || {};
    const value = number(item.value),
      target = targets[key];
    const valid =
      value !== null &&
      value >= 0 &&
      (key === "consistency" || value <= 100) &&
      (number(item.samples) ?? 0) > 0;
    const inverse = key === "misses" || key === "consistency";
    let score = null;
    if (valid)
      score = clamp(
        inverse
          ? value === 0
            ? 100
            : (100 * target) / value
          : target <= 0
            ? 100
            : (100 * value) / target,
      );
    const unit =
      key === "consistency"
        ? " pp de dispersión"
        : key === "misses"
          ? " % de misses"
          : " %";
    return {
      key,
      label,
      score,
      raw: valid ? format(value) + unit : "Sin datos",
      status: item.status || "learning",
      provisional: item.confidence !== "medium",
      samples: item.samples || 0,
      maps: item.distinct_maps || 0,
      sessions: item.sessions || 0,
      target: "Referencia: " + (inverse ? "≤ " : "≥ ") + format(target) + unit,
      evidence: item.evidence || "Todavía faltan mediciones.",
    };
  });
}

function svgElement(tag, attributes = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([key, value]) =>
    node.setAttribute(key, String(value)),
  );
  if (text !== undefined) node.textContent = text;
  return node;
}
function htmlElement(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function renderRadar(
  host,
  axes,
  { id = host.id, mode = "control", compact = false } = {},
) {
  const svg = svgElement("svg", {
    viewBox: compact ? "0 0 360 300" : "0 0 360 320",
    role: "img",
    "aria-labelledby": id + "-title " + id + "-description",
  });
  svg.append(
    svgElement(
      "title",
      { id: id + "-title" },
      mode === "tags"
        ? "Rendimiento por tipo de mapa"
        : "Habilidades: control respecto de tus referencias",
    ),
  );
  const description = axes
    .map(
      (axis) =>
        `${axis.label}: ${axis.raw}; ${axis.score === null ? "sin punto" : statusText(axis.status)}.`,
    )
    .join(" ");
  svg.append(
    svgElement(
      "desc",
      { id: id + "-description" },
      description + " Puntos huecos: evidencia inicial. El borde es 100.",
    ),
  );
  const cx = 180,
    cy = 155,
    radius = 100;
  const xy = (index, scale = 1) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / axes.length;
    return [
      cx + Math.cos(angle) * radius * scale,
      cy + Math.sin(angle) * radius * scale,
    ];
  };
  const points = (scale) =>
    axes.map((axis, index) => xy(index, scale).join(",")).join(" ");
  if (compact)
    svg.append(
      svgElement("polygon", { points: points(1), class: "radar-reference" }),
    );
  for (const step of [25, 50, 75, 100])
    svg.append(
      svgElement("polygon", {
        points: points(step / 100),
        class: "radar-grid",
      }),
    );
  axes.forEach((axis, index) => {
    const [x, y] = xy(index);
    svg.append(
      svgElement("line", { x1: cx, y1: cy, x2: x, y2: y, class: "radar-axis" }),
    );
    const [lx, ly] = xy(index, 1.22);
    const label = svgElement("text", {
      x: lx,
      y: ly,
      class: "radar-label",
      "text-anchor":
        Math.abs(lx - cx) < 10 ? "middle" : lx > cx ? "start" : "end",
    });
    const displayLabel = compact
      ? { misses: "Control de misses", completion: "Completar" }[axis.key] ||
        axis.label
      : axis.label;
    const words = displayLabel.split(" "),
      lines = [];
    let line = "";
    words.forEach((word) => {
      if (line && (line + " " + word).length > 12) {
        lines.push(line);
        line = word;
      } else line += (line ? " " : "") + word;
    });
    if (line) lines.push(line);
    lines.forEach((part, i) =>
      label.append(
        svgElement(
          "tspan",
          {
            x: lx,
            dy: i === 0 ? (ly < cy ? -(lines.length - 1) * 13 : 5) : 13,
          },
          part,
        ),
      ),
    );
    svg.append(label);
  });
  for (const step of [50, 100])
    svg.append(
      svgElement(
        "text",
        { x: cx + 5, y: cy - (radius * step) / 100 + 11, class: "radar-guide" },
        String(step),
      ),
    );
  const plotted = axes.map((axis, index) =>
    axis.score === null ? null : xy(index, axis.score / 100),
  );
  // Never fill or bridge missing axes: no observation is not a zero.
  if (plotted.every(Boolean))
    svg.append(
      svgElement("polygon", {
        points: plotted.map((p) => p.join(",")).join(" "),
        class: "radar-fill",
      }),
    );
  axes.forEach((axis, index) => {
    const next = (index + 1) % axes.length,
      a = plotted[index],
      b = plotted[next];
    if (a && b)
      svg.append(
        svgElement("line", {
          x1: a[0],
          y1: a[1],
          x2: b[0],
          y2: b[1],
          class: "radar-edge",
          "data-provisional": axis.provisional || axes[next].provisional,
        }),
      );
  });
  axes.forEach((axis, index) => {
    const point = plotted[index];
    if (!point) return;
    const circle = svgElement("circle", {
      cx: point[0],
      cy: point[1],
      r: 4,
      class: "radar-point",
      "data-provisional": axis.provisional,
    });
    circle.append(
      svgElement(
        "title",
        {},
        `${axis.label}: ${axis.raw}. ${axis.target}. ${statusText(axis.status)}. ${axis.samples} partidas, ${axis.maps} mapas, ${axis.sessions} sesiones.`,
      ),
    );
    svg.append(circle);
  });
  host.replaceChildren(svg);
  if (!plotted.some(Boolean))
    host.append(
      htmlElement(
        "p",
        "radar-empty",
        "La telaraña se dibuja al reunir partidas comparables.",
      ),
    );
  if (!compact && axes.some((axis) => axis.score !== null && axis.provisional))
    host.append(
      htmlElement(
        "p",
        "radar-caption",
        "○ Evidencia inicial · ● Evidencia moderada",
      ),
    );
}

export function renderRadarValues(host, axes) {
  host.replaceChildren(
    ...axes.map((axis) => {
      const row = htmlElement("div", "radar-value");
      row.dataset.status = axis.status;
      row.append(
        htmlElement("span", "", axis.label),
        htmlElement("strong", "", axis.raw),
      );
      row.append(
        htmlElement(
          "p",
          "",
          `${statusText(axis.status)} · ${axis.samples} partidas · ${axis.maps} mapas · ${axis.sessions} sesiones`,
        ),
      );
      row.append(htmlElement("p", "", axis.target));
      return row;
    }),
  );
}
