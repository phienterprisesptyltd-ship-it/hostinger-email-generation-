"""Adapter registry.

Adapters are looked up by name so that swapping UI-assisted capture for the
Enterprise Compliance API is a one-word change at the command line.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import AdapterError
from .base import (  # noqa: F401  (re-exported for adapter authors)
    CapturedAttachment,
    CapturedConversation,
    CapturedMessage,
    ImportAdapter,
    ImportPayload,
)

_REGISTRY: dict = {}


def register(adapter: ImportAdapter) -> ImportAdapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def get(name: str) -> ImportAdapter:
    if name not in _REGISTRY:
        raise AdapterError(
            "unknown adapter %r (available: %s)" % (name, ", ".join(sorted(_REGISTRY)))
        )
    return _REGISTRY[name]


def available() -> list:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def detect(path: Path) -> ImportAdapter:
    """Pick the adapter that recognises ``path``."""
    path = Path(path)
    for adapter in available():
        try:
            if adapter.sniff(path):
                return adapter
        except Exception:  # a sniff must never break detection
            continue
    raise AdapterError(
        "no adapter recognised %s; pass --adapter explicitly (available: %s)"
        % (path, ", ".join(sorted(_REGISTRY)))
    )


def _bootstrap() -> None:
    from . import chatgpt_export, ui_capture, compliance_api  # noqa: F401


_bootstrap()
