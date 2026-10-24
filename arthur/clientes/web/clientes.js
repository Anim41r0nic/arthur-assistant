// Página de clientes. Todo el texto se pone con textContent (nunca innerHTML): un nombre, una nota o un mensaje
// con «<script>» se ve como texto, no se ejecuta. Las peticiones que cambian algo llevan X-Arthur (protección
// CSRF, como el panel y Apuntes). Rutas de la página: #  (lista) · #c12 (ficha del C-0012) · #nuevo.
"use strict";

const ES = {
  skip: "Saltar al contenido", new: "Nuevo cliente", logout: "Salir",
  loginTitle: "Entrar a clientes",
  loginHelp: "Abre el enlace con llave que te dio ARTHUR o pega aquí la llave de este aparato. Solo hace falta una vez.",
  loginKey: "Llave del aparato", loginButton: "Entrar",
  listTitle: "Clientes", search: "Buscar", searchHint: "Nombre, correo, teléfono o C-0012", statusFilter: "Estado",
  allStatus: "Todos los estados", count: (n) => (n === 1 ? "1 cliente" : `${n} clientes`),
  emptyAll: "Todavía no hay clientes. Cuando alguien escriba desde la web, aparecerá aquí.",
  emptySearch: "Nadie coincide con la búsqueda.",
  unread: (n) => (n === 1 ? "1 mensaje sin leer" : `${n} mensajes sin leer`),
  possibleDup: "posible duplicado",
  back: "Todos los clientes", status: "Estado",
  dupText: "Puede ser la misma persona que", dupHelp: "Revisa las dos fichas y borra la que sobre.",
  history: "Mensajes", historyEmpty: "Todavía no hay mensajes.", reply: "Responder por correo",
  replySubject: "Re: tu mensaje en la web",
  addMessage: "Apuntar mensaje", via: "Por dónde", whatSaid: "Qué te ha dicho", save: "Guardar",
  data: "Datos", name: "Nombre", email: "Correo", phone: "Teléfono", saveData: "Guardar datos",
  notes: "Notas privadas", saveNotes: "Guardar notas", delete: "Borrar cliente",
  confirmDelete: (num) => `¿Borrar ${num} y todos sus mensajes? No se puede deshacer.`,
  deleted: (num) => `${num} borrado.`, saved: "Guardado.", statusSaved: (s) => `Estado: ${s}.`,
  newTitle: "Nuevo cliente", newHelp: "Si el correo o el teléfono ya son de alguien, se abre su ficha.",
  origin: "Cómo ha llegado", firstMessage: "Primer mensaje (opcional)", create: "Crear",
  since: (d) => `Desde el ${d}`, lastContact: (d) => `último contacto: ${d}`,
  notFound: "Ese cliente ya no existe.", needLogin: "Tienes que entrar primero.", error: (c) => `Error ${c}`,
  fields: {
    name: "Escribe el nombre (hasta 100 caracteres).", email: "El correo no es válido.",
    phone: "El teléfono no es válido: usa solo cifras, espacios o guiones, con un mínimo de 9 cifras.",
    text: "Escribe el mensaje (hasta 2000 caracteres).", notes: "Las notas son demasiado largas (hasta 4000 caracteres).",
    contact: "Hace falta un correo o un teléfono.", duplicado: "Ese correo o teléfono ya es de otro cliente.",
  },
  estados: { nuevo: "Nuevo", hablando: "Hablando", presupuesto: "Presupuesto", cliente: "Cliente",
             descartado: "Descartado" },
  origenes: { web: "Web", telefono: "Teléfono", whatsapp: "WhatsApp", persona: "En persona", otro: "Otro" },
};
const CA = {
  skip: "Salta al contingut", new: "Client nou", logout: "Surt",
  loginTitle: "Entra a clients",
  loginHelp: "Obre l'enllaç amb clau que et va donar ARTHUR o enganxa aquí la clau d'aquest aparell. Només cal fer-ho un cop.",
  loginKey: "Clau de l'aparell", loginButton: "Entra",
  listTitle: "Clients", search: "Cerca", searchHint: "Nom, correu, telèfon o C-0012", statusFilter: "Estat",
  allStatus: "Tots els estats", count: (n) => (n === 1 ? "1 client" : `${n} clients`),
  emptyAll: "Encara no hi ha clients. Quan algú escrigui des del web, apareixerà aquí.",
  emptySearch: "Ningú no coincideix amb la cerca.",
  unread: (n) => (n === 1 ? "1 missatge sense llegir" : `${n} missatges sense llegir`),
  possibleDup: "possible duplicat",
  back: "Tots els clients", status: "Estat",
  dupText: "Pot ser la mateixa persona que", dupHelp: "Revisa les dues fitxes i esborra la que sobri.",
  history: "Missatges", historyEmpty: "Encara no hi ha missatges.", reply: "Respon per correu",
  replySubject: "Re: el teu missatge al web",
  addMessage: "Anota un missatge", via: "Per on", whatSaid: "Què t'ha dit", save: "Desa",
  data: "Dades", name: "Nom", email: "Correu", phone: "Telèfon", saveData: "Desa les dades",
  notes: "Notes privades", saveNotes: "Desa les notes", delete: "Esborra el client",
  confirmDelete: (num) => `Vols esborrar ${num} i tots els seus missatges? No es pot desfer.`,
  deleted: (num) => `${num} esborrat.`, saved: "Desat.", statusSaved: (s) => `Estat: ${s}.`,
  newTitle: "Client nou", newHelp: "Si el correu o el telèfon ja són d'algú, s'obre la seva fitxa.",
  origin: "Com ha arribat", firstMessage: "Primer missatge (opcional)", create: "Crea",
  since: (d) => `Des del ${d}`, lastContact: (d) => `darrer contacte: ${d}`,
  notFound: "Aquest client ja no existeix.", needLogin: "Primer has d'entrar.", error: (c) => `Error ${c}`,
  fields: {
    name: "Escriu el nom (fins a 100 caràcters).", email: "El correu no és vàlid.",
    phone: "El telèfon no és vàlid: fes servir només xifres, espais o guions, amb un mínim de 9 xifres.",
    text: "Escriu el missatge (fins a 2000 caràcters).", notes: "Les notes són massa llargues (fins a 4000 caràcters).",
    contact: "Cal un correu o un telèfon.", duplicado: "Aquest correu o telèfon ja és d'un altre client.",
  },
  estados: { nuevo: "Nou", hablando: "En conversa", presupuesto: "Pressupost", cliente: "Client",
             descartado: "Descartat" },
  origenes: { web: "Web", telefono: "Telèfon", whatsapp: "WhatsApp", persona: "En persona", otro: "Altres" },
};
const EN = {
  skip: "Skip to content", new: "New client", logout: "Log out",
  loginTitle: "Sign in to Clients",
  loginHelp: "Open the key link ARTHUR gave you, or paste this device's key here. You only need to do this once.",
  loginKey: "Device key", loginButton: "Sign in",
  listTitle: "Clients", search: "Search", searchHint: "Name, email, phone or C-0012", statusFilter: "Status",
  allStatus: "All statuses", count: (n) => (n === 1 ? "1 client" : `${n} clients`),
  emptyAll: "No clients yet. When someone writes from the website, they'll show up here.",
  emptySearch: "Nobody matches your search.",
  unread: (n) => (n === 1 ? "1 unread message" : `${n} unread messages`),
  possibleDup: "possible duplicate",
  back: "All clients", status: "Status",
  dupText: "This may be the same person as", dupHelp: "Check both records and delete the one you don't need.",
  history: "Messages", historyEmpty: "No messages yet.", reply: "Reply by email",
  replySubject: "Re: your message on the website",
  addMessage: "Log a message", via: "Channel", whatSaid: "What they said", save: "Save",
  data: "Details", name: "Name", email: "Email", phone: "Phone", saveData: "Save details",
  notes: "Private notes", saveNotes: "Save notes", delete: "Delete client",
  confirmDelete: (num) => `Delete ${num} and all their messages? This can't be undone.`,
  deleted: (num) => `${num} deleted.`, saved: "Saved.", statusSaved: (s) => `Status: ${s}.`,
  newTitle: "New client", newHelp: "If the email or phone already belongs to someone, their record opens instead.",
  origin: "How they found you", firstMessage: "First message (optional)", create: "Create",
  since: (d) => `Since ${d}`, lastContact: (d) => `last contact: ${d}`,
  notFound: "This client no longer exists.", needLogin: "You need to sign in first.", error: (c) => `Error ${c}`,
  fields: {
    name: "Enter a name (up to 100 characters).", email: "That email isn't valid.",
    phone: "That phone number isn't valid: use only digits, spaces or hyphens, with at least 9 digits.",
    text: "Write the message (up to 2000 characters).", notes: "The notes are too long (up to 4000 characters).",
    contact: "An email or a phone number is required.", duplicado: "That email or phone already belongs to another client.",
  },
  estados: { nuevo: "New", hablando: "In talks", presupuesto: "Quote", cliente: "Client",
             descartado: "Dismissed" },
  origenes: { web: "Website", telefono: "Phone", whatsapp: "WhatsApp", persona: "In person", otro: "Other" },
};
const I18N = { es: ES, ca: CA, en: EN };

