"""Import adapter interface.

An adapter turns *some* source of conversation material into a stream of
:class:`CapturedConversation` objects plus the exact bytes those objects came
from.  Everything downstream - hashing, provenance, normalisation,
reconstruction - is adapter-agnostic, which is what lets a future OpenAI
Enterprise Compliance / API importer replace UI-assisted capture without
touching the archive.

An adapter must:

* return the *exact source bytes* for each conversation (``raw_bytes``) plus,
  where the conversation came from a larger container, the byte span it
  occupied in that container;
* retain every field it saw in ``metadata`` rather than dropping unknown keys;
* never read, request, or store authentication material.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CapturedAttachment:
    kind: str = "file"          # file | image | audio | link | citation | tool_output
    name: "str | None" = None
    mime_type: "str | None" = None
    byte_size: "int | None" = None
    source_uri: "str | None" = None
    source_file_id: "str | None" = None
    data: "bytes | None" = None  # present only when the bytes were captured
    metadata: dict = field(default_factory=dict)


@dataclass
class CapturedMessage:
    source_message_id: "str | None"
    role: str
    content_text: str
    seq: int
    author_name: "str | None" = None
    create_time_raw: object = None
    update_time_raw: object = None
    content_type: "str | None" = None
    content_parts: list = field(default_factory=list)
    parent_source_message_id: "str | None" = None
    on_canonical_path: bool = True
    branch_note: "str | None" = None
    metadata: dict = field(default_factory=dict)
    attachments: list = field(default_factory=list)


@dataclass
class CapturedConversation:
    source_conversation_id: str
    title: "str | None"
    messages: list
    raw_bytes: bytes
    raw_media_type: str = "application/json"
    #: True when ``raw_bytes`` are the untouched original bytes for exactly this
    #: conversation.  False when they are a canonical re-serialisation, in which
    #: case reconstruction claims structural rather than byte identity.
    raw_is_verbatim: bool = True
    source_uri: "str | None" = None
    source_system: str = "chatgpt"
    create_time_raw: object = None
    update_time_raw: object = None
    model: "str | None" = None
    byte_start: "int | None" = None
    byte_end: "int | None" = None
    metadata: dict = field(default_factory=dict)
    capture_notes: "str | None" = None


@dataclass
class ImportPayload:
    """What an adapter hands the ingestion pipeline."""

    container_bytes: bytes
    container_media_type: str
    conversations: list
    extraction_date: str
    source_method: str
    input_path: "str | None" = None
    metadata: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


class ImportAdapter(abc.ABC):
    """Base class for every import path into the archive."""

    #: Short stable identifier recorded on every source record.
    name: str = "abstract"
    #: Bump whenever parsing changes in a way that could alter normalisation.
    version: str = "0"
    #: How the material was obtained.  Recorded on every source record.
    source_method: str = "unknown"
    #: Human-readable one-liner for `arcs adapters`.
    description: str = ""
    #: True when the adapter only ever reads from its source.
    read_only: bool = True

    @abc.abstractmethod
    def sniff(self, path: Path) -> bool:
        """Cheap check: does this input look like something we can read?"""

    @abc.abstractmethod
    def read(self, path: Path) -> ImportPayload:
        """Parse the input.  Must not mutate it."""

    def describe(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "source_method": self.source_method,
            "description": self.description,
            "read_only": self.read_only,
        }
