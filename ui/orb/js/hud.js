// HUD: los textos alrededor del Orb (reloj, estado, conversación, actividad).
//
// Seguridad: todo lo que viene de ARTHUR (y por tanto del modelo) se pinta con
// textContent, NUNCA con innerHTML. Así, si una respuesta contiene "<script>",
// se muestra como texto y no se ejecuta.

import { STATES } from "./states.js";

const WEEKDAYS = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];
const TOOL_STATUS = { ok: "OK", denied: "DENEGADO", cancelled: "CANCELADO", error: "ERROR" };
const MAX_ITEMS = 8;
// Los nombres de arthur/interfaces/gestos/names.py y las acciones de gestos/actions.py, para la lista de actividad.
const GESTURE_ICONS = {
  palma: "✋", pulgar_arriba: "👍", pulgar_abajo: "👎", puno: "✊", indice_arriba: "☝️", circulo: "👌",
  dos_dedos: "✌️", saludo: "👋",
};
const GESTURE_ACTIONS = { calla: "calla", no: "no", si: "sí", repite: "repite", pausa_play: "pausa/play", hola: "hola" };

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function push(list, item) {
  list.append(item);
  while (list.children.length > MAX_ITEMS) list.firstElementChild.remove();
}

function time(ts) {
  return new Date(ts * 1000).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export class Hud {
  constructor() {
    this.transcript = $("transcript");
    this.activity = $("activity");
    this.tick();
    setInterval(() => this.tick(), 1000);
  }

  tick() {
    const now = new Date();
    $("clock").textContent = now.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
    $("date").textContent = `${WEEKDAYS[now.getDay()]} ${now.getDate()} ${MONTHS[now.getMonth()]}`;
  }

  setName(name) {
    document.querySelector("h1").textContent = name;
    document.title = name;
  }

  setOnline(online) {
    document.body.classList.toggle("online", online);
    $("link-text").textContent = online ? "conectado" : "sin conexión";
  }

  setState(name) {
    const state = STATES[name] ?? STATES.idle;
    document.body.dataset.state = name;
    $("state-name").textContent = state.label;
    $("state-desc").textContent = state.desc;
  }

  message(role, text) {
    const item = el("li", role);
    item.append(el("span", "who", role === "user" ? "tú" : document.title.toLowerCase()), el("span", "", text));
    push(this.transcript, item);
  }

  toolStarted(event) {
    const item = el("li", "", `▸ ${event.data.tool}`);
    item.prepend(el("span", "who", time(event.ts)));
    push(this.activity, item);
  }

  toolFinished(event) {
    const status = event.data.status;
    const item = el("li");
    item.append(
      el("span", "who", time(event.ts)),
      el("span", "", `${event.data.tool} · `),
      el("span", `status-${status}`, TOOL_STATUS[status] ?? status),
    );
    push(this.activity, item);
  }

  confirmation(event) {
    const item = el("li", "", `⚠ ${event.data.summary}`);
    item.prepend(el("span", "who", `${time(event.ts)} · confirmación`));
    push(this.activity, item);
  }

  gesture(event) {
    const { gesture, action, status, detail } = event.data;
    const item = el("li", "", `${GESTURE_ICONS[gesture] ?? "✦"} ${GESTURE_ACTIONS[action] ?? action} · ${detail ?? ""}`);
    item.prepend(el("span", `who ${status === "ok" ? "" : "status-error"}`.trim(), `${time(event.ts)} · gesto`));
    push(this.activity, item);
  }

  error(event) {
    const item = el("li", "", event.data.message);
    item.prepend(el("span", "who status-error", `${time(event.ts)} · error`));
    push(this.activity, item);
  }
}
