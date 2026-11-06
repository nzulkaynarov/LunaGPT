# 🌙 LunaGPT

**LunaGPT** — персональная ИИ-лаборатория и интеллектуальный шлюз с логической маршрутизацией агентов, локальным выполнением на GPU и 100% аудитом в **Langfuse**.

Объединяет **Роль 1 (AI-разработка и агенты в IDE)** и **Роль 3 (Личный ChatGPT + база знаний в браузере)** в единую отказоустойчивую экосистему на вашем ПК.

---

## 🏛 Архитектура системы

```text
┌────────────────────────────────────────────────────────────────────────┐
│                               КЛИЕНТЫ                                  │
│  • Open WebUI (Браузер на ПК и смартфоне через Tailscale)             │
│  • VS Code / Continue (Автокомплит и чат в IDE)                       │
│  • Агенты (Antigravity / Claude Code / Python скрипты)                │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ OpenAI API (:4000/v1)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   LunaGPT Gateway (LiteLLM Proxy)                      │
│  • Интеллектуальный роутинг моделей и агентов                          │
│  • Автоматический Failover & Балансировка                              │
│                                                                        │
│  Виртуальные модели:                                                   │
│   ├── luna-coder → Ollama (AMD Radeon RX 580 GPU, 8GB VRAM, $0)       │
│   ├── luna-fast  → Google Gemini 2.5 Flash (Сверхбыстро, 1M+ контекст) │
│   ├── luna-smart → Anthropic Claude 3.5 Sonnet (Архитектура и логика)  │
│   ├── luna-pro   → Google Gemini 2.5 Pro (Глубокий анализ кода)        │
│   └── luna-embed → Ollama bge-m3 (Мультиязычный RAG на GPU)            │
└─────────────────┬────────────────────────────────────┬─────────────────┘
                  │ Async Logging                      │ Запросы
                  ▼                                    ▼
┌───────────────────────────────────┐    ┌───────────────────────────────┐
│          LANGFUSE SERVER          │    │      ПРОВАЙДЕРЫ МОДЕЛЕЙ       │
│  • Полный трейсинг диалогов       │    │  1. Ollama (GPU RX 580)       │
│  • Подсчёт затрат ($) и токенов   │    │  2. Google Gemini API         │
│  • Анализ задержек (Latency)      │    │  3. Anthropic Claude API      │
│  • Оценка качества промптов       │    └───────────────────────────────┘
└───────────────────────────────────┘
```

---

## 🎯 Логика распределения задач (Routing Policy)

| Модель | Назначение | Провайдер | Стоимость |
| :--- | :--- | :--- | :--- |
| **`luna-coder`** | Tab-автокомплит, тесты, генерация типовых функций и скриптов | Локальная **AMD RX 580 (8 ГБ)** | **$0.00** (34.2 токена/сек) |
| **`luna-fast`** | Большие документы (до 1-2 млн токенов), перевод RU/UZ/EN, суммаризация | Google Gemini 2.5 Flash | Бесплатный лимит / копейки |
| **`luna-smart`** | Архитектура монорепо, безопасность, дизайн-документы, сложные баги | Anthropic Claude 3.5 Sonnet | По тарифу API Anthropic |
| **`luna-pro`** | Долгие рассуждения, комплексный аудит всей кодовой базы | Google Gemini 2.5 Pro | По тарифу API Google |
| **`luna-embed`** | Индексация документов и базы знаний (RAG) | Локальная Ollama (`bge-m3`) | **$0.00** |

---

## 🚀 Быстрый старт

### 1. Подготовка ключей
```bash
cd ~/projects/LunaGPT
cp .env.example .env
```
Откройте `.env` и впишите ваши API-ключи:
* `GEMINI_API_KEY`
* `ANTHROPIC_API_KEY`

### 2. Запуск LunaGPT
```bash
make up
```

Все сервисы запустятся в фоновом режиме:
* 🌐 **Luna Web UI (Интерфейс чата):** [http://localhost:3000](http://localhost:3000)
* 📊 **Langfuse (Аналитика и трейсинг):** [http://localhost:3001](http://localhost:3001)
* ⚡ **LiteLLM Gateway (API Шлюз):** `http://localhost:4000/v1`

---

## 🔌 Подключение к VS Code (Continue)

Чтобы все запросы из редактора шли через шлюз LunaGPT и логировались в Langfuse:

Откройте `~/.continue/config.yaml` и укажите провайдер `openai` с адресом LunaGPT Gateway:

```yaml
models:
  - name: Luna Coder (RX 580 Local)
    provider: openai
    model: luna-coder
    apiBase: http://localhost:4000/v1
    apiKey: sk-luna-master-key-2026
    roles:
      - autocomplete
      - edit

  - name: Luna Smart (Claude 3.5 Sonnet)
    provider: openai
    model: luna-smart
    apiBase: http://localhost:4000/v1
    apiKey: sk-luna-master-key-2026
    roles:
      - chat

  - name: Luna Fast (Gemini Flash)
    provider: openai
    model: luna-fast
    apiBase: http://localhost:4000/v1
    apiKey: sk-luna-master-key-2026
    roles:
      - chat
```

---

## 📱 Удалённый доступ со смартфона или ноутбука (Tailscale)

Так как на вашем ПК уже настроен **Tailscale**, веб-интерфейс LunaGPT доступен с любого вашего авторизованного устройства без необходимости пробрасывать порты в интернет:

1. Узнайте постоянный IP машины: `tailscale ip -4` (например, `100.x.y.z`).
2. В браузере телефона или ноутбука откройте:
   * Чат LunaGPT: `http://100.x.y.z:3000`
   * Панель Langfuse: `http://100.x.y.z:3001`

---

## 🛠 Управление проектом

* `make status` — проверить здоровье контейнеров и локальной Ollama GPU.
* `make logs` — просмотр живых логов LiteLLM и веб-интерфейса.
* `make test-router` — тестовый запуск логического агента-классификатора.
* `make down` — остановка сервисов.
