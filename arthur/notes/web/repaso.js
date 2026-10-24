// Pestaña «Repaso» de ARTHUR Apuntes: una sesión diaria de preguntas sacadas de tus apuntes, con repaso espaciado.
// La página no decide nada: pide las preguntas a Apuntes (que las trae del cerebro con su llave), manda lo que
// contestas y pinta la corrección que devuelve el servidor. La respuesta no llega al navegador hasta contestar.
//
// Seguridad, como el resto de Apuntes: el texto va siempre con textContent; las fórmulas con katex.render, que
// crea los nodos él mismo (pasarlas como HTML no: la CSP bloquea los estilos en línea que lleva ese HTML y abriría
// la puerta a meter marcas); solo se enlaza a direcciones http(s). El almacén del navegador solo recuerda el
// último tema del modo libre, y siempre dentro de try/catch (en modo privado o sin espacio, lanza).
"use strict";

const R_SCREENS = ["r-home", "r-question", "r-result", "r-report", "r-end", "r-free", "r-settings"];
const R_LAST_FOLDER = "arthur-repaso-tema";
const R_TIPOS = { test: "Pregunta de test", vf: "Verdadero o falso", formula: "Pregunta de fórmula",
                  corta: "Respuesta corta" };
const R_HUECO = /_{3,}/g;   // el hueco de las preguntas de fórmula (generate.py HUECO = "___")

const rs = {
  hoy: null,          // lo último de /api/repaso/hoy
  mode: "diaria",     // «diaria» o «libre»
  folder: "",         // el tema del modo libre
  queue: [],          // las preguntas que quedan por delante
  i: 0,               // la que se está contestando
  offset: 0,          // las ya hechas antes de abrir la página (para «Pregunta 4 de 10»)
  total: 0,
  ok: 0,              // aciertos en esta tanda
  answered: 0,
  racha: 0,
  hecha: false,
  last: null,         // la última corrección (corta: la solución, a falta de la nota)
  before: "r-question", // de dónde se ha ido a «Esta pregunta está mal»
  busy: false,
  fallos: {},         // fallos de cada pregunta en esta sesión diaria (a los 3, el servidor deja de devolverla)
};

const rq = () => rs.queue[rs.i];

function rScreen(id, focusId) {
  for (const s of R_SCREENS) $(s).hidden = s !== id;
  window.scrollTo(0, 0);
  const target = focusId && $(focusId);
  if (target) target.focus({ preventScroll: true });
}

function rApi(path, body) {
  return body === undefined ? api(path) : post(path, body);
}

// --- fórmulas ---
function mathNode(tex, display) {
  const node = document.createElement(display ? "div" : "span");
  node.className = display ? "r-math r-math--block" : "r-math";
  if (typeof katex === "undefined") {   // KaTeX no ha cargado: mejor la fórmula en crudo que nada
    node.textContent = display ? `$$${tex}$$` : `$${tex}$`;
    return node;
  }
  try {
    katex.render(tex, node, { throwOnError: false, displayMode: display, maxSize: 20, maxExpand: 200 });
  } catch (_) {
    node.textContent = tex;
  }
  return node;
}

const withGap = (tex) => tex.replace(R_HUECO, "\\boxed{\\phantom{00}}");

// Texto con fórmulas $…$ (en línea) y $$…$$ (aparte): lo de fuera, como texto; lo de dentro, con KaTeX.
function richText(box, text) {
  box.replaceChildren();
  const re = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) box.append(document.createTextNode(text.slice(last, m.index)));
    box.append(mathNode(withGap(m[1] !== undefined ? m[1] : m[2]), m[1] !== undefined));
    last = re.lastIndex;
  }
  if (last < text.length) box.append(document.createTextNode(text.slice(last)));
}

// En las de fórmula, la fórmula con el hueco va aparte y en grande; el resto del enunciado, encima.
function splitFormula(text) {
  const re = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g;
  let m;
  while ((m = re.exec(text))) {
    const tex = m[1] !== undefined ? m[1] : m[2];
    if (/_{3,}/.test(tex)) {
      const rest = (text.slice(0, m.index) + text.slice(re.lastIndex)).trim().replace(/[:\s]+$/, "");
      return { prompt: rest ? `${rest}.`.replace(/([.?!])\.$/, "$1") : "Completa el hueco de la fórmula.", tex };
    }
  }
  return { prompt: text, tex: null };
}

