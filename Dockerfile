FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    TZ=Europe/Kyiv \
    FLAT_HUNT_DB=/data/flats.db \
    FLAT_HUNT_CONFIG=/app/config.yaml

WORKDIR /app

# Спершу лише маніфести — шар із залежностями кешується між збірками
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY flathunt/ ./flathunt/
COPY config.yaml ./
RUN uv sync --locked --no-dev

RUN mkdir -p /data
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=20s --start-period=3m \
  CMD python -c "import sqlite3,os,sys; \
      c=sqlite3.connect(os.environ['FLAT_HUNT_DB']); \
      sys.exit(0 if c.execute('select count(*) from runs').fetchone()[0] else 1)"

CMD ["flat-hunt", "watch", "--interval", "600"]
