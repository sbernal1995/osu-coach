import {
  buildRadarModel,
  renderRadar,
  renderRadarValues,
} from "./profile-radar.js";

("use strict");
const $ = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("es-AR", {
  maximumFractionDigits: 2,
});
const dateFormat = new Intl.DateTimeFormat("es-AR", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});
const progressDateFormat = new Intl.DateTimeFormat("es-AR", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
let currentState = null;
let currentRender = "";
let toastTimer;
let busyAction = false;
let online = false;
let stopped = false;
let observedQuestBoardId = null;
let observedCompletedQuests = new Set();
let observedCompletionProfile = null;
let observedCompletionIds = new Set();
let settingsFields = new Map();
let settingsSchemaSignature = "";
let settingsFormBase = {};
let settingsDirty = false;
let settingsSaving = false;
function text(id, value) {
  $(id).textContent = value;
}
function numeric(value) {
  return value !== null && value !== "" && Number.isFinite(Number(value));
}
function format(value, fallback = "Sin datos") {
  return numeric(value) ? numberFormat.format(Number(value)) : fallback;
}
function accuracy(value) {
  return numeric(value) ? format(value) + "\u00a0%" : "Sin datos";
}
function duration(value) {
  if (!numeric(value)) return "Sin duración";
  const seconds = Math.max(0, Math.round(Number(value)));
  return Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
}
function element(tag, className, content) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (content !== undefined) el.textContent = String(content ?? "");
  return el;
}
function tell(message, error = false) {
  clearTimeout(toastTimer);
  text("toast", message);
  $("toast").dataset.error = String(error);
  $("toast").hidden = false;
  toastTimer = setTimeout(
    () => {
      $("toast").hidden = true;
    },
    error ? 7500 : 3500,
  );
}
function safeMapURL(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" &&
      url.hostname === "osu.ppy.sh" &&
      !url.username &&
      !url.password &&
      !url.port
      ? url.href
      : null;
  } catch {
    return null;
  }
}
async function copySearch(value, button) {
  if (!value) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      const field = element("textarea");
      field.value = value;
      field.style.position = "fixed";
      field.style.left = "-9999px";
      document.body.append(field);
      field.select();
      const copied = document.execCommand("copy");
      field.remove();
      button.focus();
      if (!copied) throw new Error("copy");
    }
    tell("Búsqueda copiada. Pegala en el selector de mapas de osu!.");
  } catch {
    tell("El navegador bloqueó la copia. Texto de búsqueda: " + value, true);
  }
}
function manualDiscoveryAvailable(state) {
  const discovery = state?.discovery;
  return (
    Boolean(discovery) &&
    discovery.state !== "loading" &&
    (discovery.state !== "paused" ||
      (discovery.automatic === false && discovery.manual_allowed === true))
  );
}
function setAvailability() {
  $("stop-button").disabled = !online || busyAction || stopped;
  $("reset-button").disabled = !online || busyAction || !currentState;
  $("rescan-button").disabled =
    !online || busyAction || !currentState || Boolean(currentState.scanning);
  $("tag-sync-button").disabled =
    !online ||
    busyAction ||
    stopped ||
    !currentState?.tag_sync ||
    currentState.tag_sync.state === "loading";
  $("discovery-sync-button").disabled =
    !online || busyAction || stopped || !manualDiscoveryAvailable(currentState);
  $("quest-new-button").disabled =
    !online ||
    busyAction ||
    stopped ||
    !currentState ||
    !Object.prototype.hasOwnProperty.call(currentState, "quest_board");
  $("confirm-reset").disabled = busyAction || !online;
  $("cancel-reset").disabled = busyAction;
  document
    .querySelectorAll(".pending-actions button, .song-preference-button")
    .forEach((button) => {
      button.disabled = !online || busyAction;
    });
  updateSettingsAvailability();
}
async function request(path, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {
      method: payload === undefined ? "GET" : "POST",
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
      headers:
        payload === undefined
          ? {}
          : {
              "Content-Type": "application/json",
              "X-Coach-Token": currentState?.token || "",
            },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
    });
    const data = await response.json();
    if (!response.ok)
      throw new Error(
        data.error || data.message || "El script no pudo completar la acción.",
      );
    return data;
  } finally {
    clearTimeout(timer);
  }
}
async function action(path, payload, success) {
  if (busyAction || !online) return;
  busyAction = true;
  setAvailability();
  try {
    await request(path, payload);
    if (path === "/api/reset") $("reset-dialog").close();
    tell(success);
    await refresh();
  } catch (error) {
    tell(
      error.name === "AbortError"
        ? "El script tardó en responder. Revisá la conexión e intentá otra vez."
        : error.message,
      true,
    );
  } finally {
    busyAction = false;
    setAvailability();
  }
}
function settingValue(state, key, fallback) {
  const value = state?.settings?.values?.[key];
  return value === undefined || value === null ? fallback : value;
}
function renderSettingsHelp(state) {
  const v = (key, fallback) => format(settingValue(state, key, fallback));
  const memory =
    v("reference_plays", 100) +
    " partidas en " +
    v("reference_days", 30) +
    " días";
  const strong =
    "al menos " +
    v("strong_accuracy", 97) +
    " % de precisión, hasta " +
    v("strong_miss_percent", 0.5) +
    " % de misses y al menos " +
    v("strong_combo_percent", 80) +
    " % del combo máximo";
  const evidence =
    v("profile_min_plays", 8) +
    " mediciones válidas en al menos " +
    v("profile_min_maps", 5) +
    " mapas comparables y " +
    v("profile_min_sessions", 2) +
    " sesiones";
  const help = (id, label, message) =>
    $(id).replaceChildren(
      element("strong", "", label + ". "),
      document.createTextNode(message),
    );
  help(
    "help-reference",
    "Referencia y sesión",
    "La referencia recuerda hasta " +
      v("reference_plays", 100) +
      " partidas de los últimos " +
      v("reference_days", 30) +
      " días. El estado de la sesión y la tabla reciente miran hasta " +
      v("session_plays", 20) +
      " partidas de los últimos " +
      v("session_days", 7) +
      " días. Cada perfil parte del mismo jugador, cliente de osu! y combinación de mods. Las partidas de misiones con mods recomendados se suman a la progresión que las propuso, guardando los mods usados. Tu ranking y tus PP históricos quedan fuera del cálculo. El registro local conserva todas las partidas guardadas, aunque salgan de estas ventanas.",
  );
  help(
    "help-calibration",
    "Calibración y memoria",
    "Reuní " +
      v("calibration_plays", 5) +
      " partidas en al menos " +
      v("calibration_maps", 3) +
      " mapas distintos para establecer una referencia; podés usar el coach desde la primera partida. La memoria toma como máximo " +
      v("max_attempts_per_map", 2) +
      " intentos recientes de cada mapa. Una partida de hace " +
      v("half_life_days", 10) +
      " días pesa la mitad que una de hoy. El cálculo recorta un " +
      v("trim_percent", 20) +
      " % del peso de cada extremo para limitar la influencia de resultados atípicos. Los resultados nuevos ajustan la referencia junto con lo que venías demostrando.",
  );
  help(
    "help-rank",
    "Máximo rango demostrado",
    "Registra los resultados sólidos que alcanzaste y se conserva al recalibrar o al bajar tu forma reciente. Avanza en pasos de " +
      v("rank_step", 0.25) +
      " ★ al reunir resultados sólidos en " +
      v("rank_required_maps", 3) +
      " mapas distintos cerca del rango objetivo, dentro de las últimas " +
      memory +
      ": mapas completados con " +
      strong +
      ". La próxima práctica sigue tu referencia actual. Cambiar los ajustes conserva tus rangos; los próximos ascensos se evalúan al aceptar partidas nuevas. El gráfico conserva la etapa desde la última calibración y separa los cambios de cálculo o de ajustes.",
  );
  help(
    "help-profile",
    "Cómo usa tu perfil",
    "Una fortaleza o un aspecto para mejorar necesita " +
      evidence +
      ". Una pausa de " +
      v("session_gap_minutes", 60) +
      " minutos o más separa sesiones. La evidencia usa hasta " +
      memory +
      ", con más peso para las recientes. Al consolidar se mantiene la dificultad y se ajustan los objetivos. Una sesión difícil puede reducir temporalmente la práctica y pausar el desafío. Si la sesión pide una reducción mayor, se usa esa reducción. El desafío habilitado apunta a +" +
      v("consolidate_increment", 0.1) +
      " ★ al consolidar o +" +
      v("challenge_increment", 0.15) +
      " ★ al avanzar.",
  );
  help(
    "help-reference-change",
    "Cuándo cambia la referencia",
    "Cada resultado aporta una estimación según la dificultad y el control que demostraste. La referencia combina esas estimaciones con la memoria de " +
      memory +
      ". Los resultados sólidos en mapas más difíciles pueden elevarla; las dificultades repetidas pueden reducirla. También puede variar al cambiar el peso de las partidas antiguas o al salir resultados de la ventana.",
  );
  help(
    "help-session",
    "Cómo responde la sesión",
    "El estado reciente señala mejora con resultados sólidos en 3 mapas distintos entre los últimos 5 intentos. Si 2 de los últimos 3 resultan difíciles de controlar, puede bajar la práctica " +
      v("recovery_drop", 0.25) +
      " ★ respecto de la referencia. Es un ajuste de la práctica; la referencia sigue reuniendo la memoria más amplia. Un resultado sólido completa el mapa con " +
      strong +
      ", cuando el dato de combo está disponible.",
  );
  help(
    "help-challenge",
    "Consolidar y probar un desafío",
    "Consolidar mantiene mapas controlables y variados. El desafío aparece marcado dentro de Práctica principal, con una meta concreta que admite misses. Una vez calibrado, se habilita cuando tus últimas " +
      v("challenge_maps", 3) +
      " partidas son en " +
      v("challenge_maps", 3) +
      " mapas distintos, todas completadas con al menos " +
      v("challenge_accuracy", 94) +
      " % de precisión y hasta " +
      v("challenge_miss_percent", 2) +
      " % de misses, siempre que el perfil permita salir de recuperación. Probalo si la práctica principal salió cómoda. El tipo de mapa prioritario orienta la selección de práctica cuando hay evidencia suficiente.",
  );
  $("help-challenge").append(document.createTextNode(" La misión exige completar y alcanzar la meta principal con el control indicado. El grado y el combo pueden ser orientativos. Las referencias repetidas esperan " + v("benchmark_cooldown_days", 7) + " días, con un máximo de una activa; se pueden desactivar en Ajustes. La evolución compara hasta " + v("trend_plays", 1000) + " partidas de " + v("trend_days", 90) + " días en condiciones equivalentes. El BPM no es un límite estricto salvo que lo actives."));
  help(
    "help-tags",
    "Tipos de mapa",
    "Los tags agrupan tus resultados cerca de tu referencia (±" +
      v("comparable_star_band", 0.5) +
      " ★), usando hasta " +
      memory +
      " y como máximo " +
      v("max_attempts_per_map", 2) +
      " intentos por mapa. Una tendencia necesita " +
      evidence +
      ". Las tarjetas muestran qué evidencia la respalda; mientras falten datos, conservan la lectura inicial. Podés consultar el nombre original y la fuente de cada tag al mantener el cursor sobre él.",
  );
  const enabled = settingValue(state, "discovery_enabled", true);
  const introduction = enabled
    ? "Cuando faltan misiones o reserva, el coach recorre hasta " +
      v("discovery_batches_per_pass", 10) +
      " lotes seguidos y comprueba después de cada uno si ya hay suficientes opciones. Si todavía faltan mapas, hace una pausa de " +
      v("discovery_retry_minutes", 1) +
      " minutos y continúa desde donde quedó. Al completar las opciones, revisa novedades cada " +
      v("discovery_interval_hours", 24) +
      " horas mientras está abierto y conserva los resultados para la próxima sesión. "
    : "La búsqueda automática está desactivada en Configuración. Podés activarla para que el coach busque opciones cuando falten dificultades adecuadas. ";
  const quality = element(
    "span",
    "",
    discoveryQualityMessage(state.discovery, state),
  );
  quality.id = "discovery-quality-help";
  $("help-new-maps").replaceChildren(
    element("strong", "", "Mapas nuevos. "),
    document.createTextNode(introduction),
    quality,
    document.createTextNode(
      " Cuando hay suficientes mapas locales, agrega hasta una opción por descargar por grupo; si faltan locales, puede ofrecer hasta tres. Con mods, la búsqueda queda pausada hasta disponer de una dificultad comparable.",
    ),
  );
  text(
    "reset-requirements",
    "Jugá " +
      v("calibration_plays", 5) +
      " partidas en al menos " +
      v("calibration_maps", 3) +
      " mapas distintos para establecer una nueva referencia.",
  );
}
function setSettingsFeedback(message, kind = "info") {
  text("settings-feedback", message);
  $("settings-feedback").dataset.state = kind;
}
// Duration settings are stored in seconds; the form displays and accepts mm:ss.
function isDurationSetting(spec) {
  return spec.type === "integer" && spec.key.endsWith("_seconds");
}
function settingsDuration(value) {
  return duration(value).padStart(5, "0");
}
function settingsInputValue(field) {
  if (isDurationSetting(field.spec)) {
    const match = /^(\d+):([0-5]\d)$/.exec(field.input.value.trim());
    if (!match) return null;
    const seconds = Number(match[1]) * 60 + Number(match[2]);
    return Number.isSafeInteger(seconds) ? seconds : null;
  }
  if (field.spec.type === "choice") return field.input.value;
  return field.spec.type === "boolean"
    ? field.input.checked
    : field.input.value.trim() === ""
      ? null
      : Number(field.input.value);
}
function settingsHasEdits() {
  return Array.from(settingsFields.values()).some(
    (field) => settingsInputValue(field) !== settingsFormBase[field.spec.key],
  );
}
function updateSettingsAvailability() {
  const disabled = !online || busyAction || stopped || !currentState?.settings;
  $("settings-save").disabled = disabled || !settingsDirty;
  $("settings-reset").disabled = disabled;
  $("settings-discard").disabled = disabled;
  $("settings-discard").hidden = !settingsDirty;
  settingsFields.forEach((field) => {
    field.input.disabled = disabled;
  });
  text("settings-save", settingsSaving ? "Guardando…" : "Guardar ajustes");
}
function applySettingsValues(settings) {
  settingsFields.forEach((field) => {
    const value =
      settings?.values?.[field.spec.key] ??
      settings?.defaults?.[field.spec.key] ??
      field.spec.default;
    if (field.spec.type === "boolean") field.input.checked = Boolean(value);
    else if (isDurationSetting(field.spec))
      field.input.value = settingsDuration(value);
    else
      field.input.value =
        value === undefined || value === null ? "" : String(value);
    field.input.removeAttribute("aria-invalid");
    field.error.hidden = true;
    settingsFormBase[field.spec.key] = settingsInputValue(field);
  });
  settingsDirty = false;
}
function renderSettings(state) {
  const settings = state.settings;
  const schema = Array.isArray(settings?.schema)
    ? settings.schema.filter(
        (spec) =>
          spec &&
          typeof spec.key === "string" &&
          ["integer", "number", "boolean", "choice"].includes(spec.type),
      )
    : [];
  $("settings-section").hidden = !schema.length;
  $("settings-open").hidden = !schema.length;
  if (!schema.length || settingsSaving) return;
  const signature = JSON.stringify(schema);
  if (signature !== settingsSchemaSignature) {
    const edits = settingsDirty
      ? new Map(
          Array.from(settingsFields, ([key, field]) => [
            key,
            {
              value: field.input.value,
              checked: field.input.checked,
              base: settingsFormBase[key],
            },
          ]),
        )
      : null;
    const openGroups = new Set(
      Array.from(
        $("settings-groups").querySelectorAll("details[open]"),
        (node) => node.dataset.group,
      ),
    );
    settingsSchemaSignature = signature;
    settingsFields = new Map();
    settingsFormBase = {};
    const groups = new Map();
    const root = $("settings-groups");
    root.replaceChildren();
    schema.forEach((spec, index) => {
      const groupName = String(spec.group || "Otros ajustes");
      if (!groups.has(groupName)) {
        const group = element("details", "settings-group");
        group.dataset.group = groupName;
        group.open = openGroups.size
          ? openGroups.has(groupName)
          : groups.size === 0;
        group.append(element("summary", "", groupName));
        const fields = element("fieldset", "settings-fields");
        fields.append(element("legend", "visually-hidden", groupName));
        group.append(fields);
        groups.set(groupName, fields);
        root.append(group);
      }
      const row = element("div", "settings-field");
      if (spec.type === "choice") row.classList.add("settings-field-choice");
      const input = element(spec.type === "choice" ? "select" : "input");
      const id = "setting-field-" + index;
      input.id = id;
      input.name = spec.key;
      if (spec.type === "choice") {
        (spec.options || []).forEach((option) => {
          const entry = element("option", "", option.label);
          entry.value = option.value;
          input.append(entry);
        });
      } else if (isDurationSetting(spec)) {
        input.type = "text";
        input.required = true;
        input.placeholder = "mm:ss";
        input.autocomplete = "off";
        input.spellcheck = false;
        input.addEventListener("blur", () => {
          const value = settingsInputValue({ spec, input });
          if (value !== null) input.value = settingsDuration(value);
        });
      } else input.type = spec.type === "boolean" ? "checkbox" : "number";
      if (
        spec.type !== "boolean" &&
        spec.type !== "choice" &&
        !isDurationSetting(spec)
      ) {
        input.required = true;
        input.inputMode = spec.type === "integer" ? "numeric" : "decimal";
        if (numeric(spec.min)) input.min = String(spec.min);
        if (numeric(spec.max)) input.max = String(spec.max);
        input.step = numeric(spec.step)
          ? String(spec.step)
          : spec.type === "integer"
            ? "1"
            : "any";
      }
      const label = element(
        "label",
        "",
        isDurationSetting(spec)
          ? (spec.label || spec.key).replace("(segundos)", "(mm:ss)")
          : spec.label || spec.key,
      );
      label.htmlFor = id;
      const displayValue = isDurationSetting(spec) ? settingsDuration : format;
      const bounds =
        spec.type === "boolean"
          ? ""
          : numeric(spec.min) && numeric(spec.max)
            ? " Valores de " +
              displayValue(spec.min) +
              " a " +
              displayValue(spec.max) +
              "."
            : "";
      const description = element(
        "p",
        "settings-field-description",
        (isDurationSetting(spec)
          ? (spec.description || "").replace(/\b0\b/g, "00:00") +
            " Ingresá minutos y segundos, por ejemplo 02:30."
          : spec.description || "") + bounds,
      );
      description.id = id + "-description";
      const error = element("p", "settings-field-error");
      error.id = id + "-error";
      error.hidden = true;
      input.setAttribute("aria-describedby", description.id + " " + error.id);
      row.append(label, input, description, error);
      settingsFields.set(spec.key, { spec, input, error });
      groups.get(groupName).append(row);
    });
    applySettingsValues(settings);
    if (edits) {
      edits.forEach((draft, key) => {
        const field = settingsFields.get(key);
        if (!field) return;
        field.input.value = draft.value;
        field.input.checked = draft.checked;
        settingsFormBase[key] = draft.base;
      });
      settingsDirty = settingsHasEdits();
    } else {
      setSettingsFeedback("Estos son tus ajustes guardados.");
    }
  } else if (!settingsDirty) {
    applySettingsValues(settings);
  }
  updateSettingsAvailability();
}
function readSettingsChanges() {
  const values = {};
  let firstInvalid = null;
  settingsFields.forEach((field) => {
    const value = settingsInputValue(field);
    let message = "";
    if (field.spec.type === "choice") {
      if (!(field.spec.options || []).some((option) => option.value === value))
        message = "Elegí una opción.";
    } else if (isDurationSetting(field.spec)) {
      if (value === null)
        message = "Usá mm:ss, por ejemplo 02:30. Los segundos van de 00 a 59.";
      else if (value < field.spec.min)
        message = "El mínimo es " + settingsDuration(field.spec.min) + ".";
      else if (value > field.spec.max)
        message = "El máximo es " + settingsDuration(field.spec.max) + ".";
      else if (
        numeric(field.spec.step) &&
        field.spec.step > 0 &&
        (value - field.spec.min) % field.spec.step !== 0
      )
        message =
          "Usá incrementos de " + settingsDuration(field.spec.step) + ".";
    } else if (field.spec.type !== "boolean") {
      if (value === null || !Number.isFinite(value))
        message = "Ingresá un número.";
      else if (field.spec.type === "integer" && !Number.isInteger(value))
        message = "Ingresá un número entero.";
      else if (field.input.validity.rangeUnderflow)
        message = "El mínimo es " + format(field.spec.min) + ".";
      else if (field.input.validity.rangeOverflow)
        message = "El máximo es " + format(field.spec.max) + ".";
      else if (field.input.validity.stepMismatch)
        message = "Usá incrementos de " + format(field.spec.step, "1") + ".";
    }
    field.error.textContent = message;
    field.error.hidden = !message;
    if (message) {
      field.input.setAttribute("aria-invalid", "true");
      if (!firstInvalid) firstInvalid = field.input;
    } else {
      field.input.removeAttribute("aria-invalid");
      if (value !== settingsFormBase[field.spec.key])
        values[field.spec.key] = value;
    }
  });
  if (firstInvalid) {
    setSettingsFeedback(
      "Revisá los campos señalados antes de guardar.",
      "error",
    );
    const group = firstInvalid.closest(".settings-group");
    if (group) group.open = true;
    firstInvalid.focus();
    return null;
  }
  return values;
}
async function saveSettings(reset = false) {
  if (busyAction || !online || stopped || !currentState?.settings) return;
  const values = reset ? null : readSettingsChanges();
  if (!reset && values === null) return;
  if (!reset && !Object.keys(values).length) return;
  busyAction = true;
  settingsSaving = true;
  setSettingsFeedback(
    reset ? "Restaurando los valores iniciales…" : "Guardando tus ajustes…",
  );
  setAvailability();
  try {
    const result = await request(
      "/api/settings",
      reset ? { reset: true } : { values },
    );
    await refresh();
    if (result?.settings && currentState)
      currentState.settings = result.settings;
    settingsDirty = false;
    applySettingsValues(currentState.settings);
    setSettingsFeedback(
      reset
        ? "Valores iniciales restaurados. Tus partidas y logros siguen guardados."
        : "Ajustes guardados. Se usarán en las próximas recomendaciones.",
      "success",
    );
    tell(reset ? "Valores iniciales restaurados." : "Ajustes guardados.");
  } catch (error) {
    setSettingsFeedback(
      error.name === "AbortError"
        ? "La respuesta tardó demasiado. Tus cambios siguen en el formulario; podés volver a guardar."
        : error.message,
      "error",
    );
  } finally {
    settingsSaving = false;
    busyAction = false;
    if (currentState) renderSettings(currentState);
    setAvailability();
  }
}
function tagSource(source) {
  return (
    {
      community: "Comunidad de osu!",
      mapper: "Autor del mapa",
      manual: "Etiqueta manual",
    }[source] || "Tags del mapa"
  );
}
function shortTagName(name) {
  const value = String(name || "");
  const analysisItems = currentState?.tag_analysis?.items;
  const named = Array.isArray(analysisItems)
    ? analysisItems.find((item) => item?.tag === value && item.name)
    : null;
  if (named) return named.name;
  const tail = value.split("/").pop();
  const names = {
    aim: "Aim",
    speed: "Velocidad",
    stamina: "Resistencia",
    reading: "Lectura",
    precision: "Precisión",
    rhythm: "Ritmo",
    jumps: "Saltos",
    jump: "Saltos",
    streams: "Streams",
    stream: "Streams",
    bursts: "Ráfagas",
    burst: "Ráfagas",
    "finger-control": "Control de dedos",
    fingercontrol: "Control de dedos",
    "finger control": "Control de dedos",
    tech: "Técnico",
    technical: "Técnico",
    flowaim: "Aim fluido",
    "flow aim": "Aim fluido",
    consistency: "Consistencia",
    sliders: "Sliders",
  };
  return (
    names[tail.toLowerCase()] || tail.replaceAll("_", " ").replaceAll("-", " ")
  );
}
function mapTagChips(tags, tagStatus) {
  const valid = Array.isArray(tags)
    ? tags.filter(
        (tag) => tag && typeof tag.name === "string" && tag.name.trim(),
      )
    : [];
  if (!valid.length) {
    const messages = {
      missing: "Tags por consultar",
      none: "Sin tags publicados",
      weak: "Tags con pocos votos",
      known: "Sin tags de habilidad",
    };
    if (!messages[tagStatus]) return null;
    const empty = element("p", "tag-evidence", messages[tagStatus]);
    empty.style.marginTop = "10px";
    if (tagStatus === "weak")
      empty.title =
        "Los tags de la comunidad necesitan al menos 5 votos para orientar la práctica.";
    return empty;
  }
  const root = element("div", "map-tags");
  root.setAttribute("aria-label", "Tipos de mapa");
  const chip = (tag) => {
    const tagEl = element("span", "map-tag", shortTagName(tag.name));
    tagEl.title =
      tag.name +
      " · " +
      tagSource(tag.source) +
      (numeric(tag.count) ? " · " + format(tag.count) + " votos" : "");
    return tagEl;
  };
  valid.slice(0, 4).forEach((tag) => root.append(chip(tag)));
  if (valid.length > 4) {
    const more = element("details", "map-tag-more");
    const remainder = element("div");
    valid.slice(4).forEach((tag) => remainder.append(chip(tag)));
    more.append(
      element("summary", "", "+" + (valid.length - 4) + " tags"),
      remainder,
    );
    root.append(more);
  }
  return root;
}
function coachDate(value) {
  return value && Number.isFinite(new Date(value).getTime())
    ? progressDateFormat.format(new Date(value))
    : "Fecha sin datos";
}
function coachSvgElement(name, attributes = {}, content) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attributes).forEach(([key, value]) =>
    node.setAttribute(key, String(value)),
  );
  if (content !== undefined) node.textContent = content;
  return node;
}
function referenceSettingsSignature(value) {
  return (
    value.settings_signature ||
    ((Number(value.method_version) || 1) >= 2 ? "default-v2" : "legacy")
  );
}
function renderCoachChart(progress) {
  const host = $("coach-reference-chart");
  host.replaceChildren();
  const history = Array.isArray(progress?.history) ? progress.history : [];
  const points = history
    .filter(
      (point) =>
        point &&
        numeric(point.reference) &&
        point.played_at &&
        Number.isFinite(new Date(point.played_at).getTime()),
    )
    .map((point) => ({
      ...point,
      time: new Date(point.played_at).getTime(),
      reference: Number(point.reference),
    }))
    .sort((a, b) => a.time - b.time);
  if (!points.length) {
    host.append(
      element(
        "p",
        "coach-chart-empty",
        "La gráfica aparecerá al registrar referencias de tus partidas.",
      ),
    );
    text(
      "coach-chart-legend",
      "El historial usa las referencias registradas desde tu última calibración.",
    );
    return;
  }
  const width = Math.max(280, Math.round(host.clientWidth || 800));
  const height = 180,
    left = 47,
    right = width - 12,
    top = 15,
    bottom = 146;
  const values = points.map((point) => point.reference);
  const minimum = Math.min(...values),
    maximum = Math.max(...values);
  const padding = Math.max(0.05, (maximum - minimum) * 0.15);
  const low = Math.max(0, minimum - padding),
    high = maximum + padding;
  const start = points[0].time,
    end = points[points.length - 1].time;
  const x = (time) =>
    end === start
      ? (left + right) / 2
      : left + ((time - start) / (end - start)) * (right - left);
  const y = (value) =>
    bottom - ((value - low) / Math.max(0.01, high - low)) * (bottom - top);
  const svg = coachSvgElement("svg", {
    viewBox: "0 0 " + width + " " + height,
    role: "img",
    "aria-labelledby": "coach-chart-svg-title coach-chart-svg-description",
  });
  svg.append(
    coachSvgElement(
      "title",
      { id: "coach-chart-svg-title" },
      "Evolución de tu referencia en estrellas",
    ),
  );
  svg.append(
    coachSvgElement(
      "desc",
      { id: "coach-chart-svg-description" },
      points.length +
        " referencias desde " +
        coachDate(points[0].played_at) +
        " hasta " +
        coachDate(points[points.length - 1].played_at) +
        ". Los valores y sus fechas están disponibles en el historial debajo de la gráfica.",
    ),
  );
  for (const value of [low, (low + high) / 2, high]) {
    const position = y(value);
    svg.append(
      coachSvgElement("line", {
        x1: left,
        x2: right,
        y1: position,
        y2: position,
        stroke: "#39313f",
        "stroke-width": 1,
      }),
    );
    svg.append(
      coachSvgElement(
        "text",
        {
          x: left - 8,
          y: position + 3.5,
          fill: "#aaa4b8",
          "font-size": 10,
          "text-anchor": "end",
        },
        format(value) + " ★",
      ),
    );
  }
  const currentMethod = Number(progress.method_version) || 1;
  const currentSignature = referenceSettingsSignature(progress);
  const isCurrent = (point) =>
    (Number(point.method_version) || 1) === currentMethod &&
    referenceSettingsSignature(point) === currentSignature;
  const hasPreviousMethod = points.some((point) => !isCurrent(point));
  const hasCurrentMethod = points.some(isCurrent);
  const segments = [];
  points.forEach((point) => {
    const method = Number(point.method_version) || 1;
    const signature = referenceSettingsSignature(point);
    if (
      !segments.length ||
      segments[segments.length - 1].method !== method ||
      segments[segments.length - 1].signature !== signature
    )
      segments.push({
        method,
        signature,
        current: isCurrent(point),
        points: [],
      });
    segments[segments.length - 1].points.push(point);
  });
  segments.forEach((segment) => {
    if (segment.points.length < 2) return;
    const path = segment.points
      .map(
        (point, index) =>
          (index ? "L" : "M") +
          x(point.time).toFixed(2) +
          " " +
          y(point.reference).toFixed(2),
      )
      .join(" ");
    svg.append(
      coachSvgElement("path", {
        d: path,
        fill: "none",
        stroke: segment.current ? "#ff8cb0" : "#aaa4b8",
        "stroke-width": 2,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
        "data-method-version": segment.method,
        "data-comparison": segment.current ? "current" : "previous",
      }),
    );
  });
  points.forEach((point, index) => {
    const previous = !isCurrent(point);
    const color = previous ? "#aaa4b8" : "#ff8cb0";
    const circle = coachSvgElement("circle", {
      cx: x(point.time),
      cy: y(point.reference),
      r: index === points.length - 1 ? 3.5 : 2.5,
      fill: point.calibrated ? color : "#1c1b25",
      stroke: point.calibrated ? color : "#b9a4c3",
      "stroke-width": 1.5,
    });
    circle.append(
      coachSvgElement(
        "title",
        {},
        coachDate(point.played_at) +
          ": " +
          format(point.reference) +
          " ★" +
          (point.calibrated
            ? " · Referencia calibrada"
            : " · Durante la calibración") +
          (hasPreviousMethod
            ? previous
              ? " · Otros ajustes o cálculo"
              : " · Ajustes y cálculo actuales"
            : ""),
      ),
    );
    svg.append(circle);
  });
  if (start === end) {
    svg.append(
      coachSvgElement(
        "text",
        {
          x: (left + right) / 2,
          y: height - 9,
          fill: "#aaa4b8",
          "font-size": 10,
          "text-anchor": "middle",
        },
        dateFormat.format(new Date(start)),
      ),
    );
  } else {
    svg.append(
      coachSvgElement(
        "text",
        { x: left, y: height - 9, fill: "#aaa4b8", "font-size": 10 },
        dateFormat.format(new Date(start)),
      ),
    );
    svg.append(
      coachSvgElement(
        "text",
        {
          x: right,
          y: height - 9,
          fill: "#aaa4b8",
          "font-size": 10,
          "text-anchor": "end",
        },
        dateFormat.format(new Date(end)),
      ),
    );
  }
  host.append(svg);
  const methodLegend = hasPreviousMethod
    ? hasCurrentMethod
      ? "Los tramos separados usan cálculos o ajustes diferentes: gris para los anteriores, rosa para los actuales. "
      : "La curva visible corresponde a otros ajustes o a un cálculo anterior. Las nuevas referencias se mostrarán en un tramo aparte. "
    : "";
  text(
    "coach-chart-legend",
    methodLegend +
      (points.some((point) => !point.calibrated)
        ? "Puntos rellenos: referencia calibrada. Puntos huecos: calibración inicial."
        : "Cada punto muestra una referencia calibrada al registrar un resultado."),
  );
}
function renderTrainingLevel(state) {
  const level = state.profile?.training_level;
  $("training-level-panel").hidden = !level;
  if (!level) return;
  text("training-level-value", format(level.stars) + " ★");
  text("training-level-next", numeric(level.next_stars) ? "Próximo paso: " + format(level.next_stars) + " ★" : "Nivel máximo alcanzado");
  text("training-level-counts", `${Math.min(level.completed_maps, level.required_maps)} de ${level.required_maps} dificultades · ${Math.min(level.completed_sessions, level.required_sessions)} de ${level.required_sessions} sesiones`);
  text("training-level-explanation", `Cumplí los objetivos de las misiones marcadas «Cuenta para subir práctica» en dificultades distintas. Al reunir ambos requisitos, subís ${format(level.step)} ★. Los calentamientos y las repeticiones de referencia no suman. El nivel se conserva aunque baje tu rendimiento reciente; recalibrar inicia una nueva progresión. Los ajustes del paso iniciado se mantienen hasta completarlo.`);
  const host = $("training-level-evidence");
  host.replaceChildren();
  for (const item of level.credits || []) host.append(element("p", "", `✓ ${item.title}${item.version ? " [" + item.version + "]" : ""} · ${format(item.stars)} ★ · ${coachDate(item.played_at)}`));
  if (!level.credits?.length) host.append(element("p", "", "Este paso empieza con las nuevas misiones marcadas."));
  for (const item of [...(level.history || [])].reverse()) host.append(element("p", "", `${format(item.from)} → ${format(item.to)} ★ · ${coachDate(item.completed_at)}`));
}

