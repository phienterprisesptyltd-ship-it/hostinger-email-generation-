"""Adapter for the official ChatGPT data export (``conversations.json``).

This is the highest-fidelity import path available without an enterprise
agreement: it is the provider's own serialisation, it is obtained by the user
from their own account, and it involves no scraping and no credentials.

Fidelity rules kept here:

* the exact byte span of each conversation inside the export file is recorded,
  so a single conversation can be handed back verbatim;
* the complete original node object for every message is retained in
  ``metadata`` - including branches that are not on the current path - so the
  export can be rebuilt from the normalised tables alone;
* nothing is dropped because this adapter did not recognise it.
"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path

from ..hashing import canonical_json
from ..jsonspans import array_spans
from ..util import to_iso, utcnow
from . import register
from .base import (
    CapturedAsset,
    CapturedAttachment,
    CapturedConversation,
    CapturedMessage,
    ImportAdapter,
    ImportPayload,
)

#: Files the export ships that are metadata about the export, not attachments.
EXPORT_METADATA_FILES = {
    "conversations.json", "message_feedback.json", "model_comparisons.json",
    "shared_conversations.json", "user.json", "chat.html", "index.html",
}

#: Refuse to slurp something pathological into memory without saying so.
DEFAULT_MAX_ASSET_BYTES = 256 * 1024 * 1024

#: Message roles that carry no user-visible content and are hidden by ChatGPT.
_HIDDEN_HINT_KEYS = ("is_visually_hidden_from_conversation",)


def render_content(content) -> tuple:
    """Return ``(content_type, text, parts, attachments)`` for a content object.

    ``parts`` is the original structure, untouched.  ``text`` is a rendering for
    search and for the Markdown projection; it is never the authority, and the
    reconstruction path ignores it entirely.
    """
    attachments: list = []
    if content is None:
        return None, "", [], attachments
    if isinstance(content, str):
        return "text", content, [content], attachments
    if not isinstance(content, dict):
        return None, canonical_json(content), [content], attachments

    ctype = content.get("content_type")
    parts = content.get("parts")

    if ctype in ("text", "multimodal_text") and isinstance(parts, list):
        chunks = []
        for i, part in enumerate(parts):
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                # Image / audio / file pointers inside a multimodal message.
                pointer = part.get("asset_pointer") or part.get("audio_asset_pointer")
                name = part.get("name") or part.get("file_name")
                attachments.append(
                    CapturedAttachment(
                        kind=part.get("content_type", "image") or "image",
                        name=name,
                        mime_type=part.get("mime_type"),
                        byte_size=part.get("size_bytes") or part.get("size"),
                        source_uri=pointer if isinstance(pointer, str) else None,
                        source_file_id=(part.get("file_id") or part.get("id")
                                        or pointer_file_id(pointer)),
                        metadata={"part_index": i, "part": part},
                    )
                )
                label = name or pointer or part.get("content_type") or "asset"
                chunks.append("[%s: %s]" % (part.get("content_type", "asset"), label))
            else:
                chunks.append(canonical_json(part))
        return ctype, "\n\n".join(chunks), parts if isinstance(parts, list) else [], attachments

    if ctype == "code":
        text = content.get("text", "")
        return ctype, text, [text], attachments

    if ctype in ("execution_output", "system_error"):
        text = content.get("text") or content.get("result") or ""
        return ctype, text, [text], attachments

    if ctype in ("tether_browsing_display", "tether_quote"):
        text = content.get("result") or content.get("text") or ""
        for i, citation in enumerate(content.get("domains") or []):
            attachments.append(
                CapturedAttachment(kind="citation", name=str(citation),
                                   metadata={"index": i, "source": "tether"})
            )
        return ctype, text, [text], attachments

    # Unknown content type: keep the structure verbatim and render canonically
    # rather than guessing (and rather than losing it).
    return ctype, canonical_json(content), [content], attachments


def _attachments_from_metadata(meta: dict) -> list:
    out: list = []
    if not isinstance(meta, dict):
        return out
    for i, att in enumerate(meta.get("attachments") or []):
        if not isinstance(att, dict):
            continue
        out.append(
            CapturedAttachment(
                kind="file",
                name=att.get("name"),
                mime_type=att.get("mime_type") or att.get("mimeType"),
                byte_size=att.get("size") or att.get("fileSizeTokens"),
                source_file_id=att.get("id"),
                metadata={"index": i, "attachment": att},
            )
        )
    for i, cite in enumerate(meta.get("citations") or []):
        if not isinstance(cite, dict):
            continue
        meta_cite = cite.get("metadata") or {}
        out.append(
            CapturedAttachment(
                kind="citation",
                name=meta_cite.get("title") or meta_cite.get("text"),
                source_uri=meta_cite.get("url"),
                metadata={"index": i, "citation": cite},
            )
        )
    return out


_FILE_ID = re.compile(r"^(file[-_][A-Za-z0-9]+)")


def pointer_file_id(value) -> "str | None":
    """Extract a provider file id from an asset pointer or attachment id.

    Handles the forms seen in exports: a bare ``file-AbC123``, the older
    ``file-service://file-AbC123`` and the newer ``sediment://file_0000abcd``.
    The scheme is stripped first, so ``file-service`` is never mistaken for the
    identifier it prefixes.
    """
    if not isinstance(value, str):
        return None
    tail = value.rsplit("//", 1)[-1].strip()
    match = _FILE_ID.match(tail)
    return match.group(1) if match else None


def asset_file_ids(path: Path) -> set:
    """The provider ids a file on disk could answer to.

    Exports name attachments ``<file-id>-<original name>.<ext>``, and image
    generations by id alone, so both shapes are matched - and the full stem is
    kept as a candidate for anything that names itself differently.
    """
    stem = path.stem
    candidates = {stem}
    match = re.match(r"^(file[-_][A-Za-z0-9]+)", stem)
    if match:
        candidates.add(match.group(1))
    return {c for c in candidates if c}


def index_export_assets(directory: Path, max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
                        warnings=None):
    """Every non-metadata file in an export directory, indexed by provider id.

    Returns ``(assets, by_file_id)`` where ``assets`` is every file found - the
    whole export is archived, whether or not a message references it.
    """
    warnings = warnings if warnings is not None else []
    assets, by_file_id = [], {}
    if not directory.is_dir():
        return assets, by_file_id
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relpath = path.relative_to(directory).as_posix()
        if path.name in EXPORT_METADATA_FILES and "/" not in relpath:
            continue
        size = path.stat().st_size
        if size > max_bytes:
            warnings.append(
                "skipped %s: %d bytes exceeds the %d byte asset limit"
                % (relpath, size, max_bytes)
            )
            continue
        file_ids = asset_file_ids(path)
        asset = CapturedAsset(
            relpath=relpath,
            data=path.read_bytes(),
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            file_ids=tuple(sorted(file_ids)),
            metadata={"original_filename": path.name, "byte_size": size},
        )
        assets.append(asset)
        for file_id in file_ids:
            by_file_id.setdefault(file_id, asset)
    return assets, by_file_id


def link_assets(conversation, by_file_id: dict) -> int:
    """Point a conversation's attachments at the files that shipped with it."""
    linked = 0
    for message in conversation.messages:
        for attachment in message.attachments:
            if attachment.data is not None:
                continue
            keys = [
                attachment.source_file_id,
                pointer_file_id(attachment.source_file_id),
                pointer_file_id(attachment.source_uri),
            ]
            asset = next((by_file_id[k] for k in keys if k and k in by_file_id), None)
            if asset is None:
                continue
            attachment.data = asset.data
            attachment.mime_type = attachment.mime_type or asset.media_type
            attachment.byte_size = attachment.byte_size or len(asset.data)
            attachment.metadata["export_relpath"] = asset.relpath
            attachment.metadata["original_filename"] = asset.metadata.get("original_filename")
            # The source's own claim about size stays in byte_size; what we
            # actually hold is recorded beside it so a mismatch is visible.
            attachment.metadata["stored_byte_size"] = len(asset.data)
            linked += 1
    return linked


