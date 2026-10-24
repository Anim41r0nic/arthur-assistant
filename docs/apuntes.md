# ARTHUR Apuntes

Una página (también instalable como app en el móvil) para pasar a limpio los apuntes de clase:

1. **Subes** fotos (JPEG/PNG) o PDF, ordenados en carpetas por asignatura y tema.
2. **Se pasan a texto** en Markdown con las fórmulas en LaTeX. Hay cuatro motores a elegir:
   - **Automático** (por defecto): el texto que ya trae el PDF si lo tiene y, si son fotos, Gemini (o el local).
   - **Gemini**: la capa gratuita de Google ([clave de AI Studio](https://aistudio.google.com/apikey)). El que mejor
     lee la letra a mano. Ojo: en la capa gratis Google puede usar lo que se le manda para mejorar sus productos.
   - **Local**: `qwen3-vl` en tu Ollama. Nada sale de casa, pero tarda ~1-2 min por página.
   - **Texto del PDF**: sin IA, solo el texto que trae el PDF.
3. **Retocas** el texto viendo la página original al lado.
4. **Descargas** un PDF limpio hecho con [Typst](https://typst.app) (tres estilos) o el escaneo mejorado con texto
   buscable ([OCRmyPDF](https://ocrmypdf.readthedocs.io)).

Además, **«Preguntar a la IA»**: marcas los apuntes o exámenes que quieras y le pides «explícame…», «hazme
ejercicios como estos (con soluciones)» u otra cosa. Responde con Gemini en el mismo formato, y la respuesta se
puede guardar como un apunte más (con su PDF). Solo se manda a Google el texto de los apuntes marcados, no las fotos.

Y, si tienes el [segundo cerebro](cerebro.md):

- **«🧠 Preguntar a mi cerebro»**: busca en tus apuntes, tus notas de Obsidian y tus PDF y contesta con enlaces a
  la fuente. Apuntes hace de intermediario con su llave: el navegador nunca habla con el cerebro.
- **Pestaña Repaso** (`#repaso`): preguntas cortas sacadas de tus apuntes «listos», con repaso espaciado (FSRS-6).
  Sesión diaria de 10 (como mucho 5 nuevas), modo libre por asignatura o tema, racha, «Esta pregunta está mal» (la
  retira para siempre) y ajustes (asignaturas, hora del aviso, retención). Las preguntas las genera el cerebro cada
  noche; la respuesta correcta no llega al navegador hasta que contestas. Detalles en
  [cerebro.md, «El Repaso»](cerebro.md#el-repaso).
- **Fórmulas con KaTeX** en la pestaña Repaso: KaTeX 0.19.0 (MIT) va dentro del repositorio
  (`arthur/notes/web/katex/`, con sus huellas en `ORIGEN.txt`) y lo sirve la propia página en `/katex/`: sin CDN,
  nada se pide a terceros.

## Ponerla en marcha (servidor con Docker)

`Dockerfile.apuntes` trae pandoc, OCRmyPDF, Tesseract (es/ca/en) y Typst (binario oficial con versión y sha256
fijos). El servicio `apuntes-web` ya está en `compose.yaml` (perfil `apuntes`). En el servidor, desde la carpeta de
ARTHUR:

```
bash scripts/desplegar.sh apuntes            # «apuntes cerebro» para tener también el cerebro y el Repaso
```

El script la construye, crea la llave `apuntes_core` (para manejarla desde el chat de ARTHUR) sin enseñarla y
conecta el núcleo (`ARTHUR_APUNTES_URL=http://apuntes-web:8774` en `.env`). La página escucha en `127.0.0.1:8774` y puede publicarse en una dirección privada del servidor. No la expongas
directamente a internet. Pon en `.env`
`ARTHUR_APUNTES_PUBLIC_URL` (p. ej. `http://<IP-PRIVADA-DEL-SERVIDOR>:8774`) para que el aviso del Repaso y las citas del cerebro
abran la página.

Después, una vez:

```
docker compose exec apuntes-web python -m arthur --apuntes-clave-gemini            # pega la clave (no se ve)
docker compose exec apuntes-web python -m arthur --apuntes-dispositivo-nuevo movil  # llave del aparato
```

Abre `http://<IP-PRIVADA-DEL-SERVIDOR>:8774/#llave=<la llave>` en el móvil: la llave se cambia por una cookie y desaparece
de la dirección. `--apuntes-importar CARPETA` importa una carpeta entera (subcarpetas = carpetas).

## Seguridad

- Llaves por aparato; con cookie, todo lo que cambia algo exige la cabecera `X-Arthur` y el mismo origen.
- Los ficheros se comprueban por su contenido y se guardan con nombres propios; nunca se sirven rutas que vengan de
  la petición.
- Pensada para una red privada. Si necesitas acceso desde internet, pon una puerta de acceso segura delante (p. ej. Cloudflare
  Access con código por correo).
- Desde el chat de ARTHUR se puede manejar (carpetas, guardar una foto en una carpeta…) si el núcleo tiene
  `ARTHUR_APUNTES_URL` y la llave del aparato «core».
- La llave del aparato `cerebro` solo sirve para leer el texto de los apuntes (`GET /api/export`): no puede crear,
  mover ni borrar nada, ni entrar en la página. Las rutas `/api/repaso/*` admiten como mucho 8 KiB.
- KaTeX y las fuentes se sirven con nombres fijos sacados al arrancar de los ficheros del repositorio: la petición
  nunca forma una ruta.
