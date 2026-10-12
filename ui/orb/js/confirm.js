// Confirmaciones desde el Orb: cuando ARTHUR necesita tu permiso (en peticiones
// hechas con el atajo de teclado), aparece un cuadro con Sí / No.
//
// Lo único que el Orb envía a ARTHUR es la respuesta a ESA pregunta, identificada
// por un número aleatorio. Si no respondes a tiempo, ARTHUR lo toma como NO.

const $ = (id) => document.getElementById(id);
const RISK_LABELS = { low: "bajo riesgo", write: "escritura", private: "datos privados", dangerous: "acción peligrosa" };

const YES_BY_GESTURE = new Set(["low", "write"]);  // como YES_RISKS en arthur/interfaces/gestos/actions.py

export class ConfirmPanel {
  constructor() {
    this.queue = [];  // puede haber más de una pregunta abierta: se muestran de una en una
    $("confirm-yes").addEventListener("click", () => this.respond(true));
    $("confirm-no").addEventListener("click", () => this.respond(false));
    window.addEventListener("keydown", (e) => {
      if ($("confirm").hidden) return;
      if (e.key === "s" || e.key === "S") this.respond(true);
      if (e.key === "n" || e.key === "N" || e.key === "Escape") this.respond(false);
    });
  }

  get current() {
    return this.queue[0];
  }

  add(item) {
    if (this.queue.some((q) => q.id === item.id)) return;
    this.queue.push({ ...item, shownAt: performance.now() });
    if (this.queue.length === 1) this.render();
  }

  resolved(id) {
    const wasCurrent = this.current?.id === id;
    this.queue = this.queue.filter((q) => q.id !== id);
    if (wasCurrent) this.render();
  }

  reset(items) {
    this.queue = [];
    for (const item of items) this.add(item);
    if (!items.length) this.render();
  }

  render() {
    const item = this.current;
    $("confirm").hidden = !item;
    if (!item) return;
    item.shownAt = performance.now();
    $("confirm-risk").textContent = RISK_LABELS[item.risk] ?? item.risk;
    $("confirm-summary").textContent = item.summary; // texto plano, nunca HTML
    // La pista de los gestos (visible solo con la cámara encendida, ver camera.js). El «Sí» con gesto solo vale para
    // riesgo bajo o de escritura: lo decide ARTHUR (gestos/actions.py); aquí solo se explica.
    $("confirm-gesture").textContent = YES_BY_GESTURE.has(item.risk)
      ? "Con gestos: 👍/👌 mantenido (1,5 s) = Sí · 👎/✊ (1 s) = No"
      : "Con gestos: 👎/✊ (1 s) = No · el Sí, solo con el clic";
    $("confirm-no").focus(); // por defecto, el botón seguro
  }

  // Barra de tiempo restante (se llama en cada fotograma).
  tick() {
    const item = this.current;
    if (!item) return;
    const left = Math.max(0, 1 - (performance.now() - item.shownAt) / (item.timeout * 1000));
    $("confirm-timer").firstElementChild.style.transform = `scaleX(${left})`;
  }

  async respond(answer) {
    const item = this.current;
    if (!item) return;
    this.resolved(item.id);
    try {
      const response = await fetch("/api/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Arthur": "1" },
        body: JSON.stringify({ id: item.id, answer }),
      });
      if (!response.ok) console.warn("ARTHUR no aceptó la respuesta:", await response.text());
    } catch (error) {
      console.warn("No he podido enviar la respuesta:", error);
    }
  }
}
