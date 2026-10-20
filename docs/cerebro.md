# Segundo cerebro

## Qué es

Una forma de **encontrar lo que ya guardaste**. Escribes tus notas en Obsidian, pasas tus apuntes por ARTHUR Apuntes
y dejas PDF con texto en la carpeta `PDF` de tu bóveda. Luego preguntas («¿cómo se calculaba el límite de una
sucesión?», «¿qué decidimos del precio de la muestra?») y el cerebro te contesta **con un enlace** a la nota, el
apunte o el PDF de donde lo saca.

El cerebro solo **lee**. Nunca escribe en Obsidian, no tiene herramientas y no puede hacer nada por su cuenta: solo
devuelve texto con sus fuentes. Las notas las sigues escribiendo, enlazando y viendo en el grafo de Obsidian, como
siempre.

## Cómo le pregunto

Desde tres sitios, y en los tres contesta lo mismo:

- **El chat de ARTHUR**: «pregúntale a mi cerebro cómo se hacía la regla de L'Hôpital». ARTHUR usa la herramienta
  `preguntar_cerebro` (bajo riesgo, sin confirmación: solo lee).
- **El panel de vida**: la caja «Pregunta a tu cerebro».
- **Apuntes**: el botón «🧠 Preguntar a mi cerebro».

Consejos:

- Pregunta en concreto. Puedes escribir sin tildes, con faltas o en catalán («limit d'una successió»): busca por
  palabras **y** por significado.
- Mira siempre el enlace de la respuesta cuando sea algo importante (una fórmula para un examen, una decisión).
- Si no lo encuentra, te dice **«No lo encuentro en tus notas.»** y te enseña los 3 títulos más cercanos. En ese
  caso no ha llamado a ninguna IA, así que no se ha inventado nada. Prueba con el nombre de la asignatura u otra
  palabra del apunte.

Quién contesta, de mejor a peor:

1. **Workers AI** (el mismo modelo que el chat de la web), con los 8 trozos que mejor encajan.
2. Si Workers AI falla o el presupuesto del día se ha acabado: **Ollama** en el servidor, con 4 trozos. Más lento.
3. Si tampoco: te devuelve los trozos encontrados **sin redactar** («Esto es lo que tengo:»), con sus enlaces.

Solo salen de casa los trozos necesarios para cada pregunta, nunca las bóvedas enteras.

## De dónde lee

| Fuente | Dónde está | Cómo llega al servidor |
|---|---|---|
| Notas personales | bóveda `~/Cerebro` en Obsidian | Syncthing → `ARTHUR_BOVEDA` (por defecto `./obsidian/cerebro`) |
| Una segunda bóveda (opcional, p. ej. la de tu negocio) | la que quieras | Syncthing → `ARTHUR_BOVEDA_NEGOCIO` (`./obsidian/negocio`) |
| Apuntes | ARTHUR Apuntes (solo los que están «listos») | el cerebro se los pide a Apuntes con su llave |
| PDF sueltos | `~/Cerebro/PDF/` (puedes hacer subcarpetas) | viajan con la bóveda por Syncthing |
| Lo hecho con tus ayudantes de IA | `~/Cerebro/Diario/IA/` (la nota diaria, ver abajo) | viaja con la bóveda |

Las rutas `./obsidian/…` son relativas a la carpeta de ARTHUR en el servidor (las variables van en `.env`).

Las dos bóvedas se montan **solo lectura** en el contenedor del cerebro. La segunda sigue aparte: el cerebro lee las
dos, pero no las mezcla (cada cita dice de cuál viene; dentro del contenedor se llama `business-lab`).

Cada 15 minutos (y al arrancar) el cerebro mira qué ha cambiado y solo vuelve a leer eso. Una nota nueva tarda
como mucho ese rato más lo que tarde Syncthing. Si borras, renombras o mueves una nota, en la siguiente pasada
desaparece de donde estaba y aparece donde está ahora, sin duplicados.

## Qué se salta (para que no lea algo)

- Las carpetas `.obsidian/` (la configuración), `.trash/` (la papelera) y `.stversions/` (versiones viejas de
  Syncthing).
- **Cualquier carpeta que se llame `private`**, esté donde esté.
- Una nota suelta, si pones esto al principio del fichero:

  ```text
  ---
  cerebro: no
  ---
  ```

  Si ya estaba en el índice, desaparece en la siguiente pasada.
- Los restos de Syncthing: ficheros `*.sync-conflict-*.md` y temporales `~syncthing~*.tmp`.
- Los trozos que parecen una contraseña o una clave (un token `ghp_…`, una clave de API): no se guardan. Aun así,
  **no escribas contraseñas en tus notas**.
- Los PDF escaneados (sin texto): se apuntan como «sin texto» y se saltan. Para leerlos, súbelos a Apuntes.

## El presupuesto (Neurons)

Workers AI gratis da **10 000 Neurons al día para toda la cuenta de Cloudflare**, y los comparte con el chat de la
web y el resumen del panel. Para que el cerebro no se los coma, lleva su propia cuenta con un tope de
**3 000 al día** y este orden:

1. Tus preguntas (pueden usar todo el tope).
2. Los embeddings, lo que permite buscar por significado (hasta el 90 %). Si no caben, esperan al día siguiente;
   mientras, ese trozo se encuentra igual por palabras.
3. Generar preguntas del Repaso, en la fase siguiente (hasta el 80 %).

El día de Cloudflare cambia a las 00:00 UTC (la 01:00 o las 02:00 en Madrid). La primera vez, con unos cientos de
apuntes y las dos bóvedas, el índice por significado puede tardar unos días en completarse; la búsqueda por palabras funciona
desde el primer momento.

Para cambiar el tope: edita `ajustes.toml` en el volumen del cerebro (abajo, «Órdenes útiles») y reinicia.

## Antes de desplegar: la bóveda `~/Cerebro`

### 1. Crear la bóveda en Obsidian

1. Abre Obsidian → icono de bóvedas (abajo a la izquierda) → **Crear nueva bóveda**.
2. Nombre: **`Cerebro`** (exactamente así, con mayúscula: los enlaces de las citas abren
   `obsidian://open?vault=Cerebro&file=…`). Ubicación: tu carpeta personal, para que quede en `~/Cerebro`.
3. Dentro, crea las carpetas `Diario`, `Diario/IA`, `PDF` y `Plantillas`.
4. Copia la plantilla de la nota diaria:

   ```
   cp config/cerebro/plantilla-nota-diaria.md ~/Cerebro/Plantillas/      # desde la carpeta de ARTHUR
   ```

5. En Obsidian: **Ajustes → Plugins principales → Notas diarias**: actívalo. En sus opciones pon
   «Formato de fecha» `YYYY-MM-DD`, «Carpeta de las notas nuevas» `Diario` y «Plantilla»
   `Plantillas/plantilla-nota-diaria`.

Así cada nota diaria tiene `## Clases`, `## Notas`, `## Ideas` y un enlace a la nota «Hecho con IA» de ese día.

### 2. Compartir la carpeta con Syncthing

En el **portátil** (la interfaz de Syncthing, http://127.0.0.1:8384):

1. **Añadir carpeta**. Etiqueta `Cerebro`, ID `cerebro`, ruta `~/Cerebro`.
2. Pestaña **Compartir**: marca el servidor.
3. Pestaña **Patrones a ignorar**: escribe estas líneas:

   ```text
   .git
   .obsidian/workspace*
   private
   ```

4. Guardar.

En el **servidor**, `scripts/desplegar.sh` crea la carpeta de `ARTHUR_BOVEDA` (por defecto `obsidian/cerebro` dentro
de la carpeta de ARTHUR). Si el servidor no tiene pantalla, abre su interfaz de Syncthing por SSH desde el portátil:

```
ssh -L 8385:127.0.0.1:8384 TU-SERVIDOR
```

Deja esa terminal abierta y abre http://127.0.0.1:8385 en el navegador del portátil. Saldrá el aviso «el portátil
quiere compartir la carpeta Cerebro»: **Añadir**, y como ruta pon la carpeta de `ARTHUR_BOVEDA` (ruta completa).
Guardar. Cierra la terminal del SSH cuando acabes.

Si el servidor lo acepta en otra ruta, el cerebro no verá la bóveda: tiene que ser exactamente esa.

## Desplegar

En el servidor, desde la carpeta de ARTHUR (con ARTHUR ya instalado con `scripts/instalar-servidor.sh`):

```
bash scripts/desplegar.sh apuntes panel cerebro
```

El cerebro funciona solo, pero con Apuntes indexa tus apuntes (y hay Repaso) y con el panel tienes la caja de
preguntas y la nota diaria. Qué hace el script, por orden (si algo falla, se para ahí; se puede relanzar):

1. Apunta los perfiles en `COMPOSE_PROFILES` (`.env`) y crea las carpetas de las bóvedas.
2. Construye las imágenes y arranca el núcleo.
3. Crea las llaves internas que falten directamente en `secrets/` (permisos 600). **No salen en pantalla**.
4. Conecta el chat, el panel y Apuntes con el cerebro (`ARTHUR_CEREBRO_URL=http://cerebro:8778` en `.env`).
5. Levanta todo y comprueba la versión de cada contenedor.

El servicio `cerebro` está en `compose.yaml` (perfil `cerebro`). **No tiene `ports:`**: solo se le puede hablar desde
dentro de la red de Docker (el chat, el panel y Apuntes), ni por Tailscale ni por Internet. Es de solo lectura,
sin capacidades (`cap_drop: [ALL]`), con 384 MB como mucho, y las bóvedas van montadas `:ro`.

Antes, pon en `.env` `ARTHUR_CF_ACCOUNT` (tu id de cuenta de Cloudflare) y el token de Workers AI en
`secrets/workers_ai_token` (docs/webchat.md, «Workers AI: cuenta y token»), y `ARTHUR_APUNTES_PUBLIC_URL` para que las
citas abran Apuntes. Sin Workers AI, contesta Ollama y solo busca por palabras.

### Las llaves

Cada servicio habla con los demás con su propia llave de aparato. `scripts/desplegar.sh` las crea todas menos la
del portátil:

| Secreto | Qué es | Se crea con |
|---|---|---|
| `cerebro_core` | el chat pregunta al cerebro | `--cerebro-dispositivo-nuevo core` |
| `cerebro_panel` | el panel pregunta al cerebro (y le pasa la nota diaria) | `--cerebro-dispositivo-nuevo panel` |
| `cerebro_apuntes_web` | Apuntes pregunta al cerebro | `--cerebro-dispositivo-nuevo apuntes` |
| `cerebro_apuntes` | el cerebro lee el texto de los apuntes | `--apuntes-dispositivo-nuevo cerebro` (en Apuntes) |
| `cerebro_avisos` | el cerebro manda el aviso diario del Repaso | `--dispositivo-nuevo cerebro` (en el núcleo) |
| `workers_ai_token` | Workers AI (el mismo que usa el chat de la web) | lo pones tú |
| `~/.config/arthur/cerebro-portatil.key` (portátil) | el portátil manda la nota diaria al panel | `--panel-dispositivo-nuevo portatil` (en el panel, a mano) |

La llave del aparato `cerebro` en Apuntes **solo sirve para sacar el texto de los apuntes**: no puede entrar en la
página ni preguntar.

## La nota «Hecho con IA»

Cada noche a las **23:30**, el portátil junta lo que hiciste ese día con tus ayudantes de IA: las líneas con esa fecha
de un registro de trabajo opcional, los ficheros de memoria de IA que cambiaron ese día y los `git log` de los repos
que configures. No recoge conversaciones enteras y, antes de salir, quita lo que parezca un secreto. Lo manda al panel
por Tailscale; el panel se lo pasa al cerebro y este lo resume en **Hecho**, **Decidido** y **Pendiente**.

Las rutas se cambian con variables de entorno en el servicio de systemd: `ARTHUR_CEREBRO_PROYECTOS`,
`ARTHUR_CEREBRO_EQUIPO`, `ARTHUR_CEREBRO_MEMORIA` y `ARTHUR_CEREBRO_BOVEDA` (por defecto `~/Cerebro`).

La nota se guarda en la bóveda configurada, en `Diario/IA/AAAA-MM-DD.md`, y empieza con un enlace a la nota diaria.

- Si ese día no hubo nada, no se escribe nota.
- Si ya existe, no la pisa: escribe otra con el sufijo ` (2)`.
- Si el portátil estaba apagado, al encenderlo escribe los días que falten (como mucho los 7 últimos).
- Si no hay IA disponible, escribe las listas sin resumir.

Para activarlo en el portátil (una vez): crea la llave en el panel y guárdala en `~/.config/arthur/cerebro-portatil.key`
con permisos 600 (`docker compose exec panel-web python -m arthur --panel-dispositivo-nuevo portatil` en el servidor),
crea `~/.config/arthur/cerebro-diario.env` con la línea `ARTHUR_PANEL_URL=` seguida de la dirección de tu panel
por Tailscale o HTTPS. Dale permisos 600. Desde la carpeta de ARTHUR, instala la unidad:

```
mkdir -p ~/.config/systemd/user
sed "s|%h/Proyectos/arthur|$PWD|g" scripts/systemd/arthur-cerebro-diario.service > ~/.config/systemd/user/arthur-cerebro-diario.service
cp scripts/systemd/arthur-cerebro-diario.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now arthur-cerebro-diario.timer
```

Para probarlo ahora mismo: `systemctl --user start arthur-cerebro-diario` y luego
`journalctl --user -u arthur-cerebro-diario -n 20` (solo dice los nombres de los ficheros, nunca el contenido).

## Órdenes útiles

En el servidor, dentro de la carpeta de ARTHUR:

| Para | Orden |
|---|---|
| ver si está en marcha | `docker compose ps cerebro` |
| ver qué hace | `docker compose logs --tail 50 cerebro` |
| ver cuánta memoria usa | `docker stats --no-stream $(docker compose ps -q cerebro)` |
| reiniciarlo | `docker compose restart cerebro` |
| ver el estado (índice, gasto, errores) | la orden de abajo |
| cambiar el presupuesto | sacar el fichero, editarlo y devolverlo (las tres órdenes de abajo) y reiniciar |
| una llave nueva para otro aparato | `docker compose run --rm --no-deps cerebro python -m arthur --cerebro-dispositivo-nuevo NOMBRE` |

Los aparatos que el cerebro acepta son solo `core`, `panel`, `apuntes` y `portatil`.

Cambiar el presupuesto (el contenedor es de solo lectura y no trae editor, así que se edita fuera):

```
docker compose cp cerebro:/data/share/arthur/cerebro/ajustes.toml ./ajustes-cerebro.toml
nano ajustes-cerebro.toml
docker compose cp ./ajustes-cerebro.toml cerebro:/data/share/arthur/cerebro/ajustes.toml && docker compose restart cerebro
```

El estado (lo pregunta el chat con su llave, porque el cerebro no tiene puerto fuera de Docker):

```
docker compose exec -T core python -c 'import json,urllib.request; k=open("/run/secrets/cerebro_core").read().strip(); r=urllib.request.Request("http://cerebro:8778/api/estado", headers={"Authorization": "Bearer "+k}); print(json.dumps(json.load(urllib.request.urlopen(r, timeout=10)), indent=2, ensure_ascii=False))'
```

Qué mirar ahí: `trozos` (cuántos trozos hay en el índice), `pendientes` (trozos que aún no se pueden buscar por
significado; bajan cada día), `gastado_hoy` de `presupuesto`, y en `fuentes` el `error` de cada una (si Apuntes
estaba apagado o falta la carpeta de Syncthing, lo dice ahí). `pdf_sin_texto` lista los PDF escaneados que se ha
saltado.

## Qué probar después de desplegar

La prueba real son **tres preguntas** (en el chat, el panel o Apuntes, como prefieras):

1. **Una de tus apuntes.** Algo que sepas que está en un apunte «listo», por ejemplo «¿cómo se calcula el límite
   de una sucesión?». Tiene que contestar y citar el apunte; al tocar la cita se abre en Apuntes.
2. **Una de Obsidian.** Escribe antes una nota corta en `~/Cerebro` (por ejemplo «Decidido: la reunión con el
   cliente es el lunes»), espera unos 15-20 minutos y pregunta «¿cuándo es la reunión con el cliente?». Tiene que citar esa
   nota; al tocar la cita se abre en Obsidian.
3. **Una que no está.** Por ejemplo «¿cuál es la capital de Mongolia?». Tiene que decir «No lo encuentro en tus
   notas.» con tres títulos cercanos, sin inventarse la respuesta.

Si la 1 no encuentra nada, mira en el estado si `fuentes` → `apuntes` tiene un `error`. Si la 2 no, mira si la
nota ha llegado al servidor (`ls obsidian/cerebro` en la carpeta de ARTHUR) y si `fuentes` → `obsidian:cerebro` tiene un
`error`. Al principio muchos trozos estarán `pendientes`: entonces solo encuentra por palabras, así que usa en la
pregunta alguna palabra que esté en la nota.

## Si algo va mal

- **El cerebro no arranca**: `docker compose logs --tail 50 cerebro`. Un `ajustes.toml` mal escrito lo dice ahí,
  con la línea.
- **«El cerebro no está disponible ahora»** en el chat, el panel o Apuntes: el cerebro está apagado o ese servicio
  no tiene su llave. Mira `docker compose ps` y los logs del servicio que lo dice.
- **Va lento**: si Workers AI no contesta, contesta Ollama, que en el servidor tarda bastante más.

Apagar el cerebro (**solo si quieres**, no es un paso): quita `cerebro` de `COMPOSE_PROFILES` en `.env`, vacía
`ARTHUR_CEREBRO_URL` y escribe `docker compose up -d` y `docker compose stop cerebro`. El volumen `cerebro-data` y
los `secrets/cerebro_*` se quedan.

## El Repaso

Preguntas cortas sacadas de tus apuntes, como en Duolingo, que vuelven justo cuando estás a punto de olvidarlas.
Está en Apuntes, pestaña **Repaso** (el aviso del móvil la abre directamente).

### Cómo funciona, en palabras sencillas

- **Cada noche a las 02:00** el cerebro lee tus apuntes «listos» de las asignaturas elegidas y prepara preguntas
  nuevas (40 como mucho por noche, y solo si queda presupuesto de Neurons después de tus preguntas). Antes de
  guardarlas las comprueba: si una no se puede contestar con el apunte, se tira.
- **La sesión del día** son 10 preguntas: primero las que toca repasar y, si hay hueco, hasta 5 nuevas.
- **Repaso espaciado:** si aciertas una pregunta, vuelve dentro de más días cada vez. Si fallas, vuelve al final
  de la misma sesión, como en Duolingo (como mucho tres intentos ese día, para no atascarte), y queda programada
  para otro repaso. Lo calcula el algoritmo FSRS, que estima cuándo la ibas a olvidar.
- **La racha** son los días seguidos con la sesión terminada. Un día sin sesión hecha la rompe, aunque ese día no
  hubiera preguntas. Hasta que acabe el día no se rompe: si hoy aún no la has hecho, cuenta hasta ayer.
- **Modo libre:** repasa una asignatura o un tema cuando quieras. Cuenta para el repaso espaciado, pero no
  completa la sesión del día.
- **El aviso:** a las 20:00, si no has hecho la sesión, te llega un aviso al móvil. Si ya la hiciste, nada.
- En el panel sale una línea «Repaso: racha N · X % esta semana». Si el cerebro no contesta, no sale nada.

### Ajustes

En la pestaña Repaso, botón de ajustes:

| Ajuste | Qué hace | Por defecto |
|---|---|---|
| Asignaturas | de cuáles se sacan preguntas (las de este curso) | elígelas el primer día |
| Hora del aviso | cuándo te avisa si no has hecho la sesión | 20:00 |
| Retención | cuánto quieres acordarte: más alto, más repasos | 0,9 (entre 0,7 y 0,97) |

Se guardan en el volumen del cerebro (`cerebro-data`), no en el navegador.

### Si una pregunta está mal

Pulsa **«Esta pregunta está mal»** y elige qué falla o escribe un motivo; uno de los dos es obligatorio. Los
detalles adicionales son opcionales. Esa pregunta se retira y no vuelve a salir. Si la señalas antes de responder,
no cuenta como fallo; si ya habías respondido, esa respuesta queda registrada. Si un apunte da muchas preguntas
malas, suele ser porque la transcripción de Apuntes tiene errores: corrige el texto en Apuntes y las preguntas
nuevas saldrán bien.

### Desplegar

Viene con el cerebro y Apuntes: `bash scripts/desplegar.sh apuntes cerebro` (y `panel` si quieres la línea del
panel). El script da de alta el aparato `cerebro` en el núcleo (secreto `cerebro_avisos`, sin enseñar la llave) para
el aviso. Para que el aviso y la línea del panel abran la pestaña Repaso, pon `ARTHUR_APUNTES_PUBLIC_URL` en `.env`.

**La prueba real:** elige las asignaturas en Ajustes; al día siguiente, haz una sesión completa (o usa el modo
libre si aún no hay preguntas del día) y comprueba que ese día no llega el aviso de las 20:00; otro día no la
hagas y comprueba que sí llega y que al tocarlo se abre la pestaña Repaso.

## Variables del contenedor

| Variable | Qué es | Por defecto |
|---|---|---|
| `ARTHUR_CEREBRO_HOST` / `ARTHUR_CEREBRO_PORT` | dónde escucha (la imagen pone `0.0.0.0`, sin publicar) | `127.0.0.1` / `8778` |
| `ARTHUR_APUNTES_URL` | Apuntes en la red de Docker (sin ella, no se indexan los apuntes) | vacío |
| `ARTHUR_CEREBRO_APUNTES_KEY_FILE` | llave del aparato `cerebro` en Apuntes | `/run/secrets/cerebro_apuntes` |
| `ARTHUR_APUNTES_PUBLIC_URL` | la dirección de Apuntes que abres tú (para los enlaces de las citas) | vacío |
| `ARTHUR_WEBCHAT_CF_ACCOUNT` | id de cuenta de Cloudflare (sin él, contesta Ollama y solo busca por palabras) | vacío |
| `ARTHUR_CEREBRO_CF_TOKEN_FILE` | token de Workers AI | `/run/secrets/workers_ai_token` |
| `ARTHUR_CEREBRO_AVISOS_URL` / `ARTHUR_CEREBRO_AVISOS_TOKEN_FILE` | el núcleo para el aviso del Repaso y su llave | vacío / `/run/secrets/cerebro_avisos` |
| `ARTHUR_OLLAMA_URL` / `ARTHUR_OLLAMA_KEEP_ALIVE` | Ollama, el motor de respaldo | `http://127.0.0.1:11434` / `10m` |

En el chat, el panel y Apuntes: `ARTHUR_CEREBRO_URL` (`http://cerebro:8778`) y su llave en
`/run/secrets/cerebro_core`, `cerebro_panel` o `cerebro_apuntes_web`.

Ajustes en `/data/share/arthur/cerebro/ajustes.toml` (se crea solo la primera vez):

```toml
presupuesto = 3000
zona = "Europe/Madrid"
boveda_cerebro = "/vaults/cerebro"
boveda_negocio = "/vaults/business-lab"
pdf = "/vaults/cerebro/PDF"
```