const LANG = (() => {
  const want = (navigator.language || "es").slice(0, 2).toLowerCase();
  return I18N[want] ? want : "es";
})();
const T = I18N[LANG];
const LOCALE = { es: "es-ES", ca: "ca-ES", en: "en-GB" }[LANG];
const ZONA = "Europe/Madrid";
const ESTADOS = ["nuevo", "hablando", "presupuesto", "cliente", "descartado"];
const MANUALES = ["telefono", "whatsapp", "persona", "otro"];  // la «web» solo la pone el chat

const $ = (id) => document.getElementById(id);
let current = null;      // la ficha abierta
let searchTimer = null;

async function api(path, options = {}) {
  const init = { credentials: "same-origin", ...options, headers: { ...(options.headers || {}) } };
  if (init.method === "POST") {
    init.headers["X-Arthur"] = "1";
    if (init.body !== undefined) init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch (_) { /* sin cuerpo JSON */ }
  if (res.status === 401) {
    showLogin();
    const err = new Error(data.error || T.needLogin);
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    const err = new Error((data.field && T.fields[data.field]) || data.error || T.error(res.status));
    err.status = res.status;
    err.field = data.field;
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

function options(select, values, labels, first) {
  select.replaceChildren();
  if (first) select.append(new Option(first, ""));
  for (const v of values) select.append(new Option(labels[v] || v, v));
}

const fecha = (iso) => new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", year: "numeric",
  hour: "2-digit", minute: "2-digit", timeZone: ZONA }).format(new Date(iso));
const dia = (iso) => new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", year: "numeric",
  timeZone: ZONA }).format(new Date(iso));

