"""Phone numbers in one canonical E.164 form, so a number always matches itself."""

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

from lunagpt.config import settings

MIN_SEARCH_DIGITS = 7


def _e164(raw: str, region: str | None) -> str | None:
    try:
        number = phonenumbers.parse(raw, region)
    except NumberParseException:
        return None
    return phonenumbers.format_number(number, PhoneNumberFormat.E164) if phonenumbers.is_valid_number(number) else None


def _digits(raw: str) -> str:
    return "".join(char for char in raw if char.isdigit())


def from_telegram(raw: str) -> str:
    """Telegram sends international numbers, with or without the plus; an unusual one is still kept."""
    candidate = f"+{_digits(raw)}"
    return (_e164(candidate, None) or candidate)[:32]


def from_adult(raw: str) -> str | None:
    """Read a number as an adult types it, local or international; None when it is not a number."""
    text = raw.strip()
    digits = _digits(text)
    if len(digits) < MIN_SEARCH_DIGITS:
        return None
    if text.startswith("+"):
        return _e164(text, None) or f"+{digits}"[:32]
    return _e164(text, settings.luna_phone_region) or _e164(f"+{digits}", None) or f"+{digits}"[:32]
