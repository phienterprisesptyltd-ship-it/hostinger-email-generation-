-- ARCS Archive Bridge - source layer schema (archive/db/source.sqlite3)
--
-- This database holds (a) immutable source records exactly as captured and
-- (b) a normalised projection of conversations and messages.  Nothing in this
-- file stores an AI interpretation: those live in derived.sqlite3.
--
-- Invariants enforced here or by arcs_bridge.integrity:
--   * a source blob is content-addressed and never rewritten;
--   * a source record is append-only and is never deleted by dedupe;
--   * a conversation/message version is never updated in place - corrections
--     append a new version that supersedes the previous one.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- provenance
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id        TEXT PRIMARY KEY,
    adapter         TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    source_method   TEXT NOT NULL,      -- ui_assisted_capture | official_export | compliance_api | ...
    started_at      TEXT NOT NULL,      -- ISO-8601 UTC
    finished_at     TEXT,
    operator        TEXT,
    host            TEXT,
    input_path      TEXT,
    input_sha256    TEXT,
    params_json     TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'running',  -- running | complete | failed | refused
    notes           TEXT
);

-- Content-addressed store.  The same bytes captured twice are stored once;
-- every capture event still gets its own source_records row (see below), so
-- deduplication never costs provenance.
CREATE TABLE IF NOT EXISTS source_blobs (
    sha256        TEXT PRIMARY KEY,
    byte_length   INTEGER NOT NULL,
    media_type    TEXT NOT NULL,
    storage       TEXT NOT NULL,        -- 'inline' | 'file'
    content       BLOB,                 -- populated when storage='inline'
    relpath       TEXT,                 -- populated when storage='file'
    first_seen_at TEXT NOT NULL
);

-- One row per capture event.  Append-only.
CREATE TABLE IF NOT EXISTS source_records (
    record_id           TEXT PRIMARY KEY,
    blob_sha256         TEXT NOT NULL REFERENCES source_blobs(sha256),
    batch_id            TEXT NOT NULL REFERENCES import_batches(batch_id),
    source_method       TEXT NOT NULL,
    adapter             TEXT NOT NULL,
    adapter_version     TEXT NOT NULL,
    record_kind         TEXT NOT NULL,  -- 'container' (whole file) | 'conversation' | 'attachment'
    source_uri          TEXT,           -- e.g. https://chatgpt.com/c/<id>  (no credentials)
    source_conversation_id TEXT,
    extraction_date     TEXT NOT NULL,  -- when the material was taken from the source
    ingested_at         TEXT NOT NULL,  -- when this archive recorded it
    parent_record_id    TEXT REFERENCES source_records(record_id),
    byte_start          INTEGER,        -- exact span inside parent container, when known
    byte_end            INTEGER,
    duplicate_of_record_id TEXT REFERENCES source_records(record_id),
    capture_notes       TEXT,
    metadata_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_source_records_blob ON source_records(blob_sha256);
CREATE INDEX IF NOT EXISTS ix_source_records_conv ON source_records(source_conversation_id);
CREATE INDEX IF NOT EXISTS ix_source_records_batch ON source_records(batch_id);

-- ------------------------------------------------------------ conversations
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id         TEXT PRIMARY KEY,   -- stable local id (uuid5)
    source_conversation_id  TEXT NOT NULL,
    source_system           TEXT NOT NULL DEFAULT 'chatgpt',
    security_class          TEXT NOT NULL DEFAULT 'Normal',
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    UNIQUE (source_system, source_conversation_id)
);

-- Append-only version history.  A later capture that differs appends a row;
-- the earlier row keeps is_current=0 and is never modified again.
CREATE TABLE IF NOT EXISTS conversation_versions (
    version_id        TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL REFERENCES conversations(conversation_id),
    version_no        INTEGER NOT NULL,
    source_record_id  TEXT NOT NULL REFERENCES source_records(record_id),
    title             TEXT,
    create_time       TEXT,             -- ISO-8601 UTC, when known
    update_time       TEXT,
    create_time_raw   TEXT,             -- original representation (e.g. epoch float)
    update_time_raw   TEXT,
    model             TEXT,
    message_count     INTEGER NOT NULL DEFAULT 0,
    content_sha256    TEXT NOT NULL,    -- hash of the canonical normalised form
    source_sha256     TEXT NOT NULL,    -- hash of the exact source bytes
    metadata_json     TEXT NOT NULL DEFAULT '{}',   -- retained original fields
    supersedes_version_id TEXT REFERENCES conversation_versions(version_id),
    correction_reason TEXT,
    is_current        INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    UNIQUE (conversation_id, version_no)
);
CREATE INDEX IF NOT EXISTS ix_convver_current ON conversation_versions(conversation_id, is_current);

