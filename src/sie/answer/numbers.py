"""Finding numbers in text, and showing a figure the one way it may be shown."""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

from sie.answer.models import Figure

# {F:id} is the only way a figure enters text.
PLACEHOLDER = re.compile(r"\{F:([A-Za-z][A-Za-z0-9_.-]{0,63})\}")

_DIGITS = re.compile(
    r"\d+(?:[.,]\d+)*"
)  # \d is Unicode-aware: Arabic-Indic and Devanagari digits count
# English number words, ordinals and multiplier words. Other languages: digits and Unicode numeric
# characters are still caught, number words are not (stated limit in docs/ANSWER_CONTRACT_DESIGN.md).
_WORDS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
    "hundred",
    "thousand",
    "million",
    "billion",
    "trillion",
    "dozen",
    "half",
    "twice",
    "double",
    "triple",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
]
_WORD = re.compile(r"(?<!\w)(?:" + "|".join(_WORDS) + r")(?!\w)", re.IGNORECASE)


def numeric_tokens(text: str) -> list[str]:
    """Every digit run, number word and other numeric character in ``text``, in order.

    Callers remove the ``{F:id}`` placeholders first: what is left is a number somebody typed.
    """
    found: list[tuple[int, str]] = []
    for m in _DIGITS.finditer(text):
        found.append((m.start(), m.group(0)))
    for m in _WORD.finditer(text):
        found.append((m.start(), m.group(0)))
    for i, ch in enumerate(text):  # fractions, superscripts, roman numerals, CJK numerals
        if not ch.isdecimal() and unicodedata.numeric(ch, None) is not None:
            found.append((i, ch))
    return [tok for _, tok in sorted(found)]


def format_figure(fig: Figure) -> str:
    """Deterministic display: half-up rounding, comma thousands, ASCII minus, unit after the number."""
    quantum = Decimal(1).scaleb(-fig.decimals)
    shown = fig.value.quantize(quantum, rounding=ROUND_HALF_UP)
    if shown == 0:
        shown = abs(shown)  # no "-0"
    text = f"{shown:,.{fig.decimals}f}"
    if not fig.unit:
        return text
    return text + fig.unit if fig.unit == "%" else f"{text} {fig.unit}"
