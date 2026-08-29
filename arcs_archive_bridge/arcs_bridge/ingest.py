"""Ingestion pipeline: capture -> immutable source record -> normalised rows.

Order of operations matters and is deliberate:

1. **Credential gate.**  Nothing is written until the input has been checked for
   authentication material (:mod:`arcs_bridge.credentials`).
2. **Store the bytes.**  The exact source bytes are hashed and written to the
   content-addressed blob store, and a source record is appended for this
   capture event.  Source records are never deleted, so re-importing the same
   material deduplicates the *bytes* while keeping every sighting.
3. **Normalise.**  Only then are conversations and messages projected into
   relational form.  Normalisation is lossless: the complete original object is
   retained alongside the projection, and :mod:`arcs_bridge.reconstruct` proves
   it by rebuilding the input from the tables.
4. **Version, never overwrite.**  A changed conversation appends a version and
   supersedes the previous one; an unchanged one records a sighting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import adapters as adapter_registry
from . import credentials as cred
from .blobstore import BlobStore
from .errors import CredentialMaterialFound
from .hashing import content_hash, sha256_bytes
from .security import RESTRICTED, most_restrictive, parse_class
from .util import new_id, stable_id, to_iso, utcnow


@dataclass
class ConversationOutcome:
    source_conversation_id: str
    conversation_id: str
    title: str = ""
    status: str = "unchanged"          # new | updated | unchanged
    version_no: int = 0
    version_id: str = ""
    source_record_id: str = ""
    source_sha256: str = ""
    duplicate_of_record_id: str = ""
    messages_new: int = 0
    messages_updated: int = 0
    messages_unchanged: int = 0
    verbatim_bytes: bool = True
    security_class: str = "Normal"
    flags: list = field(default_factory=list)


@dataclass
class IngestResult:
    batch_id: str
    adapter: str
    adapter_version: str
    source_method: str
    input_path: str
    input_sha256: str
    extraction_date: str
    conversations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    refused: bool = False
    refusal_reason: str = ""

    @property
    def new_count(self) -> int:
        return sum(1 for c in self.conversations if c.status == "new")

    @property
    def updated_count(self) -> int:
        return sum(1 for c in self.conversations if c.status == "updated")

    @property
    def unchanged_count(self) -> int:
        return sum(1 for c in self.conversations if c.status == "unchanged")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["summary"] = {
            "conversations": len(self.conversations),
            "new": self.new_count,
            "updated": self.updated_count,
            "unchanged": self.unchanged_count,
            "messages_new": sum(c.messages_new for c in self.conversations),
            "messages_updated": sum(c.messages_updated for c in self.conversations),
        }
        return data


# --------------------------------------------------------------- credentials
def _credential_check(payload, policy: str) -> tuple:
    """Return ``(refuse, flag_by_conversation)`` findings for a payload."""
    refuse: list = []
    flags: dict = {}

    container_json = None
    try:
        container_json = json.loads(payload.container_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        container_json = None
    if container_json is not None:
        refuse.extend(cred.scan_keys(container_json))

    if policy == "strict":
        envelope_findings = cred.scan_json(payload.metadata, "$.batch_metadata")
        r, _ = cred.partition(envelope_findings)
        refuse.extend(r)

    for conv in payload.conversations:
        conv_flags: list = []
        if policy == "strict":
            envelope = {k: v for k, v in (conv.metadata or {}).items() if k != "capture_entry"}
            r, f = cred.partition(cred.scan_json(envelope, "$.%s" % conv.source_conversation_id))
            refuse.extend(r)
            conv_flags.extend(f)
        for msg in conv.messages:
            # Message *content* never blocks ingestion: preserving what the user
            # actually said outranks tidiness.  It is flagged and reclassified.
            _, f = cred.partition(cred.scan_text(msg.content_text, "message"))
            conv_flags.extend(f)
        if conv_flags:
            flags[conv.source_conversation_id] = conv_flags
    return refuse, flags


def _write_refusal_report(config, batch_id: str, path: Path, findings) -> Path:
    """Record *that* an input was refused, never the material itself."""
    config.quarantine_dir.mkdir(parents=True, exist_ok=True)
    report = config.quarantine_dir / ("%s.refusal.json" % batch_id)
    report.write_text(
        json.dumps(
            {
                "batch_id": batch_id,
                "refused_at": utcnow(),
                "input_path": str(path),
                "reason": "input contained authentication material",
                "finding_kinds": sorted({f.kind for f in findings}),
                "finding_locations": sorted({f.where for f in findings})[:50],
                "note": (
                    "The offending file was NOT copied into the archive. Fix the "
                    "capture so it carries no credentials, then re-run ingestion."
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return report


# ----------------------------------------------------------------- fingerprints
def message_fingerprint(msg) -> str:
    return content_hash(
        {
            "role": msg.role,
            "author_name": msg.author_name,
            "content_type": msg.content_type,
            "content_text": msg.content_text,
            "content_parts": msg.content_parts,
            "create_time_raw": msg.create_time_raw,
            "update_time_raw": msg.update_time_raw,
            "parent": msg.parent_source_message_id,
            "on_canonical_path": msg.on_canonical_path,
            "seq": msg.seq,
            "metadata": msg.metadata,
            "attachments": [
                {
                    "kind": a.kind,
                    "name": a.name,
                    "mime_type": a.mime_type,
                    "byte_size": a.byte_size,
                    "source_uri": a.source_uri,
                    "source_file_id": a.source_file_id,
                    "metadata": a.metadata,
                    "sha256": sha256_bytes(a.data) if a.data else None,
                }
                for a in msg.attachments
            ],
        }
    )


def conversation_fingerprint(conv, message_hashes) -> str:
    return content_hash(
        {
            "source_conversation_id": conv.source_conversation_id,
            "title": conv.title,
            "create_time_raw": conv.create_time_raw,
            "update_time_raw": conv.update_time_raw,
            "model": conv.model,
            "metadata": conv.metadata,
            "messages": list(message_hashes),
        }
    )


# --------------------------------------------------------------------- ingest
def ingest_path(archive, path, adapter_name=None, operator: str = "", notes: str = "",
                default_security_class: str = "Normal", correction_reason: str = "",
                credential_policy: str = "strict") -> IngestResult:
    """Ingest one file (or export directory) into the archive."""
    path = Path(path)
    adapter = (
        adapter_registry.get(adapter_name) if adapter_name else adapter_registry.detect(path)
    )
    payload = adapter.read(path)
    default_security_class = parse_class(default_security_class)

    batch_id = new_id("batch")
    input_sha = sha256_bytes(payload.container_bytes)
    started = utcnow()

    refuse, flags = _credential_check(payload, credential_policy)
    if refuse:
        report = _write_refusal_report(archive.config, batch_id, path, refuse)
        archive.source.execute(
            "INSERT INTO import_batches(batch_id, adapter, adapter_version, source_method, "
            "started_at, finished_at, operator, host, input_path, input_sha256, params_json, "
            "status, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, adapter.name, adapter.version, payload.source_method, started, utcnow(),
             operator or archive.config.operator, "", str(path), input_sha,
             json.dumps({"credential_policy": credential_policy}), "refused",
             "refused: authentication material detected"),
        )
        archive.source.commit()
        archive.log("ingest.refused", {"batch_id": batch_id, "input": str(path),
                                       "kinds": sorted({f.kind for f in refuse})})
        raise CredentialMaterialFound(
            "refusing to ingest %s: %s. Nothing was stored. Report: %s"
            % (path, cred.summarise(refuse), report)
        )

    result = IngestResult(
        batch_id=batch_id,
        adapter=adapter.name,
        adapter_version=adapter.version,
        source_method=payload.source_method,
        input_path=str(payload.input_path or path),
        input_sha256=input_sha,
        extraction_date=payload.extraction_date,
        warnings=list(payload.warnings),
    )

    store = BlobStore(archive.config.blobs_dir)
    conn = archive.source
    with archive.transaction(conn):
        conn.execute(
            "INSERT INTO import_batches(batch_id, adapter, adapter_version, source_method, "
            "started_at, operator, host, input_path, input_sha256, params_json, status, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'running', ?)",
            (batch_id, adapter.name, adapter.version, payload.source_method, started,
             operator or archive.config.operator, "", str(payload.input_path or path),
             input_sha, json.dumps({"credential_policy": credential_policy,
                                    "default_security_class": default_security_class}), notes),
        )

        # 1. the container, verbatim
        container_blob = store.put(payload.container_bytes, payload.container_media_type)
        store.register(conn, container_blob)
        container_record_id = _append_source_record(
            conn, blob=container_blob, batch_id=batch_id, adapter=adapter,
            payload=payload, record_kind="container", source_uri=str(payload.input_path or path),
            source_conversation_id=None, parent_record_id=None,
            metadata={"warnings": payload.warnings, "adapter_metadata": payload.metadata},
            notes=notes,
        )

        # 2. each conversation
        for conv in payload.conversations:
            outcome = _ingest_conversation(
                archive, conn, store, adapter, payload, conv, batch_id,
                container_record_id, default_security_class, correction_reason,
                flags.get(conv.source_conversation_id) or [],
            )
            result.conversations.append(outcome)

        conn.execute(
            "UPDATE import_batches SET finished_at=?, status='complete' WHERE batch_id=?",
            (utcnow(), batch_id),
        )

    archive.log(
        "ingest",
        {
            "batch_id": batch_id,
            "adapter": adapter.name,
            "source_method": payload.source_method,
            "input": str(payload.input_path or path),
            "input_sha256": input_sha,
            "conversations": len(result.conversations),
            "new": result.new_count,
            "updated": result.updated_count,
            "unchanged": result.unchanged_count,
        },
        actor=operator,
    )
    return result


def _append_source_record(conn, blob, batch_id, adapter, payload, record_kind,
                          source_uri, source_conversation_id, parent_record_id,
                          metadata, notes="", byte_start=None, byte_end=None) -> str:
    """Append an immutable source record.  Duplicates are linked, never dropped."""
    record_id = new_id("src")
    prior = conn.execute(
        "SELECT record_id FROM source_records WHERE blob_sha256=? ORDER BY ingested_at LIMIT 1",
        (blob.sha256,),
    ).fetchone()
    conn.execute(
        "INSERT INTO source_records(record_id, blob_sha256, batch_id, source_method, adapter, "
        "adapter_version, record_kind, source_uri, source_conversation_id, extraction_date, "
        "ingested_at, parent_record_id, byte_start, byte_end, duplicate_of_record_id, "
        "capture_notes, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            record_id, blob.sha256, batch_id, payload.source_method, adapter.name,
            adapter.version, record_kind, source_uri, source_conversation_id,
            payload.extraction_date, utcnow(), parent_record_id, byte_start, byte_end,
            prior["record_id"] if prior else None, notes,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
        ),
    )
    return record_id


def _ingest_conversation(archive, conn, store, adapter, payload, conv, batch_id,
                         container_record_id, default_class, correction_reason,
                         flag_findings) -> ConversationOutcome:
    now = utcnow()
    conversation_id = stable_id("conversation", conv.source_system, conv.source_conversation_id)

    blob = store.put(conv.raw_bytes, conv.raw_media_type)
    store.register(conn, blob)
    record_id = _append_source_record(
        conn, blob=blob, batch_id=batch_id, adapter=adapter, payload=payload,
        record_kind="conversation", source_uri=conv.source_uri,
        source_conversation_id=conv.source_conversation_id,
        parent_record_id=container_record_id,
        metadata={
            "verbatim_bytes": conv.raw_is_verbatim,
            "fidelity": (conv.metadata or {}).get("fidelity"),
            "title": conv.title,
        },
        notes=conv.capture_notes or "",
        byte_start=conv.byte_start, byte_end=conv.byte_end,
    )
    dup = conn.execute(
        "SELECT duplicate_of_record_id FROM source_records WHERE record_id=?", (record_id,)
    ).fetchone()["duplicate_of_record_id"]

    security_class = default_class
    flag_notes = []
    if flag_findings:
        security_class = most_restrictive(security_class, RESTRICTED)
        flag_notes = sorted({f.kind for f in flag_findings})

    existing = conn.execute(
        "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO conversations(conversation_id, source_conversation_id, source_system, "
            "security_class, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
            (conversation_id, conv.source_conversation_id, conv.source_system,
             security_class, now, now),
        )
        if security_class != "Normal":
            _record_class(conn, "conversation", conversation_id, None, security_class,
                          "set at ingestion" + (": " + ", ".join(flag_notes) if flag_notes else ""),
                          archive.config.operator)
    else:
        merged = most_restrictive(existing["security_class"], security_class)
        conn.execute(
            "UPDATE conversations SET last_seen_at=?, security_class=? WHERE conversation_id=?",
            (now, merged, conversation_id),
        )
        if merged != existing["security_class"]:
            _record_class(conn, "conversation", conversation_id, existing["security_class"],
                          merged, "escalated at ingestion: " + ", ".join(flag_notes),
                          archive.config.operator)
        security_class = merged

    outcome = ConversationOutcome(
        source_conversation_id=conv.source_conversation_id,
        conversation_id=conversation_id,
        title=conv.title or "",
        source_record_id=record_id,
        source_sha256=blob.sha256,
        duplicate_of_record_id=dup or "",
        verbatim_bytes=conv.raw_is_verbatim,
        security_class=security_class,
        flags=flag_notes,
    )

    msg_hashes = [message_fingerprint(m) for m in conv.messages]
    conv_hash = conversation_fingerprint(conv, msg_hashes)

    current = conn.execute(
        "SELECT * FROM conversation_versions WHERE conversation_id=? AND is_current=1",
        (conversation_id,),
    ).fetchone()

    if current is not None and current["content_sha256"] == conv_hash:
        # Identical content seen again: record the sighting, add no version.
        conn.execute(
            "INSERT INTO conversation_version_sightings(sighting_id, version_id, source_record_id, "
            "batch_id, observed_at) VALUES (?,?,?,?,?)",
            (new_id("sight"), current["version_id"], record_id, batch_id, now),
        )
        for row in conn.execute(
            "SELECT mv.version_id FROM conversation_version_messages cvm "
            "JOIN message_versions mv ON mv.version_id = cvm.message_version_id "
            "WHERE cvm.conversation_version_id=?",
            (current["version_id"],),
        ).fetchall():
            conn.execute(
                "INSERT INTO message_version_sightings(sighting_id, version_id, source_record_id, "
                "batch_id, observed_at) VALUES (?,?,?,?,?)",
                (new_id("sight"), row["version_id"], record_id, batch_id, now),
            )
        conn.execute(
            "UPDATE messages SET last_seen_at=? WHERE conversation_id=?", (now, conversation_id)
        )
        outcome.status = "unchanged"
        outcome.version_id = current["version_id"]
        outcome.version_no = current["version_no"]
        outcome.messages_unchanged = len(conv.messages)
        return outcome

    version_no = (conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) FROM conversation_versions WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()[0]) + 1
    version_id = stable_id("conversation_version", conversation_id, version_no, conv_hash)

    if current is not None:
        conn.execute(
            "UPDATE conversation_versions SET is_current=0 WHERE version_id=?",
            (current["version_id"],),
        )
    conn.execute(
        "INSERT INTO conversation_versions(version_id, conversation_id, version_no, "
        "source_record_id, title, create_time, update_time, create_time_raw, update_time_raw, "
        "model, message_count, content_sha256, source_sha256, metadata_json, "
        "supersedes_version_id, correction_reason, is_current, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (
            version_id, conversation_id, version_no, record_id, conv.title,
            to_iso(conv.create_time_raw), to_iso(conv.update_time_raw),
            json.dumps(conv.create_time_raw), json.dumps(conv.update_time_raw),
            conv.model, len(conv.messages), conv_hash, blob.sha256,
            json.dumps(conv.metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
            current["version_id"] if current is not None else None,
            correction_reason or (None if current is None else "later capture supersedes earlier"),
            now,
        ),
    )
    conn.execute(
        "INSERT INTO conversation_version_sightings(sighting_id, version_id, source_record_id, "
        "batch_id, observed_at) VALUES (?,?,?,?,?)",
        (new_id("sight"), version_id, record_id, batch_id, now),
    )

    for msg, msg_hash in zip(conv.messages, msg_hashes):
        status, mv_id = _ingest_message(
            archive, conn, store, conv, msg, msg_hash, conversation_id, version_id,
            record_id, batch_id, now, correction_reason, security_class,
        )
        conn.execute(
            "INSERT INTO conversation_version_messages(conversation_version_id, "
            "message_version_id, seq) VALUES (?,?,?)",
            (version_id, mv_id, msg.seq),
        )
        if status == "new":
            outcome.messages_new += 1
        elif status == "updated":
            outcome.messages_updated += 1
        else:
            outcome.messages_unchanged += 1

    _reindex_conversation_fts(conn, conversation_id, conv.title)

    outcome.status = "new" if current is None else "updated"
    outcome.version_id = version_id
    outcome.version_no = version_no
    return outcome


def _ingest_message(archive, conn, store, conv, msg, msg_hash, conversation_id,
                    conv_version_id, record_id, batch_id, now, correction_reason,
                    conversation_class) -> tuple:
    source_message_id = msg.source_message_id or ("seq:%d" % msg.seq)
    message_id = stable_id("message", conversation_id, source_message_id)

    existing = conn.execute(
        "SELECT * FROM messages WHERE message_id=?", (message_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO messages(message_id, conversation_id, source_message_id, "
            "security_class, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
            (message_id, conversation_id, source_message_id, conversation_class, now, now),
        )
    else:
        conn.execute("UPDATE messages SET last_seen_at=? WHERE message_id=?", (now, message_id))

    current = conn.execute(
        "SELECT * FROM message_versions WHERE message_id=? AND is_current=1", (message_id,)
    ).fetchone()

    if current is not None and current["content_sha256"] == msg_hash:
        conn.execute(
            "INSERT INTO message_version_sightings(sighting_id, version_id, source_record_id, "
            "batch_id, observed_at) VALUES (?,?,?,?,?)",
            (new_id("sight"), current["version_id"], record_id, batch_id, now),
        )
        return "unchanged", current["version_id"]

    version_no = (conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) FROM message_versions WHERE message_id=?",
        (message_id,),
    ).fetchone()[0]) + 1
    version_id = stable_id("message_version", message_id, version_no, msg_hash)

    if current is not None:
        conn.execute(
            "UPDATE message_versions SET is_current=0 WHERE version_id=?", (current["version_id"],)
        )
    conn.execute(
        "INSERT INTO message_versions(version_id, message_id, conversation_id, "
        "first_conversation_version_id, version_no, source_record_id, seq, role, author_name, "
        "create_time, create_time_raw, update_time, content_type, content_text, "
        "content_parts_json, content_sha256, parent_source_message_id, on_canonical_path, "
        "branch_note, metadata_json, supersedes_version_id, correction_reason, is_current, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (
            version_id, message_id, conversation_id, conv_version_id, version_no, record_id,
            msg.seq, msg.role, msg.author_name, to_iso(msg.create_time_raw),
            json.dumps(msg.create_time_raw), to_iso(msg.update_time_raw), msg.content_type,
            msg.content_text,
            json.dumps(msg.content_parts, ensure_ascii=False, default=str),
            msg_hash, msg.parent_source_message_id, 1 if msg.on_canonical_path else 0,
            msg.branch_note,
            json.dumps(msg.metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
            current["version_id"] if current is not None else None,
            correction_reason or (None if current is None else "later capture supersedes earlier"),
            now,
        ),
    )
    conn.execute(
        "INSERT INTO message_version_sightings(sighting_id, version_id, source_record_id, "
        "batch_id, observed_at) VALUES (?,?,?,?,?)",
        (new_id("sight"), version_id, record_id, batch_id, now),
    )

    for i, att in enumerate(msg.attachments):
        blob_sha = None
        if att.data:
            att_blob = store.put(att.data, att.mime_type or "application/octet-stream")
            store.register(conn, att_blob)
            blob_sha = att_blob.sha256
        conn.execute(
            "INSERT INTO attachments(attachment_id, message_version_id, conversation_id, kind, "
            "name, mime_type, byte_size, source_uri, source_file_id, blob_sha256, "
            "content_present, metadata_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                stable_id("attachment", version_id, i), version_id, conversation_id, att.kind,
                att.name, att.mime_type, att.byte_size, att.source_uri, att.source_file_id,
                blob_sha, 1 if blob_sha else 0,
                json.dumps(att.metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
                now,
            ),
        )

    _reindex_message_fts(conn, message_id, version_id, conversation_id, msg, conv.title)
    return ("new" if current is None else "updated"), version_id


def _reindex_message_fts(conn, message_id, version_id, conversation_id, msg, title) -> None:
    conn.execute("DELETE FROM message_fts WHERE message_id=?", (message_id,))
    conn.execute(
        "INSERT INTO message_fts(content_text, title, role, message_id, version_id, "
        "conversation_id) VALUES (?,?,?,?,?,?)",
        (msg.content_text, title or "", msg.role, message_id, version_id, conversation_id),
    )


def _reindex_conversation_fts(conn, conversation_id, title) -> None:
    conn.execute("DELETE FROM conversation_fts WHERE conversation_id=?", (conversation_id,))
    conn.execute(
        "INSERT INTO conversation_fts(title, conversation_id) VALUES (?,?)",
        (title or "", conversation_id),
    )
    conn.execute(
        "UPDATE message_fts SET title=? WHERE conversation_id=?", (title or "", conversation_id)
    )


def _record_class(conn, subject_type, subject_id, previous, security_class, rationale, actor):
    conn.execute(
        "INSERT INTO security_class_history(entry_id, subject_type, subject_id, previous_class, "
        "security_class, rationale, set_by, set_at) VALUES (?,?,?,?,?,?,?,?)",
        (new_id("sec"), subject_type, subject_id, previous, security_class, rationale,
         actor, utcnow()),
    )
