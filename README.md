# 🌙 LunaGPT v2.0

> **Sovereign AI Infrastructure, Intelligent Model Router, Document RAG & Self-Improving Agent Memory**

LunaGPT — суверенная персональная ИИ-лаборатория и интеллектуальный шлюз с логической маршрутизацией запросов, локальным выполнением задач на GPU AMD Radeon RX 580 (8 ГБ VRAM, $0/токен), полнотекстовым и векторным RAG на `pgvector`, и 100% аудитом в **Langfuse**.

---

## 🏛 Архитектура системы

```text
┌────────────────────────────────────────────────────────────────────────┐
│                               КЛИЕНТЫ                                  │
│  • VS Code + Continue (Автокомплит, чат, рефакторинг :4000/v1)        │
│  • Open WebUI (Персональный ChatGPT-аналог в браузере :3000)           │
│  • LunaGPT Core API (Swagger :8010/docs)                              │
│  • CLI Агенты (Antigravity, Claude Code, Python)                       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ OpenAI API (:4000/v1)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   LunaGPT Gateway (LiteLLM Proxy :4000)                │
│  • Интеллектуальный роутинг моделей и агентов                          │
│  • Автоматический Failover & Балансировка                              │
│  • Redis 7.4 кэширование и координация воркеров                        │
│  • Langfuse v2 Compatibility Logger (/api/public/ingestion)            │
│                                                                        │
│  Виртуальные модели:                                                   │
│   ├── luna-coder → Ollama (qwen2.5-coder:7b на AMD RX 580 GPU, $0)     │
│   ├── luna-fast  → Google Gemini 3.5 Flash Lite (Сверхбыстро, 1M+ ctx) │
│   ├── luna-smart → Anthropic Claude Sonnet 4.6 (Архитектура и логика)  │
│   ├── luna-pro   → Anthropic Claude Opus 4.6 (Глубокий анализ и аудит) │
│   ├── luna-embed → Ollama bge-m3 (Мультиязычный RAG на GPU, 1024-dim)  │
│   └── luna-auto  → Каскадный роутер с авто-эскалацией при ошибках      │
└─────────────────┬───────────────────┬───────────────────┬──────────────┘
                  │                   │                   │
                  ▼                   ▼                   ▼
    ┌──────────────────────┐   ┌─────────────┐   ┌───────────────────────┐
    │     OLLAMA HOST      │   │ CLOUD APIS  │   │  LANGFUSE SERVER :3001│
    │ • AMD RX 580 (8 ГБ)  │   │ • Gemini    │   │ • Полный трейсинг     │
    │ • qwen2.5-coder:7b   │   │ • Anthropic │   │ • Затраты ($) и токены│
    │ • bge-m3:latest      │   │   Claude    │   │ • Feedback Scores 0/1 │
    └──────────────────────┘   └─────────────┘   └───────────┬───────────┘
                                                             │
                                                             ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      ХРАНИЛИЩЕ И ВЕКТОРНАЯ ПАМЯТЬ                      │
│  • PostgreSQL 17 + pgvector 0.8.7 (Port :5433)                         │
│      ├── БД langfuse (Трейсы, сессии, скоры)                           │
│      └── БД lunagpt (Векторный RAG документов, feedback_memories)      │
│  • Redis 7.4 (Port :6380)                                              │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🎯 Матрица моделей (Routing Policy)

| Модель | Назначение | Провайдер | Стоимость |
| :--- | :--- | :--- | :--- |
| **`luna-coder`** | Tab-автокомплит, тесты, генерация функций и скриптов | Локальная **AMD RX 580 (8 ГБ)** | **$0.00** (34.2 tok/s) |
| **`luna-fast`** | Большие документы, перевод RU/UZ/EN, суммаризация | Google Gemini 3.5 Flash Lite | Доли цента |
| **`luna-smart`** | Архитектура монорепо, безопасность, дизайн-документы, баги | Anthropic Claude Sonnet 4.6 | По тарифу Anthropic |
| **`luna-pro`** | Долгие рассуждения, комплексный аудит всей кодовой базы | Anthropic Claude Opus 4.6 | По тарифу Anthropic |
| **`luna-embed`** | Индексация документов и RAG базы знаний | Локальная Ollama (`bge-m3`) | **$0.00** |
| **`luna-auto`** | Авто-выбор с эскалацией при повторных ошибках | Ollama $\to$ Claude Sonnet $\to$ Gemini | Оптимизированная |

---

## 🔄 4-Уровневый контур самообучения (Active Learning)

1. **Level 1 (Realtime Reflection & Self-Correction):** Детектор критики пользователя в промптах (*«не работает», «ошибка», «TypeError»*) автоматически эскалирует задачу на `luna-smart` (Claude Sonnet 4.6) с включением протокола пошаговой рефлексии причин сбоя.
2. **Level 2 (Langfuse Observability & Scoring):** Фиксация оценок 👍 (1.0) / 👎 (0.0) и комментариев пользователя к каждому `trace_id`.
3. **Level 3 (pgvector Mistake Memory):** Сохранение пар «запрос — отвергнутый ответ — правильный ответ» в таблицу `feedback_memories` с HNSW косинусным поиском через локальную `bge-m3`. При повторении схожей задачи модель получает блок «Past Lessons» и не наступает на те же грабли.
4. **Level 4 (Fine-Tuning Dataset Export):** Выгрузка накопленных троек (Prompt, Rejected, Chosen) в формате DPO для периодического локального дообучения модели `qwen2.5-coder:7b`.

---

## 🚀 Быстрый старт

### 1. Подготовка окружения
```bash
# Клонирование (всегда с shallow clone)
git clone --depth 1 git@github.com:nzulkaynarov/LunaGPT.git
cd LunaGPT

