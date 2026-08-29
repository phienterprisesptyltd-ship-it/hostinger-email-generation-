"""Read-only evidence packets for external analysis.

A packet is a self-contained, hash-verified directory (and matching zip) that a
collaborator - Grace, Grok, a reviewer, a court - can read without access to the
archive.  Three properties make it evidence rather than an export:

* **Verbatim source is included.**  Every conversation ships as the exact
  original bytes, with its SHA-256, so the recipient can check the readable
  transcript against the source rather than trusting it.
* **Nothing is writable.**  Files are written read-only, the manifest is hashed,
  and ``CHECKSUMS.sha256`` covers every file in the packet.  Tampering is
  detectable with ``sha256sum -c``.
* **Interpretation is quarantined.**  Derived material, if included at all, sits
  in its own ``derived/`` directory with its own README, never mixed with
  source.

Security classes are enforced at build time.  Restricted and Sacred material
requires an explicit acknowledgement string that is recorded in the manifest and
in the archive's operation log; without it the build refuses rather than
silently emitting a redacted packet that looks complete.
"""

from __future__ import annotations

import json
import os
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import integrity
from .blobstore import BlobStore
from .errors import SecurityClassViolation
from .hashing import merkle_root, sha256_bytes, sha256_file
from .security import REQUIRES_ACKNOWLEDGEMENT, effective_class, parse_class, permits
from .util import new_id, slugify, utcnow

KNOWN_RECIPIENTS = ("Grace", "Grok")


@dataclass
class PacketResult:
    packet_id: str
    path: str
    zip_path: str
    recipient: str
    max_security_class: str
    conversations: int = 0
    messages: int = 0
    propositions: int = 0
    withheld: list = field(default_factory=list)
    manifest_sha256: str = ""
    files: int = 0

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        return data