// --- utilidades ---
const pct = (f) => `${Math.round(100 * f)}\u202f%`;   // espacio fino que no se parte
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const rSafeUrl = (url) => typeof url === "string" && /^https?:\/\//.test(url);

function rEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function rIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "a-icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function longDay(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const text = new Intl.DateTimeFormat("es-ES", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, d)));
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function lastFolder() {
  try { return localStorage.getItem(R_LAST_FOLDER) || ""; } catch (_) { return ""; }
}

function rememberFolder(folder) {
  try { localStorage.setItem(R_LAST_FOLDER, folder); } catch (_) { /* sin almacenamiento: no pasa nada */ }
}

// --- 1. Inicio ---
async function openRepaso() {
  rScreen("r-home");
  $("r-down").hidden = true;
  $("r-home-body").hidden = false;
  $("r-lead").textContent = "Cargando…";
  $("r-sub").textContent = "";
  $("r-start").hidden = true;
  let hoy, resumen;
  try {
    [hoy, resumen] = await Promise.all([rApi("/api/repaso/hoy"), rApi("/api/repaso/resumen")]);
  } catch (e) {
    if (!$("login").hidden) return;   // sin llave: api() ya enseña «Entrar»
    $("r-down-text").replaceChildren(rEl("strong", "", "El Repaso no está disponible ahora."), " ",
                                     rEl("span", "", e.message === "El cerebro no está disponible ahora" ? ""
                                       : e.message));
    $("r-down").hidden = false;
    $("r-home-body").hidden = true;
    return;
  }
  rs.hoy = hoy;
  $("r-date").textContent = hoy.dia ? longDay(hoy.dia) : "";
  $("r-date").hidden = !hoy.dia;
  const left = hoy.preguntas.length;
  const start = $("r-start");
  start.hidden = left === 0;
  if (hoy.total === 0) {
    $("r-lead").textContent = "Hoy no hay preguntas todavía.";
    $("r-sub").textContent = "Se preparan cada noche a partir de los apuntes de tus asignaturas activas.";
  } else if (left === 0) {
    $("r-lead").textContent = "Ya has hecho el repaso de hoy.";
    $("r-sub").textContent = "Mañana tendrás otra sesión. Si quieres más, prueba el repaso libre.";
  } else if (hoy.hechas > 0) {
    $("r-lead").textContent = `Te ${left === 1 ? "queda" : "quedan"} ${plural(left, "pregunta", "preguntas")}.`;
    $("r-sub").textContent = `Llevas ${hoy.hechas} de ${hoy.total}. Sigue por donde lo dejaste.`;
    start.textContent = "Seguir";
  } else {
    $("r-lead").textContent = `${plural(left, "pregunta", "preguntas")} te ${left === 1 ? "espera" : "esperan"}.`;
    $("r-sub").textContent = "Empieza por lo que toca recordar hoy. Puedes parar y volver después.";
    start.textContent = "Empezar";
  }
  $("r-start-note").hidden = left === 0;

  $("r-streak").textContent = String(resumen.racha);
  $("r-streak-label").textContent = resumen.racha === 1 ? "día seguido con la sesión hecha"
    : "días seguidos con la sesión hecha";
  $("r-week").textContent = resumen.aciertos_7d === null ? "–" : pct(resumen.aciertos_7d);
  $("r-week-label").textContent = resumen.aciertos_7d === null ? "aún sin respuestas esta semana"
    : "de respuestas acertadas esta semana";
  const subjects = Object.entries(resumen.por_asignatura).sort(([a], [b]) => a.localeCompare(b, "es"));
  $("r-subjects").replaceChildren(...subjects.map(([name, f]) => {
    const li = rEl("li");
    li.append(rEl("span", "", name), rEl("strong", "", pct(f)));
    return li;
  }));
  $("r-subjects-empty").hidden = subjects.length > 0;
}

