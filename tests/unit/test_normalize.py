import pytest

from app.services.normalize import (
    MESSAGE_MAX,
    InvalidInputError,
    clean_message,
    clean_name,
    collapse_text,
    first_name,
    normalize_us_phone,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jordan Alvarez", "Jordan Alvarez"),
        ("  José   García-López ", "José García-López"),
        ("Mary-Kate O'Neil", "Mary-Kate O'Neil"),
        ("Zoë\u00a0Smith", "Zoë Smith"),  # non-breaking space
        ("Ana\u200bLucía", "Ana Lucía"),  # zero-width space acts as a break
        ("J. R. Tolliver, Jr.", "J. R. Tolliver, Jr."),
        ("Line\nBreak", "Line Break"),
        ("Bad\u0007Bell", "BadBell"),  # control chars dropped
    ],
)
def test_clean_name_accepts_real_names(raw: str, expected: str) -> None:
    assert clean_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Visit https://spam.example.com",
        "www.cheap-stuff.example",
        "best deals.xyz",
        "me@example.com",
        "Winner 🎉🎉🎉",
        "Agent 007",
        "J",
        "aaaa",
        "test",
        "x" * 81,
        "<script>alert(1)</script>",
    ],
)
def test_clean_name_rejects_junk(raw: str) -> None:
    with pytest.raises(InvalidInputError):
        clean_name(raw)


@pytest.mark.parametrize(
    "raw",
    ["2025550143", "202-555-0143", "(202) 555-0143", "+1 202.555.0143", "1 202 555 0143"],
)
def test_phone_normalizes_to_e164(raw: str) -> None:
    assert normalize_us_phone(raw) == "+12025550143"


@pytest.mark.parametrize(
    "raw",
    [
        "555-0143",  # too short
        "202-555-01434",  # too long
        "+44 20 7946 0958",  # not US
        "102-555-0143",  # area code starts with 1
        "202-155-0143",  # exchange starts with 1
        "911-555-0143",  # N11 area code
        "call me maybe",
        "202-555-0143 ext 9",
    ],
)
def test_phone_rejects_invalid(raw: str) -> None:
    with pytest.raises(InvalidInputError):
        normalize_us_phone(raw)


def test_message_keeps_paragraphs_and_strips_controls() -> None:
    raw = "Line one\x00\n\n\n\nLine   two\u2028tail"
    assert clean_message(raw) == "Line one\n\nLine two tail"


def test_message_length_limit() -> None:
    clean_message("a" * MESSAGE_MAX)
    with pytest.raises(InvalidInputError):
        clean_message("a" * (MESSAGE_MAX + 1))


def test_collapse_text_single_line() -> None:
    assert collapse_text("a\tb\u3000c") == "a b c"


@pytest.mark.parametrize(
    ("full", "expected"),
    [("Jordan Alvarez", "Jordan"), ("J. R. Smith", "there"), ("", "there"), ("Ana", "Ana")],
)
def test_first_name(full: str, expected: str) -> None:
    assert first_name(full) == expected
