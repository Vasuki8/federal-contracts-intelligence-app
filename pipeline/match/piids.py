"""Find contract numbers (PIIDs) cited in notice text, and dollar amounts stated in it."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pipeline.normalize import normalize_contract_id

# Letter/digit runs, optionally joined by dashes ("W91QUZ-21-C-0001", "GS-35F-0119Y").
_TOKEN = re.compile(r"(?<![A-Z0-9])[A-Z0-9]+(?:-[A-Z0-9]+)*(?![A-Z0-9])")
MIN_LENGTH = 8
MAX_LENGTH = 20
MIN_DIGITS = 4

_AMOUNT = re.compile(
    r"\$\s?(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>billion|million|thousand|bn|mm|[bmk])?\b",
    re.IGNORECASE,
)
_UNITS = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6}
_UNITS |= {"thousand": 1e3, "k": 1e3}
MIN_STATED_VALUE = 10_000.0


@dataclass(frozen=True)
class Citation:
    piid_norm: str
    raw: str
    start: int
    end: int


def looks_like_contract_number(token: str) -> bool:
    """8-20 characters with at least 4 digits and a letter: PIIDs and solicitation numbers,
    but not dates, phone numbers or long hex ids."""
    if not MIN_LENGTH <= len(token) <= MAX_LENGTH:
        return False
    digits = sum(char.isdigit() for char in token)
    return digits >= MIN_DIGITS and any(char.isalpha() for char in token)


def find_contract_numbers(text: str | None, exclude: Iterable[str | None] = ()) -> list[Citation]:
    """Each distinct contract-number-like token in `text`, normalized like `piid_norm`.
    `exclude` drops the notice's own solicitation number."""
    if not text:
        return []
    excluded = {norm for value in exclude if (norm := normalize_contract_id(value))}
    seen: set[str] = set()
    citations = []
    for match in _TOKEN.finditer(text.upper()):
        norm = normalize_contract_id(match.group())
        if norm is None or norm in excluded or norm in seen:
            continue
        if not looks_like_contract_number(norm):
            continue
        seen.add(norm)
        citations.append(Citation(norm, text[match.start() : match.end()], *match.span()))
    return citations


def context_words(
    text: str, citation: Citation, words: Sequence[str], chars: int
) -> tuple[str, ...]:
    """Which of `words` appear within `chars` characters of the citation."""
    window = text[max(0, citation.start - chars) : citation.end + chars].lower()
    return tuple(word for word in words if word.lower() in window)


def stated_amounts(text: str | None) -> tuple[float, ...]:
    """Dollar amounts written in the text ("$1,250,000", "$2.5 million"), at least $10,000."""
    if not text:
        return ()
    amounts = []
    for match in _AMOUNT.finditer(text):
        value = float(match.group("number").replace(",", ""))
        unit = (match.group("unit") or "").lower()
        value *= _UNITS.get(unit, 1.0)
        if value >= MIN_STATED_VALUE:
            amounts.append(value)
    return tuple(dict.fromkeys(amounts))
