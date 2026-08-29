# Security classes

Four classes, ordered from least to most restricted. The order is meaningful:
everything that can emit material compares against it.

| Class | Meaning | Leaves the archive |
|---|---|---|
| `Normal` | Ordinary material. | Yes, by default. |
| `Private` | Personal. | Only when the operation explicitly asks for `--max-class Private`. |
| `Restricted` | Legally or commercially sensitive; also anything found to contain a pasted secret. | Requires `--max-class Restricted` **and** a recorded acknowledgement. |
| `Sacred` | Culturally sensitive / sacred material - wāhi tapu, urupā locations, knowledge given in confidence. | Never by default. Requires `--max-class Sacred` **and** a named acknowledgement, recorded in the packet manifest and the operation log. |

## Where a class comes from

- **At ingestion.** `--security-class` sets the default for everything in that
  import. A conversation whose content contains credential-shaped material is
  escalated to `Restricted` automatically, with the reason recorded.
- **By hand.** `arcs classify conversation <id> Sacred --reason "..." --cascade`.
- **Never downward silently.** Re-ingesting material that is already classified
  keeps the more restrictive class; classification is merged upward, never reset
  by a fresh import.

A message is never less restricted than the conversation it sits in. The
effective class is the more restrictive of the two.

## Classification is append-only

Every change writes a row to `security_class_history` with the previous class,
the new class, the rationale, who set it, and when. The current class lives on
the row for speed; the history is the record. The verification suite checks that
every non-default classification has a rationale behind it.

## What the class actually gates

**Search.** `arcs search --max-class Normal` filters by joining back to the
authoritative rows, not by trusting a copy inside the index - so reclassifying a
conversation takes effect immediately and a stale index cannot defeat it. The
result reports how many matches were withheld, without showing them.

**The Obsidian projection.** `arcs project --max-class Private` writes a **stub
note** for anything above the limit: the conversation's existence, provenance,
hashes and class are recorded; its content is not. Existence is usually not the
secret, and a silently missing note is worse than a visible withholding. If it
is, project a narrower selection.

**Evidence packets.** The strongest enforcement:

- material above the packet's class is excluded, and each exclusion is listed in
  `MANIFEST.json` with its reason, so the recipient knows the packet is partial;
- `Restricted` and `Sacred` **require an acknowledgement string**. Without one
  the build refuses outright rather than quietly emitting a redacted packet that
  looks complete;
- the acknowledgement is stored in the manifest, in `evidence_packets`, and in
  the operation log.

```
arcs packet --recipient Grace --max-class Normal
arcs packet --recipient Grok --max-class Sacred \
    --acknowledge "Released with the agreement of the kaumātua who gave it, for the purpose of X, on <date>."
```

**Propositions.** A quotation is never surfaced from material the projection is
withholding: proposition notes for a withheld conversation are not written.

## The Sacred class specifically

The design assumption is that sacred and culturally sensitive material has
custodians who are not the archive's operator, and that the archive's job is to
make the restriction *structural* rather than a note someone might not read.

That is why:

- the restriction is a property of the record, checked by every export path,
  rather than a warning in the text;
- release requires a written acknowledgement that is permanently recorded;
- an export that cannot express the restriction refuses to run rather than
  emitting something that looks complete;
- the projection records the existence and provenance of withheld material so
  its absence is visible, not silent.

The archive cannot decide what is appropriate to release. It can make sure the
decision is deliberate, attributed and recorded.