function estadoTag(estado) {
  return el("span", `a-tag c-estado--${ESTADOS.includes(estado) ? estado : "nuevo"}`, T.estados[estado] || estado);
}

// --- errores de formulario: marcan la casilla que falla y dicen por qué ---
function clearErrors(form) {
  for (const p of form.querySelectorAll(".a-field__error")) { p.hidden = true; p.textContent = ""; }
  for (const i of form.querySelectorAll("[aria-invalid]")) i.removeAttribute("aria-invalid");
}

function showError(form, prefix, err) {
  const map = { name: "nombre", email: "email", phone: "telefono", text: "texto", notes: "notas" };
  const input = map[err.field] && $(`${prefix}-${map[err.field]}`);
  const box = input ? $(`${input.id}-error`) : form.querySelector(":scope > .a-field__error");
  if (input) { input.setAttribute("aria-invalid", "true"); input.focus(); }
  if (box) { box.textContent = err.message; box.hidden = false; } else say(err.message);
}

// --- vistas ---
function showOnly(id) {
  for (const v of ["login", "list-view", "card-view", "new-view"]) $(v).hidden = v !== id;
  const inside = id !== "login";
  $("logout").hidden = !inside;
  $("new-link").hidden = !inside || id === "new-view";
}

function showLogin() { showOnly("login"); }