-- Every time a version is observed again (identical content) we record the
-- sighting instead of writing a new version: dedupe with provenance intact.
CREATE TABLE IF NOT EXISTS conversation_version_sightings (
    sighting_id      TEXT PRIMARY KEY,
    version_id       TEXT NOT NULL REFERENCES conversation_versions(version_id),
    source_record_id TEXT NOT NULL REFERENCES source_records(record_id),
    batch_id         TEXT NOT NULL REFERENCES import_batches(batch_id),
    observed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_convsight_version ON conversation_version_sightings(version_id);

-- ----------------------------------------------------------------- messages
CREATE TABLE IF NOT EXISTS messages (
    message_id             TEXT PRIMARY KEY,   -- stable local id (uuid5)
    conversation_id        TEXT NOT NULL REFERENCES conversations(conversation_id),
    source_message_id      TEXT,
    security_class         TEXT NOT NULL DEFAULT 'Normal',
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    UNIQUE (conversation_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages(conversation_id);

CREATE TABLE IF NOT EXISTS message_versions (
    version_id              TEXT PRIMARY KEY,
    message_id              TEXT NOT NULL REFERENCES messages(message_id),
    conversation_id         TEXT NOT NULL REFERENCES conversations(conversation_id),
    first_conversation_version_id TEXT NOT NULL REFERENCES conversation_versions(version_id),
    version_no              INTEGER NOT NULL,
    source_record_id        TEXT NOT NULL REFERENCES source_records(record_id),
    seq                     INTEGER NOT NULL,   -- order along the canonical path
    role                    TEXT NOT NULL,      -- user | assistant | system | tool
    author_name             TEXT,
    create_time             TEXT,
    create_time_raw         TEXT,
    update_time             TEXT,
    content_type            TEXT,               -- text | code | multimodal_text | ...
    content_text            TEXT NOT NULL,      -- exact rendered text of the message
    content_parts_json      TEXT NOT NULL DEFAULT '[]',  -- exact original parts
    content_sha256          TEXT NOT NULL,
    parent_source_message_id TEXT,
    on_canonical_path       INTEGER NOT NULL DEFAULT 1,
    branch_note             TEXT,
    metadata_json           TEXT NOT NULL DEFAULT '{}',  -- retained original fields
    supersedes_version_id   TEXT REFERENCES message_versions(version_id),
    correction_reason       TEXT,
    is_current              INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT NOT NULL,
    UNIQUE (message_id, version_no)
);
CREATE INDEX IF NOT EXISTS ix_msgver_current ON message_versions(message_id, is_current);
CREATE INDEX IF NOT EXISTS ix_msgver_conv ON message_versions(conversation_id, is_current, seq);

-- Which message versions compose a given conversation version.  Explicit
-- membership means any historical conversation version can be rebuilt exactly,
-- even when most of its messages were unchanged and kept their earlier version.
CREATE TABLE IF NOT EXISTS conversation_version_messages (
    conversation_version_id TEXT NOT NULL REFERENCES conversation_versions(version_id),
    message_version_id      TEXT NOT NULL REFERENCES message_versions(version_id),
    seq                     INTEGER NOT NULL,
    PRIMARY KEY (conversation_version_id, message_version_id)
);
CREATE INDEX IF NOT EXISTS ix_cvm_conv ON conversation_version_messages(conversation_version_id, seq);
CREATE INDEX IF NOT EXISTS ix_cvm_msg ON conversation_version_messages(message_version_id);

CREATE TABLE IF NOT EXISTS message_version_sightings (
    sighting_id      TEXT PRIMARY KEY,
    version_id       TEXT NOT NULL REFERENCES message_versions(version_id),
    source_record_id TEXT NOT NULL REFERENCES source_records(record_id),
    batch_id         TEXT NOT NULL REFERENCES import_batches(batch_id),
    observed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msgsight_version ON message_version_sightings(version_id);

-- Attachments and references carried by a message.
CREATE TABLE IF NOT EXISTS attachments (
    attachment_id      TEXT PRIMARY KEY,
    message_version_id TEXT NOT NULL REFERENCES message_versions(version_id),
    conversation_id    TEXT NOT NULL REFERENCES conversations(conversation_id),
    kind               TEXT NOT NULL,   -- file | image | audio | link | citation | tool_output
    name               TEXT,
    mime_type          TEXT,
    byte_size          INTEGER,
    source_uri         TEXT,
    source_file_id     TEXT,
    blob_sha256        TEXT REFERENCES source_blobs(sha256),  -- NULL when content was not captured
    content_present    INTEGER NOT NULL DEFAULT 0,
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attach_msgver ON attachments(message_version_id);

-- ------------------------------------------------------------ classification
CREATE TABLE IF NOT EXISTS security_class_history (
    entry_id       TEXT PRIMARY KEY,
    subject_type   TEXT NOT NULL,       -- conversation | message
    subject_id     TEXT NOT NULL,
    previous_class TEXT,
    security_class TEXT NOT NULL,       -- Normal | Private | Restricted | Sacred
    rationale      TEXT,
    set_by         TEXT,
    set_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_secclass_subject ON security_class_history(subject_type, subject_id);

-- --------------------------------------------------------------- operations
CREATE TABLE IF NOT EXISTS operation_log (
    op_id       TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    operation   TEXT NOT NULL,
    actor       TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    network_unsealed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_oplog_at ON operation_log(at);

CREATE TABLE IF NOT EXISTS integrity_runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    suite             TEXT NOT NULL,   -- raw_layer | reconstruction | completeness | full
    passed            INTEGER NOT NULL DEFAULT 0,
    checks_total      INTEGER NOT NULL DEFAULT 0,
    checks_failed     INTEGER NOT NULL DEFAULT 0,
    archive_fingerprint TEXT NOT NULL, -- merkle root over source record hashes
    report_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_integrity_suite ON integrity_runs(suite, finished_at);

CREATE TABLE IF NOT EXISTS evidence_packets (
    packet_id       TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    recipient       TEXT NOT NULL,      -- e.g. Grace | Grok
    purpose         TEXT,
    max_security_class TEXT NOT NULL,
    selection_json  TEXT NOT NULL DEFAULT '{}',
    conversation_count INTEGER NOT NULL DEFAULT 0,
    message_count   INTEGER NOT NULL DEFAULT 0,
    packet_path     TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    acknowledgement TEXT
);

-- --------------------------------------------------------- full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    content_text,
    title,
    role UNINDEXED,
    message_id UNINDEXED,
    version_id UNINDEXED,
    conversation_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
    title,
    conversation_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
