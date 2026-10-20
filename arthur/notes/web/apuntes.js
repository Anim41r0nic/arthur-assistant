// Página de ARTHUR Apuntes. Todo el texto se pone con textContent (nunca innerHTML): un título con «<script>» se ve
// como texto, no se ejecuta. Las peticiones que cambian algo llevan X-Arthur (protección CSRF).
//
// Dónde estás va en la dirección (#c=3 una carpeta, #n=12 un apunte, #p=3 preguntar a la IA en la carpeta 3): así el botón «atrás» del móvil vuelve a la
// carpeta de antes en vez de cerrar la app, y al recargar sigues en el mismo sitio.
// #repaso abre la pestaña Repaso (repaso.js); es la dirección del aviso diario del móvil.
"use strict";

const $ = (id) => document.getElementById(id);
const ENGINES = { auto: "Automático", gemini: "Gemini", local: "Local", texto: "Texto del PDF" };
let current = null;   // el apunte abierto
let folder = null;    // la carpeta abierta (lo que devuelve /api/notes?carpeta=N)
let info = { engines: { gemini: true, local: true }, scans: false };
let poll = null;      // temporizador mientras se lee
let dirty = false;    // texto cambiado sin guardar
let tab = null;       // pestaña elegida a mano en el apunte abierto
let askFolder = 0;    // carpeta de «Preguntar a la IA» (donde se guarda la respuesta)
let askMark = null;   // apunte que viene marcado al llegar desde un apunte

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

const post = (path, body) => api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

function show(section) {
  for (const id of ["login", "list", "detail", "ask", "repaso"]) $(id).hidden = id !== section;
  $("back").hidden = section === "login" || section === "repaso" ||
    (section === "list" && folder && folder.folder.id === 0);
  $("logout").hidden = section === "login";
  $("top-tabs").hidden = section === "login";
  const [on, off] = section === "repaso" ? ["nav-repaso", "nav-apuntes"] : ["nav-apuntes", "nav-repaso"];
  $(on).setAttribute("aria-current", "page");
  $(off).removeAttribute("aria-current");
  if (section !== "detail") stopPolling();
}

function say(text) {
  const box = $("message");
  box.textContent = text;
  box.hidden = false;
  clearTimeout(say.timer);
  say.timer = setTimeout(() => { box.hidden = true; }, 6000);
}

function badge(note) {
  const span = document.createElement("span");
  span.className = `badge s-${note.status}`;
  span.textContent = note.status === "leyendo" ? `Leyendo ${note.done}/${note.pages}` :
    note.status === "borrador" && note.files ? "Sin pasar a texto" : note.status_label;
  return span;
}

function stopPolling() { clearTimeout(poll); poll = null; }

function engineNote(engine) {
  const photos = info.engines.gemini ? "Gemini (Google puede usarlas para mejorar sus productos)"
    : "el modelo local (falta la clave de Gemini en el servidor)";
  return {
    auto: `Los PDF que ya traen texto se leen en tu servidor, sin IA. Las fotos y los escaneos, con ${photos}.`,
    gemini: "Todo va a Gemini: lo mejor con letra a mano y fórmulas. Google puede usarlo para mejorar sus productos.",
    local: "Nada sale de tu servidor, pero tarda ~1-2 min por página y lee peor la letra a mano.",
    texto: "Solo el texto que ya trae el PDF, sin IA: las fórmulas salen como texto plano y las fotos no se leen.",
  }[engine] || "";
}

// --- dirección: qué se ve ---
function go(hash) {
  if (location.hash === hash) return route();
  location.hash = hash;  // dispara hashchange -> route()
}

async function route() {
  // «Ver en el apunte» del cerebro llega como #n12: se pasa a #n=12 sin dejar un paso más en el historial
  const old = /^#n(\d{1,9})$/.exec(location.hash);
  if (old) history.replaceState(null, "", `#n=${old[1]}`);
  const m = /^#(c|n|p)=(\d{1,9})$/.exec(location.hash);
  if (location.hash === "#repaso") {
    show("repaso");
    return openRepaso();
  }
  try {
    if (m && m[1] === "n") return await openNote(Number(m[2]));
    if (m && m[1] === "p") return await openAsk(Number(m[2]));
    await loadFolder(m ? Number(m[2]) : 0);
  } catch (e) {
    say(e.message);
    // un apunte o carpeta que ya no existe: a la principal (si falta la llave, api() ya enseña «Entrar»)
    if (m && location.hash !== "#c=0" && $("login").hidden) go("#c=0");
  }
}

