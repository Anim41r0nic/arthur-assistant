// Punto de entrada: conecta el flujo de eventos de ARTHUR con el Orb y el HUD.
//
// ARTHUR envía eventos por /events (Server-Sent Events). EventSource, que viene
// con el navegador, mantiene la conexión abierta y se reconecta solo si se corta.

import { Orb } from "./orb.js";
import { Hud } from "./hud.js";
import { ConfirmPanel } from "./confirm.js";
import { CameraIndicator } from "./camera.js";
import { MIN_DWELL_MS, speakingDwell } from "./states.js";

const hud = new Hud();
const confirmPanel = new ConfirmPanel();
const camera = new CameraIndicator();

// Si algo falla (p. ej. el navegador no tiene WebGL), se muestra en pantalla
// en lugar de dejar la página en negro sin explicación.
function showFatal(message) {
  const box = document.getElementById("fatal");
  box.textContent = message;
  box.hidden = false;
}
window.addEventListener("error", (e) => showFatal(`Error en la interfaz: ${e.message}`));
window.addEventListener("unhandledrejection", (e) => showFatal(`Error en la interfaz: ${e.reason}`));

let orb;
try {
  orb = new Orb(document.getElementById("scene"));
} catch (error) {
  showFatal(`No puedo dibujar el Orb (¿WebGL desactivado?): ${error.message}`);
  orb = { setState() {}, setAudioLevel() {}, update() {} };
}

// --- Cola de estados -----------------------------------------------------
// El núcleo cambia de estado en milisegundos. Encolamos los cambios y mostramos
// cada uno un tiempo mínimo, para que se vean todos y la animación tenga sentido.
const pending = [];
let shownUntil = 0;
let lastReply = "";

function showState(name) {
  orb.setState(name);
  hud.setState(name);
  const dwell = name === "speaking" ? speakingDwell(lastReply) : MIN_DWELL_MS[name] ?? 0;
  shownUntil = performance.now() + dwell;
}

function frame() {
  if (pending.length && performance.now() >= shownUntil) showState(pending.shift());
  orb.update();
  confirmPanel.tick();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// --- Eventos de ARTHUR -----------------------------------------------------
const handlers = {
  hello(e) {
    hud.setName(e.data.name);
    pending.length = 0;
    showState(e.data.state);
    confirmPanel.reset(e.data.pending ?? []);
    camera.reset(e.data.camera);
  },
  state_changed(e) {
    pending.push(e.data.to);
  },
  user_message(e) {
    hud.message("user", e.data.text);
  },
  assistant_message(e) {
    lastReply = e.data.text;
    hud.message("assistant", e.data.text);
  },
  tool_started: (e) => hud.toolStarted(e),
  tool_finished: (e) => hud.toolFinished(e),
  confirmation_requested: (e) => hud.confirmation(e),
  error: (e) => hud.error(e),
  audio_level: (e) => orb.setAudioLevel(e.data.level),
  confirmation_pending: (e) => confirmPanel.add(e.data),
  confirmation_resolved: (e) => confirmPanel.resolved(e.data.id),
  camera_state: (e) => camera.set(e.data.on),
  gesture_action: (e) => hud.gesture(e),
};

const stream = new EventSource("/events");
stream.onopen = () => hud.setOnline(true);
stream.onerror = () => hud.setOnline(false);
stream.onmessage = (message) => {
  let event;
  try {
    event = JSON.parse(message.data);
  } catch {
    return;
  }
  handlers[event.type]?.(event);
};
