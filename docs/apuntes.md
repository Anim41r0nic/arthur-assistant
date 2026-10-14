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

## Ponerla en marcha (servidor con Docker)

`Dockerfile.apuntes` trae pandoc, OCRmyPDF, Tesseract (es/ca/en) y Typst (binario oficial con versión y sha256
fijos). Un servicio de ejemplo para `compose.yaml`:

```yaml
  apuntes-web:
    build: { context: ., dockerfile: Dockerfile.apuntes }
    command: ["python", "-m", "arthur", "--apuntes-web"]
    environment:
      ARTHUR_APUNTES_HOST: 0.0.0.0
      ARTHUR_APUNTES_PORT: "8774"
      ARTHUR_OLLAMA_URL: http://ollama:11434
    volumes: ["apuntes-data:/data"]
    ports: ["100.64.0.10:8774:8774"]   # SOLO la IP de Tailscale del servidor: nunca abierto a internet
    restart: unless-stopped
```

Después, una vez:

```
docker compose exec apuntes-web python -m arthur --apuntes-clave-gemini            # pega la clave (no se ve)
docker compose exec apuntes-web python -m arthur --apuntes-dispositivo-nuevo movil  # llave del aparato
```

Abre `http://<IP de Tailscale>:8774/#llave=<la llave>` en el móvil: la llave se cambia por una cookie y desaparece
de la dirección. `--apuntes-importar CARPETA` importa una carpeta entera (subcarpetas = carpetas).

## Seguridad

- Llaves por aparato; con cookie, todo lo que cambia algo exige la cabecera `X-Arthur` y el mismo origen.
- Los ficheros se comprueban por su contenido y se guardan con nombres propios; nunca se sirven rutas que vengan de
  la petición.
- Pensada para la red privada (Tailscale). Si la quieres en internet, pon otra puerta delante (p. ej. Cloudflare
  Access con código por correo).
- Desde el chat de ARTHUR se puede manejar (carpetas, guardar una foto en una carpeta…) si el núcleo tiene
  `ARTHUR_APUNTES_URL` y la llave del aparato «core».
