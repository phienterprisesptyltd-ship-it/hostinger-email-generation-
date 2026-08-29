# What is deliberately not built yet

The instruction was explicit: *do not build automated interpretation until raw
ingestion, provenance, hashing, recovery and completeness tests are working.*
This file records what that ruled out, so the boundary stays visible.

## Built and verified

- Ingestion through three interchangeable adapters
- Content-addressed, hash-verified storage of exact source bytes
- Append-only source records with full provenance
- Deduplication that keeps every capture event
- Lossless normalisation, proven by two independent reconstruction paths
- Attachment acquisition from an export directory, with per-file hashing,
  provenance, recovery and inclusion in evidence packets
- Versioning and corrections that never overwrite
- 23-check verification suite with tamper-evidence snapshots
- Security classes enforced on every export path
- Local full-text search and an optional local semantic index
- Obsidian projection with stable IDs, message anchors and backlinks
- Read-only, hash-verified evidence packets
- The interpretation gate
- ARCS Archaeology: proposition schema, manual recording, corrections,
  rule-based deterministic extraction, and the directed discovery graph

## Deliberately not built

**Automated interpretation.** No model fills `later_interpretation`, proposes
`contemporaneous_meaning`, judges `verification_status`, or asserts an edge in
the discovery graph. The extractor is rule-based, deterministic and names the
rule behind every candidate. The schema is ready for model-generated rows -
`created_by`, `method` and `interpretation_run_id` exist precisely so that
machine-generated material can never be mistaken for a human judgement - but
nothing writes them yet, and nothing should until the raw layer has been
exercised against a real archive rather than a five-conversation fixture.

**Entity resolution across conversations.** Deciding that two spellings are one
person is exactly the kind of judgement that should not be automated before the
provenance layer is trusted. The facet tables record candidates; nothing merges
them.

**Fetching attachments from the provider on demand.** Files are acquired from an
export directory, and from a UI capture where the browser can reach them
same-origin. What is *not* built is asking ChatGPT's file endpoint for a file the
archive knows about but does not hold - that needs an access token, which is what
the design refuses. `arcs files --missing` lists exactly what falls in that gap;
a fresh export, or the Compliance API, is how to close it.

**A user interface.** The Obsidian projection is the reading surface on purpose:
it is a directory of plain Markdown that outlives this program.

**Encryption at rest, backup, sync.** See [THREAT-MODEL.md](THREAT-MODEL.md);
these belong to the platform, not to the archive.

## Next, in order

1. **Run it against the real archive.** Five fixture conversations prove the
   mechanism, not the shape of real data. Expect unknown content types, missing
   timestamps, larger exports, and attachments the fixture does not cover. The
   verification suite is designed to make those visible on ingestion.
2. **Arrange Compliance API access.** It removes the browser from the loop
   entirely and gives the archive provider-grade provenance. The adapter is
   already written and tested against the format.
3. **Record real propositions by hand.** The columns were specified from a
   research practice; using them against actual material is what will show
   whether `arc`, `probability_role` and `verification_status` carry the
   distinctions the work needs, before any automation is pointed at them.
4. **Only then, consider assisted interpretation** - and when you do, make it
   propose rows for review rather than write them, keep `created_by` honest, and
   leave the gate where it is.
