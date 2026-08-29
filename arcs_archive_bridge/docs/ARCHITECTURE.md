# Architecture

## Two layers, two database files

```
                    capture / export / compliance API
                                   |
                          [ credential gate ]        refuses anything carrying auth material
                                   |
   LAYER 1  ------------------------------------------------------------------
   SOURCE            exact bytes -> SHA-256 -> content-addressed blob store
                                   |
                          append-only source record        (never deleted, never rewritten)
                                   |
                          normalisation (lossless)
                                   |
                     conversations / messages / versions / attachments
                                   |
                     [ verification: hashing, recovery, completeness ]
                                   |
                          =====  THE GATE  =====
   LAYER 2  ------------------------------------------------------------------
   DERIVED           propositions · discovery graph · embeddings · analyst notes
```

Layer 1 lives in `db/source.sqlite3` plus `source/blobs/`.
Layer 2 lives in `db/derived.sqlite3`.

They are separate **files**, not separate table prefixes. The requirement was to
keep derived interpretations apart from source records; a naming convention
would satisfy the letter of that and fail the first time someone wrote a join in
a hurry. Two files make the separation physical: you can delete the derived
database, rebuild the archive's indexes from source, and lose no source material
at all. A test asserts exactly that.

## The pipeline, in order

The order is the design. Each step refuses to run until the previous one has
succeeded.

1. **Adapter reads the input.** Returns the exact bytes plus a parsed structure.
   Adapters never mutate their source and never fetch anything.
2. **Credential gate.** The whole container is scanned. Anything shaped like
   session material fails ingestion closed, before a single byte is stored.
   Secrets *inside conversation content* are a different case - see below.
3. **Blob store.** The exact bytes are hashed and written read-only. Identical
   bytes are stored once. Files that shipped with the conversations - the
   attachments beside `conversations.json` in an export - go through the same
   store and get their own source records, so a photograph is archived on
   exactly the terms a message is.
4. **Source record.** One append-only row per capture event, carrying adapter,
   adapter version, source method, source URI, extraction date, ingestion date,
   the byte span inside its container, and a link to the earlier record with the
   same bytes if there is one.
5. **Normalisation.** Conversations and messages are projected into relational
   form. The complete original object is retained alongside the projection.
6. **Versioning.** Content hashes decide: identical content records a *sighting*;
   changed content appends a *version* that supersedes the previous one.
7. **Indexing.** FTS5 is updated for current message versions only.

## Adapters

An adapter turns some source into `CapturedConversation` objects plus the bytes
they came from. Everything downstream is adapter-agnostic, which is the whole
point: when the Enterprise Compliance API becomes available, UI-assisted capture
is replaced by changing one word on the command line, and the existing archive
keeps working, because conversations are keyed by their provider ID and a
better-quality capture of the same conversation simply appends a version.

| Adapter | Source method | Fidelity |
|---|---|---|
| `chatgpt_export` | `official_export` | Provider's own JSON. Byte spans recorded per conversation. |
| `ui_capture` | `ui_assisted_capture` | Provider JSON where the browser can get it, DOM otherwise (marked as such). |
| `compliance_api` | `enterprise_compliance_api` | Provider's own JSON, NDJSON or array, per record byte spans. |

### Attachments

An export is a *directory*, not a file: `conversations.json` plus the uploads and
generated images beside it. The export adapter treats the directory as the
container and archives every file in it, then links files to messages by
provider id - matching `file-AbC123` against `file-AbC123-original-name.png`, and
resolving `file-service://` and `sediment://` asset pointers to the same ids.

Three rules keep that honest:

- **Every file is archived, matched or not.** A file no message references is
  still source material; it is stored and reported as unreferenced rather than
  dropped.
- **Export metadata is not an attachment.** `user.json`, `message_feedback.json`
  and the rest are the export describing itself, and are skipped.
- **What is not held says so.** An attachment the capture could not reach keeps
  its name, type, size and identifier with `content_present = 0`. `arcs files`
  lists them. The archive never implies it holds a file it does not.

UI capture can also carry file bytes inline as base64, but only for assets it
can fetch same-origin with the session the page already has. That path stores
each file three times over - in the captured bundle, in the retained original
that makes the bundle rebuildable, and as the extracted file - so the export
directory is the better source for files.

Writing another one means implementing `sniff()` and `read()`, and honouring
three rules: return the exact source bytes, retain every field you saw, and
never touch authentication material.

## Why the original object is retained twice over

Each message version stores the complete original node object it came from, not
just the fields the schema has columns for. That costs disk and buys two things:

- **Reconstruction without the blob store.** The original file can be rebuilt
  from the normalised tables alone. Two independent recovery paths mean a bug in
  either one is visible instead of silent.
- **Forward compatibility.** When the provider adds a field the schema does not
  know about, it is already archived. The schema can catch up later; the data is
  not lost in the meantime.

## Secrets in content versus credentials at the boundary

These look similar and must be handled oppositely.

- A **cookie or token carried by the capture tool** means the tool is doing
  something the archive forbade. Ingestion fails closed, and the offending file
  is *not* copied into the archive - only a report naming the finding types.
- A **secret the user pasted into a conversation** is source material. Dropping
  it would violate the archive's first duty. It is ingested, flagged, and the
  conversation is escalated to `Restricted` so it cannot leave in an evidence
  packet without an explicit acknowledgement.

## The gate

`gate.authorise()` returns a token only when the most recent full verification
run passed, every required check is present in it, and the archive fingerprint
recorded by that run still matches the archive's current fingerprint. The
fingerprint is a digest over every source record and the bytes it points at, so
ingesting anything closes the gate until `arcs verify` is run again.

Every interpretation records the gate run and fingerprint that licensed it. A
claim can therefore always name the verification that stood behind it, and that
verification can be recomputed.

## Identifiers

- Conversations, messages, versions, propositions: **UUIDv5** derived from
  stable inputs, so the same material imported on another machine gets the same
  identifier, and Obsidian links survive re-imports and retitles.
- Batches, source records, log entries, packets: **random**, because they are
  events, and two identical-looking events are still two events.
```
conversation_id  = uuid5(ARCS, "conversation" + source_system + source_conversation_id)
message_id       = uuid5(ARCS, "message" + conversation_id + source_message_id)
proposition_id   = uuid5(ARCS, "proposition" + message_version_id + span + quote)
```
