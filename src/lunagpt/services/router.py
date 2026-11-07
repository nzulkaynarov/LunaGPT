"""Smart Router with Negative Feedback Detection, Reflection, and Model Escalation."""

import json
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.config import settings
from lunagpt.schemas import ChatCompletionRequest, PromptRequest, PromptResponse
from lunagpt.services.ingestion import search_document_chunks
from lunagpt.services.memory import search_relevant_feedback

BACKGROUND_TASK_PATTERNS = [
    "### Task:",
    "Generate a concise, 3-5 word title",
    "Generate 1-3 broad tags",
    "Generate 3 relevant follow-up",
    "Provide a search query to find information",
]

CRITICISM_PHRASES = [
    "не работает",
    "неправильно",
    "ты ошибся",
    "вы ошиблись",
    "переделай",
    "исправь",
    "сломал",
    "ты сломал",
    "не помогло",
    "выдает ошибку",
    "выдаёт ошибку",
    "в коде ошибка",
    "ошибка осталась",
    "все еще ошибка",
    "всё ещё ошибка",
    "снова ошибка",
    "fix this",
    "redo",
    "wrong",
    "broken",
    "did not work",
    "still failing",
    "still error",
    "doesn't work",
    "you are wrong",
    "it failed",
]

PRO_KEYWORDS = [
    "архитектур",
    "спроектируй",
    "system design",
    "паттерн",
    "trade-off",
    "стратеги",
    "security audit",
    "уязвимост",
    "orchestrat",
    "deep thinking",
    "глубокий анализ",
    "детальный аудит",
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


def _extract_text_from_content(content: Any) -> str:
    """Extract plain text string from str, list of content parts, or dict."""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(_extract_text_from_content(item["content"]))
        return " ".join(parts)
    if isinstance(content, dict):
        if "text" in content:
            return str(content["text"])
        if "content" in content:
            return _extract_text_from_content(content["content"])
    return str(content)


def is_openwebui_task(text: str) -> bool:
    """Detect automated background utility tasks from Open WebUI."""
    return any(p in text for p in BACKGROUND_TASK_PATTERNS)


def detect_criticism_and_model(prompt: Any, has_history: bool = True) -> tuple[str, str, bool]:
    """
    Classify model and detect user dissatisfaction / criticism.
    Supports str, list of parts, or dicts.
    Returns (model_name, decision_reason, is_criticism).
    """
    clean_text = _extract_text_from_content(prompt)
    if is_openwebui_task(clean_text):
        return (
            "luna-fast",
            "Служебная фоновая задача Open WebUI (Gemini 3.5 Flash Lite)",
            False,
        )

    prompt_lower = clean_text.lower()

    # Criticism is only possible if there is previous dialogue history with the assistant
    is_criticism = False
    if has_history:
        has_phrase = any(phrase in prompt_lower for phrase in CRITICISM_PHRASES)
        has_word_match = bool(
            re.search(r"\b(не то|это не то|ответ не то|код сломан)\b", prompt_lower)
            or re.search(r"\b(баг|exception|failed|error|ошибка)\b", prompt_lower)
        )
        is_criticism = has_phrase or has_word_match

    if is_criticism:
        return (
            "luna-pro",
            "Детекция критики/ошибки: авто-эскалация на Claude Opus 4.6 (luna-pro) с глубокой рефлексией",
            True,
        )

    if any(k in prompt_lower for k in PRO_KEYWORDS):
        return (
            "luna-pro",
            "Сложная архитектурная задача / глубокий системный аудит (Claude Opus 4.6)",
            False,
        )

    if len(clean_text) > 2000 or any(k in prompt_lower for k in FAST_KEYWORDS):
        return (
            "luna-fast",
            "Объёмный текст / перевод / выжимка (Gemini 3.5 Flash Lite)",
            False,
        )

    return (
        "luna-smart",
        "Универсальный интеллект: аналитика, код, логика (Claude Sonnet 4.6)",
        False,
    )


async def execute_smart_routing(db: AsyncSession, req: PromptRequest) -> PromptResponse:
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

    system_instructions = ["You are LunaGPT, a premier intelligent sovereign AI coding assistant."]

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
                system_instructions.append(f"### RELEVANT KNOWLEDGE BASE CONTEXT (RAG):\n{rag_text}")
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
        resp = await client.post(settings.litellm_endpoint, json=payload, headers=headers)
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


async def execute_openai_chat_completion(
    db: AsyncSession, req: ChatCompletionRequest
) -> tuple[dict[str, Any] | None, AsyncGenerator[bytes, None] | None]:
    """
    OpenAI-compatible chat completion handler with:
    1. Robust extraction for text, lists, dicts, tool calls, and user metadata.
    2. Criticism detection and automatic escalation to luna-smart (Claude Sonnet 4.6).
    3. Dynamic injection of past lessons (feedback_memories) from pgvector.
    4. Safe fallback for embedding models.
    5. Transparent streaming (SSE) or full JSON response from LiteLLM.
    """
    # 1. Extract latest user query safely
    user_query = ""
    for msg in reversed(req.messages):
        if msg.role == "user" and msg.content:
            user_query = _extract_text_from_content(msg.content)
            if user_query:
                break

    has_assistant_history = any(m.role == "assistant" for m in req.messages)
    is_background = is_openwebui_task(user_query)
    detected_model, reason, is_criticism = detect_criticism_and_model(user_query, has_history=has_assistant_history)

    # Safe fallback and alias mapping (3-tier lineup)
    requested_model = (req.model or "luna-smart").strip()
    if is_background:
        target_model = "luna-fast"
    elif requested_model in ("luna-auto", "auto", "default", ""):
        target_model = detected_model
    elif requested_model in ("luna-embed", "embed"):
        target_model = "luna-fast"
    elif requested_model == "luna-smart" and is_criticism:
        target_model = "luna-pro"
    elif requested_model in ("luna-fast", "luna-smart", "luna-pro", "luna-coder"):
        target_model = requested_model
    else:
        target_model = "luna-smart"

    # 2. Query past lessons and RAG documents from pgvector (skip for background tasks)
    lessons_injected = 0
    rag_chunks_injected = 0
    extra_system_parts: list[str] = []

    if user_query and not is_background:
        try:
            memories = await search_relevant_feedback(db, user_query, limit=2)
            if memories:
                lessons_injected = len(memories)
                lessons_text = "\n".join(
                    [
                        f"- PREVIOUS MISTAKE: {m.user_prompt}\n  CORRECTED BEHAVIOR: {m.corrected_response}"
                        for m in memories
                    ]
                )
                extra_system_parts.append(
                    f"### PAST LESSONS & PREVENTED MISTAKES:\n{lessons_text}\n"
                    "Do NOT repeat the previous mistakes mentioned above."
                )
        except Exception:
            pass

        try:
            chunks = await search_document_chunks(db, user_query, limit=3)
            if chunks:
                rag_chunks_injected = len(chunks)
                rag_text = "\n\n".join([f"[{c.chunk_index}] {c.content}" for c in chunks])
                extra_system_parts.append(f"### RELEVANT KNOWLEDGE BASE CONTEXT (RAG):\n{rag_text}")
        except Exception:
            pass

    if is_criticism:
        extra_system_parts.append(
            "### SELF-CORRECTION PROTOCOL:\n"
            "The user indicated an error or expressed dissatisfaction with a previous response.\n"
            "1. First, explicitly analyze the root cause of the error step-by-step.\n"
            "2. Provide the corrected, verified, and complete solution without defensive wording."
        )

    # 3. Format messages safely
    out_messages: list[dict[str, Any]] = []
    system_appended = False

    for m in req.messages:
        msg_dict: dict[str, Any] = {"role": m.role}
        if m.name:
            msg_dict["name"] = m.name
        if m.tool_calls:
            msg_dict["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            msg_dict["tool_call_id"] = m.tool_call_id
        if m.function_call:
            msg_dict["function_call"] = m.function_call

        if m.role == "system" and extra_system_parts and not system_appended:
            if isinstance(m.content, str):
                msg_dict["content"] = m.content + "\n\n" + "\n\n".join(extra_system_parts)
            elif isinstance(m.content, list):
                msg_dict["content"] = m.content + [{"type": "text", "text": "\n\n".join(extra_system_parts)}]
            else:
                msg_dict["content"] = "\n\n".join(extra_system_parts)
            system_appended = True
        else:
            msg_dict["content"] = m.content

        out_messages.append(msg_dict)

    if extra_system_parts and not system_appended:
        out_messages.insert(0, {"role": "system", "content": "\n\n".join(extra_system_parts)})

    # 4. Prepare LiteLLM payload with all client parameters
    trace_id = str(uuid.uuid4())
    user_id = "default-user"
    if isinstance(req.user, dict):
        user_id = str(req.user.get("email") or req.user.get("id") or req.user.get("name") or "default-user")
    elif isinstance(req.user, str) and req.user:
        user_id = req.user

    payload: dict[str, Any] = {
        "model": target_model,
        "messages": out_messages,
        "stream": req.stream,
        "metadata": {
            "trace_id": trace_id,
            "user_id": user_id,
            "routing_reason": reason,
            "is_criticism": is_criticism,
            "lessons_injected": lessons_injected,
            "rag_chunks_injected": rag_chunks_injected,
            "is_background_task": is_background,
            "source": "lunagpt_core_openai_compat",
        },
    }

    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.top_p is not None:
        payload["top_p"] = req.top_p
    max_toks = req.max_tokens or req.max_completion_tokens
    if max_toks is not None:
        payload["max_tokens"] = max_toks
    if req.tools is not None:
        payload["tools"] = req.tools
    if req.tool_choice is not None:
        payload["tool_choice"] = req.tool_choice
    if req.stop is not None:
        payload["stop"] = req.stop
    if req.seed is not None:
        payload["seed"] = req.seed

    # If user is passed as a dict, serialize to string for LiteLLM
    if req.user:
        payload["user"] = json.dumps(req.user) if isinstance(req.user, dict) else str(req.user)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.litellm_master_key}",
    }

    # 5. Handle streaming vs non-streaming
    if req.stream:

        async def stream_generator() -> AsyncGenerator[bytes, None]:
            async with (
                httpx.AsyncClient(timeout=180.0) as client,
                client.stream("POST", settings.litellm_endpoint, json=payload, headers=headers) as response,
            ):
                response.raise_for_status()
                async for chunk in response.aiter_raw():
                    yield chunk

        return None, stream_generator()

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(settings.litellm_endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json(), None
