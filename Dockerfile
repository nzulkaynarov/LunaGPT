# ============================================================================
# LunaGPT Core API - Multi-stage Sovereign AI Service Container
# ============================================================================
FROM python:3.12-slim-bookworm AS builder

# The official uv binary, pinned so a rebuild never changes the toolchain silently
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 1. Install dependencies first for optimal Docker layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# 2. Copy project metadata and source code, then install project
COPY README.md ./
COPY src /app/src
RUN uv sync --frozen --no-dev

# ----------------------------------------------------------------------------
# Final Runtime Image
# ----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH"

# Copy verified virtualenv and source code from builder stage
COPY --from=builder /app/.venv /app/.venv
COPY README.md pyproject.toml alembic.ini /app/
COPY src /app/src
COPY migrations /app/migrations

# Security: run as unprivileged user
RUN groupadd -r lunagpt && useradd -r -g lunagpt -d /app -s /sbin/nologin lunagpt && \
    chown -R lunagpt:lunagpt /app
USER lunagpt

EXPOSE 8010

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8010/health')" || exit 1

CMD ["sh", "-c", "alembic upgrade head && python3 -m uvicorn lunagpt.api:app --host 0.0.0.0 --port 8010"]
