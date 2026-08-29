-- ARCS Archive Bridge - derived layer schema (archive/db/derived.sqlite3)
--
-- A physically separate database file.  Everything here is an interpretation,
-- an index, or a research artefact.  Deleting this file loses no source
-- material: it can be rebuilt from source.sqlite3 (though hand-authored
-- propositions and analyst notes would be lost, so it is backed up too).
--
-- Rows in this file anchor to the source layer by id *and* by hash, so a claim
-- can always be re-verified against the exact bytes it came from.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS derived_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Records that the raw layer was verified before any interpretation ran.
CREATE TABLE IF NOT EXISTS interpretation_runs (
    run_id              TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    kind                TEXT NOT NULL,      -- extraction | linking | embedding | summary
    method              TEXT NOT NULL,      -- rule_based:v1 | human | model:<name>
    gate_run_id         TEXT NOT NULL,      -- integrity_runs.run_id that authorised this
    gate_fingerprint    TEXT NOT NULL,      -- archive fingerprint at authorisation time
    params_json         TEXT NOT NULL DEFAULT '{}',
    produced_count      INTEGER NOT NULL DEFAULT 0,
    notes               TEXT
);

-- ------------------------------------------------------------- propositions
-- One row per atomic proposition identity.  Content lives in versions so that
-- corrections append rather than overwrite.
CREATE TABLE IF NOT EXISTS propositions (
    proposition_id     TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL,       -- -> source.conversations.conversation_id
    message_id         TEXT NOT NULL,       -- -> source.messages.message_id
    created_at         TEXT NOT NULL,
    current_version_id TEXT,
    retracted_at       TEXT,                -- retraction is recorded, never deleted
    retracted_reason   TEXT
);
CREATE INDEX IF NOT EXISTS ix_prop_conv ON propositions(conversation_id);
CREATE INDEX IF NOT EXISTS ix_prop_msg ON propositions(message_id);

CREATE TABLE IF NOT EXISTS proposition_versions (
    version_id              TEXT PRIMARY KEY,
    proposition_id          TEXT NOT NULL REFERENCES propositions(proposition_id),
    version_no              INTEGER NOT NULL,

    -- anchors into the source layer
    conversation_id         TEXT NOT NULL,
    source_conversation_id  TEXT,
    message_id              TEXT NOT NULL,
    source_message_id       TEXT,
    message_version_id      TEXT NOT NULL,
    source_record_id        TEXT NOT NULL,
    source_sha256           TEXT NOT NULL,   -- hash of the source bytes behind the quote

    -- the required proposition columns
    event_date              TEXT,            -- date the proposition is *about*
    message_date            TEXT,            -- date the message was written
    quote_text              TEXT NOT NULL,   -- exact quotation, verbatim
    quote_char_start        INTEGER,
    quote_char_end          INTEGER,
    quote_sha256            TEXT NOT NULL,
    entity                  TEXT,
    location                TEXT,
    number_raw              TEXT,
    number_value            REAL,
    number_unit             TEXT,
    arc                     TEXT,            -- ARCS arc / thread this belongs to
    contemporaneous_meaning TEXT,            -- what it meant at the time it was said
    later_interpretation    TEXT,            -- what we think now (explicitly derived)
    prompted_by             TEXT,            -- proposition_id or search_event_id
    prompted_by_kind        TEXT,            -- proposition | search_event | external
    verification_status     TEXT NOT NULL DEFAULT 'unverified',
        -- unverified | quote_verified | corroborated | contradicted | refuted | confirmed
    probability_role        TEXT,            -- assertion | hypothesis | inference | speculation | question
    probability             REAL,            -- 0..1 subjective, optional
    confidence_note         TEXT,

    -- provenance of the interpretation itself
    created_by              TEXT NOT NULL,   -- human:<name> | rule_based:v1 | model:<name>
    interpretation_run_id   TEXT REFERENCES interpretation_runs(run_id),
    method                  TEXT,
    correction_reason       TEXT,
    supersedes_version_id   TEXT REFERENCES proposition_versions(version_id),
    is_current              INTEGER NOT NULL DEFAULT 1,
    content_sha256          TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    UNIQUE (proposition_id, version_no)
);
CREATE INDEX IF NOT EXISTS ix_propver_current ON proposition_versions(proposition_id, is_current);
CREATE INDEX IF NOT EXISTS ix_propver_arc ON proposition_versions(arc, is_current);

