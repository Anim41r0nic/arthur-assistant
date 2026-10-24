// Página de revisión de ARTHUR Studio. Todo el texto se pone con textContent (nunca innerHTML): un título con
// «<script>» se ve como texto, no se ejecuta. Las peticiones que cambian algo llevan X-Arthur (protección CSRF).
"use strict";

const $ = (id) => document.getElementById(id);
let current = null; // la revisión abierta
let chosenDay = null; // el día elegido arriba («AAAA-MM-DD»); se mantiene al volver de un vídeo
let chosenProfile = null; // el canal elegido al entrar («estoicismo_es»…); «Vídeos» enseña solo los suyos
const PROFILE_KEY = "studio.canal"; // dónde se recuerda el canal en ESTE aparato (solo comodidad, no seguridad)
const accountProfiles = ["estoicismo_es", "estoicismo_en", "frutinovelas"];
const accountStates = {
  disconnected: "Sin conectar",
  unverified: "Permiso guardado · sin comprobar",
  connected: "Conectada",
  reconnect: "Requiere reconexión",
  wrong_account: "Cuenta equivocada",
  unavailable: "No se pudo comprobar",
};

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
  if (!res.ok) {
    const error = new Error(data.error || `Error ${res.status}`);
    error.status = res.status; // p. ej. 400 si el canal recordado ya no existe
    throw error;
  }
  return data;
}

// El último canal elegido se recuerda en el aparato. localStorage puede fallar (modo privado, datos bloqueados):
// entonces no se recuerda y se pregunta otra vez, sin romper la página.
function rememberProfile(profile) {
  try { localStorage.setItem(PROFILE_KEY, profile); } catch (_) { /* sin almacenamiento: se preguntará otra vez */ }
}

function recalledProfile() {
  let value = null;
  try { value = localStorage.getItem(PROFILE_KEY); } catch (_) { /* sin almacenamiento */ }
  return accountProfiles.includes(value) ? value : null; // algo raro guardado: como si no hubiera nada
}

function forgetProfile() {
  try { localStorage.removeItem(PROFILE_KEY); } catch (_) { /* sin almacenamiento */ }
}

function show(section) {
  for (const id of ["login", "channels", "list", "detail", "accounts"]) $(id).hidden = id !== section;
  $("back").hidden = section !== "detail";
  $("logout").hidden = section === "login";
  $("main-nav").hidden = section === "login" || section === "detail";
  // Elegir canal es la entrada de «Vídeos»: la pestaña sigue marcada.
  const videos = section === "list" || section === "channels";
  for (const [id, selected] of [["nav-videos", videos], ["nav-accounts", section === "accounts"]]) {
    if (selected) $(id).setAttribute("aria-current", "page");
    else $(id).removeAttribute("aria-current");
  }
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
    const button = document.createElement("button");
    button.type = "button";
    button.className = "review-card";
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
    info.textContent = `${r.kind === "corto" ? "▮ Corto · " : ""}${r.imported ? "Importado · " : ""}` +
      `${r.channel} · ${r.created_at.slice(11, 16)} `;
    info.append(badge(r));
    body.append(title, info);
    button.append(img, body);
    button.addEventListener("click", () => openReview(r.id).catch((e) => say(e.message)));
    li.append(button);
    ul.append(li);
  }
}

function accountDate(value) {
  if (!value) return "Aún no comprobada";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Fecha de comprobación no disponible";
  return `Comprobada: ${date.toLocaleString("es-ES", { dateStyle: "medium", timeStyle: "short" })}`;
}

// Un símbolo además del color: el estado se entiende igual sin distinguir colores (y lo lee el lector de pantalla).
const accountMarks = { connected: "✓", unverified: "○", disconnected: "○", reconnect: "!", wrong_account: "✕", unavailable: "?" };

function connectLabel(status) {
  if (status === "disconnected") return "Conectar";
  if (status === "reconnect" || status === "wrong_account") return "Reconectar";
  return "Cómo reconectar";
}

