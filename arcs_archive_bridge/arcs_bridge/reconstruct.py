"""Prove that the originals can always be rebuilt from the archive.

Two independent reconstruction paths, deliberately not sharing code:

**Path A - the blob store.**  The exact source bytes are read back from the
content-addressed store and re-hashed.  If the hash matches the source record,
the original is recovered byte-for-byte.  This path does not consult the
normalised tables at all.

**Path B - the normalised tables.**  The original JSON object is rebuilt from
``conversation_versions`` and ``message_versions`` alone - the retained original
node objects, the mapping order, the envelope - and compared structurally to
the parsed original.  This path does not consult the blob store at all.

A conversation is only reported as recoverable when *both* paths succeed.  Path
A alone would prove the bytes survived; path B alone would prove normalisation
kept everything; together they prove the archive is not silently lossy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .blobstore import BlobStore
from .errors import NotFound, ReconstructionError
from .hashing import canonical_json, sha256_bytes
from .util import utcnow

MAX_DIFFS = 25


# ------------------------------------------------------------------ diffing
def diff_json(a, b, path: str = "$", out=None, limit: int = MAX_DIFFS) -> list:
    """Structural difference report between two parsed JSON values."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append("%s: type %s != %s" % (path, type(a).__name__, type(b).__name__))
        return out
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append("%s.%s: missing in rebuilt" % (path, key))
            elif key not in b:
                out.append("%s.%s: unexpected in rebuilt" % (path, key))
            else:
                diff_json(a[key], b[key], "%s.%s" % (path, key), out, limit)
            if len(out) >= limit:
                return out
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append("%s: length %d != %d" % (path, len(a), len(b)))
        for i in range(min(len(a), len(b))):
            diff_json(a[i], b[i], "%s[%d]" % (path, i), out, limit)
            if len(out) >= limit:
                return out
    elif a != b:
        out.append("%s: value differs" % path)
    return out


# ------------------------------------------------------------------- path A
def source_bytes(archive, record_id: str) -> bytes:
    """Read a source record's exact bytes back, verifying the hash."""
    row = archive.one(
        "SELECT blob_sha256 FROM source_records WHERE record_id=?", (record_id,)
    )
    if row is None:
        raise NotFound("no source record %s" % record_id)
    store = BlobStore(archive.config.blobs_dir)
    data = store.get(row["blob_sha256"])  # raises on hash mismatch
    return data


# ------------------------------------------------------------------- path B
def _message_nodes(archive, conversation_version_id: str) -> list:
    return archive.all(
        "SELECT mv.* FROM conversation_version_messages cvm "
        "JOIN message_versions mv ON mv.version_id = cvm.message_version_id "
        "WHERE cvm.conversation_version_id=? ORDER BY cvm.seq",
        (conversation_version_id,),
    )


def _ordered_object(key_order, values: dict, extra_key: str, extra_value) -> dict:
    """Rebuild an object honouring the original key order where recorded."""
    out = {}
    for key in key_order or []:
        if key == extra_key:
            out[key] = extra_value
        elif key in values:
            out[key] = values[key]
    for key, value in values.items():  # any key the order list did not mention
        out.setdefault(key, value)
    out.setdefault(extra_key, extra_value)
    return out


def rebuild_mapping_object(archive, version_row) -> dict:
    """Rebuild a ChatGPT-shaped conversation object from normalised rows."""
    meta = json.loads(version_row["metadata_json"] or "{}")
    envelope = meta.get("envelope") or {}
    mapping_order = meta.get("mapping_order") or []
    structural = meta.get("structural_nodes") or {}

    nodes = dict(structural)
    for row in _message_nodes(archive, version_row["version_id"]):
        node_meta = json.loads(row["metadata_json"] or "{}")
        node_id = node_meta.get("node_id")
        node = node_meta.get("node")
        if node_id is None or node is None:
            raise ReconstructionError(
                "message version %s did not retain its original node" % row["version_id"]
            )
        nodes[node_id] = node

    mapping = {nid: nodes[nid] for nid in mapping_order if nid in nodes}
    for nid, node in nodes.items():  # anything the order list missed
        mapping.setdefault(nid, node)

    return _ordered_object(meta.get("envelope_key_order"), envelope, "mapping", mapping)


