#!/usr/bin/env bash
# Activa (o pone al día) los servicios opcionales de ARTHUR en un servidor ya instalado con
# scripts/instalar-servidor.sh. Se ejecuta en el servidor, desde la carpeta del repositorio:
#
#     bash scripts/desplegar.sh apuntes panel cerebro     activa esos perfiles (se suman a los que ya había)
#     bash scripts/desplegar.sh                           pone al día los que ya están en COMPOSE_PROFILES (.env)
#
# Perfiles: apuntes, panel, clientes, webchat, cerebro (qué es cada uno: compose.yaml y docs/servidor.md).
#
# Qué hace, por orden (si algo falla, se para ahí; se puede volver a lanzar: lo hecho no se repite):
#   1. Apunta los perfiles en .env (COMPOSE_PROFILES) y prepara secrets/ y las carpetas de las bóvedas.
#   2. Construye las imágenes y arranca el núcleo.
#   3. Crea las llaves internas que falten (cada servicio habla con los demás con su llave de aparato) y las guarda
#      en secrets/ (600) SIN enseñarlas: la orden las imprime y aquí van directas al fichero.
#   4. Conecta cada servicio con el núcleo en .env (ARTHUR_*_URL, solo red interna de Docker).
#   5. Levanta todo y comprueba la versión de cada contenedor.
#
# Lo que pones tú (no lo puede crear el script): secrets/workers_ai_token y secrets/turnstile_secret para el chat de
# la web y Workers AI, ARTHUR_CF_ACCOUNT y las direcciones *_PUBLIC_URL en .env (ver .env.example).
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f compose.yaml && -f .env ]] || { echo "✗ Ejecútalo en la carpeta de ARTHUR después de scripts/instalar-servidor.sh (falta .env)"; exit 1; }

VALIDOS=(apuntes panel clientes webchat cerebro)
OPCIONALES=(apuntes_core panel_core cerebro_core studio_core panel_avisos panel_google_key panel_studio cerebro_panel
            cerebro_apuntes_web cerebro_apuntes cerebro_avisos webchat_avisos clientes_webchat workers_ai_token
            turnstile_secret)

# --- Utilidades -----------------------------------------------------------------------------------------------
env_get() {  # env_get NOMBRE → su valor en .env (vacío si no está)
  sed -nE "s/^$1=(.*)$/\1/p" .env | tail -n 1
}
env_set() {  # env_set NOMBRE VALOR: lo cambia en .env o lo añade al final
  local tmp
  tmp=$(mktemp .env.XXXXXX)
  grep -v -E "^$1=" .env > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  chmod --reference=.env "$tmp" 2>/dev/null || true
  mv "$tmp" .env
}
env_default() {  # env_default NOMBRE VALOR: solo si aún está vacío (lo que hayas puesto a mano se respeta)
  [[ -n "$(env_get "$1")" ]] || { env_set "$1" "$2"; echo "   .env: $1=$2"; }
}

# secrets/ es del usuario 1000 (con el que corren los contenedores). Si tu usuario no es el 1000, se escribe con sudo.
if [[ -w secrets ]] || [[ ! -e secrets ]]; then SUDO=(); else SUDO=(sudo); fi
guardar() {  # guardar NOMBRE (el contenido llega por la entrada estándar)
  local f="secrets/$1"
  "${SUDO[@]}" sh -c "umask 077; cat > '$f.nuevo' && mv '$f.nuevo' '$f'"
  if [[ $(id -u) != 1000 ]]; then "${SUDO[@]}" chown 1000:1000 "$f" 2>/dev/null || sudo chown 1000:1000 "$f"; fi
}
lleno() { [[ -s "secrets/$1" ]] || "${SUDO[@]}" test -s "secrets/$1"; }

