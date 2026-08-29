"""Obsidian-compatible Markdown projection.

The vault is a *projection*, never the archive.  It is regenerated from the
database, it is safe to delete, and every note says so.  What makes it useful
for research is that identifiers are stable across regenerations:

* a conversation note is named ``<slug>--<first 8 of conversation_id>.md`` and
  carries the full ``arcs_id`` in front matter, so links survive a retitle;
* each message gets an Obsidian block anchor ``^m<seq>``, so a proposition can
  deep-link the exact message it quotes: ``[[note#^m7]]``;
* propositions, arcs and entities become their own notes that link back to the
  conversation and forward along the discovery graph, so Obsidian's backlink
  pane shows the provenance chain without any plugin.

Security classes are honoured: material above the requested class is projected
as a stub that records the conversation's existence, provenance and class but
none of its content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .security import NORMAL, effective_class, permits
from .util import slugify, utcnow

VAULT_INDEX = "ARCS Archive.md"
CONV_DIR = "conversations"
PROP_DIR = "propositions (derived)"
ARC_DIR = "arcs (derived)"

_BANNER = (
    "> [!info] Derived projection\n"
    "> This note is generated from the ARCS archive database. It is not the "
    "source record. The authoritative copy is the hashed source blob named "
    "under *Provenance*; regenerate this vault with `arcs project` at any time.\n"
)


@dataclass
class ProjectionResult:
    out_dir: str
    conversations: int = 0
    stubs: int = 0
    propositions: int = 0
    arcs: int = 0
    files: list = field(default_factory=list)


def _yaml_value(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if any(ch in text for ch in ':#[]{}",\n') or text.strip() != text or not text:
        return json.dumps(text, ensure_ascii=False)
    return text


def frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            lines.append("%s:" % key)
            for item in value:
                lines.append("  - %s" % _yaml_value(item))
        else:
            lines.append("%s: %s" % (key, _yaml_value(value)))
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def conversation_note_name(conversation_id: str, title: str) -> str:
    return "%s--%s" % (slugify(title or "untitled"), conversation_id[:8])


def _link(note_name: str, alias: str = "") -> str:
    return "[[%s|%s]]" % (note_name, alias) if alias else "[[%s]]" % note_name


def project(archive, out_dir=None, max_class: str = "Private",
            include_derived: bool = True, conversation_ids=None) -> ProjectionResult:
    out_dir = Path(out_dir or archive.config.obsidian_dir)
    (out_dir / CONV_DIR).mkdir(parents=True, exist_ok=True)
    result = ProjectionResult(out_dir=str(out_dir))

    where, params = "", ()
    if conversation_ids:
        where = " AND c.conversation_id IN (%s)" % ",".join("?" * len(conversation_ids))
        params = tuple(conversation_ids)

    rows = archive.all(
        "SELECT c.*, cv.version_id, cv.version_no, cv.title, cv.create_time, cv.update_time, "
        "cv.model, cv.message_count, cv.source_sha256, cv.content_sha256, cv.correction_reason, "
        "sr.adapter, sr.source_method, sr.extraction_date, sr.source_uri, sr.record_id "
        "FROM conversations c "
        "JOIN conversation_versions cv ON cv.conversation_id = c.conversation_id AND cv.is_current=1 "
        "JOIN source_records sr ON sr.record_id = cv.source_record_id "
        "WHERE 1=1%s ORDER BY cv.create_time, cv.title" % where,
        params,
    )

    index_entries = []
    for row in rows:
        note_name = conversation_note_name(row["conversation_id"], row["title"])
        visible = permits(max_class, row["security_class"])
        body = _conversation_note(archive, row, note_name, visible, include_derived)
        path = out_dir / CONV_DIR / (note_name + ".md")
        path.write_text(body, encoding="utf-8")
        result.files.append(str(path))
        result.conversations += 1
        if not visible:
            result.stubs += 1
        index_entries.append((row, note_name, visible))

    if include_derived:
        result.propositions, result.arcs = _project_derived(archive, out_dir, max_class)

    (out_dir / VAULT_INDEX).write_text(
        _index_note(archive, index_entries, max_class, result), encoding="utf-8"
    )
    result.files.append(str(out_dir / VAULT_INDEX))
    archive.log("project", {"out_dir": str(out_dir), "conversations": result.conversations,
                            "stubs": result.stubs, "max_class": max_class})
    return result


def _conversation_note(archive, row, note_name, visible: bool, include_derived: bool) -> str:
    meta = {
        "arcs_id": row["conversation_id"],
        "arcs_type": "conversation",
        "source_conversation_id": row["source_conversation_id"],
        "title": row["title"] or "",
        "created": row["create_time"] or "",
        "updated": row["update_time"] or "",
        "model": row["model"] or "",
        "security_class": row["security_class"],
        "archive_version": row["version_no"],
        "message_count": row["message_count"],
        "source_method": row["source_method"],
        "adapter": row["adapter"],
        "extraction_date": row["extraction_date"],
        "source_sha256": row["source_sha256"],
        "source_record_id": row["record_id"],
        "projected_at": utcnow(),
        "tags": ["arcs/conversation", "arcs/class/" + row["security_class"].lower()],
    }
    parts = [frontmatter(meta), "# %s\n\n" % (row["title"] or "(untitled conversation)"), _BANNER, "\n"]
    parts.append("Index: %s\n\n" % _link(Path(VAULT_INDEX).stem))

    if not visible:
        parts.append(
            "> [!warning] Withheld\n"
            "> This conversation is classified **%s**, above the projection limit "
            "used to build this vault. Its existence, provenance and hash are "
            "recorded here; its content is not projected.\n> \n"
            "> Regenerate with `arcs project --max-class %s` if release is "
            "appropriate.\n\n" % (row["security_class"], row["security_class"])
        )
        parts.append(_provenance_section(archive, row))
        return "".join(parts)

    versions = archive.all(
        "SELECT version_no, created_at, correction_reason, source_sha256 "
        "FROM conversation_versions WHERE conversation_id=? ORDER BY version_no",
        (row["conversation_id"],),
    )
    if len(versions) > 1:
        parts.append("> [!note] Corrections\n> This conversation has %d archived versions; "
                     "earlier ones are retained in full.\n\n" % len(versions))

    parts.append("## Transcript\n\n")
    messages = archive.all(
        "SELECT mv.*, m.security_class AS message_class FROM conversation_version_messages cvm "
        "JOIN message_versions mv ON mv.version_id = cvm.message_version_id "
        "JOIN messages m ON m.message_id = mv.message_id "
        "WHERE cvm.conversation_version_id=? ORDER BY cvm.seq",
        (row["version_id"],),
    )
    for msg in messages:
        cls = effective_class(row["security_class"], msg["message_class"])
        when = msg["create_time"] or ""
        flags = []
        if not msg["on_canonical_path"]:
            flags.append("off-path branch")
        if msg["branch_note"]:
            flags.append(msg["branch_note"])
        if cls != NORMAL:
            flags.append(cls)
        header = "### %d · %s%s%s" % (
            msg["seq"], msg["role"],
            " · %s" % when if when else "",
            " · _%s_" % "; ".join(flags) if flags else "",
        )
        parts.append(header + "\n\n")
        parts.append((msg["content_text"] or "").rstrip() + "\n\n")
        attachments = archive.all(
            "SELECT kind, name, source_uri, content_present, blob_sha256, mime_type "
            "FROM attachments WHERE message_version_id=? ORDER BY attachment_id",
            (msg["version_id"],)
        )
        for att in attachments:
            label = att["name"] or att["source_uri"] or "(unnamed)"
            if att["content_present"]:
                held = " — file held, `%s`" % (att["blob_sha256"] or "")[:16]
            else:
                held = " _(reference only: the file itself is not in the archive)_"
            parts.append("- %s **%s**%s\n" % (att["kind"], label, held))
        if attachments:
            parts.append("\n")
        parts.append("^m%d\n\n" % msg["seq"])

    if include_derived:
        props = _propositions_for(archive, row["conversation_id"])
        if props:
            parts.append("## Propositions extracted (derived)\n\n")
            for prop in props:
                parts.append("- %s — %s\n" % (
                    _link(_prop_note_name(prop["proposition_id"]),
                          (prop["quote_text"] or "")[:70]),
                    prop["verification_status"],
                ))
            parts.append("\n")

    parts.append(_provenance_section(archive, row))
    return "".join(parts)


def _provenance_section(archive, row) -> str:
    sightings = archive.all(
        "SELECT s.observed_at, sr.source_method, sr.adapter, sr.extraction_date "
        "FROM conversation_version_sightings s "
        "JOIN source_records sr ON sr.record_id = s.source_record_id "
        "WHERE s.version_id=? ORDER BY s.observed_at", (row["version_id"],)
    )
    lines = [
        "## Provenance\n\n",
        "| field | value |\n|---|---|\n",
        "| source conversation id | `%s` |\n" % row["source_conversation_id"],
        "| archive id | `%s` |\n" % row["conversation_id"],
        "| archive version | %d |\n" % row["version_no"],
        "| source method | %s |\n" % row["source_method"],
        "| adapter | %s |\n" % row["adapter"],
        "| extraction date | %s |\n" % row["extraction_date"],
        "| source SHA-256 | `%s` |\n" % row["source_sha256"],
        "| source URI | %s |\n" % (row["source_uri"] or "—"),
        "| security class | %s |\n" % row["security_class"],
        "| times observed | %d |\n" % len(sightings),
    ]
    lines.append(
        "\nRecover the exact original bytes with:\n\n"
        "```\narcs recover --conversation %s --out ./recovered\n```\n" % row["conversation_id"]
    )
    return "".join(lines)


def _prop_note_name(proposition_id: str) -> str:
    return "prop--%s" % proposition_id[:12]


def _propositions_for(archive, conversation_id: str) -> list:
    try:
        return archive.all(
            "SELECT pv.* FROM proposition_versions pv WHERE pv.is_current=1 "
            "AND pv.conversation_id=? ORDER BY pv.event_date, pv.created_at",
            (conversation_id,), conn=archive.derived,
        )
    except Exception:  # derived database absent or empty
        return []


def _project_derived(archive, out_dir: Path, max_class: str) -> tuple:
    props = archive.all(
        "SELECT * FROM proposition_versions WHERE is_current=1 ORDER BY arc, event_date",
        conn=archive.derived,
    )
    if not props:
        return 0, 0
    (out_dir / PROP_DIR).mkdir(parents=True, exist_ok=True)
    (out_dir / ARC_DIR).mkdir(parents=True, exist_ok=True)

    visible_conversations = {
        r["conversation_id"]: r for r in archive.all(
            "SELECT c.conversation_id, c.security_class, cv.title "
            "FROM conversations c JOIN conversation_versions cv "
            "ON cv.conversation_id=c.conversation_id AND cv.is_current=1"
        )
    }

    edges_out, edges_in = {}, {}
    for edge in archive.all(
        "SELECT * FROM discovery_edges WHERE from_kind='proposition' AND to_kind='proposition'",
        conn=archive.derived,
    ):
        edges_out.setdefault(edge["from_id"], []).append(edge)
        edges_in.setdefault(edge["to_id"], []).append(edge)

    arcs: dict = {}
    written = 0
    for prop in props:
        conv = visible_conversations.get(prop["conversation_id"])
        if conv and not permits(max_class, conv["security_class"]):
            continue  # never surface a quotation from withheld material
        note_name = _prop_note_name(prop["proposition_id"])
        conv_note = (
            conversation_note_name(prop["conversation_id"], conv["title"]) if conv else ""
        )
        meta = {
            "arcs_id": prop["proposition_id"],
            "arcs_type": "proposition",
            "arcs_version": prop["version_no"],
            "conversation": prop["conversation_id"],
            "message": prop["message_id"],
            "message_version": prop["message_version_id"],
            "event_date": prop["event_date"] or "",
            "entity": prop["entity"] or "",
            "location": prop["location"] or "",
            "number": prop["number_raw"] or "",
            "arc": prop["arc"] or "",
            "verification_status": prop["verification_status"],
            "probability_role": prop["probability_role"] or "",
            "probability": prop["probability"],
            "created_by": prop["created_by"],
            "quote_sha256": prop["quote_sha256"],
            "source_sha256": prop["source_sha256"],
            "tags": ["arcs/proposition", "arcs/arc/" + slugify(prop["arc"] or "unassigned")],
        }
        body = [frontmatter(meta), "# Proposition %s\n\n" % prop["proposition_id"][:12], _BANNER, "\n"]
        body.append("## Exact quotation\n\n> %s\n\n" % (prop["quote_text"] or "").replace("\n", "\n> "))
        if conv_note:
            anchor = "#^m%s" % _seq_for(archive, prop["message_version_id"])
            body.append("Source: %s\n\n" % _link(conv_note + anchor, "in context"))
        body.append("## Contemporaneous meaning\n\n%s\n\n"
                    % (prop["contemporaneous_meaning"] or "_not recorded_"))
        body.append("## Later interpretation (derived)\n\n%s\n\n"
                    % (prop["later_interpretation"] or "_none_"))
        if prop["arc"]:
            body.append("Arc: %s\n\n" % _link(prop["arc"]))
            arcs.setdefault(prop["arc"], []).append((prop, note_name))
        prompted = prop["prompted_by"]
        if prompted and prop["prompted_by_kind"] == "proposition":
            body.append("Prompted by: %s\n\n" % _link(_prop_note_name(prompted)))
        nexts = edges_out.get(prop["proposition_id"], [])
        if nexts:
            body.append("## Generated next\n\n")
            for edge in nexts:
                body.append("- %s (%s)\n" % (_link(_prop_note_name(edge["to_id"])), edge["relation"]))
            body.append("\n")
        backs = edges_in.get(prop["proposition_id"], [])
        if backs:
            body.append("## Prompted by\n\n")
            for edge in backs:
                body.append("- %s (%s)\n" % (_link(_prop_note_name(edge["from_id"])), edge["relation"]))
            body.append("\n")
        body.append("## Provenance\n\n"
                    "| field | value |\n|---|---|\n"
                    "| verification | %s |\n| probability role | %s |\n"
                    "| extracted by | %s |\n| method | %s |\n"
                    "| quote SHA-256 | `%s` |\n| source SHA-256 | `%s` |\n"
                    % (prop["verification_status"], prop["probability_role"] or "—",
                       prop["created_by"], prop["method"] or "—",
                       prop["quote_sha256"], prop["source_sha256"]))
        (out_dir / PROP_DIR / (note_name + ".md")).write_text("".join(body), encoding="utf-8")
        written += 1

    for arc_name, members in sorted(arcs.items()):
        body = [
            frontmatter({"arcs_type": "arc", "arc": arc_name,
                         "tags": ["arcs/arc", "arcs/arc/" + slugify(arc_name)]}),
            "# %s\n\n" % arc_name, _BANNER, "\n## Propositions\n\n",
        ]
        for prop, note_name in sorted(members, key=lambda p: (p[0]["event_date"] or "")):
            body.append("- `%s` %s — %s\n" % (prop["event_date"] or "undated",
                                              _link(note_name), (prop["quote_text"] or "")[:70]))
        (out_dir / ARC_DIR / (slugify(arc_name) + ".md")).write_text("".join(body), encoding="utf-8")

    return written, len(arcs)


def _seq_for(archive, message_version_id: str):
    row = archive.one("SELECT seq FROM message_versions WHERE version_id=?", (message_version_id,))
    return row["seq"] if row else 0


def _index_note(archive, entries, max_class, result) -> str:
    lines = [
        frontmatter({"arcs_type": "index", "projected_at": utcnow(),
                     "projection_max_class": max_class, "tags": ["arcs/index"]}),
        "# ARCS Archive\n\n", _BANNER, "\n",
        "Projected %d conversation(s); %d withheld as stubs above class **%s**.\n\n"
        % (result.conversations, result.stubs, max_class),
        "## Conversations\n\n",
        "| date | conversation | messages | class | source |\n|---|---|---|---|---|\n",
    ]
    for row, note_name, visible in entries:
        lines.append(
            "| %s | %s | %d | %s | %s |\n"
            % ((row["create_time"] or "")[:10], _link(note_name, row["title"] or note_name),
               row["message_count"], row["security_class"],
               row["source_method"] + ("" if visible else " · withheld"))
        )
    counts = {
        "conversations": archive.scalar("SELECT COUNT(*) FROM conversations"),
        "messages": archive.scalar("SELECT COUNT(*) FROM messages"),
        "message versions": archive.scalar("SELECT COUNT(*) FROM message_versions"),
        "source records": archive.scalar("SELECT COUNT(*) FROM source_records"),
        "source blobs": archive.scalar("SELECT COUNT(*) FROM source_blobs"),
    }
    lines.append("\n## Archive counts\n\n")
    for key, value in counts.items():
        lines.append("- %s: %s\n" % (key, value))
    lines.append(
        "\n## How to read this vault\n\n"
        "- Notes here are **derived**. The source of truth is `db/source.sqlite3` "
        "plus the hashed blobs under `source/blobs/`.\n"
        "- Every conversation note carries its source SHA-256; `arcs recover` "
        "returns the exact original bytes.\n"
        "- Message anchors (`^m3`) let propositions link the precise message they quote.\n"
        "- Notes in *%s* and *%s* are interpretations, kept separate from source material.\n"
        % (PROP_DIR, ARC_DIR)
    )
    return "".join(lines)
