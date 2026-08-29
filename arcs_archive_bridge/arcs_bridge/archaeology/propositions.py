"""Atomic propositions.

One proposition is one claim, anchored to one exact quotation in one message
version.  The columns are the ones ARCS asked for:

    source conversation | source message | date | exact quotation | entity |
    location | number | arc | contemporaneous meaning | later interpretation |
    prompted-by | generated-next | verification status | probability role |
    correction history

Two of those are structural rather than columnar: *generated-next* is an edge in
the discovery graph (a proposition can generate several), and *correction
history* is the version chain - corrections append a new version and supersede
the old one, exactly as conversations do.

The rule the whole layer exists to protect: **contemporaneous meaning and later
interpretation never share a field.**  What a source meant at the time is
recorded separately from what we now think it refers to, so a later reading can
never be mistaken for the record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..errors import ArcsError, NotFound
from ..gate import authorise
from ..hashing import content_hash, sha256_text
from ..util import new_id, stable_id, utcnow

VERIFICATION_STATUSES = (
    "unverified",        # recorded, not yet checked
    "quote_verified",    # the quotation matches the source bytes exactly
    "corroborated",      # an independent source agrees
    "contradicted",      # an independent source disagrees
    "confirmed",         # verified against a primary record
    "refuted",           # shown to be false
)

PROBABILITY_ROLES = (
    "assertion",     # stated as fact by the source
    "hypothesis",    # offered as a possibility
    "inference",     # derived from other statements
    "speculation",   # explicitly uncertain
    "question",      # asked, not claimed
    "instruction",   # a directive rather than a claim
)

_FIELDS = (
    "event_date", "message_date", "quote_text", "quote_char_start", "quote_char_end",
    "entity", "location", "number_raw", "number_value", "number_unit", "arc",
    "contemporaneous_meaning", "later_interpretation", "prompted_by", "prompted_by_kind",
    "verification_status", "probability_role", "probability", "confidence_note",
    "created_by", "method",
)


@dataclass
class QuoteAnchor:
    conversation_id: str
    message_id: str
    message_version_id: str
    source_record_id: str
    source_sha256: str
    source_conversation_id: str
    source_message_id: str
    message_date: str
    char_start: int
    char_end: int
    quote_text: str


def resolve_quote(archive, message_version_id: str, quote: str,
                  char_start=None) -> QuoteAnchor:
    """Locate a quotation inside a message version and refuse if it is not there."""
    row = archive.one(
        "SELECT mv.*, c.source_conversation_id, m.source_message_id "
        "FROM message_versions mv "
        "JOIN conversations c ON c.conversation_id = mv.conversation_id "
        "JOIN messages m ON m.message_id = mv.message_id "
        "WHERE mv.version_id=?",
        (message_version_id,),
    )
    if row is None:
        raise NotFound("no message version %s" % message_version_id)
    text = row["content_text"] or ""
    if char_start is None:
        index = text.find(quote)
        if index < 0:
            raise ArcsError(
                "the quotation is not present verbatim in message version %s; "
                "propositions may only quote what the source actually says"
                % message_version_id
            )
    else:
        index = int(char_start)
        if text[index:index + len(quote)] != quote:
            raise ArcsError(
                "the quotation does not appear at character %d of message version %s"
                % (index, message_version_id)
            )
    record = archive.one(
        "SELECT blob_sha256 FROM source_records WHERE record_id=?", (row["source_record_id"],)
    )
    return QuoteAnchor(
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        message_version_id=message_version_id,
        source_record_id=row["source_record_id"],
        source_sha256=record["blob_sha256"] if record else "",
        source_conversation_id=row["source_conversation_id"],
        source_message_id=row["source_message_id"] or "",
        message_date=row["create_time"] or "",
        char_start=index,
        char_end=index + len(quote),
        quote_text=quote,
    )


def _version_payload(values: dict) -> dict:
    return {key: values.get(key) for key in _FIELDS}


def add_proposition(archive, message_version_id: str, quote: str, *, entity=None,
                    location=None, arc=None, event_date=None, number_raw=None,
                    number_value=None, number_unit=None, contemporaneous_meaning=None,
                    later_interpretation=None, prompted_by=None, prompted_by_kind=None,
                    verification_status="quote_verified", probability_role="assertion",
                    probability=None, confidence_note=None, created_by=None, method=None,
                    char_start=None, facets=None, interpretation_run_id=None,
                    gate_token=None, proposition_id=None) -> str:
    """Record one proposition.  Returns its stable proposition id."""
    token = gate_token or authorise(archive, purpose="recording a proposition")
    if verification_status not in VERIFICATION_STATUSES:
        raise ArcsError("unknown verification status %r" % verification_status)
    if probability_role and probability_role not in PROBABILITY_ROLES:
        raise ArcsError("unknown probability role %r" % probability_role)

    anchor = resolve_quote(archive, message_version_id, quote, char_start)
    created_by = created_by or ("human:" + (archive.config.operator or "unattributed"))
    now = utcnow()
    proposition_id = proposition_id or stable_id(
        "proposition", anchor.message_version_id, anchor.char_start, anchor.char_end, quote
    )

    values = {
        "event_date": event_date or (anchor.message_date[:10] if anchor.message_date else None),
        "message_date": anchor.message_date,
        "quote_text": quote,
        "quote_char_start": anchor.char_start,
        "quote_char_end": anchor.char_end,
        "entity": entity,
        "location": location,
        "number_raw": number_raw,
        "number_value": number_value,
        "number_unit": number_unit,
        "arc": arc,
        "contemporaneous_meaning": contemporaneous_meaning,
        "later_interpretation": later_interpretation,
        "prompted_by": prompted_by,
        "prompted_by_kind": prompted_by_kind or ("proposition" if prompted_by else None),
        "verification_status": verification_status,
        "probability_role": probability_role,
        "probability": probability,
        "confidence_note": confidence_note,
        "created_by": created_by,
        "method": method,
    }

    conn = archive.derived
    existing = conn.execute(
        "SELECT proposition_id FROM propositions WHERE proposition_id=?", (proposition_id,)
    ).fetchone()
    if existing is not None:
        # Same quotation recorded twice: treat it as a correction, never a
        # silent overwrite and never a duplicate row.
        return correct_proposition(
            archive, proposition_id, reason="re-recorded from the same quotation",
            gate_token=token, **{k: v for k, v in values.items() if v is not None}
        )

    version_no = 1
    version_id = stable_id("proposition_version", proposition_id, version_no)
    conn.execute(
        "INSERT INTO propositions(proposition_id, conversation_id, message_id, created_at, "
        "current_version_id) VALUES (?,?,?,?,?)",
        (proposition_id, anchor.conversation_id, anchor.message_id, now, version_id),
    )
    _insert_version(archive, conn, proposition_id, version_id, version_no, anchor, values,
                    supersedes=None, correction_reason=None,
                    interpretation_run_id=interpretation_run_id, now=now)
    _insert_facets(conn, version_id, facets or [])
    conn.commit()

    if prompted_by and (prompted_by_kind or "proposition") in ("proposition", "search_event"):
        from .graph import add_edge

        add_edge(archive, (prompted_by_kind or "proposition"), prompted_by,
                 "proposition", proposition_id, relation="generated",
                 rationale="recorded as prompted-by at extraction time",
                 created_by=created_by, interpretation_run_id=interpretation_run_id,
                 gate_token=token)
    return proposition_id


def _insert_version(archive, conn, proposition_id, version_id, version_no, anchor, values,
                    supersedes, correction_reason, interpretation_run_id, now) -> None:
    payload = _version_payload(values)
    conn.execute(
        "INSERT INTO proposition_versions(version_id, proposition_id, version_no, "
        "conversation_id, source_conversation_id, message_id, source_message_id, "
        "message_version_id, source_record_id, source_sha256, event_date, message_date, "
        "quote_text, quote_char_start, quote_char_end, quote_sha256, entity, location, "
        "number_raw, number_value, number_unit, arc, contemporaneous_meaning, "
        "later_interpretation, prompted_by, prompted_by_kind, verification_status, "
        "probability_role, probability, confidence_note, created_by, interpretation_run_id, "
        "method, correction_reason, supersedes_version_id, is_current, content_sha256, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (
            version_id, proposition_id, version_no, anchor.conversation_id,
            anchor.source_conversation_id, anchor.message_id, anchor.source_message_id,
            anchor.message_version_id, anchor.source_record_id, anchor.source_sha256,
            values["event_date"], values["message_date"], values["quote_text"],
            values["quote_char_start"], values["quote_char_end"], sha256_text(values["quote_text"]),
            values["entity"], values["location"], values["number_raw"], values["number_value"],
            values["number_unit"], values["arc"], values["contemporaneous_meaning"],
            values["later_interpretation"], values["prompted_by"], values["prompted_by_kind"],
            values["verification_status"], values["probability_role"], values["probability"],
            values["confidence_note"], values["created_by"], interpretation_run_id,
            values["method"], correction_reason, supersedes, content_hash(payload), now,
        ),
    )
    conn.execute(
        "UPDATE propositions SET current_version_id=? WHERE proposition_id=?",
        (version_id, proposition_id),
    )
    conn.execute("DELETE FROM proposition_fts WHERE proposition_id=?", (proposition_id,))
    conn.execute(
        "INSERT INTO proposition_fts(quote_text, contemporaneous_meaning, later_interpretation, "
        "entity, location, arc, proposition_id, version_id) VALUES (?,?,?,?,?,?,?,?)",
        (values["quote_text"] or "", values["contemporaneous_meaning"] or "",
         values["later_interpretation"] or "", values["entity"] or "", values["location"] or "",
         values["arc"] or "", proposition_id, version_id),
    )


def _insert_facets(conn, version_id, facets) -> None:
    for facet in facets or []:
        conn.execute(
            "INSERT INTO proposition_facets(facet_id, version_id, facet_type, value_text, "
            "value_number, value_unit, char_start, char_end, extractor) VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id("facet"), version_id, facet.get("facet_type", "term"),
             facet.get("value_text", ""), facet.get("value_number"), facet.get("value_unit"),
             facet.get("char_start"), facet.get("char_end"), facet.get("extractor")),
        )


def correct_proposition(archive, proposition_id: str, reason: str, *, facets=None,
                        gate_token=None, interpretation_run_id=None, **changes) -> str:
    """Append a corrected version.  The earlier version is retained untouched."""
    token = gate_token or authorise(archive, purpose="correcting a proposition")
    if not reason:
        raise ArcsError("a correction requires a reason; corrections are part of the record")
    conn = archive.derived
    current = conn.execute(
        "SELECT * FROM proposition_versions WHERE proposition_id=? AND is_current=1",
        (proposition_id,),
    ).fetchone()
    if current is None:
        raise NotFound("no proposition %s" % proposition_id)

    unknown = set(changes) - set(_FIELDS)
    if unknown:
        raise ArcsError("unknown proposition field(s): %s" % ", ".join(sorted(unknown)))

    values = {key: current[key] for key in _FIELDS}
    values.update({k: v for k, v in changes.items()})

    quote = values["quote_text"]
    anchor = resolve_quote(
        archive, changes.get("message_version_id", current["message_version_id"]),
        quote, values.get("quote_char_start"),
    )
    values["quote_char_start"] = anchor.char_start
    values["quote_char_end"] = anchor.char_end
    values["message_date"] = anchor.message_date

    version_no = current["version_no"] + 1
    version_id = stable_id("proposition_version", proposition_id, version_no)
    conn.execute(
        "UPDATE proposition_versions SET is_current=0 WHERE version_id=?", (current["version_id"],)
    )
    _insert_version(archive, conn, proposition_id, version_id, version_no, anchor, values,
                    supersedes=current["version_id"], correction_reason=reason,
                    interpretation_run_id=interpretation_run_id, now=utcnow())
    _insert_facets(conn, version_id, facets or [])
    conn.commit()
    archive.log("proposition.correct", {"proposition_id": proposition_id, "version_no": version_no,
                                        "reason": reason, "gate_run_id": token.run_id})
    return proposition_id


def retract_proposition(archive, proposition_id: str, reason: str) -> None:
    """Mark a proposition retracted.  Nothing is deleted."""
    if not reason:
        raise ArcsError("a retraction requires a reason")
    conn = archive.derived
    cur = conn.execute(
        "UPDATE propositions SET retracted_at=?, retracted_reason=? WHERE proposition_id=?",
        (utcnow(), reason, proposition_id),
    )
    if cur.rowcount == 0:
        raise NotFound("no proposition %s" % proposition_id)
    conn.commit()
    archive.log("proposition.retract", {"proposition_id": proposition_id, "reason": reason})


def get_proposition(archive, proposition_id: str) -> dict:
    row = archive.one(
        "SELECT pv.*, p.retracted_at, p.retracted_reason FROM proposition_versions pv "
        "JOIN propositions p ON p.proposition_id = pv.proposition_id "
        "WHERE pv.proposition_id=? AND pv.is_current=1",
        (proposition_id,), conn=archive.derived,
    )
    if row is None:
        raise NotFound("no proposition %s" % proposition_id)
    data = dict(row)
    data["generated_next"] = [
        dict(e) for e in archive.all(
            "SELECT to_kind, to_id, relation, rationale FROM discovery_edges "
            "WHERE from_kind='proposition' AND from_id=?",
            (proposition_id,), conn=archive.derived,
        )
    ]
    data["facets"] = [
        dict(f) for f in archive.all(
            "SELECT facet_type, value_text, value_number, value_unit FROM proposition_facets "
            "WHERE version_id=?", (row["version_id"],), conn=archive.derived,
        )
    ]
    return data


def proposition_history(archive, proposition_id: str) -> list:
    return [
        dict(r) for r in archive.all(
            "SELECT * FROM proposition_versions WHERE proposition_id=? ORDER BY version_no",
            (proposition_id,), conn=archive.derived,
        )
    ]


def list_propositions(archive, arc=None, conversation_id=None, entity=None,
                      verification_status=None, limit: int = 100) -> list:
    sql = ("SELECT pv.*, p.retracted_at FROM proposition_versions pv "
           "JOIN propositions p ON p.proposition_id = pv.proposition_id "
           "WHERE pv.is_current=1")
    params: list = []
    for column, value in (("arc", arc), ("conversation_id", conversation_id),
                          ("entity", entity), ("verification_status", verification_status)):
        if value:
            sql += " AND pv.%s = ?" % column
            params.append(value)
    sql += " ORDER BY pv.event_date, pv.created_at LIMIT ?"
    params.append(limit)
    return [dict(r) for r in archive.all(sql, tuple(params), conn=archive.derived)]


def verify_quote(archive, proposition_id: str) -> dict:
    """Re-check a quotation against the source bytes it claims to come from."""
    from ..blobstore import BlobStore

    row = archive.one(
        "SELECT * FROM proposition_versions WHERE proposition_id=? AND is_current=1",
        (proposition_id,), conn=archive.derived,
    )
    if row is None:
        raise NotFound("no proposition %s" % proposition_id)
    result = {
        "proposition_id": proposition_id,
        "quote_hash_ok": sha256_text(row["quote_text"]) == row["quote_sha256"],
        "in_message_version": False,
        "in_source_bytes": False,
        "source_sha256": row["source_sha256"],
    }
    message = archive.one(
        "SELECT content_text FROM message_versions WHERE version_id=?",
        (row["message_version_id"],),
    )
    if message is not None:
        text = message["content_text"] or ""
        start, end = row["quote_char_start"], row["quote_char_end"]
        result["in_message_version"] = text[start:end] == row["quote_text"]
    try:
        raw = BlobStore(archive.config.blobs_dir).get(row["source_sha256"])
        # The source is JSON-escaped, so compare against the decoded document.
        result["in_source_bytes"] = json.dumps(row["quote_text"], ensure_ascii=False)[1:-1] in \
            raw.decode("utf-8", "replace")
    except Exception as exc:
        result["source_error"] = str(exc)
    result["ok"] = bool(result["quote_hash_ok"] and result["in_message_version"])
    return result