def rebuild_dom_entry(archive, version_row) -> dict:
    """Rebuild a DOM-captured bundle entry from normalised rows."""
    meta = json.loads(version_row["metadata_json"] or "{}")
    envelope = meta.get("envelope") or {}
    messages = []
    for row in _message_nodes(archive, version_row["version_id"]):
        node_meta = json.loads(row["metadata_json"] or "{}")
        if "message" not in node_meta:
            raise ReconstructionError(
                "message version %s did not retain its original entry" % row["version_id"]
            )
        messages.append((node_meta.get("entry_index", 0), node_meta["message"]))
    messages.sort(key=lambda pair: pair[0])
    return _ordered_object(
        meta.get("entry_key_order"), envelope, "messages", [m for _, m in messages]
    )


def rebuild_capture_entry(archive, version_row) -> dict:
    """Rebuild a UI-capture bundle entry that wrapped provider JSON."""
    meta = json.loads(version_row["metadata_json"] or "{}")
    payload = rebuild_mapping_object(archive, version_row)
    entry = meta.get("capture_entry") or {}
    return _ordered_object(meta.get("entry_key_order"), entry, "payload", payload)


def rebuild_conversation_object(archive, version_row) -> dict:
    """Dispatch on how the material arrived."""
    meta = json.loads(version_row["metadata_json"] or "{}")
    record = archive.one(
        "SELECT adapter FROM source_records WHERE record_id=?", (version_row["source_record_id"],)
    )
    adapter = record["adapter"] if record else "chatgpt_export"
    if adapter == "ui_capture":
        if "capture_entry" in meta:
            return rebuild_capture_entry(archive, version_row)
        return rebuild_dom_entry(archive, version_row)
    return rebuild_mapping_object(archive, version_row)


# ------------------------------------------------------------------- report
@dataclass
class ReconstructionReport:
    conversation_id: str
    version_id: str
    version_no: int
    source_record_id: str
    source_sha256: str
    title: str = ""
    adapter: str = ""
    verbatim_bytes: bool = True
    bytes_recovered: bool = False
    bytes_sha256_match: bool = False
    structural_match: bool = False
    canonical_match: bool = False
    differences: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.bytes_recovered and self.bytes_sha256_match and self.structural_match

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ok"] = self.ok
        return data


def verify_conversation(archive, conversation_id: str, version_id=None) -> ReconstructionReport:
    if version_id:
        row = archive.one(
            "SELECT * FROM conversation_versions WHERE version_id=?", (version_id,)
        )
    else:
        row = archive.one(
            "SELECT * FROM conversation_versions WHERE conversation_id=? AND is_current=1",
            (conversation_id,),
        )
    if row is None:
        raise NotFound("no conversation version for %s" % conversation_id)

    record = archive.one(
        "SELECT * FROM source_records WHERE record_id=?", (row["source_record_id"],)
    )
    record_meta = json.loads(record["metadata_json"] or "{}") if record else {}
    report = ReconstructionReport(
        conversation_id=row["conversation_id"],
        version_id=row["version_id"],
        version_no=row["version_no"],
        source_record_id=row["source_record_id"],
        source_sha256=row["source_sha256"],
        title=row["title"] or "",
        adapter=record["adapter"] if record else "",
        verbatim_bytes=bool(record_meta.get("verbatim_bytes", True)),
    )

    # Path A: bytes back out of the blob store.
    try:
        data = source_bytes(archive, row["source_record_id"])
        report.bytes_recovered = True
        report.bytes_sha256_match = sha256_bytes(data) == row["source_sha256"]
    except Exception as exc:
        report.error = "byte recovery failed: %s" % exc
        return report

    # Path B: rebuild from the normalised tables and compare.
    try:
        original = json.loads(data.decode("utf-8"))
        rebuilt = rebuild_conversation_object(archive, row)
        report.differences = diff_json(original, rebuilt)
        report.structural_match = not report.differences
        report.canonical_match = canonical_json(original) == canonical_json(rebuilt)
    except Exception as exc:
        report.error = "structural rebuild failed: %s" % exc
    return report