function crumbs(box, path, last) {
  box.replaceChildren();
  const items = [{ id: 0, name: "Apuntes" }, ...path];
  items.forEach((f, i) => {
    if (i) box.append(" › ");
    const isLast = i === items.length - 1 && last;
    const el = document.createElement(isLast ? "span" : "button");
    el.textContent = f.name;
    if (!isLast) {
      el.type = "button";
      el.className = "link";
      el.addEventListener("click", () => go(`#c=${f.id}`));
    }
    box.append(el);
  });
}

// --- una carpeta ---
async function loadFolder(id) {
  const data = await api(`/api/notes?carpeta=${id}`);
  folder = data;
  info = data;
  current = null;
  crumbs($("crumbs"), data.path, true);
  $("f-name").textContent = id === 0 ? "Tus asignaturas" : data.folder.name;
  $("folder-actions").hidden = id === 0;
  $("show-ask").hidden = !data.tutor;
  $("show-ask").textContent = id === 0 ? "✨ Preguntar a la IA" : `✨ Preguntar a la IA sobre ${data.folder.name}`;
  $("delete-folder").disabled = data.folder.folders + data.folder.notes > 0;
  $("delete-folder").title = $("delete-folder").disabled ? "Solo se puede borrar vacía" : "";
  $("engine-note").textContent = engineNote($("new-engine").value);

  const fl = $("folders");
  fl.replaceChildren();
  for (const f of data.folders) {
    const li = document.createElement("li");
    li.className = "folder";
    const title = document.createElement("p");
    title.className = "title";
    title.textContent = `📁 ${f.name}`;
    const meta = document.createElement("p");
    meta.className = "muted small";
    meta.textContent = [f.folders ? `${f.folders} carpeta(s)` : "", f.notes ? `${f.notes} apunte(s)` : ""]
      .filter(Boolean).join(" · ") || "vacía";
    li.append(title, meta);
    li.addEventListener("click", () => go(`#c=${f.id}`));
    fl.append(li);
  }
  const ul = $("notes");
  ul.replaceChildren();
  for (const n of data.notes) {
    const li = document.createElement("li");
    const title = document.createElement("p");
    title.className = "title";
    title.textContent = n.title;
    const meta = document.createElement("p");
    meta.className = "muted small";
    meta.textContent = `${n.pages || n.files} ${n.pages ? "pág." : "fichero(s)"} · ${n.engine_label} `;
    meta.append(badge(n));
    li.append(title, meta);
    li.addEventListener("click", () => go(`#n=${n.id}`));
    ul.append(li);
  }
  $("empty").hidden = data.folders.length + data.notes.length > 0;
  show("list");
}

