"""The interpretation gate.

Nothing in the ARCS Archaeology layer may run until the raw layer has been
shown to work: ingestion, provenance, hashing, recovery and completeness.  That
rule is enforced here rather than left to discipline.

An authorisation is bound to an exact archive state.  Ingest new material and
the recorded fingerprint no longer matches, so the gate closes until the suite
is re-run.  This keeps the guarantee honest: an interpretation can always name
the verification run that licensed it, and that run's fingerprint can be
recomputed from the source records to check the claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import integrity
from .errors import InterpretationGateError

#: Checks that must have passed for the gate to open.  Named explicitly so that
#: adding a check to the suite cannot silently weaken the gate.
REQUIRED_CHECKS = (
    "blobs_present",
    "blobs_hash_verified",
    "source_records_have_blobs",
    "dedupe_preserves_provenance",
    "provenance_fields_present",
    "version_chains_intact",
    "source_records_append_only",
    "derived_layer_separated",
    "originals_recoverable_bytes",
    "originals_rebuildable_from_tables",
    "message_counts_match",
    "message_order_preserved",
    "required_fields_present",
    "membership_recorded",
)


@dataclass(frozen=True)
class GateToken:
    """Proof that the raw layer verified at a specific archive state."""

    run_id: str
    fingerprint: str
    verified_at: str
    suite: str

    def to_dict(self) -> dict:
        return {
            "gate_run_id": self.run_id,
            "gate_fingerprint": self.fingerprint,
            "verified_at": self.verified_at,
            "suite": self.suite,
        }


def status(archive) -> dict:
    """Why the gate is open or closed, in a form fit to print."""
    current = integrity.archive_fingerprint(archive)
    row = integrity.latest_run(archive, "full")
    if row is None:
        return {"open": False, "reason": "no verification run has been recorded",
                "current_fingerprint": current}
    report = _report(row)
    missing = _missing_checks(report)
    stale = row["archive_fingerprint"] != current
    open_ = bool(row["passed"]) and not missing and not stale
    reason = "verified"
    if not row["passed"]:
        reason = "the last verification run failed"
    elif missing:
        reason = "required checks did not run or did not pass: " + ", ".join(missing)
    elif stale:
        reason = "the archive changed since the last verification run"
    return {
        "open": open_,
        "reason": reason,
        "run_id": row["run_id"],
        "verified_at": row["finished_at"],
        "verified_fingerprint": row["archive_fingerprint"],
        "current_fingerprint": current,
        "checks_total": row["checks_total"],
        "checks_failed": row["checks_failed"],
    }


def _report(row) -> dict:
    import json

    return json.loads(row["report_json"] or "{}")


def _missing_checks(report: dict) -> list:
    passed = {c["name"] for c in report.get("checks", []) if c.get("passed")}
    return [name for name in REQUIRED_CHECKS if name not in passed]


def authorise(archive, purpose: str = "", auto_verify: bool = False) -> GateToken:
    """Return a token, or refuse with an explanation of what to fix.

    ``auto_verify`` re-runs the suite first; it is what the CLI does when the
    only problem is that the archive has grown since the last run.
    """
    state = status(archive)
    if not state["open"] and auto_verify:
        integrity.run(archive, "full")
        state = status(archive)
    if not state["open"]:
        raise InterpretationGateError(
            "interpretation is not authorised: %s. "
            "Run `arcs verify` and fix any failing check first%s."
            % (state["reason"], (" (purpose: %s)" % purpose) if purpose else "")
        )
    return GateToken(
        run_id=state["run_id"],
        fingerprint=state["verified_fingerprint"],
        verified_at=state["verified_at"],
        suite="full",
    )