# Настройка переменных окружения
cp .env.example .env
# Заполните в .env: GEMINI_API_KEY, ANTHROPIC_API_KEY
```

### 2. Запуск сервисов
```bash
# Запуск контейнеров (Gateway, Langfuse, Postgres+pgvector, Redis)
make up

# Запуск Core API (FastAPI)
uv run uvicorn lunagpt.api:app --host 0.0.0.0 --port 8010
```

### 3. Доступные порты и интерфейсы
* ⚡ **LiteLLM Gateway (API :4000/v1):** `http://localhost:4000` (Ключ: из переменной `LITELLM_MASTER_KEY` в `.env`)
* 📊 **Langfuse (Аналитика и трейсы):** [http://localhost:3001](http://localhost:3001) (`admin@lunagpt.local`)
* 📖 **LunaGPT Core API (Swagger UI):** [http://localhost:8010/docs](http://localhost:8010/docs)
* 🗄 **PostgreSQL 17 + pgvector:** `localhost:5433` (Базы: `lunagpt`, `openwebui` и `langfuse`)
* ⚡ **Redis 7.4:** `localhost:6380`

---

## 🔌 Подключение к VS Code (Continue)

В `~/.continue/config.yaml` укажите (подставив ваш `LITELLM_MASTER_KEY` из `.env`):
```yaml
name: LunaGPT Config
version: 1.0.0
schema: v1
models:
  - name: Luna Coder (Local Ollama $0)
    provider: openai
    model: luna-coder
    apiBase: http://localhost:4000/v1
    apiKey: YOUR_LITELLM_MASTER_KEY
    roles: [chat, autocomplete, edit]

  - name: Luna Smart (Claude Sonnet 4.6)
    provider: openai
    model: luna-smart
    apiBase: http://localhost:4000/v1
    apiKey: YOUR_LITELLM_MASTER_KEY
    roles: [chat, edit]

  - name: Luna Auto (Smart Router)
    provider: openai
    model: luna-auto
    apiBase: http://localhost:4000/v1
    apiKey: YOUR_LITELLM_MASTER_KEY
    roles: [chat]
```

---

## 🧪 Тестирование и качество кода

```bash
# Запуск тестов
uv run pytest tests/

# Проверка и форматирование кода
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

---

## 📚 Документация проекта
- [docs/architecture.md](docs/architecture.md) — детальная архитектура и схема потоков данных
- [docs/roadmap.md](docs/roadmap.md) — дорожная карта и выполненные этапы
- [MEMORY.md](MEMORY.md) — контекстная память проекта
- [AGENTS.md](AGENTS.md) — регламент для автономных ИИ-агентов

## Luna AI Assistant

Семейный ассистент в Telegram работает на этом хосте как сервис `luna-assistant`. Вход только по приглашениям, первая ссылка создаётся командой:

```bash
docker exec luna-assistant python3 -m lunagpt.assistant.invite owner "Имя"
```

Архитектура: [docs/architecture.md](docs/architecture.md). Решения и источники: [docs/research](docs/research/2026-11-luna-assistant-decisions.md).
