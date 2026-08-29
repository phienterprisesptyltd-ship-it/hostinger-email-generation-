"""Adapter for UI-assisted capture bundles.

How the capture works, and what it deliberately does not do:

* The user opens ChatGPT in their normal browser, already signed in, and runs
  ``browser_capture/arcs-ui-capture.user.js``.
* The script issues **GET requests only**, same-origin, and lets the browser
  attach the existing session the way it does for any other page request.  It
  never reads ``document.cookie``, never copies an ``Authorization`` header,
  and never writes anything back to ChatGPT.
* The script saves a JSON bundle to the user's Downloads folder.  That file is
  the only thing this adapter ever reads.

So the archive uses the authenticated session without ever holding the
credential, and ChatGPT is strictly read-only.  Every bundle is scanned for
credential material at the ingestion boundary before a single byte is stored
(see :mod:`arcs_bridge.credentials`).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..hashing import canonical_json
from ..jsonspans import array_spans
from ..util import utcnow
from . import register
from .base import (
    CapturedAttachment,
    CapturedConversation,
    CapturedMessage,
    ImportAdapter,
    ImportPayload,
)
from .chatgpt_export import parse_conversation as parse_export_conversation

BUNDLE_VERSION = 1


def _attachments(entry: dict) -> list:
    out = []
    for i, att in enumerate(entry.get("attachments") or []):
        if isinstance(att, dict):
            out.append(
                CapturedAttachment(
                    kind=att.get("kind") or "file",
                    name=att.get("name"),
                    mime_type=att.get("mime_type"),
                    byte_size=att.get("size"),
                    source_uri=att.get("url"),
                    source_file_id=att.get("file_id"),
                    metadata={"index": i, "attachment": att},
                )
            )
        elif isinstance(att, str):
            out.append(CapturedAttachment(kind="link", source_uri=att,
                                          metadata={"index": i}))
    return out


def parse_dom_conversation(entry: dict, raw_bytes: bytes, byte_start=None,
                           byte_end=None, verbatim: bool = True) -> CapturedConversation:
    """Build a conversation from DOM-scraped messages.

    Lower fidelity than a backend payload - the DOM has already rendered
    markdown and dropped metadata - so every original field of the entry is
    retained and the loss is stated rather than hidden.
    """
    messages = []
    for i, msg in enumerate(entry.get("messages") or []):
        messages.append(
            CapturedMessage(
                source_message_id=msg.get("message_id") or msg.get("id"),
                role=msg.get("role") or "unknown",
                author_name=msg.get("author_name"),
                content_text=msg.get("text") or "",
                content_type=msg.get("content_type") or "text",
                content_parts=[msg.get("text") or ""],
                seq=int(msg.get("order", i + 1)),
                create_time_raw=msg.get("timestamp"),
                on_canonical_path=True,
                branch_note=None,
                metadata={"entry_index": i, "message": msg},
                attachments=_attachments(msg),
            )
        )
    envelope = {k: v for k, v in entry.items() if k != "messages"}
    conv_id = entry.get("conversation_id") or entry.get("id") or "unknown"
    return CapturedConversation(
        source_conversation_id=str(conv_id),
        title=entry.get("title"),
        messages=messages,
        raw_bytes=raw_bytes,
        raw_is_verbatim=verbatim,
        source_uri=entry.get("url") or ("https://chatgpt.com/c/%s" % conv_id),
        create_time_raw=entry.get("create_time"),
        update_time_raw=entry.get("update_time"),
        model=entry.get("model"),
        byte_start=byte_start,
        byte_end=byte_end,
        metadata={
            "envelope": envelope,
            "entry_key_order": list(entry.keys()),
            "fidelity": "dom_rendered",
        },
        capture_notes=entry.get("notes"),
    )


class UICaptureAdapter(ImportAdapter):
    name = "ui_capture"
    version = "1"
    source_method = "ui_assisted_capture"
    description = (
        "JSON bundle written by the browser capture script; uses the user's "
        "existing session, stores no credentials, GET-only."
    )

    def sniff(self, path: Path) -> bool:
        path = Path(path)
        if path.is_dir() or path.suffix.lower() != ".json":
            return False
        head = path.read_bytes()[:2048].decode("utf-8", "replace")
        return "arcs_capture_version" in head

    def read(self, path: Path) -> ImportPayload:
        path = Path(path)
        data = path.read_bytes()
        text = data.decode("utf-8")
        bundle = json.loads(text)
        version = bundle.get("arcs_capture_version")
        if version != BUNDLE_VERSION:
            raise ValueError(
                "unsupported capture bundle version %r (expected %d)" % (version, BUNDLE_VERSION)
            )
        warnings = list(bundle.get("warnings") or [])
        captured_at = bundle.get("captured_at") or utcnow()

        # Exact byte spans of each conversation entry inside the bundle, so the
        # capture is reconstructable verbatim, not just structurally.
        try:
            spans = {s.index: s for s in array_spans(text, key="conversations")}
        except ValueError as exc:
            spans = {}
            warnings.append("byte-span scan unavailable (%s)" % exc)

        conversations = []
        for i, entry in enumerate(bundle.get("conversations") or []):
            span = spans.get(i)
            if span is not None:
                raw = data[span.byte_start:span.byte_end]
                verbatim, start, end = True, span.byte_start, span.byte_end
            else:
                raw = canonical_json(entry).encode("utf-8")
                verbatim, start, end = False, None, None

            payload = entry.get("payload")
            if isinstance(payload, dict) and payload.get("mapping"):
                # The browser captured the provider's own JSON for this
                # conversation: parse it with the export parser so UI capture
                # and official export normalise identically.
                conv = parse_export_conversation(
                    payload, raw_bytes=raw, byte_start=start, byte_end=end,
                    verbatim=verbatim, source_uri=entry.get("url"),
                )
                conv.metadata["capture_entry"] = {
                    k: v for k, v in entry.items() if k != "payload"
                }
                conv.metadata["fidelity"] = "provider_json_via_ui"
                conv.metadata["entry_key_order"] = list(entry.keys())
                conv.capture_notes = entry.get("notes")
            else:
                if not entry.get("messages"):
                    warnings.append("conversation entry %d has neither payload nor messages" % i)
                conv = parse_dom_conversation(entry, raw, start, end, verbatim)
            conversations.append(conv)

        return ImportPayload(
            container_bytes=data,
            container_media_type="application/json",
            conversations=conversations,
            extraction_date=captured_at,
            source_method=self.source_method,
            input_path=str(path),
            metadata={
                "capture_method": bundle.get("capture_method"),
                "capture_script_version": bundle.get("script_version"),
                "source_system": bundle.get("source_system", "chatgpt"),
                "conversation_count": len(conversations),
            },
            warnings=warnings,
        )


register(UICaptureAdapter())
