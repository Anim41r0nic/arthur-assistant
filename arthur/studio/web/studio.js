// Página de revisión de ARTHUR Studio. Todo el texto se pone con textContent (nunca innerHTML): un título con
// «<script>» se ve como texto, no se ejecuta. Las peticiones que cambian algo llevan X-Arthur (protección CSRF).
"use strict";

const $ = (id) => document.getElementById(id);
let current = null; // la revisión abierta
let chosenDay = null; // el día elegido arriba («AAAA-MM-DD»); se mantiene al volver de un vídeo

async function api(path, options = {}) {
  const init = { credentials: "same-origin", ...options, headers: { ...(options.headers || {}) } };
  if (init.method === "POST") {
    init.headers["X-Arthur"] = "1";
    if (init.body !== undefined) init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch (_) { /* sin cuerpo JSON */ }
  if (res.status === 401) { show("login"); throw new Error(data.error || "Hace falta entrar"); }
  if (!res.ok) throw new Error(data.error || `Error ${res.status}`);
  return data;
}

function show(section) {
  for (const id of ["login", "list", "detail"]) $(id).hidden = id !== section;
  $("back").hidden = section !== "detail";
  $("logout").hidden = section === "login";
  if (section !== "detail") { const p = $("player"); p.pause(); p.removeAttribute("src"); p.load(); }
}

function say(text) {
  const box = $("message");
  box.textContent = text;
  box.hidden = false;
  clearTimeout(say.timer);
  say.timer = setTimeout(() => { box.hidden = true; }, 5000);
}

function badge(review) {
  const span = document.createElement("span");
  span.className = `badge s-${review.status}`;
  span.textContent = review.status_label;
  return span;
}

// «2026-10-13T17:15:17» → «2026-10-13». La hora la pone el servidor (hora de España).
const dayOf = (review) => review.created_at.slice(0, 10);

function dayLabel(day) {
  const date = new Date(`${day}T12:00:00`);
  const today = new Date();
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  if (day === iso(today)) return "Hoy";
  if (day === iso(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1))) return "Ayer";
  return date.toLocaleDateString("es-ES", { weekday: "short", day: "numeric", month: "short" });
}

