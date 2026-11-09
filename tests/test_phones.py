"""Every way a number can reach us must end in the same canonical form."""

import pytest

from lunagpt.assistant import phones

CANONICAL = "+998901234567"


@pytest.mark.parametrize("raw", ["998901234567", "+998901234567", "+998 90 123-45-67"])
def test_telegram_number_with_or_without_plus(raw: str) -> None:
    assert phones.from_telegram(raw) == CANONICAL


def test_unusual_telegram_number_is_still_kept() -> None:
    assert phones.from_telegram("88812345") == "+88812345"


@pytest.mark.parametrize("raw", ["90 123 45 67", "901234567", "998901234567", "+998 (90) 123-45-67"])
def test_adult_can_type_a_number_any_way(raw: str) -> None:
    assert phones.from_adult(raw) == CANONICAL


def test_foreign_number_without_plus() -> None:
    assert phones.from_adult("79161234567") == "+79161234567"


@pytest.mark.parametrize("raw", ["", "Миша", "123", "позвони мне"])
def test_text_that_is_not_a_number(raw: str) -> None:
    assert phones.from_adult(raw) is None
