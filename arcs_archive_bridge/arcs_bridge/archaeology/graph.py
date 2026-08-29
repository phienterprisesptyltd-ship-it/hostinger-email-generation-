"""The directed discovery graph.

The question this answers: *what made me look?*  For any discovery in the
archive it should be possible to walk back through the observations, searches
and earlier propositions that led to it, and forward to everything it went on
to generate.

Nodes are typed and live where they belong: ``proposition``, ``search_event``
(an observation, an archive query, a field visit), ``conversation`` and
``message``.  Edges are directed and carry a relation and a rationale, so the
graph records not just *that* one thing followed another but *why* the
researcher thought so.
"""

from __future__ import annotations

import json
from collections import deque

from ..errors import ArcsError
from ..gate import authorise
from ..util import new_id, utcnow

NODE_KINDS = ("proposition", "search_event", "conversation", "message")

RELATIONS = (
    "prompted",       # A made the researcher look, and B is what they looked at
    "generated",      # A directly produced B
    "corroborates",   # B supports A
    "contradicts",    # B conflicts with A
    "corrects",       # B corrects A
    "cites",          # A cites B
    "answers",        # B answers the question in A
    "derived_from",   # B was inferred from A
)


def add_search_event(archive, kind: str, query: str = "", tool: str = "",
                     conversation_id=None, message_id=None, result_summary: str = "",
                     result_count=None, occurred_at=None, notes: str = "",
                     created_by: str = "", gate_token=None) -> str:
    """Record an observation or search: the thing that generates a discovery."""
    if gate_token is None:
        authorise(archive, purpose="recording a search event")
    search_event_id = new_id("search")
    archive.derived.execute(
        "INSERT INTO search_events(search_event_id, occurred_at, kind, query, tool, "
        "conversation_id, message_id, result_summary, result_count, created_by, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (search_event_id, occurred_at or utcnow(), kind, query, tool, conversation_id,
         message_id, result_summary, result_count,
         created_by or ("human:" + (archive.config.operator or "unattributed")), notes),
    )
    archive.derived.commit()
    return search_event_id


def add_edge(archive, from_kind: str, from_id: str, to_kind: str, to_id: str,
             relation: str = "generated", rationale: str = "", weight=None,
             created_by: str = "", interpretation_run_id=None, gate_token=None) -> str:
    """Add a directed edge.  Idempotent on (from, to, relation)."""
    if gate_token is None:
        authorise(archive, purpose="adding a discovery edge")
    if from_kind not in NODE_KINDS or to_kind not in NODE_KINDS:
        raise ArcsError("node kind must be one of %s" % ", ".join(NODE_KINDS))
    if relation not in RELATIONS:
        raise ArcsError("relation must be one of %s" % ", ".join(RELATIONS))
    edge_id = new_id("edge")
    archive.derived.execute(
        "INSERT INTO discovery_edges(edge_id, from_kind, from_id, to_kind, to_id, relation, "
        "weight, rationale, created_by, created_at, interpretation_run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(from_kind, from_id, to_kind, to_id, relation) DO UPDATE SET "
        "rationale=excluded.rationale, weight=excluded.weight",
        (edge_id, from_kind, from_id, to_kind, to_id, relation, weight, rationale,
         created_by or ("human:" + (archive.config.operator or "unattributed")),
         utcnow(), interpretation_run_id),
    )
    archive.derived.commit()
    return edge_id


def _neighbours(archive, kind: str, node_id: str, forward: bool):
    if forward:
        rows = archive.all(
            "SELECT to_kind AS kind, to_id AS id, relation, rationale FROM discovery_edges "
            "WHERE from_kind=? AND from_id=?", (kind, node_id), conn=archive.derived,
        )
    else:
        rows = archive.all(
            "SELECT from_kind AS kind, from_id AS id, relation, rationale FROM discovery_edges "
            "WHERE to_kind=? AND to_id=?", (kind, node_id), conn=archive.derived,
        )
    return [(r["kind"], r["id"], r["relation"], r["rationale"]) for r in rows]


