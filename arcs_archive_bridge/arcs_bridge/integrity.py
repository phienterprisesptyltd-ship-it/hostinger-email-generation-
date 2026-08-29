"""Completeness, recovery and provenance tests.

This is the suite the whole design waits on: no automated interpretation runs
until it passes (see :mod:`arcs_bridge.gate`).  A run records its result and the
archive fingerprint it saw, so an authorisation cannot outlive the state it was
granted for - ingest new material and the gate closes again until re-verified.

Each check is small, named, and reports the rows it failed on rather than a
bare boolean, because a failing archive needs to be repairable, not just
detectable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import reconstruct
from .blobstore import BlobStore
from .hashing import merkle_root, sha256_bytes, sha256_text
from .security import CLASSES
from .util import new_id, utcnow

SUITES = ("raw_layer", "reconstruction", "completeness", "archaeology", "full")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    failures: list = field(default_factory=list)
    count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IntegrityReport:
    run_id: str
    suite: str
    started_at: str
    finished_at: str = ""
    archive_fingerprint: str = ""
    checks: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> list:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "suite": self.suite,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "archive_fingerprint": self.archive_fingerprint,
            "passed": self.passed,
            "checks_total": len(self.checks),
            "checks_failed": len(self.failed),
            "checks": [c.to_dict() for c in self.checks],
        }


def archive_fingerprint(archive) -> str:
    """A digest over every source record and the bytes it points at.

    Adding, removing or altering any source record changes this value, which is
    what ties an interpretation authorisation to an exact archive state.
    """
    rows = archive.all("SELECT record_id, blob_sha256 FROM source_records")
    return merkle_root("%s:%s" % (r["record_id"], r["blob_sha256"]) for r in rows)


# ------------------------------------------------------------------- checks
def check_blob_store(archive) -> list:
    store = BlobStore(archive.config.blobs_dir)
    missing, corrupt = [], []
    for row in archive.all("SELECT sha256, storage FROM source_blobs"):
        if row["storage"] != "file":
            continue
        path = store.path_for(row["sha256"])
        if not path.exists():
            missing.append(row["sha256"])
        elif sha256_bytes(path.read_bytes()) != row["sha256"]:
            corrupt.append(row["sha256"])
    total = archive.scalar("SELECT COUNT(*) FROM source_blobs") or 0
    return [
        Check("blobs_present", not missing,
              "%d/%d blobs present on disk" % (total - len(missing), total),
              missing[:20], total),
        Check("blobs_hash_verified", not corrupt,
              "every stored blob still hashes to its key" if not corrupt
              else "%d blob(s) failed their hash check" % len(corrupt),
              corrupt[:20], total),
    ]


def check_source_records(archive) -> list:
    orphans = [
        r["record_id"] for r in archive.all(
            "SELECT sr.record_id FROM source_records sr "
            "LEFT JOIN source_blobs b ON b.sha256 = sr.blob_sha256 WHERE b.sha256 IS NULL"
        )
    ]
    bad_dupes = [
        r["record_id"] for r in archive.all(
            "SELECT a.record_id FROM source_records a "
            "JOIN source_records b ON b.record_id = a.duplicate_of_record_id "
            "WHERE a.duplicate_of_record_id IS NOT NULL AND a.blob_sha256 <> b.blob_sha256"
        )
    ]
    dangling = [
        r["record_id"] for r in archive.all(
            "SELECT a.record_id FROM source_records a "
            "LEFT JOIN source_records b ON b.record_id = a.duplicate_of_record_id "
            "WHERE a.duplicate_of_record_id IS NOT NULL AND b.record_id IS NULL"
        )
    ]
    missing_prov = [
        r["record_id"] for r in archive.all(
            "SELECT record_id FROM source_records WHERE source_method IS NULL "
            "OR source_method='' OR extraction_date IS NULL OR extraction_date='' "
            "OR adapter IS NULL OR adapter=''"
        )
    ]
    total = archive.scalar("SELECT COUNT(*) FROM source_records") or 0
    return [
        Check("source_records_have_blobs", not orphans,
              "every source record points at a stored blob", orphans[:20], total),
        Check("dedupe_preserves_provenance", not bad_dupes and not dangling,
              "duplicate records keep their own row and link to the first sighting",
              (bad_dupes + dangling)[:20], total),
        Check("provenance_fields_present", not missing_prov,
              "every source record carries adapter, source method and extraction date",
              missing_prov[:20], total),
    ]


def check_version_chains(archive) -> list:
    failures = []
    for table, id_col in (("conversation_versions", "conversation_id"),
                          ("message_versions", "message_id")):
        multi = archive.all(
            "SELECT %s AS subject, COUNT(*) AS n FROM %s WHERE is_current=1 "
            "GROUP BY %s HAVING n <> 1" % (id_col, table, id_col)
        )
        failures += ["%s: %s has %d current versions" % (table, r["subject"], r["n"]) for r in multi]

        gaps = archive.all(
            "SELECT %s AS subject, COUNT(*) AS n, MAX(version_no) AS mx FROM %s "
            "GROUP BY %s HAVING n <> mx" % (id_col, table, id_col)
        )
        failures += ["%s: %s version numbers are not contiguous (%d rows, max %d)"
                     % (table, r["subject"], r["n"], r["mx"]) for r in gaps]

        broken = archive.all(
            "SELECT a.version_id FROM %s a LEFT JOIN %s b "
            "ON b.version_id = a.supersedes_version_id "
            "WHERE a.supersedes_version_id IS NOT NULL AND b.version_id IS NULL" % (table, table)
        )
        failures += ["%s: %s supersedes a version that does not exist"
                     % (table, r["version_id"]) for r in broken]

    orphan_current = archive.all(
        "SELECT c.conversation_id FROM conversations c LEFT JOIN conversation_versions v "
        "ON v.conversation_id = c.conversation_id AND v.is_current=1 WHERE v.version_id IS NULL"
    )
    failures += ["conversation %s has no current version" % r["conversation_id"]
                 for r in orphan_current]

    total = archive.scalar("SELECT COUNT(*) FROM conversation_versions") or 0
    return [Check("version_chains_intact", not failures,
                  "corrections append and supersede; nothing is overwritten",
                  failures[:20], total)]


def check_completeness(archive) -> list:
    """Every message the source contained is present, in order, with its fields."""
    count_mismatch, order_broken, missing_fields, empty_membership = [], [], [], []

    for row in archive.all("SELECT * FROM conversation_versions"):
        members = archive.all(
            "SELECT cvm.seq, mv.version_id, mv.role, mv.content_sha256, mv.metadata_json "
            "FROM conversation_version_messages cvm "
            "JOIN message_versions mv ON mv.version_id = cvm.message_version_id "
            "WHERE cvm.conversation_version_id=? ORDER BY cvm.seq",
            (row["version_id"],),
        )
        if row["message_count"] != len(members):
            count_mismatch.append(
                "%s v%d: recorded %d messages, linked %d"
                % (row["conversation_id"], row["version_no"], row["message_count"], len(members))
            )
        if row["message_count"] and not members:
            empty_membership.append("%s v%d" % (row["conversation_id"], row["version_no"]))
        seqs = [m["seq"] for m in members]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            order_broken.append("%s v%d: message order is not a strict sequence"
                                % (row["conversation_id"], row["version_no"]))
        for m in members:
            if not m["role"] or not m["content_sha256"]:
                missing_fields.append(m["version_id"])
            meta = json.loads(m["metadata_json"] or "{}")
            if not meta:
                missing_fields.append(m["version_id"] + " (no retained original)")

    total = archive.scalar("SELECT COUNT(*) FROM message_versions") or 0
    checks = [
        Check("message_counts_match", not count_mismatch,
              "each conversation version links exactly the messages it recorded",
              count_mismatch[:20], total),
        Check("message_order_preserved", not order_broken,
              "message order is a strict increasing sequence within each version",
              order_broken[:20], total),
        Check("required_fields_present", not missing_fields,
              "role, content hash and retained original are present on every message",
              missing_fields[:20], total),
        Check("membership_recorded", not empty_membership,
              "no non-empty conversation version has lost its message membership",
              empty_membership[:20], total),
    ]

    # Timestamps, titles and source method are the fields the archive promised
    # to retain; report anything the source simply never provided as a warning
    # rather than a failure.
    no_title = archive.scalar(
        "SELECT COUNT(*) FROM conversation_versions WHERE title IS NULL OR title=''"
    ) or 0
    no_time = archive.scalar(
        "SELECT COUNT(*) FROM message_versions WHERE is_current=1 AND "
        "(create_time IS NULL) AND (create_time_raw IS NULL OR create_time_raw='null')"
    ) or 0
    checks.append(
        Check("retained_metadata", True,
              "%d conversation version(s) without a title in the source; "
              "%d current message(s) with no timestamp in the source "
              "(retained as absent, not invented)" % (no_title, no_time))
    )
    return checks


def check_attachments(archive) -> list:
    missing = [
        r["attachment_id"] for r in archive.all(
            "SELECT a.attachment_id FROM attachments a "
            "LEFT JOIN source_blobs b ON b.sha256 = a.blob_sha256 "
            "WHERE a.content_present=1 AND b.sha256 IS NULL"
        )
    ]
    total = archive.scalar("SELECT COUNT(*) FROM attachments") or 0
    return [Check("attachment_blobs_present", not missing,
                  "attachments claiming stored content have it", missing[:20], total)]


def check_search_index(archive) -> list:
    missing = [
        r["message_id"] for r in archive.all(
            "SELECT m.message_id FROM message_versions m "
            "LEFT JOIN message_fts f ON f.message_id = m.message_id "
            "WHERE m.is_current=1 AND f.message_id IS NULL"
        )
    ]
    stale = [
        r["message_id"] for r in archive.all(
            "SELECT f.message_id FROM message_fts f "
            "JOIN message_versions m ON m.message_id = f.message_id AND m.is_current=1 "
            "WHERE f.version_id <> m.version_id"
        )
    ]
    total = archive.scalar("SELECT COUNT(*) FROM message_versions WHERE is_current=1") or 0
    return [
        Check("search_index_complete", not missing,
              "every current message is searchable", missing[:20], total),
        Check("search_index_current", not stale,
              "the search index points at current message versions", stale[:20], total),
    ]


def check_security_classes(archive) -> list:
    bad = []
    for table, id_col in (("conversations", "conversation_id"), ("messages", "message_id")):
        for row in archive.all(
            "SELECT %s AS subject, security_class FROM %s" % (id_col, table)
        ):
            if row["security_class"] not in CLASSES:
                bad.append("%s %s: %r" % (table, row["subject"], row["security_class"]))
    unlogged = archive.all(
        "SELECT c.conversation_id FROM conversations c "
        "LEFT JOIN security_class_history h ON h.subject_type='conversation' "
        "AND h.subject_id = c.conversation_id "
        "WHERE c.security_class <> 'Normal' AND h.entry_id IS NULL"
    )
    return [
        Check("security_classes_valid", not bad,
              "every class is one of " + ", ".join(CLASSES), bad[:20]),
        Check("classification_changes_logged", not unlogged,
              "every non-default classification has a recorded rationale",
              [r["conversation_id"] for r in unlogged][:20]),
    ]


def check_reconstruction(archive, include_history: bool = True) -> list:
    reports = reconstruct.verify_all(archive, include_history=include_history)
    byte_fail = [r.version_id for r in reports if not (r.bytes_recovered and r.bytes_sha256_match)]
    struct_fail = [
        "%s v%d: %s" % (r.conversation_id, r.version_no, "; ".join(r.differences[:3]) or r.error)
        for r in reports if not r.structural_match
    ]
    return [
        Check("originals_recoverable_bytes", not byte_fail,
              "%d version(s) recovered byte-for-byte from the blob store"
              % (len(reports) - len(byte_fail)), byte_fail[:20], len(reports)),
        Check("originals_rebuildable_from_tables", not struct_fail,
              "%d version(s) rebuilt structurally from the normalised tables alone"
              % (len(reports) - len(struct_fail)), struct_fail[:20], len(reports)),
    ]


def check_derived_separation(archive) -> list:
    """Interpretations must not be sitting in the source database."""
    failures = []
    if archive.config.source_db_path.resolve() == archive.config.derived_db_path.resolve():
        failures.append("source and derived databases are the same file")
    source_tables = {
        r[0] for r in archive.source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for forbidden in ("propositions", "proposition_versions", "discovery_edges",
                      "embeddings", "analyst_notes", "interpretation_runs"):
        if forbidden in source_tables:
            failures.append("derived table %r found in the source database" % forbidden)
    return [Check("derived_layer_separated", not failures,
                  "interpretations live in a separate database file", failures)]


def check_proposition_anchors(archive) -> list:
    """Every quotation still matches the source bytes it claims to come from."""
    rows = archive.all(
        "SELECT version_id, proposition_id, message_version_id, quote_text, quote_sha256, "
        "quote_char_start, quote_char_end FROM proposition_versions WHERE is_current=1",
        conn=archive.derived,
    )
    bad_hash, orphan, misaligned = [], [], []
    for row in rows:
        if sha256_text(row["quote_text"]) != row["quote_sha256"]:
            bad_hash.append(row["version_id"])
            continue
        msg = archive.one(
            "SELECT content_text FROM message_versions WHERE version_id=?",
            (row["message_version_id"],),
        )
        if msg is None:
            orphan.append(row["version_id"])
            continue
        text = msg["content_text"] or ""
        start, end = row["quote_char_start"], row["quote_char_end"]
        if start is not None and end is not None:
            if text[start:end] != row["quote_text"]:
                misaligned.append(row["version_id"])
        elif row["quote_text"] not in text:
            misaligned.append(row["version_id"])
    return [
        Check("quote_hashes_match", not bad_hash,
              "every quotation hashes to its recorded digest", bad_hash[:20], len(rows)),
        Check("quote_anchors_resolve", not orphan,
              "every proposition anchors to a message version that exists", orphan[:20], len(rows)),
        Check("quotes_verbatim_in_source", not misaligned,
              "every quotation is present verbatim in the message it cites",
              misaligned[:20], len(rows)),
    ]


def check_tamper_evidence(archive) -> list:
    """Compare against the previous run's snapshot: source records only ever grow."""
    snap_dir = archive.config.logs_dir / "integrity"
    snap_dir.mkdir(parents=True, exist_ok=True)
    current = {
        r["record_id"]: r["blob_sha256"]
        for r in archive.all("SELECT record_id, blob_sha256 FROM source_records")
    }
    previous_files = sorted(snap_dir.glob("snapshot-*.json"))
    failures = []
    if previous_files:
        previous = json.loads(previous_files[-1].read_text(encoding="utf-8"))["records"]
        for record_id, sha in previous.items():
            if record_id not in current:
                failures.append("source record %s has been deleted since %s"
                                % (record_id, previous_files[-1].name))
            elif current[record_id] != sha:
                failures.append("source record %s now points at different bytes" % record_id)
    detail = ("first snapshot recorded; future runs will detect deletion or alteration"
              if not previous_files else
              "checked %d record(s) against %s" % (len(current), previous_files[-1].name))
    return [Check("source_records_append_only", not failures, detail, failures[:20], len(current))]


