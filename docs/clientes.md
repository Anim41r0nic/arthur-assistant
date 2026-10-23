# Clientes

## Qué es

Una página para el móvil (y el portátil) con **todas las personas que te han escrito o a las que has apuntado**:

- cada una tiene su número de cliente (`C-0001`, `C-0002`…);
- si alguien vuelve a escribir con el **mismo correo o el mismo teléfono**, el mensaje va a su ficha: no se duplica;
- en cada ficha: estado (nuevo → hablando → presupuesto → cliente → descartado), notas privadas, todos sus mensajes,
  «Apuntar mensaje» (una llamada o un WhatsApp), «Responder por correo» y «Borrar cliente»;
- también puedes dar de alta a mano a quien te llama o te escribe por WhatsApp («Nuevo cliente»).

Los contactos del chat de la web (`docs/webchat.md`) llegan solos: el chat se los pasa a este servicio por la red
interna de Docker. El chat **solo puede entregar** contactos: no puede ver la lista, ni leer fichas, ni borrar.

«Responder por correo» abre tu programa de correo con la dirección y el asunto puestos: tú escribes y envías. La
página nunca manda nada sola.

## 0. Ponerlo en marcha

El servicio `clientes` está en `compose.yaml` (perfil `clientes`). En el servidor, desde la carpeta de ARTHUR:

```
bash scripts/desplegar.sh clientes          # o «clientes webchat» si también quieres el chat de tu web
```

Pon en `.env` `ARTHUR_CLIENTES_PUBLIC_URL` (la dirección que abre tu móvil, p. ej. `http://<IP-PRIVADA-DEL-SERVIDOR>:8777`): con
ella, tocar el aviso de un cliente nuevo abre su ficha.

## 1. Darle una llave al móvil

En el servidor, dentro del contenedor de clientes:

```
docker compose exec clientes python -m arthur --clientes-dispositivo-nuevo movil
```

Te enseña una llave **una sola vez** (el servidor solo guarda su huella). Abre en el móvil la dirección de clientes
(la dirección privada configurada, puerto 8777), pega la llave y listo: el móvil se acuerda de ella. Para otro aparato, repite con
otro nombre (`portatil`…). El nombre `webchat` está reservado para el chat de la web.

## 2. La llave del chat de la web

El chat necesita su propia llave para entregar contactos. Se crea así, y la orden imprime **solo** la llave en una
línea, para guardarla directamente como secreto de Docker:

```
docker compose exec -T clientes python -m arthur --clientes-llave-webchat > secrets/clientes_webchat
```

(`scripts/desplegar.sh clientes webchat` ya lo hace por ti.) Después reinicia el chat para que la lea. Si la vuelves a crear, la anterior deja de valer al momento (útil si crees
que se ha filtrado): repite la orden y reinicia el chat.

## 3. Duplicados: cómo decide si es la misma persona

- El correo se compara sin espacios y sin mayúsculas: `Ana@Example.com ` es `ana@example.com`.
- El teléfono se compara solo por sus cifras; si no empieza por `+` ni por `00`, se entiende que es de España:
  `+34 600 11 22 33`, `0034600112233`, `600-112-233` y `(600) 112 233` son el mismo. Un teléfono con letras o con
  menos de 9 cifras no se acepta.
- Si coincide el correo **o** el teléfono, el mensaje va a esa ficha. Si a la ficha le faltaba el teléfono (o el
  correo), se completa. El nombre de la ficha no cambia; cada mensaje guarda el nombre con el que escribió.
- Si el correo es de una ficha y el teléfono de **otra**, no se juntan: el mensaje va a la del correo y esa ficha
  enseña el aviso «Puede ser la misma persona que C-00xx». Tú decides (de momento, borrando la que sobre).
- Alguien «descartado» que vuelve a escribir pasa a «hablando».

## 4. Cuánto se guarda

- Cada día (y al arrancar) se borran las fichas cuyo **último contacto** fue hace más de 365 días, **salvo** las que
  están en estado «cliente»: esas se conservan por las obligaciones de facturación y nunca se borran solas.
- «Borrar cliente» en la ficha borra la ficha y todos sus mensajes (derecho de supresión). Pide confirmación.
- Si haces copias del servidor, recuerda que el volumen `clientes-data` lleva datos personales de terceros.

## 5. Dónde vive cada cosa

En el contenedor, todo en `/data` (el volumen `clientes-data`):

- `clientes.sqlite3`: las fichas y los mensajes (permisos 600);
- `devices.json`: las huellas de las llaves de los aparatos;
- `webchat.key`: la huella de la llave del chat.

Variables de entorno: `ARTHUR_CLIENTES_HOST` (en la imagen, `0.0.0.0`), `ARTHUR_CLIENTES_PORT` (8777) y
`ARTHUR_CLIENTES_DATA` (en la imagen, `/data`; fuera de Docker, `~/.local/share/arthur/clientes`).

## 6. Seguridad

- Contenedor `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, usuario 1000, `mem_limit: 128m`; el puerto solo se
  publica en una dirección privada del servidor. El chat le habla por la red interna de Docker.
- Dos clases de llave que no se mezclan: la del chat **solo** sirve para `POST /api/interno/contacto` (cualquier otra
  ruta: 403) y las de los aparatos **solo** para la página (la ruta interna les da 403). Una llave mala espera medio
  segundo antes de contestar.
- Con la cookie de la página, todo lo que cambia algo (editar, apuntar, borrar) exige venir de la propia página.
- El registro solo apunta método, ruta, código y tiempo (sin lo que se busca): **nunca** nombres, correos,
  teléfonos, notas ni mensajes.
- Todo lo que escribe la gente se enseña como texto (nunca como HTML): un nombre con `<script>` se ve tal cual.

## 7. Cómo comprobar que funciona (sin ver datos de nadie)

```
docker compose logs --tail 50 clientes | grep -E "clientes-web: (GET|POST)|purga"
```

Deberías ver líneas como `clientes-web: POST /api/interno/contacto 200 3 ms` cuando llega un contacto del chat, y
`Clientes: purga hecha (…)` una vez al día.

## Para quien programa

- `arthur/clientes/store.py`: SQLite, reglas de duplicados (índices únicos parciales y `BEGIN IMMEDIATE`: dos
  contactos a la vez del mismo correo dan una sola ficha), purga.
- `arthur/clientes/web.py`: servidor HTTP de la biblioteca estándar (como el panel) y `WebchatKey`.
- `arthur/clientes/cli.py`: las órdenes y la purga diaria.
- `arthur/clientes/web/`: la página (sin dependencias; `tokens.css`, `componentes.css`, fuentes e iconos copiados
  del panel). Textos en `I18N` de `clientes.js` (es/ca/en).
- Contrato con el chat: `POST /api/interno/contacto`, `Authorization: Bearer <llave del chat>`, cuerpo JSON
  `{"name","email","phone","need","lang"}` (`phone` puede faltar o ir vacío) → `200 {"id": 12, "nuevo": true}`;
  `400 {"error","field"}` si un dato no vale (`field`: name, email, phone, need, contact); 401 sin llave o con una
  mala; 403 con una llave de aparato.