function codeLine(label, text) {
  const p = document.createElement("p");
  p.className = "account-command";
  const strong = document.createElement("span");
  strong.className = "muted small";
  strong.textContent = label;
  const code = document.createElement("code");
  code.textContent = text;
  p.append(strong, code);
  return p;
}

function renderAccounts(accounts, focusProfile = null) {
  const list = $("account-list");
  list.replaceChildren();
  const byProfile = new Map(accounts.map((account) => [account.profile, account]));
  for (const profile of accountProfiles) {
    const account = byProfile.get(profile);
    if (!account) continue;
    const known = accountStates[account.status] ? account.status : "unavailable";
    const item = document.createElement("li");
    item.className = "account-item";
    const head = document.createElement("div");
    head.className = "account-head";
    const heading = document.createElement("h3");
    heading.textContent = account.name;
    const status = document.createElement("p");
    status.className = `account-state state-${known}`;
    const mark = document.createElement("span");
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = accountMarks[known];
    status.append(mark, ` ${accountStates[known]}`);
    head.append(heading, status);
    const handle = document.createElement("p");
    handle.className = "account-handle";
    handle.textContent = account.channel_id ? `${account.handle} · ID ${account.channel_id}` : account.handle;
    const date = document.createElement("p");
    date.className = "muted small account-date";
    date.textContent = accountDate(account.checked_at);
    item.append(head, handle, date);
    if (account.detail) {
      const detail = document.createElement("p");
      detail.className = "account-detail";
      detail.textContent = account.detail;
      item.append(detail);
    }
    if (account.publishing === false) {
      const off = document.createElement("p");
      off.className = "account-flag";
      off.textContent = "Publicación desactivada: conectar este canal no crea ni sube vídeos.";
      item.append(off);
    }
    const actions = document.createElement("div");
    actions.className = "account-actions";
    const check = document.createElement("button");
    check.type = "button";
    check.className = "secondary";
    check.dataset.profile = profile;
    check.textContent = "Comprobar conexión";
    check.setAttribute("aria-label", `Comprobar conexión de ${account.name}`);
    check.addEventListener("click", async () => {
      check.disabled = true;
      check.textContent = "Comprobando…";
      try {
        // Solo pregunta a Google de qué canal es el permiso: no sube nada.
        const updated = await api(`/api/accounts/${encodeURIComponent(profile)}/check`, { method: "POST" });
        byProfile.set(profile, updated);
        renderAccounts([...byProfile.values()], profile);
        say(`${updated.name}: ${accountStates[updated.status] || accountStates.unavailable}.`);
      } catch (e) {
        say(e.message);
        check.disabled = false;
        check.textContent = "Comprobar conexión";
      }
    });
    actions.append(check);
    // No hay botón que conecte desde aquí: Google vuelve a 127.0.0.1 del ordenador que pidió el permiso, y eso solo
    // lo resuelve la orden de la terminal. La página lo dice en vez de prometer lo que no hace.
    const connect = document.createElement("details");
    connect.className = "account-connect";
    const summary = document.createElement("summary");
    summary.textContent = connectLabel(account.status);
    const explanation = document.createElement("p");
    explanation.textContent = `Studio pedirá permiso a Google para ver qué canal es y subir vídeos (no puede borrar ni ` +
      `cambiar nada). La conexión se termina en la terminal del ordenador donde funciona Studio, no en esta página:`;
    const steps = document.createElement("p");
    steps.textContent = `Abre la dirección que te dé, entra con la cuenta del canal y elige ${account.handle}. Si el ` +
      "ordenador no tiene pantalla, al final te pedirá que pegues la dirección de la página que no carga. " +
      "Si Google devuelve otro canal, no se guarda nada. Studio nunca te pedirá la contraseña.";
    const after = document.createElement("p");
    after.textContent = "Al terminar, vuelve aquí y pulsa «Comprobar conexión».";
    connect.append(summary, explanation,
      codeLine("En el servidor (carpeta ~/arthur-server):", account.connect_command_server),
      codeLine("En el portátil:", account.connect_command), steps, after);
    item.append(actions, connect);
    list.append(item);
  }
  if (focusProfile) list.querySelector(`button[data-profile="${focusProfile}"]`)?.focus();
}

