"""Small shared helpers: identifiers, timestamps, text handling."""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
import uuid

#: Namespace for deterministic archive identifiers.  Stable across machines and
#: re-imports, which is what makes Obsidian links and evidence packets durable.
ARCS_NAMESPACE = uuid.UUID("6f5f0f6c-4a1e-5a2e-9a54-4152435331ff")


def new_id(prefix: str = "") -> str:
    """A random identifier for events (batches, runs, log entries)."""
    value = uuid.uuid4().hex
    return f"{prefix}_{value}" if prefix else value


def stable_id(*parts: object) -> str:
    """A deterministic identifier derived from its parts (uuid5)."""
    key = "".join("" if p is None else str(p) for p in parts)
    return str(uuid.uuid5(ARCS_NAMESPACE, key))


def utcnow() -> str:
    """Current time as an ISO-8601 UTC string with a trailing Z."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_iso(value) -> "str | None":
    """Best-effort conversion of a source timestamp to ISO-8601 UTC.

    The *original* representation is always retained separately; this is only a
    convenience projection for sorting and display.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            dt = _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return to_iso(float(text))
        except ValueError:
            pass
        try:
            dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        dt = dt.astimezone(_dt.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return None


def date_of(iso):
    return iso[:10] if iso else None


_SLUG_STRIP = re.compile(r"[^a-zA-Z0-9\- ]+")
_SLUG_SPACE = re.compile(r"[\s_]+")


def slugify(text: str, max_length: int = 60) -> str:
    """Filesystem- and Obsidian-safe slug.  Never used as an identity."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _SLUG_STRIP.sub("", text)
    text = _SLUG_SPACE.sub("-", text.strip())
    text = re.sub(r"-{2,}", "-", text).strip("-").lower()
    return (text[:max_length].rstrip("-")) or "untitled"


def short(value: str, n: int = 8) -> str:
    return (value or "")[:n]


def truncate(text: str, limit: int = 160) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