-- Multi-valued facets, kept alongside the single-value columns above.
CREATE TABLE IF NOT EXISTS proposition_facets (
    facet_id        TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL REFERENCES proposition_versions(version_id),
    facet_type      TEXT NOT NULL,   -- entity | location | number | date | arc | term
    value_text      TEXT NOT NULL,
    value_number    REAL,
    value_unit      TEXT,
    char_start      INTEGER,
    char_end        INTEGER,
    extractor       TEXT
);
CREATE INDEX IF NOT EXISTS ix_facet_version ON proposition_facets(version_id);
CREATE INDEX IF NOT EXISTS ix_facet_type ON proposition_facets(facet_type, value_text);

-- ----------------------------------------------------- discovery graph nodes
-- An observation or search performed by the researcher: the thing that
-- generated a subsequent discovery.
CREATE TABLE IF NOT EXISTS search_events (
    search_event_id TEXT PRIMARY KEY,
    occurred_at     TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- archive_search | web_search | archive_read | field_visit | conversation
    query           TEXT,
    tool            TEXT,
    conversation_id TEXT,
    message_id      TEXT,
    result_summary  TEXT,
    result_count    INTEGER,
    created_by      TEXT,
    notes           TEXT
);

-- Directed edges: which observation/search generated which discovery.
CREATE TABLE IF NOT EXISTS discovery_edges (
    edge_id      TEXT PRIMARY KEY,
    from_kind    TEXT NOT NULL,      -- proposition | search_event | conversation | message
    from_id      TEXT NOT NULL,
    to_kind      TEXT NOT NULL,
    to_id        TEXT NOT NULL,
    relation     TEXT NOT NULL,      -- prompted | generated | corroborates | contradicts |
                                     -- corrects | cites | answers | derived_from
    weight       REAL,
    rationale    TEXT,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    interpretation_run_id TEXT REFERENCES interpretation_runs(run_id),
    UNIQUE (from_kind, from_id, to_kind, to_id, relation)
);
CREATE INDEX IF NOT EXISTS ix_edge_from ON discovery_edges(from_kind, from_id);
CREATE INDEX IF NOT EXISTS ix_edge_to ON discovery_edges(to_kind, to_id);

-- --------------------------------------------------------- semantic indexing
CREATE TABLE IF NOT EXISTS embeddings (
    embedding_id  TEXT PRIMARY KEY,
    subject_type  TEXT NOT NULL,      -- message | conversation | proposition
    subject_id    TEXT NOT NULL,      -- the *version* id, so vectors track corrections
    model_name    TEXT NOT NULL,
    model_kind    TEXT NOT NULL,      -- local_hashing | sentence_transformers
    dim           INTEGER NOT NULL,
    vector        BLOB NOT NULL,      -- float32, little-endian
    norm          REAL NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (subject_type, subject_id, model_name)
);
CREATE INDEX IF NOT EXISTS ix_emb_subject ON embeddings(subject_type, model_name);

-- Analyst notes: human commentary that is explicitly *not* source material.
CREATE TABLE IF NOT EXISTS analyst_notes (
    note_id        TEXT PRIMARY KEY,
    subject_type   TEXT NOT NULL,
    subject_id     TEXT NOT NULL,
    body           TEXT NOT NULL,
    author         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    supersedes_note_id TEXT REFERENCES analyst_notes(note_id)
);
CREATE INDEX IF NOT EXISTS ix_note_subject ON analyst_notes(subject_type, subject_id);

CREATE VIRTUAL TABLE IF NOT EXISTS proposition_fts USING fts5(
    quote_text,
    contemporaneous_meaning,
    later_interpretation,
    entity,
    location,
    arc,
    proposition_id UNINDEXED,
    version_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
