"""Cut what the customer typed into pieces Jev can choose between.

Jev chooses; it never writes. So a value that only exists in the customer's own words —
an amount, a tenure, an OTP, an IFSC, a name — has to be offered to Jev as an option.
This module only cuts the message into words and runs of words; it never decides what a
piece means. Which piece is the amount, the IFSC or the payee's name — or whether the
customer gave one at all — is Jev's call (slots.py). No patterns, no regex.
"""

from __future__ import annotations

from dataclasses import dataclass

_EDGE = ".,;:!?\"'()[]{}<>"
_CONTEXT = 4       # words shown either side of a piece, so Jev sees how it was used
_MAX_WORDS = 40    # only the last 40 words of a long message are cut into pieces


@dataclass(frozen=True)
class Piece:
    text: str     # as the customer wrote it, minus surrounding punctuation
    context: str  # the words around it, with the piece in [brackets]


def _words(text: str) -> list[str]:
    return (text or "").split()[-_MAX_WORDS:]


def _piece(words: list[str], i: int, j: int) -> Piece | None:
    text = " ".join(words[i:j]).strip(_EDGE)
    if not text:
        return None
    before = words[max(0, i - _CONTEXT):i]
    after = words[j:j + _CONTEXT]
    context = " ".join(
        (["…"] if i > _CONTEXT else []) + before + [f"[{text}]"] + after + (["…"] if j + _CONTEXT < len(words) else [])
    )
    return Piece(text, context)


def _unique(pieces: list[Piece | None]) -> list[Piece]:
    seen: set[str] = set()
    out = []
    for p in pieces:
        if p and p.text.lower() not in seen:
            seen.add(p.text.lower())
            out.append(p)
    return out


def has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def words(text: str, *, with_digits: bool = False) -> list[Piece]:
    """Every single word; with_digits keeps only those with a digit in them."""
    ws = _words(text)
    return [p for p in _unique([_piece(ws, i, i + 1) for i in range(len(ws))]) if not with_digits or has_digit(p.text)]


def runs(text: str, longest: int) -> list[Piece]:
    """Every run of 1..longest consecutive words ('Arjun', 'Arjun Rao', 'Tata Power' …)."""
    ws = _words(text)
    return _unique([_piece(ws, i, j) for i in range(len(ws)) for j in range(i + 1, min(i + longest, len(ws)) + 1)])


def tails(text: str, shortest: int = 2) -> list[Piece]:
    """Everything from each word to the end — where an address or a free value starts."""
    ws = _words(text)
    return _unique([_piece(ws, i, len(ws)) for i in range(len(ws) - shortest + 1)])


def trim(text: str) -> str:
    return (text or "").strip().strip(_EDGE)


def digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def number(text: str) -> float | int | None:
    """The digits of a piece as a number: '₹5,000' -> 5000, '10th' -> 10, '1.5' -> 1.5.
    Any unit or scale written around it (lakh, years) is a separate Jev question."""
    kept, dot = [], False
    for ch in text:
        if ch.isdigit():
            kept.append(ch)
        elif ch == "." and not dot and kept:
            kept.append(ch)
            dot = True
    s = "".join(kept).rstrip(".")
    if not s:
        return None
    n = float(s)
    return int(n) if n.is_integer() else n
