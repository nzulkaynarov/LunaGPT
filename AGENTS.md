# Global AI Agent Rules (AGENTS.md) — LunaGPT

These rules apply across all AI coding agents (Antigravity / Gemini, Claude Code, Ollama local agents, OpenCode, Codex, Cursor, Aider). Keep solutions concise, modular, and production-grade.

## 1. Communication & Workflow
- **Language**: Respond to the user in **Russian** (`ru`). Write all code identifiers, docstrings, comments, log messages, and Git commit messages in **English** (`en`).
- **Verify Before Claiming Done**: Run linters, typechecks, and relevant tests before finishing a task (`uv run ruff check`, `uv run ruff format --check`, `uv run pytest`).
- **Minimal Diffs**: Keep changes focused on the request. Preserve existing comments and architecture unless refactoring is requested.
- **Secrets & Security**: Never hardcode API keys, tokens, or database credentials. Always use `.env` (with `.env.example`) and `pydantic-settings` / environment variables.
- **Bandwidth & Git Clones**: Always use `git clone --depth 1` (shallow clone) when cloning remote repositories to prevent timeouts and optimize bandwidth on international routes.

## 2. Application AI & LLM Stack
- **Unified Model Routing via LiteLLM**:
  - Route **all** application LLM and embedding calls (Claude, Gemini, and local **Ollama** models) through **LiteLLM** (`litellm` Python SDK or LiteLLM Proxy service on `http://localhost:4000/v1`).
  - Never hardcode or scatter direct vendor SDK calls across business logic. Centralize model configs, fallbacks, and retries in [config/litellm-config.yaml](file:///home/umarbek/projects/LunaGPT/config/litellm-config.yaml).
- **Mandatory Observability via Langfuse**:
  - Every LLM generation, embedding call, agent step, and tool invocation in the application **must** be traced in **Langfuse** (`callbacks=["config.langfuse_v2_logger.langfuse_v2_logger"]` in LiteLLM + direct Ingestion REST API `/api/public/ingestion`).
  - Always attach `session_id`, `user_id`, `tags`, and `metadata` to traces.
- **Document Ingestion & Parsing via Docling**:
  - Use **Docling** / structured chunkers as the default engine for parsing PDFs, DOCX, PPTX, HTML, and complex tables/layouts into structured Markdown or JSON (and `HybridChunker` when chunking for RAG / PostgreSQL `pgvector`).
  - **CPU Performance Optimization**: When running on CPU, configure `PdfPipelineOptions(do_ocr=False)` for digital documents (only enable OCR for scanned images) and set `ocr_options.num_threads=6..8` to avoid blocking event loops.
- **Local Models via Ollama (Installed & Active on this Host)**:
  - An active Ollama service is running on `http://localhost:11434` with models stored on the `/data/ollama/models` SSD.
  - Pre-installed models:
    - `qwen2.5-coder:7b` — fast local code generation, refactoring, docstrings, and offline agent steps ($0 cost).
    - `bge-m3:latest` — state-of-the-art multilingual (RU/EN) 1024-dim dense embeddings for Docling RAG chunking and PostgreSQL `pgvector`.
  - Interaction modes:
    - Route via LiteLLM: `model="luna-coder"` and `model="luna-embed"` with `api_base="http://localhost:4000/v1"`.

## 3. Python Standards
- **Package & Env Manager**: Use `uv` (`uv run`, `uv add`, `uv sync`) by default instead of raw `pip`/`venv`.
- **Linting & Formatting**: Use `ruff` (`ruff check --fix`, `ruff format`) and strict type annotations checked with `mypy` or `pyright`.
- **Framework & Validation**: Prefer `FastAPI`, `Pydantic v2` (`model_validate`, `ConfigDict`), and `pydantic-settings`.
- **Async I/O**: Use `httpx.AsyncClient` for HTTP requests and async database drivers (`psycopg` v3 async / `asyncpg`) for I/O-bound services.

## 4. PostgreSQL Standards
- **ORM & Queries**: Use `SQLAlchemy 2.0` (modern `select()`, `Mapped`, `mapped_column`) with async drivers.
- **Performance & Indexing**:
  - Use `UUID(as_uuid=True)` for primary keys, `TIMESTAMPTZ` for timestamps, and `JSONB` for semi-structured data.
  - For vector search on PostgreSQL, use `pgvector` with HNSW indexes (`vector_cosine_ops`, `m=16, ef_construction=64`).

## 5. Active Learning & Feedback Loop (4 Levels)
- **Level 1 (Realtime Reflection)**: Automatic detection of user dissatisfaction and errors $\to$ escalation to `luna-smart` (Claude Sonnet 4.6) with step-by-step root cause analysis.
- **Level 2 (Langfuse Scoring)**: Record feedback scores (0/1) for all traces.
- **Level 3 (pgvector Mistake Memory)**: Store rejected vs corrected prompt lessons in `feedback_memories` with 1024-dim embeddings (`bge-m3`).
- **Level 4 (Fine-Tuning Dataset)**: Export DPO pairs (Prompt, Rejected, Chosen) for periodic local fine-tuning of Ollama models.
