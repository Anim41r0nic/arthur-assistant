#!/usr/bin/env bash
# Prepara un ordenador Linux como APARATO de tu ARTHUR (el cerebro está en el servidor: docs/servidor.md).
# Se ejecuta en ese ordenador, desde la carpeta del repositorio, con tu usuario (sin sudo):
#
#     bash scripts/instalar-aparato.sh          te pregunta cada opción (la recomendada sale en verde)
#     bash scripts/instalar-aparato.sh --si     acepta todas las recomendadas
#
# Qué hace: `arthur` desde cualquier carpeta (~/.local/bin), te empareja con el servidor (pegas la llave del
# aparato), crea ~/.config/arthur/config.toml con lo que elijas («Hey Arthur», gestos, avisos) y deja el servicio
# de usuario arthur-cliente arrancando con tu escritorio. No instala paquetes: si falta alguno, te dice cuál.
set -euo pipefail

cd "$(dirname "$0")/.."
RAIZ=$(pwd)
[[ -f arthur/__main__.py ]] || { echo "Ejecútalo desde la carpeta de ARTHUR"; exit 1; }
ACEPTAR_TODO=0
[[ ${1:-} == --si ]] && ACEPTAR_TODO=1
[[ ${1:-} == -h || ${1:-} == --help ]] && { sed -n '2,10p' "$0"; exit 0; }

if [[ -t 1 ]]; then VERDE=$'\e[1;32m'; GRIS=$'\e[2m'; NEGRITA=$'\e[1m'; ROJO=$'\e[1;31m'; FIN=$'\e[0m'
else VERDE=""; GRIS=""; NEGRITA=""; ROJO=""; FIN=""; fi
titulo() { printf '\n%s== %s ==%s\n' "$NEGRITA" "$1" "$FIN"; }
aviso()  { printf '%s⚠ %s%s\n' "$ROJO" "$1" "$FIN"; }

