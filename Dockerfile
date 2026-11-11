FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    APP_DATA_DIR=/data \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.7
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-cache
COPY app ./app
COPY contracts/schema.json contracts/openapi.json ./contracts/
RUN groupadd --gid 10001 backend && useradd --uid 10001 --gid backend --no-create-home backend \
    && mkdir -p /data && chown backend:backend /data
USER backend
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('APP_PORT', '8000') + '/health', timeout=2)"
CMD ["python", "-m", "app"]
