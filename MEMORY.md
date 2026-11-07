# Project Memory: LunaGPT

## Обзор и архитектурный контекст
LunaGPT — персональный суверенный ИИ-шлюз и среда выполнения на базе Linux хоста с локальным GPU AMD Radeon RX 580 (8 ГБ VRAM).
Основные компоненты:
1. **LiteLLM Proxy Gateway (:4000/v1) & Core Router (:8010/v1)**: Единая точка доступа по протоколу OpenAI.
   Витрина из 3 основных моделей:
   - `luna-smart` (Anthropic Claude Sonnet 4.6 — основная модель по умолчанию)
   - `luna-fast` (Google Gemini 3.5 Flash Lite — сверхбыстрые ответы, выжимки, фоновые задачи)
   - `luna-pro` (Anthropic Claude Opus 4.6 — сложная архитектура, системный аудит, авто-эскалация при критике)
   Внутренние сервисные воркеры ($0.00 cost):
   - `luna-coder` (локальная Ollama `qwen2.5-coder:7b` на GPU AMD RX 580)
   - `luna-embed` (локальная Ollama `bge-m3:latest`, 1024-dim векторные эмбеддинги)
   - `luna-auto` (умный роутер с автоматической каскадной эскалацией)
2. **Observability**: Langfuse v2.95.11 (:3001) + PostgreSQL 17 + `pgvector` (:5433) + Redis 7.4 (:6380).
3. **Core API (:8010)**: FastAPI сервис для RAG (векторные чанки документов + порог косинусного сходства) и 4-уровневого контура самокоррекции/самообучения.
4. **UI**: Open WebUI (:3000) / SearXNG (:8082).

## Важные соглашения и правила реализации
1. **Язык**: Код, идентификаторы, докстринги, коммиты, логи — строго на английском (`en`). Ответы пользователю — на русском (`ru`).
2. **Пакетный менеджер**: `uv` (`uv run`, `uv add`, `uv sync`).
3. **Качество кода**: `ruff check --fix`, `ruff format`, `mypy --strict`, `pytest`.
4. **База данных**: PostgreSQL 17 с расширением `vector` (HNSW индексы, `vector_cosine_ops`), миграции только через `Alembic`.
5. **LLM маршрутизация**: Все вызовы — через LiteLLM Proxy / Core API с трейсингом в Langfuse (`session_id`, `user_id`, `routing_reason`).
6. **Парсинг документов**: `docling` / fallback-парсинг с `do_ocr=False` по умолчанию.
7. **Контур улучшения (Flywheel)**:
   - Level 1: Детекция критики/ошибок с контекстом истории и эскалация на Claude Opus 4.6 (`luna-pro`).
   - Level 2: Фиксация Feedback Scores (0/1) в Langfuse Ingestion API по `trace_id`.
   - Level 3: Сохранение антипаттернов и уроков в `feedback_memories` (`pgvector`) с фильтрацией по порогу расстояния `distance <= 1 - threshold`.
   - Level 4: Экспорт DPO датасетов (`GET /api/v1/feedback/export-dpo`) для локального дообучения Ollama.
