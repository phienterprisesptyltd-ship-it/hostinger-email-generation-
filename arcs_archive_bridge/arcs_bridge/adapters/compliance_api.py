"""Adapter for the OpenAI Enterprise Compliance API export.

This is the intended *replacement* for UI-assisted capture.  The Compliance API
returns conversations for a workspace as newline-delimited JSON, with the
provider's own identifiers and timestamps - strictly better provenance than
anything a browser can observe.

The adapter reads a file that has already been fetched.  Fetching is left to a
separate, explicitly-invoked step for two reasons: the archive process itself is
network-sealed (:mod:`arcs_bridge.netguard`), and a compliance credential must
live in the operator's own tooling, never inside the archive.  Point this
adapter at the file that tooling produced.

Accepted shapes:

* NDJSON, one conversation object per line (the Compliance API's own form);
* a JSON array of conversation objects;
* an object with a ``data`` or ``conversations`` array.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..jsonspans import array_spans
from ..util import to_iso, utcnow
from . import register
from .base import ImportAdapter, ImportPayload
from .chatgpt_export import parse_conversation as parse_export_conversation


def _line_spans(data: bytes):
    """Byte span of every non-empty line, so NDJSON records stay verbatim."""
    start = 0
    for line in data.splitlines(keepends=True):
        end = start + len(line)
        stripped = line.strip()
        if stripped:
            offset = len(line) - len(line.lstrip())
            yield start + offset, start + offset + len(stripped), stripped
        start = end


class ComplianceApiAdapter(ImportAdapter):
    name = "compliance_api"
    version = "1"
    source_method = "enterprise_compliance_api"
    description = (
        "OpenAI Enterprise Compliance API export (NDJSON or JSON array), "
        "fetched out-of-band by operator tooling."
    )

    def sniff(self, path: Path) -> bool:
        path = Path(path)
        if path.is_dir():
            return False
        if path.suffix.lower() not in (".ndjson", ".jsonl"):
            return False
        head = path.read_bytes()[:4096].decode("utf-8", "replace")
        return '"mapping"' in head or '"conversation_id"' in head

    def read(self, path: Path) -> ImportPayload:
        path = Path(path)
        data = path.read_bytes()
        text = data.decode("utf-8")
        warnings: list = []
        conversations: list = []

        if path.suffix.lower() in (".ndjson", ".jsonl"):
            for start, end, chunk in _line_spans(data):
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError as exc:
                    warnings.append("unparsable NDJSON line at byte %d: %s" % (start, exc))
                    continue
                if not isinstance(obj, dict):
                    warnings.append("skipped non-object NDJSON line at byte %d" % start)
                    continue
                conversations.append(
                    parse_export_conversation(
                        obj, raw_bytes=data[start:end], byte_start=start,
                        byte_end=end, verbatim=True,
                    )
                )
        else:
            key = None
            probe = json.loads(text)
            if isinstance(probe, dict):
                key = "data" if "data" in probe else "conversations"
            for span in array_spans(text, key=key):
                if not isinstance(span.value, dict):
                    warnings.append("skipped non-object element at index %d" % span.index)
                    continue
                conversations.append(
                    parse_export_conversation(
                        span.value, raw_bytes=data[span.byte_start:span.byte_end],
                        byte_start=span.byte_start, byte_end=span.byte_end, verbatim=True,
                    )
                )

        try:
            extraction_date = to_iso(path.stat().st_mtime) or utcnow()
        except OSError:  # pragma: no cover - defensive
            extraction_date = utcnow()

        return ImportPayload(
            container_bytes=data,
            container_media_type="application/x-ndjson"
            if path.suffix.lower() in (".ndjson", ".jsonl") else "application/json",
            conversations=conversations,
            extraction_date=extraction_date,
            source_method=self.source_method,
            input_path=str(path),
            metadata={"conversation_count": len(conversations)},
            warnings=warnings,
        )


register(ComplianceApiAdapter())