function renderCoachProgress(state) {
  renderTrainingLevel(state);
  const progress = state.coach_progress;
  const valid = progress && typeof progress === "object";
  $("coach-progress-section").hidden = !valid;
  if (!valid) return;
  const stars = (value) =>
    numeric(value) ? format(value) + " ★" : "Por calibrar";
  const currentMethod = Number(progress.method_version) || 1;
  const newMethod = currentMethod >= 2;
  text(
    "coach-initial-label",
    newMethod
      ? "Inicio de la comparación"
      : "Primera referencia calibrada",
  );
  text(
    "coach-change-label",
    newMethod ? "Variación desde el inicio" : "Cambio desde la calibración",
  );
  text(
    "coach-best-label",
    newMethod
      ? "Máxima referencia comparable"
      : "Mejor referencia de esta calibración",
  );
  $("coach-method-note").hidden = !progress.method_note;
  text("coach-method-note", progress.method_note || "");
  const rank = progress.rank || {};
  const next = progress.next_rank || {};
  const maximumRank =
    !numeric(next.stars) && numeric(rank.stars) && Number(rank.stars) >= 10.5;
  text(
    "coach-earned-rank",
    numeric(rank.stars) ? stars(rank.stars) : "Rango por demostrar",
  );
  text(
    "coach-earned-date",
    numeric(rank.stars)
      ? (rank.earned_at
          ? "Ganado el " + coachDate(rank.earned_at) + ". "
          : "") + "Se conserva aunque fluctúe tu referencia actual."
      : "Se gana al sostener resultados sólidos en mapas distintos.",
  );
  text(
    "coach-next-rank",
    maximumRank
      ? "Rango máximo alcanzado"
      : numeric(next.stars)
        ? "Próximo logro: demostrar " + stars(next.stars)
        : "Próximo rango por definir",
  );
  $("coach-next-count").hidden = maximumRank;
  $("coach-next-progress").hidden = maximumRank;
  $("coach-rank-goal").hidden = maximumRank;
  const required = Math.max(
    1,
    Number(next.required_maps) ||
      Number(settingValue(state, "rank_required_maps", 3)),
  );
  const completed = Math.max(
    0,
    Math.min(required, Number(next.completed_maps) || 0),
  );
  const missing = required - completed;
  text("coach-next-count", completed + " de " + required + " mapas");
  $("coach-next-progress").setAttribute("aria-valuemax", String(required));
  $("coach-next-progress").setAttribute("aria-valuenow", String(completed));
  $("coach-next-fill").style.width = (completed / required) * 100 + "%";
  const low = Number(next.stars);
  const high = low + Number(settingValue(state, "comparable_star_band", 0.5));
  const band = numeric(next.stars) ? `${format(low)}–${format(high)} ★` : "la dificultad del próximo rango";
  const missingMessage = missing
    ? `Te ${missing === 1 ? "falta" : "faltan"} ${missing} ${missing === 1 ? "dificultad distinta" : "dificultades distintas"} de ${band} con los objetivos de abajo.`
    : "Ya reuniste los mapas requeridos. El rango se actualiza al aceptar los resultados.";
  text(
    "coach-next-message",
    maximumRank
      ? "Alcanzaste el rango más alto del coach. Seguí practicando con tu referencia actual."
      : next.calibrated ? missingMessage : "Primero completá la calibración. Después podrás sumar mapas para este logro.",
  );
  const missLimit = Number(settingValue(state, "strong_miss_percent", 0.5));
  const requirements = [
    `Elegí una dificultad de ${band}. Repetir una que ya cuenta no suma otro mapa; otra dificultad de la misma canción sí puede contar.`,
    "Terminá y aprobá el mapa (el registro debe mostrar al menos 98 % completado).",
    `En ese mismo intento: ≥${format(settingValue(state, "strong_accuracy", 97))} % de precisión y ≥${format(settingValue(state, "strong_combo_percent", 80))} % del combo máximo.`,
    `Misses: hasta ${format(missLimit)} % de los objetos juzgados. Por ejemplo, con 1.000 objetos, hasta ${Math.floor(missLimit * 10)} misses.`,
  ];
  $("coach-rank-requirements").replaceChildren(
    ...requirements.map((item) => element("li", "", item)),
  );
  text("coach-rank-window", `Cuentan los mejores intentos válidos de cada dificultad entre tus últimas ${next.window_plays || settingValue(state, "reference_plays", 100)} partidas de los últimos ${next.window_days || settingValue(state, "reference_days", 30)} días. El avance hacia este logro puede cambiar cuando un resultado sale de esa ventana; los rangos ya ganados se conservan.`);
  const maps = Array.isArray(next.qualifying_maps)
    ? next.qualifying_maps.filter((item) => item && item.title)
    : [];
  const mapsRoot = $("coach-rank-maps");
  mapsRoot.replaceChildren();
  if (!maps.length) mapsRoot.append(element("p", "", "Todavía no hay resultados que cuenten para este logro."));
  if (maps.length)
    mapsRoot.append(element("p", "", "Estos resultados ya cumplen los objetivos:"));
  maps.forEach((map) => {
    const result = [map.title + (map.version ? " [" + map.version + "]" : "")];
    if (numeric(map.stars)) result.push(stars(map.stars));
    if (numeric(map.accuracy))
      result.push(accuracy(map.accuracy) + " de precisión");
    if (numeric(map.misses))
      result.push(
        format(map.misses) + (Number(map.misses) === 1 ? " miss" : " misses"),
      );
    if (numeric(map.combo_ratio))
      result.push(accuracy(Number(map.combo_ratio) * 100) + " del combo");
    mapsRoot.append(element("p", "", result.join(" · ")));
  });
  const comparableStars = (value) =>
    numeric(value)
      ? stars(value)
      : newMethod && progress.status === "ready"
        ? "Por registrar"
        : "Por calibrar";
  text("coach-initial-reference", comparableStars(progress.initial_reference));
  text("coach-current-reference", stars(progress.current_reference));
  text(
    "coach-reference-change",
    numeric(progress.change)
      ? (Number(progress.change) > 0 ? "+" : "") + stars(progress.change)
      : comparableStars(null),
  );
  $("coach-reference-change").dataset.direction =
    !numeric(progress.change) || Number(progress.change) === 0
      ? "steady"
      : Number(progress.change) > 0
        ? "up"
        : "down";
  text("coach-best-reference", comparableStars(progress.best_reference));
  const coverage = numeric(progress.tracked_plays)
    ? format(progress.tracked_plays) +
      (Number(progress.tracked_plays) === 1
        ? " partida registrada"
        : " partidas registradas") +
      (progress.since ? " desde " + coachDate(progress.since) : "") +
      ". "
    : "";
  text("coach-progress-method", coverage + "Cada punto guarda la referencia calculada al registrar una partida.");
  text(
    "coach-history-method",
    (progress.method ||
      "El rango se gana con resultados sólidos en mapas distintos. Las recomendaciones siguen tu rendimiento actual.") +
      " Los puntos nuevos se guardan al aceptar el resultado; los reconstruidos usan la fecha de la partida.",
  );
  const history = Array.isArray(progress.history)
    ? progress.history.filter((point) => point && numeric(point.reference))
    : [];
  text(
    "coach-history-label",
    "Ver historial de referencias (" + history.length + ")",
  );
  $("coach-history-source").hidden = !history.some(
    (point) => point.source === "reconstructed",
  );
  const historyRoot = $("coach-history-list");
  historyRoot.replaceChildren();
  history
    .slice()
    .sort(
      (a, b) =>
        (new Date(b.played_at).getTime() || 0) -
        (new Date(a.played_at).getTime() || 0),
    )
    .forEach((point) => {
      const row = element("li");
      const origin =
        point.source === "reconstructed"
          ? "Reconstruido"
          : "Registrado por el coach";
      const sameMethod = (Number(point.method_version) || 1) === currentMethod;
      const methodLabel = newMethod
        ? (sameMethod ? " · Cálculo actual" : " · Cálculo anterior") +
          (sameMethod && progress.settings_signature
            ? referenceSettingsSignature(point) ===
              referenceSettingsSignature(progress)
              ? " · Ajustes actuales"
              : " · Otros ajustes"
            : "")
        : "";
      row.append(
        element("span", "", coachDate(point.played_at)),
        element("b", "", stars(point.reference)),
        element(
          "small",
          "",
          (point.calibrated
            ? "Referencia calibrada"
            : "Durante la calibración") +
            methodLabel +
            " · " +
            origin,
        ),
      );
      historyRoot.append(row);
    });
  if (!history.length)
    historyRoot.append(
      element("li", "", "Las referencias aparecerán al jugar."),
    );
  const milestones = Array.isArray(progress.milestones)
    ? progress.milestones
        .filter((item) => item && numeric(item.stars))
        .slice(0, 12)
    : [];
  text("coach-milestones-label", "Rangos ganados (" + milestones.length + ")");
  const milestoneRoot = $("coach-milestone-list");
  milestoneRoot.replaceChildren();
  milestones.forEach((item) => {
    const row = element("li");
    row.append(
      element("b", "", stars(item.stars)),
      element(
        "span",
        "",
        coachDate(item.earned_at) +
          (item.source === "reconstructed"
            ? " · Reconstruido con partidas guardadas"
            : ""),
      ),
    );
    milestoneRoot.append(row);
  });
  if (!milestones.length)
    milestoneRoot.append(
      element(
        "li",
        "",
        "Tu primer rango aparecerá al consolidar los mapas requeridos.",
      ),
    );
  renderCoachChart(progress);
}
function playerEvidence(plays, maps, sessions) {
  return (
    format(plays, "0") +
    (Number(plays) === 1 ? " partida" : " partidas") +
    " en " +
    format(maps, "0") +
    (Number(maps) === 1 ? " mapa" : " mapas") +
    (numeric(sessions)
      ? " · " +
        format(sessions) +
        (Number(sessions) === 1 ? " sesión" : " sesiones")
      : "")
  );
}
function profileValue(item) {
  const unit = item.unit === "pp" ? "puntos porcentuales" : item.unit;
  return numeric(item.value)
    ? format(item.value) + (unit ? "\u00a0" + unit : "")
    : "Sin datos suficientes";
}
function renderTrainingEvidence(state) {
  const host = $("player-skill-levels");
  if (host) {
    host.replaceChildren();
    (state.player_profile?.skill_levels || []).forEach(item => {
      const card = element("article", "player-observation");
      card.append(element("h4", "", item.label), element("div", "player-observation-value", numeric(item.reference) ? format(item.reference) + " ★" : "Por calibrar"));
      card.append(element("p", "", format(item.samples) + " partidas · " + format(item.distinct_maps) + " mapas · " + format(item.sessions) + (item.sessions === 1 ? " sesión" : " sesiones")));
      const status = element("span", "player-observation-status", {practice: "Trabajar a este nivel", strength: "Buen control a mayor dificultad", steady: "Cerca de tu referencia", learning: "Faltan partidas variadas"}[item.status]);
      status.dataset.status = item.status;
      card.append(status);
      host.append(card);
    });
  }
  const trend = state.player_profile?.evolution;
  const root = $("evolution-comparisons");
  if (!root || !trend) return;
  root.replaceChildren();
  text("evolution-summary", format(trend.improved_maps) + " dificultades con algún indicador mejorado de " + format(trend.compared_maps) + " comparadas. Memoria de evolución: hasta " + format(trend.window_plays) + " partidas / " + format(trend.window_days) + " días.");
  text("evolution-method", trend.method + " La sesión, la referencia actual y la evolución usan ventanas independientes y configurables. Son criterios del coach, no umbrales universales de aprendizaje.");
  if (!trend.comparisons?.length) root.append(element("p", "player-empty", "La primera comparación aparece al repetir una dificultad en otra sesión y con los mismos mods. Las misiones de referencia pueden hacerlo automáticamente cuando se cumple la espera configurada."));
  (trend.comparisons || []).slice(0, 12).forEach(item => {
    const row = element("article", "evolution-row");
    row.append(element("strong", "", item.title + (item.version ? " [" + item.version + "]" : "")));
    const mods = (item.mods?.mods || []).map(mod => mod.acronym).join(" + ") || "Sin mods";
    row.append(element("small", "", coachDate(item.before_at) + " → " + coachDate(item.played_at) + " · " + mods + (item.mods?.rate !== 1 ? " · ×" + format(item.mods?.rate) : "")));
    if (!item.completed) row.append(element("p", "", "Último intento incompleto. La precisión parcial no cuenta como mejora."));
    (item.changes || []).forEach(change => {
      const text = change.label + ": " + format(change.before) + " → " + format(change.after) + (change.key === "accuracy" ? " %" : change.key === "max_combo" ? "×" : "");
      const metric = element("span", change.improved ? "practice-improvement" : "", (change.improved ? "✓ " : "") + text);
      row.append(metric);
    });
    if (item.has_setback && item.improved) row.append(element("small", "", "Hubo mejoras y retrocesos: revisá cada indicador."));
    root.append(row);
  });
}