def _freeze(path: Path) -> None:
    """Make a file read-only for everyone."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:  # pragma: no cover - platform dependent
        pass


def _select(archive, conversation_ids=None, arc=None, query=None) -> list:
    if conversation_ids:
        placeholders = ",".join("?" * len(conversation_ids))
        return archive.all(
            "SELECT c.conversation_id FROM conversations c WHERE c.conversation_id IN (%s) "
            "OR c.source_conversation_id IN (%s)" % (placeholders, placeholders),
            tuple(conversation_ids) * 2,
        )
    if arc:
        ids = {
            r["conversation_id"] for r in archive.all(
                "SELECT DISTINCT conversation_id FROM proposition_versions "
                "WHERE is_current=1 AND arc=?", (arc,), conn=archive.derived,
            )
        }
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return archive.all(
            "SELECT conversation_id FROM conversations WHERE conversation_id IN (%s)"
            % placeholders, tuple(ids),
        )
    if query:
        from .search import search_messages

        hits = search_messages(archive, query, limit=500)
        ids = sorted({h.conversation_id for h in hits.hits})
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return archive.all(
            "SELECT conversation_id FROM conversations WHERE conversation_id IN (%s)"
            % placeholders, tuple(ids),
        )
    return archive.all("SELECT conversation_id FROM conversations ORDER BY first_seen_at")


def build_packet(archive, recipient: str, conversation_ids=None, arc=None, query=None,
                 max_class: str = "Normal", purpose: str = "", acknowledgement: str = "",
                 out_dir=None, include_propositions: bool = False,
                 include_zip: bool = True) -> PacketResult:
    max_class = parse_class(max_class)
    if max_class in REQUIRES_ACKNOWLEDGEMENT and not acknowledgement:
        raise SecurityClassViolation(
            "releasing %s material requires --acknowledge \"<statement>\"; the statement "
            "is recorded in the packet manifest and in the archive's operation log. "
            "Refusing to build a packet that would look complete while withholding "
            "material." % max_class
        )

    packet_id = new_id("packet")
    created = utcnow()
    root = Path(out_dir or archive.config.packets_dir) / (
        "%s-%s-%s" % (slugify(recipient), created[:10], packet_id[-8:])
    )
    (root / "source").mkdir(parents=True, exist_ok=True)
    (root / "readable").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)

    store = BlobStore(archive.config.blobs_dir)
    selected = [r["conversation_id"] for r in _select(archive, conversation_ids, arc, query)]

    result = PacketResult(packet_id=packet_id, path=str(root), zip_path="",
                          recipient=recipient, max_security_class=max_class)
    conv_rows, msg_rows = [], []

    for conversation_id in selected:
        row = archive.one(
            "SELECT c.*, cv.version_id, cv.version_no, cv.title, cv.create_time, cv.update_time, "
            "cv.model, cv.message_count, cv.source_sha256, sr.adapter, sr.source_method, "
            "sr.extraction_date, sr.source_uri "
            "FROM conversations c "
            "JOIN conversation_versions cv ON cv.conversation_id=c.conversation_id AND cv.is_current=1 "
            "JOIN source_records sr ON sr.record_id = cv.source_record_id "
            "WHERE c.conversation_id=?",
            (conversation_id,),
        )
        if row is None:
            continue
        if not permits(max_class, row["security_class"]):
            result.withheld.append(
                {"conversation_id": conversation_id, "title": row["title"],
                 "security_class": row["security_class"],
                 "reason": "above the packet's %s limit" % max_class}
            )
            continue

        name = "%s--%s" % (slugify(row["title"] or "untitled"), conversation_id[:8])
        raw = store.get(row["source_sha256"])
        source_path = root / "source" / (name + ".json")
        source_path.write_bytes(raw)
        _freeze(source_path)

        messages = archive.all(
            "SELECT mv.*, m.security_class AS message_class FROM conversation_version_messages cvm "
            "JOIN message_versions mv ON mv.version_id = cvm.message_version_id "
            "JOIN messages m ON m.message_id = mv.message_id "
            "WHERE cvm.conversation_version_id=? ORDER BY cvm.seq",
            (row["version_id"],),
        )
        kept = []
        for msg in messages:
            cls = effective_class(row["security_class"], msg["message_class"])
            if not permits(max_class, cls):
                result.withheld.append(
                    {"message_id": msg["message_id"], "security_class": cls,
                     "reason": "message above the packet's %s limit" % max_class}
                )
                continue
            kept.append(msg)

        transcript = _transcript(row, kept)
        readable_path = root / "readable" / (name + ".md")
        readable_path.write_text(transcript, encoding="utf-8")
        _freeze(readable_path)

        conv_rows.append(
            {
                "conversation_id": conversation_id,
                "source_conversation_id": row["source_conversation_id"],
                "title": row["title"],
                "created": row["create_time"],
                "updated": row["update_time"],
                "model": row["model"],
                "security_class": row["security_class"],
                "archive_version": row["version_no"],
                "message_count": len(kept),
                "source_method": row["source_method"],
                "adapter": row["adapter"],
                "extraction_date": row["extraction_date"],
                "source_uri": row["source_uri"],
                "source_sha256": row["source_sha256"],
                "source_file": "source/%s.json" % name,
                "readable_file": "readable/%s.md" % name,
            }
        )
        for msg in kept:
            msg_rows.append(
                {
                    "conversation_id": conversation_id,
                    "message_id": msg["message_id"],
                    "message_version_id": msg["version_id"],
                    "seq": msg["seq"],
                    "role": msg["role"],
                    "author_name": msg["author_name"],
                    "created": msg["create_time"],
                    "created_raw": msg["create_time_raw"],
                    "content_type": msg["content_type"],
                    "content_text": msg["content_text"],
                    "content_sha256": msg["content_sha256"],
                    "on_canonical_path": bool(msg["on_canonical_path"]),
                    "branch_note": msg["branch_note"],
                    "archive_version": msg["version_no"],
                    "security_class": effective_class(row["security_class"], msg["message_class"]),
                }
            )
        result.conversations += 1
        result.messages += len(kept)

    _write_jsonl(root / "data" / "conversations.jsonl", conv_rows)
    _write_jsonl(root / "data" / "messages.jsonl", msg_rows)

    if include_propositions:
        (root / "derived").mkdir(exist_ok=True)
        props = [
            dict(r) for r in archive.all(
                "SELECT * FROM proposition_versions WHERE is_current=1"
                + (" AND arc=?" if arc else ""),
                (arc,) if arc else (), conn=archive.derived,
            )
            if r["conversation_id"] in {c["conversation_id"] for c in conv_rows}
        ]
        _write_jsonl(root / "derived" / "propositions.jsonl", props)
        edges = [dict(r) for r in archive.all("SELECT * FROM discovery_edges",
                                              conn=archive.derived)]
        _write_jsonl(root / "derived" / "discovery_edges.jsonl", edges)
        readme = root / "derived" / "README.md"
        readme.write_text(_derived_readme(), encoding="utf-8")
        _freeze(readme)
        result.propositions = len(props)

    verify_run = integrity.latest_run(archive, "full")
    manifest = {
        "packet_id": packet_id,
        "created_at": created,
        "recipient": recipient,
        "purpose": purpose,
        "produced_by": "ARCS Archive Bridge",
        "archive_name": archive.config.archive_name,
        "archive_fingerprint": integrity.archive_fingerprint(archive),
        "last_verification": (
            {"run_id": verify_run["run_id"], "finished_at": verify_run["finished_at"],
             "passed": bool(verify_run["passed"]), "checks_total": verify_run["checks_total"],
             "checks_failed": verify_run["checks_failed"]}
            if verify_run else None
        ),
        "security": {
            "max_security_class": max_class,
            "acknowledgement": acknowledgement or None,
            "withheld_count": len(result.withheld),
            "withheld": result.withheld,
        },
        "selection": {"conversation_ids": conversation_ids, "arc": arc, "query": query},
        "counts": {
            "conversations": result.conversations,
            "messages": result.messages,
            "propositions": result.propositions,
        },
        "contents": {
            "source/": "exact original bytes, one file per conversation (authoritative)",
            "readable/": "Markdown transcripts rendered from the archive (convenience)",
            "data/": "normalised conversation and message rows as JSON Lines",
            "derived/": ("interpretations, kept separate from source"
                         if include_propositions else "absent: no interpretations included"),
        },
        "verification": (
            "Run `sha256sum -c CHECKSUMS.sha256` in this directory. Each entry in "
            "data/conversations.jsonl carries the SHA-256 of its source file; those "
            "hashes are the archive's own record and can be checked independently."
        ),
        "conversations": conv_rows,
    }
    manifest_path = root / "MANIFEST.json"
    manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    result.manifest_sha256 = sha256_bytes(manifest_bytes)

    (root / "README.md").write_text(_packet_readme(manifest, result), encoding="utf-8")

    checksums, files = [], []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "CHECKSUMS.sha256":
            digest = sha256_file(path)
            rel = path.relative_to(root).as_posix()
            checksums.append("%s  %s" % (digest, rel))
            files.append(digest)
    (root / "CHECKSUMS.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")

    for path in sorted(root.rglob("*")):
        if path.is_file():
            _freeze(path)
    result.files = len(checksums) + 1

    if include_zip:
        zip_path = Path(str(root) + ".zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    zf.write(path, arcname=str(Path(root.name) / path.relative_to(root)))
        _freeze(zip_path)
        result.zip_path = str(zip_path)

    archive.source.execute(
        "INSERT INTO evidence_packets(packet_id, created_at, recipient, purpose, "
        "max_security_class, selection_json, conversation_count, message_count, packet_path, "
        "manifest_sha256, acknowledgement) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (packet_id, created, recipient, purpose, max_class,
         json.dumps(manifest["selection"], ensure_ascii=False), result.conversations,
         result.messages, str(root), result.manifest_sha256, acknowledgement or None),
    )
    archive.source.commit()
    archive.log("evidence.packet", {
        "packet_id": packet_id, "recipient": recipient, "max_class": max_class,
        "conversations": result.conversations, "messages": result.messages,
        "withheld": len(result.withheld), "packet_tree_hash": merkle_root(files),
        "acknowledgement": bool(acknowledgement),
    })
    return result


def _write_jsonl(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    _freeze(path)


def _transcript(row, messages) -> str:
    lines = [
        "# %s\n\n" % (row["title"] or "(untitled conversation)"),
        "*Source conversation `%s` · archive version %d · %s · extracted %s*\n\n"
        % (row["source_conversation_id"], row["version_no"], row["source_method"],
           row["extraction_date"]),
        "*Source SHA-256: `%s`*\n\n" % row["source_sha256"],
        "> This transcript is rendered from the archive. The authoritative record is the "
        "matching file under `source/`, whose hash is printed above.\n\n---\n\n",
    ]
    for msg in messages:
        lines.append("### %d · %s%s\n\n" % (
            msg["seq"], msg["role"],
            " · %s" % msg["create_time"] if msg["create_time"] else "",
        ))
        if not msg["on_canonical_path"]:
            lines.append("*(off the conversation's current path: an edited or regenerated "
                         "branch, retained)*\n\n")
        lines.append((msg["content_text"] or "").rstrip() + "\n\n")
    return "".join(lines)


def _derived_readme() -> str:
    return (
        "# Derived material\n\n"
        "Everything in this directory is an **interpretation**, not a source record.\n\n"
        "- `propositions.jsonl` - atomic propositions. Each carries the exact quotation it "
        "rests on, the SHA-256 of that quotation, and the SHA-256 of the source record it "
        "came from. `contemporaneous_meaning` (what it meant at the time) and "
        "`later_interpretation` (what we think now) are separate fields and must stay "
        "separate in any downstream use.\n"
        "- `discovery_edges.jsonl` - the directed discovery graph: which observation or "
        "search generated which subsequent discovery.\n\n"
        "Check any claim against `../source/` before relying on it.\n"
    )


def _packet_readme(manifest: dict, result: PacketResult) -> str:
    return (
        "# ARCS evidence packet\n\n"
        "**Packet** `%s`  \n**For** %s  \n**Created** %s  \n"
        "**Security limit** %s\n\n%s\n\n"
        "## What is in here\n\n"
        "| directory | contents |\n|---|---|\n"
        "| `source/` | the exact original bytes for each conversation - authoritative |\n"
        "| `readable/` | Markdown transcripts for reading - convenience only |\n"
        "| `data/` | normalised rows as JSON Lines, for analysis |\n"
        "| `derived/` | interpretations, if any were included - never source |\n\n"
        "## How to verify it\n\n"
        "```\nsha256sum -c CHECKSUMS.sha256\n```\n\n"
        "Every file is listed. `MANIFEST.json` records the archive fingerprint and the "
        "last verification run at the time this packet was built.\n\n"
        "## What this packet is not\n\n"
        "- It is **not writable**: files are read-only and any change breaks the checksums.\n"
        "- It is **not complete** unless the manifest says so: %d item(s) were withheld "
        "above the %s class, and each withholding is listed in `MANIFEST.json`.\n"
        "- It carries **no credentials** and no account identifiers.\n\n"
        "## Counts\n\n- conversations: %d\n- messages: %d\n- propositions: %d\n"
        % (
            manifest["packet_id"], manifest["recipient"], manifest["created_at"],
            manifest["security"]["max_security_class"],
            ("**Purpose:** " + manifest["purpose"]) if manifest["purpose"] else "",
            len(result.withheld), manifest["security"]["max_security_class"],
            result.conversations, result.messages, result.propositions,
        )
    )


def verify_packet(path) -> dict:
    """Re-check a packet's checksums.  Usable by the recipient, offline."""
    root = Path(path)
    checksums = root / "CHECKSUMS.sha256"
    if not checksums.exists():
        return {"ok": False, "error": "no CHECKSUMS.sha256 in %s" % root}
    expected = {}
    for line in checksums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, rel = line.partition("  ")
        expected[rel] = digest
    mismatched, missing = [], []
    for rel, digest in expected.items():
        target = root / rel
        if not target.exists():
            missing.append(rel)
        elif sha256_file(target) != digest:
            mismatched.append(rel)
    present = {
        p.relative_to(root).as_posix() for p in root.rglob("*")
        if p.is_file() and p.name != "CHECKSUMS.sha256"
    }
    extra = sorted(present - set(expected))
    return {
        "ok": not (mismatched or missing or extra),
        "files_checked": len(expected),
        "mismatched": mismatched,
        "missing": missing,
        "unexpected": extra,
        "read_only": all(
            not (os.stat(p).st_mode & stat.S_IWUSR)
            for p in root.rglob("*") if p.is_file()
        ),
    }
