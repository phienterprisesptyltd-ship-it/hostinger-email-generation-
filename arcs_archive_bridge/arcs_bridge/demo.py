"""The prototype demonstration.

Runs the whole bridge end to end against five sample conversations and proves
the property everything else rests on: **the originals can always be
reconstructed from the archive.**

The proof is deliberately independent of the archive's own bookkeeping.  The
demo re-reads the untouched sample file from disk, extracts the byte range each
conversation occupied in it, and compares that against what the archive hands
back.  A bug in the hashing, the blob store or the recovery path shows up as a
byte difference, not as a passing self-report.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import evidence, gate, integrity, obsidian, reconstruct, search, semantic
from .archaeology import extract as arch_extract
from .archaeology import graph as arch_graph
from .archaeology import propositions as arch_props
from .config import ArchiveConfig
from .db import Archive
from .errors import ArcsError, CredentialMaterialFound, InterpretationGateError
from .ingest import ingest_path
from .hashing import sha256_bytes
from .jsonspans import array_spans
from .security import set_class
from .util import utcnow

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _step(text: str) -> None:
    print("\n-- %s" % text)


def run_demo(out_dir: Path, samples_dir=None, keep: bool = False) -> int:
    samples = Path(samples_dir or SAMPLES)
    export_v1 = samples / "chatgpt_export" / "conversations.json"
    export_files = samples / "chatgpt_export_with_files"
    export_v2 = samples / "chatgpt_export_v2" / "conversations.json"
    capture = samples / "ui_capture" / "arcs-capture-bundle.json"
    leaky = samples / "bad_capture" / "leaky-bundle.json"
    for path in (export_v1, export_v2, capture, leaky, export_files):
        if not path.exists():
            raise ArcsError("sample %s is missing; run samples/generate_samples.py" % path)

    out_dir = Path(out_dir)
    if out_dir.exists() and not keep:
        shutil.rmtree(out_dir)
    config = ArchiveConfig(root=out_dir / "archive", archive_name="ARCS demo archive",
                           operator="demo", created_at=utcnow())
    config.save()
    archive = Archive.open(config)
    failures = []

    # ---------------------------------------------------------------- ingest
    _rule("1. Ingest - raw source first, interpretation never")
    result = ingest_path(archive, export_v1, operator="demo")
    print("adapter %s · method %s · input sha256 %s"
          % (result.adapter, result.source_method, result.input_sha256[:16]))
    for conv in result.conversations:
        print("  %-7s v%d  %-42s %d message(s)  source %s"
              % (conv.status, conv.version_no, (conv.title or "")[:42],
                 conv.messages_new, conv.source_sha256[:12]))

    _step("re-ingesting the identical file: deduplicate, keep every sighting")
    again = ingest_path(archive, export_v1, operator="demo")
    dup_records = sum(1 for c in again.conversations if c.duplicate_of_record_id)
    print("   statuses: %s" % ", ".join(sorted({c.status for c in again.conversations})))
    print("   %d source record(s) linked to an earlier identical capture; "
          "%d blob(s) stored in total"
          % (dup_records, archive.scalar("SELECT COUNT(*) FROM source_blobs")))
    print("   conversation versions: %d (unchanged - a repeat capture is a sighting, "
          "not a version)" % archive.scalar("SELECT COUNT(*) FROM conversation_versions"))
    if again.new_count or again.updated_count:
        failures.append("re-ingesting an identical file created new versions")

    _step("the same export as a directory: the files beside conversations.json")
    with_files = ingest_path(archive, export_files, operator="demo")
    print("   %d file(s) archived (%d bytes); %d attachment(s) are references only"
          % (with_files.assets_stored, with_files.assets_bytes,
             sum(c.files_referenced_only for c in with_files.conversations)))
    for warning in with_files.warnings:
        print("   ! %s" % warning)
    for row in archive.all(
        "SELECT name, kind, content_present, blob_sha256, export_relpath FROM attachments "
        "WHERE content_present=1 ORDER BY name"
    ):
        print("     held  %-32s %s  (%s)" % (row["name"][:32], row["blob_sha256"][:12],
                                             row["export_relpath"]))
    unreferenced = archive.scalar(
        "SELECT COUNT(*) FROM source_records sr WHERE sr.record_kind='attachment' "
        "AND sr.record_id NOT IN (SELECT source_record_id FROM attachments "
        "WHERE source_record_id IS NOT NULL) AND sr.duplicate_of_record_id IS NULL"
    )
    print("     %d file(s) in the export matched no message and were archived anyway"
          % unreferenced)

    _step("a later capture with corrections: append, never overwrite")
    later = ingest_path(archive, export_v2, operator="demo",
                        notes="second export, three weeks later")
    for conv in later.conversations:
        if conv.status != "unchanged":
            print("   %-8s v%d  %s (+%d new, %d updated message versions)"
                  % (conv.status, conv.version_no, (conv.title or "")[:44],
                     conv.messages_new, conv.messages_updated))

    _step("UI-assisted capture bundle (browser session used, no credentials stored)")
    ui = ingest_path(archive, capture, operator="demo")
    for conv in ui.conversations:
        print("   %-7s v%d  %s" % (conv.status, conv.version_no, (conv.title or "")[:50]))

    _step("a capture bundle carrying a session cookie")
    try:
        ingest_path(archive, leaky, operator="demo")
        failures.append("a bundle containing a session cookie was ingested")
        print("   !! INGESTED - this is a bug")
    except CredentialMaterialFound as exc:
        print("   refused, nothing stored: %s" % str(exc).split(".")[0])

    # ------------------------------------------------------------ verify
    _rule("2. Verify - provenance, hashing, recovery, completeness")
    report = integrity.run(archive, "full")
    for check in report.checks:
        print("  %s %-36s %s" % ("PASS" if check.passed else "FAIL", check.name, check.detail))
        if not check.passed:
            failures.append("integrity check failed: " + check.name)
    print("\n  %s - %d checks, %d failed" % ("PASSED" if report.passed else "FAILED",
                                             len(report.checks), len(report.failed)))

    # -------------------------------------------------- independent proof
    _rule("3. Proof - the originals come back byte-for-byte")
    print("Recovering every archived version to disk, then comparing against the "
          "untouched sample file that has never been through the archive.\n")
    recovered_dir = out_dir / "recovered"
    manifest = reconstruct.export_originals(archive, recovered_dir, include_history=True)

    original_text = export_v1.read_text(encoding="utf-8")
    original_bytes = export_v1.read_bytes()
    spans = {}
    for span in array_spans(original_text):
        conv_id = span.value.get("conversation_id") or span.value.get("id")
        spans[conv_id] = original_bytes[span.byte_start:span.byte_end]

    compared = matched = 0
    for item in manifest["files"]:
        source_id = item.get("source_conversation_id")
        if source_id not in spans or item.get("version_no") != 1:
            continue
        compared += 1
        recovered = (recovered_dir / item["file"]).read_bytes()
        same = recovered == spans[source_id]
        matched += same
        print("  %s %-24s %6d bytes  %s"
              % ("identical" if same else "DIFFERENT", source_id, len(recovered),
                 item["sha256"][:16]))
        if not same:
            failures.append("recovered bytes differ from the original for " + source_id)
    print("\n  %d/%d first-capture conversations recovered byte-for-byte from the archive."
          % (matched, compared))

    attachments = [f for f in manifest["files"] if f.get("kind") == "attachment"]
    for item in attachments:
        recovered_bytes = (recovered_dir / item["file"]).read_bytes()
        relpath = item.get("export_relpath")
        original = (export_files / relpath) if relpath else None
        if original is not None and original.is_file():
            # Came from the export directory: compare against the file on disk.
            same = recovered_bytes == original.read_bytes()
            how = "vs the export"
        else:
            # Carried inline by a capture: the recorded hash is the reference.
            same = sha256_bytes(recovered_bytes) == item["sha256"]
            how = "vs its hash"
        print("  %s %-38s %6d bytes  %s  %s"
              % ("identical" if same else "DIFFERENT", item["name"][:38],
                 item["byte_length"], item["sha256"][:16], how))
        if not same:
            failures.append("recovered file differs from the original: " + item["name"])
    if attachments:
        print("  %d attachment file(s) recovered from the archive." % len(attachments))

    container = [f for f in manifest["files"] if f.get("kind") == "container"]
    whole_file_ok = any(
        (recovered_dir / f["file"]).read_bytes() == original_bytes for f in container
    )
    print("  whole original export file recovered byte-for-byte: %s"
          % ("yes" if whole_file_ok else "NO"))
    if not whole_file_ok:
        failures.append("the original export file did not come back byte-for-byte")

    _step("and the same material rebuilds from the normalised tables alone")
    reports = reconstruct.verify_all(archive, include_history=True)
    for item in reports:
        print("  %s %-40s v%d  tables->original: %s"
              % ("ok  " if item.ok else "FAIL", (item.title or "")[:40], item.version_no,
                 "identical" if item.structural_match else "DIFFERENT"))
        if not item.ok:
            failures.append("reconstruction failed for %s v%d" % (item.title, item.version_no))

    # ------------------------------------------------------- classification
    _rule("4. Security classes")
    sacred = archive.one(
        "SELECT conversation_id FROM conversations WHERE source_conversation_id=?",
        ("arcs-0005-restricted",),
    )
    set_class(archive, "conversation", sacred["conversation_id"], "Sacred",
              rationale="urupā location given in confidence by a kaumātua; not for publication",
              actor="demo", cascade=True)
    print("  one conversation reclassified Sacred (with a recorded rationale and cascade)")
    for row in archive.all(
        "SELECT security_class, COUNT(*) AS n FROM conversations GROUP BY security_class"
    ):
        print("    %-10s %d conversation(s)" % (row["security_class"], row["n"]))

    # -------------------------------------------------------------- search
    _rule("5. Search - local full text, then local semantics")
    hits = search.search_messages(archive, "chains karaka", limit=3)
    for hit in hits.hits:
        print("  %-40s %s · msg %d\n      %s"
              % (hit.conversation_title[:40], hit.role, hit.seq, hit.snippet[:110]))

    _step("semantic index (local hashing model, no external API)")
    index = semantic.build_index(archive, rebuild=True)
    print("   indexed %d message version(s) with %s (%d dims)"
          % (index.indexed, index.model_name, index.dim))
    for hit in semantic.semantic_search(archive, "how far did the shoreline move", k=3):
        print("   %.3f  %-38s %s" % (hit["score"], hit["title"][:38],
                                     hit["text"][:70].replace("\n", " ")))

    # ------------------------------------------------------------ projection
    _rule("6. Obsidian projection")
    projection = obsidian.project(archive, max_class="Private")
    print("  %d note(s) written to %s" % (len(projection.files), projection.out_dir))
    print("  %d conversation(s) projected; %d withheld as stubs (above Private)"
          % (projection.conversations, projection.stubs))

    # ------------------------------------------------------------- the gate
    _rule("7. The interpretation gate")
    state = gate.status(archive)
    print("  gate is %s: %s" % ("OPEN" if state["open"] else "CLOSED", state["reason"]))
    print("  bound to archive fingerprint %s" % state["current_fingerprint"][:32])

    # ------------------------------------------------------- archaeology
    _rule("8. ARCS Archaeology - propositions and the discovery graph")
    ledger = archive.one(
        "SELECT conversation_id FROM conversations WHERE source_conversation_id=?",
        ("arcs-0001-ledger",),
    )["conversation_id"]
    coast = archive.one(
        "SELECT conversation_id FROM conversations WHERE source_conversation_id=?",
        ("arcs-0003-coast",),
    )["conversation_id"]

    observation = arch_graph.add_search_event(
        archive, kind="archive_read", tool="Kaiora survey ledger, page 14",
        query="1887 survey ledger, chain/bearing/remarks columns",
        result_summary="photograph of a ledger page with three legible rows",
        notes="the observation that started this line of enquiry",
    )
    print("  recorded the originating observation %s" % observation)

    summary = arch_extract.extract_conversation(archive, ledger, arc="Kaiora boundary",
                                                dry_run=False)
    print("  extracted %d proposition(s) from the ledger conversation (rule-based, "
          "meanings left blank for the researcher)" % summary["recorded"])
    coast_summary = arch_extract.extract_conversation(archive, coast, arc="Kaiora coastline",
                                                      dry_run=False)
    print("  extracted %d proposition(s) from the coastline conversation"
          % coast_summary["recorded"])

    first = summary["proposition_ids"][0] if summary["proposition_ids"] else None
    if first:
        arch_graph.add_edge(archive, "search_event", observation, "proposition", first,
                            relation="generated",
                            rationale="the ledger photograph is what prompted the first reading")

    _step("a proposition recorded by hand, with the two meanings kept apart")
    message = archive.one(
        "SELECT mv.version_id, mv.content_text FROM message_versions mv "
        "JOIN conversation_version_messages cvm ON cvm.message_version_id = mv.version_id "
        "JOIN conversation_versions cv ON cv.version_id = cvm.conversation_version_id "
        "WHERE cv.conversation_id=? AND cv.is_current=1 AND mv.role='user' ORDER BY cvm.seq",
        (ledger,),
    )
    quote = "Survey of the Kaiora Point reserve, commenced 14 March 1887"
    manual = arch_props.add_proposition(
        archive, message["version_id"], quote,
        entity="Kaiora Point reserve", location="Kaiora Point", event_date="1887-03-14",
        arc="Kaiora boundary",
        contemporaneous_meaning=(
            "In 1887 'reserve' meant land set aside by the Crown under the survey "
            "regulations of the day; the heading dates the commencement of fieldwork, "
            "not the completion of the survey."
        ),
        later_interpretation=(
            "Read now as fixing the earliest date at which the reserve boundary was "
            "physically walked - kept separate from the contemporaneous meaning above."
        ),
        prompted_by=observation, prompted_by_kind="search_event",
        verification_status="quote_verified", probability_role="assertion", probability=0.95,
        method="manual",
    )
    print("   proposition %s" % manual)

    _step("a correction: the earlier version is retained, not overwritten")
    arch_props.correct_proposition(
        archive, manual,
        reason="the 1898 succession order fixes the surveyed party; entity narrowed",
        entity="Kaiora Point reserve (holding 214)",
        verification_status="corroborated",
    )
    history = arch_props.proposition_history(archive, manual)
    for version in history:
        print("   v%d %s  %s" % (version["version_no"], version["created_at"],
                                 version["correction_reason"] or "(original)"))

    _step("the discovery graph: what made me look, and what it produced")
    chain = arch_graph.discovery_chain(archive, "proposition", manual)
    for step in chain:
        print("   <- %-13s %s (%s)" % (step["kind"], step["id"][:20], step["relation"]))
    forward = arch_graph.descendants(archive, "search_event", observation)
    print("   the originating observation generated %d downstream node(s)" % len(forward))
    dot_path = out_dir / "discovery-graph.dot"
    dot_path.write_text(arch_graph.to_dot(archive), encoding="utf-8")
    print("   graph written to %s (render with: dot -Tsvg -O %s)" % (dot_path, dot_path.name))

    verification = arch_props.verify_quote(archive, manual)
    print("   quotation re-verified against the stored source bytes: %s"
          % ("yes" if verification["ok"] and verification["in_source_bytes"] else
             "hash ok=%s in-source=%s" % (verification["quote_hash_ok"],
                                          verification["in_source_bytes"])))
    if not verification["ok"]:
        failures.append("a recorded quotation did not verify against its source")

    # ------------------------------------------------------ evidence packets
    _rule("9. Evidence packets for external analysis")
    for recipient in ("Grace", "Grok"):
        packet = evidence.build_packet(
            archive, recipient=recipient, max_class="Normal",
            purpose="independent reading of the Kaiora material",
            include_propositions=(recipient == "Grok"),
        )
        check = evidence.verify_packet(packet.path)
        print("  %-6s %d conversation(s), %d message(s), %d file(s), %d proposition(s); "
              "%d withheld; checksums %s; read-only %s"
              % (recipient, packet.conversations, packet.messages, packet.attachments,
                 packet.propositions, len(packet.withheld),
                 "ok" if check["ok"] else "FAILED", check["read_only"]))
        if not check["ok"]:
            failures.append("packet for %s failed its own checksum verification" % recipient)
        if any(w.get("security_class") == "Sacred" for w in packet.withheld):
            print("         Sacred material withheld and recorded in MANIFEST.json")

    _step("attempting a packet that would include Sacred material without acknowledgement")
    try:
        evidence.build_packet(archive, recipient="Grok", max_class="Sacred")
        failures.append("a Sacred packet was built without an acknowledgement")
        print("   !! BUILT - this is a bug")
    except Exception as exc:
        print("   refused: %s" % str(exc).split(";")[0])

    # ------------------------------------------------------------- gate again
    _rule("10. The gate closes when the archive changes")
    ingest_path(archive, capture, operator="demo")  # a fresh sighting changes the fingerprint
    state = gate.status(archive)
    print("  after another capture, gate is %s: %s"
          % ("OPEN" if state["open"] else "CLOSED", state["reason"]))
    try:
        arch_extract.extract_conversation(archive, coast, dry_run=True)
        if not state["open"]:
            failures.append("extraction ran while the gate was closed")
    except InterpretationGateError as exc:
        print("  extraction refused: %s" % str(exc).split(".")[0])
    integrity.run(archive, "full")
    print("  after `arcs verify`, gate is %s"
          % ("OPEN" if gate.status(archive)["open"] else "CLOSED"))

    # ------------------------------------------------------------------ done
    _rule("Result")
    counts = {
        "conversations": archive.scalar("SELECT COUNT(*) FROM conversations"),
        "conversation versions": archive.scalar("SELECT COUNT(*) FROM conversation_versions"),
        "messages": archive.scalar("SELECT COUNT(*) FROM messages"),
        "message versions": archive.scalar("SELECT COUNT(*) FROM message_versions"),
        "source records": archive.scalar("SELECT COUNT(*) FROM source_records"),
        "source blobs": archive.scalar("SELECT COUNT(*) FROM source_blobs"),
        "attachment files held": archive.scalar(
            "SELECT COUNT(*) FROM attachments WHERE content_present=1"),
        "propositions": archive.scalar("SELECT COUNT(*) FROM propositions", conn=archive.derived),
        "discovery edges": archive.scalar("SELECT COUNT(*) FROM discovery_edges",
                                          conn=archive.derived),
    }
    for key, value in counts.items():
        print("  %-22s %s" % (key, value))
    print("\n  archive: %s" % config.root)
    (out_dir / "demo-summary.json").write_text(
        json.dumps({"counts": counts, "failures": failures, "at": utcnow()},
                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if failures:
        print("\n%d PROBLEM(S):" % len(failures))
        for failure in failures:
            print("  - %s" % failure)
        return 1
    print("\nEverything checked out: every original is recoverable byte-for-byte and "
          "rebuildable from the normalised tables.")
    return 0
