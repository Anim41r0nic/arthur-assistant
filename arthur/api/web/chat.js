// La web de chat de ARTHUR (fase S5). Habla con arthur-core por su API (api/core_api.py):
//   - entrar: POST /api/v1/login con la llave → el servidor la guarda en una cookie HttpOnly;
//   - escribir: POST /api/v1/message → la respuesta;
//   - en directo (EventSource /api/v1/events): estado, avisos y preguntas de confirmación (Sí/No).
// Todo se pinta con textContent: lo que llega del servidor nunca se interpreta como HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const STATES = {
  idle: "listo", listening: "escuchando", thinking: "pensando…", executing: "haciendo…",
  speaking: "respondiendo", error: "algo ha fallado",
};

let events = null;          // la conexión en directo
let busy = false;           // hay un mensaje esperando respuesta
const confirmCards = new Map();  // id de la pregunta → su tarjeta

// --- llamadas a la API ---
async function api(path, body) {
  const options = { method: body === undefined ? "GET" : "POST", credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-Arthur"] = "1";  // así el servidor sabe que viene de esta página y no de otra web
    options.body = JSON.stringify(body);
  }
  const r = await fetch(path, options);
  let data = {};
  try { data = await r.json(); } catch { /* sin JSON */ }
  if (!r.ok) {
    const err = new Error(data.error || `error ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return data;
}

// --- pantallas ---
function showLogin(message = "") {
  if (events) { events.close(); events = null; }
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-error").textContent = message;
  $("login-key").focus();
}

function showChat(info) {
  $("login").hidden = true;
  $("app").hidden = false;
  $("device").textContent = info.device ? `este aparato: ${info.device}` : "";
  $("mic").hidden = !info.voice;  // sin arthur-voz en el servidor, no hay botón
  $("attach").hidden = !info.vision;  // solo si el cerebro del servidor ve fotos (brain.vision)
  setupPush(info);
  loadHistory();
  connectEvents();
  $("input").focus();
}

// --- la conversación hasta ahora (al abrir o recargar la web) ---
// Es UNA conversación, la de ARTHUR: también sale lo que le dijiste por voz desde el portátil. Vive en la RAM del
// servidor (si se reinicia, empieza de cero) y las fotos no se guardan: se marca que la hubo.
async function loadHistory() {
  let data;
  try { data = await api("/api/v1/history"); } catch { return; }
  if (!data.messages || !data.messages.length) return;
  $("messages").replaceChildren();
  for (const m of data.messages) {
    if (m.role === "user") addMessage("user", m.photo ? `📷 ${m.text}` : m.text);
    else addMessage("arthur", m.text);
  }
}

// --- mensajes ---
function addMessage(kind, text, imageUrl = null) {
  const empty = document.querySelector(".messages .empty");
  if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = `msg ${kind}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (imageUrl) {
    const img = document.createElement("img");
    img.src = imageUrl;
    img.alt = "Foto enviada";
    bubble.append(img);
  }
  bubble.append(document.createTextNode(text));
  row.append(bubble);
  $("messages").append(row);
  scrollDown();
  return row;
}

function scrollDown() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

async function send(text) {
  busy = true;
  $("send").disabled = true;
  const withPhoto = photo;
  clearPhoto(false);
  addMessage("user", text, withPhoto && withPhoto.url);
  // Leer una foto nueva le cuesta al servidor (sin tarjeta gráfica) alrededor de minuto y medio.
  const thinking = addMessage("arthur thinking", withPhoto ? "Mirando la foto (puede tardar 1-2 minutos)" : "Pensando");
  try {
    const body = withPhoto ? { text, image: withPhoto.base64 } : { text };
    const data = await api("/api/v1/message", body);
    thinking.remove();
    addMessage("arthur", data.reply || "(sin respuesta)");
  } catch (e) {
    thinking.remove();
    if (e.status === 401) return showLogin("La llave ya no vale (¿la has revocado?). Vuelve a entrar.");
    addMessage("error", `No he podido hablar con el servidor: ${e.message}`);
  } finally {
    busy = false;
    $("send").disabled = false;
    $("input").focus();
  }
}

// --- fotos (fase S5, paso 3) ---
// Antes de enviarla, la foto se redibuja en un lienzo a como mucho 1280 px y se guarda como JPEG: pesa poco
// (~200-400 KB), las HEIC del iPhone pasan a JPEG y se quedan fuera los datos ocultos (ubicación GPS, cámara…).
const PHOTO_MAX_SIDE = 1280;
let photo = null;  // { base64, url } de la foto preparada

async function preparePhoto(file) {
  if (!file || !file.type.startsWith("image/")) return;
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    addMessage("error", "No puedo abrir esa imagen. Prueba con una foto JPEG o PNG.");
    return;
  }
  const scale = Math.min(1, PHOTO_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
  if (!blob) return addMessage("error", "No he podido preparar la foto.");
  const dataUrl = await new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(blob);
  });
  clearPhoto();
  photo = { base64: String(dataUrl).split(",")[1], url: URL.createObjectURL(blob) };
  $("preview-img").src = photo.url;
  $("preview").hidden = false;
  $("input").focus();
}