def _walk(mapping: dict, root_ids: list) -> list:
    """Deterministic depth-first pre-order over the node tree."""
    order: list = []
    seen: set = set()

    def visit(node_id):
        if node_id in seen or node_id not in mapping:
            return
        seen.add(node_id)
        order.append(node_id)
        for child in mapping[node_id].get("children") or []:
            visit(child)

    for rid in root_ids:
        visit(rid)
    # Any orphan the tree walk missed is still archived, in file order.
    for node_id in mapping:
        if node_id not in seen:
            seen.add(node_id)
            order.append(node_id)
    return order


def _canonical_path(mapping: dict, current_node) -> list:
    """Node ids from root to ``current_node`` - the conversation as displayed."""
    if not current_node or current_node not in mapping:
        return []
    path: list = []
    node_id = current_node
    guard = 0
    while node_id and node_id in mapping and guard <= len(mapping):
        path.append(node_id)
        node_id = mapping[node_id].get("parent")
        guard += 1
    path.reverse()
    return path


def parse_conversation(obj: dict, raw_bytes: bytes, byte_start=None, byte_end=None,
                       verbatim: bool = True, source_uri=None) -> CapturedConversation:
    mapping = obj.get("mapping") or {}
    root_ids = [nid for nid, node in mapping.items() if not node.get("parent")]
    if not root_ids:
        root_ids = list(mapping)[:1]
    order = _walk(mapping, root_ids)
    on_path = set(_canonical_path(mapping, obj.get("current_node")))
    if not on_path:
        on_path = set(order)  # no current_node recorded: treat every node as shown

    messages: list = []
    seq = 0
    model = None
    for node_id in order:
        node = mapping.get(node_id) or {}
        message = node.get("message")
        if not isinstance(message, dict):
            continue  # structural root node: retained via mapping_order below
        author = message.get("author") or {}
        meta = message.get("metadata") or {}
        model = model or meta.get("model_slug")
        ctype, text, parts, part_attachments = render_content(message.get("content"))
        attachments = part_attachments + _attachments_from_metadata(meta)
        branch_note = None
        if node_id not in on_path:
            branch_note = "off canonical path (edited/regenerated branch)"
        hidden = any(meta.get(k) for k in _HIDDEN_HINT_KEYS)
        seq += 1
        messages.append(
            CapturedMessage(
                source_message_id=message.get("id") or node_id,
                role=author.get("role") or "unknown",
                author_name=author.get("name"),
                content_text=text,
                content_type=ctype,
                content_parts=parts,
                seq=seq,
                create_time_raw=message.get("create_time"),
                update_time_raw=message.get("update_time"),
                parent_source_message_id=node.get("parent"),
                on_canonical_path=node_id in on_path,
                branch_note=branch_note if not hidden else (branch_note or "") + " hidden",
                # The complete original node, so the export can be rebuilt from
                # the normalised tables without consulting the blob store.
                metadata={"node_id": node_id, "node": node},
                attachments=attachments,
            )
        )

    envelope = {k: v for k, v in obj.items() if k != "mapping"}
    conv_id = (
        obj.get("conversation_id")
        or obj.get("id")
        or obj.get("conversation_template_id")
        or "unknown"
    )
    return CapturedConversation(
        source_conversation_id=str(conv_id),
        title=obj.get("title"),
        messages=messages,
        raw_bytes=raw_bytes,
        raw_is_verbatim=verbatim,
        source_uri=source_uri or ("https://chatgpt.com/c/%s" % conv_id),
        create_time_raw=obj.get("create_time"),
        update_time_raw=obj.get("update_time"),
        model=model or obj.get("default_model_slug"),
        byte_start=byte_start,
        byte_end=byte_end,
        metadata={
            "envelope": envelope,
            "mapping_order": list(mapping.keys()),
            "envelope_key_order": list(obj.keys()),
            # Tree nodes that carry no message (the synthetic root, and any
            # placeholder) are retained here so the mapping can be rebuilt
            # exactly from the normalised tables.
            "structural_nodes": {
                nid: node for nid, node in mapping.items()
                if not isinstance(node.get("message"), dict)
            },
        },
    )