function renderPlayerProfile(state) {
  renderCoachProgress(state);
  renderTrainingEvidence(state);
  const profile = state.player_profile || {};
  const evidence = profile.evidence || {};
  const ready = profile.status === "ready";
  text(
    "player-profile-state",
    ready ? "Basado en tus partidas" : "Lectura inicial",
  );
  $("player-profile-state").dataset.ready = String(ready);
  text(
    "player-profile-summary",
    profile.summary ||
      "Jugá varios mapas de tu rango para conocer tus fortalezas actuales y elegir qué practicar.",
  );
  text(
    "player-profile-evidence",
    "Memoria: " +
      playerEvidence(
        evidence.plays,
        evidence.distinct_maps,
        evidence.sessions,
      ) +
      ". Comparables: " +
      playerEvidence(
        evidence.comparable_plays,
        evidence.comparable_maps,
        evidence.comparable_sessions,
      ) +
      ". Últimos " +
      (numeric(evidence.days)
        ? format(evidence.days)
        : format(settingValue(state, "reference_days", 30))) +
      " días.",
  );
  for (const [id, source, empty] of [
    [
      "player-strengths",
      profile.strengths,
      "Todavía faltan resultados comparables para señalar una fortaleza.",
    ],
    [
      "player-weaknesses",
      profile.weaknesses,
      ready
        ? "Todavía no aparecen dificultades repetidas con estos criterios."
        : "Jugá mapas distintos para encontrar un aspecto concreto para mejorar.",
    ],
  ]) {
    const root = $(id);
    root.replaceChildren();
    const items = Array.isArray(source)
      ? source
          .filter((item) => item && item.label && item.status !== "learning")
          .slice(0, 3)
      : [];
    if (!items.length) root.append(element("p", "player-empty", empty));
    items.forEach((item) => {
      const finding = element("div", "player-finding");
      const head = element("div", "player-finding-head");
      head.append(
        element("span", "player-finding-title", item.label),
        element(
          "span",
          "player-finding-confidence",
          item.confidence === "medium" ? "Evidencia moderada" : "Señal inicial",
        ),
      );
      finding.append(head);
      if (item.evidence) finding.append(element("p", "", item.evidence));
      else if (numeric(item.value))
        finding.append(element("p", "", profileValue(item)));
      if (item.action) finding.append(element("p", "", item.action));
      root.append(finding);
    });
  }
  const progression = profile.progression || {};
  const sessionRecovery = Number(state.profile?.session?.adjustment) < 0;
  const sessionAdjustment = state.profile?.training_adjustments || {};
  const modes = {
    calibrate: "Calibrar tu punto de partida",
    recover: "Recuperar control",
    consolidate: "Consolidar tu nivel",
    advance: "Probar el siguiente paso",
  };
  text(
    "player-plan-mode",
    modes[
      sessionRecovery ? sessionAdjustment.mode || "recover" : progression.mode
    ] || "Reunir partidas",
  );
  const reasons = sessionRecovery
    ? [
        sessionAdjustment.reason ||
          "La sesión reciente pide bajar " +
            format(settingValue(state, "recovery_drop", 0.25)) +
            " ★ para recuperar el control.",
      ]
    : Array.isArray(progression.reasons)
      ? progression.reasons.filter(
          (reason) => typeof reason === "string" && reason.trim(),
        )
      : [];
  text(
    "player-plan-reasons",
    reasons.join(" ") ||
      "La próxima sesión se ajustará al reunir más resultados en mapas de tu rango.",
  );
  const priorities = Array.isArray(profile.priorities)
    ? profile.priorities.filter((item) => item && item.label).slice(0, 2)
    : [];
  const prioritiesRoot = $("player-priorities");
  prioritiesRoot.replaceChildren();
  priorities.forEach((item, index) => {
    const priority = element("div", "player-priority");
    priority.append(
      element(
        "strong",
        "",
        (index === 0 ? "Próximo foco: " : "Después: ") + item.label,
      ),
    );
    if (item.reason && !reasons.includes(item.reason))
      priority.append(element("p", "", item.reason));
    if (item.action)
      priority.append(element("p", "player-next-action", item.action));
    prioritiesRoot.append(priority);
  });
  prioritiesRoot.hidden = !priorities.length;
  const unknowns = Array.isArray(profile.unknowns)
    ? profile.unknowns.filter((item) => typeof item === "string" && item.trim())
    : [];
  text(
    "player-limits",
    unknowns.slice(0, 2).join(" ") ||
      (ready
        ? "El perfil describe tus resultados recientes y se ajusta con tus próximas partidas."
        : "Las fortalezas y los aspectos para mejorar necesitan resultados comparables en varios mapas."),
  );
  const dimensions = Array.isArray(profile.dimensions)
    ? profile.dimensions.filter((item) => item && item.label)
    : [];
  const observations = $("player-observations");
  observations.replaceChildren();
  dimensions.forEach((item) => {
    const observation = element("article", "player-observation");
    observation.append(
      element("h4", "", item.label),
      element("div", "player-observation-value", profileValue(item)),
    );
    const statuses = {
      strength: "Te va bien",
      practice: "A practicar",
      steady: "Estable",
      learning: "Faltan partidas",
    };
    const status = element(
      "span",
      "player-observation-status",
      (statuses[item.status] || "Faltan partidas") +
        " · " +
        (item.confidence === "medium"
          ? "Evidencia moderada"
          : "Lectura inicial"),
    );
    status.dataset.status = item.status || "learning";
    observation.append(status);
    if (item.evidence) observation.append(element("p", "", item.evidence));
    if (numeric(item.samples) || numeric(item.distinct_maps))
      observation.append(
        element(
          "p",
          "",
          playerEvidence(item.samples, item.distinct_maps, item.sessions),
        ),
      );
    if (item.action) observation.append(element("p", "", item.action));
    observations.append(observation);
  });
  if (!dimensions.length)
    observations.append(
      element(
        "p",
        "player-empty",
        "Las estadísticas aparecerán cuando haya partidas para comparar.",
      ),
    );
  const minimum =
    numeric(evidence.min_plays) &&
    numeric(evidence.min_maps) &&
    numeric(evidence.min_sessions)
      ? "Cada conclusión necesita al menos " +
        format(evidence.min_plays) +
        " mediciones válidas en " +
        format(evidence.min_maps) +
        " mapas comparables y " +
        format(evidence.min_sessions) +
        " sesiones. Una pausa de " +
        format(settingValue(state, "session_gap_minutes", 60)) +
        " minutos o más separa sesiones. "
      : "";
  text(
    "player-stats-note",
    "Se usan hasta " +
      (numeric(evidence.max_plays)
        ? format(evidence.max_plays)
        : format(settingValue(state, "reference_plays", 100))) +
      " partidas del mismo perfil en los últimos " +
      (numeric(evidence.days)
        ? format(evidence.days)
        : format(settingValue(state, "reference_days", 30))) +
      " días. " +
      minimum +
      unknowns.slice(2).join(" "),
  );
}
function renderTagAnalysis(state) {
  renderPlayerProfile(state);
  const analysis = state.tag_analysis || {};
  const sync = state.tag_sync || {};
  const loading = sync.state === "loading";
  text("tag-sync-button", loading ? "Actualizando…" : "Actualizar tags");
  $("tag-sync-status").dataset.state = sync.state || "";
  const syncParts = [];
  if (sync.message) syncParts.push(sync.message);
  if (numeric(sync.tagged_maps) && numeric(sync.known_maps))
    syncParts.push(
      format(sync.tagged_maps) +
        " de " +
        format(sync.known_maps) +
        " mapas consultados tienen tags utilizables",
    );
  if (
    loading &&
    numeric(sync.processed) &&
    numeric(sync.total) &&
    Number(sync.total) > 0
  )
    syncParts.push(
      "Consultas: " + format(sync.processed) + "/" + format(sync.total),
    );
  text(
    "tag-sync-status",
    syncParts.join(" · ") ||
      "Las etiquetas se consultarán al conectar el entrenador.",
  );
  if (
    sync.last_updated &&
    Number.isFinite(new Date(sync.last_updated).getTime())
  )
    $("tag-sync-status").title =
      "Última actualización: " + dateFormat.format(new Date(sync.last_updated));
  else $("tag-sync-status").removeAttribute("title");
  const playCoverage =
    numeric(analysis.tagged_plays) && numeric(analysis.total_plays)
      ? "Tags utilizables en " +
        format(analysis.tagged_plays) +
        " de " +
        format(analysis.total_plays) +
        (Number(analysis.total_plays) === 1
          ? " partida reciente. "
          : " partidas recientes. ")
      : "";
  text(
    "tag-analysis-message",
    playCoverage +
      (analysis.message ||
        "Las tendencias aparecerán al reunir partidas con tags."),
  );
  $("tag-focus").hidden = !analysis.focus_tag;
  text(
    "tag-focus",
    analysis.focus_tag
      ? "Próxima práctica: " +
          (analysis.focus_name || shortTagName(analysis.focus_tag))
      : "",
  );
  const items = Array.isArray(analysis.items)
    ? analysis.items.filter((item) => item && item.tag)
    : [];
  const root = $("tag-analysis");
  const extra = $("tag-analysis-more");
  root.replaceChildren();
  extra.replaceChildren();
  $("tag-empty").hidden = Boolean(items.length);
  text(
    "tag-empty",
    loading
      ? "Buscando los tags de tus mapas. El análisis se actualizará automáticamente."
      : "Jugá mapas con tags para reunir resultados por tipo. Podés actualizar las etiquetas de tu biblioteca con «Actualizar tags».",
  );
  const evidence = (plays, maps, sessions) =>
    format(plays, "0") +
    (Number(plays) === 1 ? " partida" : " partidas") +
    " · " +
    format(maps, "0") +
    (Number(maps) === 1 ? " mapa" : " mapas") +
    (numeric(sessions)
      ? " · " +
        format(sessions) +
        (Number(sessions) === 1 ? " sesión" : " sesiones")
      : "");
  const tagEvidence = analysis.evidence || {};
  const tagMinimum =
    numeric(tagEvidence.min_plays) &&
    numeric(tagEvidence.min_maps) &&
    numeric(tagEvidence.min_sessions)
      ? "Una tendencia necesita " +
        format(tagEvidence.min_plays) +
        " mediciones en " +
        format(tagEvidence.min_maps) +
        " mapas y " +
        format(tagEvidence.min_sessions) +
        " sesiones. "
      : "";
  text(
    "tag-method",
    tagMinimum +
      (numeric(tagEvidence.max_plays) && numeric(tagEvidence.days)
        ? "Memoria de hasta " +
          format(tagEvidence.max_plays) +
          " partidas en " +
          format(tagEvidence.days) +
          " días, con más peso para las recientes y hasta " +
          format(settingValue(state, "max_attempts_per_map", 2)) +
          " intentos por mapa. "
        : "") +
      "Cada partida puede aportar a varios tipos de mapa. Comparamos resultados dentro de ±" +
      format(settingValue(state, "comparable_star_band", 0.5)) +
      " ★ de tu referencia dentro de este entrenamiento, incluidas las misiones con mods recomendados. Los tags describen el mapa; ubicar cada error requiere analizar la partida.",
  );
  items.forEach((item, index) => {
    const card = element("article", "tag-card");
    const statuses = {
      strength: "Te va bien",
      practice: "A practicar",
      learning: "Faltan partidas",
      explore: "Por explorar",
    };
    card.dataset.status = statuses[item.status] ? item.status : "learning";
    const top = element("div", "tag-card-top");
    const name = element("h3", "", item.name || shortTagName(item.tag));
    name.title = item.tag + " · " + tagSource(item.source);
    const status = element(
      "span",
      "tag-status",
      statuses[item.status] || "Faltan partidas",
    );
    if (item.label) status.title = item.label;
    top.append(name, status);
    const plays = item.comparable_plays ?? item.plays ?? 0;
    const maps = item.comparable_maps ?? item.distinct_maps ?? 0;
    const counts = element(
      "p",
      "tag-evidence",
      evidence(plays, maps, item.comparable_sessions) + " en tu rango",
    );
    if (Number(item.outside_band_plays) > 0)
      counts.title =
        evidence(item.plays, item.distinct_maps, item.sessions) +
        " en total; " +
        format(item.outside_band_plays) +
        " fuera del rango comparable.";
    card.append(top, counts);
    const metrics = element("div", "tag-metrics");
    for (const [value, label] of [
      [item.accuracy, "precisión"],
      [numeric(item.miss_rate) ? Number(item.miss_rate) * 100 : null, "fallos"],
      [
        numeric(item.pass_rate) ? Number(item.pass_rate) * 100 : null,
        "completados",
      ],
    ]) {
      if (!numeric(value)) continue;
      const metric = element("span");
      metric.append(
        element("b", "", accuracy(value)),
        document.createTextNode(" " + label),
      );
      metrics.append(metric);
    }
    if (metrics.childElementCount) card.append(metrics);
    if (item.message) card.append(element("p", "tag-message", item.message));
    const meta = element(
      "span",
      "tag-meta",
      (item.confidence === "medium"
        ? "Evidencia moderada"
        : "Lectura inicial") +
        " · " +
        tagSource(item.source),
    );
    card.append(meta);
    (index < 8 ? root : extra).append(card);
  });
  $("tag-more").hidden = items.length <= 8;
  text(
    "tag-more-label",
    "Ver otros tipos de mapa (" + Math.max(0, items.length - 8) + ")",
  );
}
function questCheckValue(check, value, target = false) {
  if (check.key === "mods")
    return typeof value === "string"
      ? value
      : "Mods distintos o no verificables";
  if (value === null || value === undefined || value === "")
    return "Falta dato";
  if (check.key === "complete" && typeof value === "boolean")
    return target ? "Completar" : value ? "Completado" : "Sin completar";
  if (typeof value === "object") return "Falta dato";
  if (typeof value === "string" && !numeric(value)) {
    return target &&
      check.key === "grade" &&
      ["S", "A", "B", "C", "D"].includes(value)
      ? value + " o mejor"
      : value;
  }
  if (check.key === "accuracy" && numeric(value))
    return (target ? "≥ " : "") + accuracy(value);
  if (check.key === "combo" && numeric(value))
    return (target ? "≥ " : "") + format(value) + "×";
  if (check.key === "misses" && numeric(value))
    return (
      (target && Number(value) > 0 ? "≤ " : "") +
      format(value) +
      (Number(value) === 1 ? " miss" : " misses")
    );
  return String(value);
}
function questGoal(map, quest) {
  const expected = map.expectation || {};
  const goal = element("div", "goal personal-goal quest-goal");
  goal.append(element("span", "goal-label", "Meta de esta misión"));
  let checks = [];
  const attempt = quest.last_attempt;
  if (attempt && Array.isArray(attempt.checks) && attempt.checks.length) {
    checks = attempt.checks.filter((check) => check && check.key);
  } else {
    if (expected.complete_required !== false)
      checks.push({
        key: "complete",
        label: "Completar el mapa",
        target: true,
      });
    if (expected.grade_min)
      checks.push({
        key: "grade",
        label: "Grado",
        target: expected.grade_label || expected.grade_min,
      });
    if (numeric(expected.accuracy_min))
      checks.push({
        key: "accuracy",
        label: "Precisión",
        target: expected.accuracy_min,
      });
    if (numeric(expected.misses_max))
      checks.push({
        key: "misses",
        label: "Misses",
        target: expected.misses_max,
      });
    if (numeric(expected.combo_min))
      checks.push({ key: "combo", label: "Combo", target: expected.combo_min });
  }
  if (map.play_conditions && !checks.some((check) => check.key === "mods"))
    checks.unshift({
      key: "mods",
      label: "Mods y velocidad",
      target: map.mods_label,
      status: "pending",
    });
  const required = expected.required_keys;
  const optional = Array.isArray(required) ? checks.filter(check => check.key !== "mods" && !required.includes(check.key)) : [];
  if (Array.isArray(required)) checks = checks.filter(check => check.key === "mods" || required.includes(check.key));
  const list = element("ul", "quest-checks");
  const labels = {
    complete: "Completar el mapa",
    grade: "Grado",
    accuracy: "Precisión",
    misses: "Misses",
    combo: "Combo",
  };
  checks.forEach((check) => {
    const status = ["met", "unmet", "unknown"].includes(check.status)
      ? check.status
      : "pending";
    const item = element("li", "quest-check");
    item.dataset.status = status;
    const icon = element(
      "span",
      "quest-check-icon",
      { met: "✓", unmet: "•", unknown: "?", pending: "○" }[status],
    );
    icon.setAttribute("aria-hidden", "true");
    const content = element("div", "quest-check-body");
    const head = element("div", "quest-check-head");
    head.append(
      element("span", "", check.label || labels[check.key] || "Requisito"),
      element(
        "span",
        "quest-check-target",
        questCheckValue(check, check.target, true),
      ),
    );
    content.append(head);
    if (status !== "pending") {
      const result = {
        met: "Cumplido",
        unmet: "Por alcanzar",
        unknown: "No verificable",
      }[status];
      content.append(
        element(
          "p",
          "quest-check-result",
          result + " · " + questCheckValue(check, check.actual),
        ),
      );
    }
    item.append(icon, content);
    list.append(item);
  });
  goal.append(list);
  if (optional.length) {
    const indicators = element("details", "goal-conditions");
    indicators.append(element("summary", "", "Indicadores orientativos · no son requisitos"));
    optional.forEach(check => indicators.append(element("p", "goal-grade-note", (check.label || check.key) + ": " + questCheckValue(check, check.target, true))));
    goal.append(indicators);
  }
  const improvements = (attempt?.improvements || []).filter(item => item.improved);
  if (improvements.length) goal.append(element("p", "practice-improvement", "Mejoraste respecto de la referencia: " + improvements.map(item => ({accuracy: "precisión", misses: "misses", combo: "combo"}[item.key]) + " " + format(item.before) + " → " + format(item.after)).join(" · ")));
  if (!map.expectation && map.goal)
    goal.append(element("p", "goal-grade-note", map.goal));
  if (expected.grade_note)
    goal.append(element("p", "goal-grade-note", expected.grade_note));
  const requirements = Array.isArray(expected.grade_requirements)
    ? expected.grade_requirements.filter(
        (item) => typeof item === "string" && item.trim(),
      )
    : [];
  if (requirements.length) {
    const details = element("details", "goal-conditions");
    const requirementsList = element("ul");
    requirements.forEach((item) =>
      requirementsList.append(element("li", "", item)),
    );
    details.append(
      element("summary", "", "Condiciones del grado"),
      requirementsList,
    );
    goal.append(details);
  }
  if (
    expected.basis ||
    expected.note ||
    expected.confidence ||
    expected.focus
  ) {
    const why = element("details", "goal-conditions");
    why.append(element("summary", "", "Por qué esta meta"));
    const confidence = {
      provisional: "Meta provisional",
      orientative: "Meta orientativa",
      moderate: "Meta ajustada a tus partidas",
    }[expected.confidence];
    if (confidence) why.append(element("p", "goal-grade-note", confidence));
    if (expected.basis)
      why.append(element("p", "goal-grade-note", expected.basis));
    if (expected.note)
      why.append(element("p", "goal-grade-note", expected.note));
    const focus =
      typeof expected.focus === "string"
        ? expected.focus
        : expected.focus?.action || expected.focus?.label;
    if (focus) why.append(element("p", "goal-grade-note", focus));
    why.append(
      element(
        "p",
        "goal-grade-note",
        "Tus resultados nuevos orientan las próximas misiones. Esta misión conserva su objetivo mientras esté pendiente.",
      ),
    );
    goal.append(why);
  }
  if (quest.status === "completed") {
    const completed =
      quest.completed_at &&
      Number.isFinite(new Date(quest.completed_at).getTime())
        ? " · " + dateFormat.format(new Date(quest.completed_at))
        : "";
    goal.append(
      element("p", "quest-last-attempt", "Misión completada" + completed + "."),
    );
  } else if (attempt) {
    const played =
      attempt.played_at &&
      Number.isFinite(new Date(attempt.played_at).getTime())
        ? dateFormat.format(new Date(attempt.played_at))
        : "";
    goal.append(
      element(
        "p",
        "quest-last-attempt",
        (played ? "Último intento: " + played + ". " : "") +
          "Podés reintentar; la misma partida debe cumplir todos los requisitos.",
      ),
    );
  } else {
    goal.append(
      element(
        "p",
        "quest-last-attempt",
        "Cumplí todos los requisitos en una misma partida nueva.",
      ),
    );
  }
  return goal;
}
function mapGoal(map, quest = null) {
  if (quest) return questGoal(map, quest);
  const expected = map.expectation;
  const goal = element("div", expected ? "goal personal-goal" : "goal");
  goal.append(element("span", "goal-label", "Meta para vos"));
  if (!expected || typeof expected !== "object") {
    goal.append(
      document.createTextNode(
        map.goal ||
          "Registrá el resultado para ajustar la próxima recomendación.",
      ),
    );
    return goal;
  }
  const grade = ["S", "A", "B", "C", "D"].includes(expected.grade_min)
    ? expected.grade_min
    : null;
  if (grade) {
    const head = element("div", "goal-head");
    const badge = element("span", "goal-grade", grade);
    badge.setAttribute("aria-hidden", "true");
    head.append(
      badge,
      element(
        "p",
        "goal-grade-label",
        expected.grade_label || grade + " o mejor",
      ),
    );
    goal.append(head);
  } else {
    goal.append(element("p", "goal-grade-label solo", "Completar el mapa"));
  }
  const metrics = element("div", "goal-metrics");
  if (numeric(expected.accuracy_min))
    metrics.append(
      element(
        "span",
        "goal-accuracy",
        "≥ " + accuracy(expected.accuracy_min) + " de precisión",
      ),
    );
  if (numeric(expected.misses_max)) {
    const misses = Math.max(0, Math.floor(Number(expected.misses_max)));
    metrics.append(
      element(
        "span",
        "",
        misses === 0
          ? "0 misses"
          : "Máximo " + format(misses) + (misses === 1 ? " miss" : " misses"),
      ),
    );
  }
  if (metrics.childElementCount) goal.append(metrics);
  if (numeric(expected.combo_min) && Number(expected.combo_min) > 0)
    goal.append(
      element(
        "p",
        "goal-combo",
        "Combo de al menos " + format(expected.combo_min) + "×",
      ),
    );
  if (expected.grade_note)
    goal.append(element("p", "goal-grade-note", expected.grade_note));
  const context = element("p", "goal-context");
  const confidence =
    {
      provisional: "Meta provisional",
      orientative: "Meta orientativa",
      moderate: "Meta ajustada a tus partidas",
    }[expected.confidence] || "Meta orientativa";
  context.append(element("strong", "", confidence));
  if (expected.basis)
    context.append(document.createTextNode(" · " + expected.basis));
  if (expected.note)
    context.append(document.createTextNode(" " + expected.note));
  goal.append(context);
  const requirements = Array.isArray(expected.grade_requirements)
    ? expected.grade_requirements.filter(
        (item) => typeof item === "string" && item.trim(),
      )
    : [];
  if (grade && requirements.length) {
    const details = element("details", "goal-conditions");
    const list = element("ul");
    requirements.forEach((item) => list.append(element("li", "", item)));
    details.append(element("summary", "", "Condiciones del grado"), list);
    goal.append(details);
  }
  return goal;
}
function mapCard(map, quest = null, availability = null, automatic = false) {
  if (!map.mods_label && availability?.mods_label)
    map = { ...map, mods_label: availability.mods_label };
  if (numeric(availability?.difficulty?.stars)) {
    const updated = availability.difficulty.stars;
    const changed =
      numeric(map.stars) && Math.abs(updated - map.stars) >= 0.005;
    map = {
      ...map,
      ...availability.difficulty,
      reason: changed
        ? "Estrellas actualizadas con osu!lazer. Se conserva el objetivo de la misión, asignada con " +
          format(map.stars) +
          " ★. " +
          (map.reason ? "Motivo original: " + map.reason : "")
        : map.reason,
    };
  } else if (availability?.difficulty_pending) {
    map = {
      ...map,
      stars: null,
      reason: "Actualizando estrellas. El objetivo de esta misión se conserva.",
    };
  }
  const card = element("article", "map-card");
  const lookupValue = (key) => availability?.[key] ?? map[key];
  if (quest) {
    card.classList.add("quest-card");
    card.dataset.questStatus = ["pending", "in_progress", "completed"].includes(
      quest.status,
    )
      ? quest.status
      : "pending";
    const missionTop = element("div", "quest-card-top");
    const statuses = {
      pending: "Misión pendiente",
      in_progress: "En práctica",
      completed: automatic
        ? "Completada · esperando otro mapa"
        : "Misión completada",
    };
    const attempts = Math.max(0, Number(quest.attempt_count) || 0);
    missionTop.append(
      element("span", "quest-status", statuses[card.dataset.questStatus]),
      element(
        "span",
        "quest-attempts",
        attempts
          ? format(attempts) + (attempts === 1 ? " intento" : " intentos")
          : "Sin intentos todavía",
      ),
    );
    card.append(missionTop);
    if (automatic) {
      const stageLabel =
        quest.stage_label ||
        {
          warmup: "Entrar en ritmo",
          practice: "Práctica principal",
          consolidate: "Consolidar",
          challenge: "Pequeño desafío",
        }[map.expectation?.stage];
      const stageText = [
        stageLabel,
        numeric(quest.stage_target)
          ? "Referencia al asignarla: " + format(quest.stage_target) + " ★"
          : null,
      ]
        .filter(Boolean)
        .join(" · ");
      if (stageText)
        card.append(element("p", "quest-assigned-stage", stageText));
    }
  }
  const remote =
    typeof availability?.installed === "boolean"
      ? !availability.installed
      : (map.source === "online" || map.local === false) &&
        !(quest && Number(quest.attempt_count) > 0);
  if (remote) card.dataset.source = "online";
  const heading = element("div", "map-heading");
  const title = element("div");
  title.append(
    element("h3", "song-title", map.title || "Mapa sin título"),
    element("p", "artist", map.artist || "Artista sin datos"),
  );
  heading.append(title, element("span", "stars", format(map.stars) + " ★"));
  card.append(
    heading,
    element("span", "version", map.version || "Dificultad sin nombre"),
  );
  const creator = lookupValue("creator");
  if (typeof creator === "string" && creator.trim())
    card.append(element("p", "map-creator", "Mapper: " + creator.trim()));
  if (remote) card.append(element("span", "source-badge", "Por descargar"));
  const tags = mapTagChips(map.tags, map.tag_status);
  if (tags) card.append(tags);
  const stats = element("div", "map-stats");
  if (map.mods_label) {
    const badge = element("span", "map-mods", map.mods_label);
    badge.title =
      "Mods requeridos. Las estrellas y la duración incluyen su efecto.";
    stats.append(badge);
  }
  for (const [label, value] of [
    ["BPM", format(map.bpm)],
    ["Duración", duration(map.length)],
    ["AR", format(map.ar)],
  ]) {
    const stat = element("span");
    stat.append(element("b", "", value), document.createTextNode(" " + label));
    stats.append(stat);
  }
  card.append(stats);
  const popularity = lookupValue("popularity") || {};
  if (remote) {
    const reputation = element("p", "map-popularity");
    const votes = popularity.rating_votes ?? popularity.votes;
    const hasRating = numeric(popularity.rating) && Number(votes) > 0;
    const hasPlays =
      numeric(popularity.play_count) && Number(popularity.play_count) >= 0;
    if (hasRating) {
      const rating = element("span");
      rating.append(
        element("b", "", format(popularity.rating) + "/10"),
        document.createTextNode(
          " · " + format(votes) + (Number(votes) === 1 ? " voto" : " votos"),
        ),
      );
      reputation.append(rating);
    } else {
      reputation.append(
        element("span", "", "Valoración: sin datos suficientes"),
      );
    }
    if (hasPlays) {
      const plays = element("span");
      plays.append(
        element("b", "", format(popularity.play_count)),
        document.createTextNode(
          Number(popularity.play_count) === 1
            ? " partida jugada"
            : " partidas jugadas",
        ),
      );
      reputation.append(plays);
    } else {
      reputation.append(element("span", "", "Partidas jugadas: sin datos"));
    }
    reputation.append(
      element("small", "", "Datos del conjunto de dificultades"),
    );
    card.append(reputation);
  }
  card.append(
    element(
      "p",
      "map-note",
      map.reason || "Una opción cercana a tu dificultad de referencia.",
    ),
    mapGoal(map, quest),
  );
  const actions = element("div", "map-actions");
  const copy = element("button", "copy-button", "Copiar búsqueda");
  const searchText = lookupValue("search_text");
  const canCopy = typeof searchText === "string" && Boolean(searchText.trim());
  copy.type = "button";
  copy.disabled = !canCopy;
  copy.setAttribute(
    "aria-label",
    "Copiar búsqueda de " + (map.title || "este mapa"),
  );
  copy.addEventListener("click", () =>
    copySearch(canCopy ? searchText : "", copy),
  );
  actions.append(copy);
  const url = safeMapURL(map.url);
  if (url) {
    const link = element(
      "a",
      "map-link",
      remote ? "Ver / descargar ↗" : "Ver mapa ↗",
    );
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute(
      "aria-label",
      (remote ? "Ver o descargar " : "Ver ") +
        (map.title || "mapa") +
        " en osu! (abre otra pestaña)",
    );
    actions.append(link);
  }
  card.append(actions);
  if (canCopy && lookupValue("search_method") === "id")
    card.append(
      element(
        "p",
        "map-search-hint",
        "Pegá el código en la búsqueda de osu! para encontrar esta dificultad.",
      ),
    );
  const alternatives = [
    ["search_title_text", "Copiar por título"],
    ["search_mapper_text", "Copiar por mapper"],
  ]
    .map(([key, label]) => ({ value: lookupValue(key), label }))
    .filter(
      (option) =>
        typeof option.value === "string" &&
        option.value.trim() &&
        option.value !== searchText,
    );
  if (alternatives.length) {
    const details = element("details", "map-search-alternatives");
    const options = element("div", "map-search-options");
    details.append(element("summary", "", "Otras formas de buscar"));
    alternatives.forEach((option) => {
      const row = element("div", "map-search-option");
      const button = element("button", "subtle-button", option.label);
      button.type = "button";
      button.setAttribute(
        "aria-label",
        option.label + ": " + (map.title || "este mapa"),
      );
      button.addEventListener("click", () => copySearch(option.value, button));
      row.append(button, element("p", "map-search-query", option.value));
      options.append(row);
    });
    details.append(
      options,
      element(
        "p",
        "map-search-hint",
        "Si aparecen varias opciones, elegí la dificultad de esta tarjeta. Si no aparece, revisá los filtros y la colección seleccionada en osu!.",
      ),
    );
    card.append(details);
  }
  compactMapCard(card, map, quest);
  if (quest && ["pending", "in_progress"].includes(quest.status)) {
    const preference = element("div", "song-preference");
    const ban = element(
      "button",
      "text-button song-preference-button",
      "No me gusta esta canción",
    );
    ban.type = "button";
    ban.title = "Excluir todas sus dificultades. Podés deshacerlo en Ajustes.";
    ban.setAttribute(
      "aria-label",
      "No recomendar la canción " +
        (map.title || "de esta misión") +
        ": excluir todas sus dificultades",
    );
    ban.addEventListener("click", async () => {
      await action(
        "/api/songs/ban",
        { board_id: currentState?.quest_board?.id, quest_id: quest.id },
        "Canción excluida en todas sus dificultades. Podés volver a permitirla en Ajustes.",
      );
      if (!ban.isConnected)
        $("recommendations").querySelector(".map-actions button")?.focus();
    });
    preference.append(ban);
    card.querySelector(".map-details").append(preference);
  }
  return card;
}
function automaticSearchNotice(state, stage = null, empty = false) {
  const discovery = state.discovery;
  if (!discovery) return null;
  const stageKey = (value) => (value === "consolidate" ? "challenge" : value);
  const requested = stage
    ? discovery.needs
    : discovery.search_needs || discovery.needs;
  const needs = (Array.isArray(requested) ? requested : []).filter(
    (need) => need && (!stage || stageKey(need.stage) === stageKey(stage)),
  );
  if (
    settingValue(state, "discovery_enabled", discovery.automatic !== false) ===
      false &&
    (needs.length || empty)
  )
    return {
      state: "paused",
      title: "Búsqueda automática desactivada",
      message:
        discovery.message ||
        "Podés activarla en Configuración o iniciar una búsqueda manual cuando los mods lo permitan.",
    };
  if (!discovery.automatic) return null;
  if (!needs.length && !empty) return null;
  const retry =
    discovery.next_retry &&
    Number.isFinite(new Date(discovery.next_retry).getTime())
      ? dateFormat.format(new Date(discovery.next_retry))
      : null;
  const retryText = retry
    ? "Próximo intento automático: " + retry + (retry.endsWith(".") ? "" : ".")
    : "Se reintentará automáticamente mientras el entrenador esté abierto.";
  if (discovery.state === "paused")
    return {
      state: "paused",
      title: "Búsqueda pausada por los mods",
      message:
        discovery.message ||
        "La búsqueda online necesita una dificultad comparable para estos mods.",
    };
  if (!needs.length)
    return {
      state: "waiting",
      title: "Preparando la búsqueda automática",
      message:
        "El coach comprobará qué dificultades faltan y buscará opciones adecuadas. Las misiones aparecerán cuando encuentre candidatos.",
    };
  if (discovery.state === "loading")
    return {
      state: "loading",
      title: stage
        ? "Buscando un mapa para esta etapa"
        : "Buscando mapas para tus misiones y reserva",
      message:
        (numeric(discovery.active_batch) && numeric(discovery.batch_limit)
          ? "Lote " +
            format(discovery.active_batch) +
            " de hasta " +
            format(discovery.batch_limit) +
            ". "
          : "") +
        "Recorriendo el catálogo y verificando candidatos. Después de cada lote se comprueba lo que falta; las nuevas misiones aparecen cuando un mapa cumple tus filtros.",
    };
  if (discovery.state === "error")
    return {
      state: "error",
      title: "La búsqueda se volverá a intentar",
      message:
        "La última búsqueda falló. " +
        retryText +
        " Las misiones que ya tenés se conservan.",
    };
  if (discovery.continuing)
    return {
      state: "loading",
      title: "Continuando la búsqueda",
      message:
        "Se revisaron " +
        format(discovery.batches_completed, "0") +
        " lotes de este grupo. Todavía faltan opciones; el coach seguirá con el próximo lote desde donde quedó.",
    };
  return {
    state: "waiting",
    title: discovery.exhausted
      ? "Fin del recorrido disponible"
      : "Pausa entre grupos de búsqueda",
    message:
      (discovery.exhausted
        ? "Se llegó al final de la fuente consultada y todavía faltan opciones compatibles. "
        : "Terminó el grupo de lotes y todavía faltan mapas compatibles con tus filtros. ") +
      retryText +
      " Las nuevas misiones aparecerán automáticamente.",
  };
}
function searchNoticeElement(notice, compact = false) {
  const box = element(
    "div",
    compact ? "empty-stage quest-search-note" : "empty-stage",
  );
  box.dataset.searchState = notice.state;
  box.append(
    element("strong", "", notice.title),
    document.createTextNode(notice.message),
  );
  return box;
}
function emptyRange(rec, state) {
  const search = automaticSearchNotice(state, rec.stage, true);
  if (search) return searchNoticeElement(search);
  const empty = element("div", "empty-stage");
  const unplayed =
    state.recommendation_policy?.mode === "unplayed" ||
    rec.empty_reason === "no_unplayed_maps_in_range";
  empty.append(
    element(
      "strong",
      "",
      state.scanning
        ? "Buscando en tu biblioteca"
        : unplayed
          ? "Faltan dificultades sin jugar en esta etapa"
          : "Faltan mapas en este rango",
    ),
  );
  empty.append(
    document.createTextNode(
      state.scanning
        ? "Las opciones aparecerán cuando termine la lectura."
        : unplayed
          ? "No quedan dificultades sin jugar adecuadas para esta etapa. Buscá mapas nuevos o agregá otros a tu biblioteca."
          : "Tu biblioteca tiene pocos mapas en este rango. Podés descargar otros y volver a leer mapas.",
    ),
  );
  const url = safeMapURL(rec.search_url);
  if (url && !state.scanning) {
    const link = element("a", "map-link", "Buscar mapas en este rango ↗");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    empty.append(link);
  }
  return empty;
}
function discoveryQualityMessage(discovery, state = currentState) {
  const policy = discovery?.quality_policy || {};
  const threshold = (key, setting, fallback) =>
    settingValue(
      state,
      setting,
      numeric(policy[key]) && Number(policy[key]) >= 0
        ? Number(policy[key])
        : fallback,
    );
  return (
    "Cada conjunto debe reunir una nota mínima de " +
    format(threshold("min_rating", "quality_min_rating", 8)) +
    "/10 con al menos " +
    format(threshold("min_votes", "quality_min_votes", 10)) +
    " votos, además de " +
    format(threshold("min_play_count", "quality_min_plays", 10000)) +
    " partidas jugadas entre todas sus dificultades."
  );
}
function renderDiscovery(state) {
  const discovery = state.discovery || {};
  const loading = discovery.state === "loading";
  const qualityMessage = discoveryQualityMessage(discovery, state);
  text("discovery-quality", qualityMessage);
  text("discovery-quality-help", qualityMessage);
  const needs = Array.isArray(discovery.needs)
    ? discovery.needs.filter(Boolean)
    : [];
  const search = automaticSearchNotice(state);
  $("discovery-bar").dataset.state = discovery.state || "";
  text("discovery-sync-button", loading ? "Buscando…" : "Explorar más mapas");
  text(
    "discovery-message",
    search
      ? search.title + ". " + search.message
      : discovery.message ||
          "La búsqueda de mapas nuevos estará disponible al actualizar el entrenador.",
  );
  const meta = ["Incluye canciones antiguas y recientes"];
  const limits = (discovery.limits || []).filter(Boolean);
  if (limits.length) {
    meta.push(
      "Rango de búsqueda: " +
        format(Math.min(...limits.map((item) => item.min_stars))) +
        "–" +
        format(Math.max(...limits.map((item) => item.max_stars))) +
        " ★",
    );
    const physical = limits[0];
    if (numeric(physical.max_bpm))
      meta.push("Hasta " + format(physical.max_bpm) + " BPM");
    if (numeric(physical.max_ar)) meta.push("AR ≤ " + format(physical.max_ar));
    if (numeric(physical.min_length))
      meta.push("Duración ≥ " + duration(physical.min_length));
    if (numeric(physical.max_length))
      meta.push("Duración ≤ " + duration(physical.max_length));
    if (!physical.min_length && !physical.max_length)
      meta.push("Sin límite de duración");
    else meta.push("Duración con los mods indicados");
  }
  const reserve = discovery.reserve || [];
  if (reserve.length)
    meta.push(
      "Reserva para descargar: " +
        reserve
          .map((item) => item.label + " " + item.available + "/" + item.target)
          .join(" · "),
    );
  if (
    discovery.next_retry &&
    !needs.length &&
    reserve.some((item) => item.missing > 0)
  )
    meta.push("Preparando más alternativas automáticamente");
  if (needs.length)
    meta.push(
      "Etapas: " +
        needs
          .map(
            (need) =>
              need.label ||
              {
                warmup: "Entrar en ritmo",
                practice: "Práctica principal",
                challenge: "Consolidar y desafiar",
                consolidate: "Consolidar y desafiar",
              }[need.stage],
          )
          .filter(Boolean)
          .join(", "),
    );
  if (numeric(discovery.candidate_count))
    meta.push(
      format(discovery.candidate_count) +
        (Number(discovery.candidate_count) === 1
          ? " dificultad online guardada"
          : " dificultades online guardadas"),
    );
  if (
    discovery.last_updated &&
    Number.isFinite(new Date(discovery.last_updated).getTime())
  )
    meta.push(
      "Última búsqueda: " + dateFormat.format(new Date(discovery.last_updated)),
    );
  if (
    !needs.length &&
    !reserve.some((item) => item.missing > 0) &&
    discovery.next_update &&
    discovery.state !== "paused" &&
    Number.isFinite(new Date(discovery.next_update).getTime())
  )
    meta.push("Próxima: " + dateFormat.format(new Date(discovery.next_update)));
  meta.push(
    settingValue(state, "discovery_enabled", discovery.automatic !== false) ===
      false
      ? "Búsqueda automática desactivada en Configuración"
      : discovery.automatic
        ? "Búsqueda automática mientras el entrenador está abierto"
        : "Cada " +
          (numeric(discovery.interval_hours)
            ? format(discovery.interval_hours)
            : "24") +
          " horas mientras el entrenador está abierto",
  );
  text("discovery-meta", meta.join(" · "));
}
function renderQuestCompletions(state, automatic) {
  const completionState = state.quest_completions;
  const items = Array.isArray(completionState?.items)
    ? completionState.items
        .filter(
          (quest) =>
            quest && quest.id && quest.map && quest.status !== "skipped",
        )
        .slice(0, 30)
    : [];
  const total = Math.max(0, Number(completionState?.total) || 0);
  if (automatic && completionState) {
    const profile =
      state.quest_board?.profile_label ||
      state.profile_label ||
      "Perfil de práctica";
    if (profile === observedCompletionProfile) {
      const fresh = items.filter(
        (quest) => !observedCompletionIds.has(quest.id),
      );
      if (fresh.length)
        tell(
          fresh.length === 1
            ? "Misión completada: " +
                (fresh[0].map.title || "objetivo alcanzado") +
                ". Logro guardado."
            : format(fresh.length) + " misiones completadas. Logros guardados.",
        );
    } else {
      observedCompletionProfile = profile;
      observedCompletionIds = new Set();
    }
    items.forEach((quest) => observedCompletionIds.add(quest.id));
  } else {
    observedCompletionProfile = null;
    observedCompletionIds = new Set();
  }
  $("quest-completed-total").hidden = !automatic;
  text(
    "quest-completed-total",
    format(total) +
      (total === 1
        ? " misión completada con este perfil"
        : " misiones completadas con este perfil"),
  );
  $("quest-completions").hidden = !automatic || !items.length;
  text(
    "quest-completions-label",
    "Misiones completadas (" + format(total) + ")",
  );
  text(
    "quest-completions-note",
    (total > items.length
      ? "Mostrando las últimas " + format(items.length) + ". "
      : "") +
      "Tus logros se conservan al renovar misiones y al recalibrar. Abrí un mapa para ver su meta y el resultado que la cumplió.",
  );
  const root = $("quest-completions-list");
  const openIds = new Set(
    Array.from(
      root.querySelectorAll("details[data-quest-id][open]"),
      (details) => details.dataset.questId,
    ),
  );
  root.replaceChildren();
  items.forEach((quest) => {
    const item = element("li", "quest-achievement");
    const details = element("details");
    details.dataset.questId = quest.id;
    details.open = openIds.has(String(quest.id));
    const summary = element("summary");
    const map = quest.map;
    summary.append(
      element(
        "span",
        "quest-achievement-title",
        map.title || "Mapa sin título",
      ),
    );
    const meta = [
      map.version,
      quest.stage_label,
      quest.completed_at &&
      Number.isFinite(new Date(quest.completed_at).getTime())
        ? coachDate(quest.completed_at)
        : "Fecha sin datos",
    ];
    const attempts = Math.max(0, Number(quest.attempt_count) || 0);
    if (attempts)
      meta.push(format(attempts) + (attempts === 1 ? " intento" : " intentos"));
    summary.append(
      element(
        "span",
        "quest-achievement-meta",
        meta.filter(Boolean).join(" · "),
      ),
    );
    details.append(summary, questGoal(map, quest));
    const url = safeMapURL(map.url);
    if (url) {
      const link = element("a", "map-link", "Ver mapa ↗");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute(
        "aria-label",
        "Ver " + (map.title || "mapa") + " en osu! (abre otra pestaña)",
      );
      details.append(link);
    }
    item.append(details);
    root.append(item);
  });
}
function renderSongBans(state) {
  const items = state.song_bans?.items || [];
  text("song-bans-count", items.length ? " · " + format(items.length) : "");
  const root = $("song-bans-list");
  const signature = JSON.stringify(items);
  if (root.dataset.signature === signature) return;
  root.dataset.signature = signature;
  root.replaceChildren();
  if (!items.length)
    root.append(
      element(
        "li",
        "song-bans-empty",
        "Todavía no excluiste canciones. Podés hacerlo desde cualquier misión.",
      ),
    );
  for (const song of items) {
    const row = element("li", "song-ban-item");
    const label = element("div");
    label.append(
      element("strong", "", song.title),
      element("span", "", song.artist),
    );
    const restore = element(
      "button",
      "subtle-button song-preference-button",
      "Volver a permitir",
    );
    restore.type = "button";
    restore.setAttribute("aria-label", "Volver a recomendar " + song.title);
    restore.addEventListener("click", async () => {
      await action(
        "/api/songs/unban",
        { id: song.id },
        "Canción permitida. Puede volver a aparecer si cumple tus criterios de práctica.",
      );
      if (!restore.isConnected)
        $("song-bans-panel").querySelector("summary").focus();
    });
    row.append(label, restore);
    root.append(row);
  }
}
function renderQuestSkips(state) {
  const ledger = state.quest_skips;
  const items = Array.isArray(ledger?.items)
    ? ledger.items
        .filter((quest) => quest && quest.id && quest.map)
        .slice(0, 30)
    : [];
  const total = numeric(ledger?.total)
    ? Math.max(0, Number(ledger.total))
    : Math.max(0, Number(state.quest_board?.skipped_count) || 0);
  $("quest-skips").hidden = !total;
  text(
    "quest-skips-label",
    format(total) + (total === 1 ? " misión retirada" : " misiones retiradas"),
  );
  const root = $("quest-skips-list");
  root.replaceChildren();
  items.forEach((quest) => {
    const row = element("li");
    const title = quest.map.title || "Mapa sin título";
    row.append(
      element(
        "span",
        "quest-skip-name",
        title + (quest.map.version ? " [" + quest.map.version + "]" : ""),
      ),
    );
    row.append(
      element(
        "span",
        "",
        quest.skipped_at &&
          Number.isFinite(new Date(quest.skipped_at).getTime())
          ? ({
              song_banned: "Canción excluida por vos",
              download_quality: "Ya no cumple los filtros de descarga",
              preferences_changed: "Cambiaste las preferencias de recomendaciones",
              training_updated: "Nueva progresión del coach",
            }[quest.skipped_reason] || "Dificultad ya jugada") +
              " · " +
              coachDate(quest.skipped_at)
          : "Misión retirada",
      ),
    );
    root.append(row);
  });
  if (!items.length && total)
    root.append(
      element(
        "li",
        "",
        "Las misiones retiradas conservan su historial y no cuentan como logros.",
      ),
    );
}
function renderQuestOverview(state) {
  const supported = Object.prototype.hasOwnProperty.call(state, "quest_board");
  const board = state.quest_board;
  const automatic =
    board?.automatic_refresh === true ||
    (!board && Boolean(state.quest_completions));
  const completedQuests = (Array.isArray(board?.groups) ? board.groups : [])
    .flatMap((group) => (Array.isArray(group.quests) ? group.quests : []))
    .filter((quest) => quest && quest.id && quest.status === "completed");
  if (!automatic && board?.id && board.id === observedQuestBoardId) {
    const newlyCompleted = completedQuests.filter(
      (quest) => !observedCompletedQuests.has(quest.id),
    );
    if (newlyCompleted.length)
      tell(
        newlyCompleted.length === 1
          ? "Misión completada: " +
              (newlyCompleted[0].map?.title || "objetivo alcanzado") +
              "."
          : newlyCompleted.length + " misiones completadas.",
      );
  }
  observedQuestBoardId = board?.id || null;
  observedCompletedQuests = new Set(completedQuests.map((quest) => quest.id));
  renderQuestCompletions(state, automatic);
  renderQuestSkips(state);
  $("quest-overview").hidden = !supported;
  text("progression-heading", supported ? "Misiones" : "Tu próxima sesión");
  text(
    "progression-caption",
    automatic
      ? "Completá una misión: el logro queda guardado y aparece otra para seguir practicando."
      : supported
        ? "Metas que conservás hasta completar o renovar tu tanda."
        : "Empezá cómodo y subí la exigencia de a poco.",
  );
  const order = supported
    ? "Las misiones son opcionales. Podés empezar por cualquier grupo según cómo te sientas y reintentar las pendientes."
    : "Jugá 1 mapa para entrar en ritmo y después 2 o 3 de práctica principal, variando canciones. Mirá la meta de cada tarjeta antes de empezar.";
  $("session-order-description").replaceChildren(
    element(
      "strong",
      "",
      supported ? "Elegí cómo practicar. " : "Orden sugerido. ",
    ),
    document.createTextNode(order),
  );
  const total = Math.max(0, Number(board?.total_count) || 0);
  const completed = Math.max(
    0,
    Math.min(total, Number(board?.completed_count) || 0),
  );
  const allCompleted = total > 0 && Boolean(board?.all_completed);
  const quests = (Array.isArray(board?.groups) ? board.groups : [])
    .flatMap((group) => (Array.isArray(group.quests) ? group.quests : []))
    .filter((quest) => quest && quest.map);
  const active = numeric(board?.active_count)
    ? Math.max(0, Number(board.active_count))
    : quests.filter((quest) => !["completed", "skipped"].includes(quest.status))
        .length;
  const waiting = numeric(board?.waiting_count)
    ? Math.max(0, Number(board.waiting_count))
    : completedQuests.length;
  text(
    "quest-progress-count",
    automatic ? format(active) : format(completed) + "/" + format(total),
  );
  text(
    "quest-progress-label",
    automatic
      ? active === 1
        ? "misión disponible"
        : "misiones disponibles"
      : allCompleted
        ? "tanda completada"
        : "misiones completadas",
  );
  $("quest-progress").hidden = automatic;
  $("quest-progress").setAttribute("aria-valuemax", String(Math.max(1, total)));
  $("quest-progress").setAttribute("aria-valuenow", String(completed));
  $("quest-progress-fill").style.width =
    (total ? (completed / total) * 100 : 0) + "%";
  const unplayed = state.recommendation_policy?.mode === "unplayed";
  const search = automaticSearchNotice(state, null, true);
  const emptyBoardNote =
    unplayed && Number(state.recommendation_policy.history_plays) > 0
      ? search
        ? search.title + ". " + search.message
        : "No quedan dificultades sin jugar adecuadas para preparar misiones. Buscá mapas nuevos o agregá otros a tu biblioteca."
      : automatic
        ? "Jugá tu primera partida para preparar misiones para tu perfil."
        : "Jugá tu primera partida para preparar misiones para tu perfil. También podés pedir una tanda cuando tu perfil ya esté identificado.";
  text(
    "quest-board-note",
    automatic
      ? !board
        ? emptyBoardNote
        : "Cada partida nueva se verifica automáticamente. Cumplí todos los requisitos en el mismo intento; las otras misiones conservan sus metas." +
          (waiting
            ? " " +
              format(waiting) +
              (waiting === 1
                ? " misión completada espera otro mapa adecuado."
                : " misiones completadas esperan otros mapas adecuados.") +
              (search ? " " + search.message : "")
            : !active
              ? search
                ? " " + search.message
                : " Buscá mapas nuevos o actualizá tu biblioteca para tener más opciones."
              : "")
      : !board
        ? emptyBoardNote
        : !total
          ? "Esta tanda todavía tiene pocos mapas disponibles. Actualizá tu biblioteca y pedí una nueva tanda."
          : allCompleted
            ? "Completaste todas las misiones. Podés pedir una nueva tanda con tu perfil actual."
            : "Cada partida nueva se verifica automáticamente. Cumplí todos los requisitos de una misión en el mismo intento.",
  );
  const skippedWaiting = Math.max(0, Number(board?.skipped_waiting_count) || 0);
  $("quest-selection-policy").hidden = !unplayed;
  text(
    "quest-selection-policy",
    unplayed
      ? (state.recommendation_policy.message ||
          "Las nuevas misiones evitan dificultades que ya jugaste. Pueden incluir otras dificultades de la misma canción. Las misiones en práctica conservan sus metas.") +
          (skippedWaiting
            ? " Falta encontrar " +
              format(skippedWaiting) +
              (skippedWaiting === 1
                ? " dificultad sin jugar adecuada para otra misión."
                : " dificultades sin jugar adecuadas para otras misiones.")
            : "")
      : "",
  );
  text(
    "quest-new-button",
    automatic ? "Renovar misiones pendientes" : "Nueva tanda de misiones",
  );
  text(
    "quest-replace-note",
    automatic
      ? "Opcional: cambia todas las pendientes y conserva tus logros. Cada misión completada ya se renueva sola para que puedas seguir avanzando."
      : "Guarda el avance de esta tanda y reemplaza las pendientes por misiones nuevas.",
  );
  $("quest-renewal-help").replaceChildren(
    element(
      "strong",
      "",
      automatic ? "Renovar las misiones. " : "Renovar la tanda. ",
    ),
    document.createTextNode(
      automatic
        ? "Al completar una misión, el logro queda guardado y se asigna otra automáticamente con tu perfil actual. Las demás misiones conservan sus metas. Si falta un mapa adecuado, la misión completada queda a la espera." +
            (state.discovery?.automatic
              ? " El coach busca dificultades sin jugar y completa los lugares disponibles cuando encuentra opciones adecuadas; el panel muestra el próximo intento si la búsqueda necesita esperar."
              : "") +
            " «Renovar misiones pendientes» permite cambiar todas las pendientes cuando quieras, conservando tus logros; la renovación individual permite seguir avanzando sin usar ese botón. El historial muestra tus últimas 30 misiones completadas, también después de recalibrar." +
            (unplayed
              ? " Las misiones nuevas evitan la dificultad exacta que ya jugaste, usando todo el registro de este perfil, incluso fuera de los " +
                format(settingValue(state, "reference_plays", 100)) +
                " resultados de la memoria. El coach reconoce cada dificultad por su ID o por la huella del archivo. Otras dificultades de la misma canción pueden aparecer si son adecuadas para tu práctica. Una misión pendiente sin intentos se retira si esa dificultad ya estaba jugada antes de asignarse; la omisión queda registrada y conserva el contador de logros. Las misiones que ya empezaste mantienen sus metas."
              : "")
        : "«Nueva tanda de misiones» guarda el avance anterior y reemplaza las pendientes con objetivos calculados a partir de tu perfil actual. El historial muestra tus últimas 5 tandas anteriores.",
    ),
  );
  const boardMeta = [];
  if (
    !automatic &&
    board?.created_at &&
    Number.isFinite(new Date(board.created_at).getTime())
  )
    boardMeta.push(
      "Tanda del " + dateFormat.format(new Date(board.created_at)),
    );
  if (board?.profile_label) boardMeta.push(board.profile_label);
  text("quest-board-meta", boardMeta.join(" · "));
  const history = Array.isArray(state.quest_history)
    ? state.quest_history.filter((item) => item && item.id).slice(0, 5)
    : [];
  $("quest-history").hidden = !history.length;
  text(
    "quest-history-label",
    (automatic ? "Renovaciones manuales anteriores" : "Tandas anteriores") +
      " (" +
      history.length +
      ")",
  );
  const historyRoot = $("quest-history-list");
  historyRoot.replaceChildren();
  history.forEach((item) => {
    const row = element("li");
    const date =
      item.created_at && Number.isFinite(new Date(item.created_at).getTime())
        ? dateFormat.format(new Date(item.created_at))
        : "Fecha sin datos";
    row.append(
      element(
        "span",
        "",
        (automatic ? "Misiones asignadas el " : "Tanda del ") + date,
      ),
      element(
        "b",
        "",
        format(item.completed_count, "0") +
          (automatic
            ? " completadas"
            : "/" + format(item.total_count, "0") + " completadas"),
      ),
    );
    historyRoot.append(row);
  });
}
function renderQuestBoard(state) {
  const board = state.quest_board;
  const automatic = board.automatic_refresh === true;
  const root = $("recommendations");
  root.replaceChildren();
  root.setAttribute("aria-busy", "false");
  text(
    "session-stage-message",
    automatic
      ? "Al completar una misión, se guarda tu logro y se busca otra con tu perfil actualizado. Las pendientes mantienen su mapa y su meta. Podés elegir cualquier grupo."
      : "Los mapas y las metas de esta tanda permanecen fijos. Tu perfil sigue actualizándose con tus partidas; «Nueva tanda de misiones» usará ese nivel actualizado.",
  );
  const groups = Array.isArray(board.groups) ? board.groups : [];
  groups.forEach((group, index) => {
    const stage = element("div", "stage");
    stage.dataset.stage = ["warmup", "practice", "challenge"].includes(
      group.stage,
    )
      ? group.stage
      : "challenge";
    const top = element("div", "stage-top");
    const title = element("div");
    const stageLabels = {
      warmup: "Entrar en ritmo",
      practice: "Práctica principal",
      challenge: "Consolidar",
      consolidate: "Consolidar",
    };
    title.append(
      element(
        "h3",
        "",
        automatic
          ? stageLabels[group.stage] || group.label || "Práctica"
          : group.label || "Práctica",
      ),
    );
    if (!automatic && numeric(group.target))
      title.append(
        element(
          "p",
          "",
          "Referencia de la tanda: " + format(group.target) + " ★",
        ),
      );
    top.append(
      element("span", "step-number", String(index + 1).padStart(2, "0")),
      title,
    );
    stage.append(top);
    const descriptions = {
      warmup: "Mapas cómodos para empezar o recuperar el ritmo.",
      practice:
        "Elegí un mapa y practicá hasta alcanzar la meta de su tarjeta.",
      challenge:
        "Cada tarjeta indica si busca consolidar un resultado o probar un pequeño desafío.",
      consolidate:
        "Cada tarjeta indica si busca consolidar un resultado o probar un pequeño desafío.",
    };
    const description = automatic
      ? descriptions[group.stage] || group.description
      : group.description;
    if (description)
      stage.append(element("p", "stage-description", description));
    const quests = Array.isArray(group.quests)
      ? group.quests.filter(
          (quest) => quest && quest.map && quest.status !== "skipped",
        )
      : [];
    const completed = quests.filter(
      (quest) => quest.status === "completed",
    ).length;
    const active = quests.length - completed;
    stage.append(
      element(
        "p",
        "quest-stage-count",
        automatic
          ? format(active) +
              (active === 1 ? " misión disponible" : " misiones disponibles") +
              (completed
                ? " · " +
                  format(completed) +
                  (completed === 1
                    ? " espera otro mapa"
                    : " esperan otros mapas")
                : "")
          : completed + "/" + quests.length + " misiones completadas",
      ),
    );
    const search = automaticSearchNotice(state, group.stage, !quests.length);
    if (search) stage.append(searchNoticeElement(search, quests.length > 0));
    if (!quests.length && !search) {
      const empty = element("div", "empty-stage");
      const unplayed =
        state.recommendation_policy?.mode === "unplayed" ||
        group.empty_reason === "no_unplayed_maps_in_range";
      empty.append(
        element(
          "strong",
          "",
          unplayed
            ? "Faltan dificultades sin jugar en esta etapa"
            : "Esta etapa quedó sin misiones",
        ),
        document.createTextNode(
          unplayed
            ? "No quedan dificultades sin jugar adecuadas para esta etapa. Buscá mapas nuevos o agregá otros a tu biblioteca."
            : automatic
              ? "Podés practicar en otro grupo. Después de sumar mapas a tu biblioteca, usá «Renovar misiones pendientes» para buscar opciones en esta etapa."
              : "Podés practicar en otro grupo. Una nueva tanda volverá a buscar mapas para este rango.",
        ),
      );
      stage.append(empty);
    } else if (quests.length) {
      quests.forEach((quest) =>
        stage.append(
          mapCard(
            quest.map,
            quest,
            state.quest_availability?.[quest.id],
            automatic,
          ),
        ),
      );
    }
    root.append(stage);
  });
  if (!groups.length && state.discovery?.automatic)
    root.append(searchNoticeElement(automaticSearchNotice(state, null, true)));
  else if (!groups.length)
    root.append(
      element(
        "p",
        "empty-stage quest-empty",
        state.recommendation_policy?.mode === "unplayed"
          ? "No quedan dificultades sin jugar adecuadas para tu práctica. Buscá mapas nuevos o agregá otros a tu biblioteca."
          : automatic
            ? "Buscá mapas nuevos o actualizá tu biblioteca y después usá «Renovar misiones pendientes» para preparar opciones para tu perfil."
            : "Pedí una nueva tanda para preparar misiones con los mapas disponibles.",
      ),
    );
}
function renderRecommendations(state) {
  renderDiscovery(state);
  renderQuestOverview(state);
  if (state.quest_board && typeof state.quest_board === "object") {
    renderQuestBoard(state);
    return;
  }
  const profile = state.profile || {};
  const calibrated = profile.phase === "training";
  const playerProgression = state.player_profile?.progression || {};
  const profileBlocksChallenge =
    playerProgression.mode === "recover" &&
    playerProgression.allow_challenge === false;
  const challenge =
    Boolean(profile.challenge_unlocked) && !profileBlocksChallenge;
  text(
    "session-stage-message",
    challenge
      ? "Pequeño desafío está habilitado. Elegí uno si la práctica principal salió cómoda."
      : playerProgression.mode === "recover"
        ? "Tu perfil recomienda recuperar control antes de probar un desafío. Seguí las metas personales en mapas manejables."
        : calibrated
          ? "Ya completaste la calibración. Consolidar te propone otro mapa de tu rango para repetir un resultado controlado; el desafío aparecerá al cumplir sus condiciones."
          : "Mientras calibrás, usá Consolidar para probar otra canción de tu rango. Variar mapas ayuda a conocer tu nivel actual.",
  );
  const root = $("recommendations");
  root.replaceChildren();
  root.setAttribute("aria-busy", String(Boolean(state.scanning)));
  const stages = [
    {
      stage: "warmup",
      label: "Entrar en ritmo",
      description: "Elegí 1 mapa para empezar cómodo.",
    },
    {
      stage: "practice",
      label: "Práctica principal",
      description: "Jugá 2 o 3 mapas y seguí sus metas personales.",
    },
    {
      stage: "challenge",
      label: challenge ? "Pequeño desafío" : "Consolidar",
      description: challenge
        ? "Probalo si la práctica principal salió cómoda."
        : calibrated
          ? "Repetí un resultado controlado en otro mapa de tu rango."
          : "Sumá otra canción para completar la calibración.",
    },
  ];
  stages.forEach((fallback, index) => {
    const rec =
      (state.recommendations || []).find(
        (item) => item.stage === fallback.stage,
      ) || fallback;
    const stage = element("div", "stage");
    stage.dataset.stage = fallback.stage;
    const top = element("div", "stage-top");
    const title = element("div");
    title.append(
      element(
        "h3",
        "",
        fallback.stage === "challenge" &&
          profileBlocksChallenge &&
          rec.label === "Pequeño desafío"
          ? fallback.label
          : rec.label || fallback.label,
      ),
    );
    if (numeric(rec.target))
      title.append(
        element("p", "", "Referencia: " + format(rec.target) + " ★"),
      );
    top.append(
      element("span", "step-number", String(index + 1).padStart(2, "0")),
      title,
    );
    stage.append(
      top,
      element(
        "p",
        "stage-description",
        rec.description || fallback.description,
      ),
    );
    if (fallback.stage === "challenge" && !challenge) {
      if (profileBlocksChallenge) {
        const profileReasons = Array.isArray(playerProgression.reasons)
          ? playerProgression.reasons.filter(
              (reason) => typeof reason === "string" && reason.trim(),
            )
          : [];
        stage.append(
          element(
            "p",
            "stage-description",
            "Según tu perfil: " +
              (profileReasons.join(" ") ||
                "Conviene afianzar " +
                  (playerProgression.focus_label || "tu rendimiento actual") +
                  " antes de subir la exigencia."),
          ),
        );
      }
      const missing = [];
      if (!calibrated) {
        const plays = Math.max(
          0,
          Number(
            settingValue(
              state,
              "calibration_plays",
              profile.calibration_needed || 5,
            ),
          ) - (Number(profile.attempts) || 0),
        );
        const maps = Math.max(
          0,
          Number(settingValue(state, "calibration_maps", 3)) -
            (Number(profile.distinct_maps) || 0),
        );
        if (plays)
          missing.push(plays + (plays === 1 ? " partida" : " partidas"));
        if (maps)
          missing.push(
            maps + (maps === 1 ? " mapa distinto" : " mapas distintos"),
          );
        if (missing.length)
          stage.append(
            element(
              "p",
              "stage-description",
              "Para calibrar, necesitás " + missing.join(" y ") + " más.",
            ),
          );
      } else {
        const count = Number(settingValue(state, "challenge_maps", 3));
        const minimumAccuracy = Number(
          settingValue(state, "challenge_accuracy", 94),
        );
        const maximumMisses = Number(
          settingValue(state, "challenge_miss_percent", 2),
        );
        const latest = Array.isArray(state.recent)
          ? state.recent.slice(0, count)
          : [];
        if (latest.length < count || latest.some((play) => !play.passed))
          missing.push("Completá las últimas " + format(count) + " partidas.");
        const keys = new Set(
          latest
            .map((play) => play.beatmap_key ?? play.beatmap_id ?? play.key)
            .filter((key) => key !== null && key !== undefined && key !== "")
            .map(String),
        );
        if (keys.size < count)
          missing.push(
            "Las últimas " +
              format(count) +
              " deben ser en " +
              format(count) +
              " mapas distintos.",
          );
        if (
          latest.some(
            (play) =>
              !numeric(play.accuracy) ||
              Number(play.accuracy) < minimumAccuracy,
          )
        )
          missing.push(
            "Buscá al menos " +
              format(minimumAccuracy) +
              " % de precisión en cada una.",
          );
        if (
          latest.some(
            (play) =>
              Number(play.misses) /
                Math.max(
                  1,
                  Number(play.judged_objects) || Number(play.object_count) || 1,
                ) >
              maximumMisses / 100,
          )
        )
          missing.push(
            "Reducí los misses a un máximo del " +
              format(maximumMisses) +
              " % en cada una.",
          );
        if (missing.length)
          stage.append(
            element(
              "p",
              "stage-description",
              "Para habilitar el desafío: " + missing.join(" "),
            ),
          );
      }
    }
    if (Array.isArray(rec.maps) && rec.maps.length) {
      rec.maps.forEach((map) => stage.append(mapCard(map)));
    } else {
      stage.append(emptyRange(rec, state));
    }
    root.append(stage);
  });
}
function renderRecent(state) {
  const rows = Array.isArray(state.recent)
    ? state.recent.slice(0, Number(settingValue(state, "session_plays", 20)))
    : [];
  $("history-table").hidden = !rows.length;
  $("history-empty").hidden = Boolean(rows.length);
  text(
    "recent-caption",
    state.profile?.session
      ? "Hasta " +
          format(settingValue(state, "session_plays", 20)) +
          " partidas de los últimos " +
          format(settingValue(state, "session_days", 7)) +
          " días. La memoria de la referencia conserva hasta " +
          format(settingValue(state, "reference_plays", 100)) +
          " en " +
          format(settingValue(state, "reference_days", 30)) +
          " días; el registro completo sigue guardado."
      : rows.length
        ? "Resultados registrados en este perfil de práctica."
        : "Tu progreso empieza con esta sesión.",
  );
  const root = $("history-body");
  root.replaceChildren();
  rows.forEach((play) => {
    const row = element("tr");
    const map = element("td");
    map.append(
      element("div", "table-title", play.title || "Mapa sin título"),
      element("div", "table-version", play.version || ""),
    );
    row.append(
      map,
      element("td", "numeric", format(play.stars) + " ★"),
      element("td", "numeric", accuracy(play.accuracy)),
      element("td", "numeric", format(play.misses)),
    );
    const resultCell = element("td");
    resultCell.append(
      element(
        "span",
        play.passed ? "result" : "result failed",
        play.passed ? "Completado" : "Sin completar",
      ),
    );
    const date = new Date(play.played_at);
    const dateCell = element(
      "td",
      "numeric",
      play.played_at && Number.isFinite(date.getTime())
        ? dateFormat.format(date)
        : "Sin fecha",
    );
    row.append(resultCell, dateCell);
    root.append(row);
  });
}
function renderPending(state) {
  const pending = state.pending || [];
  $("pending-section").hidden = !pending.length;
  text(
    "pending-heading",
    pending.length === 1
      ? "Una partida por confirmar"
      : pending.length + " partidas por confirmar",
  );
  const root = $("pending-list");
  root.replaceChildren();
  pending.forEach((play) => {
    const item = element("article", "pending-item");
    const info = element("div");
    info.append(
      element("strong", "", play.title || "Mapa sin título"),
      element(
        "p",
        "",
        accuracy(play.accuracy) +
          " de precisión · " +
          format(play.misses) +
          " fallos · " +
          format(play.stars) +
          " ★",
      ),
    );
    if (play.reason) info.append(element("p", "", play.reason));
    const buttons = element("div", "pending-actions");
    for (const [accept, label] of [
      [true, "Sí, la acabo de jugar"],
      [false, "Es un resultado anterior"],
    ]) {
      const button = element("button", "", label);
      button.type = "button";
      button.addEventListener("click", () =>
        action(
          "/api/confirm",
          { id: play.id, accept },
          accept
            ? "Partida incorporada a tu progreso."
            : "Resultado anterior descartado.",
        ),
      );
      buttons.append(button);
    }
    item.append(info, buttons);
    root.append(item);
  });
}
function render(state) {
  renderSettings(state);
  renderSongBans(state);
  renderSettingsHelp(state);
  const p = state.profile || {};
  const hasSession = p.session && typeof p.session === "object";
  const session = hasSession ? p.session : p;
  const attempts = Math.max(0, Number(p.attempts) || 0);
  const distinct = Math.max(0, Number(p.distinct_maps) || 0);
  const needed = Math.max(
    1,
    Number(settingValue(state, "calibration_plays", p.calibration_needed || 5)),
  );
  const calibrated = p.phase === "training";
  $("demo-banner").hidden = !state.demo;
  text("mode-label", state.mode_label || "osu!standard");
  text("baseline-label", p.training_level ? "Nivel de práctica" : "Referencia para entrenar");
  text("baseline", format(p.training_level?.stars ?? p.baseline, "Por calibrar"));
  $("baseline").style.fontSize = numeric(p.baseline) ? "" : "26px";
  text("level-unit", numeric(p.baseline) ? "estrellas" : "");
  const baselineDescription = attempts
    ? calibrated
      ? hasSession
        ? "Referencia con memoria: hasta " +
          format(settingValue(state, "reference_plays", 100)) +
          " partidas en " +
          format(settingValue(state, "reference_days", 30)) +
          " días, con más peso para las recientes."
        : "Referencia ajustada a tu rendimiento reciente."
      : "Referencia provisional mientras calibrás."
    : "Punto de partida provisional para tu primera sesión.";
  text("baseline-description", baselineDescription);
  text("profile-label", state.profile_label || "Perfil de práctica");
  text("focus", p.focus || "Encontrar tu ritmo");
  text(
    "profile-message",
    p.message || "Jugá distintos mapas para orientar la práctica.",
  );
  const trend = hasSession
    ? {
        up: "Sesión en mejora",
        down: "Ajustando la exigencia de la sesión",
        steady: "Sesión estable",
      }[session.trend] || "Reuniendo resultados de la sesión"
    : !calibrated
      ? "Referencia provisional"
      : {
          up: "Tendencia en mejora",
          down: "Ajustando la exigencia",
          steady: "Rendimiento estable",
        }[p.trend] || "Calibrando tu referencia";
  text(
    "profile-metrics",
    numeric(session.accuracy)
      ? accuracy(session.accuracy) +
          (hasSession
            ? " de precisión en la sesión · "
            : " de precisión reciente · ") +
          trend
      : "La precisión aparecerá con tus partidas",
  );
  $("session-window-note").hidden = !hasSession;
  text(
    "session-window-note",
    hasSession
      ? playerEvidence(session.attempts, session.distinct_maps) +
          ". Hasta " +
          format(
            session.window_plays,
            format(settingValue(state, "session_plays", 20)),
          ) +
          " partidas de los últimos " +
          format(
            session.window_days,
            format(settingValue(state, "session_days", 7)),
          ) +
          " días."
      : "",
  );
  text(
    "calibration-label",
    calibrated ? "Referencia calibrada" : "Calibración inicial",
  );
  text("calibration-count", Math.min(attempts, needed) + "/" + needed);
  $("calibration-progress").setAttribute("aria-valuemax", String(needed));
  $("calibration-progress").setAttribute(
    "aria-valuenow",
    String(Math.min(attempts, needed)),
  );
  $("progress-fill").style.width =
    Math.min(100, (attempts / needed) * 100) + "%";
  text(
    "calibration-message",
    calibrated
      ? hasSession
        ? playerEvidence(attempts, distinct) +
          " en la memoria de la referencia. La calibración está completa."
        : attempts + " partidas recientes ayudan a ajustar tu próxima sesión."
      : "Jugá " +
          needed +
          " partidas en al menos " +
          format(settingValue(state, "calibration_maps", 3)) +
          " mapas distintos. Llevás " +
          distinct +
          (distinct === 1 ? " mapa." : " mapas."),
  );
  text(
    "catalog-count",
    state.scanning
      ? "Leyendo mapas: " + format(state.scan_count, "0")
      : format(state.catalog_count, "0") + " mapas disponibles",
  );
  text(
    "rescan-button",
    state.scanning ? "Leyendo mapas…" : "Volver a leer mapas",
  );
  text(
    "footer-status",
    state.demo
      ? "Datos de ejemplo · demostración"
      : hasSession
        ? "Referencia: " +
          format(settingValue(state, "reference_plays", 100)) +
          " partidas / " +
          format(settingValue(state, "reference_days", 30)) +
          " días · Sesión: " +
          format(settingValue(state, "session_plays", 20)) +
          " / " +
          format(settingValue(state, "session_days", 7)) +
          " días"
        : "Últimos " +
          format(settingValue(state, "session_days", 7)) +
          " días · Hasta " +
          format(settingValue(state, "session_plays", 20)) +
          " partidas por perfil",
  );
  const warnings = Array.isArray(state.warnings) ? state.warnings : [];
  $("warnings").hidden = !warnings.length;
  $("warnings").replaceChildren(
    ...warnings.map((warning) => element("p", "", warning)),
  );
  renderTagAnalysis(state);
  preserveMissionInteraction(() => renderRecommendations(state));
  renderCompactProfile(state);
  renderRecent(state);
  renderPending(state);
}
async function refresh() {
  if (stopped) return;
  try {
    const state = await request("/api/state");
    if (stopped) return;
    if (
      !state ||
      typeof state !== "object" ||
      !state.profile ||
      typeof state.token !== "string"
    )
      throw new Error("Respuesta incompleta");
    currentState = state;
    online = true;
    text(
      "connection-user",
      state.demo
        ? "Modo de demostración"
        : state.connection?.ok
          ? "Conectado como " +
            (state.profile_label?.split(" · ")[0] || "jugador")
          : "Esperando osu! y tosu",
    );
    $("initial-error").hidden = true;
    $("connection").dataset.offline = "false";
    $("connection").dataset.ok = String(Boolean(state.connection?.ok));
    text("connection-text", state.connection?.message || "Script conectado");
    const snapshot = JSON.stringify(state);
    if (snapshot !== currentRender) {
      currentRender = snapshot;
      render(state);
    }
    setAvailability();
  } catch {
    if (stopped) return;
    online = false;
    $("connection").dataset.offline = "true";
    $("connection").dataset.ok = "false";
    text("connection-text", "Sin conexión · reintentando…");
    text("connection-user", "Sin conexión · reintentando…");
    $("initial-error").hidden = false;
    $("recommendations").setAttribute("aria-busy", "false");
    setAvailability();
  }
}
async function poll() {
  if (stopped) return;
  await refresh();
  if (!stopped) setTimeout(poll, 3000);
}
async function stopCoach() {
  if (busyAction || !online || stopped) return;
  busyAction = true;
  setAvailability();
  try {
    await request("/api/stop", {});
    stopped = true;
    online = false;
    $("closed-banner").hidden = false;
    $("initial-error").hidden = true;
    $("connection").dataset.ok = "false";
    $("connection").dataset.offline = "false";
    text("connection-text", "Entrenador cerrado");
    text("connection-user", "Entrenador cerrado");
    text("footer-status", "Seguimiento detenido");
    $("recommendations").setAttribute("aria-busy", "false");
    $("closed-banner").scrollIntoView({ block: "center", behavior: "instant" });
  } catch (error) {
    tell(
      error.name === "AbortError"
        ? "El script tardó en responder. Revisá la conexión e intentá otra vez."
        : error.message,
      true,
    );
  } finally {
    busyAction = false;
    setAvailability();
  }
}
$("settings-open").addEventListener("click", (event) => {
  event.preventDefault();
  selectView("settings");
  $("settings-panel").open = true;
  $("settings-section").scrollIntoView({ block: "start" });
  $("settings-heading").focus();
});
$("settings-form").addEventListener("input", (event) => {
  const field = Array.from(settingsFields.values()).find(
    (item) => item.input === event.target,
  );
  if (!field) return;
  field.input.removeAttribute("aria-invalid");
  field.error.hidden = true;
  settingsDirty = settingsHasEdits();
  setSettingsFeedback(
    settingsDirty
      ? "Tenés cambios sin guardar."
      : "Estos son tus ajustes guardados.",
  );
  updateSettingsAvailability();
});
$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveSettings();
});
$("settings-reset").addEventListener("click", () => saveSettings(true));
$("settings-discard").addEventListener("click", () => {
  applySettingsValues(currentState?.settings);
  setSettingsFeedback(
    "Cambios descartados. Se muestran los ajustes guardados.",
  );
  updateSettingsAvailability();
});
$("rescan-button").addEventListener("click", () =>
  action("/api/rescan", {}, "Actualizando tu biblioteca de mapas."),
);
$("tag-sync-button").addEventListener("click", () =>
  action("/api/tags/sync", {}, "Consultando los tags de tus mapas."),
);
$("discovery-sync-button").addEventListener("click", () =>
  action(
    "/api/discovery/sync",
    {},
    "Explorando más canciones del catálogo para tu nivel.",
  ),
);
$("quest-new-button").addEventListener("click", () =>
  action(
    "/api/quests/new",
    { board_id: currentState?.quest_board?.id ?? null },
    currentState?.quest_board?.automatic_refresh ||
      currentState?.quest_completions
      ? "Misiones pendientes renovadas. Tus logros siguen guardados."
      : "Nueva tanda de misiones preparada. El avance anterior quedó guardado.",
  ),
);
$("reset-button").addEventListener("click", () =>
  $("reset-dialog").showModal(),
);
$("cancel-reset").addEventListener("click", () => $("reset-dialog").close());
$("confirm-reset").addEventListener("click", () =>
  action(
    "/api/reset",
    {},
    "Nueva calibración iniciada. Tu historial se conserva.",
  ),
);
$("reset-dialog").addEventListener("cancel", (event) => {
  if (busyAction) event.preventDefault();
});
$("stop-button").addEventListener("click", stopCoach);
window.addEventListener("resize", () => {
  if (currentState?.coach_progress)
    renderCoachChart(currentState.coach_progress);
});

function uiIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.6",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    class: "ui-icon",
  }))
    svg.setAttribute(key, value);
  const shapes = {
    star: [
      [
        "path",
        {
          d: "m12 2 3.05 6.18 6.82.99-4.93 4.81 1.16 6.8L12 17.57l-6.1 3.21 1.17-6.8L2.14 9.17l6.81-.99Z",
          fill: "currentColor",
          stroke: "none",
        },
      ],
    ],
    target: [
      ["circle", { cx: 12, cy: 12, r: 10 }],
      ["circle", { cx: 12, cy: 12, r: 6.5 }],
      ["circle", { cx: 12, cy: 12, r: 3 }],
      ["path", { d: "m12 12 6-8" }],
    ],
    crosshair: [
      ["circle", { cx: 12, cy: 12, r: 8 }],
      ["path", { d: "M12 1v6m0 10v6M1 12h6m10 0h6" }],
    ],
    clock: [
      ["circle", { cx: 12, cy: 12, r: 10 }],
      ["path", { d: "M12 6v6l4 2" }],
    ],
    check: [
      ["circle", { cx: 12, cy: 12, r: 10 }],
      ["path", { d: "m6.5 12 3.5 3.5 7.5-8" }],
    ],
    refresh: [
      [
        "path",
        {
          d: "M20 7V3l-3 3A8 8 0 0 0 4 9M4 17v4l3-3a8 8 0 0 0 13-3M20 3h-4M4 21h4",
        },
      ],
    ],
    list: [
      ["path", { d: "M8 5h13M8 12h13M8 19h13" }],
      ["circle", { cx: 3, cy: 5, r: 1 }],
      ["circle", { cx: 3, cy: 12, r: 1 }],
      ["circle", { cx: 3, cy: 19, r: 1 }],
    ],
    search: [
      ["circle", { cx: 10, cy: 10, r: 7 }],
      ["path", { d: "m15 15 7 7" }],
    ],
    chevron: [["path", { d: "m8 5 7 7-7 7" }]],
  };
  for (const [tag, attributes] of shapes[name] || []) {
    const shape = document.createElementNS(svg.namespaceURI, tag);
    for (const [key, value] of Object.entries(attributes))
      shape.setAttribute(key, value);
    svg.append(shape);
  }
  return svg;
}
function setupTrainingLayout() {
  document
    .querySelectorAll("[data-icon]")
    .forEach((node) => node.replaceChildren(uiIcon(node.dataset.icon)));
  const mission = $("mission-section"),
    overview = $("quest-overview"),
    guide = mission.querySelector(".session-guide");
  guide.append(
    mission.querySelector(".catalog-meta"),
    mission.querySelector(".quest-explainer"),
  );
  const title = overview.querySelector(".quest-progress-title"),
    completed = $("quest-completed-total"),
    renew = $("quest-new-button");
  guide.append($("quest-progress"));
  overview.replaceChildren(
    uiIcon("list"),
    title,
    element("span", "toolbar-divider", "/"),
    completed,
    element("span", "toolbar-divider", "/"),
    renew,
  );
  renew.setAttribute("aria-label", "Renovar misiones pendientes");
  const toolbar = element("div", "mission-toolbar");
  mission.insertBefore(toolbar, overview);
  toolbar.append(overview, guide);
  const discovery = $("discovery-bar"),
    drawer = element("details", "discovery-drawer"),
    summary = element("summary");
  const label = element("span", "discovery-preview-label");
  label.append(
    element("strong", "", "Buscar mapas nuevos"),
    element(
      "small",
      "",
      "Explorá canciones que se adapten a tu entrenamiento.",
    ),
  );
  label.lastChild.id = "discovery-preview-message";
  const quality = element("span", "discovery-preview-quality");
  quality.id = "discovery-preview-quality";
  summary.append(uiIcon("search"), label, quality, uiIcon("chevron"));
  drawer.append(summary);
  discovery.before(drawer);
  drawer.append(discovery);
}

