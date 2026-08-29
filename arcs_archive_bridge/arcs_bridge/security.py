"""Security classes.

Four classes, ordered from least to most restricted:

``Normal``
    Ordinary material.  May leave the archive in an evidence packet.
``Private``
    Personal material.  Requires an explicit flag to leave the archive.
``Restricted``
    Legally or commercially sensitive.  Requires an explicit flag *and* a
    recorded acknowledgement.
``Sacred``
    Culturally sensitive / sacred material.  Never leaves the archive by
    default; release requires a named acknowledgement string recorded in the
    packet manifest and in the operation log, and is refused otherwise.

Classification is append-only history: the current class lives on the
conversation/message row, and every change is written to
``security_class_history`` with a rationale.
"""

from __future__ import annotations

from .errors import SecurityClassViolation
from .util import new_id, utcnow

NORMAL = "Normal"
PRIVATE = "Private"
RESTRICTED = "Restricted"
SACRED = "Sacred"

#: Canonical order, least to most restricted.
CLASSES = (NORMAL, PRIVATE, RESTRICTED, SACRED)

#: Accepted aliases, mapped to the canonical name.
ALIASES = {
    "normal": NORMAL,
    "public": NORMAL,
    "private": PRIVATE,
    "restricted": RESTRICTED,
    "sacred": SACRED,
    "culturally sensitive": SACRED,
    "sacred/culturally sensitive": SACRED,
    "sacred_culturally_sensitive": SACRED,
}

#: Classes that may never be exported without an explicit acknowledgement.
REQUIRES_ACKNOWLEDGEMENT = (RESTRICTED, SACRED)


def parse_class(value: str) -> str:
    if value in CLASSES:
        return value
    key = (value or "").strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    raise SecurityClassViolation(
        "unknown security class %r (expected one of %s)" % (value, ", ".join(CLASSES))
    )


def rank(value: str) -> int:
    return CLASSES.index(parse_class(value))


def most_restrictive(*values: str) -> str:
    present = [v for v in values if v]
    if not present:
        return NORMAL
    return max(present, key=rank)


def permits(max_class: str, subject_class: str) -> bool:
    """True when material classified ``subject_class`` fits inside ``max_class``."""
    return rank(subject_class) <= rank(max_class)


def set_class(archive, subject_type: str, subject_id: str, security_class: str,
              rationale: str = "", actor: str = "", cascade: bool = False) -> str:
    """Reclassify a conversation or message, recording the change."""
    security_class = parse_class(security_class)
    if subject_type not in ("conversation", "message"):
        raise SecurityClassViolation("subject_type must be 'conversation' or 'message'")
    table = "conversations" if subject_type == "conversation" else "messages"
    id_col = "conversation_id" if subject_type == "conversation" else "message_id"
    row = archive.one(
        "SELECT security_class FROM %s WHERE %s=?" % (table, id_col), (subject_id,)
    )
    if row is None:
        raise SecurityClassViolation("no such %s: %s" % (subject_type, subject_id))
    previous = row["security_class"]
    entry_id = new_id("sec")
    with archive.transaction():
        archive.source.execute(
            "UPDATE %s SET security_class=? WHERE %s=?" % (table, id_col),
            (security_class, subject_id),
        )
        archive.source.execute(
            "INSERT INTO security_class_history"
            "(entry_id, subject_type, subject_id, previous_class, security_class, rationale, set_by, set_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (entry_id, subject_type, subject_id, previous, security_class,
             rationale, actor or archive.config.operator, utcnow()),
        )
        if cascade and subject_type == "conversation":
            for msg in archive.all(
                "SELECT message_id, security_class FROM messages WHERE conversation_id=?",
                (subject_id,),
            ):
                archive.source.execute(
                    "UPDATE messages SET security_class=? WHERE message_id=?",
                    (security_class, msg["message_id"]),
                )
                archive.source.execute(
                    "INSERT INTO security_class_history"
                    "(entry_id, subject_type, subject_id, previous_class, security_class, rationale, set_by, set_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (new_id("sec"), "message", msg["message_id"], msg["security_class"],
                     security_class, "cascade from conversation " + subject_id,
                     actor or archive.config.operator, utcnow()),
                )
    return entry_id


def effective_class(conversation_class: str, message_class: str) -> str:
    """A message is never less restricted than the conversation it sits in."""
    return most_restrictive(conversation_class, message_class)


def history(archive, subject_type: str, subject_id: str):
    return archive.all(
        "SELECT * FROM security_class_history WHERE subject_type=? AND subject_id=? "
        "ORDER BY set_at",
        (subject_type, subject_id),
    )
