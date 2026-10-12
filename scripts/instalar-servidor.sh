#!/usr/bin/env bash
# Instala ARTHUR en un servidor con Docker (Debian, Ubuntu…). Se ejecuta desde la carpeta del repositorio:
#
#     bash scripts/instalar-servidor.sh             te pregunta cada opción (la recomendada sale en verde)
#     bash scripts/instalar-servidor.sh --si        acepta todas las recomendadas sin preguntar
#     bash scripts/instalar-servidor.sh --solo-ficheros   solo escribe .env, config y llaves (no descarga ni arranca nada)
#
# Qué hace: te pregunta el modelo de IA, si dejarlo siempre cargado, si usar la gráfica integrada y si publicar
# ARTHUR en tu red Tailscale; crea .env, config/servidor.toml y las llaves internas (secrets/, permisos 600);
# descarga los modelos de voz comprobando su huella sha256; construye y arranca los contenedores; descarga el modelo
# de IA y da de alta tu primer aparato. No usa sudo para nada salvo, si hace falta, para dar las llaves al usuario
# 1000 (con el que corren los contenedores). Puedes volver a ejecutarlo: lo que ya existe, lo respeta.
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f compose.yaml && -f Dockerfile ]] || { echo "Ejecútalo desde la carpeta de ARTHUR (no encuentro compose.yaml)"; exit 1; }

ACEPTAR_TODO=0
SOLO_FICHEROS=0
for arg in "$@"; do
  case "$arg" in
    --si) ACEPTAR_TODO=1 ;;
    --solo-ficheros) SOLO_FICHEROS=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Opción desconocida: $arg"; exit 2 ;;
  esac
done

if [[ -t 1 ]]; then VERDE=$'\e[1;32m'; GRIS=$'\e[2m'; NEGRITA=$'\e[1m'; ROJO=$'\e[1;31m'; FIN=$'\e[0m'
else VERDE=""; GRIS=""; NEGRITA=""; ROJO=""; FIN=""; fi

titulo() { printf '\n%s== %s ==%s\n' "$NEGRITA" "$1" "$FIN"; }
aviso()  { printf '%s⚠ %s%s\n' "$ROJO" "$1" "$FIN"; }