async function loadAccounts() {
  $("accounts-error").hidden = true;
  const data = await api("/api/accounts");
  if (!Array.isArray(data.accounts)) throw new Error("No se pudieron leer las cuentas.");
  renderAccounts(data.accounts);
  show("accounts");
}

function channelCard(channel) {
  const known = accountStates[channel.status] ? channel.status : "unavailable";
  const card = document.createElement("button");
  card.type = "button";
  card.className = "channel-card";
  card.dataset.profile = channel.profile;
  // Dentro de un botón solo puede haber texto en línea: <span> con display:block en vez de <h3> o <p>.
  const name = document.createElement("span");
  name.className = "channel-card-name";
  name.textContent = channel.name;
  const handle = document.createElement("span");
  handle.className = "channel-card-handle";
  handle.textContent = channel.handle;
  // El mismo estado que en «Cuentas», con símbolo y texto: no depende del color.
  const state = document.createElement("span");
  state.className = `account-state state-${known}`;
  const mark = document.createElement("span");
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = accountMarks[known];
  state.append(mark, ` ${accountStates[known]}`);
  const pending = document.createElement("span");
  const count = Number.isInteger(channel.pending) ? channel.pending : 0;
  pending.className = count ? "channel-card-pending waiting" : "channel-card-pending";
  pending.textContent = count ? `${count} vídeo${count === 1 ? "" : "s"} por revisar` : "Nada por revisar";
  card.append(name, handle, state, pending);
  if (channel.profile === chosenProfile) {
    card.setAttribute("aria-current", "true");
    const now = document.createElement("span");
    now.className = "channel-card-now";
    now.textContent = "Canal actual";
    card.append(now);
  }
  card.addEventListener("click", () => chooseProfile(channel.profile));
  return card;
}

async function loadChannels() {
  $("channels-error").hidden = true;
  const data = await api("/api/channels");
  if (!Array.isArray(data.channels)) throw new Error("No se pudieron leer los canales.");
  const list = $("channel-list");
  list.replaceChildren();
  const byProfile = new Map(data.channels.map((channel) => [channel.profile, channel]));
  for (const profile of accountProfiles) { // siempre en el orden de channels.ORDER
    const channel = byProfile.get(profile);
    if (!channel) continue;
    const li = document.createElement("li");
    li.append(channelCard(channel));
    list.append(li);
  }
  show("channels");
  // El foco va al canal actual (o al primero): con teclado o lector de pantalla se sigue desde ahí.
  (list.querySelector('[aria-current="true"]') || list.querySelector("button"))?.focus();
}

function showChannels() {
  loadChannels().catch((e) => {
    $("channels-error").textContent = `${e.message} Prueba a pulsar «Vídeos» de nuevo.`;
    $("channels-error").hidden = false;
    show("channels");
  });
}

function chooseProfile(profile) {
  if (profile !== chosenProfile) chosenDay = null; // los días son de cada canal
  chosenProfile = profile;
  rememberProfile(profile);
  loadList().catch((e) => say(e.message));
}

