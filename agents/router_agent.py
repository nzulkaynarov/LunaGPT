#!/usr/bin/env python3
"""
LunaGPT Intelligent Agent Router CLI
------------------------------------
Запуск маршрутизации с детекцией критики, авто-эскалацией и трейсингом в Langfuse.
"""

import json
import os
import sys
import time
from urllib.request import Request, urlopen

LITELLM_ENDPOINT = os.getenv("LITELLM_ENDPOINT", "http://localhost:4000/v1/chat/completions")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-luna-master-key-2026")

CRITICISM_KEYWORDS = [
    "не работает",
    "ошибка",
    "неправильно",
    "ты ошибся",
    "переделай",
    "исправь",
    "не то",
    "баг",
    "exception",
    "failed",
    "error",
    "wrong",
    "fix this",
    "redo",
    "invalid",
    "сломал",
]

SMART_KEYWORDS = [
    "архитектур",
    "спроектируй",
    "refactor",
    "security",
    "уязвимост",
    "system design",
    "паттерн",
    "trade-off",
    "стратеги",
    "orchestrat",
]

FAST_KEYWORDS = [
    "переведи",
    "кратко изложи",
    "summary",
    "документ",
    "стать",
    "html",
    "таблиц",
    "сравни текст",
    "выжимк",
    "объясни простыми словами",
]


def classify_task(prompt: str) -> tuple[str, str, bool]:
    prompt_lower = prompt.lower()
    is_criticism = any(k in prompt_lower for k in CRITICISM_KEYWORDS)

    if is_criticism:
        return (
            "luna-smart",
            "Детекция критики/ошибки: авто-эскалация на Claude 3.5 Sonnet с рефлексией",
            True,
        )

    if any(k in prompt_lower for k in SMART_KEYWORDS):
        return (
            "luna-smart",
            "Сложная архитектурная задача / глубокий анализ (Claude 3.5 Sonnet)",
            False,
        )

    if len(prompt) > 2000 or any(k in prompt_lower for k in FAST_KEYWORDS):
        return (
            "luna-fast",
            "Объёмный текст / перевод / суммаризация (Gemini 2.5 Flash)",
            False,
        )

    return (
        "luna-coder",
        "Код / генерация скрипта / локальная задача (Ollama RX 580 8GB $0)",
        False,
    )


def execute_routed_query(prompt: str, user_id: str = "umarbek", session_id: str = "cli-session") -> dict:
    target_model, decision_reason, is_criticism = classify_task(prompt)

    system_content = "You are LunaGPT, a helpful intelligent AI assistant."
    if is_criticism:
        system_content += (
            "\n### SELF-CORRECTION PROTOCOL:\n"
            "The user indicated an error in a previous step. Analyze the root cause step-by-step and fix it."
        )

    payload = {
        "model": target_model,
        "messages": [{"role": "system", "content": system_content}, {"role": "user", "content": prompt}],
        "metadata": {
            "user_id": user_id,
            "session_id": session_id,
            "routing_reason": decision_reason,
            "is_criticism": is_criticism,
            "agent_router": "LunaGPT-v2",
        },
    }

    req = Request(
        LITELLM_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LITELLM_KEY}"},
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
                "usage": usage,
            }
    except Exception as e:
        return {"success": False, "model": target_model, "reason": decision_reason, "error": str(e)}


if __name__ == "__main__":
    test_query = sys.argv[1] if len(sys.argv) > 1 else "Напиши функцию на Python для вычисления чисел Фибоначчи"
    print(f"\n[LunaGPT Router v2] Анализ запроса: «{test_query}»...")
    model, reason, is_crit = classify_task(test_query)
    color = "\033[1;31m" if is_crit else "\033[1;32m"
    print(f"→ Выбрана модель: {color}{model}\033[0m")
    print(f"→ Обоснование:   \033[0;36m{reason}\033[0m")

    res = execute_routed_query(test_query)
    if res["success"]:
        print(f"\n✓ Успешно выполнено за {res['duration_sec']}с (Токены: {res.get('usage', {})})")
        print("─" * 60)
        print(res["response"][:300] + ("..." if len(res["response"]) > 300 else ""))
        print("─" * 60)
        print("Трейс автоматически зафиксирован в Langfuse: http://localhost:3001\n")
    else:
        print(f"\n✗ Ошибка вызова: {res.get('error')}\n")