function clearPhoto(revoke = true) {
  if (photo && revoke) URL.revokeObjectURL(photo.url);  // si se envió, la sigue usando la burbuja del chat
  photo = null;
  $("preview").hidden = true;
  $("preview-img").removeAttribute("src");
  $("file").value = "";
}

function setupPhotos() {
  $("attach").addEventListener("click", () => $("file").click());
  $("file").addEventListener("change", () => preparePhoto($("file").files[0]));
  $("preview-remove").addEventListener("click", () => clearPhoto());
  // Pegar (Ctrl+V) una captura o una imagen copiada.
  document.addEventListener("paste", (e) => {
    if ($("attach").hidden) return;
    const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith("image/"));
    if (file) { e.preventDefault(); preparePhoto(file); }
  });
  // Arrastrar una imagen a la ventana.
  document.addEventListener("dragover", (e) => {
    if ($("attach").hidden || !e.dataTransfer?.types.includes("Files")) return;
    e.preventDefault();
    document.body.classList.add("dragging");
  });
  document.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) document.body.classList.remove("dragging");
  });
  document.addEventListener("drop", (e) => {
    document.body.classList.remove("dragging");
    if ($("attach").hidden || !e.dataTransfer?.files.length) return;
    e.preventDefault();
    preparePhoto(e.dataTransfer.files[0]);
  });
}

// --- preguntas de confirmación ---
function addConfirm(item) {
  if (!item || !item.id || confirmCards.has(item.id)) return;
  const empty = document.querySelector(".messages .empty");
  if (empty) empty.remove();
  const card = document.createElement("div");
  card.className = "confirm";
  const q = document.createElement("div");
  q.className = "q";
  q.textContent = item.summary || item.tool || "";
  const buttons = document.createElement("div");
  buttons.className = "buttons";
  const yes = document.createElement("button");
  yes.type = "button";
  yes.textContent = "Sí";
  const no = document.createElement("button");
  no.type = "button";
  no.className = "no";
  no.textContent = "No";
  yes.onclick = () => answer(item.id, true);
  no.onclick = () => answer(item.id, false);
  buttons.append(yes, no);
  card.append(q, buttons);
  $("messages").append(card);
  confirmCards.set(item.id, card);
  scrollDown();
}

async function answer(id, value) {
  closeConfirm(id, value ? "Has dicho que sí." : "Has dicho que no.");
  try {
    await api("/api/v1/confirm", { id, answer: value });
  } catch (e) {
    if (e.status === 409) closeConfirm(id, "Llegó tarde: la pregunta ya había caducado.", true);
    else addMessage("error", `No he podido enviar tu respuesta: ${e.message}`);
  }
}

