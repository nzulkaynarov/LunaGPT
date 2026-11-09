"""Create the first owner from the host: python -m lunagpt.assistant.add_owner TELEGRAM_ID Name."""

import asyncio
import sys

from lunagpt.db import async_session_factory
from lunagpt.models import AssistantMember


async def main(telegram_id: int, name: str) -> None:
    async with async_session_factory() as db:
        db.add(AssistantMember(telegram_id=telegram_id, name=name, role="owner"))
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), " ".join(sys.argv[2:])))
