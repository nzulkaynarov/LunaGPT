# LunaGPT Management Makefile
.PHONY: help setup up down restart logs status test-router clean

help: ## Показать список доступных команд
	@grep -E '^[a-z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## Создать .env из .env.example (если не создан)
	@if [ ! -f .env ]; then cp .env.example .env && echo "✓ Файл .env создан. Заполните API ключи в .env"; else echo "✓ Файл .env уже существует"; fi

up: setup ## Запустить весь стек LunaGPT (Gateway, WebUI, Langfuse)
	docker compose up -d
	@echo "\n======================================================="
	@echo "🎉 LunaGPT успешно запущен!"
	@echo "======================================================="
	@echo "• Web UI (Чат / Документы): http://localhost:3000"
	@echo "• Langfuse (Трейсинг и аналитика): http://localhost:3001"
	@echo "• LiteLLM Gateway (API :4000/v1): http://localhost:4000"
	@echo "• Локальная Ollama (GPU RX 580):  http://localhost:11434"
	@echo "======================================================="

down: ## Остановить контейнеры LunaGPT
	docker compose down

restart: down up ## Перезапустить контейнеры

logs: ## Посмотреть логи всех сервисов
	docker compose logs -f

status: ## Проверить статус контейнеров и локальной Ollama
	@echo "\n--- Контейнеры LunaGPT ---"
	docker compose ps
	@echo "\n--- Статус локальной Ollama (GPU RX 580) ---"
	@curl -s http://127.0.0.1:11434/api/ps || echo "Ollama не отвечает"

test-router: ## Протестировать логический роутер агентов
	python3 agents/router_agent.py "Спроектируй схему шины событий для ERP"
	python3 agents/router_agent.py "Напиши юнит-тест на Python для проверки авторизации"
	python3 agents/router_agent.py "Сделай краткую выжимку этого длинного юридического соглашения"

clean: ## Удалить данные контейнеров (очистка томов)
	docker compose down -v