def verify_all(archive, conversation_ids=None, include_history: bool = False) -> list:
    if conversation_ids:
        placeholders = ",".join("?" * len(conversation_ids))
        rows = archive.all(
            "SELECT conversation_id, version_id FROM conversation_versions "
            "WHERE conversation_id IN (%s)%s ORDER BY conversation_id, version_no"
            % (placeholders, "" if include_history else " AND is_current=1"),
            tuple(conversation_ids),
        )
    else:
        rows = archive.all(
            "SELECT conversation_id, version_id FROM conversation_versions%s "
            "ORDER BY conversation_id, version_no"
            % ("" if include_history else " WHERE is_current=1")
        )
    return [verify_conversation(archive, r["conversation_id"], r["version_id"]) for r in rows]


# ------------------------------------------------------------ recovery to disk
def export_originals(archive, out_dir, conversation_ids=None, include_history: bool = False) -> dict:
    """Write the original source bytes back out to a directory, with a manifest.

    This is the disaster-recovery path: given only the archive directory, get
    the exact material back.  Every file is verified against its recorded hash
    as it is written.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = BlobStore(archive.config.blobs_dir)

    where = "" if include_history else " AND cv.is_current=1"
    params: tuple = ()
    if conversation_ids:
        where += " AND cv.conversation_id IN (%s)" % ",".join("?" * len(conversation_ids))
        params = tuple(conversation_ids)
    rows = archive.all(
        "SELECT cv.*, c.source_conversation_id, sr.adapter, sr.source_method, "
        "sr.extraction_date, sr.byte_start, sr.byte_end, sr.source_uri "
        "FROM conversation_versions cv "
        "JOIN conversations c ON c.conversation_id = cv.conversation_id "
        "JOIN source_records sr ON sr.record_id = cv.source_record_id "
        "WHERE 1=1%s ORDER BY c.source_conversation_id, cv.version_no" % where,
        params,
    )

    manifest = {
        "generated_at": utcnow(),
        "archive": str(archive.config.root),
        "note": "Byte-exact source material recovered from the ARCS archive.",
        "files": [],
    }
    for row in rows:
        data = store.get(row["source_sha256"])
        name = "%s.v%d.json" % (row["source_conversation_id"], row["version_no"])
        target = out_dir / name
        target.write_bytes(data)
        manifest["files"].append(
            {
                "file": name,
                "conversation_id": row["conversation_id"],
                "source_conversation_id": row["source_conversation_id"],
                "version_no": row["version_no"],
                "sha256": row["source_sha256"],
                "byte_length": len(data),
                "adapter": row["adapter"],
                "source_method": row["source_method"],
                "extraction_date": row["extraction_date"],
                "source_uri": row["source_uri"],
                "byte_span_in_container": (
                    [row["byte_start"], row["byte_end"]]
                    if row["byte_start"] is not None else None
                ),
                "verified": sha256_bytes(data) == row["source_sha256"],
            }
        )

    # Containers (whole original export files) are recovered too.
    for row in archive.all(
        "SELECT sr.*, b.byte_length FROM source_records sr "
        "JOIN source_blobs b ON b.sha256 = sr.blob_sha256 "
        "WHERE sr.record_kind='container' AND sr.duplicate_of_record_id IS NULL "
        "ORDER BY sr.ingested_at"
    ):
        data = store.get(row["blob_sha256"])
        name = "container-%s.json" % row["blob_sha256"][:16]
        (out_dir / name).write_bytes(data)
        manifest["files"].append(
            {
                "file": name,
                "kind": "container",
                "sha256": row["blob_sha256"],
                "byte_length": len(data),
                "adapter": row["adapter"],
                "source_method": row["source_method"],
                "original_path": row["source_uri"],
                "extraction_date": row["extraction_date"],
                "verified": sha256_bytes(data) == row["blob_sha256"],
            }
        )

    (out_dir / "RECOVERY-MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    archive.log("recover", {"out_dir": str(out_dir), "files": len(manifest["files"])})
    return manifest