# nueva_llave SECRETO orden…: crea la llave de un aparato y la guarda sin enseñarla. Si el secreto ya tiene algo, no
# se toca. Todo «docker compose exec/run» lleva </dev/null para que no se coma la entrada del script.
nueva_llave() {
  local nombre=$1 salida llave
  shift
  if lleno "$nombre"; then echo "   secrets/$nombre ya existe: lo dejo"; return 0; fi
  if ! salida=$("$@" </dev/null 2>&1); then
    echo "✗ No se pudo crear la llave de secrets/$nombre. Lo que dijo (sin llaves):"
    printf '%s\n' "$salida" | grep -v 'arthur_' || true
    echo "  Si dice que el aparato ya existe, revócalo en ese servicio y vuelve a lanzar el script."
    exit 1
  fi
  llave=$(printf '%s\n' "$salida" | sed -nE 's/^[[:space:]]*(arthur_[A-Za-z0-9_-]{20,})[[:space:]]*$/\1/p' | head -n 1)
  [[ -n $llave ]] || { echo "✗ No encuentro la llave en lo que contestó la orden (secrets/$nombre)"; exit 1; }
  printf '%s\n' "$llave" | guardar "$nombre"
  unset llave salida
  echo "   secrets/$nombre creado (600, sin enseñarlo)"
}

core()   { docker compose exec -T core python -m arthur "$@"; }
correr() { local s=$1; shift; docker compose run --rm --no-deps -T "$s" python -m arthur "$@"; }

# --- 1. Perfiles, secrets y carpetas -------------------------------------------------------------------------------
echo "== 1/5 Perfiles, llaves y carpetas"
IFS=',' read -r -a ACTUALES <<< "$(env_get COMPOSE_PROFILES)"
PERFILES=()
for p in "${ACTUALES[@]}" "$@"; do
  [[ -n $p ]] || continue
  [[ " ${VALIDOS[*]} " == *" $p "* ]] || { echo "✗ Perfil desconocido: «$p» (hay: ${VALIDOS[*]})"; exit 2; }
  [[ " ${PERFILES[*]} " == *" $p "* ]] || PERFILES+=("$p")