function closeConfirm(id, text, force = false) {
  const card = confirmCards.get(id);
  if (!card || (card.classList.contains("done") && !force)) return;
  card.classList.add("done");
  const old = card.querySelector(".buttons, .note");
  const note = document.createElement("div");
  note.className = "note dim small";
  note.textContent = text;
  old.replaceWith(note);
}

// --- la voz (fase S5, paso 2) ---
// Pulsas 🎙, hablas y vuelves a pulsar: el audio se pasa a WAV de 16 kHz (lo que espera el servidor) y va a
// /api/v1/voice; el servidor oye, piensa y contesta, y la respuesta se oye con /api/v1/speech. Si escribes,
// contesta solo por escrito. El navegador solo deja usar el micro por HTTPS (o en el propio ordenador).
const VOICE_RATE = 16000;
const MAX_SECONDS = 59;      // el servidor acepta hasta 60 s
const MIN_SECONDS = 0.4;     // un toque sin querer no se envía
let mic = null;              // la grabación en curso
let player = null;           // un único <audio>: en el iPhone solo suena si ya se usó en un toque tuyo

function setRecording(on) {
  $("mic").classList.toggle("recording", on);
  $("mic").setAttribute("aria-label", on ? "Enviar lo que has dicho" : "Hablar");
  setState(on ? "listening" : "idle");
}

function unlockPlayer() {
  if (!player) player = new Audio();
  // Medio segundo de silencio dentro del toque: desde ahí, el navegador deja sonar las respuestas.
  player.src = URL.createObjectURL(new Blob([wavFromPcm(new Int16Array(VOICE_RATE / 2))], { type: "audio/wav" }));
  player.play().catch(() => {});
}

function stopSpeaking() {
  if (player) player.pause();
}

async function startMic() {
  if (!window.isSecureContext || !navigator.mediaDevices) {
    addMessage("error", "El navegador solo deja usar el micrófono por HTTPS. Entra por la dirección https://…ts.net.");
    return;
  }
  stopSpeaking();
  unlockPlayer();
  const ctx = new AudioContext();  // se crea dentro del toque (Safari lo exige)
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    await ctx.audioWorklet.addModule("/mic.js");
  } catch (e) {
    ctx.close();
    if (stream) stream.getTracks().forEach((track) => track.stop());
    addMessage("error", e.name === "NotAllowedError"
      ? "No me has dado permiso para el micrófono (el candado de la barra de direcciones lo cambia)."
      : `No puedo abrir el micrófono: ${e.message}`);
    return;
  }
  const source = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "arthur-mic");
  const chunks = [];
  node.port.onmessage = (e) => chunks.push(e.data);
  source.connect(node);
  node.connect(ctx.destination);  // no suena nada (el procesador no escribe salida), pero así el audio fluye
  mic = { ctx, stream, chunks, rate: ctx.sampleRate, timer: setTimeout(() => finishMic(true), MAX_SECONDS * 1000) };
  setRecording(true);
}

function closeMic() {
  const m = mic;
  mic = null;
  clearTimeout(m.timer);
  m.stream.getTracks().forEach((track) => track.stop());  // se apaga el piloto del micro
  m.ctx.close();
  setRecording(false);
  return m;
}

function cancelMic() {
  if (mic) closeMic();
}

async function finishMic(tooLong = false) {
  if (!mic) return;
  const m = closeMic();
  const pcm = toPcm16(m.chunks, m.rate);
  if (pcm.length < VOICE_RATE * MIN_SECONDS) return;
  busy = true;
  $("send").disabled = $("mic").disabled = true;
  const said = addMessage("user voice", "…");
  const thinking = addMessage("arthur thinking", tooLong ? "Has llegado al minuto; lo envío" : "Escuchando");
  try {
    const r = await fetch("/api/v1/voice", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "audio/wav", "X-Arthur": "1" }, body: wavFromPcm(pcm),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(data.error || `error ${r.status}`);
      err.status = r.status;
      throw err;
    }
    thinking.remove();
    if (!data.heard) {
      said.remove();
      addMessage("error", "No te he oído decir nada.");
      return;
    }
    said.querySelector(".bubble").textContent = data.heard;
    if (data.reply) {
      addMessage("arthur", data.reply);
      speak(data.reply);
    }
  } catch (e) {
    thinking.remove();
    said.remove();
    if (e.status === 401) return showLogin("La llave ya no vale (¿la has revocado?). Vuelve a entrar.");
    addMessage("error", `No he podido hablar con el servidor: ${e.message}`);
  } finally {
    busy = false;
    $("send").disabled = $("mic").disabled = false;
  }
}

