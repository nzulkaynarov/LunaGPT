---
name: lunagpt-agent
description: Comprehensive operational skill for maintaining, extending, and operating the LunaGPT AI gateway, RAG pipeline, and Langfuse tracing.
---

# LunaGPT Operational Skill

This skill guides AI agents working on the LunaGPT repository.

## Commands & Workflows

### 1. Environment & Dependencies
```bash
# Sync python environment using uv
uv sync

# Run tests
uv run pytest tests/

# Check and fix formatting
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
```

### 2. Infrastructure Management (Docker)
```bash
# Start Core Infrastructure
make up
# or: docker compose up -d langfuse-db redis langfuse-server litellm

# Check Service Status
make status
# or: docker compose ps

# View Logs
make logs
```

### 3. Model Routing & Ports
- **LiteLLM Gateway**: `http://localhost:4000/v1` (API Key: `LITELLM_MASTER_KEY` from `.env`)
  - `luna-coder` $\to$ Ollama AMD RX 580 (`qwen2.5-coder:7b`) [$0.00]
  - `luna-embed` $\to$ Ollama AMD RX 580 (`bge-m3:latest`) [1024-dim]
  - `luna-fast` $\to$ Google Gemini (`gemini-3.5-flash-lite`)
  - `luna-smart` $\to$ Anthropic Claude (`claude-sonnet-4-6`)
  - `luna-pro` $\to$ Anthropic Claude (`claude-opus-4-6`)
  - `luna-auto` $\to$ Intelligent failover router
- **Langfuse UI**: `http://localhost:3001` or via Tailscale `http://100.65.202.127:3001` (`admin@lunagpt.local`, password: `LANGFUSE_ADMIN_PASSWORD` from `.env`)
  - Configured with `AUTH_TRUST_HOST=true` and `NEXTAUTH_URL` for multi-host (LAN/Tailscale/localhost) access.
  - Model pricing is maintained in both `config/litellm-config.yaml` (`input_cost_per_token` / `output_cost_per_token`) and Langfuse's `models` table with regex match patterns.
  - Retain `ghcr.io/langfuse/langfuse:2` (v2.95.x). Do NOT upgrade to v3 without adding ClickHouse, MinIO/S3, and `langfuse-worker` services.
- **LunaGPT Core API**: `http://localhost:8010` (Swagger: `/docs`)
- **PostgreSQL 17 + pgvector**: `localhost:5433` (DB: `lunagpt` & `langfuse`)
- **Redis 7.4**: `localhost:6380`

### 4. Memory & Feedback Ingestion
When users report errors or corrections:
```bash
curl -X POST http://localhost:8010/api/v1/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "trace_id": "<uuid>",
    "session_id": "<session>",
    "user_prompt": "<original_prompt>",
    "rejected_response": "<bad_code>",
    "corrected_response": "<good_code>",
    "score": 0.0,
    "model_used": "luna-coder"
  }'
```
This automatically updates `feedback_memories` in `pgvector` with HNSW cosine distance indexing.
