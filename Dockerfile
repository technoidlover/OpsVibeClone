FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPSVIBES_DATA_DIR=/etc/opsvibes \
    OPSVIBES_WEB_DIR=/app/web \
    OPSVIBES_DOCKER=true

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/vibepanel /app/backend/vibepanel
COPY web /app/web

RUN pip install --no-cache-dir /app/backend

EXPOSE 3000
VOLUME ["/etc/opsvibes"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=2)"

CMD ["opsvibes"]
