# ARCS Archive Bridge

A local application that builds a **privacy-preserving, provenance-complete
archive** of ChatGPT Business conversation history and the ARCS research that
goes with it.

Its first duty is to **preserve raw source material before interpretation**.
Everything else in the design follows from that: the exact bytes are stored and
hashed first, normalisation is proven lossless, corrections append rather than
overwrite, and no interpretation may run until the raw layer has been verified.

```
arcs init ~/arcs-archive
arcs ingest ~/Downloads/conversations.json     # or an ARCS capture bundle
arcs verify                                    # provenance, hashing, recovery, completeness
arcs recover --out ./originals                 # the exact original bytes, back out
arcs project                                   # Obsidian vault
arcs packet --recipient Grace --purpose "independent reading"
```

No dependencies beyond the Python standard library. Nothing is uploaded, ever.

---

## The guarantees, and how each one is enforced

| Requirement | How it is enforced | Where |
|---|---|---|
| Runs locally | A single directory plus two SQLite files. No server, no account, no service. | `config.py`, `db.py` |
| Never uploads archive contents | Outbound sockets are **blocked at the socket layer** for the life of the process. There is no upload command; a future one would have to unseal the guard with a stated, logged reason. | `netguard.py` |
| Never stores ChatGPT cookies or tokens | The capture script never reads the cookie jar and never requests an access token; every byte at the ingestion boundary is scanned, and a bundle carrying credentials is **refused without being copied into the archive**. | `credentials.py`, `browser_capture/` |
| Uses your normal authenticated browser session | The capture script runs in your signed-in browser and issues same-origin `GET`s; the browser attaches the session, so the archive never holds it. | `browser_capture/arcs-ui-capture.user.js` |
| ChatGPT is read-only | The script makes no `POST`/`PUT`/`PATCH`/`DELETE` call of any kind. A test asserts this against the shipped file. | `tests/test_bridge.py::TestCaptureScript` |
| Modular import adapters | Adapters are registered by name and are interchangeable. `compliance_api` already reads the Enterprise Compliance export format; swapping it in is one flag. | `adapters/` |
| Retains the original representation | The complete original object for every conversation *and* every message is retained, including branches that are off the current path. | `adapters/chatgpt_export.py` |
| Normalises into SQLite | Conversations, messages, versions, attachments, sightings, classifications. | `schema_source.sql` |
| Retains title, dates, role, order, exact content, attachments, source method, extraction date | Columns for each, with the original timestamp form kept next to the normalised one. | `schema_source.sql` |
| SHA-256 hashes immutable source records | Content-addressed blob store; every source record carries the hash of its exact bytes. | `blobstore.py`, `hashing.py` |
| Deduplicates without deleting provenance | Identical bytes are stored once; **every capture event still gets its own append-only source record**, and repeat observations are recorded as sightings. | `ingest.py` |
| Keeps corrections and later versions | Versions append and supersede. Nothing is ever updated in place. | `ingest.py`, `versioning` tests |
| Obsidian projection with stable IDs and backlinks | Deterministic note names, `arcs_id` front matter, `^m<n>` message anchors, proposition and arc notes that link back. | `obsidian.py` |
| Local full-text search | SQLite FTS5, offline. | `search.py` |
| Optional local semantic index, no external APIs | A deterministic pure-Python embedder by default; a local `sentence-transformers` directory if you have one. Never downloads. | `semantic.py` |
| Security classes | Normal / Private / Restricted / **Sacred (culturally sensitive)**, with append-only classification history and enforcement on every export path. | `security.py` |
| Read-only evidence packets for Grace and Grok | Hash-verified, read-only directories with verbatim source, transcripts, and a manifest that lists what was withheld and why. | `evidence.py` |
| Derived interpretations separate from source | A **physically separate database file**, checked by the integrity suite. | `schema_derived.sql`, `integrity.py` |
| No automated interpretation until the raw layer works | An **interpretation gate** bound to a passing verification run *and* the archive fingerprint it verified. Ingest new material and the gate closes. | `gate.py` |

---

## Install

Nothing to install. Python 3.10+ with SQLite FTS5 (standard on every current
build) is enough:

```
./arcs --help
```

Or install it properly to get an `arcs` on your `PATH`:

```
pip install -e .
```

The optional semantic backend is the only extra, and it is genuinely optional:

```
pip install -e '.[semantic]'
```

## Try it: the prototype demonstration

```
python3 samples/generate_samples.py      # five sample conversations
./arcs demo --out ./arcs-demo
```

The demo ingests five conversations, re-ingests them to show deduplication,
ingests a later export to show corrections, ingests a UI-capture bundle,
refuses a bundle carrying a session cookie, runs the full verification suite,
and then does the thing everything rests on:

> **it recovers every conversation from the archive and compares it byte-for-byte
> against the untouched original file** - and separately rebuilds the same
> material from the normalised database tables alone.

It then classifies material, searches it, projects it to Obsidian, opens the
interpretation gate, extracts propositions, builds the discovery graph, and
produces evidence packets for Grace and Grok. Exit code 0 means every one of
those checks passed.

