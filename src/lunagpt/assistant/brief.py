"""Morning brief: the day, the weather in the person's city and today's reminders, sent first by Luna.

Weather comes from Open-Meteo (free, no key, only the city's coordinates leave the house), not
from a model, so the numbers are the forecast's and not a paraphrase of a search snippet."""

import logging
from dataclasses import dataclass
from datetime import date, datetime

import httpx

logger = logging.getLogger("lunagpt.assistant.brief")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 10.0
BRIEF_TIMES = ("06:30", "07:00", "07:30", "08:00", "09:00")
# A brief later than this after its time is stale: the PC was off in the morning.
LATE_LIMIT_HOURS = 3
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
DAY_NAMES = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
# WMO weather interpretation codes, as documented by Open-Meteo.
WEATHER = {
    0: "ясно ☀️",
    1: "в основном ясно 🌤",
    2: "переменная облачность ⛅",
    3: "пасмурно ☁️",
    45: "туман 🌫",
    48: "изморозь 🌫",
    51: "морось 🌦",
    53: "морось 🌦",
    55: "сильная морось 🌧",
    56: "ледяная морось 🌧",
    57: "ледяная морось 🌧",
    61: "небольшой дождь 🌦",
    63: "дождь 🌧",
    65: "сильный дождь 🌧",
    66: "ледяной дождь 🌧",
    67: "ледяной дождь 🌧",
    71: "небольшой снег 🌨",
    73: "снег 🌨",
    75: "сильный снег ❄️",
    77: "снежная крупа 🌨",
    80: "ливни 🌦",
    81: "ливни 🌧",
    82: "сильные ливни ⛈",
    85: "снегопад 🌨",
    86: "сильный снегопад ❄️",
    95: "гроза ⛈",
    96: "гроза с градом ⛈",
    99: "гроза с градом ⛈",
}


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Forecast:
    low: float
    high: float
    rain_chance: int | None
    code: int


async def find_city(name: str, transport: httpx.AsyncBaseTransport | None = None) -> Place | None:
    """The best match for a city name, or None when there is none or the service is down."""
    params: dict[str, str | int] = {"name": name.strip()[:100], "count": 1, "language": "ru", "format": "json"}
    try:
        async with httpx.AsyncClient(transport=transport) as http:
            response = await http.get(GEOCODE_URL, params=params, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            results = response.json().get("results") or []
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("city lookup failed: %s", type(exc).__name__)
        return None
    if not results:
        return None
    first = results[0]
    return Place(str(first["name"]), float(first["latitude"]), float(first["longitude"]))


async def forecast(lat: float, lon: float, transport: httpx.AsyncBaseTransport | None = None) -> Forecast | None:
    params: dict[str, str | float | int] = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "timezone": "auto",
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(transport=transport) as http:
            response = await http.get(FORECAST_URL, params=params, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            daily = response.json()["daily"]
        chance = daily["precipitation_probability_max"][0]
        return Forecast(
            low=float(daily["temperature_2m_min"][0]),
            high=float(daily["temperature_2m_max"][0]),
            rain_chance=None if chance is None else int(chance),
            code=int(daily["weather_code"][0]),
        )
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("forecast failed: %s", type(exc).__name__)
        return None


def weather_line(city: str, weather: Forecast) -> str:
    sky = WEATHER.get(weather.code, "")
    line = f"🌡 {city}: от {round(weather.low)}° до {round(weather.high)}°"
    if sky:
        line += f", {sky}"
    if weather.rain_chance is not None and weather.rain_chance >= 30:
        line += f". Осадки {weather.rain_chance}%, возьми зонт ☂️"
    return line + "."


def date_line(day: date) -> str:
    return f"Сегодня {DAY_NAMES[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}."


def compose(name: str, day: date, weather: str | None, today: list[tuple[datetime, str]]) -> str:
    """The brief text; `today` holds this day's reminders as (local time, text)."""
    lines = [f"☀️ Доброе утро, {name}! {date_line(day)}"]
    if weather:
        lines.append(weather)
    if today:
        lines.append("\n🔔 Сегодня:")
        lines += [f"• {moment:%H:%M} — {text or 'напоминание'}" for moment, text in today]
    else:
        lines.append("\nНапоминаний на сегодня нет. Скажи «напомни…», и я напомню.")
    return "\n".join(lines)


def is_due(brief_time: str | None, sent_on: date | None, now: datetime) -> bool:
    """Whether the brief should go out now: its time has come today, it was not sent, and it is not stale."""
    if not brief_time or sent_on == now.date():
        return False
    hour, minute = (int(part) for part in brief_time.split(":"))
    planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return planned <= now and (now - planned).total_seconds() < LATE_LIMIT_HOURS * 3600
