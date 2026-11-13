# 🌙 LunaGPT

Luna — семейный ассистент в Telegram: помнит дела каждого человека, напоминает, ведёт списки, отвечает голосом и текстом, разбирает фото, помогает детям с уроками. LunaGPT — всё, на чём Luna работает: бот, шлюз моделей, база и трассировка.

Домашней сетью Luna не управляет: это бот HomeSec на Raspberry Pi (отдельный репозиторий). Граница описана в `docs/product.md`.

## Что работает

```text
Telegram ──► luna-assistant (aiogram, long polling)
                │
                ├─► LiteLLM gateway :4000 ──► Claude (взрослые), Gemini (дети, голос),
                │       │                      Ollama на RX 580 (гости, фоновая работа)
                │       └─► Langfuse :3001 (трассы без текста разговоров семьи)
                ├─► Postgres + pgvector (память, напоминания, списки, учёт использования)
                ├─► SearXNG (поиск в интернете)
                └─► HomeSec на Pi (люди, время в интернете детей)

HomeSec на Pi ──► LiteLLM gateway (свой ключ с лимитом) и core :8010 (распознавание голоса)
```

| Сервис | Контейнер | Зачем |
|---|---|---|
| Luna | `luna-assistant` | Бот: `src/lunagpt/assistant/` |
| Шлюз | `lunagpt-gateway` | Все вызовы моделей, ключи клиентов с лимитами: `config/litellm-config.yaml` |
| База | `lunagpt-postgres` | Базы `lunagpt`, `litellm`, `langfuse` и сохранённая `openwebui` |
| Трассы | `lunagpt-langfuse` | Langfuse v2, логгер `config/langfuse_v2_logger.py` |
| Поиск | `lunagpt-searxng` | Поиск для ответов Luna |
| Core | `lunagpt-core` | Только распознавание голоса для HomeSec, уйдёт после перевода HomeSec на шлюз |
| Миграции | `lunagpt-migrate` | Alembic при каждом запуске, остальные ждут его |

Модели Luna и их порядок для каждой функции заданы в `src/lunagpt/assistant/llm.py`; запасных цепочек у моделей `luna-*` на шлюзе нет.

## Разработка

```bash
uv sync
uv run ruff check src/ tests/ config/ migrations/
uv run ruff format --check src/ tests/ config/ migrations/
uv run mypy src/ tests/ config/
uv run pytest tests/
```

Тесты не трогают боевые данные: `tests/conftest.py` подставляет поддельные ключи и недоступные адреса, а базу поднимает в одноразовом контейнере (нужен Docker). В CI база своя.

## Выкладка

```bash
git pull && scripts/deploy.sh
```

Скрипт собирает образ текущего коммита, сохраняет работающий как `lunagpt-core:previous`, применяет миграции, перезапускает core и Luna и откатывается, если Luna не поднялась. Код берётся из образа, рабочая папка в контейнеры не подключается.

## Эксплуатация

Резервные копии, восстановление и удаление данных: `docs/operations.md`. Решения о продукте: `docs/product.md`, устройство: `docs/architecture.md`.