done
((${#PERFILES[@]})) || { echo "Nada que hacer: di qué activar, p. ej.  bash scripts/desplegar.sh apuntes panel"; exit 0; }
tiene() { [[ " ${PERFILES[*]} " == *" $1 "* ]]; }
env_set COMPOSE_PROFILES "$(IFS=,; echo "${PERFILES[*]}")"
echo "   perfiles: ${PERFILES[*]}"

"${SUDO[@]}" mkdir -p secrets
for s in "${OPCIONALES[@]}"; do
  [[ -e secrets/$s ]] || "${SUDO[@]}" test -e "secrets/$s" || printf '' | guardar "$s"
done

if tiene panel || tiene cerebro; then
  # Si la carpeta no existe, Docker la crearía como root: se crea antes, tuya.
  BOVEDA=$(env_get ARTHUR_BOVEDA); BOVEDA=${BOVEDA:-./obsidian/cerebro}
  NEGOCIO=$(env_get ARTHUR_BOVEDA_NEGOCIO); NEGOCIO=${NEGOCIO:-./obsidian/negocio}
  mkdir -p "$BOVEDA" "$NEGOCIO"
  echo "   bóvedas: $BOVEDA y $NEGOCIO (llénalas con Syncthing; docs/cerebro.md)"
fi
if tiene webchat; then
  CONOCIMIENTO=$(env_get ARTHUR_WEBCHAT_CONOCIMIENTO); CONOCIMIENTO=${CONOCIMIENTO:-./conocimiento.md}
  if [[ ! -f $CONOCIMIENTO ]]; then
    printf '# Conocimiento\n\nEscribe aquí lo que el chat de tu web puede contar (servicios, horarios…).\n\n## Precios\n\n[PRECIO]\n' > "$CONOCIMIENTO"
    echo "   $CONOCIMIENTO creado de ejemplo: escríbelo tú (docs/webchat.md)"
  fi
  lleno turnstile_secret || echo "   ⚠ secrets/turnstile_secret está vacío: el chat contestará «no disponible» hasta que lo pongas"
fi
if tiene webchat || tiene panel || tiene cerebro; then
  lleno workers_ai_token || echo "   ⚠ secrets/workers_ai_token está vacío: sin Workers AI, contesta Ollama (docs/webchat.md)"
fi

# --- 2. Imágenes y núcleo ------------------------------------------------------------------------------------------
echo "== 2/5 Construyo las imágenes (la primera vez tarda) y arranco el núcleo"
docker compose build </dev/null
docker compose up -d core </dev/null
for _ in $(seq 1 30); do core --dispositivos </dev/null >/dev/null 2>&1 && break; sleep 2; done

# --- 3. Llaves ------------------------------------------------------------------------------------------------------
echo "== 3/5 Llaves internas (sin enseñarlas)"
if tiene apuntes; then
  nueva_llave apuntes_core correr apuntes-web --apuntes-dispositivo-nuevo core
fi
if tiene panel; then
  nueva_llave panel_avisos core --dispositivo-nuevo panel
  nueva_llave panel_core correr panel-web --panel-dispositivo-nuevo core
  if ! lleno panel_google_key; then
    python3 -c 'import secrets; print(secrets.token_hex(32))' | guardar panel_google_key
    echo "   secrets/panel_google_key creado (cifra el permiso de Google del panel; no lo copies junto al volumen)"
  fi
fi
if tiene webchat; then
  nueva_llave webchat_avisos core --dispositivo-nuevo webchat
fi
if tiene webchat && tiene clientes; then
  nueva_llave clientes_webchat correr clientes --clientes-llave-webchat
fi
if tiene cerebro; then
  nueva_llave cerebro_avisos core --dispositivo-nuevo cerebro
  nueva_llave cerebro_core correr cerebro --cerebro-dispositivo-nuevo core
  tiene panel && nueva_llave cerebro_panel correr cerebro --cerebro-dispositivo-nuevo panel
  if tiene apuntes; then
    nueva_llave cerebro_apuntes_web correr cerebro --cerebro-dispositivo-nuevo apuntes
    nueva_llave cerebro_apuntes correr apuntes-web --apuntes-dispositivo-nuevo cerebro
  fi
fi

# --- 4. Conexiones en .env ------------------------------------------------------------------------------------------
echo "== 4/5 Conexiones por la red interna de Docker (.env)"
tiene apuntes && env_default ARTHUR_APUNTES_URL http://apuntes-web:8774
tiene panel && env_default ARTHUR_PANEL_URL http://panel-web:8775
tiene cerebro && env_default ARTHUR_CEREBRO_URL http://cerebro:8778
tiene webchat && tiene clientes && env_default ARTHUR_WEBCHAT_CLIENTES_URL http://clientes:8777
for v in APUNTES PANEL CLIENTES; do
  p=$(echo "$v" | tr 'A-Z' 'a-z')
  if tiene "$p" && [[ -z "$(env_get "ARTHUR_${v}_PUBLIC_URL")" ]]; then
    echo "   ⚠ Falta ARTHUR_${v}_PUBLIC_URL en .env (la dirección que abres tú, p. ej. http://<IP-PRIVADA-DEL-SERVIDOR>:PUERTO):"
    echo "     sin ella, los avisos al móvil no abren esa página"
  fi
done

# --- 5. Levantar y comprobar ----------------------------------------------------------------------------------------
echo "== 5/5 Levanto todo y compruebo"
docker compose up -d </dev/null
sleep 10
docker compose ps --format '{{.Service}}\t{{.Status}}'
declare -A NOMBRE=([apuntes]=apuntes-web [panel]=panel-web [clientes]=clientes [webchat]=webchat [cerebro]=cerebro)
for s in core "${PERFILES[@]}"; do
  c=${NOMBRE[$s]:-$s}
  printf '   %s: ' "$c"
  docker compose exec -T "$c" python -c 'import arthur; print(arthur.__version__)' </dev/null 2>/dev/null \
    || echo "no contesta (mira: docker compose logs --tail 50 $c)"
done
if tiene cerebro || tiene webchat; then
  printf '   puertos de cerebro y webchat (no debe salir ninguna IP delante): '
  docker compose ps cerebro webchat --format '{{.Service}} {{.Ports}}' 2>/dev/null | tr '\n' ' '; echo
fi

echo
echo "LISTO. Lo que queda a mano está en la guía de cada servicio (docs/apuntes.md, panel.md, clientes.md,"
echo "webchat.md y cerebro.md): llaves de tus aparatos (móvil…), Google en el panel, Syncthing para las bóvedas."
echo "Para apagar un servicio opcional (solo si quieres): quita su perfil de COMPOSE_PROFILES en .env y"
echo "para su contenedor con  docker compose stop NOMBRE  (los volúmenes y secrets/ se quedan)."