// Keep primary tasks visible; the original details and actions remain reachable.
function compactMapCard(card, map, quest) {
  card.dataset.mapKey = String(
    quest?.id ?? map.key ?? map.beatmap_id ?? map.title + "|" + map.version,
  );
  const details = element("details", "map-details");
  details.append(element("summary", "", "Ver objetivos y detalles"));
  const actions = card.querySelector(".map-actions");
  const originalCreator = card.querySelector(".map-creator")?.textContent || "";
  const creator = originalCreator.replace(/^Mapper:\s*/, "");
  // Full metadata, grade rules, attempt evidence and search alternatives stay reachable.
  for (const child of Array.from(card.children))
    if (child !== actions) details.append(child);
  const heading = element("div", "map-heading");
  const title = element("h3", "song-title", map.title || "Mapa sin título");
  title.title = map.title || "Mapa sin título";
  const difficulty = element(
    "span",
    "difficulty-badge",
    map.version || "Sin dificultad",
  );
  difficulty.title = "Dificultad: " + (map.version || "Sin datos");
  heading.append(title, difficulty);
  const byline = element("p", "map-byline");
  byline.append(element("span", "", map.artist || "Artista sin datos"));
  if (creator)
    byline.append(
      element("span", "byline-divider", "|"),
      element("span", "", "mapa de " + creator),
    );
  byline.title = [map.artist, creator ? "Mapa de " + creator : ""]
    .filter(Boolean)
    .join(" · ");
  const stats = element("div", "map-stats");
  if (map.mods_label) {
    const badge = element("span", "map-mods", map.mods_label);
    badge.title =
      "Mods requeridos. Las estrellas y la duración incluyen su efecto.";
    stats.append(badge);
  }
  const stars = element("span", "star-rating");
  stars.append(uiIcon("star"), document.createTextNode(format(map.stars)));
  stats.append(
    stars,
    element("span", "", format(map.bpm) + " BPM"),
    element("span", "", duration(map.length).padStart(5, "0")),
    element("span", "", "AR " + format(map.ar)),
  );
  const expected = map.expectation || {};
  const goal = element("div", "compact-goal");
  goal.append(element("strong", "", "Objetivo: "));
  const targets = [
    {
      key: expected.grade_min || expected.grade_label ? "grade" : "complete",
      label:
        expected.grade_label ||
        (expected.grade_min ? expected.grade_min + " o mejor" : "Completar"),
    },
  ];
  if (numeric(expected.accuracy_min))
    targets.push({
      key: "accuracy",
      label: "≥" + accuracy(expected.accuracy_min).replace(/\s+%/, "%"),
    });
  if (numeric(expected.misses_max))
    targets.push({
      key: "misses",
      label: "≤" + format(expected.misses_max) + " misses",
    });
  if (numeric(expected.combo_min))
    targets.push({
      key: "combo",
      label: "≥" + format(expected.combo_min) + "×",
    });
  // Use the same evaluated attempt as the detailed checklist, never a mix of plays.
  const checks = Array.isArray(quest?.last_attempt?.checks)
    ? quest.last_attempt.checks
    : [];
  const targetList = element("span", "compact-goal-targets");
  let visibleTargets = targets;
  if (Array.isArray(expected.required_keys)) {
    visibleTargets = targets.filter(item => expected.required_keys.includes(item.key));
    if (!visibleTargets.some(item => item.key === "complete")) visibleTargets.unshift({key: "complete", label: "Completar"});
  }
  visibleTargets.forEach(({ key, label }, index) => {
    if (index) targetList.append(document.createTextNode(" · "));
    const check = checks.find((item) => item?.key === key);
    const status = ["met", "unmet", "unknown"].includes(check?.status)
      ? check.status
      : "pending";
    const target = element("span", "compact-goal-target");
    target.dataset.key = key;
    target.dataset.status = status;
    if (status === "met") {
      const icon = element("span", "compact-goal-check", "✓ ");
      icon.setAttribute("aria-hidden", "true");
      target.append(icon);
    }
    target.append(document.createTextNode(label));
    const description = {
      met: "Cumplido en el último intento",
      unmet: "Por alcanzar en el último intento",
      unknown: "No verificable en el último intento",
      pending: "Pendiente de un intento",
    }[status];
    target.title =
      description +
      (check ? " · Resultado: " + questCheckValue(check, check.actual) : "");
    target.append(element("span", "visually-hidden", " (" + description + ")"));
    targetList.append(target);
  });
  goal.append(targetList);
  goal.title =
    "El verde indica los requisitos cumplidos en el último intento. " +
    "Completá el mapa y cumplí todos los objetivos en una misma partida.";
  if (!map.expectation && map.goal) goal.append(element("span", "", map.goal));
  const status = quest?.status;
  card.replaceChildren(heading, byline, stats);
  if (status === "in_progress" || status === "completed") {
    card.append(
      element(
        "p",
        "compact-attempt",
        status === "completed"
          ? "Completada · buscando reemplazo"
          : "En práctica · " +
              format(quest.attempt_count, "0") +
              (quest.attempt_count === 1 ? " intento" : " intentos"),
      ),
    );
  }
  const role = {benchmark: "Referencia · medí tu avance", challenge: "Desafío · ampliar tu control", practice: "Práctica específica", consolidate: "Consolidar", warmup: "Entrar en ritmo"}[map.training_role || expected.training_role];
  if (role) card.append(element("p", "training-role", role));
  if (map.training_progress?.eligible) card.append(element("p", "training-credit", map.training_progress.cycle === currentState?.profile?.training_level?.cycle ? "Cuenta para subir práctica" : "Objetivos conservados · no suma al paso actual"));
  const improved = (quest?.last_attempt?.improvements || []).filter(item => item.improved);
  if (improved.length) card.append(element("p", "practice-improvement", "✓ Mejora registrada: " + improved.map(item => ({accuracy: "precisión", misses: "misses", combo: "combo"}[item.key])).join(" · ")));
  card.append(goal, actions, details);
}

