#!/usr/bin/env python3
"""
LunaGPT Intelligent Agent Router
--------------------------------
Модуль логического роутинга запросов с полной трассировкой в Langfuse.

Логика распределения:
  - Code / Tests / Quick Script  → luna-coder (Ollama на AMD RX 580 GPU, $0, 34 tok/s)
  - Large Docs / Summarization   → luna-fast  (Gemini 2.5 Flash, миллионные контексты)
  - Architecture / Complex Logic → luna-smart (Claude 3.5 Sonnet)
"""

import os
import sys
import time
from typing import Dict, Any, Tuple
from urllib.request import Request, urlopen
import json

LITELLM_ENDPOINT = os.getenv("LITELLM_ENDPOINT", "http://localhost:4000/v1/chat/completions")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-luna-master-key-2026")


def classify_task(prompt: str) -> Tuple[str, str]:
    """
    Эвристический и семантический классификатор задачи.
    Возвращает (имя_модели, причина_выбора).
    """
    prompt_lower = prompt.lower()

    # 1. Признаки сложной архитектуры, безопасности и глубокого рассуждения -> Claude
    smart_keywords = [
        "архитектур", "спроектируй", "refactor", "security", "уязвимост",
        "system design", "паттерн", "trade-off", "стратеги", "orchestrat"
    ]
    if any(k in prompt_lower for k in smart_keywords):
        return "luna-smart", "Сложная архитектурная задача / глубокий анализ (Claude 3.5 Sonnet)"

    # 2. Признаки больших объёмов текста, суммаризации и перевода -> Gemini Flash
    fast_keywords = [
        "переведи", "кратко изложи", "summary", "документ", "стать",
        "html", "таблиц", "сравни текст", "выжимк", "объясни простыми словами"
    ]
    if len(prompt) > 2000 or any(k in prompt_lower for k in fast_keywords):
        return "luna-fast", "Объёмный текст / перевод / суммаризация (Gemini 2.5 Flash)"

    # 3. Всё программирование, скрипты, тесты и быстрые вопросы -> Локальная GPU RX 580 ($0)
    return "luna-coder", "Код / генерация скрипта / локальная задача (Ollama RX 580 8GB)"


def execute_routed_query(prompt: str, user_id: str = "umarbek", session_id: str = "cli-session") -> Dict[str, Any]:
    """
    Выполняет запрос через LunaGPT Gateway с автоматической отправкой трассировки в Langfuse.
    """
    target_model, decision_reason = classify_task(prompt)

    payload = {
        "model": target_model,
        "messages": [
            {"role": "system", "content": "You are LunaGPT, a helpful intelligent AI assistant."},
            {"role": "user", "content": prompt}
        ],
        "metadata": {
            "user_id": user_id,
            "session_id": session_id,
            "routing_reason": decision_reason,
            "agent_router": "LunaGPT-v1"
        }
    }

    req = Request(
        LITELLM_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_KEY}"
        }
    )

    start_time = time.time()
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = time.time() - start_time
            response_text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return {
                "success": True,
                "model": target_model,
                "reason": decision_reason,
                "duration_sec": round(elapsed, 2),
                "response": response_text,
                "usage": usage
            }
    except Exception as e:
        return {
            "success": False,
            "model": target_model,
            "reason": decision_reason,
            "error": str(e)
        }


if __name__ == "__main__":
    test_query = sys.argv[1] if len(sys.argv) > 1 else "Напиши функцию на Python для вычисления чисел Фибоначчи"
    print(f"\n[LunaGPT Router] Анализ запроса: «{test_query}»...")
    model, reason = classify_task(test_query)
    print(f"→ Выбрана модель: \033[1;32m{model}\033[0m")
    print(f"→ Обоснование:   \033[0;36m{reason}\033[0m")
    print("\nДля отправки запроса убедитесь, что запущен шлюз: 'make up' в /home/umarbek/projects/LunaGPT\n")