function startDaily() {
  const hoy = rs.hoy;
  Object.assign(rs, { mode: "diaria", folder: "", queue: hoy.preguntas.slice(), i: 0, offset: hoy.hechas,
                      total: hoy.total, ok: 0, answered: 0, racha: hoy.racha, hecha: hoy.hecha, fallos: {} });
  showQuestion();
}

// --- 2. Pregunta ---
function choice(name, value, label, math) {
  const wrap = rEl("label", "a-choice");
  const input = document.createElement("input");
  input.type = "radio";
  input.name = name;
  input.value = value;
  const text = rEl("span");
  if (math) richText(text, label); else text.textContent = label;
  wrap.append(input, text);
  return wrap;
}

// Al acabar la tanda de la sesión diaria se vuelve a pedir /hoy: las falladas vuelven al final de la misma sesión
// (el servidor decide cuáles: hasta acertarlas o fallarlas 3 veces ese día), y se siguen aquí mismo.
async function moreDaily() {
  let hoy;
  try { hoy = await rApi("/api/repaso/hoy"); } catch (e) { say(e.message); return false; }
  if (typeof hoy.hecha === "boolean") rs.hecha = rs.hecha || hoy.hecha;
  if (!hoy.preguntas.length) return false;
  rs.queue.push(...hoy.preguntas);
  return true;
}

async function showQuestion() {
  if (!rq() && rs.mode === "diaria" && !rs.hecha && rs.queue.length && await moreDaily()) return showQuestion();
  const q = rq();
  rs.last = null;
  if (!q) return showEnd();
  const n = rs.offset + rs.i + 1, total = Math.max(rs.total, n);
  $("r-q-title").textContent = R_TIPOS[q.tipo] || "Pregunta";
  $("r-q-subject").textContent = q.carpeta.length ? q.carpeta.join(" · ") : q.asignatura;
  $("r-q-count").textContent = `Pregunta ${n} de ${total}`;
  $("r-q-mode").textContent = rs.mode === "diaria" ? "Sesión diaria" : `Repaso libre${q.nueva ? " · nueva" : ""}`;
  const bar = $("r-progress");
  bar.max = total;
  bar.value = n - 1;
  bar.textContent = `${n - 1} de ${total}`;
  bar.setAttribute("aria-label", `Progreso de la sesión: pregunta ${n} de ${total}`);
  $("r-q-error").hidden = true;

  const prompt = $("r-prompt"), eq = $("r-equation"), choices = $("r-choices");
  eq.hidden = true;
  eq.replaceChildren();
  choices.replaceChildren();
  $("r-answer-field").hidden = true;
  $("r-corta-help").hidden = true;
  $("r-answer").value = "";
  $("r-preview-box").hidden = true;
  $("r-check").textContent = "Comprobar respuesta";

  if (q.tipo === "formula") {
    const { prompt: text, tex } = splitFormula(q.enunciado);
    richText(prompt, text);
    if (tex) {
      eq.append(mathNode(withGap(tex), true));
      eq.hidden = false;
      eq.dataset.tex = tex;
    }
    $("r-answer-field").hidden = false;
  } else {
    richText(prompt, q.enunciado);
  }
  if (q.tipo === "test") {
    q.opciones.forEach((o) => choices.append(choice("r-opcion", o, o, true)));
  } else if (q.tipo === "vf") {
    choices.append(choice("r-opcion", "true", "Verdadero"), choice("r-opcion", "false", "Falso"));
  } else if (q.tipo === "corta") {
    $("r-corta-help").hidden = false;
    $("r-check").textContent = "Ver la respuesta";
  }
  rScreen("r-question", "r-prompt");
}

function previewFormula() {
  const tex = $("r-equation").dataset.tex, value = $("r-answer").value.trim();
  const box = $("r-preview-box");
  box.hidden = !tex || !value;
  if (box.hidden) return;
  $("r-preview").replaceChildren(mathNode(tex.replace(R_HUECO, () => value), true));
}

