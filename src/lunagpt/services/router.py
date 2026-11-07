"""Smart Router with Negative Feedback Detection, Reflection, and Model Escalation."""

from typing import Tuple
import uuid
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.config import settings
from lunagpt.schemas import PromptRequest, PromptResponse
from lunagpt.services.ingestion import search_document_chunks
from lunagpt.services.memory import search_relevant_feedback

CRITICISM_KEYWORDS = [
    "не работает", "ошибка", "неправильно", "ты ошибся", "переделай",
    "исправь", "не то", "баг", "exception", "failed", "error", "wrong",
    "fix this", "redo", "invalid", "сломал"
]

SMART_KEYWORDS = [
    "архитектур", "спроектируй", "refactor", "security", "уязвимост",
    "system design", "паттерн", "trade-off", "стратеги", "orchestrat"
]

FAST_KEYWORDS = [
    "переведи", "кратко изложи", "summary", "документ", "стать",
    "html", "таблиц", "сравни текст", "выжимк", "объясни простыми словами"
]


def detect_criticism_and_model(prompt: str) -> Tuple[str, str, bool]:
    """
    Classify model and detect user dissatisfaction / criticism.
    Returns (model_name, decision_reason, is_criticism).
    """
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
        "Код / генерация скрипта / типовая задача (Ollama RX 580 8GB $0)",
        False,
    )


async def execute_smart_routing(
    db: AsyncSession, req: PromptRequest
) -> PromptResponse:
    """
    Main agent execution flow:
    1. Detect criticism & select model.
    2. Query past lessons (feedback_memories) from pgvector.
    3. Query ingested document chunks from pgvector.
    4. Build dynamic system prompt with Self-Correction Protocol if criticism detected.
    5. Execute via LiteLLM Gateway (tracked in Langfuse).
    """
    model, reason, is_criticism = detect_criticism_and_model(req.prompt)
    trace_id = str(uuid.uuid4())

    system_instructions = [
        "You are LunaGPT, a premier intelligent sovereign AI coding assistant."
    ]

    lessons_count = 0
    if req.use_memory:
        try:
            memories = await search_relevant_feedback(db, req.prompt, limit=2)
            if memories:
                lessons_count = len(memories)
                lessons_text = "\n".join(
                    [
                        f"- ПРЕДЫДУЩАЯ ОШИБКА: {m.user_prompt}\n  ПРАВИЛЬНОЕ РЕШЕНИЕ: {m.corrected_response}"
                        for m in memories
                    ]
                )
                system_instructions.append(
                    f"### PAST LESSONS & PREVENTED MISTAKES:\n{lessons_text}\n"
                    "Do NOT repeat the previous mistakes mentioned above."
                )
        except Exception:
            pass

    rag_count = 0
    if req.use_rag:
        try:
            chunks = await search_document_chunks(db, req.prompt, limit=3)
            if chunks:
                rag_count = len(chunks)
                rag_text = "\n\n".join([f"[{c.chunk_index}] {c.content}" for c in chunks])
                system_instructions.append(
                    f"### RELEVANT KNOWLEDGE BASE CONTEXT (RAG):\n{rag_text}"
                )
        except Exception:
            pass

    if is_criticism:
        system_instructions.append(
            "### SELF-CORRECTION PROTOCOL:\n"
            "The user indicated an error or expressed dissatisfaction with a previous response.\n"
            "1. First, explicitly analyze the root cause of the error step-by-step.\n"
            "2. Provide the corrected, verified, and complete solution without defensive wording."
        )

    messages = [
        {"role": "system", "content": "\n\n".join(system_instructions)},
        {"role": "user", "content": req.prompt},
    ]

    payload = {
        "model": model,
        "messages": messages,
        "metadata": {
            "trace_id": trace_id,
            "user_id": req.user_id,
            "session_id": req.session_id,
            "routing_reason": reason,
            "is_criticism": is_criticism,
            "lessons_injected": lessons_count,
            "rag_chunks_injected": rag_count,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_master_key}",
    }

    import time
    start_t = time.time()

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            settings.litellm_endpoint, json=payload, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        duration = round(time.time() - start_t, 2)

        content = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {})

        return PromptResponse(
            success=True,
            model=model,
            reason=reason,
            duration_sec=duration,
            response=content,
            trace_id=trace_id,
            tokens=tokens,
            lessons_injected=lessons_count,
            rag_chunks_injected=rag_count,
        )