async function route() {
  const card = /^#c(\d{1,9})$/.exec(location.hash);
  try {
    if (card) return await openCard(Number(card[1]));
    if (location.hash === "#nuevo") return openNew();
    return await openList();
  } catch (e) {
    if (e.status === 404) { say(T.notFound); location.hash = ""; return; }
    // Sin sesión ya se enseña la pantalla de entrar: un aviso de «hace falta una llave» encima solo confunde.
    if (e.status === 401) return;
    say(e.message);
  }
}

// --- lista ---
async function openList() {
  const q = $("q").value.trim(), estado = $("estado-filter").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (estado) params.set("estado", estado);
  const query = params.toString();
  const data = await api(`/api/clientes${query ? `?${query}` : ""}`);
  showOnly("list-view");
  renderList(data.clientes, Boolean(q || estado));
}

function renderList(clientes, filtered) {
  const list = $("list");
  list.replaceChildren();
  $("list-count").textContent = T.count(clientes.length);
  for (const c of clientes) {
    const a = el("a", "c-row");
    a.href = `#c${c.id}`;
    const name = el("span", "c-row__name");
    if (c.sin_leer > 0) {
      const dot = el("span", "c-unread");
      dot.setAttribute("role", "img");
      dot.setAttribute("aria-label", T.unread(c.sin_leer));
      name.append(dot);
    }
    name.append(el("span", "", c.nombre));
    const meta = el("span", "c-row__meta");
    meta.append(el("span", "a-data", c.numero), el("span", "", T.lastContact(dia(c.ultimo_contacto))));
    if (c.posible_duplicado) meta.append(el("span", "a-tag a-state--warning", T.possibleDup));
    const state = estadoTag(c.estado);
    state.classList.add("c-row__state");
    a.append(name, state, meta);
    const li = el("li");
    li.append(a);
    list.append(li);
  }
  const empty = $("list-empty");
  empty.textContent = filtered ? T.emptySearch : T.emptyAll;
  empty.hidden = clientes.length > 0;
  list.hidden = clientes.length === 0;
}

// --- ficha ---
async function openCard(id) {
  const ficha = await api(`/api/clientes/${id}`);  // al abrirla, sus mensajes quedan leídos
  showOnly("card-view");
  renderCard(ficha);
  $("main").focus();
}

function renderCard(f) {
  current = f;
  $("card-number").textContent = f.numero;
  $("card-title").textContent = f.nombre;
  $("card-meta").textContent = `${T.origenes[f.origen] || f.origen} · ${T.since(dia(f.alta))}`;
  $("card-status").value = f.estado;
  const dup = $("card-dup");
  dup.hidden = !f.posible_duplicado;
  if (f.posible_duplicado) {
    const link = $("card-dup-link");
    link.href = `#c${f.posible_duplicado}`;
    link.textContent = `C-${String(f.posible_duplicado).padStart(4, "0")}`;
  }
  const reply = $("reply");
  reply.hidden = !f.email;
  // mailto: Marc escribe y envía desde su correo; la página nunca manda nada sola.
  if (f.email) reply.href = `mailto:${encodeURIComponent(f.email).replace("%40", "@")}?subject=${encodeURIComponent(T.replySubject)}`;
  const history = $("history");
  history.replaceChildren();
  for (const m of f.mensajes) {
    const li = el("li", "c-msg");
    const meta = el("p", "c-msg__meta");
    const time = el("time", "", fecha(m.fecha));
    time.dateTime = m.fecha;
    meta.append(time, el("span", "", T.origenes[m.origen] || m.origen));
    if (m.nombre && m.nombre !== f.nombre) meta.append(el("span", "", m.nombre));
    if (m.idioma) meta.append(el("span", "a-data", m.idioma));
    li.append(meta, el("p", "c-msg__text", m.texto));
    history.append(li);
  }
  $("history-empty").hidden = f.mensajes.length > 0;
  $("d-nombre").value = f.nombre;
  $("d-email").value = f.email;
  $("d-telefono").value = f.telefono ? `+${f.telefono}` : "";
  $("d-notas").value = f.notas;
  for (const form of ["data-form", "notes-form", "note-form"]) clearErrors($(form));
}

async function saveCard(form, prefix, body) {
  clearErrors(form);
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    renderCard(await post(`/api/clientes/${current.id}`, body));
    say(T.saved);
  } catch (e) {
    showError(form, prefix, e);
  } finally {
    button.disabled = false;
  }
}

