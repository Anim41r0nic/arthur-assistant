// Cámara de los gestos: el punto rojo fijo mientras está encendida y el botón para encenderla o apagarla.
//
// Lo único que este botón manda a ARTHUR es {"enabled": true|false}. El punto rojo NO se fía del clic: solo
// cambia cuando ARTHUR dice que la cámara se ha encendido o apagado (evento camera_state, o «hello» al conectar).
// Así, si ARTHUR la apaga solo (unos minutos sin gestos, al bloquear la pantalla…), el Orb lo refleja.

const $ = (id) => document.getElementById(id);

export class CameraIndicator {
  constructor() {
    this.on = false;
    $("camera-toggle").addEventListener("click", () => this.toggle());
  }

  // Al conectar: ¿hay gestos permitidos (botón visible)? ¿está encendida ahora?
  reset(status) {
    $("camera-toggle").hidden = status?.available !== true;
    this.set(status?.on === true);
  }

  set(on) {
    this.on = on === true;
    $("camera").hidden = !this.on;
    $("confirm-gesture").hidden = !this.on;  // la pista del pulgar abajo, solo si la cámara te está viendo
    $("camera-toggle").textContent = this.on ? "Apagar cámara" : "Gestos: encender cámara";
  }

  async toggle() {
    const button = $("camera-toggle");
    button.disabled = true;
    try {
      const response = await fetch("/api/gestures", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Arthur": "1" },
        body: JSON.stringify({ enabled: !this.on }),
      });
      if (!response.ok) console.warn("ARTHUR no aceptó el botón de la cámara:", await response.text());
    } catch (error) {
      console.warn("No he podido enviar el botón de la cámara:", error);
    } finally {
      button.disabled = false;
    }
  }
}
