# Исследование и решения: Luna AI Assistant (ноябрь 2026)

Правило проекта: каждый шаг опирается на исследование или бенчмарк, используем проверенные решения, код закрепляем тестами.

| Вопрос | Решение | Основание |
|---|---|---|
| Клиент Telegram | aiogram 3 | Уже работает в HomeSec; самописный клиент заменён |
| Подключение семьи | Люди и устройства только на Pi, Telegram привязывается одноразовой deep-link ссылкой | Официальный механизм Telegram для привязки аккаунта; параметр до 64 символов |
| Интерфейс бота | Постоянная клавиатура для разделов, inline-кнопки для действий с правкой того же сообщения, ForceReply для ввода имени, aiogram FSM | Рекомендации Telegram; права проверяются на сервере независимо от меню |
| Общие устройства | На время режима устройство считается взрослым, автоматический возврат | Использует существующие правила и фильтрацию; питание потребует Home Assistant |
| Основная локальная модель | `gemma4:e4b`, без рассуждений | Собственный бенчмарк 8 задач HomeSec и 14 детских вопросов, см. ниже |
| Контроль падений | Heartbeat от Luna на Pi (dead man's switch), опрос Pi с ПК | Push-heartbeat это стандартный шаблон; Uptime Kuma и Healthchecks рассылают по фиксированным каналам, а нужна рассылка всем участникам через токен Luna |
| Падение обеих машин | Внешний Healthchecks.io (план) | Единственный вариант, не зависящий от дома |
| Google Workspace | `taylorwilsdon/google_workspace_mcp` (план, проверить сопровождение и безопасность) | OAuth 2.1 для нескольких пользователей, контейнерный режим |
| Шлюз MCP | LiteLLM MCP Gateway | Права на серверы и инструменты по ключам, фильтрация при обнаружении и вызове |
| Open WebUI | Встроенный MCP по Streamable HTTP | Поддерживается с 0.6.31; ограничений по инструментам нет, поэтому через LiteLLM |
| Действия агента | Подтверждение привязано к точному действию, одноразовое; письма считаются недоверенными | OWASP MCP Security, AI Agent Security, LLM Prompt Injection Prevention |
| Продукт: семейный контроль | Запрос времени и одобрение родителем как главный сценарий | Family Link и аналоги; сравнений по удержанию и наградам в открытых источниках не найдено, поэтому метрики проверяем сами |

## Бенчмарк локальных моделей (9 ноября 2026, RX 580 8 ГБ)

| Задача | qwen2.5-coder:7b | qwen3:8b | gemma4:e4b |
|---|---|---|---|
| Объяснение ребёнку | Сбивчиво | Хорошо | Хорошо |
| Наводящий вопрос | Отказ | Дала ответ | Верно |
| Безопасность | Сухой отказ | Хорошо | Хорошо с поддержкой |
| Викторина | Неверный факт | Верно | Верно |
| Вызов инструментов | Верно, зацикливается | Верно | Верно |

В отдельном прогоне викторин `gemma4:e4b` выдала вымышленный факт («морская корова» — кашалот), поэтому викторины генерирует Gemini.

Без рассуждений `gemma4:e4b` отвечает за 1–7 с. Детский набор из 14 вопросов: все 8 рискованных отклонены или перенаправлены к взрослым. Это разовые прогоны, не статистический бенчмарк.

## Источники

- Telegram: [Клавиатуры и функции ботов](https://core.telegram.org/bots/features), [Bot API](https://core.telegram.org/bots/api)
- Telegram: [Deep linking](https://core.telegram.org/bots/features), [формат ссылок](https://core.telegram.org/api/links)
- LiteLLM: [MCP Permission Management](https://docs.litellm.ai/docs/mcp_control), [MCP Overview](https://docs.litellm.ai/docs/mcp)
- Open WebUI: [MCP](https://docs.openwebui.com/features/extensibility/mcp/)
- Google Workspace: [google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)
- OWASP: [MCP Security](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html), [AI Agent Security](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html), [Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- Мониторинг: [Uptime Kuma vs Healthchecks](https://selfhosting.sh/compare/uptime-kuma-vs-healthchecks/)
- Родительский контроль: [сравнение приложений 2026](https://www.security.org/parental-controls/best/)
- Локальные модели: [Ollama Gemma 4](https://ollama.com/library/gemma4)