## Getting your conversations in

Three ways in, in descending order of fidelity. All three normalise to the same
tables, so you can start with one and move to another without losing anything.

**1. Official ChatGPT data export (recommended).** Settings → Data controls →
Export data. Unzip and point the bridge at it:

```
arcs ingest ~/Downloads/chatgpt-export/conversations.json
```

This is the provider's own serialisation, obtained from your own account, with
no scraping and no credentials. The bridge records the exact byte span each
conversation occupied inside the file, so it can hand back a single
conversation verbatim.

**2. UI-assisted capture.** For anything not yet in an export. Load
`browser_capture/arcs-ui-capture.user.js` in Tampermonkey (or paste it into the
browser console) on a page you are already signed in to, run `arcsCapture()`,
and ingest the file it saves:

```
arcs ingest ~/Downloads/arcs-capture-2026-08-20T09-14-03.json
```

The script never reads your cookies and never asks for an access token. That is
a deliberate limitation with a visible cost: without a token the backend JSON
endpoint usually refuses, so the script falls back to reading the rendered page,
and those captures are marked `fidelity: dom_rendered` rather than being passed
off as provider data.

**3. OpenAI Enterprise Compliance API.** The intended long-term replacement for
(2). Fetch the export with your own tooling - the archive process is
network-sealed and must not hold a compliance credential - then:

```
arcs ingest ./workspace-export.ndjson --adapter compliance_api
```

## Everyday use

```
arcs status                          # counts, gate state, network state
arcs list                            # conversations
arcs show <id> --full                # one conversation
arcs history <id>                    # versions, corrections, sightings
arcs search "karaka stand"           # full-text
arcs semantic index && arcs semantic search "how far did the shoreline move"
arcs classify conversation <id> Sacred --reason "..." --cascade
arcs project --max-class Private     # Obsidian vault
arcs verify                          # re-run the whole suite
arcs recover --out ./originals       # get the exact bytes back
arcs log                             # what this archive has done
```

## The ARCS Archaeology layer

Layer 2 extracts **atomic propositions**, each anchored to one exact quotation
in one message version, with the columns ARCS asked for:

source conversation · source message · date · exact quotation · entity ·
location · number · arc · contemporaneous meaning · later interpretation ·
prompted-by · generated-next · verification status · probability role ·
correction history

The rule the layer exists to protect: **contemporaneous meaning and later
interpretation never share a field.** What a source meant at the time is stored
apart from what we now think it refers to, because a field that holds both will
eventually be read as the record.

```
arcs gate                                        # is interpretation authorised?
arcs arch extract <conversation> --arc "Kaiora boundary"          # dry run
arcs arch extract <conversation> --arc "Kaiora boundary" --commit
arcs arch add <message_version_id> --quote "..." --contemporaneous "..." --later "..."
arcs arch correct <proposition_id> --reason "..." --status corroborated
arcs arch observe --kind archive_read --query "1887 ledger page 14"
arcs arch link --from-kind search_event --from-id <id> \
               --to-kind proposition --to-id <id> --relation generated
arcs arch chain <proposition_id>                 # what led here, what it produced
arcs arch graph --format dot --out graph.dot     # dot -Tsvg -O graph.dot
```

The **discovery graph** is the point: for any discovery you can walk back
through the observations and searches that produced it, and forward to
everything it went on to generate.

Extraction is rule-based and deterministic, never a model: each candidate names
the rule that matched it, and `later_interpretation` is left empty because no
rule can supply it. It runs only through the gate.

## Where things live

```
<archive>/
  archive.json                 configuration
  db/source.sqlite3            source records + normalised conversations
  db/derived.sqlite3           interpretations, embeddings, discovery graph
  source/blobs/                content-addressed exact source bytes (read-only)
  source/incoming/             drop captures here
  projection/obsidian/         regenerable Markdown vault
  packets/                     evidence packets
  logs/integrity/              verification snapshots (tamper evidence)
  quarantine/                  refusal reports - never the refused file itself
```

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - the layers and why they are separate
- [`docs/PROVENANCE.md`](docs/PROVENANCE.md) - hashing, versions, dedupe, reconstruction
- [`docs/SECURITY-CLASSES.md`](docs/SECURITY-CLASSES.md) - the four classes and what they gate
- [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md) - what this protects against, and what it does not
- [`docs/ROADMAP.md`](docs/ROADMAP.md) - what is deliberately not built yet

## Tests

```
python3 -m unittest discover -s tests -t . -v
```

70 tests covering ingestion, provenance, deduplication, versioning,
byte-for-byte reconstruction, tamper detection, the credential boundary, the
network seal, the capture script's own guarantees, security classes, evidence
packets, the projection, search, the gate, propositions, extraction, the
discovery graph and layer separation.

## Status

Prototype, and honest about it. The raw layer is complete and verified against
five conversations. What is deliberately *not* built yet is listed in
[`docs/ROADMAP.md`](docs/ROADMAP.md) - most importantly, automated
interpretation, which stays unbuilt until the raw layer has been exercised
against a real archive rather than a fixture.