// --- un apunte ---
async function openNote(id, keepText = false) {
  const n = await api(`/api/notes/${id}`);
  const changedNote = !current || current.id !== n.id;
  if (changedNote) { tab = null; dirty = false; }
  current = n;
  crumbs($("d-crumbs"), n.path, false);
  $("d-status").replaceChildren(badge(n));
  $("d-title").textContent = n.title;
  $("d-info").textContent = `${n.engine_label} · ` +
    `${{ es: "castellano", ca: "català", en: "English" }[n.lang] || n.lang} · ${n.files} fichero(s)` +
    (n.pages ? ` · ${n.pages} página(s)` : "") + (n.edited ? " · texto retocado a mano" : "");

  // Original
  const ol = $("originals");
  ol.replaceChildren();
  for (const o of n.originals) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `/files/${n.id}/original/${o.n}`;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = `${o.kind === "pdf" ? "📄 PDF" : "🖼 Foto"} ${n.originals.length > 1 ? o.n : ""} — abrir`;
    li.append(a);
    ol.append(li);
  }
  $("upload-box").hidden = n.status !== "borrador";
  $("working-box").hidden = !["en_cola", "leyendo"].includes(n.status);
  $("error-box").hidden = n.status !== "error";
  $("up-info").textContent = n.files ? `${n.files} fichero(s) subido(s).` : "Aún no has subido nada.";
  $("start").disabled = n.files === 0;
  if (changedNote) $("start-engine").value = n.engine;
  $("start-note").textContent = engineNote($("start-engine").value);
  $("error-text").textContent = n.error;
  $("retry-other").textContent = n.engine === "local" ? "Probar con Gemini" : "Probar con el local";
  $("read-progress").max = Math.max(n.pages, 1);
  $("read-progress").value = n.done;
  $("read-info").textContent = n.status === "en_cola" ? "En cola…" :
    `Leyendo… ${n.done} de ${n.pages || "?"} páginas`;

  // Texto y PDF
  const ready = n.status === "listo";
  $("no-text").hidden = ready;
  $("text-box").hidden = !ready;
  $("no-pdf").hidden = ready;
  $("pdf-box").hidden = !ready;
  if (ready) {
    if (!keepText || changedNote || !dirty) { $("text").value = n.markdown; dirty = false; }
    buildThumbs(n);
    scanInfo(n);
  }
  showTab(tab || (ready ? "texto" : "original"));
  $("ask-note").hidden = !(ready && info.tutor);
  fillMoveTo(n).catch(() => {});
  show("detail");
  stopPolling();
  const busy = ["en_cola", "leyendo"].includes(n.status) || ["en_cola", "haciendo"].includes(n.scan);
  if (busy) poll = setTimeout(() => openNote(n.id, true).catch((e) => say(e.message)), 3000);
  if (changedNote) window.scrollTo(0, 0);
}

function showTab(name) {
  for (const t of ["original", "texto", "pdf"]) {
    $(`pane-${t}`).hidden = t !== name;
    $(`tab-${t}`).classList.toggle("on", t === name);
    $(`tab-${t}`).setAttribute("aria-selected", String(t === name));
  }
}

async function fillMoveTo(n) {
  const data = await api("/api/folders");
  const select = $("move-to");
  select.replaceChildren();
  for (const f of data.folders) {
    const option = document.createElement("option");
    option.value = String(f.id);
    option.textContent = f.path;
    option.selected = f.id === n.folder;
    select.append(option);
  }
}

function scanInfo(n) {
  const messages = {
    no: info.scans ? "El PDF escaneado aún no se ha hecho." : "El PDF escaneado no está instalado en este servidor.",
    en_cola: "PDF escaneado: en cola (se hace después de leer).",
    haciendo: "PDF escaneado: haciéndose… (tarda un poco más: endereza y limpia cada foto).",
    listo: "PDF escaneado listo: el original limpio, con el texto para buscar y copiar.",
    error: `PDF escaneado: error. ${n.scan_error}`,
  };
  $("scan-info").textContent = messages[n.scan] || "";
  const canMake = (n.scan === "error" || n.scan === "no") && info.scans;
  $("dl-scan").textContent = canMake ? "↻ Hacer PDF escaneado" : "⬇ PDF escaneado";
  $("dl-scan").disabled = !(n.scan === "listo" || canMake);
}

function buildThumbs(n) {
  const box = $("thumbs");
  if (box.dataset.note === String(n.id) && box.children.length === n.pages) return;
  box.dataset.note = String(n.id);
  box.replaceChildren();
  $("page-view").hidden = true;
  for (let p = 1; p <= n.pages; p++) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = String(p);
    b.title = `Ver la página ${p}`;
    b.addEventListener("click", () => {
      const view = $("page-view");
      const same = !view.hidden && b.classList.contains("on");
      for (const other of box.children) other.classList.remove("on");
      if (same) { view.hidden = true; return; }
      b.classList.add("on");
      view.src = `/files/${n.id}/page/${p}`;
      view.hidden = false;
    });
    box.append(b);
  }
}

// --- preguntar a la IA ---
async function openAsk(id) {
  if (!folder || folder.folder.id !== id) {  // al recargar en #p=N: hace falta saber qué carpeta es
    folder = await api(`/api/notes?carpeta=${id}`);
    info = folder;
  }
  askFolder = id;
  crumbs($("a-crumbs"), folder.path, false);
  const data = await api(`/api/ask/notes?carpeta=${id}`);
  const ul = $("ask-notes");
  ul.replaceChildren();
  for (const n of data.notes) {
    const li = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = String(n.id);
    box.checked = n.id === askMark;
    const text = document.createElement("span");
    text.textContent = n.title;
    label.append(box, text);
    if (n.path) {
      const where = document.createElement("span");
      where.className = "where";
      where.textContent = `· ${n.path}`;
      label.append(where);
    }
    li.append(label);
    ul.append(li);
  }
  askMark = null;
  $("ask-none").hidden = data.notes.length > 0;
  $("ask-tools").hidden = data.notes.length < 2;
  show("ask");
  $("ask-question").focus();
}