async function loadList() {
  if (!chosenProfile) { showChannels(); return; }
  let data;
  try {
    // El servidor filtra: el móvil no descarga los vídeos de los otros canales.
    data = await api(`/api/reviews?perfil=${encodeURIComponent(chosenProfile)}`);
  } catch (e) {
    if (e.status === 400) { // el canal recordado ya no existe: se olvida y se pregunta otra vez
      chosenProfile = null;
      forgetProfile();
      showChannels();
      return;
    }
    throw e;
  }
  $("channel-name").textContent = data.profile_name || chosenProfile;
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
  // Un vídeo abierto por su enlace (#id, desde un aviso) puede ser de otro canal: se pasa a ese canal, así «‹ Vídeos»
  // vuelve a la lista donde está este vídeo.
  if (r.profile && accountProfiles.includes(r.profile) && r.profile !== chosenProfile) {
    chosenProfile = r.profile;
    chosenDay = null;
    rememberProfile(r.profile);
  }
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
  $("d-auto").hidden = !((r.autopilot || r.imported) && r.status === "pending");
  // Un Short importado (hecho fuera de Studio) nunca se publica solo: el servidor tampoco lo aprueba a las 18:00.
  $("d-auto").textContent = r.imported
    ? "Importado: hecho fuera de Studio. NO se publica solo: solo si pulsas Aprobar, y sube en privado." +
      (r.publishing ? "" : " La publicación de este canal está desactivada: si lo apruebas, espera a que se active.")
    : (r.warnings || []).length
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
  if (r.status === "approved") result.append(Object.assign(document.createElement("p"), {
    className: "muted",
    textContent: r.publishing === false
      ? "Aprobado. La publicación de este canal está desactivada: se subirá cuando se active."
      : "Subiendo a YouTube… recarga en un rato." }));
  if (r.feedback) say(r.imported
    ? `Cambios apuntados: ${r.feedback}. Este vídeo se hizo fuera de Studio: rehazlo allí e impórtalo otra vez.`
    : `Cambios pedidos: ${r.feedback}. Se está rehaciendo (tarda un rato): el vídeo nuevo saldrá en la lista y ARTHUR te avisará.`);
  show("detail");
  window.scrollTo(0, 0);
}

// «#v12-frutinovelas» en la dirección (lo pone el núcleo en los avisos al móvil): abre el vídeo 12 ya en el canal
// Frutinovelas. También vale «#12» a secas (el canal sale entonces del propio vídeo). Luego se quita de la dirección
// para que recargar no lo vuelva a abrir.
function hashReview() {
  const match = /^#(?:v([1-9][0-9]{0,8})-([a-z_]{1,40})|([1-9][0-9]{0,8}))$/.exec(location.hash);
  if (!match) return null;
  if (match[3]) return { id: Number(match[3]), profile: null };
  return { id: Number(match[1]), profile: accountProfiles.includes(match[2]) ? match[2] : null };
}

async function openFromHash(link) {
  if (link.profile && link.profile !== chosenProfile) { // el canal del aviso, antes incluso de abrir el vídeo
    chosenProfile = link.profile;
    chosenDay = null;
    rememberProfile(link.profile);
  }
  await openReview(link.id);
  history.replaceState(null, "", location.pathname + location.search);
}

// Lo que se ve al entrar: el vídeo del enlace, o los vídeos del canal recordado, o elegir canal.
async function enter() {
  const link = hashReview();
  if (link !== null) {
    try { await openFromHash(link); return; } catch (e) {
      if (!$("login").hidden) throw e; // sin llave: a la pantalla de entrar
      say(e.message); // ese vídeo no existe: se sigue con lo normal
    }
  }
  chosenProfile = chosenProfile || recalledProfile(); // un enlace que falló ya pudo dejar su canal elegido
  if (chosenProfile) await loadList();
  else await loadChannels();
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
      await enter();
    } catch (e) { say(e.message); }
  });
  $("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    // Quien entre después en este aparato elige su canal.
    chosenProfile = null;
    forgetProfile();
    show("login");
  });
  $("back").addEventListener("click", () => loadList().catch((e) => say(e.message)));
  $("nav-videos").addEventListener("click", () => loadList().catch((e) => say(e.message)));
  $("change-channel").addEventListener("click", showChannels);
  window.addEventListener("hashchange", () => {
    const link = hashReview();
    if (link !== null) openFromHash(link).catch((e) => say(e.message));
  });
  $("nav-accounts").addEventListener("click", () => {
    loadAccounts().catch((e) => {
      $("accounts-error").textContent = `${e.message} Prueba a abrir Cuentas de nuevo.`;
      $("accounts-error").hidden = false;
      show("accounts");
    });
  });
  $("approve").addEventListener("click", () => {
    const where = current.publishing === false
      ? `La publicación en ${current.channel} está desactivada: queda aprobado y se subirá EN PRIVADO cuando se active.`
      : `Se subirá al canal ${current.channel} ${current.autopilot ? "en PÚBLICO" : "en PRIVADO"}.`;
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
  enter().catch(() => show("login"));
});