async function changeStatus() {
  const estado = $("card-status").value;
  try {
    renderCard(await post(`/api/clientes/${current.id}`, { estado }));
    say(T.statusSaved(T.estados[estado]));
  } catch (e) {
    $("card-status").value = current.estado;
    say(e.message);
  }
}

async function addNote(ev) {
  ev.preventDefault();
  const form = $("note-form");
  clearErrors(form);
  try {
    renderCard(await post(`/api/clientes/${current.id}/mensaje`,
      { texto: $("note-text").value, origen: $("note-origin").value }));
    $("note-text").value = "";
    say(T.saved);
  } catch (e) {
    if (e.field === "text") {
      $("note-text").setAttribute("aria-invalid", "true");
      $("note-text-error").textContent = e.message;
      $("note-text-error").hidden = false;
    } else say(e.message);
  }
}

async function deleteCard() {
  if (!current || !window.confirm(T.confirmDelete(current.numero))) return;
  try {
    await post(`/api/clientes/${current.id}/borrar`, {});
    say(T.deleted(current.numero));
    current = null;
    location.hash = "";
  } catch (e) { say(e.message); }
}

// --- nuevo ---
function openNew() {
  showOnly("new-view");
  clearErrors($("new-form"));
  $("n-nombre").focus();
}

async function createClient(ev) {
  ev.preventDefault();
  const form = $("new-form");
  clearErrors(form);
  const body = { nombre: $("n-nombre").value, email: $("n-email").value, telefono: $("n-telefono").value,
                 origen: $("n-origen").value, texto: $("n-texto").value };
  try {
    const { id } = await post("/api/clientes", body);
    form.reset();
    location.hash = `#c${id}`;
  } catch (e) { showError(form, "n", e); }
}

// --- entrar ---
// Entrar con el enlace que da ARTHUR (…/#llave=arthur_…): la llave va tras «#», que el navegador nunca manda al
// servidor ni a otras webs; se cambia por la cookie y se borra de la dirección al momento.
async function loginFromLink() {
  const m = /^#llave=([A-Za-z0-9_-]{10,200})$/.exec(location.hash);
  if (!m) return;
  history.replaceState(null, "", location.pathname);
  try { await post("/api/login", { key: m[1] }); } catch (e) { say(e.message); }
}

function translate() {
  document.documentElement.lang = LANG;
  for (const node of document.querySelectorAll("[data-t]")) {
    const text = T[node.dataset.t];
    if (typeof text === "string") node.textContent = text;
  }
  for (const node of document.querySelectorAll("[data-tp]")) node.placeholder = T[node.dataset.tp];
  options($("estado-filter"), ESTADOS, T.estados, T.allStatus);
  options($("card-status"), ESTADOS, T.estados);
  options($("note-origin"), MANUALES, T.origenes);
  options($("n-origen"), MANUALES, T.origenes);
}

document.addEventListener("DOMContentLoaded", async () => {
  translate();
  $("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await post("/api/login", { key: $("key").value });
      $("key").value = "";
      await route();
    } catch (e) { say(e.message); }
  });
  $("logout").addEventListener("click", async () => {
    await post("/api/logout").catch(() => {});
    showLogin();
  });
  $("filter").addEventListener("submit", (ev) => { ev.preventDefault(); openList().catch((e) => say(e.message)); });
  $("q").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => openList().catch((e) => say(e.message)), 250);
  });
  $("estado-filter").addEventListener("change", () => openList().catch((e) => say(e.message)));
  $("card-status").addEventListener("change", changeStatus);
  $("data-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveCard($("data-form"), "d", { nombre: $("d-nombre").value, email: $("d-email").value,
                                    telefono: $("d-telefono").value });
  });
  $("notes-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveCard($("notes-form"), "d", { notas: $("d-notas").value });
  });
  $("note-form").addEventListener("submit", addNote);
  $("delete").addEventListener("click", deleteCard);
  $("new-form").addEventListener("submit", createClient);
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("list-view").hidden) openList().catch(() => {});
  });
  await loginFromLink();
  await route();
});
