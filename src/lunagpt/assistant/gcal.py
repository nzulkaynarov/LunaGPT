"""Google Calendar through a service account that each person shares their calendar with.

Google's device sign-in flow does not allow Calendar scopes and Luna has no public address for the web flow,
so the owner creates one service account; a person shares their calendar with its email ("Make changes to
events") and tells Luna the calendar address. Tokens come from google-auth."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import service_account

API_URL = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
HTTP_TIMEOUT = 15.0
MAX_EVENTS = 50


class CalendarError(Exception):
    """kind: "not_shared" when Google refuses this calendar (or the event is not there), "gone" for a deleted
    event, "unavailable" when Google or the key fails."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class Event:
    title: str
    start: datetime  # local wall-clock time, naive; midnight for all-day events
    end: datetime
    all_day: bool


def _local(value: dict[str, Any], offset: timedelta) -> tuple[datetime, bool]:
    if "date" in value:
        return datetime.combine(date.fromisoformat(value["date"]), datetime.min.time()), True
    moment = datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00"))
    return (moment.astimezone(UTC) + offset).replace(tzinfo=None), False


class GoogleCalendar:
    """Authentication is Google's own google-auth library; the three Calendar REST calls go through httpx."""

    def __init__(
        self,
        credentials: service_account.Credentials,
        utc_offset_hours: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self.email: str = credentials.service_account_email
        self._offset = timedelta(hours=utc_offset_hours)
        self._transport = transport
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def from_json(cls, service_account_json: str, utc_offset_hours: int) -> "GoogleCalendar":
        """Raises ValueError (or KeyError) for a key that is not a service account key."""
        info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])  # type: ignore[no-untyped-call]
        return cls(credentials, utc_offset_hours)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=HTTP_TIMEOUT)

    async def _access_token(self) -> str:
        async with self._refresh_lock:
            if not self._credentials.valid:
                try:
                    # google-auth refreshes synchronously; once an hour in a worker thread is fine.
                    await asyncio.to_thread(self._credentials.refresh, Request())
                except GoogleAuthError as exc:
                    raise CalendarError("unavailable") from exc
            return str(self._credentials.token)

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        try:
            async with self._client() as http:
                response = await http.request(method, f"{API_URL}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise CalendarError("unavailable") from exc
        if response.status_code in (403, 404):
            raise CalendarError("not_shared")
        if response.status_code == 410:
            raise CalendarError("gone")
        if response.status_code >= 300:
            raise CalendarError("unavailable")
        if response.status_code == 204 or not response.content:
            return {}
        result: dict[str, Any] = response.json()
        return result

    def _rfc3339(self, local: datetime) -> str:
        return (local - self._offset).replace(tzinfo=UTC).isoformat()

    async def events(self, calendar_id: str, start: datetime, end: datetime) -> list[Event]:
        """Events between two local moments, repeating ones expanded, in start order."""
        params: dict[str, str | int] = {
            "timeMin": self._rfc3339(start),
            "timeMax": self._rfc3339(end),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": MAX_EVENTS,
        }
        body = await self._call("GET", f"/calendars/{quote(calendar_id, safe='')}/events", params=params)
        found = []
        for item in body.get("items", []):
            if item.get("status") == "cancelled" or "start" not in item:
                continue
            begin, all_day = _local(item["start"], self._offset)
            finish, _ = _local(item.get("end", item["start"]), self._offset)
            found.append(Event(str(item.get("summary") or "(без названия)"), begin, finish, all_day))
        return found

    async def check(self, calendar_id: str) -> None:
        """Raises CalendarError unless the calendar is shared with Luna."""
        now = datetime.now(UTC).replace(tzinfo=None) + self._offset
        await self.events(calendar_id, now, now + timedelta(days=1))

    async def add(self, calendar_id: str, title: str, start: datetime, end: datetime, all_day: bool) -> str:
        """Create an event and return its id."""
        if all_day:
            span = {"start": {"date": start.date().isoformat()}, "end": {"date": end.date().isoformat()}}
        else:
            span = {"start": {"dateTime": self._rfc3339(start)}, "end": {"dateTime": self._rfc3339(end)}}
        body = {"summary": title, **span, "reminders": {"useDefault": True}}
        created = await self._call("POST", f"/calendars/{quote(calendar_id, safe='')}/events", json=body)
        return str(created.get("id", ""))

    async def delete(self, calendar_id: str, event_id: str) -> None:
        """Remove an event; one already gone (deleted by hand, 404 or 410) counts as removed."""
        path = f"/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"
        try:
            await self._call("DELETE", path)
        except CalendarError as exc:
            if exc.kind not in ("not_shared", "gone"):
                raise
