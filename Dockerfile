# ============================================================================
# LunaGPT Core API - Multi-stage Sovereign AI Service Container
# ============================================================================
FROM ghcr.io/astral-sh/uv:0.6.1-python3.12-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies with frozen lockfile for reproducible caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# ----------------------------------------------------------------------------
# Final Runtime Image
# ----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Copy verified virtualenv from builder stage
COPY --from=builder /app/.venv /app/.venv
COPY src /app/src
COPY migrations /app/migrations
COPY alembic.ini pyproject.toml /app/

# Security: run as unprivileged user
RUN groupadd -r lunagpt && useradd -r -g lunagpt -d /app -s /sbin/nologin lunagpt && \
    chown -R lunagpt:lunagpt /app
USER lunagpt

EXPOSE 8010

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8010/health')" || exit 1

CMD ["python3", "-m", "uvicorn", "lunagpt.api:app", "--host", "0.0.0.0", "--port", "8010"]
