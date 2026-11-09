"""Client for the HomeSec network API on the Raspberry Pi."""

from typing import Any
from urllib.parse import quote

import httpx

from lunagpt.config import settings


class PiUnavailableError(Exception):
    """The Pi did not answer; network features are temporarily off."""


class PiConflictError(Exception):
    """The Pi refused because of its own rules, such as a pending request."""


class PiNotFoundError(Exception):
    """The Pi does not know this person or request."""


class PiClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.luna_pi_api_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.luna_pi_api_token}"},
            timeout=10.0,
            transport=transport,
        )

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise PiUnavailableError(type(exc).__name__) from exc
        if response.status_code == 404:
            raise PiNotFoundError(path)
        if response.status_code == 409:
            raise PiConflictError(path)
        if response.status_code >= 300:  # includes the panel's login redirect
            raise PiUnavailableError(str(response.status_code))
        try:
            return response.json()
        except ValueError as exc:
            raise PiUnavailableError("invalid response") from exc

    @staticmethod
    def _person(name: str) -> str:
        return f"people/{quote(name, safe='')}"

    async def is_healthy(self) -> bool:
        """Any answer below 500 means the Pi is up, even during a partial rollout."""
        try:
            response = await self._client.get("health")
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    async def heartbeat(self, member_chat_ids: list[int]) -> None:
        await self._request("POST", "heartbeat", {"member_chat_ids": member_chat_ids})

    async def people(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "people")
        return result

    async def link_telegram(self, person_id: int, telegram_id: int) -> None:
        await self._request("PUT", f"people/{person_id}/telegram", {"telegram_id": telegram_id})

    async def shared_devices(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "shared-devices")
        return result

    async def set_shared_state(self, device_id: int, choice: str) -> dict[str, Any]:
        """Apply a menu choice: off, kid, m (adults until morning), or a number of hours for adults."""
        body: dict[str, Any] = {"state": choice} if choice in ("off", "kid") else {"state": "adult"}
        if choice == "m":
            body["until_morning"] = True
        elif choice not in ("off", "kid"):
            body["hours"] = float(choice)
        result: dict[str, Any] = await self._request("POST", f"shared-devices/{device_id}/state", body)
        return result

    async def device_triage(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "device-triage")
        return result

    async def sort_devices(self, device_id: int, destination: str) -> dict[str, Any]:
        """Apply a menu choice: "shared", "iot", or a person's id."""
        body: dict[str, Any] = (
            {"kind": destination} if destination in ("shared", "iot") else {"person_id": int(destination)}
        )
        result: dict[str, Any] = await self._request("POST", f"device-triage/{device_id}", body)
        return result

    async def quota(self, name: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", f"{self._person(name)}/quota")
        return result

    async def request_time(self, name: str, reason: str) -> int:
        result = await self._request("POST", f"{self._person(name)}/time-requests", {"reason": reason})
        return int(result["id"])

    async def time_request_status(self, request_id: int) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("GET", f"time-requests/{request_id}")
        return result

    async def quiz_available(self, name: str) -> bool:
        return bool((await self._request("GET", f"{self._person(name)}/quiz"))["available"])

    async def quiz_result(self, name: str, correct: bool, topic: str) -> int:
        body = {"correct": correct, "topic": topic}
        return int((await self._request("POST", f"{self._person(name)}/quiz-result", body))["awarded_minutes"])
