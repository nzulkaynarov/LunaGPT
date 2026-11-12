"""What Luna knows about herself: who she is, what she can do for this person, what is new, and playful
sample answers. The text lives in about.md next to this module and goes into the prompt only when the
question is about Luna."""

import re
from functools import cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.models import AssistantMember

ABOUT_FILE = Path(__file__).with_name("about.md")
SECTIONS_BY_ROLE = {
    "owner": ("all", "family", "adult"),
    "adult": ("all", "family", "adult"),
    "kid": ("all", "family", "kid"),
    "guest": ("all", "guest"),
}
UNKNOWN_OWNER = "хозяин дома"
ABOUT_QUESTION = re.compile(
    r"\bкто\s+ты\b|\bты\s+кто\b|что\s+ты\s+(?:умеешь|можешь|знаешь\s+о\s+себе)|расскажи\s+(?:о|про)\s+себя"
    r"|кто\s+(?:тебя|твой)\s+(?:создал\w*|сделал\w*|придумал\w*|написал\w*|хозяин|папа|создатель|автор)"
    r"|как\s+тебя\s+зовут|сколько\s+тебе\s+лет|где\s+ты\s+живёшь|где\s+ты\s+живешь|ты\s+спишь"
    r"|\bты\s+(?:робот|бот|человек|живая|настоящая|кошка|нейросеть|ии|chatgpt|чатгпт|gpt)\b"
    r"|что\s+(?:у\s+тебя\s+)?нового|как(?:ие|ая)\s+у\s+тебя\s+(?:функци\w*|возможност\w*|модел\w*)"
    r"|на\s+какой\s+модели|как\s+ты\s+устроена|ты\s+меня\s+любишь|ты\s+умнее"
    r"|(?:истори\w+|случа\w+)\s+(?:про|о|из)\s+(?:себя|тебя|своей)|про\s+луну|о\s+луне|\bты\s+помнишь\b"
    r"|твои\s+повадки|смешн\w+\s+(?:истори\w+|случа\w+)"
    r"|\bwho\s+are\s+you\b|what\s+can\s+you\s+do|who\s+(?:made|created|built)\s+you",
    re.IGNORECASE,
)


def is_about_question(text: str) -> bool:
    return bool(ABOUT_QUESTION.search(text))


@cache
def _sections() -> list[tuple[str, str]]:
    """(section, text) in file order; a section name may repeat."""
    text = re.sub(r"<!--.*?-->", "", ABOUT_FILE.read_text(encoding="utf-8"), flags=re.DOTALL)
    found = []
    for block in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        name, _, body = block.partition("\n")
        found.append((name.strip(), body.strip()))
    return found


def about_luna(role: str, owner: str | None) -> str:
    """The parts of about.md this person may read, with the owner's name filled in."""
    allowed = SECTIONS_BY_ROLE.get(role, SECTIONS_BY_ROLE["guest"])
    parts = [body for name, body in _sections() if name in allowed]
    return "\n\n".join(parts).replace("{owner}", owner or UNKNOWN_OWNER)


async def owner_name(db: AsyncSession) -> str | None:
    name: str | None = await db.scalar(
        select(AssistantMember.name).where(AssistantMember.role == "owner").order_by(AssistantMember.created_at)
    )
    return name