function marked() {
  return Array.from($("ask-notes").querySelectorAll("input:checked")).map((b) => Number(b.value));
}

// --- subir (XMLHttpRequest para tener barra de progreso; uno detrás de otro, en orden) ---
function uploadOne(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/notes/${current.id}/upload`);
    xhr.setRequestHeader("X-Arthur", "1");
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) onProgress(ev.loaded / ev.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* nada */ }
      if (xhr.status === 200) resolve(data); else reject(new Error(`${file.name}: ${data.error || xhr.status}`));
    };
    xhr.onerror = () => reject(new Error(`${file.name}: la conexión se ha cortado`));
    xhr.send(file);
  });
}

async function upload(files) {
  if (!files.length) return;
  const bar = $("up-progress");
  bar.hidden = false;
  $("start").disabled = true;
  let i = 0;
  try {
    for (const file of files) {
      $("up-info").textContent = `Subiendo ${i + 1} de ${files.length}: ${file.name}`;
      await uploadOne(file, (part) => { bar.value = (i + part) / files.length; });
      i++;
    }
    say(`${files.length} fichero(s) subido(s).`);
  } catch (e) {
    say(e.message);
  } finally {
    bar.hidden = true;
    bar.value = 0;
    await openNote(current.id);
  }
}

// --- descargas: se piden con fetch para poder enseñar el motivo si falla ---
async function download(path, fallbackName) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) {
    let data = {};
    try { data = await res.json(); } catch (_) { /* nada */ }
    throw new Error(data.error || `Error ${res.status}`);
  }
  const blob = await res.blob();
  const match = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = match ? match[1] : fallbackName;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60000);
}

async function busyButton(button, label, work) {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { await work(); } catch (e) { say(e.message); } finally { button.disabled = false; button.textContent = old; }
}

async function saveText() {
  await post(`/api/notes/${current.id}/text`, { markdown: $("text").value });
  dirty = false;
}

async function startReading(engine) {
  if (dirty && !confirm("Tienes cambios sin guardar en el texto: se perderán. ¿Seguir?")) return;
  dirty = false;
  tab = "original";
  await post(`/api/notes/${current.id}/start`, engine ? { engine } : undefined);
  await openNote(current.id);
}

// --- preguntar a mi cerebro (lo contesta el cerebro; esta página solo hace de intermediaria) ---
// Solo se enlaza a Obsidian y a páginas web: un «javascript:» que viniera en la respuesta no se convierte en enlace.
const safeUrl = (url) => typeof url === "string" && /^(obsidian:\/\/open\?|https?:\/\/)/.test(url);

function renderBrain(data) {
  $("brain-answer").textContent = data.answer;
  $("brain-sources").replaceChildren(...data.citations.map((c) => {
    const li = document.createElement("li");
    const id = document.createElement("span");
    id.className = "muted small";
    id.textContent = c.id;
    let title;
    if (safeUrl(c.url)) {
      title = document.createElement("a");
      title.href = c.url;
      title.target = "_blank";
      title.rel = "noopener noreferrer";
    } else {
      title = document.createElement("span");
    }
    title.textContent = c.title;
    li.append(id, title);
    return li;
  }));
  $("brain-result").hidden = false;
}

async function askBrain(ev) {
  ev.preventDefault();
  const q = $("brain-q").value.trim();
  if (!q) { $("brain-q").focus(); return; }
  await busyButton($("brain-go"), "Buscando…", async () => {
    renderBrain(await post("/api/cerebro/ask", { q }));
  });
}

// Entrar con el enlace que da ARTHUR (…/#llave=arthur_…): la llave va tras «#», que el navegador nunca manda al
// servidor ni a otras webs; se cambia por la cookie y se borra de la dirección al momento.
async function loginFromLink() {
  const m = /^#llave=([A-Za-z0-9_-]{10,200})$/.exec(location.hash);
  if (!m) return;
  history.replaceState(null, "", location.pathname);
  try {
    await post("/api/login", { key: m[1] });
    say("Listo: este aparato ya está dentro.");
  } catch (e) { say(e.message); }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("show-brain").addEventListener("click", () => {
    const form = $("brain-form");
    form.hidden = !form.hidden;
    $("show-brain").setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) $("brain-q").focus();
  });
  $("brain-form").addEventListener("submit", askBrain);
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
    show("login");
  });
  $("back").addEventListener("click", () => {
    if (dirty && !confirm("Tienes cambios sin guardar en el texto. ¿Salir igualmente?")) return;
    dirty = false;
    if (!$("detail").hidden && current) return go(`#c=${current.folder}`);
    if (!$("ask").hidden) return go(`#c=${askFolder}`);
    const path = folder ? folder.path : [];
    go(`#c=${path.length > 1 ? path[path.length - 2].id : 0}`);
  });

  // carpetas
  $("show-new").addEventListener("click", () => {
    $("new-form").hidden = !$("new-form").hidden;
    if (!$("new-form").hidden) $("new-title").focus();
  });
  $("new-folder").addEventListener("click", async () => {
    const name = prompt("Nombre de la carpeta nueva (p. ej. una asignatura o un tema):");
    if (!name || !name.trim()) return;
    try {
      await post("/api/folders", { parent: folder.folder.id, name });
      await loadFolder(folder.folder.id);
    } catch (e) { say(e.message); }
  });
  $("rename-folder").addEventListener("click", async () => {
    const name = prompt("Nombre nuevo:", folder.folder.name);
    if (!name || !name.trim()) return;
    try {
      await post(`/api/folders/${folder.folder.id}/rename`, { name });
      await loadFolder(folder.folder.id);
    } catch (e) { say(e.message); }
  });
  $("delete-folder").addEventListener("click", async () => {
    if (!confirm(`¿Borrar la carpeta «${folder.folder.name}»? Está vacía.`)) return;
    try {
      const r = await post(`/api/folders/${folder.folder.id}/delete`);
      go(`#c=${r.parent}`);
    } catch (e) { say(e.message); }
  });
  $("new-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      const n = await post("/api/notes", {
        title: $("new-title").value, engine: $("new-engine").value, lang: $("new-lang").value,
        folder: folder.folder.id });
      $("new-title").value = "";
      $("new-form").hidden = true;
      go(`#n=${n.id}`);
    } catch (e) { say(e.message); }
  });
  $("new-engine").addEventListener("change", () => { $("engine-note").textContent = engineNote($("new-engine").value); });
  $("start-engine").addEventListener("change", () => { $("start-note").textContent = engineNote($("start-engine").value); });

  // preguntar a la IA
  $("show-ask").addEventListener("click", () => go(`#p=${folder.folder.id}`));
  $("ask-note").addEventListener("click", () => {
    if (dirty && !confirm("Tienes cambios sin guardar en el texto. ¿Salir igualmente?")) return;
    dirty = false;
    askMark = current.id;
    go(`#p=${current.folder}`);
  });
  $("ask-all").addEventListener("click", () => { for (const b of $("ask-notes").querySelectorAll("input")) b.checked = true; });
  $("ask-clear").addEventListener("click", () => { for (const b of $("ask-notes").querySelectorAll("input")) b.checked = false; });
  $("ask-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    busyButton($("ask-go"), "Pensando… (puede tardar hasta 1 min)", async () => {
      const r = await post("/api/ask", { mode: $("ask-mode").value, question: $("ask-question").value, notes: marked() });
      $("answer").value = r.answer;
      $("answer-box").hidden = false;
      $("answer-box").scrollIntoView({ behavior: "smooth" });
    });
  });
  $("answer-save").addEventListener("click", () => {
    const suggestion = $("ask-question").value.trim().split("\n")[0].slice(0, 80);
    const title = prompt("Título del apunte nuevo:", suggestion ? `IA: ${suggestion}` : "Respuesta de la IA");
    if (title === null) return;
    busyButton($("answer-save"), "Guardando…", async () => {
      const n = await post("/api/ask/save", { title, markdown: $("answer").value, folder: askFolder, lang: "es" });
      say("Guardado como apunte. En la pestaña PDF lo ves con las fórmulas bonitas.");
      $("answer-box").hidden = true;
      $("ask-question").value = "";
      go(`#n=${n.id}`);
    });
  });
  $("answer-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("answer").value); say("Copiado."); }
    catch (_) { $("answer").select(); say("Selecciónalo y cópialo a mano (el navegador no deja copiar aquí)."); }
  });

  // apunte
  for (const t of ["original", "texto", "pdf"]) {
    $(`tab-${t}`).addEventListener("click", () => { tab = t; showTab(t); });
  }
  for (const id of ["camera", "files"]) {
    $(id).addEventListener("change", async (ev) => {
      const files = Array.from(ev.target.files);
      ev.target.value = "";
      await upload(files);
    });
  }
  $("start").addEventListener("click", () => busyButton($("start"), "Empezando…",
    () => startReading($("start-engine").value)));
  $("retry").addEventListener("click", () => busyButton($("retry"), "…", () => startReading()));
  $("retry-other").addEventListener("click", () => busyButton($("retry-other"), "…",
    () => startReading(current.engine === "local" ? "gemini" : "local")));
  $("reread").addEventListener("click", () => {
    const choice = prompt("Volver a leer TODAS las páginas (se pierde el texto actual).\n\n" +
                          "Escribe el motor: auto, gemini, local o texto. Ahora es «" + current.engine + "».",
                          current.engine === "gemini" ? "local" : "gemini");
    if (choice === null) return;
    const engine = choice.trim().toLowerCase();
    if (!(engine in ENGINES)) { say("Escribe auto, gemini, local o texto."); return; }
    busyButton($("reread"), "…", () => startReading(engine));
  });
  $("text").addEventListener("input", () => { dirty = true; });
  $("save").addEventListener("click", () => busyButton($("save"), "Guardando…", async () => {
    await saveText();
    say("Guardado.");
    await openNote(current.id, true);
  }));
  $("view-clean").addEventListener("click", () => busyButton($("view-clean"), "Haciendo el PDF…", async () => {
    if (dirty) await saveText();  // lo que se ve es lo que hay escrito
    window.open(`/files/${current.id}/limpio?estilo=${encodeURIComponent($("style").value)}&ver=1`, "_blank",
                "noopener");
  }));
  $("dl-clean").addEventListener("click", () => busyButton($("dl-clean"), "Haciendo el PDF…", async () => {
    if (dirty) await saveText();
    await download(`/files/${current.id}/limpio?estilo=${encodeURIComponent($("style").value)}`, "apuntes.pdf");
  }));
  $("dl-scan").addEventListener("click", () => busyButton($("dl-scan"), "…", async () => {
    if (current.scan === "listo") return download(`/files/${current.id}/escaneo`, "apuntes-escaneo.pdf");
    await post(`/api/notes/${current.id}/scan`);
    await openNote(current.id, true);
  }));
  $("dl-md").addEventListener("click", () => busyButton($("dl-md"), "…", async () => {
    if (dirty) await saveText();
    await download(`/files/${current.id}/texto`, "apuntes.md");
  }));
  $("move").addEventListener("click", () => busyButton($("move"), "…", async () => {
    const target = Number($("move-to").value);
    if (target === current.folder) return;
    await post(`/api/notes/${current.id}/move`, { folder: target });
    say("Movido.");
    await openNote(current.id, true);
  }));
  $("delete").addEventListener("click", async () => {
    if (!confirm(`¿Borrar «${current.title}» con sus ficheros y su texto? No se puede deshacer.`)) return;
    try {
      const back = current.folder;
      await post(`/api/notes/${current.id}/delete`);
      dirty = false;
      current = null;
      say("Borrado.");
      go(`#c=${back}`);
    } catch (e) { say(e.message); }
  });
  window.addEventListener("beforeunload", (ev) => { if (dirty) ev.preventDefault(); });
  window.addEventListener("hashchange", () => {
    if (/^#llave=/.test(location.hash)) return;
    dirty = false;  // el aviso de cambios sin guardar ya lo dan «Atrás» y beforeunload
    route();
  });

  await loginFromLink();
  route();
});