def _walk(archive, kind: str, node_id: str, forward: bool, max_depth: int = 12) -> list:
    seen = {(kind, node_id)}
    queue = deque([(kind, node_id, 0, None, None)])
    out = []
    while queue:
        k, i, depth, relation, rationale = queue.popleft()
        if depth:
            out.append({"kind": k, "id": i, "depth": depth, "relation": relation,
                        "rationale": rationale})
        if depth >= max_depth:
            continue
        for nk, ni, rel, why in _neighbours(archive, k, i, forward):
            if (nk, ni) in seen:
                continue
            seen.add((nk, ni))
            queue.append((nk, ni, depth + 1, rel, why))
    return out


def descendants(archive, kind: str, node_id: str, max_depth: int = 12) -> list:
    """Everything this observation went on to generate."""
    return _walk(archive, kind, node_id, forward=True, max_depth=max_depth)


def ancestors(archive, kind: str, node_id: str, max_depth: int = 12) -> list:
    """Everything that led to this discovery."""
    return _walk(archive, kind, node_id, forward=False, max_depth=max_depth)


def discovery_chain(archive, kind: str, node_id: str, max_depth: int = 12) -> list:
    """The shortest path back to a root observation, nearest ancestor first."""
    chain = []
    current = (kind, node_id)
    seen = {current}
    for _ in range(max_depth):
        parents = _neighbours(archive, current[0], current[1], forward=False)
        if not parents:
            break
        parent = parents[0]
        key = (parent[0], parent[1])
        if key in seen:
            break
        seen.add(key)
        chain.append({"kind": parent[0], "id": parent[1], "relation": parent[2],
                      "rationale": parent[3]})
        current = key
    return chain


def _label(archive, kind: str, node_id: str) -> str:
    if kind == "proposition":
        row = archive.one(
            "SELECT quote_text, event_date, arc FROM proposition_versions "
            "WHERE proposition_id=? AND is_current=1", (node_id,), conn=archive.derived,
        )
        if row:
            text = (row["quote_text"] or "")[:60].replace("\n", " ")
            return "%s\\n%s" % (row["event_date"] or "undated", text)
    elif kind == "search_event":
        row = archive.one(
            "SELECT kind, query, occurred_at FROM search_events WHERE search_event_id=?",
            (node_id,), conn=archive.derived,
        )
        if row:
            return "%s: %s" % (row["kind"], (row["query"] or "")[:50])
    elif kind == "conversation":
        row = archive.one(
            "SELECT title FROM conversation_versions WHERE conversation_id=? AND is_current=1",
            (node_id,),
        )
        if row:
            return (row["title"] or node_id)[:60]
    return node_id[:16]


_SHAPES = {"proposition": "box", "search_event": "ellipse", "conversation": "folder",
           "message": "note"}


def to_json(archive, arc=None) -> dict:
    """The whole graph (or one arc's subgraph) as nodes and edges."""
    edges = archive.all("SELECT * FROM discovery_edges", conn=archive.derived)
    edges = [dict(e) for e in edges]
    if arc:
        keep = {
            r["proposition_id"] for r in archive.all(
                "SELECT proposition_id FROM proposition_versions WHERE is_current=1 AND arc=?",
                (arc,), conn=archive.derived,
            )
        }
        edges = [e for e in edges
                 if (e["from_kind"] == "proposition" and e["from_id"] in keep)
                 or (e["to_kind"] == "proposition" and e["to_id"] in keep)]
    nodes = {}
    for edge in edges:
        for kind, node_id in ((edge["from_kind"], edge["from_id"]),
                              (edge["to_kind"], edge["to_id"])):
            nodes.setdefault((kind, node_id), {"kind": kind, "id": node_id,
                                               "label": _label(archive, kind, node_id)})
    return {"generated_at": utcnow(), "arc": arc, "nodes": list(nodes.values()), "edges": edges}


def to_dot(archive, arc=None) -> str:
    """Graphviz DOT, so the discovery chain can be looked at, not just queried."""
    graph = to_json(archive, arc)
    lines = ["digraph arcs_discovery {", "  rankdir=LR;",
             '  node [fontname="Helvetica", fontsize=10];',
             '  edge [fontname="Helvetica", fontsize=9];']
    for node in graph["nodes"]:
        lines.append('  "%s" [label=%s, shape=%s];'
                     % (node["id"], json.dumps(node["label"]), _SHAPES.get(node["kind"], "box")))
    for edge in graph["edges"]:
        lines.append('  "%s" -> "%s" [label=%s];'
                     % (edge["from_id"], edge["to_id"], json.dumps(edge["relation"])))
    lines.append("}")
    return "\n".join(lines) + "\n"
