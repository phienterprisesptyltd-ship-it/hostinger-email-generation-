# Provenance

## What is recorded about every piece of material

For each **capture event** (`source_records`, append-only):

| Field | Meaning |
|---|---|
| `blob_sha256` | SHA-256 of the exact bytes |
| `source_method` | `official_export` / `ui_assisted_capture` / `enterprise_compliance_api` |
| `adapter`, `adapter_version` | what parsed it, and which version of that parser |
| `record_kind` | `container` (the whole file) / `conversation` / `attachment` |
| `source_uri` | where it came from - never with credentials |
| `extraction_date` | when the material was taken from the source |
| `ingested_at` | when this archive recorded it |
| `parent_record_id`, `byte_start`, `byte_end` | the exact span inside its container |
| `duplicate_of_record_id` | the earlier record with identical bytes, if any |
| `batch_id` | the import run |

Attachment files get their own records of `record_kind = 'attachment'`, parented
to the container they arrived in and inheriting its adapter, source method and
extraction date - because that is where they came from. The `attachments` row
links to that record, so a file's provenance is a query away from the message
that carried it.

For each **conversation version**: title, create/update time (normalised *and*
in the source's original form), model, message count, content hash, source hash,
the retained original envelope, what it supersedes, and why.

For each **message version**: order, role, author name, timestamps (both forms),
content type, exact text, the exact original parts, the complete original node,
whether it sits on the conversation's current path, and its branch note.

## Two hashes, two jobs

- **Blob hash** - SHA-256 over the exact source bytes. This is what makes a
  record immutable and recoverable. It is never computed over a re-serialisation.
- **Content hash** - SHA-256 over a canonical JSON encoding of a normalised
  structure. Used to decide whether something changed. Never a substitute for
  the blob hash.

## Deduplication without losing provenance

Ingest the same export twice and you get:

- **one** blob (identical bytes are stored once);
- **two** source records - the second one links to the first via
  `duplicate_of_record_id` and is never deleted;
- **no** new conversation version - instead a row in
  `conversation_version_sightings` recording that this content was observed again,
  by which capture, at what time.

So "how many times have I seen this, and when, and by what method" survives
deduplication. That is the difference between deduplicating an archive and
deduplicating a download folder.

## Corrections and later versions

Nothing is ever updated in place.

```
conversation_versions:  v1 (is_current=0) <-- superseded by -- v2 (is_current=1)
message_versions:       v1 (is_current=0) <-- superseded by -- v2 (is_current=1)
proposition_versions:   v1 (is_current=0) <-- superseded by -- v2 (is_current=1)
```

A later capture of the same conversation appends a version and records
`correction_reason`. Messages are versioned independently: a conversation that
gained two messages does not re-version the twenty that did not change, and
`conversation_version_messages` records exactly which message versions compose
each conversation version, so any historical version rebuilds exactly.

Propositions work the same way, and a correction *requires* a reason - the
reason is part of the record, not metadata about it.

## Reconstruction: two independent paths

**Path A - the blob store.** Read the bytes back by hash, re-hash, compare. Does
not touch the normalised tables.

**Path B - the normalised tables.** Rebuild the original object from the
retained envelope, mapping order and per-message original nodes. Does not touch
the blob store.

A conversation counts as recoverable only when both succeed. Path A alone would
prove the bytes survived; path B alone would prove normalisation kept
everything; together they prove the archive is not silently lossy in either
direction.

```
arcs reconstruct --all-versions     # check both paths for every version
arcs recover --out ./originals      # write the exact bytes back to disk
```

`arcs recover` writes a `RECOVERY-MANIFEST.json` recording, for every file, its
hash, its adapter, its source method, its extraction date and its byte span in
the original container - and whether the bytes it just wrote verified.
Attachments come back too, under `attachments/<conversation>/`, byte-for-byte
identical to the files that shipped in the export.

Acquiring files for conversations already in the archive is a *correction*, not
an overwrite: the message fingerprint includes the hash of each attachment's
bytes, so ingesting an export directory after ingesting only its JSON appends a
new version. The archive gains; nothing is replaced.

The reverse case needs care. A later capture often carries an attachment's
metadata but not its bytes - a fileless export, a DOM capture. Left alone, that
version would supersede the one that had the file and the archive would report
holding no file while holding it, hashed, in the blob store. So the linkage is
**carried forward**: a new attachment row re-points at bytes the archive already
has, matched on the provider's file id (or the filename within the same
conversation), and records that it did so in
`metadata_json.linked_from_earlier_capture`.

Two limits keep that honest. It only ever re-links bytes already in the archive -
an archive that never saw the files still shows references, and a test asserts
it. And it is deliberately not part of the message fingerprint: what a capture
actually contained remains the basis of versioning, so re-ingesting the same
input is still a no-op.

## Tamper evidence

Every verification run writes a snapshot of `(record_id -> blob_sha256)` under
`logs/integrity/`. The next run compares against it: a source record that has
disappeared, or now points at different bytes, is a failed check with the record
named. Blobs are stored read-only, and every read re-verifies the hash.

This detects accidental damage and casual editing. It is not proof against a
determined local attacker with write access to the whole directory - see
[THREAT-MODEL.md](THREAT-MODEL.md).

## What the verification suite checks

23 checks in five groups:

- **blob store** - present, hash-verified
- **source records** - every record has its blob; duplicates link correctly and
  are retained; adapter, method and extraction date present on every record
- **structure** - one current version per subject, contiguous version numbers,
  intact supersession chains, message membership recorded, order strictly
  increasing, required fields and retained originals present
- **recovery** - every version recovers byte-for-byte *and* rebuilds from tables
- **layer separation and quotations** - no interpretation tables in the source
  database; every proposition's quotation still matches the source it cites