def _write_snapshot(archive, run_id: str) -> Path:
    snap_dir = archive.config.logs_dir / "integrity"
    snap_dir.mkdir(parents=True, exist_ok=True)
    records = {
        r["record_id"]: r["blob_sha256"]
        for r in archive.all("SELECT record_id, blob_sha256 FROM source_records")
    }
    path = snap_dir / ("snapshot-%s.json" % utcnow().replace(":", "").replace(".", "-"))
    path.write_text(
        json.dumps({"run_id": run_id, "at": utcnow(), "records": records},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


SUITE_CHECKS = {
    "raw_layer": (check_blob_store, check_source_records, check_version_chains,
                  check_attachments, check_security_classes, check_tamper_evidence,
                  check_derived_separation),
    "reconstruction": (check_reconstruction,),
    "completeness": (check_completeness, check_search_index),
    "archaeology": (check_proposition_anchors,),
}
SUITE_CHECKS["full"] = tuple(
    fn for suite in ("raw_layer", "reconstruction", "completeness", "archaeology")
    for fn in SUITE_CHECKS[suite]
)


def run(archive, suite: str = "full") -> IntegrityReport:
    if suite not in SUITE_CHECKS:
        raise ValueError("unknown suite %r (expected one of %s)" % (suite, ", ".join(SUITES)))
    run_id = new_id("run")
    report = IntegrityReport(run_id=run_id, suite=suite, started_at=utcnow())
    for fn in SUITE_CHECKS[suite]:
        try:
            report.checks.extend(fn(archive))
        except Exception as exc:  # a broken check is a failing check
            report.checks.append(Check(fn.__name__, False, "check raised: %s" % exc))
    report.finished_at = utcnow()
    report.archive_fingerprint = archive_fingerprint(archive)

    archive.source.execute(
        "INSERT INTO integrity_runs(run_id, started_at, finished_at, suite, passed, "
        "checks_total, checks_failed, archive_fingerprint, report_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, report.started_at, report.finished_at, suite, 1 if report.passed else 0,
         len(report.checks), len(report.failed), report.archive_fingerprint,
         json.dumps(report.to_dict(), ensure_ascii=False)),
    )
    archive.source.commit()
    _write_snapshot(archive, run_id)
    archive.log("verify", {"run_id": run_id, "suite": suite, "passed": report.passed,
                           "failed": [c.name for c in report.failed]})
    return report


def latest_run(archive, suite: str = "full"):
    return archive.one(
        "SELECT * FROM integrity_runs WHERE suite=? ORDER BY finished_at DESC LIMIT 1", (suite,)
    )
