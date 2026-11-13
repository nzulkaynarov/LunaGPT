"""What Luna keeps of each person's conversation between messages: the last turns, the request rate, and a photo
with the question that goes with it. All in memory; only the turns are also saved through Memory."""

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable

from lunagpt.assistant.memories import Memory

logger = logging.getLogger("lunagpt.assistant.dialogue")

HISTORY_MESSAGES = 8
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60
# Telegram sends "what cat is this?" and the photo as two messages, in either order: a question a minute
# before a captionless photo becomes its caption, and the photo stays attached to follow-up questions.
QUESTION_BEFORE_PHOTO_SECONDS = 60.0
PHOTO_KEPT_SECONDS = 600.0


class Dialogue:
    def __init__(self, memory: Callable[[], Memory | None]) -> None:
        self._memory = memory
        self.history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_MESSAGES))
        self._loaded: set[int] = set()
        self.request_times: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=RATE_LIMIT))
        self._last_question: dict[int, tuple[str, float]] = {}
        self._last_photo: dict[int, tuple[bytes, str, float]] = {}  # never stored

    def forget(self, user_id: int) -> None:
        self.history[user_id].clear()

    async def turns(self, user_id: int) -> list[dict[str, str]]:
        """The last turns; after a restart they come back from the database once."""
        memory = self._memory()
        if memory and user_id not in self._loaded:
            self._loaded.add(user_id)
            try:
                self.history[user_id].extend(await memory.load_history(user_id, HISTORY_MESSAGES))
            except Exception:
                logger.warning("could not load the conversation", exc_info=True)
        return list(self.history[user_id])

    async def add(self, user_id: int, said: str, reply: str) -> None:
        """One exchange, kept here and saved; the caller has loaded the stored turns first."""
        self.history[user_id].append({"role": "user", "content": said})
        self.history[user_id].append({"role": "assistant", "content": reply})
        memory = self._memory()
        if memory:
            await memory.save_exchange(user_id, said, reply, HISTORY_MESSAGES)

    def over_rate_limit(self, user_id: int) -> bool:
        times = self.request_times[user_id]
        return len(times) == RATE_LIMIT and time.monotonic() - times[0] < RATE_WINDOW_SECONDS

    def allow_request(self, user_id: int) -> bool:
        if self.over_rate_limit(user_id):
            return False
        self.request_times[user_id].append(time.monotonic())
        return True

    def note_question(self, user_id: int, text: str) -> None:
        self._last_question[user_id] = (text, time.monotonic())

    def recent_question(self, user_id: int) -> str | None:
        """The text sent just before a captionless photo, which is usually a question about it."""
        said, at = self._last_question.pop(user_id, ("", 0.0))
        return said if said and time.monotonic() - at < QUESTION_BEFORE_PHOTO_SECONDS else None

    def keep_photo(self, user_id: int, image: bytes, image_type: str) -> None:
        self._last_question.pop(user_id, None)
        self._last_photo[user_id] = (image, image_type, time.monotonic())

    def recent_photo(self, user_id: int) -> tuple[bytes, str] | None:
        kept = self._last_photo.get(user_id)
        if kept is None:
            return None
        if time.monotonic() - kept[2] >= PHOTO_KEPT_SECONDS:
            del self._last_photo[user_id]
            return None
        return kept[0], kept[1]
