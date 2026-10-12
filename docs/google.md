# Conectar ARTHUR con Google: Calendar y Gmail

ARTHUR pide tres permisos:

| Permiso | Para qué | Qué NO puede hacer |
|---|---|---|
| `calendar.readonly` | Ver tus calendarios y eventos | — |
| `calendar.events` | **Crear, cambiar y borrar eventos** («apúntame dentista el jueves a las 5», «pasa el dentista al viernes», «borra el dentista») | Tocar otras cosas de Google (calendarios enteros, ajustes). Siempre con tu confirmación |
| `gmail.readonly` | Ver quién te escribe y leer un correo si lo pides | Enviar, borrar o modificar correos |

Un evento solo se crea o se cambia después de que TÚ confirmes (terminal u Orb) y nunca se invita ni se avisa a nadie.
**Nota honesta sobre `calendar.events`:** Google no ofrece un permiso de «solo crear» ni de «solo crear y
cambiar»; es uno solo para crear, cambiar y borrar eventos, y ARTHUR usa las tres cosas, siempre
con tu confirmación. Si prefieres no darlo, quítalo al conectar: ARTHUR
seguirá leyendo tu calendario y te dirá que le falta permiso solo cuando le pidas escribir algo.

El permiso se guarda cifrado en tu llavero (gnome-keyring); en el servidor, en un fichero 600 de su volumen,
y puedes retirarlo cuando quieras en <https://myaccount.google.com/permissions>.

## 1. Crear la "aplicación" de ARTHUR en Google Cloud (gratis, una sola vez)

1. Entra en <https://console.cloud.google.com/> con tu cuenta de Google y crea un proyecto llamado `ARTHUR`.
2. **APIs y servicios → Biblioteca** → habilita **Google Calendar API** y **Gmail API**.
3. **Google Auth Platform** (antes "Pantalla de consentimiento de OAuth"):
   - *Branding*: nombre `ARTHUR` y tu correo.
   - *Audiencia*: **Externo**. En *Usuarios de prueba* añade **tu propio Gmail**.
4. **Clientes → Crear cliente** → tipo **Aplicación de escritorio** → nombre `ARTHUR` → **Descargar JSON**.

## 2. Darle el fichero a ARTHUR

```fish
mkdir -p ~/.config/arthur
mv ~/Descargas/client_secret_*.json ~/.config/arthur/google-client.json
chmod 600 ~/.config/arthur/google-client.json
```

## 3. Autorizar

```fish
arthur --conectar-google
```

Se abre Google en el navegador: elige tu cuenta. Como la "aplicación" es tuya y no está verificada por Google,
verás **«Google no ha verificado esta aplicación»** → *Configuración avanzada* → *Ir a ARTHUR*. Acepta los
permisos de **ver tu calendario**, **crear eventos** y **leer tus correos**. La pestaña dirá «ARTHUR ya tiene permiso».

## Aviso: caducidad a los 7 días

Mientras la aplicación esté en **modo de prueba**, Google hace caducar el permiso cada 7 días (ARTHUR te dirá
que vuelvas a ejecutar `arthur --conectar-google`). Para evitarlo: *Google Auth Platform → Audiencia →
Publicar aplicación*. Sigue siendo solo tuya; Google solo seguirá mostrando el aviso de "no verificada".

## Desconectar

```fish
arthur --desconectar-google     # borra el permiso del llavero
```
y retíralo también en <https://myaccount.google.com/permissions>.

## Si añades permisos nuevos (p. ej. Gmail después del calendario)

Habilita la API nueva en Google Cloud y vuelve a ejecutar `arthur --conectar-google`: el permiso antiguo se
sustituye por uno que incluye ambos.

## Crear eventos: cómo funciona

- «Apúntame dentista el jueves a las 5», «pon cena con Laura el sábado de 21:00 a 23:00»,
  «añade examen de programación el 20 de octubre, todo el día».
- Antes de crear, ARTHUR te enseña el resumen exacto («Crear en tu calendario: Dentista, jueves 8/10 de 17:00
  a 18:00»). **Mira el día de la semana**: si el modelo calcula mal la fecha, aquí se nota. Responde NO y repítelo.
- Zona horaria: siempre Europe/Madrid. Si no dices la hora de fin, dura 1 hora.
- La herramienta rechaza fechas que no existen, eventos en el pasado, a más de 2 años vista y eventos que
  pasen de medianoche.
