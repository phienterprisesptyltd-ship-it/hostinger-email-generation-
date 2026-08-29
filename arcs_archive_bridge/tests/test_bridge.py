"""Tests for the ARCS Archive Bridge.

The suite is ordered the way the design is: the raw layer first (ingestion,
provenance, hashing, recovery, completeness), the guarantees that protect it
(credentials, network, security classes) next, and the archaeology layer last -
behind the gate that the earlier tests are what opens.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcs_bridge import evidence, gate, integrity, netguard, obsidian, reconstruct  # noqa: E402
from arcs_bridge import search, semantic  # noqa: E402
from arcs_bridge.archaeology import extract as arch_extract  # noqa: E402
from arcs_bridge.archaeology import graph as arch_graph  # noqa: E402
from arcs_bridge.archaeology import propositions as arch_props  # noqa: E402
from arcs_bridge.config import ArchiveConfig  # noqa: E402
from arcs_bridge.db import Archive  # noqa: E402
from arcs_bridge.errors import (  # noqa: E402
    ArcsError,
    CredentialMaterialFound,
    InterpretationGateError,
    NetworkBlocked,
    SecurityClassViolation,
)
from arcs_bridge.hashing import sha256_bytes  # noqa: E402
from arcs_bridge.ingest import ingest_path  # noqa: E402
from arcs_bridge.jsonspans import array_spans  # noqa: E402
from arcs_bridge.security import set_class  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
EXPORT_V1 = SAMPLES / "chatgpt_export" / "conversations.json"
EXPORT_V2 = SAMPLES / "chatgpt_export_v2" / "conversations.json"
CAPTURE = SAMPLES / "ui_capture" / "arcs-capture-bundle.json"
LEAKY = SAMPLES / "bad_capture" / "leaky-bundle.json"
USERSCRIPT = ROOT / "browser_capture" / "arcs-ui-capture.user.js"


class ArchiveTestCase(unittest.TestCase):
    """Base: a temporary archive with the first sample export ingested."""

    ingest_on_setup = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="arcs-test-"))
        self.config = ArchiveConfig(root=self.tmp / "archive", operator="test")
        self.config.save()
        self.archive = Archive.open(self.config)
        if self.ingest_on_setup:
            self.result = ingest_path(self.archive, EXPORT_V1, operator="test")

    def tearDown(self):
        self.archive.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conversation(self, source_id: str) -> str:
        row = self.archive.one(
            "SELECT conversation_id FROM conversations WHERE source_conversation_id=?",
            (source_id,),
        )
        self.assertIsNotNone(row, "no conversation %s" % source_id)
        return row["conversation_id"]

    def open_gate(self):
        integrity.run(self.archive, "full")
        return gate.authorise(self.archive)


# ---------------------------------------------------------------- ingestion
class TestIngestion(ArchiveTestCase):
    def test_all_sample_conversations_ingested(self):
        self.assertEqual(len(self.result.conversations), 5)
        self.assertEqual(self.result.new_count, 5)
        self.assertEqual(self.archive.scalar("SELECT COUNT(*) FROM conversations"), 5)
        self.assertEqual(self.archive.scalar("SELECT COUNT(*) FROM messages"), 19)

    def test_every_message_retains_its_original_node(self):
        for row in self.archive.all("SELECT metadata_json FROM message_versions"):
            meta = json.loads(row["metadata_json"])
            self.assertIn("node", meta, "the original node object was not retained")
            self.assertIn("message", meta["node"])

    def test_provenance_recorded_on_every_source_record(self):
        rows = self.archive.all("SELECT * FROM source_records")
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["source_method"])
            self.assertTrue(row["adapter"])
            self.assertTrue(row["extraction_date"])
            self.assertTrue(row["blob_sha256"])

    def test_source_bytes_are_hashed_and_stored(self):
        for row in self.archive.all("SELECT source_sha256 FROM conversation_versions"):
            path = self.config.blobs_dir / row["source_sha256"][:2] / \
                row["source_sha256"][2:4] / (row["source_sha256"] + ".bin")
            self.assertTrue(path.exists())
            self.assertEqual(sha256_bytes(path.read_bytes()), row["source_sha256"])

    def test_source_blobs_are_read_only_on_disk(self):
        for path in self.config.blobs_dir.rglob("*.bin"):
            self.assertFalse(os.stat(path).st_mode & 0o200,
                             "%s is writable; source material must not be" % path)

    def test_message_order_and_roles_preserved(self):
        conversation_id = self.conversation("arcs-0001-ledger")
        rows = self.archive.all(
            "SELECT mv.seq, mv.role, mv.on_canonical_path FROM conversation_version_messages cvm "
            "JOIN message_versions mv ON mv.version_id=cvm.message_version_id "
            "JOIN conversation_versions cv ON cv.version_id=cvm.conversation_version_id "
            "WHERE cv.conversation_id=? AND cv.is_current=1 ORDER BY cvm.seq",
            (conversation_id,),
        )
        self.assertEqual([r["seq"] for r in rows], sorted(r["seq"] for r in rows))
        self.assertEqual(rows[0]["role"], "user")
        self.assertTrue(any(not r["on_canonical_path"] for r in rows),
                        "the edited branch was not retained")

    def test_attachments_and_citations_captured(self):
        kinds = {r["kind"] for r in self.archive.all("SELECT kind FROM attachments")}
        self.assertIn("file", kinds)
        self.assertIn("citation", kinds)


class TestDeduplication(ArchiveTestCase):
    def test_identical_reingest_adds_sightings_not_versions(self):
        before_versions = self.archive.scalar("SELECT COUNT(*) FROM conversation_versions")
        before_blobs = self.archive.scalar("SELECT COUNT(*) FROM source_blobs")
        before_records = self.archive.scalar("SELECT COUNT(*) FROM source_records")

        again = ingest_path(self.archive, EXPORT_V1, operator="test")

        self.assertEqual([c.status for c in again.conversations], ["unchanged"] * 5)
        self.assertEqual(
            self.archive.scalar("SELECT COUNT(*) FROM conversation_versions"), before_versions
        )
        self.assertEqual(self.archive.scalar("SELECT COUNT(*) FROM source_blobs"), before_blobs)
        self.assertGreater(
            self.archive.scalar("SELECT COUNT(*) FROM source_records"), before_records,
            "the second capture event lost its provenance record",
        )
        self.assertEqual(
            self.archive.scalar(
                "SELECT COUNT(*) FROM source_records WHERE duplicate_of_record_id IS NOT NULL"),
            6,  # 5 conversations + the container
        )
        self.assertEqual(
            self.archive.scalar("SELECT COUNT(*) FROM conversation_version_sightings"), 10
        )


class TestVersioning(ArchiveTestCase):
    def test_later_capture_appends_a_version_and_keeps_the_earlier_one(self):
        conversation_id = self.conversation("arcs-0002-namelist")
        first = self.archive.one(
            "SELECT * FROM conversation_versions WHERE conversation_id=? AND is_current=1",
            (conversation_id,),
        )
        ingest_path(self.archive, EXPORT_V2, operator="test", correction_reason="second export")

        versions = self.archive.all(
            "SELECT * FROM conversation_versions WHERE conversation_id=? ORDER BY version_no",
            (conversation_id,),
        )
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["version_id"], first["version_id"])
        self.assertEqual(versions[0]["is_current"], 0)
        self.assertEqual(versions[1]["is_current"], 1)
        self.assertEqual(versions[1]["supersedes_version_id"], first["version_id"])
        self.assertTrue(versions[1]["correction_reason"])
        # the earlier version's own row is untouched
        self.assertEqual(versions[0]["content_sha256"], first["content_sha256"])
        self.assertEqual(versions[0]["message_count"], first["message_count"])

    def test_title_correction_creates_a_version_without_touching_messages(self):
        conversation_id = self.conversation("arcs-0003-coast")
        before = self.archive.scalar(
            "SELECT COUNT(*) FROM message_versions WHERE conversation_id=?", (conversation_id,)
        )
        ingest_path(self.archive, EXPORT_V2, operator="test")
        after = self.archive.scalar(
            "SELECT COUNT(*) FROM message_versions WHERE conversation_id=?", (conversation_id,)
        )
        self.assertEqual(before, after, "an unchanged message was re-versioned")
        titles = [
            r["title"] for r in self.archive.all(
                "SELECT title FROM conversation_versions WHERE conversation_id=? "
                "ORDER BY version_no", (conversation_id,))
        ]
        self.assertEqual(len(titles), 2)
        self.assertNotEqual(titles[0], titles[1])

    def test_message_versions_are_superseded_not_overwritten(self):
        ingest_path(self.archive, EXPORT_V2, operator="test")
        superseded = self.archive.all(
            "SELECT * FROM message_versions WHERE is_current=0"
        )
        self.assertTrue(superseded, "no superseded message versions were retained")
        for row in superseded:
            self.assertIsNotNone(
                self.archive.one(
                    "SELECT version_id FROM message_versions WHERE supersedes_version_id=?",
                    (row["version_id"],))
            )


# ----------------------------------------------------------- reconstruction
class TestReconstruction(ArchiveTestCase):
    def test_conversations_recover_byte_for_byte(self):
        """The strong claim, checked against the untouched original file."""
        original = EXPORT_V1.read_bytes()
        text = EXPORT_V1.read_text(encoding="utf-8")
        spans = {
            span.value["conversation_id"]: original[span.byte_start:span.byte_end]
            for span in array_spans(text)
        }
        out = self.tmp / "recovered"
        manifest = reconstruct.export_originals(self.archive, out)
        checked = 0
        for item in manifest["files"]:
            source_id = item.get("source_conversation_id")
            if source_id in spans:
                self.assertEqual((out / item["file"]).read_bytes(), spans[source_id],
                                 "recovered bytes differ for " + source_id)
                checked += 1
        self.assertEqual(checked, 5)

    def test_whole_export_file_recovers_byte_for_byte(self):
        out = self.tmp / "recovered"
        manifest = reconstruct.export_originals(self.archive, out)
        containers = [f for f in manifest["files"] if f.get("kind") == "container"]
        self.assertTrue(containers)
        self.assertTrue(
            any((out / f["file"]).read_bytes() == EXPORT_V1.read_bytes() for f in containers)
        )

    def test_originals_rebuild_from_normalised_tables_alone(self):
        for report in reconstruct.verify_all(self.archive, include_history=True):
            self.assertTrue(report.structural_match,
                            "%s: %s" % (report.title, report.differences[:3] or report.error))
            self.assertTrue(report.canonical_match)

    def test_reconstruction_survives_every_adapter(self):
        ingest_path(self.archive, EXPORT_V2, operator="test")
        ingest_path(self.archive, CAPTURE, operator="test")
        reports = reconstruct.verify_all(self.archive, include_history=True)
        adapters = {r.adapter for r in reports}
        self.assertEqual(adapters, {"chatgpt_export", "ui_capture"})
        self.assertTrue(all(r.ok for r in reports))

    def test_corruption_is_detected(self):
        row = self.archive.one("SELECT source_sha256 FROM conversation_versions LIMIT 1")
        digest = row["source_sha256"]
        path = self.config.blobs_dir / digest[:2] / digest[2:4] / (digest + ".bin")
        path.chmod(0o644)
        path.write_bytes(b'{"tampered": true}')
        reports = reconstruct.verify_all(self.archive)
        self.assertTrue(any(not r.ok for r in reports))
        report = integrity.run(self.archive, "raw_layer")
        self.assertFalse(report.passed)
        self.assertIn("blobs_hash_verified", [c.name for c in report.failed])


class TestIntegritySuite(ArchiveTestCase):
    def test_full_suite_passes_on_a_healthy_archive(self):
        ingest_path(self.archive, EXPORT_V2, operator="test")
        ingest_path(self.archive, CAPTURE, operator="test")
        report = integrity.run(self.archive, "full")
        self.assertTrue(report.passed, [c.name for c in report.failed])
        self.assertGreaterEqual(len(report.checks), 20)

    def test_deleting_a_source_record_is_detected(self):
        """Simulates an out-of-band edit, e.g. someone with the sqlite3 CLI."""
        integrity.run(self.archive, "full")  # first snapshot
        record = self.archive.one("SELECT record_id FROM source_records LIMIT 1")
        self.archive.source.execute("PRAGMA foreign_keys = OFF")
        self.archive.source.execute("DELETE FROM source_records WHERE record_id=?",
                                    (record["record_id"],))
        self.archive.source.commit()
        self.archive.source.execute("PRAGMA foreign_keys = ON")
        report = integrity.run(self.archive, "raw_layer")
        self.assertIn("source_records_append_only", [c.name for c in report.failed])

    def test_repointing_a_source_record_at_other_bytes_is_detected(self):
        integrity.run(self.archive, "full")
        record = self.archive.one("SELECT record_id, blob_sha256 FROM source_records LIMIT 1")
        other = self.archive.one(
            "SELECT sha256 FROM source_blobs WHERE sha256 <> ? LIMIT 1",
            (record["blob_sha256"],))
        self.archive.source.execute(
            "UPDATE source_records SET blob_sha256=? WHERE record_id=?",
            (other["sha256"], record["record_id"]))
        self.archive.source.commit()
        report = integrity.run(self.archive, "raw_layer")
        self.assertIn("source_records_append_only", [c.name for c in report.failed])

    def test_fingerprint_changes_when_the_archive_changes(self):
        before = integrity.archive_fingerprint(self.archive)
        ingest_path(self.archive, EXPORT_V2, operator="test")
        self.assertNotEqual(before, integrity.archive_fingerprint(self.archive))


# ------------------------------------------------------------- credentials
class TestCredentialBoundary(ArchiveTestCase):
    def test_bundle_carrying_a_session_cookie_is_refused(self):
        before = self.archive.scalar("SELECT COUNT(*) FROM source_records")
        with self.assertRaises(CredentialMaterialFound):
            ingest_path(self.archive, LEAKY, operator="test")
        self.assertEqual(self.archive.scalar("SELECT COUNT(*) FROM source_records"), before,
                         "a refused input still wrote source records")
        self.assertEqual(
            self.archive.scalar("SELECT COUNT(*) FROM import_batches WHERE status='refused'"), 1
        )

    def test_refusal_does_not_copy_the_offending_file_into_the_archive(self):
        with self.assertRaises(CredentialMaterialFound):
            ingest_path(self.archive, LEAKY, operator="test")
        reports = list(self.config.quarantine_dir.glob("*.refusal.json"))
        self.assertEqual(len(reports), 1)
        body = json.loads(reports[0].read_text())
        self.assertIn("forbidden_key:cookie", body["finding_kinds"])
        leaked = LEAKY.read_text(encoding="utf-8")
        for path in self.config.root.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(b"__Secure-next-auth.session-token", content,
                                 "credential material reached %s" % path)
        self.assertNotIn("__Secure-next-auth", json.dumps(body))
        del leaked

    def test_a_pasted_secret_in_conversation_content_is_preserved_and_flagged(self):
        """Source material is never dropped; it is classified instead."""
        payload = json.loads(EXPORT_V1.read_text(encoding="utf-8"))
        conv = payload[0]
        node = next(n for n in conv["mapping"].values() if n.get("message"))
        node["message"]["content"]["parts"] = [
            "Here is the key I was given: sk-abcdefghijklmnopqrstuvwxyz012345"
        ]
        conv["conversation_id"] = conv["id"] = "arcs-secret-in-content"
        path = self.tmp / "secret.json"
        path.write_text(json.dumps([conv], indent=2), encoding="utf-8")

        result = ingest_path(self.archive, path, adapter_name="chatgpt_export", operator="test")
        self.assertEqual(len(result.conversations), 1)
        outcome = result.conversations[0]
        self.assertEqual(outcome.status, "new")
        self.assertIn("openai_api_key", outcome.flags)
        self.assertEqual(outcome.security_class, "Restricted")


class TestNetworkSeal(unittest.TestCase):
    def test_outbound_connections_are_blocked_while_sealed(self):
        netguard.seal("test")
        try:
            self.assertTrue(netguard.is_sealed())
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            with self.assertRaises(NetworkBlocked):
                sock.connect(("example.com", 80))
            sock.close()
            with self.assertRaises(NetworkBlocked):
                socket.create_connection(("example.com", 80), timeout=1)
        finally:
            netguard.unseal("test teardown")

    def test_unsealing_requires_a_reason(self):
        with self.assertRaises(ValueError):
            netguard.unseal("")

    def test_local_connections_still_work(self):
        netguard.seal("test")
        try:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client = socket.socket()
            client.connect(listener.getsockname())  # must not raise
            client.close()
            listener.close()
        finally:
            netguard.unseal("test teardown")


class TestCaptureScript(unittest.TestCase):
    """The promise about credentials is checked against the shipped script."""

    def setUp(self):
        self.source = USERSCRIPT.read_text(encoding="utf-8")

    def test_script_never_reads_cookies(self):
        self.assertIsNone(re.search(r"document\s*\.\s*cookie", self.source))

    def test_script_never_requests_an_auth_token(self):
        self.assertNotIn("/api/auth/session", self.source.replace(
            "does NOT call\n *     /api/auth/session", ""))

    def test_script_issues_only_get_requests(self):
        methods = re.findall(r"(?<![A-Za-z0-9_])method\s*:\s*[\"']([A-Z]+)[\"']", self.source)
        self.assertTrue(methods)
        self.assertEqual(set(methods), {"GET"})

    def test_script_sends_nothing_anywhere(self):
        self.assertNotIn("XMLHttpRequest", self.source)
        self.assertNotIn("sendBeacon", self.source)
        for match in re.findall(r"fetch\(\s*[\"'`]([^\"'`]+)", self.source):
            self.assertTrue(match.startswith("/"),
                            "the capture script must only fetch same-origin paths: " + match)

    def test_script_scrubs_credential_shaped_fields(self):
        self.assertIn("FORBIDDEN_KEYS", self.source)
        for key in ("access_token", "authorization", "session_token"):
            self.assertIn(key, self.source)


# ------------------------------------------------------- security classes
class TestSecurityClasses(ArchiveTestCase):
    def test_reclassification_is_recorded_with_a_rationale(self):
        conversation_id = self.conversation("arcs-0005-restricted")
        set_class(self.archive, "conversation", conversation_id, "Sacred",
                  rationale="urupā location given in confidence", actor="test", cascade=True)
        row = self.archive.one("SELECT security_class FROM conversations WHERE conversation_id=?",
                               (conversation_id,))
        self.assertEqual(row["security_class"], "Sacred")
        history = self.archive.all(
            "SELECT * FROM security_class_history WHERE subject_type='conversation' "
            "AND subject_id=?", (conversation_id,))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["previous_class"], "Normal")
        self.assertTrue(history[0]["rationale"])
        messages = {
            r["security_class"] for r in self.archive.all(
                "SELECT security_class FROM messages WHERE conversation_id=?", (conversation_id,))
        }
        self.assertEqual(messages, {"Sacred"})

    def test_search_can_withhold_above_a_class(self):
        conversation_id = self.conversation("arcs-0005-restricted")
        set_class(self.archive, "conversation", conversation_id, "Sacred",
                  rationale="test", cascade=True)
        unfiltered = search.search_messages(self.archive, "urupā", limit=10)
        filtered = search.search_messages(self.archive, "urupā", limit=10, max_class="Normal")
        self.assertGreater(unfiltered.total, 0)
        self.assertEqual(filtered.total, 0)
        self.assertGreater(filtered.withheld, 0)


class TestEvidencePackets(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        self.sacred = self.conversation("arcs-0005-restricted")
        set_class(self.archive, "conversation", self.sacred, "Sacred",
                  rationale="test", cascade=True)
        integrity.run(self.archive, "full")

    def test_packet_excludes_material_above_its_class_and_says_so(self):
        packet = evidence.build_packet(self.archive, recipient="Grace", max_class="Normal",
                                       purpose="test")
        self.assertEqual(packet.conversations, 4)
        self.assertTrue(any(w.get("conversation_id") == self.sacred for w in packet.withheld))
        manifest = json.loads((Path(packet.path) / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["security"]["max_security_class"], "Normal")
        self.assertGreaterEqual(manifest["security"]["withheld_count"], 1)

    def test_sacred_release_requires_an_acknowledgement(self):
        with self.assertRaises(SecurityClassViolation):
            evidence.build_packet(self.archive, recipient="Grok", max_class="Sacred")
        packet = evidence.build_packet(
            self.archive, recipient="Grok", max_class="Sacred",
            acknowledgement="Released with the agreement of the kaumātua who gave it.",
        )
        self.assertEqual(packet.conversations, 5)
        manifest = json.loads((Path(packet.path) / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertIn("kaumātua", manifest["security"]["acknowledgement"])
        row = self.archive.one("SELECT acknowledgement FROM evidence_packets WHERE packet_id=?",
                               (packet.packet_id,))
        self.assertTrue(row["acknowledgement"])

    def test_packet_is_read_only_and_self_verifying(self):
        packet = evidence.build_packet(self.archive, recipient="Grace", max_class="Normal")
        report = evidence.verify_packet(packet.path)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["read_only"])

    def test_tampering_with_a_packet_is_detected(self):
        packet = evidence.build_packet(self.archive, recipient="Grace", max_class="Normal")
        target = next(Path(packet.path).glob("readable/*.md"))
        target.chmod(0o644)
        target.write_text("this is not what the archive holds", encoding="utf-8")
        report = evidence.verify_packet(packet.path)
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["mismatched"]), 1)

    def test_packet_source_files_match_the_archive_hashes(self):
        packet = evidence.build_packet(self.archive, recipient="Grace", max_class="Normal")
        rows = json.loads(
            "[" + ",".join(
                (Path(packet.path) / "data" / "conversations.jsonl")
                .read_text(encoding="utf-8").splitlines()
            ) + "]"
        )
        for row in rows:
            data = (Path(packet.path) / row["source_file"]).read_bytes()
            self.assertEqual(sha256_bytes(data), row["source_sha256"])


# --------------------------------------------------------------- projection
class TestObsidianProjection(ArchiveTestCase):
    def test_projection_has_stable_names_anchors_and_backlinks(self):
        result = obsidian.project(self.archive, max_class="Private")
        self.assertEqual(result.conversations, 5)
        note = next((Path(result.out_dir) / "conversations").glob("*ledger*.md"))
        body = note.read_text(encoding="utf-8")
        conversation_id = self.conversation("arcs-0001-ledger")
        self.assertIn("arcs_id: " + conversation_id, body)
        self.assertIn("^m1", body)
        self.assertIn("[[ARCS Archive]]", body)
        self.assertIn("source_sha256:", body)

        # regenerating must not move the note
        again = obsidian.project(self.archive, max_class="Private")
        self.assertEqual(sorted(result.files), sorted(again.files))

    def test_material_above_the_class_limit_is_a_stub(self):
        conversation_id = self.conversation("arcs-0005-restricted")
        set_class(self.archive, "conversation", conversation_id, "Sacred",
                  rationale="test", cascade=True)
        result = obsidian.project(self.archive, max_class="Private")
        self.assertEqual(result.stubs, 1)
        note = next((Path(result.out_dir) / "conversations").glob("*wahi*.md"))
        body = note.read_text(encoding="utf-8")
        self.assertIn("Withheld", body)
        self.assertIn("source_sha256:", body)   # provenance still recorded
        self.assertNotIn("urupā", body)          # content is not


# --------------------------------------------------------------- searching
class TestSearch(ArchiveTestCase):
    def test_full_text_search_finds_terms(self):
        result = search.search_messages(self.archive, "karaka", limit=10)
        self.assertGreater(result.total, 0)
        self.assertTrue(all("karaka" in h.snippet.lower() or h.snippet for h in result.hits))

    def test_search_index_follows_corrections(self):
        ingest_path(self.archive, EXPORT_V2, operator="test")
        result = search.search_messages(self.archive, "burial record", limit=5)
        self.assertGreater(result.total, 0)
        for hit in result.hits:
            row = self.archive.one(
                "SELECT is_current FROM message_versions WHERE version_id=?", (hit.version_id,))
            self.assertEqual(row["is_current"], 1)

    def test_query_special_characters_do_not_break_search(self):
        for query in ('"', "AND", "chains (12)", "N 47° E", ""):
            search.search_messages(self.archive, query, limit=3)


class TestSemanticIndex(ArchiveTestCase):
    def test_indexing_is_gated_and_deterministic(self):
        with self.assertRaises(InterpretationGateError):
            semantic.build_index(self.archive)
        self.open_gate()
        first = semantic.build_index(self.archive)
        self.assertGreater(first.indexed, 0)
        vectors = {
            r["subject_id"]: bytes(r["vector"]) for r in self.archive.all(
                "SELECT subject_id, vector FROM embeddings", conn=self.archive.derived)
        }
        again = semantic.build_index(self.archive, rebuild=True)
        self.assertEqual(again.indexed, first.indexed)
        for row in self.archive.all("SELECT subject_id, vector FROM embeddings",
                                    conn=self.archive.derived):
            self.assertEqual(bytes(row["vector"]), vectors[row["subject_id"]],
                             "the local embedder is not deterministic")

    def test_semantic_search_returns_related_material(self):
        self.open_gate()
        semantic.build_index(self.archive)
        hits = semantic.semantic_search(self.archive, "surveyor's chain measurement", k=5)
        self.assertTrue(hits)
        self.assertTrue(any("chain" in (h["text"] or "").lower() for h in hits))

    def test_vectors_live_in_the_derived_database_only(self):
        self.open_gate()
        semantic.build_index(self.archive)
        with self.assertRaises(sqlite3.OperationalError):
            self.archive.source.execute("SELECT COUNT(*) FROM embeddings")


# ------------------------------------------------------------------- gate
class TestInterpretationGate(ArchiveTestCase):
    def test_gate_is_closed_until_the_raw_layer_verifies(self):
        state = gate.status(self.archive)
        self.assertFalse(state["open"])
        with self.assertRaises(InterpretationGateError):
            gate.authorise(self.archive)

    def test_gate_opens_after_a_passing_run(self):
        integrity.run(self.archive, "full")
        state = gate.status(self.archive)
        self.assertTrue(state["open"], state["reason"])
        token = gate.authorise(self.archive)
        self.assertTrue(token.run_id)

    def test_gate_closes_again_when_the_archive_changes(self):
        integrity.run(self.archive, "full")
        self.assertTrue(gate.status(self.archive)["open"])
        ingest_path(self.archive, EXPORT_V2, operator="test")
        state = gate.status(self.archive)
        self.assertFalse(state["open"])
        self.assertIn("changed", state["reason"])

    def test_extraction_refuses_while_the_gate_is_closed(self):
        with self.assertRaises(InterpretationGateError):
            arch_extract.extract_conversation(
                self.archive, self.conversation("arcs-0001-ledger"), dry_run=True)

    def test_a_failing_check_keeps_the_gate_closed(self):
        row = self.archive.one("SELECT source_sha256 FROM conversation_versions LIMIT 1")
        digest = row["source_sha256"]
        path = self.config.blobs_dir / digest[:2] / digest[2:4] / (digest + ".bin")
        path.chmod(0o644)
        path.write_bytes(b"tampered")
        integrity.run(self.archive, "full")
        self.assertFalse(gate.status(self.archive)["open"])


# ------------------------------------------------------------- archaeology
class TestPropositions(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.open_gate()
        self.message = self.archive.one(
            "SELECT mv.version_id, mv.content_text FROM message_versions mv "
            "JOIN conversations c ON c.conversation_id = mv.conversation_id "
            "WHERE c.source_conversation_id='arcs-0001-ledger' AND mv.role='user' "
            "ORDER BY mv.seq LIMIT 1"
        )
        self.quote = "Survey of the Kaiora Point reserve, commenced 14 March 1887"

    def test_a_proposition_records_every_required_column(self):
        proposition_id = arch_props.add_proposition(
            self.archive, self.message["version_id"], self.quote,
            entity="Kaiora Point reserve", location="Kaiora Point", event_date="1887-03-14",
            number_raw="12 chains", number_value=12.0, number_unit="chains", arc="Kaiora boundary",
            contemporaneous_meaning="what 'reserve' meant in 1887",
            later_interpretation="what we now read it as",
            verification_status="quote_verified", probability_role="assertion", probability=0.9,
        )
        data = arch_props.get_proposition(self.archive, proposition_id)
        for column in ("conversation_id", "message_id", "event_date", "quote_text", "entity",
                       "location", "number_raw", "arc", "contemporaneous_meaning",
                       "later_interpretation", "verification_status", "probability_role"):
            self.assertTrue(data[column], "missing %s" % column)
        self.assertIn("generated_next", data)
        self.assertEqual(data["quote_text"], self.quote)
        self.assertNotEqual(data["contemporaneous_meaning"], data["later_interpretation"])

    def test_a_quotation_that_is_not_in_the_source_is_refused(self):
        with self.assertRaises(ArcsError):
            arch_props.add_proposition(
                self.archive, self.message["version_id"],
                "Survey of the Kaiora Point reserve, commenced 15 March 1887")

    def test_corrections_append_and_keep_the_earlier_version(self):
        proposition_id = arch_props.add_proposition(
            self.archive, self.message["version_id"], self.quote, entity="Kaiora Point")
        arch_props.correct_proposition(self.archive, proposition_id,
                                       reason="entity narrowed by the 1898 succession order",
                                       entity="Kaiora Point reserve (holding 214)")
        history = arch_props.proposition_history(self.archive, proposition_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["entity"], "Kaiora Point")
        self.assertEqual(history[0]["is_current"], 0)
        self.assertEqual(history[1]["is_current"], 1)
        self.assertEqual(history[1]["supersedes_version_id"], history[0]["version_id"])
        self.assertTrue(history[1]["correction_reason"])

    def test_a_correction_requires_a_reason(self):
        proposition_id = arch_props.add_proposition(
            self.archive, self.message["version_id"], self.quote)
        with self.assertRaises(ArcsError):
            arch_props.correct_proposition(self.archive, proposition_id, reason="")

    def test_retraction_keeps_the_record(self):
        proposition_id = arch_props.add_proposition(
            self.archive, self.message["version_id"], self.quote)
        arch_props.retract_proposition(self.archive, proposition_id, reason="misread the ledger")
        data = arch_props.get_proposition(self.archive, proposition_id)
        self.assertTrue(data["retracted_at"])
        self.assertEqual(data["quote_text"], self.quote)

    def test_quotations_verify_against_the_stored_source_bytes(self):
        proposition_id = arch_props.add_proposition(
            self.archive, self.message["version_id"], self.quote)
        report = arch_props.verify_quote(self.archive, proposition_id)
        self.assertTrue(report["quote_hash_ok"])
        self.assertTrue(report["in_message_version"])
        self.assertTrue(report["in_source_bytes"])

    def test_propositions_live_outside_the_source_database(self):
        arch_props.add_proposition(self.archive, self.message["version_id"], self.quote)
        with self.assertRaises(sqlite3.OperationalError):
            self.archive.source.execute("SELECT COUNT(*) FROM propositions")
        self.assertNotEqual(self.config.source_db_path, self.config.derived_db_path)


class TestExtraction(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.open_gate()
        self.conversation_id = self.conversation("arcs-0001-ledger")

    def test_dry_run_records_nothing(self):
        summary = arch_extract.extract_conversation(self.archive, self.conversation_id,
                                                    dry_run=True)
        self.assertGreater(summary["candidates"], 0)
        self.assertEqual(
            self.archive.scalar("SELECT COUNT(*) FROM propositions", conn=self.archive.derived), 0
        )

    def test_extraction_quotes_verbatim_and_records_its_method(self):
        summary = arch_extract.extract_conversation(self.archive, self.conversation_id,
                                                    arc="Kaiora boundary", dry_run=False)
        self.assertGreater(summary["recorded"], 0)
        for row in self.archive.all(
            "SELECT * FROM proposition_versions WHERE is_current=1", conn=self.archive.derived
        ):
            message = self.archive.one(
                "SELECT content_text FROM message_versions WHERE version_id=?",
                (row["message_version_id"],))
            self.assertEqual(
                message["content_text"][row["quote_char_start"]:row["quote_char_end"]],
                row["quote_text"])
            self.assertEqual(row["created_by"], "rule_based:v1")
            self.assertTrue(row["method"].startswith("sentence_scan"))
            self.assertIsNone(row["later_interpretation"],
                              "the extractor must not invent an interpretation")

    def test_extraction_is_bound_to_a_verification_run(self):
        arch_extract.extract_conversation(self.archive, self.conversation_id, dry_run=False)
        run = self.archive.one("SELECT * FROM interpretation_runs", conn=self.archive.derived)
        self.assertEqual(run["gate_run_id"], self.token.run_id)
        self.assertEqual(run["gate_fingerprint"], self.token.fingerprint)
        self.assertEqual(run["method"], "rule_based:v1")

    def test_extraction_is_deterministic(self):
        first = arch_extract.find_candidates(self.archive, self.conversation_id)
        second = arch_extract.find_candidates(self.archive, self.conversation_id)
        self.assertEqual([c.quote for c in first], [c.quote for c in second])

    def test_hedged_statements_are_not_filed_as_assertions(self):
        summary = arch_extract.extract_conversation(self.archive,
                                                    self.conversation("arcs-0003-coast"),
                                                    dry_run=True)
        roles = {item["probability_role"] for item in summary["items"]}
        self.assertTrue(roles - {"assertion"},
                        "no hedged sentence was recognised: %s" % roles)


class TestDiscoveryGraph(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.open_gate()
        self.conversation_id = self.conversation("arcs-0001-ledger")
        self.observation = arch_graph.add_search_event(
            self.archive, kind="archive_read", query="1887 ledger page 14",
            result_summary="photograph of a ledger page")
        summary = arch_extract.extract_conversation(self.archive, self.conversation_id,
                                                    arc="Kaiora boundary", dry_run=False)
        self.propositions = summary["proposition_ids"]
        arch_graph.add_edge(self.archive, "search_event", self.observation,
                            "proposition", self.propositions[0], relation="generated",
                            rationale="the photograph is what prompted the reading")

    def test_edges_are_directed_and_typed(self):
        edges = self.archive.all("SELECT * FROM discovery_edges", conn=self.archive.derived)
        self.assertTrue(edges)
        for edge in edges:
            self.assertIn(edge["relation"], arch_graph.RELATIONS)
            self.assertIn(edge["from_kind"], arch_graph.NODE_KINDS)

    def test_a_discovery_traces_back_to_the_observation_that_generated_it(self):
        last = self.propositions[-1]
        chain = arch_graph.ancestors(self.archive, "proposition", last)
        kinds = {step["kind"] for step in chain}
        ids = {step["id"] for step in chain}
        self.assertIn("search_event", kinds)
        self.assertIn(self.observation, ids)

    def test_descendants_show_what_an_observation_produced(self):
        produced = arch_graph.descendants(self.archive, "search_event", self.observation)
        self.assertGreaterEqual(len(produced), len(self.propositions) - 1)

    def test_graph_exports(self):
        dot = arch_graph.to_dot(self.archive)
        self.assertIn("digraph arcs_discovery", dot)
        self.assertIn("->", dot)
        data = arch_graph.to_json(self.archive)
        self.assertTrue(data["nodes"])
        self.assertTrue(data["edges"])

    def test_edges_are_idempotent(self):
        before = self.archive.scalar("SELECT COUNT(*) FROM discovery_edges",
                                     conn=self.archive.derived)
        arch_graph.add_edge(self.archive, "search_event", self.observation,
                            "proposition", self.propositions[0], relation="generated")
        after = self.archive.scalar("SELECT COUNT(*) FROM discovery_edges",
                                    conn=self.archive.derived)
        self.assertEqual(before, after)


class TestLayerSeparation(ArchiveTestCase):
    def test_source_database_holds_no_interpretation_tables(self):
        report = integrity.run(self.archive, "raw_layer")
        check = next(c for c in report.checks if c.name == "derived_layer_separated")
        self.assertTrue(check.passed, check.failures)

    def test_deleting_the_derived_database_loses_no_source_material(self):
        integrity.run(self.archive, "full")
        self.archive.close()
        os.remove(self.config.derived_db_path)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(self.config.derived_db_path) + suffix)
            if extra.exists():
                extra.unlink()
        self.archive = Archive.open(self.config)
        report = integrity.run(self.archive, "full")
        self.assertTrue(report.passed, [c.name for c in report.failed])
        self.assertEqual(self.archive.scalar("SELECT COUNT(*) FROM conversations"), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
