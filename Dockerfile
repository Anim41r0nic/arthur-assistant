# arthur-core: el ARTHUR del servidor (docs/servidor.md).
# Solo la biblioteca estándar de Python: la imagen no instala ningún paquete de pip.
FROM python:3.14.8-slim-trixie

# Usuario sin privilegios: el proceso nunca corre como root dentro del contenedor.
RUN useradd --uid 1000 --create-home --home-dir /data arthur

WORKDIR /app
COPY arthur/ /app/arthur/
COPY pyproject.toml README.md /app/

# Todo lo que ARTHUR guarda (config, memoria, aparatos, registro, permiso de Google) vive en /data (un volumen).
ENV HOME=/data \
    XDG_CONFIG_HOME=/data/config \
    XDG_DATA_HOME=/data/share \
    XDG_STATE_HOME=/data/state \
    ARTHUR_KEYRING=file \
    ARTHUR_CORE_HOST=0.0.0.0 \
    ARTHUR_CORE_PORT=8770 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER arthur
VOLUME ["/data"]
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/api/v1/health', timeout=3).status == 200 else 1)"]
ENTRYPOINT ["python", "-m", "arthur"]
CMD ["--core"]
