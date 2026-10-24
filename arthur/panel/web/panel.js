// Página del panel de vida. Todo el texto se pone con textContent (nunca innerHTML): un título de tarea o el
// asunto de un correo con «<script>» se ve como texto, no se ejecuta. Las peticiones que cambian algo llevan
// X-Arthur (protección CSRF, como Apuntes).
"use strict";

const $ = (id) => document.getElementById(id);
const ZONA = "Europe/Madrid";
const FUENTES = { arthur: "ARTHUR", studio: "Studio", obsidian: "Obsidian", calendar: "Calendario" };
const REFRESCO = 5 * 60 * 1000;  // el día cambia poco: cada 5 minutos basta (y al volver a la pestaña)
let day = null;                  // lo último que devolvió /api/today
let timer = null;

async function api(path, options = {}) {
  const init = { credentials: "same-origin", ...options, headers: { ...(options.headers || {}) } };
  if (init.method === "POST") {
    init.headers["X-Arthur"] = "1";
    if (init.body !== undefined) init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch (_) { /* sin cuerpo JSON */ }
  if (res.status === 401) { showLogin(); throw new Error(data.error || "Hace falta entrar"); }
  if (!res.ok) {
    const err = new Error(data.error || `Error ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

const post = (path, body) => api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

function say(text) {
  const toast = $("toast");
  toast.textContent = text;
  toast.classList.add("is-on");
  clearTimeout(say.t);
  say.t = setTimeout(() => toast.classList.remove("is-on"), 5000);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "a-icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// --- fechas y horas (siempre en hora de Madrid, esté donde esté el móvil) ---
const hora = (iso) => new Intl.DateTimeFormat("es-ES", { hour: "2-digit", minute: "2-digit", timeZone: ZONA })
  .format(new Date(iso));

function diaLargo(isoDay) {
  const [y, m, d] = isoDay.split("-").map(Number);
  return new Intl.DateTimeFormat("es-ES", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, d)));
}

function diaCorto(isoDay) {
  const [y, m, d] = isoDay.split("-").map(Number);
  return new Intl.DateTimeFormat("es-ES", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, d)));
}

function minutosHasta(iso, nowIso) {
  return Math.round((new Date(iso) - new Date(nowIso)) / 60000);
}

function dentroDe(min) {
  if (min < 1) return "ya";
  if (min < 60) return `en ${min} min`;
  const h = Math.floor(min / 60), m = min % 60;
  return m ? `en ${h} h ${m} min` : `en ${h} h`;
}

function saludo(nowIso) {
  const h = Number(new Intl.DateTimeFormat("es-ES", { hour: "numeric", hourCycle: "h23", timeZone: ZONA })
    .format(new Date(nowIso)));
  if (h >= 6 && h < 14) return "Buenos días";
  if (h >= 14 && h < 21) return "Buenas tardes";
  return "Buenas noches";
}

// --- pintar ---
function showLogin() {
  $("panel").hidden = true;
  $("logout").hidden = true;
  $("login").hidden = false;
  stopRefresh();
}

function showPanel() {
  $("login").hidden = true;
  $("panel").hidden = false;
  $("logout").hidden = false;
}

function renderHeader() {
  $("today-date").textContent = diaLargo(day.date);
  $("greeting").textContent = saludo(day.now);
}

function renderProgress() {
  const tareas = day.tasks;
  const total = tareas.length, hechas = tareas.filter((t) => t.done).length;
  const pct = total ? Math.round(100 * hechas / total) : 0;
  const ring = $("ring"), bar = $("progress");
  if (!total) {
    // Sin tareas no es «0 %»: no hay objetivo que medir.
    ring.style.setProperty("--a-value", 0);
    $("ring-value").textContent = "–";
    $("ring-caption").textContent = "sin tareas";
    ring.setAttribute("aria-label", "Tareas de hoy: sin objetivo, no hay tareas");
    $("progress-text").textContent = "Sin tareas";
    bar.max = 1; bar.value = 0;
    return;
  }
  ring.style.setProperty("--a-value", Math.min(100, Math.max(0, pct)));
  $("ring-value").textContent = `${pct} %`;
  $("ring-caption").textContent = `${hechas} de ${total} hechas`;
  ring.setAttribute("aria-label", `Tareas de hoy hechas: ${pct} %, ${hechas} de ${total}`);
  $("progress-text").textContent = `${hechas} de ${total} · ${pct} %`;
  bar.max = total; bar.value = hechas;
}

function renderBriefing(briefing) {
  const text = $("briefing-text"), meta = $("briefing-meta");
  if (!briefing) {
    text.textContent = "Todavía no hay resumen de hoy. Pulsa «Hacer otro» y te lo escribo ahora.";
    text.classList.add("a-muted");
    meta.textContent = "";
    return;
  }
  // El modelo a veces escribe Markdown: se quitan las marcas de negrita para leerlo como texto normal.
  text.textContent = briefing.text.replace(/\*\*|__/g, "").trim();
  text.classList.remove("a-muted");
  const motor = { plantilla: "plantilla fija", "workers-ai": "Workers AI", local: "ARTHUR local" }[briefing.engine]
    || briefing.engine;
  meta.textContent = briefing.sent_at ? `${hora(briefing.sent_at)} · ${motor}` : motor;
}

function renderNow() {
  const { current, following } = day.next || {};
  const failed = day.problems.includes("Calendario no disponible ahora");
  $("now-current").textContent = current ? current.title
    : failed ? "Calendario no disponible" : "Nada en el calendario";
  $("now-current").classList.toggle("a-muted", !current);
  $("now-current-meta").textContent = current ? `hasta las ${hora(current.end)}`
    : failed ? "Vuelve a cargar en un rato." : "Buen momento para una tarea.";
  $("now-next").textContent = following ? following.title : failed ? "Sin datos" : "Nada más hoy";
  $("now-next").classList.toggle("a-muted", !following);
  $("now-next-meta").textContent = following
    ? `a las ${hora(following.start)} · ${dentroDe(minutosHasta(following.start, day.now))}` : "";
}

function renderProblems() {
  const list = $("problems-list");
  list.replaceChildren(...day.problems.map((p) => el("li", "", p)));
  $("problems").hidden = day.problems.length === 0;
}

// «Yo:» sirve en la nota para saber de quién es la casilla; aquí todas son tuyas. Al servidor va el texto entero.
const shownTitle = (task) => task.source === "obsidian" ? task.title.replace(/^Yo:\s*/, "") : task.title;

function taskItem(task) {
  const li = el("li", "p-task");
  li.dataset.done = task.done ? "1" : "";
  const label = el("label", "p-task__check");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = task.done;
  if (task.source === "obsidian") {
    box.setAttribute("aria-describedby", "obsidian-help");
  }
  box.addEventListener("change", () => toggleTask(task, box));
  const body = el("span", "p-task__body");
  body.append(el("span", "p-task__title", shownTitle(task)));
  const meta = el("span", "p-task__meta");
  meta.append(el("span", `a-tag p-src p-src--${task.source}`, FUENTES[task.source] || task.source));
  if (task.due && task.due < day.date && !task.done) {
    meta.append(el("span", "a-tag a-state--warning", `Atrasada · ${diaCorto(task.due)}`));
  } else if (task.due && task.due > day.date) {
    meta.append(el("span", "a-tag", diaCorto(task.due)));
  }
  body.append(meta);
  label.append(box, body);
  li.append(label);
  return li;
}

function renderTasks() {
  $("tasks").replaceChildren(...day.tasks.map(taskItem));
  $("tasks-empty").hidden = day.tasks.length > 0;
  renderProgress();
}

function renderTimeline() {
  const events = day.events;
  const nowMs = new Date(day.now).getTime();
  $("timeline").replaceChildren(...events.map((ev) => {
    const li = el("li", "p-event");
    const start = new Date(ev.start).getTime(), end = new Date(ev.end).getTime();
    let state = "";
    if (!ev.all_day && start <= nowMs && nowMs < end) state = "now";
    else if (!ev.all_day && end <= nowMs) state = "past";
    li.dataset.state = state;
    const when = el("span", "p-event__time a-data", ev.all_day ? "Todo el día" : `${hora(ev.start)}–${hora(ev.end)}`);
    const body = el("span", "p-event__body");
    body.append(el("span", "p-event__title", ev.title));
    if (state === "now") body.append(el("span", "a-tag a-state--success", "Ahora"));
    if (state === "past") body.append(el("span", "a-sr", "(ya ha pasado)"));
    if (ev.study) body.append(marksGroup(ev));
    li.append(when, body);
    return li;
  }));
  const failed = day.problems.includes("Calendario no disponible ahora");
  $("cal-empty").hidden = events.length > 0 || failed;
  $("cal-count").textContent = events.length ? `${events.length} ${events.length === 1 ? "evento" : "eventos"}` : "";
}

function renderMail() {
  const mails = day.mail;
  $("mail").replaceChildren(...mails.map((m) => {
    const li = el("li", "p-mailitem");
    const text = el("span", "p-mailitem__text");
    text.append(el("span", "p-mailitem__from", m.sender), el("span", "p-mailitem__subject a-muted", m.subject));
    if (m.snippet) text.append(el("span", "p-mailitem__subject a-muted", m.snippet));
    li.append(text);
    // Solo enlaces a Gmail: lo que venga en `link` no puede llevar a otra web.
    if (/^https:\/\/mail\.google\.com\//.test(m.link)) {
      const a = el("a", "p-mailitem__open");
      a.href = m.link;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.append(el("span", "", "Abrir"), icon("arrow"));
      a.setAttribute("aria-label", `Abrir en Gmail: ${m.subject}`);
      li.append(a);
    }
    return li;
  }));
  const failed = day.problems.includes("Correo no disponible ahora");
  $("mail-empty").hidden = mails.length > 0 || failed;
  $("mail-count").textContent = mails.length ? `${mails.length} sin leer` : "";
}

// «Repaso: racha 6 · 82 % esta semana», con enlace a la pestaña de Apuntes. Sin cerebro, nada (ni un hueco).
function renderRepaso() {
  const line = $("repaso-line"), r = day.repaso;
  line.hidden = !r;
  if (!r) return;
  const pct = r.aciertos_7d === null ? "sin respuestas esta semana" : `${r.aciertos_7d} % esta semana`;
  const text = `Repaso: racha ${r.racha} · ${pct}`;
  const hint = r.hecha ? "hoy, hecho" : r.pendientes ? `${r.pendientes} por hacer hoy` : "";
  if (safeUrl(r.url)) {
    const a = el("a", "p-repaso__link", text);
    a.href = r.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.append(icon("arrow"));
    line.replaceChildren(a);
  } else {
    line.replaceChildren(el("span", "p-repaso__text", text));
  }
  if (hint) line.append(el("span", r.hecha ? "a-tag a-state--success" : "a-tag", hint));
}

// --- bloques de estudio: ✓ hecho · ½ a medias · ✗ no. Sin tocar = «sin marcar», nunca «fallado» ---
const MARCAS = [["hecho", "check", "Hecho"], ["medio", "half", "A medias"], ["no", "x", "No"]];

function marksGroup(ev) {
  const group = el("span", "p-marks");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", `¿Hiciste «${ev.title}»?`);
  group.setAttribute("aria-describedby", "marks-help");
  for (const [mark, name, label] of MARCAS) {
    const b = el("button", "p-mark");
    b.type = "button";
    b.dataset.mark = mark;
    b.setAttribute("aria-pressed", ev.mark === mark ? "true" : "false");
    b.setAttribute("aria-label", label);
    b.title = label;
    b.append(icon(name));
    b.addEventListener("click", () => markBlock(ev, ev.mark === mark ? null : mark, group));
    group.append(b);
  }
  return group;
}

async function markBlock(ev, mark, group) {
  const buttons = group.querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await post("/api/revision/bloque", { fecha: day.date, clave: ev.key, marca: mark });
    ev.mark = mark;
    buttons.forEach((b) => b.setAttribute("aria-pressed", b.dataset.mark === mark ? "true" : "false"));
    const label = { hecho: "hecho", medio: "a medias", no: "no hecho" }[mark] || "sin marcar";
    say(`${ev.title}: ${label}`);
    loadRevision();
  } catch (e) {
    say(e.message);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// --- Cómo va hoy: la revisión. Los números vienen del servidor (los cuenta el código); el texto de la IA, aparte ---
let rev = null;
const LETRAS = ["L", "M", "X", "J", "V", "S", "D"];
const ESTADOS = { activo: "activo", protegido: "protegido por el comodín", pausa: "en pausa", inactivo: "sin actividad",
  sin_datos: "sin datos", hoy: "hoy, aún abierto", futuro: "aún no ha llegado" };
const medios = (n) => (n % 2 ? `${Math.floor(n / 2)},5` : `${n / 2}`);
const plural = (n, uno, varios) => `${n} ${n === 1 ? uno : varios}`;

function statRow(dl, label, value, note) {
  const row = el("div", "p-rev__stat");
  row.append(el("dt", "", label));
  const dd = el("dd", "", value);
  if (note) dd.append(el("span", "p-rev__note a-muted", note));
  row.append(dd);
  dl.append(row);
}

function tareasText(t) {
  return t.total ? `${t.hechas} de ${t.total} · ${t.porcentaje} %` : "Sin tareas en el plan";
}

function estudioText(e) {
  return `${medios(e.medios)} de ${e.total} bloques` + (e.porcentaje === null ? "" : ` · ${e.porcentaje} %`);
}

function renderStats(hoy) {
  const dl = $("rev-stats");
  dl.replaceChildren();
  statRow(dl, "Tareas", tareasText(hoy.tareas), hoy.extra.total ? `+${hoy.extra.total} extra` : "");
  if (hoy.estudio.total) {
    statRow(dl, "Estudio", estudioText(hoy.estudio),
      hoy.estudio.sin_marcar ? `${hoy.estudio.sin_marcar} sin marcar` : "");
  }
  const r = hoy.repaso;
  let repaso = "Sin datos";
  if (r.hecha === true) repaso = "Hecho";
  else if (r.hecha === false) repaso = r.preguntas ? `${r.preguntas} contestadas` : "Por hacer";
  const aciertos = r.preguntas && r.aciertos !== null ? `${r.aciertos} de ${r.preguntas} aciertos` : "";
  statRow(dl, "Repaso", repaso, r.hecha ? aciertos : "");
}

function list(title, items) {
  const box = el("div", "p-rev__list");
  box.append(el("h3", "p-label", title));
  const ul = el("ul", "");
  ul.append(...(items.length ? items : ["Nada"]).map((t) => el("li", items.length ? "" : "a-muted", t)));
  box.append(ul);
  return box;
}

function renderLists(hoy) {
  const hechas = hoy.tareas.hechas_lista.map((t) => t.titulo)
    .concat(hoy.estudio.bloques.filter((b) => b.marca === "hecho" || b.marca === "medio")
      .map((b) => b.titulo + (b.marca === "medio" ? " (a medias)" : "")));
  if (hoy.repaso.hecha) hechas.push("Sesión del Repaso");
  const quedan = hoy.tareas.pendientes_lista.map((t) => t.titulo + (t.atrasada ? " (atrasada)" : ""))
    .concat(hoy.estudio.bloques.filter((b) => b.marca === null || b.marca === "no")
      .map((b) => b.titulo + (b.marca === null ? " (sin marcar)" : "")));
  const boxes = [list("Has hecho", hechas), list("Ha quedado", quedan)];
  if (hoy.extra.total) boxes.push(list("Extra", hoy.extra.lista.map((x) => x.titulo)));
  $("rev-lists").replaceChildren(...boxes);
}

function dayLabel(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Intl.DateTimeFormat("es-ES", { weekday: "long", day: "numeric", timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, d)));
}

function renderGame(g) {
  const n = g.nivel, r = g.racha;
  $("rev-level").textContent = `Nivel ${n.nivel} · Explorador ${n.rango}`;
  $("rev-points").textContent = `${n.puntos} pt · +${g.puntos_hoy.total} hoy`;
  const bar = $("rev-progress");
  bar.max = n.coste; bar.value = n.avance;
  $("rev-next").textContent = `${n.avance} de ${n.coste} pt hacia el nivel ${n.nivel + 1}. `
    + "El nivel mide práctica, no es una nota.";
  let title;
  if (r.en_pausa) title = `En pausa · racha de ${plural(r.racha, "día", "días")}`;
  else if (r.racha) {
    title = `${plural(r.racha, "día", "días")} de racha · ${plural(r.activos, "activo", "activos")}`
      + (r.protegidos ? ` y ${plural(r.protegidos, "protegido", "protegidos")}` : "");
  } else title = r.dias_activos ? "Puedes retomar hoy" : "Empieza cuando quieras";
  $("rev-streak").textContent = title;
  $("rev-days").setAttribute("aria-label", "Esta semana");
  $("rev-days").replaceChildren(...r.semana.map((d, i) => {
    const li = el("li", "", LETRAS[i]);
    if (d.estado === "activo") { li.dataset.done = ""; li.append(icon("check")); }
    if (d.estado === "protegido") { li.dataset.protected = ""; li.append(icon("diamond")); }
    if (d.estado === "pausa") { li.dataset.pause = ""; li.append(icon("pause")); }
    li.setAttribute("aria-label", `${dayLabel(d.fecha)}: ${ESTADOS[d.estado] || d.estado}`);
    return li;
  }));
  $("rev-streak-help").textContent = `${plural(r.dias_activos, "día activo", "días activos")} en total · comodín de esta semana: `
    + (r.comodin_disponible ? "disponible" : "usado");
  const got = g.insignias.filter((b) => b.conseguida).length;
  $("rev-badges-title").textContent = `Insignias · ${got} de ${g.insignias.length}`;
  $("rev-badges").replaceChildren(...g.insignias.map((b) => {
    const li = el("li", "a-achievement p-rev__badge");
    if (!b.conseguida) li.dataset.locked = "";
    const mark = el("span", "a-achievement__mark");
    if (b.conseguida) {
      const img = document.createElement("img");
      img.src = `/insignias/${encodeURIComponent(b.id)}.svg`;
      img.alt = "";
      img.width = 44; img.height = 44;
      mark.append(img);
    } else {
      mark.append(icon("lock"));
    }
    const text = el("span", "p-rev__badge-text");
    text.append(el("strong", "", b.nombre), el("span", "a-small", b.conseguida ? b.texto : b.condicion));
    text.append(el("span", b.conseguida ? "a-tag a-state--success" : "a-tag", b.conseguida ? "Conseguida" : "Por conseguir"));
    li.append(mark, text);
    return li;
  }));
}

function renderWeek(w) {
  const estado = w.estado === "cerrada" ? "cerrada" : "provisional";
  $("rev-week-title").textContent = `Tu semana · ${estado}`;
  const dl = $("rev-week-stats");
  dl.replaceChildren();
  statRow(dl, "Periodo", w.titulo.replace(/^Semana /, ""));
  statRow(dl, "Tareas", tareasText(w.tareas), w.extra ? `+${w.extra} extra` : "");
  if (w.estudio.total) {
    statRow(dl, "Estudio", estudioText(w.estudio),
      w.estudio.sin_marcar ? `${w.estudio.sin_marcar} sin marcar: provisional` : "");
    const asig = w.estudio.por_asignatura.map((a) => `${a.asignatura} ${medios(a.medios)}/${a.total}`).join(" · ");
    if (asig) statRow(dl, "Por asignatura", asig);
  }
  const r = w.repaso;
  if (r.dias_con_datos) {
    statRow(dl, "Repaso", `${r.dias_hechos} de ${r.dias_con_datos} días`,
      r.porcentaje === null ? "" : `${r.porcentaje} % aciertos`);
  }
  statRow(dl, "Días activos", `${w.dias_activos} de ${w.dias_incluidos}`);
  if (rev.juego) statRow(dl, "Puntos", `${w.puntos} pt esta semana`);
  const text = $("rev-week-text");
  text.hidden = !w.revision;
  text.textContent = w.revision ? w.revision.texto : "";
  $("rev-week-read").hidden = !w.revision || w.leida;
  if (w.mostrar && !w.leida) $("rev-week").open = true;
}

function renderRevision() {
  if (!rev) return;
  const hoy = rev.hoy;
  $("revision").hidden = false;
  $("rev-hasta").textContent = rev.pausa ? `Día de descanso · hasta las ${rev.hasta}` : `hasta las ${rev.hasta}`;
  renderStats(hoy);
  renderLists(hoy);
  const quote = $("rev-quote");
  quote.hidden = !rev.revision;
  if (rev.revision) {
    $("rev-text").textContent = rev.revision.texto;
    const motor = { plantilla: "plantilla fija", "workers-ai": "Workers AI", local: "ARTHUR local" }[rev.revision.motor]
      || rev.revision.motor;
    $("rev-meta").textContent = `${rev.revision.tipo === "semana" ? "Resumen semanal" : "Revisión"} de las `
      + `${hora(rev.revision.creada_en)} · ${motor}`;
  }
  $("rev-game").hidden = !rev.juego;
  if (rev.juego) renderGame(rev.juego);
  renderWeek(rev.semana);
  const pause = $("rev-pause");
  pause.setAttribute("aria-pressed", rev.pausa ? "true" : "false");
  pause.querySelector("span").textContent = rev.pausa ? "Quitar descanso" : "Hoy descanso";
  $("rev-hide").textContent = rev.oculto ? "Mostrar juego" : "Ocultar juego";
}

async function loadRevision() {
  try {
    rev = await api("/api/revision");
    renderRevision();
  } catch (_) {
    $("revision").hidden = true;   // sin revisión (panel antiguo o fallo): no se enseña la tarjeta
  }
}

async function revAction(button, path, body, done) {
  button.disabled = true;
  try {
    const out = await post(path, body);
    if (done) say(done(out));
    await loadRevision();
  } catch (e) {
    say(e.message);
  } finally {
    button.disabled = false;
  }
}

function render() {
  renderHeader();
  renderRepaso();
  renderProblems();
  renderNow();
  renderTasks();
  renderTimeline();
  renderMail();
}

// --- cargar ---
async function load() {
  try {
    day = await api("/api/today");
  } catch (e) {
    if ($("login").hidden) say(e.message);
    return;
  }
  showPanel();
  render();
  api("/api/briefing").then((data) => renderBriefing(data.briefing)).catch(() => {});
  loadRevision();
  startRefresh();
}

function startRefresh() {
  stopRefresh();
  timer = setInterval(() => { if (!document.hidden) reload(); }, REFRESCO);
}

function stopRefresh() {
  if (timer) clearInterval(timer);
  timer = null;
}

async function reload() {
  try {
    day = await api("/api/today");
    render();
    loadRevision();
  } catch (_) { /* se reintenta en el siguiente ciclo */ }
}

// --- acciones ---
async function toggleTask(task, box) {
  box.disabled = true;
  try {
    if (task.source === "obsidian") {
      const updated = await post("/api/obsidian/toggle", { file: task.file, line: task.line, text: task.title });
      task.done = updated.done;
    } else {
      const n = task.id.split("-").pop();
      const updated = await post(`/api/tasks/${n}/toggle`);
      task.done = updated.done;
    }
    box.checked = task.done;
    box.closest("li").dataset.done = task.done ? "1" : "";
    renderProgress();
    loadRevision();
    say(task.done ? `Hecha: ${shownTitle(task)}` : `Pendiente otra vez: ${shownTitle(task)}`);
  } catch (e) {
    box.checked = task.done;
    say(e.message);
    if (e.status === 409) reload();  // la nota cambió en Obsidian: se trae la versión nueva
  } finally {
    box.disabled = false;
  }
}

async function addTask(ev) {
  ev.preventDefault();
  const input = $("add-text"), error = $("add-error"), button = $("add-button");
  const text = input.value.trim();
  if (!text) {
    error.textContent = "Escribe qué tienes que hacer.";
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.focus();
    return;
  }
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    const task = await post("/api/tasks", { text });
    input.value = "";
    error.hidden = true;
    input.removeAttribute("aria-invalid");
    say(task.due && task.due !== day.date ? `Apuntada para el ${diaCorto(task.due)}: ${task.title}`
      : `Apuntada: ${task.title}`);
    await reload();
  } catch (e) {
    // Se conserva lo escrito para poder corregirlo.
    error.textContent = e.message;
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

async function redoBriefing() {
  const button = $("redo");
  const label = button.lastChild;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  label.textContent = "Escribiendo…";
  try {
    const data = await post("/api/briefing");
    renderBriefing(data.briefing);
    say("Listo. Resumen nuevo.");
  } catch (e) {
    say(e.message);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    label.textContent = "Hacer otro";
  }
}

// --- pregunta a tu cerebro ---
const MOTORES = { "workers-ai": "Workers AI", local: "ARTHUR local", ninguno: "sin redactar" };

// Solo se enlaza a Obsidian y a páginas web: un «javascript:» que viniera en la respuesta no se convierte en enlace.
const safeUrl = (url) => typeof url === "string" && /^(obsidian:\/\/open\?|https?:\/\/)/.test(url);

function renderBrain(data) {
  $("brain-answer").textContent = data.answer;
  $("brain-meta").textContent = data.found ? `Contesta: ${MOTORES[data.engine] || data.engine}` : "";
  $("brain-sources").replaceChildren(...data.citations.map((c) => {
    const li = el("li", "p-brain__source");
    li.append(el("span", "a-data p-brain__id", c.id));
    if (safeUrl(c.url)) {
      const a = el("a", "p-brain__link", c.title);
      a.href = c.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      li.append(a);
    } else {
      li.append(el("span", "", c.title));
    }
    return li;
  }));
  $("brain-result").hidden = false;
}

async function askBrain(ev) {
  ev.preventDefault();
  const input = $("brain-q"), button = $("brain-button");
  const q = input.value.trim();
  if (!q) { input.focus(); return; }
  const label = button.lastChild;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  label.textContent = "Buscando…";
  try {
    renderBrain(await post("/api/cerebro/ask", { q }));
  } catch (e) {
    say(e.message);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    label.textContent = "Preguntar";
  }
}

// Entrar con el enlace que da ARTHUR (…/#llave=arthur_…): la llave va tras «#», que el navegador nunca manda al
// servidor ni a otras webs; se cambia por la cookie y se borra de la dirección al momento.
async function loginFromLink() {
  const m = /^#llave=([A-Za-z0-9_-]{10,200})$/.exec(location.hash);
  if (!m) return;
  history.replaceState(null, "", location.pathname);
  try {
    await post("/api/login", { key: m[1] });
  } catch (e) { say(e.message); }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await post("/api/login", { key: $("key").value });
      $("key").value = "";
      await load();
    } catch (e) { say(e.message); }
  });
  $("logout").addEventListener("click", async () => {
    await post("/api/logout").catch(() => {});
    showLogin();
  });
  $("add-form").addEventListener("submit", addTask);
  $("redo").addEventListener("click", redoBriefing);
  $("brain-form").addEventListener("submit", askBrain);
  $("rev-pause").addEventListener("click", (ev) => revAction(ev.currentTarget, "/api/revision/descanso",
    { activo: !(rev && rev.pausa) }, (out) => (out.pausa ? "Hoy, descanso. La racha queda en pausa." : "Descanso quitado.")));
  $("rev-hide").addEventListener("click", (ev) => revAction(ev.currentTarget, "/api/revision/ocultar",
    { ocultar: !(rev && rev.oculto) }));
  $("rev-week-read").addEventListener("click", (ev) => revAction(ev.currentTarget, "/api/revision/semana/leida", {},
    () => "Semana leída."));
  document.addEventListener("visibilitychange", () => { if (!document.hidden && day && $("login").hidden) reload(); });
  await loginFromLink();
  await load();
});
