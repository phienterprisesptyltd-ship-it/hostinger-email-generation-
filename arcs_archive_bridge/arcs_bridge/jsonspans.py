"""Locate the exact byte span of each element of a top-level JSON array.

The official ChatGPT export ships one large ``conversations.json``.  Storing
only the whole file would make per-conversation provenance a claim about a
re-serialisation; storing byte spans instead lets the archive hand back the
*exact original bytes* for a single conversation, and prove it by hash.

If a file cannot be scanned (unexpected shape, malformed JSON) callers fall
back to canonical re-serialisation and record that fact, so the weaker claim is
never silently presented as the stronger one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

_WS = " \t\r\n"
_DECODER = json.JSONDecoder()


@dataclass(frozen=True)
class Span:
    index: int
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    value: object


def find_array_start(text: str, key: "str | None" = None) -> int:
    """Return the index of the '[' that opens the array of interest."""
    i = 0
    n = len(text)
    while i < n and text[i] in _WS:
        i += 1
    if i < n and text[i] == "[":
        return i
    if i < n and text[i] == "{":
        if key is None:
            raise ValueError("top-level value is an object; a key is required")
        needle = json.dumps(key)
        pos = text.find(needle, i)
        while pos != -1:
            j = pos + len(needle)
            while j < n and text[j] in _WS:
                j += 1
            if j < n and text[j] == ":":
                j += 1
                while j < n and text[j] in _WS:
                    j += 1
                if j < n and text[j] == "[":
                    return j
            pos = text.find(needle, pos + 1)
        raise ValueError("no array found under key %r" % key)
    raise ValueError("input is not a JSON array or object")


def iter_array_spans(text: str, start: int = 0):
    """Yield a :class:`Span` per element of the array beginning at ``start``."""
    if text[start] != "[":
        raise ValueError("expected '[' at index %d" % start)
    i = start + 1
    n = len(text)
    index = 0
    # Incremental char -> byte conversion; indices only ever move forward.
    last_char, last_byte = 0, 0

    def byte_of(char_index: int) -> int:
        nonlocal last_char, last_byte
        if char_index < last_char:  # pragma: no cover - defensive
            return len(text[:char_index].encode("utf-8"))
        last_byte += len(text[last_char:char_index].encode("utf-8"))
        last_char = char_index
        return last_byte

    while i < n:
        while i < n and text[i] in _WS:
            i += 1
        if i >= n:
            raise ValueError("unterminated JSON array")
        if text[i] == "]":
            return
        if text[i] == ",":
            i += 1
            continue
        value, end = _DECODER.raw_decode(text, i)
        yield Span(index, i, end, byte_of(i), byte_of(end), value)
        index += 1
        i = end


def array_spans(text: str, key: "str | None" = None) -> list:
    return list(iter_array_spans(text, find_array_start(text, key)))
