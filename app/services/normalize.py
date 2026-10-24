"""Input cleaning for untrusted lead fields: names, US phone numbers and free text.

Names arrive from public forms and ad platforms and are later read back to the lead
(texts, emails, voice). They must be one plain line, look like a human name, and never
carry links, emoji spam or invisible characters.
"""

from __future__ import annotations

import re
import unicodedata

NAME_MAX = 80
MESSAGE_MAX = 1000

_URLISH_RE = re.compile(r"(https?://|www\.|\.(com|net|org|io|ru|xyz|info|biz)\b)", re.IGNORECASE)
_ZERO_WIDTH_BREAKS = "\u200b\u2060\ufeff"
_NAME_PUNCT = set("'\u2019\u02bc-\u2010\u2011., ")
_JUNK_NAMES = {"test", "asdf", "qwerty", "none", "na", "n/a", "null", "xxx", "aaa"}


class InvalidInputError(ValueError):
    """Raised when a field cannot be cleaned into something safe to store."""


def _is_space(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch) in ("Zs", "Zl", "Zp")


def collapse_text(raw: str, *, keep_newlines: bool = False) -> str:
    """NFC-normalize, turn every Unicode space into a plain space, drop control/format chars."""
    text = unicodedata.normalize("NFC", raw)
    out: list[str] = []
    for ch in text:
        if keep_newlines and ch == "\n":
            out.append("\n")
        elif _is_space(ch):
            out.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            out.append(" " if ch in _ZERO_WIDTH_BREAKS else "")
        else:
            out.append(ch)
    joined = "".join(out)
    if keep_newlines:
        lines = [" ".join(line.split()) for line in joined.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return " ".join(joined.split())


def clean_name(raw: str) -> str:
    """Return a cleaned full name or raise InvalidInputError.

    Allowed: letters (any script, accents included), spaces, apostrophes, hyphens, periods
    and commas. Rejected: URLs, emails, digits, emoji/symbols, keyboard mash, overlong input.
    """
    name = collapse_text(raw)
    if not name:
        raise InvalidInputError("name is required")
    if len(name) > NAME_MAX:
        raise InvalidInputError(f"name must be at most {NAME_MAX} characters")
    if _URLISH_RE.search(name) or "@" in name:
        raise InvalidInputError("name must not contain links or email addresses")
    letters = 0
    for ch in name:
        if ch.isalpha():
            letters += 1
        elif unicodedata.category(ch) in ("Mn", "Mc"):
            continue
        elif ch not in _NAME_PUNCT:
            raise InvalidInputError("name contains characters that are not allowed")
    if letters < 2:
        raise InvalidInputError("name must contain at least two letters")
    if name.lower() in _JUNK_NAMES or re.search(r"(.)\1{3,}", name.lower()):
        raise InvalidInputError("name does not look like a real name")
    return name


def first_name(full_name: str, fallback: str = "there") -> str:
    """First word if it has a real-name shape, else ``fallback`` (for greetings)."""
    word = full_name.split(" ")[0].strip(".,") if full_name else ""
    if 1 < len(word) <= 30 and word[0].isalpha() and word[-1].isalpha():
        return word
    return fallback


def normalize_us_phone(raw: str) -> str:
    """Normalize a US/NANP number to E.164 (+1XXXXXXXXXX) or raise InvalidInputError."""
    text = raw.strip()
    if not re.fullmatch(r"[+\d\s().\-]{7,25}", text):
        raise InvalidInputError("phone contains invalid characters")
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise InvalidInputError("phone must be a 10-digit US number")
    area, exchange = digits[:3], digits[3:6]
    if area[0] in "01" or exchange[0] in "01":
        raise InvalidInputError("phone is not a valid US number")
    if area[1:] == "11":
        raise InvalidInputError("phone area code is not valid")
    return f"+1{digits}"


def clean_message(raw: str) -> str:
    message = collapse_text(raw, keep_newlines=True)
    if len(message) > MESSAGE_MAX:
        raise InvalidInputError(f"message must be at most {MESSAGE_MAX} characters")
    return message