function renderDays(days, byDay) {
  const nav = $("days");
  nav.replaceChildren();
  for (const day of days) {
    const reviews = byDay.get(day);
    const waiting = reviews.filter((r) => r.status === "pending").length;
    const button = document.createElement("button");
    button.type = "button";
    button.className = day === chosenDay ? "day chosen" : "day";
    button.setAttribute("aria-pressed", String(day === chosenDay));
    const name = document.createElement("span");
    name.className = "day-name";
    name.textContent = dayLabel(day);
    const count = document.createElement("span");
    count.className = "day-count";
    count.textContent = `${reviews.length} vídeo${reviews.length === 1 ? "" : "s"}`;
    button.append(name, count);
    if (waiting) {
      const dot = document.createElement("span");
      dot.className = "day-waiting";
      dot.textContent = `${waiting} por revisar`;
      button.append(dot);
    }
    button.addEventListener("click", () => { chosenDay = day; renderDays(days, byDay); renderCards(byDay.get(day)); });
    nav.append(button);
  }
  nav.querySelector(".chosen")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function renderCards(reviews) {
  const ul = $("reviews");
  ul.replaceChildren();
  for (const r of reviews) {
    const li = document.createElement("li");
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = `/media/${r.id}/thumb`;
    const body = document.createElement("div");
    body.className = "body";
    const title = document.createElement("p");
    title.className = "title";
    title.textContent = r.title;
    const info = document.createElement("p");
    info.className = "muted small";
    info.textContent = `${r.kind === "corto" ? "▮ Corto · " : ""}${r.channel} · ${r.created_at.slice(11, 16)} `;
    info.append(badge(r));
    body.append(title, info);
    li.append(img, body);
    li.addEventListener("click", () => openReview(r.id));
    ul.append(li);
  }
}

async function loadList() {
  const data = await api("/api/reviews");
  // El servidor ya los da con lo que espera decisión primero: dentro de cada día se respeta ese orden.
  const byDay = new Map();
  for (const r of data.reviews) {
    if (!byDay.has(dayOf(r))) byDay.set(dayOf(r), []);
    byDay.get(dayOf(r)).push(r);
  }
  const days = [...byDay.keys()].sort().reverse(); // el más reciente primero
  if (!byDay.has(chosenDay)) chosenDay = days[0] || null;
  $("empty").hidden = days.length > 0;
  $("days").hidden = days.length === 0;
  renderDays(days, byDay);
  renderCards(chosenDay ? byDay.get(chosenDay) : []);
  show("list");
}

async function openReview(id) {
  const r = await api(`/api/reviews/${id}`);
  current = r;
  $("d-status").replaceChildren(badge(r));
  $("d-title").textContent = r.title;
  $("d-channel").textContent = r.kind === "corto" ? `${r.channel} (corto vertical)` : r.channel;
  $("player").classList.toggle("vertical", r.kind === "corto");
  $("d-length").textContent = r.duration ? `${Math.floor(r.duration / 60)} min ${Math.round(r.duration % 60)} s` : "";
  $("player").src = `/media/${r.id}/video`;
  $("download").href = `/media/${r.id}/download`;
  $("download").textContent = r.kind === "corto" ? "⬇ Descargar para Instagram / TikTok" : "⬇ Descargar el vídeo";
  $("d-thumb").src = `/media/${r.id}/thumb`;
  $("d-description").textContent = r.description || r.problem || "";
  $("d-tags").textContent = (r.tags || []).join(" · ");
  const sources = $("d-sources");
  sources.replaceChildren();
  for (const s of r.sources || []) { const li = document.createElement("li"); li.textContent = s; sources.append(li); }
  const warnings = $("d-warnings-list");
  warnings.replaceChildren();
  for (const w of r.warnings || []) { const li = document.createElement("li"); li.textContent = w; warnings.append(li); }
  $("d-warnings").hidden = !(r.warnings || []).length;
  $("d-auto").hidden = !(r.autopilot && r.status === "pending");
  $("d-auto").textContent = (r.warnings || []).length
    ? "Tiene dudas: NO se publica solo. Compruébalas; para aprobarlo tendrás que escribir PUBLICAR."
    : "Si no haces nada, se publica solo y en público a las 18:00. Descártalo o pide cambios antes si no te gusta.";
  $("d-sha").textContent = r.video_sha256;
  const pending = r.status === "pending";
  $("actions").hidden = !pending;
  $("changes-form").hidden = true;
  $("retry").hidden = r.status !== "failed";
  const result = $("d-result");
  result.replaceChildren();
  if (r.youtube_id) {
    const a = document.createElement("a");
    a.href = `https://studio.youtube.com/video/${encodeURIComponent(r.youtube_id)}/edit`;
    a.rel = "noopener noreferrer";
    a.target = "_blank";
    a.textContent = "Abrir en YouTube Studio (está en privado)";
    result.append(a);
  }
  if (r.error) {
    const p = document.createElement("p");
    p.className = r.status === "failed" ? "s-failed" : "muted";
    p.textContent = r.error;
    result.append(p);
  }
  if (r.status === "approved") result.append(Object.assign(document.createElement("p"),
    { className: "muted", textContent: "Subiendo a YouTube… recarga en un rato." }));
  if (r.feedback) say(`Cambios pedidos: ${r.feedback}`);
  show("detail");
  window.scrollTo(0, 0);
}

async function decide(action, body) {
  const buttons = document.querySelectorAll("#actions button, #changes-form button, #retry");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const r = await api(`/api/reviews/${current.id}/${action}`, {
      method: "POST", body: body === undefined ? undefined : JSON.stringify(body),
    });
    say(`Hecho: ${r.status_label}.`);
    await openReview(current.id);
  } catch (e) {
    say(e.message);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/login", { method: "POST", body: JSON.stringify({ key: $("key").value }) });
      $("key").value = "";
      await loadList();
    } catch (e) { say(e.message); }
  });
  $("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    show("login");
  });
  $("back").addEventListener("click", () => loadList().catch((e) => say(e.message)));
  $("approve").addEventListener("click", () => {
    const where = `Se subirá al canal ${current.channel} ${current.autopilot ? "en PÚBLICO" : "en PRIVADO"}.`;
    const doubts = (current.warnings || []).length;
    if (doubts) {
      // Con dudas no basta un toque: hay que escribirlo (el servidor también lo comprueba).
      const word = prompt(`«${current.title}» tiene ${doubts} duda${doubts === 1 ? "" : "s"} del revisor.\n\n` +
                          `${where}\n\nSi ya las has comprobado en las fuentes, escribe PUBLICAR:`);
      if (word === null) return;
      if (word.trim().toUpperCase() !== "PUBLICAR") { say("No se ha aprobado: no has escrito PUBLICAR."); return; }
      decide("approve", { video_sha256: current.video_sha256, confirm: word });
      return;
    }
    const ok = confirm(`¿Aprobar «${current.title}»?\n\n${where} ` +
                       "Si algo cambia después, habrá que volver a aprobarlo.");
    if (ok) decide("approve", { video_sha256: current.video_sha256 });
  });
  $("changes").addEventListener("click", () => { $("changes-form").hidden = false; $("changes-text").focus(); });
  $("changes-cancel").addEventListener("click", () => { $("changes-form").hidden = true; });
  $("changes-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    decide("changes", { text: $("changes-text").value });
    $("changes-text").value = "";
  });
  $("retry").addEventListener("click", () => decide("retry"));
  $("discard").addEventListener("click", () => {
    if (confirm(`¿Descartar «${current.title}»? No se publicará.`)) decide("discard");
  });
  loadList().catch(() => show("login"));
});