function answerBody(q) {
  if (q.tipo === "corta") return { question_id: q.id };   // primero, la solución; la nota la pone Marc después
  if (q.tipo === "formula") {
    const value = $("r-answer").value.trim();
    if (!value) throw new Error("Escribe lo que va en el hueco.");
    return { question_id: q.id, respuesta: value };
  }
  const picked = $("r-choices").querySelector("input:checked");
  if (!picked) throw new Error("Elige una respuesta.");
  return { question_id: q.id, respuesta: q.tipo === "vf" ? picked.value === "true" : picked.value };
}

async function submitAnswer(ev) {
  ev.preventDefault();
  if (rs.busy) return;
  const q = rq();
  let body;
  try { body = answerBody(q); } catch (e) {
    $("r-q-error").textContent = e.message;
    $("r-q-error").hidden = false;
    return;
  }
  const button = $("r-check");
  rs.busy = true;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    showResult(q, await rApi("/api/repaso/respuesta", body));
  } catch (e) {
    say(e.message);
  } finally {
    rs.busy = false;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

// --- 3. Corrección ---
function solutionNode(q, data) {
  const p = rEl("p");
  if (q.tipo === "vf") {
    p.textContent = `La respuesta es «${data.respuesta ? "Verdadero" : "Falso"}».`;
  } else if (q.tipo === "formula") {
    p.append("La respuesta es ", mathNode(String(data.respuesta), false), ".");
  } else if (q.tipo === "corta") {
    richText(p, String(data.respuesta));
  } else {
    p.append("La respuesta es «");
    const span = rEl("span");
    richText(span, String(data.respuesta));
    p.append(span, "».");
  }
  return p;
}

function notice(state, iconName, title, parts) {
  const box = rEl("div", `a-notice a-state--${state} r-result`);
  const body = rEl("div");
  body.append(rEl("strong", "", title), ...parts);
  box.append(rIcon(iconName), body);
  return box;
}

function showResult(q, data) {
  rs.last = data;
  const n = rs.offset + rs.i + 1;
  $("r-res-count").textContent = `Pregunta ${n} de ${Math.max(rs.total, n)}`;
  const explain = data.explicacion ? [(() => { const p = rEl("p"); richText(p, data.explicacion); return p; })()] : [];
  let box;
  if (data.falta_nota) {
    $("r-res-title").textContent = "La respuesta";
    box = notice("info", "info", "Compárala con la tuya.", [solutionNode(q, data), ...explain]);
  } else {
    countAnswer(data);
    // ¿Vuelve en esta sesión? Lo dice el servidor si llega `otra_vez`; si no, se cuenta aquí: a los 3 fallos
    // del día (MAX_FALLOS en store.py) ya no vuelve hasta otro repaso
    if (!data.correcto && rs.mode === "diaria") rs.fallos[q.id] = (rs.fallos[q.id] || 0) + 1;
    const otraVez = rs.mode === "diaria" && !data.correcto &&
      (typeof data.otra_vez === "boolean" ? data.otra_vez : rs.fallos[q.id] < 3);
    $("r-res-title").textContent = data.correcto ? "Corrección\u00a0· acierto" : "Corrección\u00a0· fallo";
    box = data.correcto
      ? notice("success", "ok", "Bien.", [solutionNode(q, data), ...explain])
      : notice("error", "ko", "Esta vez no ha salido.",
               [solutionNode(q, data), ...explain,
                rEl("p", "", otraVez ? "Esta pregunta volverá al final de esta sesión."
                                     : "Esta pregunta volverá en otro repaso.")]);
  }
  $("r-res-status").replaceChildren(box);
  $("r-grade").hidden = !data.falta_nota;
  $("r-next").hidden = data.falta_nota;
  const link = $("r-source-link");
  link.hidden = !rSafeUrl(data.enlace);
  if (!link.hidden) link.href = data.enlace;
  link.rel = "noopener noreferrer";
  // Una fallada de la sesión diaria vuelve al final: aún no se ha terminado aunque sea la última de la tanda
  const vuelve = rs.mode === "diaria" && !data.falta_nota && !data.correcto && !data.hecha &&
    (typeof data.otra_vez === "boolean" ? data.otra_vez : (rs.fallos[q.id] || 0) < 3);
  $("r-next").textContent = rs.i + 1 < rs.queue.length || vuelve ? "Siguiente pregunta" : "Terminar";
  rScreen("r-result", data.falta_nota ? null : "r-next");
  if (data.falta_nota) $("r-grade").querySelector("button").focus({ preventScroll: true });
}

function countAnswer(data) {
  rs.answered += 1;
  if (data.correcto) rs.ok += 1;
  if (typeof data.racha === "number") rs.racha = data.racha;
  if (data.hecha) rs.hecha = true;
}

async function grade(nota, button) {
  if (rs.busy) return;
  rs.busy = true;
  for (const b of $("r-grade").querySelectorAll("button")) b.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    const data = await rApi("/api/repaso/respuesta", { question_id: rq().id, nota });
    countAnswer(data);
    nextQuestion();
  } catch (e) {
    say(e.message);
  } finally {
    rs.busy = false;
    for (const b of $("r-grade").querySelectorAll("button")) b.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function nextQuestion() {
  rs.i += 1;
  showQuestion();
}

// --- 4. Esta pregunta está mal ---
function openReport(from) {
  rs.before = from;
  $("r-report-form").reset();
  $("r-report-error").hidden = true;
  rScreen("r-report", "r-report-title");
}

async function sendReport(ev) {
  ev.preventDefault();
  if (rs.busy) return;
  const picked = $("r-report-form").querySelector("input[name=motivo]:checked");
  const detail = $("r-detail").value.trim();
  if (!picked && !detail) {
    $("r-report-error").textContent = "Elige qué falla o escríbelo.";
    $("r-report-error").hidden = false;
    return;
  }
  const motivo = [picked ? picked.value : "", detail].filter(Boolean).join(": ").slice(0, 300);
  const button = $("r-report-send");
  rs.busy = true;
  button.disabled = true;
  try {
    await rApi("/api/repaso/mal", { question_id: rq().id, motivo });
    say("Gracias: esa pregunta ya no volverá a salir.");
    const id = rq().id;
    // Si estaba repetida más adelante (fallada que volvía), fuera también
    for (let k = rs.queue.length - 1; k > rs.i; k -= 1) if (rs.queue[k].id === id) rs.queue.splice(k, 1);
    if (rs.before === "r-result" && rs.last && !rs.last.falta_nota) {
      rs.i += 1;   // ya contestada: cuenta como hecha y se sigue con la siguiente
    } else {
      // Sin contestar: se quita de la tanda y de la cuenta («Pregunta 4 de 9»)
      rs.queue.splice(rs.i, 1);
      rs.total = Math.max(0, rs.total - 1);
    }
    showQuestion();
  } catch (e) {
    say(e.message);
  } finally {
    rs.busy = false;
    button.disabled = false;
  }
}

// --- 5. Fin ---
function showEnd() {
  const daily = rs.mode === "diaria";
  $("r-end-title").textContent = daily ? "Sesión terminada" : "Tema repasado";
  $("r-end-tag").textContent = daily ? "Hoy, hecho" : "Repaso libre";
  $("r-end-tag").className = daily && rs.hecha ? "a-tag a-state--success" : "a-tag";
  $("r-end-lead").textContent = daily
    ? (rs.hecha ? "Ya has repasado las preguntas de hoy." : "Has llegado al final de las preguntas de hoy.")
    : `Has repasado ${rs.folder}.`;
  $("r-end-ok").textContent = `${rs.ok} de ${rs.answered}`;
  $("r-end-streak").textContent = plural(rs.racha, "día", "días");
  rScreen("r-end", "r-end-title");
}

// --- 6. Repaso libre ---
async function openFree() {
  let data;
  try { data = await rApi("/api/repaso/libre"); } catch (e) { say(e.message); return; }
  const last = lastFolder();
  const list = $("r-folders");
  list.replaceChildren(...data.carpetas.map(({ carpeta, preguntas }) => {
    const parts = carpeta.split("/");
    const li = rEl("li", parts.length > 1 ? "r-folder r-folder--sub" : "r-folder");
    li.style.setProperty("--r-depth", String(parts.length - 1));
    const button = rEl("button", "r-folder__button");
    button.type = "button";
    const name = rEl("span", "r-folder__name", parts[parts.length - 1]);
    const meta = rEl("span", "r-folder__meta", plural(preguntas, "pregunta", "preguntas"));
    button.append(name, meta);
    if (carpeta === last) button.append(rEl("span", "a-tag", "La última"));
    button.setAttribute("aria-label", `${carpeta.split("/").join(", ")}: ${plural(preguntas, "pregunta", "preguntas")}`);
    button.addEventListener("click", () => startFree(carpeta));
    li.append(button);
    return li;
  }));
  $("r-folders-empty").hidden = data.carpetas.length > 0;
  rScreen("r-free", "r-free-title");
}

async function startFree(folder) {
  let data;
  try { data = await rApi(`/api/repaso/libre?carpeta=${encodeURIComponent(folder)}`); } catch (e) {
    say(e.message);
    return;
  }
  rememberFolder(folder);
  if (!data.preguntas.length) { say("En ese tema no hay preguntas ahora."); return; }
  Object.assign(rs, { mode: "libre", folder: folder.split("/").join(" · "), queue: data.preguntas, i: 0, offset: 0,
                      total: data.preguntas.length, ok: 0, answered: 0, hecha: false });
  showQuestion();
}

// --- 7. Ajustes ---
async function openSettings() {
  let data;
  try { data = await rApi("/api/repaso/ajustes"); } catch (e) { say(e.message); return; }
  const names = [...data.disponibles, ...data.asignaturas.filter((a) => !data.disponibles.includes(a))];
  $("r-subject-checks").replaceChildren(...names.map((name) => {
    const label = rEl("label", "r-check");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = name;
    box.checked = data.asignaturas.includes(name);
    label.append(box, rEl("span", "", name));
    return label;
  }));
  $("r-subject-none").hidden = names.length > 0;
  $("r-notice").value = data.aviso || "20:00";
  $("r-settings-error").hidden = true;
  rScreen("r-settings", "r-settings-title");
}

async function saveSettings(ev) {
  ev.preventDefault();
  const aviso = $("r-notice").value;
  if (!/^\d{2}:\d{2}$/.test(aviso)) {
    $("r-settings-error").textContent = "Pon una hora, por ejemplo 20:00.";
    $("r-settings-error").hidden = false;
    return;
  }
  const asignaturas = Array.from($("r-subject-checks").querySelectorAll("input:checked")).map((b) => b.value);
  const button = $("r-settings-save");
  button.disabled = true;
  try {
    await rApi("/api/repaso/ajustes", { asignaturas, aviso });
    say("Ajustes guardados.");
    await openRepaso();
  } catch (e) {
    $("r-settings-error").textContent = e.message;
    $("r-settings-error").hidden = false;
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("r-start").addEventListener("click", startDaily);
  $("r-retry").addEventListener("click", openRepaso);
  $("r-q-form").addEventListener("submit", submitAnswer);
  $("r-answer").addEventListener("input", previewFormula);
  $("r-next").addEventListener("click", nextQuestion);
  for (const b of $("r-grade").querySelectorAll("button")) {
    b.addEventListener("click", () => grade(Number(b.dataset.nota), b));
  }
  $("r-q-bad").addEventListener("click", () => openReport("r-question"));
  $("r-res-bad").addEventListener("click", () => openReport("r-result"));
  $("r-report-form").addEventListener("submit", sendReport);
  $("r-report-back").addEventListener("click", () => rScreen(rs.before, rs.before === "r-question" ? "r-prompt" : "r-next"));
  $("r-end-home").addEventListener("click", openRepaso);
  $("r-end-settings").addEventListener("click", openSettings);
  $("r-free-open").addEventListener("click", openFree);
  $("r-settings-open").addEventListener("click", openSettings);
  $("r-settings-form").addEventListener("submit", saveSettings);
  for (const b of document.querySelectorAll(".r-home-back")) b.addEventListener("click", openRepaso);
});
