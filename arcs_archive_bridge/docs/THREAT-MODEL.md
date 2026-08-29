# Threat model

What this design protects against, and - just as important - what it does not.

## What it protects against

**Accidental upload.** Outbound sockets are blocked at the socket layer for the
life of the process, so a mistake in an adapter, a dependency, or a future
contributor's code raises a loud error instead of leaking an archive. There is
no upload command. Loopback is still permitted so local tooling works.

**Credential capture.** The bridge never has an opportunity to store ChatGPT
authentication material: the capture script reads no cookies and requests no
token, and every byte at the ingestion boundary is scanned. A bundle carrying
session material is refused, and the refused file is *not* copied into the
archive - only a report naming the finding types.

**Silent lossy normalisation.** The commonest failure mode of an archive is that
it quietly keeps less than it seems to. Two independent reconstruction paths,
verified on every run, make that failure visible.

**Silent mutation.** Blobs are read-only and re-hashed on every read. Source
records are append-only, and each verification run snapshots them so the next
run detects deletion or repointing. Corrections append; nothing is updated in
place.

**Interpretation contaminating the record.** Interpretations live in a separate
database file, checked by the suite. Propositions keep contemporaneous meaning
and later interpretation in different columns. Automated extraction cannot run
until the raw layer has verified, and records which verification licensed it.

**Accidental disclosure of sensitive material.** Security classes are enforced
on every export path, and the packet builder refuses rather than silently
redacting.

**Undetected tampering with a delivered packet.** Packets are read-only, every
file is listed in `CHECKSUMS.sha256`, and the source files carry the archive's
own recorded hashes so a recipient can check the transcript against the source.

## What it does not protect against

**A local attacker with write access to the archive directory.** They can edit
the SQLite files, rewrite the blobs, and regenerate the integrity snapshots.
Tamper *evidence* is not tamper *proofing*. If you need the stronger property,
put the archive on append-only or WORM storage, sign verification reports with
a key held elsewhere, or publish the archive fingerprint somewhere you do not
control. The fingerprint is designed for that: it is a single digest over every
source record, cheap to publish and cheap to re-check.

**Disk failure.** There is no backup mechanism here. The archive is a directory:
back it up like one. Because blobs are content-addressed and read-only,
incremental backup is efficient and safe.

**Encryption at rest.** Not implemented. Use full-disk encryption or an
encrypted volume. Adding a bespoke encryption layer would create key-loss risk
without adding much over the platform's own.

**A hostile capture script.** The guarantees about cookies and tokens are
properties of *the script shipped in this repository*, asserted by the test
suite against that file. Paste a different script into your browser and you are
outside the model. Read `browser_capture/arcs-ui-capture.user.js` before running
it; it is deliberately short enough to read.

**Malicious content in an archived conversation.** Message text is stored and
rendered verbatim, including any markdown, HTML or prompt-injection attempt it
contains. The archive treats it as data. Anything downstream that feeds archived
text to a model should treat it as untrusted input - which is the reason
evidence packets separate `source/` from `derived/` so plainly.

**The provider's own record.** The archive can prove what it captured and when.
It cannot prove that what it captured was complete at the source, or that the
provider's copy has not since changed. The Enterprise Compliance API path
narrows that gap; UI-assisted capture does not close it.

**Metadata the source never had.** Where a timestamp or title is absent, it is
recorded as absent. The archive never fills a gap with a guess, and the
verification suite reports the count of such gaps rather than hiding them.

## Assumptions

- The machine running the bridge is trusted and under your control.
- The browser session used for UI-assisted capture is your own, and you are
  entitled to the material you are capturing.
- The operator is the person deciding what may be released, and is accountable
  for the acknowledgements they record.
