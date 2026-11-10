# ============================================================================
# LunaGPT Core API - Multi-stage Sovereign AI Service Container
# ============================================================================
FROM python:3.12-slim-bookworm AS builder

# Install the latest official uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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

# Luna's local voices, pinned by revision and checksum
ADD --checksum=sha256:8ff38212d23da300bbe3705c645e6e5b9475f0bfde01558eb17813e22acaaaaa https://huggingface.co/rhasspy/piper-voices/resolve/cf910fb59f1d79ab3b625a63993a108751c6c25d/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx /app/voices/ru_RU-irina-medium.onnx
ADD --checksum=sha256:c2ec28bb38e2b59e93b959b3e40348c1afebbd272f30fed5d41205d08e98a9d7 https://huggingface.co/rhasspy/piper-voices/resolve/cf910fb59f1d79ab3b625a63993a108751c6c25d/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json /app/voices/ru_RU-irina-medium.onnx.json
ADD --checksum=sha256:5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f https://huggingface.co/rhasspy/piper-voices/resolve/cf910fb59f1d79ab3b625a63993a108751c6c25d/en/en_US/lessac/medium/en_US-lessac-medium.onnx /app/voices/en_US-lessac-medium.onnx
ADD --checksum=sha256:efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0 https://huggingface.co/rhasspy/piper-voices/resolve/cf910fb59f1d79ab3b625a63993a108751c6c25d/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json /app/voices/en_US-lessac-medium.onnx.json

# Security: run as unprivileged user
RUN groupadd -r lunagpt && useradd -r -g lunagpt -d /app -s /sbin/nologin lunagpt && \
    chown -R lunagpt:lunagpt /app
USER lunagpt

EXPOSE 8010

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8010/health')" || exit 1

CMD ["sh", "-c", "alembic upgrade head && python3 -m uvicorn lunagpt.api:app --host 0.0.0.0 --port 8010"]