function preserveMissionInteraction(renderCards) {
  const root = $("recommendations");
  const saved = new Map();
  const stageOpen = new Map(
    Array.from(root.querySelectorAll(".stage")).map((stage) => [
      stage.dataset.stage,
      stage.querySelector(".stage-guide")?.open || false,
    ]),
  );
  for (const card of root.querySelectorAll("[data-map-key]")) {
    const open = Array.from(card.querySelectorAll("details"))
      .map((d, i) => (d.open ? i : -1))
      .filter((i) => i >= 0);
    const controls = Array.from(card.querySelectorAll("button,a,summary"));
    saved.set(card.dataset.mapKey, {
      open,
      focus: controls.indexOf(document.activeElement),
    });
  }
  renderCards();
  for (const stage of root.querySelectorAll(".stage")) {
    const descriptions = Array.from(stage.children).filter((child) =>
      child.classList.contains("stage-description"),
    );
    const count = stage.querySelector(".quest-stage-count");
    if (descriptions.length || count) {
      const more = element("details", "stage-guide");
      const summaries = {
        warmup: "Mapas accesibles para calentar y estabilizar.",
        practice: "Mapas de tu rango para trabajar habilidades clave.",
        challenge: "Afianzá tu nivel y prepará el próximo paso.",
      };
      const summary = element(
        "summary",
        "",
        summaries[stage.dataset.stage] || "Orientación de esta etapa",
      );
      summary.title = "Ver orientación y estado de esta etapa";
      more.append(summary);
      if (count) more.append(count);
      descriptions.forEach((child) => more.append(child));
      more.open = stageOpen.get(stage.dataset.stage) || false;
      stage.querySelector(".stage-top > div").append(more);
    }
    const number = stage.querySelector(".step-number");
    if (number) number.textContent = String(Number(number.textContent));
    // Active missions precede refill notices; empty stages still show their status.
    if (stage.querySelector(".map-card"))
      Array.from(stage.children)
        .filter((child) => child.classList.contains("quest-search-note"))
        .forEach((child) => stage.append(child));
  }
  for (const card of root.querySelectorAll("[data-map-key]")) {
    const prior = saved.get(card.dataset.mapKey);
    if (!prior) continue;
    const details = card.querySelectorAll("details");
    prior.open.forEach((i) => {
      if (details[i]) details[i].open = true;
    });
    if (prior.focus >= 0)
      card
        .querySelectorAll("button,a,summary")
        [prior.focus]?.focus({ preventScroll: true });
  }
}

