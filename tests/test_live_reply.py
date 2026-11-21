"""The answer shows while it is written, a stop keeps what was written, and the price still counts."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendRichMessageDraft
from aiogram.types import InputRichMessage

from lunagpt.assistant import live_reply, llm
from lunagpt.assistant.waiting import Waiting


def _sse(*chunks: dict[str, Any]) -> bytes:
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks] + ["data: [DONE]"]
    return ("\n\n".join(lines) + "\n\n").encode()


def _delta(**delta: Any) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta}]}


def _client(body: bytes, seen: list[dict[str, Any]] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_stream_grows_the_answer_and_reports_its_price() -> None:
    body = _sse(_delta(content="При"), _delta(content="вет!"), {"choices": [], "usage": {"cost": 0.0021}})
    shown: list[str] = []
    costs: list[float] = []
    seen: list[dict[str, Any]] = []
    async with _client(body, seen) as client:
        message = await llm._stream_chat(client, {"model": "luna-fast"}, costs.append, shown.append)
    assert message == {"role": "assistant", "content": "Привет!"}
    assert shown == ["При", "Привет!"]
    assert costs == [0.0021]
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_a_streamed_tool_call_is_put_together_and_not_shown() -> None:
    first = {"index": 0, "id": "call_1", "function": {"name": "web_search", "arguments": '{"que'}}
    rest = {"index": 0, "function": {"arguments": 'ry": "погода"}'}}
    body = _sse(_delta(tool_calls=[first]), _delta(tool_calls=[rest], content=""))
    shown: list[str] = []
    async with _client(body) as client:
        message = await llm._stream_chat(client, {}, None, shown.append)
    call = message["tool_calls"][0]
    assert call["id"] == "call_1" and call["function"]["name"] == "web_search"
    assert json.loads(call["function"]["arguments"]) == {"query": "погода"}
    assert shown == []


@pytest.mark.asyncio
async def test_an_error_in_the_stream_fails_the_model_so_the_next_one_answers() -> None:
    async with _client(_sse(_delta(content="на"), {"error": {"message": "overloaded"}})) as client:
        with pytest.raises(ValueError):
            await llm._stream_chat(client, {}, None, lambda text: None)


def _bot() -> MagicMock:
    return MagicMock(send_rich_message_draft=AsyncMock(), send_message_draft=AsyncMock())


@pytest.mark.asyncio
async def test_the_draft_shows_the_answer_and_the_answer_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_reply, "DRAFT_EVERY_SECONDS", 0)
    bot = _bot()
    waiting = Waiting()
    live = live_reply.LiveReply(bot, 7, waiting)

    async def answer() -> str:
        live.update("Почти")
        await asyncio.sleep(0.01)
        live.update("Почти готово")
        await asyncio.sleep(0.01)
        return "Почти готово."

    assert await live.run(answer()) == ("Почти готово.", False)
    drafts = [call.args[2].markdown for call in bot.send_rich_message_draft.await_args_list]
    assert drafts == ["Почти", "Почти готово"]
    assert bot.send_rich_message_draft.await_args.kwargs == {"can_stop": True, "keep_on_stop": True}
    assert waiting.answering  # the cat phrases give way to the answer
    assert not live_reply.stop(7, live.draft_id)  # finished: a late stop changes nothing


@pytest.mark.asyncio
async def test_stop_keeps_what_was_written_and_ends_the_wait() -> None:
    live = live_reply.LiveReply(_bot(), 7)
    endless = asyncio.Event()

    async def answer() -> str:
        live.update("Первая мысль")
        await endless.wait()
        return "never"

    async def press() -> None:
        await asyncio.sleep(0.01)
        assert live_reply.stop(7, live.draft_id)

    (reply, stopped), _ = await asyncio.gather(live.run(answer()), press())
    assert (reply, stopped) == ("Первая мысль", True)


@pytest.mark.asyncio
async def test_a_refused_rich_draft_falls_back_to_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_reply, "DRAFT_EVERY_SECONDS", 0)
    bot = _bot()
    method = SendRichMessageDraft(chat_id=7, draft_id=1, rich_message=InputRichMessage(markdown="x"))
    bot.send_rich_message_draft.side_effect = TelegramBadRequest(method, "can't parse")
    live = live_reply.LiveReply(bot, 7)

    async def answer() -> str:
        live.update("**полу")
        await asyncio.sleep(0.01)
        return "**полужирный**"

    await live.run(answer())
    assert bot.send_message_draft.await_args.kwargs["text"] == "**полу"


@pytest.mark.asyncio
async def test_an_action_under_way_finishes_even_when_stopped() -> None:
    finished: list[str] = []

    async def add_item(arguments: dict[str, Any]) -> str:
        await asyncio.sleep(0.02)
        finished.append("молоко")
        return "Добавила молоко"

    performed: list[str] = []
    action = llm._tracked(llm.Action(schema={"function": {"name": "add"}}, run=add_item), performed)
    task = asyncio.create_task(action.run({}))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.03)
    assert finished == ["молоко"]
