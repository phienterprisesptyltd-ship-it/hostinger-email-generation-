"""``arcs`` - the command line for the ARCS Archive Bridge.

Everything the application does is reachable from here, and the process seals
its own network access before parsing arguments: no command can upload the
archive, because no socket can leave the machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import adapters as adapter_registry
from . import evidence, gate, integrity, netguard, obsidian, reconstruct, search, semantic
from .archaeology import extract as arch_extract
from .archaeology import graph as arch_graph
from .archaeology import propositions as arch_props
from .config import ArchiveConfig, resolve_root
from .db import Archive
from .errors import ArcsError
from .ingest import ingest_path
from .security import CLASSES, set_class
from .util import utcnow

PROG = "arcs"


# --------------------------------------------------------------------- helpers
def _open(args) -> Archive:
    config = ArchiveConfig.load(resolve_root(getattr(args, "archive", None)))
    return Archive.open(config)


def _emit(args, payload, text_fn) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        text_fn()


def _resolve_conversation(archive, token: str) -> str:
    row = archive.one(
        "SELECT conversation_id FROM conversations WHERE conversation_id=? "
        "OR source_conversation_id=?", (token, token),
    )
    if row:
        return row["conversation_id"]
    rows = archive.all(
        "SELECT conversation_id FROM conversations WHERE conversation_id LIKE ? "
        "OR source_conversation_id LIKE ?", (token + "%", token + "%"),
    )
    if len(rows) == 1:
        return rows[0]["conversation_id"]
    if not rows:
        raise ArcsError("no conversation matching %r" % token)
    raise ArcsError("%r matches %d conversations; be more specific" % (token, len(rows)))


# ----------------------------------------------------------------- commands
def cmd_init(args) -> int:
    root = resolve_root(args.path or getattr(args, "archive", None))
    config = ArchiveConfig(
        root=root,
        archive_name=args.name or "ARCS Archive",
        operator=args.operator or "",
        created_at=utcnow(),
    )
    config.save()
    archive = Archive.open(config)
    archive.log("init", {"root": str(root), "operator": config.operator})
    print("Initialised %s at %s" % (config.archive_name, root))
    print("  source database : %s" % config.source_db_path)
    print("  derived database: %s   (interpretations, kept separate)" % config.derived_db_path)
    print("  source blobs    : %s" % config.blobs_dir)
    print("\nNext: %s ingest <export-or-capture-file>" % PROG)
    return 0


def cmd_adapters(args) -> int:
    rows = [a.describe() for a in adapter_registry.available()]
    _emit(args, rows, lambda: [
        print("%-16s %-26s %s" % (r["name"], r["source_method"], r["description"]))
        for r in rows
    ])
    return 0


def cmd_ingest(args) -> int:
    archive = _open(args)
    result = ingest_path(
        archive, args.path, adapter_name=args.adapter, operator=args.operator or "",
        notes=args.notes or "", default_security_class=args.security_class,
        correction_reason=args.reason or "", credential_policy=args.credential_policy,
    )

    def show():
        print("Batch %s · adapter %s · method %s" % (result.batch_id, result.adapter,
                                                     result.source_method))
        print("Input  %s\n  sha256 %s\n  extracted %s"
              % (result.input_path, result.input_sha256, result.extraction_date))
        for warning in result.warnings:
            print("  ! %s" % warning)
        print()
        for conv in result.conversations:
            flags = (" [%s]" % ", ".join(conv.flags)) if conv.flags else ""
            print("  %-9s v%-2d %-34s %+3d msg  %-10s %s%s"
                  % (conv.status, conv.version_no, (conv.title or conv.source_conversation_id)[:34],
                     conv.messages_new + conv.messages_updated, conv.security_class,
                     conv.source_sha256[:12], flags))
        print("\n%d conversation(s): %d new, %d updated, %d unchanged"
              % (len(result.conversations), result.new_count, result.updated_count,
                 result.unchanged_count))
        print("Run `%s verify` to re-check the archive after ingestion." % PROG)

    _emit(args, result.to_dict(), show)
    return 0


def cmd_status(args) -> int:
    archive = _open(args)
    config = archive.config
    counts = {
        "conversations": archive.scalar("SELECT COUNT(*) FROM conversations"),
        "conversation_versions": archive.scalar("SELECT COUNT(*) FROM conversation_versions"),
        "messages": archive.scalar("SELECT COUNT(*) FROM messages"),
        "message_versions": archive.scalar("SELECT COUNT(*) FROM message_versions"),
        "source_records": archive.scalar("SELECT COUNT(*) FROM source_records"),
        "source_blobs": archive.scalar("SELECT COUNT(*) FROM source_blobs"),
        "duplicate_records": archive.scalar(
            "SELECT COUNT(*) FROM source_records WHERE duplicate_of_record_id IS NOT NULL"),
        "attachments": archive.scalar("SELECT COUNT(*) FROM attachments"),
        "import_batches": archive.scalar("SELECT COUNT(*) FROM import_batches"),
        "evidence_packets": archive.scalar("SELECT COUNT(*) FROM evidence_packets"),
        "propositions": archive.scalar("SELECT COUNT(*) FROM propositions", conn=archive.derived),
        "discovery_edges": archive.scalar("SELECT COUNT(*) FROM discovery_edges",
                                          conn=archive.derived),
        "embeddings": archive.scalar("SELECT COUNT(*) FROM embeddings", conn=archive.derived),
    }
    by_class = {
        r["security_class"]: r["n"] for r in archive.all(
            "SELECT security_class, COUNT(*) AS n FROM conversations GROUP BY security_class")
    }
    gate_state = gate.status(archive)
    payload = {"archive": config.archive_name, "root": str(config.root), "counts": counts,
               "security_classes": by_class, "gate": gate_state,
               "network": netguard.status()}

    def show():
        print("%s  (%s)" % (config.archive_name, config.root))
        print("  network        : %s" % ("SEALED - nothing leaves this machine"
                                         if netguard.is_sealed() else "UNSEALED"))
        print("  interpretation : %s (%s)" % ("OPEN" if gate_state["open"] else "CLOSED",
                                              gate_state["reason"]))
        print()
        width = max(len(k) for k in counts)
        for key, value in counts.items():
            print("  %-*s %s" % (width, key, value))
        if by_class:
            print("\n  conversations by security class:")
            for cls in CLASSES:
                if cls in by_class:
                    print("    %-10s %d" % (cls, by_class[cls]))

    _emit(args, payload, show)
    return 0


def cmd_list(args) -> int:
    archive = _open(args)
    rows = archive.all(
        "SELECT c.conversation_id, c.source_conversation_id, c.security_class, cv.title, "
        "cv.create_time, cv.message_count, cv.version_no, sr.source_method "
        "FROM conversations c "
        "JOIN conversation_versions cv ON cv.conversation_id=c.conversation_id AND cv.is_current=1 "
        "JOIN source_records sr ON sr.record_id=cv.source_record_id "
        "ORDER BY cv.create_time"
    )
    data = [dict(r) for r in rows]

    def show():
        print("%-10s %-8s %-46s %5s %4s %-10s %s"
              % ("date", "id", "title", "msgs", "ver", "class", "method"))
        for r in data:
            print("%-10s %-8s %-46s %5d %4d %-10s %s"
                  % ((r["create_time"] or "")[:10], r["conversation_id"][:8],
                     (r["title"] or "")[:46], r["message_count"], r["version_no"],
                     r["security_class"], r["source_method"]))
        print("\n%d conversation(s)" % len(data))

    _emit(args, data, show)
    return 0


def cmd_show(args) -> int:
    archive = _open(args)
    conversation_id = _resolve_conversation(archive, args.conversation)
    if args.version:
        version = archive.one(
            "SELECT * FROM conversation_versions WHERE conversation_id=? AND version_no=?",
            (conversation_id, args.version),
        )
    else:
        version = archive.one(
            "SELECT * FROM conversation_versions WHERE conversation_id=? AND is_current=1",
            (conversation_id,),
        )
    if version is None:
        raise ArcsError("no such version")
    messages = archive.all(
        "SELECT mv.* FROM conversation_version_messages cvm "
        "JOIN message_versions mv ON mv.version_id=cvm.message_version_id "
        "WHERE cvm.conversation_version_id=? ORDER BY cvm.seq", (version["version_id"],)
    )
    payload = {"conversation": dict(version), "messages": [dict(m) for m in messages]}

    def show():
        print("# %s" % (version["title"] or "(untitled)"))
        print("archive id %s · version %d · source sha256 %s"
              % (conversation_id, version["version_no"], version["source_sha256"][:16]))
        print()
        for msg in messages:
            marker = "" if msg["on_canonical_path"] else "  (off-path branch)"
            print("--- %d · %s · %s%s" % (msg["seq"], msg["role"],
                                          msg["create_time"] or "no timestamp", marker))
            text = msg["content_text"] or ""
            print(text if args.full else (text[:600] + ("…" if len(text) > 600 else "")))
            print("    [message_version_id %s]" % msg["version_id"])
            print()

    _emit(args, payload, show)
    return 0


def cmd_history(args) -> int:
    archive = _open(args)
    conversation_id = _resolve_conversation(archive, args.conversation)
    versions = archive.all(
        "SELECT cv.*, sr.source_method, sr.adapter, sr.extraction_date "
        "FROM conversation_versions cv JOIN source_records sr ON sr.record_id=cv.source_record_id "
        "WHERE cv.conversation_id=? ORDER BY cv.version_no", (conversation_id,)
    )
    payload = []
    for version in versions:
        sightings = archive.all(
            "SELECT observed_at FROM conversation_version_sightings WHERE version_id=? "
            "ORDER BY observed_at", (version["version_id"],)
        )
        payload.append({**dict(version), "sightings": [s["observed_at"] for s in sightings]})

    def show():
        print("Version history for %s\n" % conversation_id)
        for item in payload:
            print("v%-2d %s  %s"
                  % (item["version_no"], item["created_at"], "(current)" if item["is_current"] else ""))
            print("    title      : %s" % (item["title"] or ""))
            print("    messages   : %d" % item["message_count"])
            print("    source     : %s via %s, extracted %s"
                  % (item["source_method"], item["adapter"], item["extraction_date"]))
            print("    source hash: %s" % item["source_sha256"])
            print("    observed   : %d time(s)" % len(item["sightings"]))
            if item["correction_reason"]:
                print("    correction : %s" % item["correction_reason"])
            if item["supersedes_version_id"]:
                print("    supersedes : %s" % item["supersedes_version_id"])
            print()
        print("Earlier versions are retained in full; nothing was overwritten.")

    _emit(args, payload, show)
    return 0


def cmd_verify(args) -> int:
    archive = _open(args)
    report = integrity.run(archive, args.suite)

    def show():
        for check in report.checks:
            print("%s %-36s %s" % ("PASS" if check.passed else "FAIL", check.name, check.detail))
            for failure in check.failures[:5]:
                print("       - %s" % failure)
        print("\n%s: %d check(s), %d failed · fingerprint %s"
              % ("PASSED" if report.passed else "FAILED", len(report.checks),
                 len(report.failed), report.archive_fingerprint[:16]))
        if report.passed:
            print("Interpretation gate is open for this archive state.")

    _emit(args, report.to_dict(), show)
    return 0 if report.passed else 1


def cmd_recover(args) -> int:
    archive = _open(args)
    ids = None
    if args.conversation:
        ids = [_resolve_conversation(archive, c) for c in args.conversation]
    manifest = reconstruct.export_originals(
        archive, args.out, conversation_ids=ids, include_history=args.all_versions
    )
    verified = sum(1 for f in manifest["files"] if f["verified"])

    def show():
        print("Recovered %d file(s) to %s" % (len(manifest["files"]), args.out))
        for item in manifest["files"]:
            print("  %s %-46s %s" % ("ok" if item["verified"] else "BAD", item["file"],
                                     item["sha256"][:16]))
        print("\n%d/%d verified against their recorded SHA-256."
              % (verified, len(manifest["files"])))

    _emit(args, manifest, show)
    return 0 if verified == len(manifest["files"]) else 1


def cmd_reconstruct(args) -> int:
    archive = _open(args)
    ids = [_resolve_conversation(archive, c) for c in args.conversation] if args.conversation else None
    reports = reconstruct.verify_all(archive, conversation_ids=ids,
                                     include_history=args.all_versions)
    payload = [r.to_dict() for r in reports]

    def show():
        for report in reports:
            print("%s %-40s v%-2d bytes:%s  tables:%s  %s"
                  % ("OK  " if report.ok else "FAIL", (report.title or "")[:40],
                     report.version_no,
                     "exact" if report.bytes_sha256_match else "FAILED",
                     "exact" if report.structural_match else "FAILED",
                     report.error or ""))
            for difference in report.differences[:5]:
                print("       - %s" % difference)
        ok = sum(1 for r in reports if r.ok)
        print("\n%d/%d version(s) reconstructed from the archive." % (ok, len(reports)))

    _emit(args, payload, show)
    return 0 if all(r.ok for r in reports) else 1


def cmd_search(args) -> int:
    archive = _open(args)
    result = search.search_messages(
        archive, args.query, limit=args.limit, max_class=args.max_class,
        conversation_id=args.conversation, role=args.role, include_text=args.full,
    )

    def show():
        for hit in result.hits:
            print("%-40s  %s · msg %d · %s"
                  % ((hit.conversation_title or "")[:40], hit.role, hit.seq,
                     (hit.created or "")[:10]))
            print("    %s" % hit.snippet)
            print("    [%s] class=%s" % (hit.version_id, hit.security_class))
        print("\n%d hit(s)%s" % (result.total,
                                 "; " + result.withheld_note if result.withheld else ""))

    _emit(args, result.to_dict(), show)
    return 0


def cmd_semantic(args) -> int:
    archive = _open(args)
    if args.action == "index":
        result = semantic.build_index(
            archive, subject_type=args.subject, backend=args.backend,
            model_path=args.model_path or "", dim=args.dim, rebuild=args.rebuild,
        )
        _emit(args, result.__dict__, lambda: print(
            "Indexed %d %s(s) (%d unchanged) with %s, %d dimensions."
            % (result.indexed, result.subject_type, result.skipped, result.model_name, result.dim)))
        return 0
    hits = semantic.semantic_search(
        archive, args.query, k=args.limit, subject_type=args.subject, backend=args.backend,
        model_path=args.model_path or "", dim=args.dim, max_class=args.max_class,
    )

    def show():
        for hit in hits:
            print("%.4f  %-38s %s · msg %s"
                  % (hit["score"], (hit.get("title") or "")[:38], hit.get("role", ""),
                     hit.get("seq", "")))
            text = (hit.get("text") or "").replace("\n", " ")
            print("       %s" % (text[:150] + ("…" if len(text) > 150 else "")))

    _emit(args, hits, show)
    return 0


def cmd_project(args) -> int:
    archive = _open(args)
    result = obsidian.project(archive, out_dir=args.out, max_class=args.max_class,
                              include_derived=not args.no_derived)
    _emit(args, result.__dict__, lambda: print(
        "Projected %d conversation(s) (%d withheld as stubs), %d proposition note(s), "
        "%d arc note(s)\n  -> %s"
        % (result.conversations, result.stubs, result.propositions, result.arcs, result.out_dir)))
    return 0


def cmd_classify(args) -> int:
    archive = _open(args)
    subject_id = (_resolve_conversation(archive, args.id)
                  if args.type == "conversation" else args.id)
    set_class(archive, args.type, subject_id, args.security_class, rationale=args.reason,
              actor=args.operator or "", cascade=args.cascade)
    print("%s %s is now %s" % (args.type, subject_id, args.security_class))
    if not args.reason:
        print("(no rationale recorded - pass --reason next time; the history keeps it)")
    return 0


def cmd_packet(args) -> int:
    archive = _open(args)
    result = evidence.build_packet(
        archive, recipient=args.recipient, conversation_ids=args.conversation, arc=args.arc,
        query=args.query, max_class=args.max_class, purpose=args.purpose or "",
        acknowledgement=args.acknowledge or "", out_dir=args.out,
        include_propositions=args.with_propositions, include_zip=not args.no_zip,
    )

    def show():
        print("Packet %s for %s" % (result.packet_id, result.recipient))
        print("  directory : %s" % result.path)
        if result.zip_path:
            print("  zip       : %s" % result.zip_path)
        print("  limit     : %s" % result.max_security_class)
        print("  contents  : %d conversation(s), %d message(s), %d proposition(s), %d file(s)"
              % (result.conversations, result.messages, result.propositions, result.files))
        if result.withheld:
            print("  withheld  : %d item(s) above the class limit (listed in MANIFEST.json)"
                  % len(result.withheld))
        print("\nVerify with: sha256sum -c CHECKSUMS.sha256  (or `%s packet-verify %s`)"
              % (PROG, result.path))

    _emit(args, result.to_dict(), show)
    return 0


def cmd_packet_verify(args) -> int:
    report = evidence.verify_packet(args.path)
    _emit(args, report, lambda: print(
        "%s  %d file(s) checked; %d mismatched, %d missing, %d unexpected; read-only: %s"
        % ("OK" if report.get("ok") else "FAILED", report.get("files_checked", 0),
           len(report.get("mismatched", [])), len(report.get("missing", [])),
           len(report.get("unexpected", [])), report.get("read_only"))))
    return 0 if report.get("ok") else 1


def cmd_gate(args) -> int:
    archive = _open(args)
    state = gate.status(archive)

    def show():
        print("Interpretation gate: %s" % ("OPEN" if state["open"] else "CLOSED"))
        print("  %s" % state["reason"])
        if state.get("run_id"):
            print("  last verification : %s at %s" % (state["run_id"], state["verified_at"]))
            print("  verified state    : %s" % state["verified_fingerprint"][:32])
        print("  current state     : %s" % state["current_fingerprint"][:32])
        if not state["open"]:
            print("\nRun `%s verify` to re-open it." % PROG)

    _emit(args, state, show)
    return 0 if state["open"] else 1


def cmd_arch_extract(args) -> int:
    archive = _open(args)
    conversation_id = _resolve_conversation(archive, args.conversation)
    summary = arch_extract.extract_conversation(
        archive, conversation_id, arc=args.arc or "", dry_run=not args.commit,
        min_length=args.min_length,
    )

    def show():
        print("%d candidate(s) in %s%s"
              % (summary["candidates"], conversation_id,
                 "" if summary["dry_run"] else " · %d recorded" % summary["recorded"]))
        for item in summary["items"][: args.limit]:
            print("  %-10s %-11s %s" % (item["event_date"] or "undated",
                                        item["probability_role"], item["quote"][:88]))
            facets = ", ".join(filter(None, [
                "entity=" + item["entity"] if item["entity"] else "",
                "location=" + item["location"] if item["location"] else "",
                "number=" + item["number"] if item["number"] else "",
            ]))
            if facets:
                print("             %s   [%s]" % (facets, ",".join(item["rules"])))
        if summary["dry_run"]:
            print("\nDry run. Re-run with --commit to record these as propositions.")

    _emit(args, summary, show)
    return 0


def cmd_arch_add(args) -> int:
    archive = _open(args)
    proposition_id = arch_props.add_proposition(
        archive, args.message_version, args.quote, entity=args.entity, location=args.location,
        arc=args.arc, event_date=args.date, number_raw=args.number,
        contemporaneous_meaning=args.contemporaneous, later_interpretation=args.later,
        prompted_by=args.prompted_by, verification_status=args.status,
        probability_role=args.role, probability=args.probability, method="manual",
    )
    print("Recorded proposition %s" % proposition_id)
    return 0


def cmd_arch_correct(args) -> int:
    archive = _open(args)
    changes = {}
    for key in ("entity", "location", "arc", "contemporaneous_meaning", "later_interpretation",
                "verification_status", "probability_role", "event_date"):
        value = getattr(args, key, None)
        if value is not None:
            changes[key] = value
    arch_props.correct_proposition(archive, args.proposition, reason=args.reason, **changes)
    history = arch_props.proposition_history(archive, args.proposition)
    print("Proposition %s now at version %d; %d earlier version(s) retained."
          % (args.proposition, len(history), len(history) - 1))
    return 0


def cmd_arch_show(args) -> int:
    archive = _open(args)
    data = arch_props.get_proposition(archive, args.proposition)
    history = arch_props.proposition_history(archive, args.proposition)
    verification = arch_props.verify_quote(archive, args.proposition)

    def show():
        print("Proposition %s (version %d of %d)"
              % (data["proposition_id"], data["version_no"], len(history)))
        print("  source conversation : %s (%s)" % (data["conversation_id"],
                                                   data["source_conversation_id"]))
        print("  source message      : %s" % data["message_version_id"])
        print("  date                : %s" % (data["event_date"] or "—"))
        print("  quotation           : %s" % data["quote_text"])
        print("  entity / location   : %s / %s" % (data["entity"] or "—", data["location"] or "—"))
        print("  number              : %s" % (data["number_raw"] or "—"))
        print("  arc                 : %s" % (data["arc"] or "—"))
        print("  contemporaneous     : %s" % (data["contemporaneous_meaning"] or "—"))
        print("  later interpretation: %s" % (data["later_interpretation"] or "—"))
        print("  prompted by         : %s (%s)" % (data["prompted_by"] or "—",
                                                   data["prompted_by_kind"] or "—"))
        print("  generated next      : %s"
              % (", ".join("%s→%s" % (e["relation"], e["to_id"][:12])
                           for e in data["generated_next"]) or "—"))
        print("  verification        : %s" % data["verification_status"])
        print("  probability role    : %s" % (data["probability_role"] or "—"))
        print("  recorded by         : %s (%s)" % (data["created_by"], data["method"] or "—"))
        print("  quote re-verified   : %s" % ("yes" if verification["ok"] else "NO"))
        if len(history) > 1:
            print("\n  correction history:")
            for version in history:
                print("    v%-2d %s %s" % (version["version_no"], version["created_at"],
                                           version["correction_reason"] or "(original)"))

    _emit(args, {"proposition": data, "history": history, "verification": verification}, show)
    return 0


def cmd_arch_list(args) -> int:
    archive = _open(args)
    rows = arch_props.list_propositions(
        archive, arc=args.arc, conversation_id=args.conversation, entity=args.entity,
        verification_status=args.status, limit=args.limit,
    )

    def show():
        for row in rows:
            print("%-14s %-10s %-11s %s" % (row["proposition_id"][:14],
                                            row["event_date"] or "undated",
                                            row["verification_status"],
                                            (row["quote_text"] or "")[:80]))
        print("\n%d proposition(s)" % len(rows))

    _emit(args, rows, show)
    return 0


def cmd_arch_observe(args) -> int:
    archive = _open(args)
    search_event_id = arch_graph.add_search_event(
        archive, kind=args.kind, query=args.query or "", tool=args.tool or "",
        result_summary=args.result or "", notes=args.notes or "",
    )
    print("Recorded observation %s" % search_event_id)
    return 0


def cmd_arch_link(args) -> int:
    archive = _open(args)
    edge_id = arch_graph.add_edge(
        archive, args.from_kind, args.from_id, args.to_kind, args.to_id,
        relation=args.relation, rationale=args.rationale or "",
    )
    print("Edge %s: %s %s -> %s %s (%s)"
          % (edge_id, args.from_kind, args.from_id[:12], args.to_kind, args.to_id[:12],
             args.relation))
    return 0


def cmd_arch_graph(args) -> int:
    archive = _open(args)
    if args.format == "dot":
        output = arch_graph.to_dot(archive, arc=args.arc)
    else:
        output = json.dumps(arch_graph.to_json(archive, arc=args.arc), indent=2,
                            ensure_ascii=False, default=str)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print("Wrote %s" % args.out)
    else:
        print(output)
    return 0


def cmd_arch_chain(args) -> int:
    archive = _open(args)
    chain = arch_graph.discovery_chain(archive, args.kind, args.id)
    forward = arch_graph.descendants(archive, args.kind, args.id)

    def show():
        print("What led to %s %s:" % (args.kind, args.id[:16]))
        if not chain:
            print("  (a root observation - nothing recorded before it)")
        for step in chain:
            print("  <- %-13s %s  (%s)" % (step["kind"], step["id"][:16], step["relation"]))
        print("\nWhat it generated:")
        if not forward:
            print("  (nothing yet)")
        for step in forward:
            print("  -> %-13s %s  depth %d (%s)"
                  % (step["kind"], step["id"][:16], step["depth"], step["relation"]))

    _emit(args, {"ancestors": chain, "descendants": forward}, show)
    return 0


def cmd_log(args) -> int:
    archive = _open(args)
    rows = archive.all("SELECT * FROM operation_log ORDER BY at DESC LIMIT ?", (args.limit,))
    data = [dict(r) for r in rows]
    _emit(args, data, lambda: [
        print("%s  %-22s %s%s" % (r["at"], r["operation"],
                                  (r["detail_json"] or "")[:100],
                                  "  [NETWORK UNSEALED]" if r["network_unsealed"] else ""))
        for r in data
    ])
    return 0


def cmd_demo(args) -> int:
    """Run the whole prototype end to end against the sample conversations."""
    from .demo import run_demo

    return run_demo(Path(args.out), samples_dir=Path(args.samples) if args.samples else None,
                    keep=args.keep)


# ------------------------------------------------------------------- parsing
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="ARCS Archive Bridge - a local, provenance-complete conversation archive.",
        epilog="This program never uploads anything: outbound network access is blocked "
               "at the socket layer for the life of the process.",
    )
    parser.add_argument("--archive", help="archive root (default: $ARCS_ARCHIVE or ./arcs-archive)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("init", help="create a new archive")
    p.add_argument("path", nargs="?", help="archive root directory")
    p.add_argument("--name", help="human-readable archive name")
    p.add_argument("--operator", help="who is running this archive")
    p.set_defaults(func=cmd_init)

    p = subparsers.add_parser("adapters", help="list import adapters")
    p.set_defaults(func=cmd_adapters)

    p = subparsers.add_parser("ingest", help="ingest an export or capture bundle")
    p.add_argument("path")
    p.add_argument("--adapter", help="force an adapter instead of detecting one")
    p.add_argument("--security-class", default="Normal", choices=list(CLASSES))
    p.add_argument("--reason", help="reason recorded on any superseded version")
    p.add_argument("--notes", help="free text recorded on the batch")
    p.add_argument("--operator")
    p.add_argument("--credential-policy", default="strict", choices=["strict", "keys-only"])
    p.set_defaults(func=cmd_ingest)

    p = subparsers.add_parser("status", help="archive counts, gate and network state")
    p.set_defaults(func=cmd_status)

    p = subparsers.add_parser("list", help="list conversations")
    p.set_defaults(func=cmd_list)

    p = subparsers.add_parser("show", help="print a conversation")
    p.add_argument("conversation")
    p.add_argument("--version", type=int, help="archive version number")
    p.add_argument("--full", action="store_true", help="do not truncate message text")
    p.set_defaults(func=cmd_show)

    p = subparsers.add_parser("history", help="version and correction history")
    p.add_argument("conversation")
    p.set_defaults(func=cmd_history)

    p = subparsers.add_parser("verify", help="run the integrity, recovery and completeness suite")
    p.add_argument("--suite", default="full", choices=list(integrity.SUITES))
    p.set_defaults(func=cmd_verify)

    p = subparsers.add_parser("reconstruct", help="check that originals rebuild from the archive")
    p.add_argument("--conversation", action="append")
    p.add_argument("--all-versions", action="store_true")
    p.set_defaults(func=cmd_reconstruct)

    p = subparsers.add_parser("recover", help="write original source bytes back to disk")
    p.add_argument("--out", required=True)
    p.add_argument("--conversation", action="append")
    p.add_argument("--all-versions", action="store_true")
    p.set_defaults(func=cmd_recover)

    p = subparsers.add_parser("search", help="local full-text search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-class", choices=list(CLASSES))
    p.add_argument("--conversation")
    p.add_argument("--role")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_search)

    p = subparsers.add_parser("semantic", help="optional local semantic index (no external APIs)")
    p.add_argument("action", choices=["index", "search"])
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--subject", default="message", choices=["message", "proposition"])
    p.add_argument("--backend", default="local-hashing",
                   choices=["local-hashing", "sentence-transformers"])
    p.add_argument("--model-path", help="local directory for the sentence-transformers backend")
    p.add_argument("--dim", type=int, default=semantic.DEFAULT_DIM)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--max-class", choices=list(CLASSES))
    p.add_argument("--rebuild", action="store_true")
    p.set_defaults(func=cmd_semantic)

    p = subparsers.add_parser("project", help="write the Obsidian Markdown projection")
    p.add_argument("--out")
    p.add_argument("--max-class", default="Private", choices=list(CLASSES))
    p.add_argument("--no-derived", action="store_true", help="omit proposition and arc notes")
    p.set_defaults(func=cmd_project)

    p = subparsers.add_parser("classify", help="set a security class")
    p.add_argument("type", choices=["conversation", "message"])
    p.add_argument("id")
    p.add_argument("security_class", choices=list(CLASSES))
    p.add_argument("--reason")
    p.add_argument("--operator")
    p.add_argument("--cascade", action="store_true", help="apply to the conversation's messages")
    p.set_defaults(func=cmd_classify)

    p = subparsers.add_parser("packet", help="build a read-only evidence packet")
    p.add_argument("--recipient", required=True, help="e.g. Grace or Grok")
    p.add_argument("--conversation", action="append")
    p.add_argument("--arc")
    p.add_argument("--query")
    p.add_argument("--max-class", default="Normal", choices=list(CLASSES))
    p.add_argument("--purpose")
    p.add_argument("--acknowledge", help="required to release Restricted or Sacred material")
    p.add_argument("--with-propositions", action="store_true")
    p.add_argument("--out")
    p.add_argument("--no-zip", action="store_true")
    p.set_defaults(func=cmd_packet)

    p = subparsers.add_parser("packet-verify", help="re-check a packet's checksums")
    p.add_argument("path")
    p.set_defaults(func=cmd_packet_verify)

    p = subparsers.add_parser("gate", help="is interpretation authorised?")
    p.set_defaults(func=cmd_gate)

    p = subparsers.add_parser("log", help="recent archive operations")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_log)

    p = subparsers.add_parser("demo", help="run the full prototype against the sample archive")
    p.add_argument("--out", default="./arcs-demo")
    p.add_argument("--samples", help="directory of sample inputs")
    p.add_argument("--keep", action="store_true", help="keep an existing demo directory")
    p.set_defaults(func=cmd_demo)

    arch = subparsers.add_parser("arch", help="ARCS Archaeology layer (gated)")
    arch_sub = arch.add_subparsers(dest="arch_command", required=True)

    p = arch_sub.add_parser("extract", help="rule-based candidate propositions")
    p.add_argument("conversation")
    p.add_argument("--arc")
    p.add_argument("--commit", action="store_true", help="record the candidates")
    p.add_argument("--min-length", type=int, default=25)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_arch_extract)

    p = arch_sub.add_parser("add", help="record one proposition by hand")
    p.add_argument("message_version")
    p.add_argument("--quote", required=True)
    p.add_argument("--entity")
    p.add_argument("--location")
    p.add_argument("--number")
    p.add_argument("--arc")
    p.add_argument("--date")
    p.add_argument("--contemporaneous", help="what it meant at the time")
    p.add_argument("--later", help="what we think now (kept separate)")
    p.add_argument("--prompted-by")
    p.add_argument("--status", default="quote_verified",
                   choices=list(arch_props.VERIFICATION_STATUSES))
    p.add_argument("--role", default="assertion", choices=list(arch_props.PROBABILITY_ROLES))
    p.add_argument("--probability", type=float)
    p.set_defaults(func=cmd_arch_add)

    p = arch_sub.add_parser("correct", help="append a corrected version")
    p.add_argument("proposition")
    p.add_argument("--reason", required=True)
    p.add_argument("--entity")
    p.add_argument("--location")
    p.add_argument("--arc")
    p.add_argument("--event-date", dest="event_date")
    p.add_argument("--contemporaneous", dest="contemporaneous_meaning")
    p.add_argument("--later", dest="later_interpretation")
    p.add_argument("--status", dest="verification_status",
                   choices=list(arch_props.VERIFICATION_STATUSES))
    p.add_argument("--role", dest="probability_role", choices=list(arch_props.PROBABILITY_ROLES))
    p.set_defaults(func=cmd_arch_correct)

    p = arch_sub.add_parser("show", help="one proposition with its correction history")
    p.add_argument("proposition")
    p.set_defaults(func=cmd_arch_show)

    p = arch_sub.add_parser("list", help="list propositions")
    p.add_argument("--arc")
    p.add_argument("--conversation")
    p.add_argument("--entity")
    p.add_argument("--status", choices=list(arch_props.VERIFICATION_STATUSES))
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_arch_list)

    p = arch_sub.add_parser("observe", help="record an observation or search")
    p.add_argument("--kind", required=True,
                   choices=["archive_search", "web_search", "archive_read", "field_visit",
                            "conversation"])
    p.add_argument("--query")
    p.add_argument("--tool")
    p.add_argument("--result")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_arch_observe)

    p = arch_sub.add_parser("link", help="add a discovery-graph edge")
    p.add_argument("--from-kind", required=True, choices=list(arch_graph.NODE_KINDS))
    p.add_argument("--from-id", required=True)
    p.add_argument("--to-kind", required=True, choices=list(arch_graph.NODE_KINDS))
    p.add_argument("--to-id", required=True)
    p.add_argument("--relation", default="generated", choices=list(arch_graph.RELATIONS))
    p.add_argument("--rationale")
    p.set_defaults(func=cmd_arch_link)

    p = arch_sub.add_parser("graph", help="export the discovery graph")
    p.add_argument("--arc")
    p.add_argument("--format", default="dot", choices=["dot", "json"])
    p.add_argument("--out")
    p.set_defaults(func=cmd_arch_graph)

    p = arch_sub.add_parser("chain", help="what led to a discovery, and what it generated")
    p.add_argument("id")
    p.add_argument("--kind", default="proposition", choices=list(arch_graph.NODE_KINDS))
    p.set_defaults(func=cmd_arch_chain)

    return parser


def main(argv=None) -> int:
    netguard.seal("archive process: contents are never uploaded")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ArcsError as exc:
        print("%s: %s" % (PROG, exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
