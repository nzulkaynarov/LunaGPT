# ============================================================================
# LunaGPT Core API - High performance sovereign AI service
# ============================================================================
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app/.venv/lib/python3.12/site-packages \
    PATH="/app/.venv/bin:$PATH"

# Copy verified Python 3.12 virtualenv and application source
COPY .venv /app/.venv
COPY src /app/src
COPY pyproject.toml /app/

EXPOSE 8010

HEALTHCHECK --interval=5s --timeout=3s --retries=5 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8010/health')" || exit 1

CMD ["python3", "-m", "uvicorn", "lunagpt.api:app", "--host", "0.0.0.0", "--port", "8010"]