elegir() {  # como en instalar-servidor.sh: la recomendada en verde y con Enter
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

# --- Lo que hace falta ------------------------------------------------------------------------------------------
titulo "Comprobaciones"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { aviso "Hace falta Python 3.11 o más nuevo"; exit 1; }
falta=()
for programa in pw-record pw-play notify-send; do command -v "$programa" >/dev/null || falta+=("$programa"); done
if ((${#falta[@]})); then
  aviso "Faltan: ${falta[*]} (PipeWire y libnotify: el micrófono, el altavoz y las notificaciones)"
  echo "  Arch: sudo pacman -S pipewire libnotify · Debian/Ubuntu: sudo apt install pipewire-bin libnotify-bin"
  exit 1
fi
echo "Python, micrófono, altavoz y notificaciones: ✔"

# --- `arthur` desde cualquier carpeta ---------------------------------------------------------------------------
mkdir -p ~/.local/bin
ln -sf "$RAIZ/scripts/arthur" ~/.local/bin/arthur
echo "Orden «arthur»: ~/.local/bin/arthur ✔"
[[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || aviso "~/.local/bin no está en tu PATH: añádelo para escribir «arthur» en la terminal"

# --- Emparejar --------------------------------------------------------------------------------------------------
titulo "1/4 · Emparejar con tu servidor"
if [[ -f ~/.config/arthur/cliente.toml ]]; then
  echo "Este aparato ya está emparejado (~/.config/arthur/cliente.toml)."
else
  echo "En el servidor, da de alta este aparato y copia su llave:"
  echo "    docker compose exec core python -m arthur --dispositivo-nuevo $(hostname -s)"
  read -rp "Dirección de tu ARTHUR (p. ej. https://servidor.tu-red.ts.net o http://100.x.y.z:8770): " URL
  python3 "$RAIZ/scripts/arthur" --emparejar "$URL"
fi

# --- Opciones ---------------------------------------------------------------------------------------------------
titulo "2/4 · «Hey Arthur»"
VOSK_DIR=~/.local/share/arthur/vosk/vosk-model-small-en-us-0.15
elegir OPC_WAKE "¿Que te escuche al decir «Hey Arthur»?" 1 \
  "Sí" "Escucha de fondo en ESTE ordenador (Vosk, ~1 % de CPU, sin guardar audio). Lo que dices después va a tu servidor." \
  "No, solo con el atajo" "Le hablas con un atajo de teclado (scripts/arthur-ptt)."
WAKE=false
if [[ $OPC_WAKE == 1 ]]; then
  WAKE=true
  python3 -c 'import vosk' 2>/dev/null || aviso "Falta Vosk para Python (Arch: sudo pacman -S python-vosk). Hasta entonces, solo el atajo."
  if [[ ! -d $VOSK_DIR ]]; then
    aviso "Falta el modelo de Vosk (~40 MB, Apache-2.0). Descárgalo de https://alphacephei.com/vosk/models:"
    echo "    mkdir -p ~/.local/share/arthur/vosk && cd ~/.local/share/arthur/vosk"
    echo "    curl -fLO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip && unzip vosk-model-small-en-us-0.15.zip"
  fi
fi

titulo "3/4 · Gestos con la cámara"
elegir OPC_GESTOS "¿Permitir los gestos (✋ calla, 👍/👎 sí/no…)?" 2 \
  "Sí, permitirlos" "La cámara NUNCA se enciende sola: la enciendes tú (botón del Orb o atajo). Necesita OpenCV (python-opencv) y bubblewrap. Ver docs/gestos.md." \
  "No" "Sin cámara. Puedes activarlo después en ~/.config/arthur/config.toml ([gestures] enabled = true)."
[[ $OPC_GESTOS == 1 ]] && GESTOS=true || GESTOS=false

titulo "4/4 · Avisos de mensajes"
elegir OPC_AVISOS "Cuando te llega un mensaje (p. ej. WhatsApp Web en el navegador), ARTHUR te avisa en voz alta:" 1 \
  "Solo quién te escribe" "«Te ha escrito Laura». El texto no se lee." \
  "Quién y qué dice" "Lee también el texto del mensaje." \
  "No avisar" "No escucha las notificaciones."
case $OPC_AVISOS in 1) AVISOS=true; TEXTO=false ;; 2) AVISOS=true; TEXTO=true ;; *) AVISOS=false; TEXTO=false ;; esac

CONF=~/.config/arthur/config.toml
mkdir -p ~/.config/arthur && chmod 700 ~/.config/arthur
if [[ -f $CONF ]]; then
  cp "$CONF" "$CONF.antes"
  echo "Ya tenías $CONF: guardo una copia en $CONF.antes y escribo uno nuevo."
fi
cat > "$CONF" <<FIN_CONF
# Creado por scripts/instalar-aparato.sh. Todas las opciones: config/config.example.toml

[notifications]
enabled = ${AVISOS}
announce_text = ${TEXTO}

[wake]
enabled = ${WAKE}

[gestures]
enabled = ${GESTOS}
FIN_CONF
chmod 600 "$CONF"
echo "Configuración: $CONF ✔"

# --- El servicio ------------------------------------------------------------------------------------------------
titulo "El servicio (arranca con tu escritorio)"
mkdir -p ~/.config/systemd/user
sed "s|%h/Proyectos/arthur|$RAIZ|g" scripts/systemd/arthur-cliente.service > ~/.config/systemd/user/arthur-cliente.service
systemctl --user daemon-reload
systemctl --user enable --now arthur-cliente.service
sleep 3
journalctl --user -u arthur-cliente -n 3 --no-pager -o cat || true

titulo "Hecho"
cat <<'FIN'
Prueba: di «Hey Arthur, ¿qué hora es?» o escribe  arthur  en una terminal.
Atajos de teclado (añádelos en tu escritorio; en Hyprland, en tu fichero de binds):
  · Hablar:                 scripts/arthur-ptt      (p. ej. Super+Shift+A)
  · Encender/apagar cámara: scripts/arthur-camara   (p. ej. Super+Shift+G)
Registro:  journalctl --user -u arthur-cliente -f
FIN