let radarMode = "control";
let radarSignature = "";
function renderCompactProfile(state) {
  const signature = JSON.stringify([
    state.player_profile,
    state.tag_analysis,
    state.settings?.values?.strong_accuracy,
    state.settings?.values?.strong_miss_percent,
    state.settings?.values?.strong_combo_percent,
    radarMode,
  ]);
  if (signature !== radarSignature) {
    radarSignature = signature;
    const band = state.player_profile?.evidence?.star_band;
    text(
      "quick-radar-scope",
      numeric(band?.min) && numeric(band?.max)
        ? `Resultados recientes en mapas de ${format(band.min)}–${format(band.max)} ★.`
        : "Resultados recientes en mapas comparables.",
    );
    const control = buildRadarModel(state, "control");
    const axes =
      radarMode === "control" ? control : buildRadarModel(state, "tags");
    renderRadar($("quick-radar"), control, { compact: true });
    renderRadar($("profile-radar"), axes, { mode: radarMode });
    renderRadarValues($("radar-values"), axes);
    text(
      "radar-description",
      radarMode === "control"
        ? "Resultados recientes con escalas ampliadas por eje. El borde representa un resultado perfecto en esa medida; la línea gris marca las referencias de control. Los valores reales y las escalas aparecen debajo."
        : "Precisión en mapas con cada tag, ampliada de 90 a 100 %. Los valores inferiores se ubican en el centro y conservan su valor real. Cada partida puede aportar a varios tags; esto describe resultados en tu rango actual.",
    );
  }
  text(
    "overview-value",
    format(state.profile?.baseline, "Por calibrar") +
      (numeric(state.profile?.baseline) ? " ★" : "") +
      " · " +
      (state.profile?.focus || "Reunir partidas"),
  );
  const strengths = state.player_profile?.strengths || [];
  const host = $("quick-strengths");
  const pills = element("div", "strength-pills");
  if (strengths.length)
    strengths.forEach((item) =>
      pills.append(element("span", "strength-pill", item.label)),
    );
  else
    pills.append(element("p", "", "Reuniendo evidencia en distintos mapas."));
  host.replaceChildren(element("h3", "", "Puntos fuertes en tu rango"), pills);
  text(
    "quick-focus-label",
    state.player_profile?.priorities?.[0]?.label ||
      state.profile?.focus ||
      "Reunir partidas comparables",
  );
  const rank = state.coach_progress?.rank,
    next = state.coach_progress?.next_rank;
  text(
    "quick-rank-value",
    numeric(rank?.stars)
      ? Number(rank.stars).toLocaleString("es-AR", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })
      : "Rango por consolidar",
  );
  text(
    "quick-rank-next",
    numeric(next?.stars)
      ? "Próximo: " +
          format(next.stars) +
          " ★ · " +
          format(next.completed_maps, "0") +
          "/" +
          format(next.required_maps, "3") +
          " mapas →"
      : "Ver tu progreso →",
  );
  text(
    "quick-rank-target",
    numeric(next?.stars) ? format(next.stars) : "Por definir",
  );
  const earned = Number(next?.completed_maps) || 0,
    required = Number(next?.required_maps) || 3;
  text("quick-rank-evidence", earned + " / " + required);
  $("quick-rank-fill").style.width =
    Math.min(100, Math.max(0, (earned / required) * 100)) + "%";
  const session = state.profile?.session || state.profile || {};
  text(
    "session-accuracy",
    numeric(session.accuracy) ? accuracy(session.accuracy) : "Sin datos",
  );
  const calibrated = state.profile?.phase === "training";
  text(
    "calibration-label",
    calibrated ? "Calibración completa" : "Calibración inicial",
  );
  text(
    "session-readiness",
    calibrated
      ? "Lista para entrenar"
      : $("calibration-count").textContent + " partidas registradas",
  );
  $("overview-panel").dataset.calibrated = String(calibrated);
  document
    .querySelector(".session-calibration [data-icon]")
    .replaceChildren(uiIcon(calibrated ? "check" : "clock"));
  text("progression-caption", "Elegí un mapa y cumplí sus objetivos.");
  if (
    state.quest_board?.automatic_refresh ||
    (!state.quest_board && state.quest_completions)
  ) {
    text(
      "quest-progress-label",
      Number(state.quest_board?.active_count) === 1 ? "activa" : "activas",
    );
    text(
      "quest-completed-total",
      format(state.quest_completions?.total, "0") + " completadas",
    );
    $("quest-new-button").replaceChildren(
      uiIcon("refresh"),
      document.createTextNode("Renovar pendientes"),
    );
  }
  const policy = state.discovery?.quality_policy || {};
  text(
    "discovery-preview-quality",
    format(policy.min_rating ?? 8) +
      " / 10 · " +
      format(policy.min_votes ?? 10) +
      " votos · " +
      format(policy.min_play_count ?? 10000) +
      " partidas",
  );
  text(
    "discovery-preview-message",
    state.discovery?.state === "loading"
      ? "Buscando más alternativas para tu entrenamiento…"
      : state.discovery?.state === "error"
        ? "La búsqueda necesita reintentarse. Ver estado y opciones."
        : "Explorá canciones que se adapten a tu entrenamiento.",
  );
}