async function speak(text) {
  try {
    const r = await fetch("/api/v1/speech", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Arthur": "1" }, body: JSON.stringify({ text }),
    });
    if (!r.ok) return;  // sin voz, la respuesta ya está escrita en el chat
    const url = URL.createObjectURL(await r.blob());
    if (!player) player = new Audio();
    const old = player.src;
    player.src = url;
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    await player.play();
  } catch { /* si el navegador no deja sonar, queda el texto */ }
}

// Junta los trozos del micro y los pasa a 16 kHz, 16 bits (media de las muestras de cada hueco: así no se
// cuelan pitidos al bajar de 48 kHz).
function toPcm16(chunks, rate) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const input = new Float32Array(total);
  let offset = 0;
  for (const c of chunks) { input.set(c, offset); offset += c.length; }
  const ratio = rate / VOICE_RATE;
  const out = new Int16Array(Math.floor(total / ratio));
  for (let i = 0; i < out.length; i++) {
    let value;
    if (ratio >= 1) {
      const start = Math.floor(i * ratio), end = Math.max(start + 1, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end && j < total; j++) sum += input[j];
      value = sum / (end - start);
    } else {
      const pos = i * ratio, j = Math.floor(pos), frac = pos - j;
      value = input[j] * (1 - frac) + (input[Math.min(j + 1, total - 1)] || 0) * frac;
    }
    out[i] = Math.max(-1, Math.min(1, value)) * 0x7fff;
  }
  return out;
}

// Cabecera WAV (RIFF) de 44 bytes + las muestras: mono, 16 kHz, 16 bits.
function wavFromPcm(pcm) {
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buffer);
  const text = (at, s) => { for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i)); };
  text(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); text(8, "WAVE");
  text(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, VOICE_RATE, true); v.setUint32(28, VOICE_RATE * 2, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); text(36, "data"); v.setUint32(40, pcm.length * 2, true);
  new Int16Array(buffer, 44).set(pcm);
  return buffer;
}

// --- en directo ---
function setState(state) {
  if (mic && state !== "listening") return;  // mientras grabas, manda el micro
  document.body.dataset.state = state || "idle";
  $("status").textContent = STATES[state] || state || "";
}

function connectEvents() {
  if (events) events.close();
  events = new EventSource("/api/v1/events");  // la cookie va sola; el navegador reconecta si se corta
  events.onopen = () => document.body.classList.remove("offline");
  events.onerror = async () => {
    document.body.classList.add("offline");
    $("status").textContent = "sin conexión, reintentando…";
    try {
      await api("/api/v1/whoami");
    } catch (e) {
      if (e.status === 401) showLogin("La sesión ha caducado. Vuelve a entrar.");
    }
  };
  events.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    const data = ev.data || {};
    switch (ev.type) {
      case "hello":
        setState(data.state);
        (data.pending || []).forEach(addConfirm);
        break;
      case "state_changed":
        setState(data.to);
        break;
      case "confirmation_pending":
        addConfirm(data);
        break;
      case "confirmation_resolved":
        closeConfirm(data.id, data.answer ? "Confirmado." : "No confirmado.");
        break;
      case "assistant_message":
        // Las respuestas a lo que escribes llegan por /message; aquí solo los avisos (recordatorios…).
        if (data.announcement && data.text) addMessage("notice", data.text);
        break;
    }
  };
}

