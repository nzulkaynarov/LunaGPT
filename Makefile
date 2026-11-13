# LunaGPT Management Makefile
.PHONY: help setup up down restart logs status test lint clean

help: ## Показать список доступных команд
	@grep -E '^[a-z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## Создать .env из .env.example (если не создан)
	@if [ ! -f .env ]; then cp .env.example .env && echo "✓ Файл .env создан. Заполните API ключи в .env"; else echo "✓ Файл .env уже существует"; fi

up: setup ## Запустить LunaGPT (шлюз, база, трассы, поиск, Luna)
	docker compose up -d
	@echo "Langfuse: http://localhost:3001  Шлюз: http://localhost:4000  Поиск: http://localhost:8082"

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

test: ## Запуск модульных тестов
	uv run pytest tests/ -v

lint: ## Проверка качества кода с ruff и mypy
	uv run ruff check src/ tests/ config/
	uv run ruff format --check src/ tests/ config/
	uv run mypy src/ tests/

clean: ## Удалить ВСЕ данные (память семьи, ключи шлюза, трассы); спросит подтверждение
	@echo "Это удалит все базы LunaGPT. Последняя копия: $$(ls -1t /data/backups/lunagpt/lunagpt-*.dump 2>/dev/null | head -1)"
	@read -p "Напишите DELETE, чтобы продолжить: " answer && [ "$$answer" = "DELETE" ]
	docker compose down -v
