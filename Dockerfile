FROM python:3.12-slim

LABEL org.opencontainers.image.title="wmts-gpx-heatmap" \
      org.opencontainers.image.version="1.0.4"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# utilisateur non-root (UID/GID surchargeables via PUID/PGID dans docker-compose.yml)
RUN useradd --uid 1000 --create-home app

COPY app /app/app
# configuration par défaut, utilisée si aucun config.yaml n'est monté sur /config
COPY config.yaml /app/config.yaml

USER app

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