# elegir VARIABLE "pregunta" RECOMENDADA "opción 1" "explicación 1" "opción 2" "explicación 2" …
# Deja en VARIABLE el número elegido (1, 2…). La recomendada sale en verde y es la que vale con Enter.
elegir() {
  local __var=$1 pregunta=$2 recomendada=$3; shift 3
  local -a nombres=() ayudas=()
  while (($#)); do nombres+=("$1"); ayudas+=("$2"); shift 2; done
  printf '\n%s\n' "$pregunta"
  local i
  for i in "${!nombres[@]}"; do
    local n=$((i + 1))
    if ((n == recomendada)); then
      printf '  %s%d) %s (recomendado)%s\n     %s%s%s\n' "$VERDE" "$n" "${nombres[$i]}" "$FIN" "$GRIS" "${ayudas[$i]}" "$FIN"
    else
      printf '  %d) %s\n     %s%s%s\n' "$n" "${nombres[$i]}" "$GRIS" "${ayudas[$i]}" "$FIN"
    fi
  done
  local respuesta=""
  if ((ACEPTAR_TODO)); then respuesta=$recomendada; echo "  → $respuesta"
  else
    while true; do
      read -rp "Elige [${recomendada}]: " respuesta
      respuesta=${respuesta:-$recomendada}
      [[ $respuesta =~ ^[0-9]+$ ]] && ((respuesta >= 1 && respuesta <= ${#nombres[@]})) && break
      echo "  Escribe un número del 1 al ${#nombres[@]}."
    done
  fi
  printf -v "$__var" '%s' "$respuesta"
}

preguntar() {  # preguntar VARIABLE "pregunta" "valor por defecto"
  local __var=$1 pregunta=$2 defecto=$3 respuesta=""
  if ((ACEPTAR_TODO)); then respuesta=$defecto
  else read -rp "$pregunta [${defecto}]: " respuesta; respuesta=${respuesta:-$defecto}; fi
  printf -v "$__var" '%s' "$respuesta"
}

# --- 0. Lo que hace falta ---------------------------------------------------------------------------------------
titulo "Comprobaciones"
if ((! SOLO_FICHEROS)); then
  command -v docker >/dev/null || { aviso "Falta Docker: https://docs.docker.com/engine/install/ (y el plugin compose)"; exit 1; }
  docker compose version >/dev/null 2>&1 || { aviso "Falta «docker compose» (el plugin compose de Docker)"; exit 1; }
  docker info >/dev/null 2>&1 || { aviso "No puedo usar Docker con tu usuario: añádete al grupo docker (sudo usermod -aG docker \$USER) y vuelve a entrar"; exit 1; }
  command -v curl >/dev/null || { aviso "Falta curl (sudo apt install curl)"; exit 1; }
fi
command -v python3 >/dev/null || { aviso "Falta python3 (sudo apt install python3)"; exit 1; }
RAM_GB=$(awk '/MemTotal/ {printf "%d", ($2 / 1048576) + 0.5}' /proc/meminfo)
echo "RAM: ${RAM_GB} GB"

# --- 1. El modelo de IA -----------------------------------------------------------------------------------------
titulo "1/5 · El modelo de IA (el «cerebro»)"
echo "Corre en este servidor con Ollama: gratis y sin que tus conversaciones salgan de aquí."
elegir OPC_MODELO "¿Qué modelo quieres?" 1 \
  "qwen3-vl:4b-instruct" "Texto, herramientas y fotos (dice qué hay en una imagen). ~3,3 GB de descarga; va en 8 GB de RAM." \
  "qwen3:4b-instruct" "Solo texto y herramientas, sin fotos. Algo más ligero." \
  "Otro de Ollama" "Escribes su nombre (https://ollama.com/library). Comprueba antes cuánta RAM necesita."
case $OPC_MODELO in
  1) MODELO="qwen3-vl:4b-instruct"; VISION=true ;;
  2) MODELO="qwen3:4b-instruct"; VISION=false ;;
  3) preguntar MODELO "Nombre del modelo (p. ej. qwen3:8b)" "qwen3:4b-instruct"
     elegir OPC_VISION "¿Ese modelo ve fotos?" 2 "Sí" "La web dejará adjuntar fotos." \
       "No / no lo sé" "Si no las ve, la web no deja adjuntarlas (mejor que una respuesta inventada)."
     [[ $OPC_VISION == 1 ]] && VISION=true || VISION=false ;;
esac

# --- 2. ¿Siempre cargado? ---------------------------------------------------------------------------------------
titulo "2/5 · ¿Dejar el modelo siempre cargado en memoria?"
((RAM_GB >= 14)) && REC_CARGA=2 || REC_CARGA=1
elegir OPC_CARGA "Ocupa ~4 GB de RAM mientras está cargado." "$REC_CARGA" \
  "Que se descargue si no lo usas (30 min)" "Deja la RAM libre para otras cosas. La primera pregunta tras un rato sin usarlo tarda más (en un servidor de 8 GB solo con procesador, ~2 min)." \
  "Siempre cargado" "Responde rápido siempre, pero esos ~4 GB quedan ocupados para siempre. Recomendado con 16 GB o más."
[[ $OPC_CARGA == 2 ]] && KEEP_ALIVE="-1m" || KEEP_ALIVE="30m"

# --- 3. La gráfica integrada ------------------------------------------------------------------------------------
titulo "3/5 · La gráfica"
GPU_RENDER=""
for nodo in /sys/class/drm/renderD*; do
  [[ -e $nodo/device/vendor ]] || continue
  fabricante=$(cat "$nodo/device/vendor")
  [[ $fabricante == 0x8086 || $fabricante == 0x1002 ]] || continue   # Intel o AMD
  pci=$(basename "$(readlink -f "$nodo/device")")
  [[ -e /dev/dri/by-path/pci-${pci}-render ]] && { GPU_RENDER="/dev/dri/by-path/pci-${pci}-render"; break; }
done
USAR_GPU=0
if [[ -n $GPU_RENDER ]]; then
  echo "He encontrado una gráfica Intel/AMD: $GPU_RENDER"
  elegir OPC_GPU "¿Que la IA la use (Vulkan)?" 1 \
    "Sí, usar la gráfica" "Con el contexto largo de ARTHUR genera ~2 veces más rápido (medido en una Intel Iris Plus). No ocupa más RAM." \
    "No, solo el procesador" "Más lento, pero sin depender del driver de la gráfica."
  [[ $OPC_GPU == 1 ]] && USAR_GPU=1
else
  echo "No he encontrado una gráfica Intel o AMD que sirva: la IA usará el procesador."
fi

# --- 4. Tailscale -----------------------------------------------------------------------------------------------
titulo "4/5 · Cómo llegan tus aparatos a ARTHUR"
TS_IP=""; TS_NOMBRE=""
if command -v tailscale >/dev/null; then
  TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
  TS_NOMBRE=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
fi
USAR_TS=0
if [[ -n $TS_IP ]]; then
  echo "Tailscale está en marcha: este servidor es ${TS_IP}${TS_NOMBRE:+ (${TS_NOMBRE})}."
  elegir OPC_TS "¿Publicar ARTHUR en tu red Tailscale?" 1 \
    "Sí" "Tus aparatos (portátil, Windows, móvil) le hablan por la red privada, desde cualquier sitio. Ni tu red de casa ni internet lo ven." \
    "No, solo en este servidor" "Solo 127.0.0.1. Tendrás que darle acceso tú (p. ej. tailscale serve o un túnel SSH)."
  [[ $OPC_TS == 1 ]] && USAR_TS=1
else
  aviso "No veo Tailscale. Sin una red privada, ARTHUR solo escucha en este servidor (127.0.0.1)."
  echo "  Recomendado: instala Tailscale (https://tailscale.com/download) en el servidor y en tus aparatos y vuelve a ejecutar esto."
fi

# --- 5. Hora y avisos -------------------------------------------------------------------------------------------
titulo "5/5 · Zona horaria y avisos al móvil"
ZONA=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "Europe/Madrid")
preguntar ZONA "Zona horaria" "$ZONA"
if [[ -n $TS_NOMBRE ]]; then SUJETO_DEF="https://${TS_NOMBRE}"; else SUJETO_DEF="mailto:tu@correo.com"; fi
echo "Apple y Google piden saber quién manda los avisos: la dirección de tu ARTHUR (https://…) o un correo (mailto:…)."
echo "No se publica en ningún sitio: solo va dentro de cada aviso, cifrado."
preguntar SUJETO "Quién manda los avisos" "$SUJETO_DEF"
[[ $SUJETO == https://* || $SUJETO == mailto:* ]] || { aviso "Debe empezar por https:// o mailto:"; exit 1; }

# --- Escribir la configuración ----------------------------------------------------------------------------------
titulo "Configuración"
COMPOSE_FILE="compose.yaml"
((USAR_TS)) && COMPOSE_FILE+=":compose.tailscale.yaml"
((USAR_GPU)) && COMPOSE_FILE+=":compose.gpu.yaml"
escribir() {  # escribir FICHERO (contenido por la entrada estándar); si ya existe, pregunta antes de cambiarlo
  local fichero=$1 contenido; contenido=$(cat)
  if [[ -f $fichero ]] && [[ "$(cat "$fichero")" != "$contenido" ]]; then
    local r="s"; ((ACEPTAR_TODO)) || read -rp "$fichero ya existe y cambia. ¿Lo sustituyo? (se guarda una copia .antes) [S/n]: " r
    [[ ${r:-s} =~ ^[sSyY] ]] || { echo "  $fichero: sin tocar"; return; }
    cp "$fichero" "$fichero.antes"
  fi
  printf '%s\n' "$contenido" > "$fichero"; echo "  $fichero ✔"
}
escribir .env <<FIN_ENV
# Creado por scripts/instalar-servidor.sh (puedes editarlo; explicación en .env.example)
COMPOSE_FILE=${COMPOSE_FILE}
TZ=${ZONA}
ARTHUR_PUSH_SUBJECT=${SUJETO}
ARTHUR_TAILSCALE_IP=${TS_IP}
ARTHUR_GPU_RENDER=${GPU_RENDER}
FIN_ENV
escribir config/servidor.toml <<FIN_CONF
# Creado por scripts/instalar-servidor.sh. Para cambiar algo: edítalo y docker compose restart core.
# Todas las opciones: config/config.example.toml

[brain]
ollama_url = "http://ollama:11434"
model = "${MODELO}"
vision = ${VISION}
timeout = 300
# "30m": se descarga si no le hablas en 30 min · "-1m": siempre cargado
keep_alive = "${KEEP_ALIVE}"
FIN_CONF

# Las llaves internas (core ↔ voz, core ↔ push). Nunca salen del servidor; los contenedores corren como el usuario 1000.
umask 077
mkdir -p secrets
for llave in voz_token push_token; do
  [[ -s secrets/$llave ]] || { python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "secrets/$llave"; echo "  secrets/$llave ✔ (nueva)"; }
done
chmod 700 secrets; chmod 600 secrets/*
if [[ $(id -u) != 1000 ]]; then
  aviso "Tu usuario no es el 1000, que es con el que corren los contenedores: les doy las llaves con sudo."
  sudo chown 1000:1000 secrets secrets/*
fi
umask 022

if ((SOLO_FICHEROS)); then echo; echo "Listo (solo ficheros). Para seguir: bash scripts/instalar-servidor.sh"; exit 0; fi

# --- Modelos de voz (comprobados por sha256) --------------------------------------------------------------------
titulo "Modelos de voz"
descargar() {  # descargar URL DESTINO SHA256
  local url=$1 destino=$2 huella=$3
  if [[ -f $destino ]] && [[ -z $huella || "$(sha256sum "$destino" | cut -d' ' -f1)" == "$huella" ]]; then
    echo "  $destino ✔ (ya estaba)"; return
  fi
  mkdir -p "$(dirname "$destino")"
  curl -fL --progress-bar -o "$destino.parcial" "$url"
  if [[ -n $huella && "$(sha256sum "$destino.parcial" | cut -d' ' -f1)" != "$huella" ]]; then
    rm -f "$destino.parcial"; aviso "La huella de $destino no coincide: no lo uso"; exit 1
  fi
  mv "$destino.parcial" "$destino"; echo "  $destino ✔"
}
# whisper.cpp (MIT): el oído. Piper es-ES davefx (CC0): la voz. Las mismas huellas que comprueba arthur-voz al arrancar.
descargar https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin \
  modelos/whisper/ggml-small-q5_1.bin ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb
VOZ=https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium
descargar "$VOZ/es_ES-davefx-medium.onnx" modelos/voices/es_ES-davefx-medium.onnx \
  6658b03b1a6c316ee4c265a9896abc1393353c2d9e1bca7d66c2c442e222a917
descargar "$VOZ/es_ES-davefx-medium.onnx.json" modelos/voices/es_ES-davefx-medium.onnx.json ""

# --- Construir y arrancar ---------------------------------------------------------------------------------------
titulo "Construir y arrancar (la primera vez tarda: compila whisper.cpp para ESTE procesador)"
docker compose build
docker compose up -d
echo "Descargando el modelo de IA ${MODELO} (varios GB)…"
docker compose exec -T ollama ollama pull "$MODELO"
docker compose restart core >/dev/null

# --- Primer aparato ---------------------------------------------------------------------------------------------
titulo "Tu primer aparato"
echo "Cada aparato (móvil, portátil, navegador…) tiene su propia llave, que puedes retirar cuando quieras."
preguntar APARATO "Nombre del primer aparato" "movil"
docker compose exec -T core python -m arthur --dispositivo-nuevo "$APARATO" || true

titulo "Hecho"
if ((USAR_TS)) && [[ -n $TS_NOMBRE ]]; then
  cat <<FIN_TS
Para usar la web con micrófono y avisos (necesitan HTTPS), publica ARTHUR en tu red Tailscale con HTTPS:
    sudo tailscale serve --bg 8770
y abre en cada aparato:  https://${TS_NOMBRE}
FIN_TS
else
  echo "La web: http://127.0.0.1:8770 (en este servidor). Para usarla desde otros aparatos, mira docs/servidor.md."
fi
cat <<'FIN'

Siguientes pasos (docs/servidor.md):
  · Más aparatos:            docker compose exec core python -m arthur --dispositivo-nuevo NOMBRE
  · Retirar uno:             docker compose exec core python -m arthur --revocar NOMBRE
  · Tu ordenador Linux con «Hey Arthur», Orb y gestos:  bash scripts/instalar-aparato.sh  (en ese ordenador)
  · Google Calendar y Gmail (opcional): docs/google.md
  · Cambiar opciones: config/servidor.toml y .env, y luego  docker compose up -d && docker compose restart core
FIN
