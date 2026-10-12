// El Orb: escena 3D con Three.js.
//
// Capas, de dentro hacia fuera:
//   1. Núcleo: icosaedro de alambre + esfera de brillo que late.
//   2. Esfera de partículas (4.000 puntos) que se deforma con ruido 3D en la GPU.
//   3. Anillos inclinados que giran, y un anillo de "escaneo" que sube y baja.
//   4. Polvo en órbita alrededor.
// Encima, el efecto bloom (resplandor) del postprocesado.
//
// Cada fotograma, los parámetros actuales se acercan poco a poco a los del estado
// objetivo (interpolación), así los cambios de estado son suaves.

import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { STATES } from "./states.js";

// --- Shaders de la esfera de partículas ------------------------------------

const SHELL_VERTEX = /* glsl */ `
  uniform float uTime;
  uniform float uEnergy;
  uniform float uPulse;
  uniform float uPixelRatio;
  attribute float aSeed;
  varying float vGlow;

  // Ruido 3D suave ("value noise"): valores pseudoaleatorios en una rejilla,
  // interpolados con una curva suave entre las esquinas de cada celda.
  float hash(vec3 p) {
    p = fract(p * 0.3183099 + 0.1);
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
  }
  float noise(vec3 x) {
    vec3 i = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash(i), hash(i + vec3(1, 0, 0)), f.x),
          mix(hash(i + vec3(0, 1, 0)), hash(i + vec3(1, 1, 0)), f.x), f.y),
      mix(mix(hash(i + vec3(0, 0, 1)), hash(i + vec3(1, 0, 1)), f.x),
          mix(hash(i + vec3(0, 1, 1)), hash(i + vec3(1, 1, 1)), f.x), f.y),
      f.z);
  }

  void main() {
    vec3 dir = normalize(position);
    float n = noise(dir * 2.2 + vec3(0.0, uTime * 0.35, uTime * 0.2));
    // Cada punto se separa del centro según el ruido, la energía del estado y el latido.
    float offset = (n - 0.5) * (0.12 + uEnergy * 0.4) + uPulse * 0.06;
    vec3 pos = position + dir * offset;
    vGlow = smoothstep(0.35, 0.85, n) * (0.5 + uEnergy) + aSeed * 0.15;

    vec4 mv = modelViewMatrix * vec4(pos, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = (1.4 + vGlow * 2.0) * uPixelRatio * (6.0 / -mv.z);
  }
`;

const SHELL_FRAGMENT = /* glsl */ `
  uniform vec3 uColor;
  varying float vGlow;
  void main() {
    // Punto redondo y difuminado en lugar de un cuadrado.
    float d = length(gl_PointCoord - 0.5);
    float alpha = smoothstep(0.5, 0.0, d);
    // Color del estado, con solo un toque de blanco en las zonas más activas.
    vec3 color = mix(uColor, vec3(1.0), vGlow * 0.12);
    gl_FragColor = vec4(color * (0.3 + vGlow * 0.55), alpha * (0.25 + vGlow * 0.5));
  }
`;

