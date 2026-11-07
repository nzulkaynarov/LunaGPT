# Project Memory: LunaGPT

## Обзор и архитектурный контекст
LunaGPT — персональный суверенный ИИ-шлюз и среда выполнения на базе Linux хоста с локальным GPU AMD Radeon RX 580 (8 ГБ VRAM).
Основные компоненты:
1. **LiteLLM Proxy Gateway (:4000/v1)**: Единая точка доступа по протоколу OpenAI. Модели:
   - `luna-coder` (локальная Ollama `qwen2.5-coder:7b` на GPU, $0)
   - `luna-embed` (локальная Ollama `bge-m3:latest`, 1024-dim, $0)
   - `luna-fast` (Google Gemini 2.5 Flash)
   - `luna-smart` (Anthropic Claude 3.5 Sonnet)
   - `luna-pro` (Google Gemini 2.5 Pro)
   - `luna-auto` (роутер с автоматической эскалацией)
2. **Observability**: Langfuse v2.95.11 (:3001) + PostgreSQL 17 + `pgvector` (:5432) + Redis 7.4 (:6379).
3. **Core API (:8010)**: FastAPI сервис для RAG (Docling + pgvector) и 4-уровневого контура самокоррекции/самообучения.
4. **UI**: Open WebUI (:3000) / LiteLLM UI.

## Важные соглашения и правила реализации
1. **Язык**: Код, идентификаторы, докстринги, коммиты, логи — строго на английском (`en`). Ответы пользователю — на русском (`ru`).
2. **Пакетный менеджер**: `uv` (`uv run`, `uv add`, `uv sync`).
3. **Качество кода**: `ruff check --fix`, `ruff format`, `mypy --strict`, `pytest`.
4. **База данных**: PostgreSQL 17 с расширением `vector` (HNSW индексы, `vector_cosine_ops`), миграции только через `Alembic`.
5. **LLM маршрутизация**: Все вызовы — строго через LiteLLM Proxy с трейсингом в Langfuse (`session_id`, `user_id`, `routing_reason`).
6. **Парсинг документов**: `docling` с `do_ocr=False` по умолчанию (OCR включать только при необходимости).
7. **Контур улучшения (Flywheel)**:
   - Детекция ошибок и негатива в промптах.
   - Self-correction рефлексия (Chain-of-Thought) и эскалация на Claude 3.5 Sonnet.
   - Сохранение антипаттернов в `feedback_memories` (`pgvector`).
   - Экспорт DPO датасетов для периодического файнтюнинга локальной Ollama.