- Se crea en tu calendario principal, sin invitados y con `sendUpdates=none` (Google no manda correos a nadie).

## Cambiar y borrar eventos

- «Pasa el dentista al viernes», «cámbiale la hora a las 6», «ponle de lugar la clínica».
- ARTHUR identifica el evento por su **número** (`#1`, `#2`…), que enseña `calendar_events` solo en los eventos que
  puede tocar (ejemplo: `17:00–18:00 Dentista [Personal] #1`) y que devuelve `create_event` al crear
  («He apuntado… (#3)»). Nunca por un nombre que invente el modelo. Los números valen para **la última lista que
  te enseñó**: si no hay lista, o el número no está, ARTHUR dice «Llama primero a calendar_events…» y vuelve a
  consultar tu agenda (puede que te lo vuelva a mostrar antes de seguir).
- **Solo toca** eventos de tu **calendario principal**, sueltos, sin invitados y normales. Los que se repiten, los
  que tienen invitados (cambiarlos podría avisar a gente), cumpleaños, ausencias, los de otros calendarios y los
  que duran varios días o pasan de medianoche, se hacen en Google Calendar (ARTHUR te lo dirá).
- Antes de preguntarte, comprueba que el evento existe. La confirmación enseña **título, fecha y día de la semana
  de antes y de después**:
  `Cambiar en tu calendario: Dentista, jueves 8/10 de 17:00 a 18:00  →  Dentista, viernes 9/10 de 17:00 a 18:00`.
- Si alguien cambia el evento mientras decides, Google rechaza tu orden (usa la versión del evento que viste) y no se pisa nada.
- Reglas: solo se envía lo que cambias; si cambias la hora de inicio y no la de fin, dura lo mismo; un evento de todo
  el día sigue siéndolo; no se puede mover al pasado ni dejar el evento a más de 2 años; los eventos que ya pasaron
  no se editan. No se puede quitar el lugar (solo cambiarlo). Hora de Madrid, fechas `AAAA-MM-DD`.
- Si Google no contesta a tiempo, ARTHUR **no lo repite solo** (podría aplicarse dos veces): te avisa de que quizá se
  guardó y que mires tu calendario.
- Si el permiso guardado no incluye eventos, te dirá que ejecutes `arthur --conectar-google`.

## Responder correos (sin permisos nuevos)

«Responde a Laura que el viernes me va bien» abre en Firefox la ventana de redactar de Gmail con la respuesta
ya escrita, para quien te escribió y con el asunto «Re: …». **Tú pulsas «Enviar».** Para esto ARTHUR solo usa
el permiso de lectura de Gmail que ya tiene (y solo mira el remitente y el asunto del correo, no su texto): no
hay que volver a conectar nada. Limitación: al abrirse como correo nuevo, Gmail puede no juntarla con la
conversación original.

### Borrar un evento

- «Borra el dentista». Es una acción de **riesgo alto**: ARTHUR pregunta **siempre** (aunque pongas
  `confirm_writes = false`) y la respuesta por defecto es **«No»** (en la terminal, `[s/N]`; en el Orb, el botón «No»
  ya viene seleccionado).
- Mismo control que al cambiar: el evento se identifica por su número (`#N`) y se comprueba que existe ANTES de preguntarte, y
  solo se borran eventos sueltos, de tu calendario principal y sin invitados.
- La confirmación enseña qué se borraría, con título, fecha y día de la semana:
  `BORRAR de tu calendario: Dentista, jueves 8/10 de 17:00 a 18:00. ARTHUR no puede deshacerlo`.
- Se puede borrar un evento que ya pasó (limpiar la agenda). Si el evento cambió mientras decidías, no se borra.
- Sin respuesta a tiempo, ARTHUR no lo repite solo: te dice que mires tu calendario.
- **Registro:** cada intento (también si dices NO) queda en `~/.local/state/arthur/audit.jsonl`, con herramienta, riesgo,
  id, **el resumen que viste** (qué evento era), tu decisión y el resultado.
- ARTHUR no puede deshacer un borrado. Google Calendar suele guardar los eventos borrados un tiempo en su
  *Papelera* (en la web de Calendar); no lo he podido comprobar desde la nube, así que no cuentes con ello.

## En el servidor

Sin navegador en el servidor, el permiso se da pegando la dirección de vuelta: ver
[servidor.md](servidor.md#google-opcional).