// Reparte N puntos uniformemente sobre una esfera (espiral de Fibonacci).
function fibonacciSphere(count, radius) {
  const positions = new Float32Array(count * 3);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const y = 1 - (i / (count - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const theta = golden * i;
    positions.set([Math.cos(theta) * r * radius, y * radius, Math.sin(theta) * r * radius], i * 3);
  }
  return positions;
}

function softDotTexture() {
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.4, "rgba(255,255,255,0.35)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  return new THREE.CanvasTexture(canvas);
}

const additive = { transparent: true, blending: THREE.AdditiveBlending, depthWrite: false };

export class Orb {
  constructor(canvas) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
    // Limitar la resolución interna: la GPU integrada lo agradece.
    this.pixelRatio = Math.min(window.devicePixelRatio, 1.5);
    this.renderer.setPixelRatio(this.pixelRatio);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
    this.camera.position.set(0, 0.2, 7.2);

    this.group = new THREE.Group();
    this.scene.add(this.group);

    // Valores actuales (se interpolan hacia el estado objetivo).
    const idle = STATES.idle;
    this.current = { energy: idle.energy, spin: idle.spin, bloom: idle.bloom, pulse: idle.pulse };
    this.color = new THREE.Color(idle.color);
    this.target = STATES.idle;
    this.targetColor = new THREE.Color(idle.color);
    this.phase = 0;
    this.audioLevel = 0;       // volumen de la voz suavizado (0-1)
    this.audioTarget = 0;      // último volumen recibido de ARTHUR
    this.pointer = new THREE.Vector2();

    // Three.js copia los colores al crear cada material: guardamos aquí los materiales
    // que deben cambiar de color con el estado y los actualizamos en cada fotograma.
    this.tinted = [];

    this.#buildCore();
    this.#buildShell();
    this.#buildRings();
    this.#buildDust();
    this.#buildComposer();

    window.addEventListener("resize", () => this.resize());
    window.addEventListener("pointermove", (e) => {
      this.pointer.set(e.clientX / window.innerWidth - 0.5, e.clientY / window.innerHeight - 0.5);
    });
    this.resize();
    this.clock = new THREE.Clock();
  }

  #buildCore() {
    this.coreWire = new THREE.LineSegments(
      new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(0.42, 1)),
      new THREE.LineBasicMaterial({ color: this.color, opacity: 0.8, ...additive }),
    );
    this.coreGlow = new THREE.Mesh(
      new THREE.SphereGeometry(0.3, 32, 32),
      new THREE.MeshBasicMaterial({ color: this.color, opacity: 0.55, ...additive }),
    );
    this.halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: softDotTexture(), color: this.color, opacity: 0.35, ...additive }));
    this.halo.scale.setScalar(2.6);
    this.tinted.push(this.coreWire.material, this.coreGlow.material, this.halo.material);
    this.group.add(this.coreWire, this.coreGlow, this.halo);
  }

  #buildShell() {
    const count = 4000;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(fibonacciSphere(count, 1.55), 3));
    const seeds = new Float32Array(count).map(() => Math.random());
    geometry.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 1));
    this.shellUniforms = {
      uTime: { value: 0 },
      uEnergy: { value: this.current.energy },
      uPulse: { value: 0 },
      uColor: { value: this.color },
      uPixelRatio: { value: this.pixelRatio },
    };
    this.shell = new THREE.Points(geometry, new THREE.ShaderMaterial({
      uniforms: this.shellUniforms, vertexShader: SHELL_VERTEX, fragmentShader: SHELL_FRAGMENT, ...additive,
    }));
    this.group.add(this.shell);
  }

  #buildRings() {
    const ring = (radius, opacity, dashed) => {
      const points = new THREE.EllipseCurve(0, 0, radius, radius).getPoints(256).map((p) => new THREE.Vector3(p.x, p.y, 0));
      const geometry = new THREE.BufferGeometry().setFromPoints(points);
      const material = dashed
        ? new THREE.LineDashedMaterial({ color: this.color, opacity, dashSize: 0.12, gapSize: 0.08, ...additive })
        : new THREE.LineBasicMaterial({ color: this.color, opacity, ...additive });
      this.tinted.push(material);
      const line = new THREE.Line(geometry, material);
      if (dashed) line.computeLineDistances();
      return line;
    };
    this.rings = [
      Object.assign(ring(2.05, 0.55, false), { userData: { axis: new THREE.Vector3(0.3, 1, 0).normalize(), speed: 0.6 } }),
      Object.assign(ring(2.3, 0.35, true), { userData: { axis: new THREE.Vector3(1, 0.2, 0.4).normalize(), speed: -0.4 } }),
      Object.assign(ring(2.55, 0.22, true), { userData: { axis: new THREE.Vector3(-0.2, 0.6, 1).normalize(), speed: 0.25 } }),
    ];
    this.rings[0].rotation.x = 1.2;
    this.rings[1].rotation.y = 0.9;
    this.rings[2].rotation.x = -0.5;
    // Anillo de escaneo: horizontal, sube y baja a lo largo de la esfera.
    this.scan = ring(1.6, 0.0, false);
    this.scan.rotation.x = Math.PI / 2;
    this.group.add(...this.rings, this.scan);
  }

  #buildDust() {
    const count = 1400;
    const positions = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      // Un disco grueso alrededor del Orb, más denso cerca de él.
      const radius = 2.6 + Math.pow(Math.random(), 1.8) * 3.2;
      const angle = Math.random() * Math.PI * 2;
      positions.set([Math.cos(angle) * radius, (Math.random() - 0.5) * 0.9 * (radius / 3), Math.sin(angle) * radius], i * 3);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    this.dust = new THREE.Points(geometry, new THREE.PointsMaterial({
      map: softDotTexture(), color: this.color, size: 0.05, opacity: 0.6, ...additive,
    }));
    this.tinted.push(this.dust.material);
    this.dust.rotation.x = 0.25;
    this.scene.add(this.dust);
  }

  #buildComposer() {
    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), this.current.bloom, 0.5, 0.12);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());
  }

  resize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
  }

  setAudioLevel(level) {
    this.audioTarget = Math.max(0, Math.min(1, level));
  }

  setState(name) {
    this.target = STATES[name] ?? STATES.idle;
    this.targetColor.setHex(this.target.color);
  }

  // Bucle de animación: se llama en cada fotograma.
  update() {
    const dt = Math.min(this.clock.getDelta(), 0.1);
    const t = this.clock.elapsedTime;
    const k = 1 - Math.exp(-dt * 3); // cuánto nos acercamos al objetivo en este fotograma

    for (const key of ["energy", "spin", "bloom", "pulse"]) {
      this.current[key] += (this.target[key] - this.current[key]) * k;
    }
    this.color.lerp(this.targetColor, k);
    // La voz sube rápido y baja más despacio, como el VU-metro de un equipo de música.
    const attack = this.audioTarget > this.audioLevel ? 0.5 : 0.12;
    this.audioLevel += (this.audioTarget - this.audioLevel) * attack;
    for (const material of this.tinted) material.color.copy(this.color);

    // Latido: una onda "afilada" (seno elevado a una potencia) para que sea un golpe, no una ola.
    this.phase += dt * this.current.pulse * Math.PI * 2;
    const beat = Math.pow((Math.sin(this.phase) + 1) / 2, 6);
    const flicker = this.target === STATES.error ? (Math.random() > 0.85 ? 0.5 : 1) : 1;

    this.shellUniforms.uTime.value = t;
    this.shellUniforms.uEnergy.value = this.current.energy + this.audioLevel * 0.6;
    this.shellUniforms.uPulse.value = beat * this.current.energy;

    this.group.rotation.y += dt * this.current.spin;
    this.coreWire.rotation.x -= dt * this.current.spin * 1.7;
    this.coreWire.rotation.z += dt * this.current.spin * 1.1;
    const coreScale = 1 + beat * 0.25 * (0.4 + this.current.energy) + this.audioLevel * 0.45;
    this.coreGlow.scale.setScalar(coreScale);
    this.coreWire.scale.setScalar(1 + beat * 0.1);
    this.coreGlow.material.opacity = (0.3 + beat * 0.35) * flicker;
    this.halo.material.opacity = (0.15 + this.current.energy * 0.18 + beat * 0.12) * flicker;

    for (const ring of this.rings) {
      ring.rotateOnAxis(ring.userData.axis, dt * ring.userData.speed * (0.4 + this.current.spin * 2));
    }
    // El escaneo solo se ve cuando ARTHUR está trabajando.
    const scanning = this.target === STATES.executing || this.target === STATES.thinking;
    const scanOpacity = this.scan.material.opacity + ((scanning ? 0.6 : 0) - this.scan.material.opacity) * k;
    this.scan.material.opacity = scanOpacity;
    const y = Math.sin(t * 1.4) * 1.45;
    this.scan.position.y = y;
    this.scan.scale.setScalar(Math.sqrt(Math.max(0.02, 1 - (y / 1.6) ** 2)));

    this.dust.rotation.y += dt * (0.02 + this.current.spin * 0.08);
    this.bloom.strength = this.current.bloom * 0.7 * (0.9 + beat * 0.25) * flicker;

    // Ligero movimiento de cámara con el ratón (paralaje).
    this.camera.position.x += (this.pointer.x * 0.8 - this.camera.position.x) * k;
    this.camera.position.y += (0.2 - this.pointer.y * 0.6 - this.camera.position.y) * k;
    this.camera.lookAt(0, 0, 0);

    this.composer.render();
  }
}
