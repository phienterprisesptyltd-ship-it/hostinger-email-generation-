"""Local full-text search over the archive.

SQLite FTS5, entirely offline.  The index covers *current* message versions;
superseded versions stay in the database and are reachable through the version
history, but a search result should show what the archive currently holds.

Security classes are applied by joining back to the authoritative rows rather
than by trusting a copy inside the index, so reclassifying a conversation takes
effect immediately and cannot be defeated by a stale index entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .security import effective_class, permits

_FTS_SPECIAL = re.compile(r'["^*():{}\[\]]')
_OPERATORS = ("AND", "OR", "NOT", "NEAR")


def escape_query(query: str) -> str:
    """Make user input safe for FTS5 while keeping obvious operators usable."""
    query = (query or "").strip()
    if not query:
        return '""'
    if query.startswith("fts:"):  # explicit escape hatch for raw FTS5 syntax
        return query[4:]
    tokens = query.split()
    terms = []
    for position, token in enumerate(tokens):
        upper = token.upper()
        # An operator only means anything between two terms; leading, trailing
        # or lone keywords are what the user typed, so quote them.
        if upper in _OPERATORS and 0 < position < len(tokens) - 1:
            terms.append(upper)
            continue
        cleaned = _FTS_SPECIAL.sub(" ", token).strip()
        if cleaned:
            terms.append('"%s"' % cleaned.replace('"', ""))
    if not terms or all(t in _OPERATORS for t in terms):
        return '""'
    return " ".join(terms)


@dataclass
class SearchHit:
    message_id: str
    version_id: str
    conversation_id: str
    conversation_title: str
    source_conversation_id: str
    role: str
    seq: int
    created: str
    snippet: str
    score: float
    security_class: str
    on_canonical_path: bool = True
    content_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchResult:
    query: str
    hits: list = field(default_factory=list)
    total: int = 0
    withheld: int = 0
    withheld_note: str = ""

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "total": self.total,
            "withheld": self.withheld,
            "withheld_note": self.withheld_note,
            "hits": [h.to_dict() for h in self.hits],
        }


def search_messages(archive, query: str, limit: int = 20, max_class=None,
                    conversation_id=None, role=None, include_text: bool = False) -> SearchResult:
    match = escape_query(query)
    sql = (
        "SELECT f.message_id, f.version_id, f.conversation_id, f.role, "
        "       bm25(message_fts) AS score, "
        "       snippet(message_fts, 0, '**', '**', ' … ', 18) AS snippet, "
        "       mv.seq, mv.create_time, mv.content_text, mv.on_canonical_path, "
        "       m.security_class AS message_class, c.security_class AS conversation_class, "
        "       c.source_conversation_id, cv.title "
        "FROM message_fts f "
        "JOIN message_versions mv ON mv.version_id = f.version_id "
        "JOIN messages m ON m.message_id = f.message_id "
        "JOIN conversations c ON c.conversation_id = f.conversation_id "
        "LEFT JOIN conversation_versions cv ON cv.conversation_id = c.conversation_id "
        "     AND cv.is_current = 1 "
        "WHERE message_fts MATCH ? "
    )
    params: list = [match]
    if conversation_id:
        sql += "AND f.conversation_id = ? "
        params.append(conversation_id)
    if role:
        sql += "AND f.role = ? "
        params.append(role)
    sql += "ORDER BY score LIMIT ?"
    params.append(max(limit * 4, limit))  # over-fetch, then apply class filtering

    result = SearchResult(query=query)
    for row in archive.all(sql, tuple(params)):
        cls = effective_class(row["conversation_class"], row["message_class"])
        if max_class is not None and not permits(max_class, cls):
            result.withheld += 1
            continue
        if len(result.hits) >= limit:
            continue
        result.hits.append(
            SearchHit(
                message_id=row["message_id"],
                version_id=row["version_id"],
                conversation_id=row["conversation_id"],
                conversation_title=row["title"] or "",
                source_conversation_id=row["source_conversation_id"],
                role=row["role"],
                seq=row["seq"],
                created=row["create_time"] or "",
                snippet=(row["snippet"] or "").replace("\n", " "),
                score=-float(row["score"]),  # bm25 is negative-better; flip for display
                security_class=cls,
                on_canonical_path=bool(row["on_canonical_path"]),
                content_text=row["content_text"] if include_text else "",
            )
        )
    result.total = len(result.hits)
    if result.withheld:
        result.withheld_note = (
            "%d match(es) withheld: above the %s security class"
            % (result.withheld, max_class)
        )
    return result


def search_conversations(archive, query: str, limit: int = 20, max_class=None) -> list:
    rows = archive.all(
        "SELECT f.conversation_id, f.title, bm25(conversation_fts) AS score, "
        "       c.security_class, c.source_conversation_id "
        "FROM conversation_fts f "
        "JOIN conversations c ON c.conversation_id = f.conversation_id "
        "WHERE conversation_fts MATCH ? ORDER BY score LIMIT ?",
        (escape_query(query), limit),
    )
    out = []
    for row in rows:
        if max_class is not None and not permits(max_class, row["security_class"]):
            continue
        out.append(dict(row))
    return out