// --- avisos al móvil (fase S6) ---
// El navegador da una «suscripción» (dirección + llaves) y el servidor la guarda. Cuando vence un recordatorio,
// el servidor manda el aviso cifrado a Apple/Google/Mozilla, que lo llevan aquí aunque la web esté cerrada; lo
// enseña sw.js. En el iPhone solo existe con la web instalada (Compartir → «Añadir a pantalla de inicio»).
const canPush = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

function setBell(on) {
  const bell = $("bell");
  bell.dataset.on = on ? "1" : "";
  bell.textContent = on ? "🔔" : "🔕";
  bell.title = on ? "Avisos al móvil activados (pulsa para quitarlos)" : "Activar avisos en este aparato";
}

async function setupPush(info) {
  $("bell").hidden = true;
  if (!info.push || !canPush()) return;
  try {
    const reg = await navigator.serviceWorker.register("/sw.js");
    const sub = await reg.pushManager.getSubscription();
    setBell(Boolean(info.push_on && sub) && Notification.permission === "granted");
    $("bell").hidden = false;
  } catch { /* sin service worker (p. ej. sin HTTPS): sin campana */ }
}

function keyBytes(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64url.length % 4)) % 4);
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

async function pushOff(quiet = false) {
  try { await api("/api/v1/push/unsubscribe", {}); } catch { /* el servidor la quitará al fallar el envío */ }
  try {
    const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
    if (sub) await sub.unsubscribe();
  } catch { /* nada que quitar */ }
  setBell(false);
  if (!quiet) addMessage("arthur", "Avisos quitados en este aparato.");
}

async function togglePush() {
  if ($("bell").dataset.on) return pushOff();
  // El permiso, lo primero y justo al pulsar: el iPhone solo lo pregunta si viene directo de tu toque.
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    return addMessage("error", "Sin permiso no te puedo avisar. Se cambia en Ajustes → Notificaciones → ARTHUR.");
  }
  try {
    const { key } = await api("/api/v1/push/key");
    const reg = await navigator.serviceWorker.ready;
    const old = await reg.pushManager.getSubscription();
    if (old) await old.unsubscribe();  // por si era de otra llave del servidor: una nueva y limpia
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
    await api("/api/v1/push/subscribe", { subscription: sub.toJSON() });
    setBell(true);
    addMessage("arthur", "Avisos activados: te llegará uno de prueba en unos segundos.");
  } catch (e) {
    addMessage("error", `No he podido activar los avisos: ${e.message}`);
  }
}

// --- arranque ---
document.addEventListener("DOMContentLoaded", async () => {
  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = $("login-key").value.trim();
    $("login-error").textContent = "";
    try {
      const info = await api("/api/v1/login", { key });
      $("login-key").value = "";
      showChat(info);
    } catch (err) {
      $("login-error").textContent = err.status === 401 ? "Esa llave no vale." : err.message;
    }
  });

  $("bell").addEventListener("click", togglePush);
  $("logout").addEventListener("click", async () => {
    if ($("bell").dataset.on) await pushOff(true);  // quien sale de este navegador deja de recibir avisos aquí
    try { await api("/api/v1/logout", {}); } catch { /* da igual: se sale igualmente */ }
    showLogin();
  });

  $("mic").addEventListener("click", () => (mic ? finishMic() : busy || startMic()));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { cancelMic(); stopSpeaking(); }  // Esc: no envía lo grabado y calla la voz
  });

  const input = $("input");
  const grow = () => { input.style.height = "auto"; input.style.height = `${input.scrollHeight}px`; };
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (e) => {
    // Enter envía; Mayús+Enter hace un salto de línea (como en ChatGPT).
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("composer").requestSubmit();
    }
  });
  setupPhotos();
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    // Una foto sola, sin texto, también vale: se le pregunta qué hay en ella.
    const text = input.value.trim() || (photo ? "¿Qué hay en esta foto?" : "");
    if (!text || busy) return;
    input.value = "";
    grow();
    send(text);
  });

  try {
    showChat(await api("/api/v1/whoami"));
  } catch (e) {
    showLogin(e.status === 401 ? "" : `No llego al servidor: ${e.message}`);
  }
});
