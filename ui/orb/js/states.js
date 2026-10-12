// Cómo se ve el Orb en cada estado. El Orb no salta de uno a otro: interpola
// suavemente hacia estos valores (ver Orb.update).
//
//   color   color principal de las partículas y anillos
//   energy  cuánto se deforma y brilla la esfera (0 = calma, 1 = máximo)
//   spin    velocidad de giro
//   bloom   intensidad del resplandor
//   pulse   frecuencia del latido del núcleo (veces por segundo)

export const STATES = {
  idle:      { label: "IDLE",      desc: "En espera",           color: 0x1aff6e, energy: 0.18, spin: 0.10, bloom: 0.9, pulse: 0.35 },
  listening: { label: "LISTENING", desc: "Escuchando",          color: 0x3de8ff, energy: 0.45, spin: 0.18, bloom: 1.1, pulse: 0.8 },
  thinking:  { label: "THINKING",  desc: "Pensando",            color: 0x8dffbf, energy: 0.85, spin: 0.55, bloom: 1.5, pulse: 1.6 },
  executing: { label: "EXECUTING", desc: "Usando herramientas", color: 0xffb020, energy: 0.7,  spin: 0.35, bloom: 1.35, pulse: 2.2 },
  speaking:  { label: "SPEAKING",  desc: "Respondiendo",        color: 0x1aff6e, energy: 0.6,  spin: 0.2,  bloom: 1.25, pulse: 3.0 },
  error:     { label: "ERROR",     desc: "Algo ha fallado",     color: 0xff3355, energy: 0.95, spin: 0.05, bloom: 1.6, pulse: 6.0 },
};

// Tiempo mínimo que se muestra cada estado. El núcleo cambia de estado en
// milisegundos; sin esto, algunos estados ni se verían.
export const MIN_DWELL_MS = { idle: 0, listening: 300, thinking: 450, executing: 600, speaking: 900, error: 1800 };

// En modo texto "hablar" es instantáneo: mantenemos SPEAKING un rato según la
// longitud de la respuesta, como si se estuviera leyendo en voz alta.
export function speakingDwell(text) {
  return Math.min(7000, Math.max(MIN_DWELL_MS.speaking, 600 + text.length * 35));
}