class ChatGPTExportAdapter(ImportAdapter):
    name = "chatgpt_export"
    #: 2 - acquires the files that ship beside conversations.json.  Bumped
    #: because it changes what normalisation produces: a re-import of the same
    #: export now carries attachment bytes, which appends message versions.
    version = "2"
    source_method = "official_export"
    description = ("Official ChatGPT data export (conversations.json plus the files "
                   "beside it), read verbatim.")


    def sniff(self, path: Path) -> bool:
        path = Path(path)
        if path.is_dir():
            return (path / "conversations.json").exists()
        if path.suffix.lower() != ".json":
            return False
        head = path.read_bytes()[:65536].decode("utf-8", "replace")
        if "arcs_capture_version" in head:
            return False  # a capture bundle; ui_capture owns that shape
        markers = ('"current_node"', '"conversation_id"', '"children"', '"author"')
        return '"mapping"' in head and any(m in head for m in markers)

    def _resolve(self, path: Path) -> Path:
        path = Path(path)
        return path / "conversations.json" if path.is_dir() else path

    def read(self, path: Path, options=None) -> ImportPayload:
        options = options or {}
        acquire_assets = options.get("acquire_assets", True)
        max_asset_bytes = options.get("max_asset_bytes", DEFAULT_MAX_ASSET_BYTES)
        target = self._resolve(path)
        # Attachments live beside conversations.json inside the export, so the
        # directory - not the JSON file - is the real container.
        asset_root = target.parent
        data = target.read_bytes()
        text = data.decode("utf-8")
        warnings: list = []
        conversations: list = []
        assets, by_file_id = [], {}
        if acquire_assets:
            assets, by_file_id = index_export_assets(
                asset_root, max_bytes=max_asset_bytes, warnings=warnings
            )

        try:
            spans = array_spans(text, key="conversations")
            for span in spans:
                if not isinstance(span.value, dict):
                    warnings.append("skipped non-object array element at index %d" % span.index)
                    continue
                conversations.append(
                    parse_conversation(
                        span.value,
                        raw_bytes=data[span.byte_start:span.byte_end],
                        byte_start=span.byte_start,
                        byte_end=span.byte_end,
                        verbatim=True,
                    )
                )
        except ValueError as exc:
            # Not an array we can scan; fall back to a canonical re-serialisation
            # and record that per-conversation bytes are structural, not verbatim.
            warnings.append("byte-span scan unavailable (%s); using canonical re-serialisation" % exc)
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else parsed.get("conversations") or [parsed]
            for obj in items:
                conversations.append(
                    parse_conversation(
                        obj,
                        raw_bytes=canonical_json(obj).encode("utf-8"),
                        verbatim=False,
                    )
                )
        # An official export was extracted from the account when the file was
        # produced, not when we happened to read it.  Prefer the file's own
        # mtime and fall back to now only if it is unavailable.
        try:
            extraction_date = to_iso(target.stat().st_mtime) or utcnow()
        except OSError:  # pragma: no cover - defensive
            extraction_date = utcnow()
        linked = sum(link_assets(conv, by_file_id) for conv in conversations)
        unmatched = sorted(
            {a.relpath for a in assets}
            - {
                att.metadata.get("export_relpath")
                for conv in conversations for msg in conv.messages
                for att in msg.attachments
            }
        )
        if unmatched:
            warnings.append(
                "%d file(s) in the export are not referenced by any message; "
                "archived anyway: %s" % (len(unmatched), ", ".join(unmatched[:5])
                                         + (" …" if len(unmatched) > 5 else ""))
            )

        return ImportPayload(
            container_bytes=data,
            container_media_type="application/json",
            conversations=conversations,
            assets=assets,
            extraction_date=extraction_date,
            source_method=self.source_method,
            input_path=str(target),
            metadata={
                "export_file": target.name,
                "export_directory": str(asset_root),
                "conversation_count": len(conversations),
                "assets_found": len(assets),
                "assets_linked": linked,
                "assets_unreferenced": unmatched,
            },
            warnings=warnings,
        )


register(ChatGPTExportAdapter())