const viewHashes = {
  train: "entrenar",
  profile: "perfil",
  progress: "progreso",
  history: "historial",
  settings: "ajustes",
};
function selectView(view, { updateHash = true, focus = false } = {}) {
  if (!Object.hasOwn(viewHashes, view)) view = "train";
  for (const key of Object.keys(viewHashes)) {
    const selected = key === view;
    $("view-" + key).hidden = !selected;
    $("tab-" + key).setAttribute("aria-selected", String(selected));
    $("tab-" + key).tabIndex = selected ? 0 : -1;
  }
  if (view === "settings") $("settings-panel").open = true;
  if (view === "progress" && currentState?.coach_progress)
    renderCoachChart(currentState.coach_progress);
  if (updateHash && location.hash !== "#" + viewHashes[view])
    history.pushState(null, "", "#" + viewHashes[view]);
  if (focus) $("view-" + view).focus({ preventScroll: true });
}
function setupNavigation() {
  const fromHash = () => {
    if (location.hash === "#main-content") return;
    selectView(
      Object.keys(viewHashes).find(
        (key) => viewHashes[key] === location.hash.slice(1),
      ) || (location.hash === "#settings-section" ? "settings" : "train"),
      { updateHash: false },
    );
  };
  const tabs = Array.from(document.querySelectorAll("[role=tab][data-view]"));
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectView(tab.dataset.view));
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft")
        next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      if (next !== null) {
        event.preventDefault();
        selectView(tabs[next].dataset.view);
        tabs[next].focus();
      }
    });
  });
  document
    .querySelectorAll("[data-go-view]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        selectView(button.dataset.goView, { focus: true }),
      ),
    );
  document.querySelectorAll("[data-radar-mode]").forEach((button) =>
    button.addEventListener("click", () => {
      radarMode = button.dataset.radarMode;
      document
        .querySelectorAll("[data-radar-mode]")
        .forEach((other) =>
          other.setAttribute("aria-pressed", String(other === button)),
        );
      if (currentState) renderCompactProfile(currentState);
    }),
  );
  window.addEventListener("popstate", fromHash);
  window.addEventListener("hashchange", fromHash);
  const historyBox = document.querySelector(".history-box");
  historyBox.tabIndex = 0;
  historyBox.setAttribute("role", "region");
  historyBox.setAttribute(
    "aria-label",
    "Tabla de partidas recientes; desplazable en pantallas pequeñas",
  );
  fromHash();
}

setupTrainingLayout();
setupNavigation();
poll();
