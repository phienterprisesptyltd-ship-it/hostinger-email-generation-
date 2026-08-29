"""Rule-based candidate extraction.

Deliberately *not* a model.  Every candidate this produces is deterministic,
explainable and re-derivable: the same archive state yields the same
candidates, and each one names the rule that produced it.  Candidates are
recorded with ``contemporaneous_meaning`` and ``later_interpretation`` left
empty, because neither can be inferred from a sentence - they are for the
researcher to fill in.

The extractor runs only through the interpretation gate, so it cannot touch an
archive whose raw layer has not verified.

What it does:

* splits message text into sentences, keeping exact character offsets so every
  candidate quotes the source verbatim;
* keeps sentences that carry a date, a measurement, or a named entity - the
  sentences that can be checked against another source;
* records the facets it found (dates, numbers with units, capitalised names,
  place references) with their offsets and the rule that matched;
* assigns a ``probability_role`` from the sentence's own hedging language, so a
  speculation is never filed as an assertion;
* links each candidate to the one before it in the conversation with a
  ``prompted`` edge, building the discovery graph's spine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..gate import authorise
from ..util import new_id, utcnow
from .graph import add_edge
from .propositions import add_proposition

# ------------------------------------------------------------------ patterns
_SENTENCE_END = re.compile(r"(?<=[.!?])[\s]+(?=[A-ZĀĒĪŌŪ“\"'(])|\n{2,}")

MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
          "November|December")
DATE_PATTERNS = (
    ("iso_date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("day_month_year", re.compile(r"\b\d{1,2}\s+(?:%s)\s+\d{4}\b" % MONTHS)),
    ("month_day_year", re.compile(r"\b(?:%s)\s+\d{1,2},?\s+\d{4}\b" % MONTHS)),
    ("year", re.compile(r"\b(?:1[5-9]\d{2}|20\d{2})\b")),
)

UNITS = (
    "chains?|links?|metres?|meters?|m|km|kilometres?|feet|ft|miles?|acres?|hectares?|ha|"
    "years?|months?|days?|degrees?|°"
)
NUMBER_PATTERN = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>%s)\b" % UNITS, re.IGNORECASE
)

# A capitalised run, allowing macronised vowels and internal particles.
ENTITY_PATTERN = re.compile(
    r"\b(?:[A-ZĀĒĪŌŪ][\wāēīōū’'-]+)(?:\s+(?:of|the|o|te|a)\s+[A-ZĀĒĪŌŪ][\wāēīōū’'-]+|"
    r"\s+[A-ZĀĒĪŌŪ][\wāēīōū’'-]+)*"
)
LOCATION_CUE = re.compile(
    r"\b(?:at|near|in|from|to|on|along|beside)\s+(?P<place>[A-ZĀĒĪŌŪ][\wāēīōū’'-]+"
    r"(?:\s+[A-ZĀĒĪŌŪ][\wāēīōū’'-]+)*)"
)

HEDGES = {
    "speculation": ("perhaps", "possibly", "conceivably", "might", "may be", "guess"),
    "hypothesis": ("suggests", "consistent with", "plausible", "probably", "likely",
                   "appears", "seems", "implies", "would imply"),
    "inference": ("therefore", "so ", "which means", "that gives", "closes it", "follows that"),
}

# Sentence-initial words that are not names.
_STOP_ENTITIES = {
    "The", "A", "An", "It", "That", "This", "There", "If", "In", "On", "At", "Two", "Three",
    "Record", "Draft", "Give", "What", "Below", "Hold", "Treat", "Store", "Found",
}


@dataclass
class Candidate:
    message_version_id: str
    conversation_id: str
    quote: str
    char_start: int
    char_end: int
    event_date: str = ""
    entity: str = ""
    location: str = ""
    number_raw: str = ""
    number_value: float = None
    number_unit: str = ""
    probability_role: str = "assertion"
    rules: list = field(default_factory=list)
    facets: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "quote": self.quote,
            "event_date": self.event_date,
            "entity": self.entity,
            "location": self.location,
            "number": self.number_raw,
            "probability_role": self.probability_role,
            "rules": self.rules,
        }


def split_sentences(text: str):
    """Yield ``(sentence, start, end)`` with offsets into the original text."""
    if not text:
        return
    position = 0
    for chunk in _SENTENCE_END.split(text):
        if chunk is None:
            continue
        index = text.find(chunk, position)
        if index < 0:
            continue
        stripped = chunk.strip()
        if not stripped:
            position = index + len(chunk)
            continue
        offset = index + chunk.index(stripped)
        yield stripped, offset, offset + len(stripped)
        position = index + len(chunk)


_MONTH_NUMBERS = {name.lower(): i for i, name in enumerate(MONTHS.split("|"), start=1)}


def normalise_date(raw: str) -> str:
    """ISO-8601 where the source is precise enough, year alone where it is not.

    The raw string the source actually used is kept as a facet, so normalising
    for sortability never costs the original wording.
    """
    if not raw:
        return ""
    text = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if match:
        month = _MONTH_NUMBERS.get(match.group(2).lower())
        if month:
            return "%s-%02d-%02d" % (match.group(3), month, int(match.group(1)))
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
    if match:
        month = _MONTH_NUMBERS.get(match.group(1).lower())
        if month:
            return "%s-%02d-%02d" % (match.group(3), month, int(match.group(2)))
    if re.fullmatch(r"\d{4}", text):
        return text
    return text


def _probability_role(sentence: str) -> str:
    lowered = sentence.lower()
    if sentence.rstrip().endswith("?"):
        return "question"
    for role, cues in HEDGES.items():
        if any(cue in lowered for cue in cues):
            return role
    return "assertion"


def analyse(sentence: str, offset: int) -> dict:
    """Facets found in one sentence, with offsets into the whole message."""
    facets = []
    rules = []

    date_value = ""
    for rule, pattern in DATE_PATTERNS:
        match = pattern.search(sentence)
        if match:
            date_value = match.group(0)
            rules.append("date:" + rule)
            facets.append({"facet_type": "date", "value_text": date_value,
                           "char_start": offset + match.start(),
                           "char_end": offset + match.end(), "extractor": "rule:" + rule})
            break

    number_raw = number_value = number_unit = None
    for match in NUMBER_PATTERN.finditer(sentence):
        raw = match.group(0)
        try:
            value = float(match.group("num").replace(",", ""))
        except ValueError:
            continue
        unit = match.group("unit").lower()
        if number_raw is None:
            number_raw, number_value, number_unit = raw, value, unit
            rules.append("number:measurement")
        facets.append({"facet_type": "number", "value_text": raw, "value_number": value,
                       "value_unit": unit, "char_start": offset + match.start(),
                       "char_end": offset + match.end(), "extractor": "rule:measurement"})

    location = ""
    cue = LOCATION_CUE.search(sentence)
    if cue:
        place = cue.group("place")
        if place.split()[0] not in _STOP_ENTITIES:
            location = place
            rules.append("location:preposition")
            facets.append({"facet_type": "location", "value_text": place,
                           "char_start": offset + cue.start("place"),
                           "char_end": offset + cue.end("place"),
                           "extractor": "rule:preposition_cue"})

    entity = ""
    for match in ENTITY_PATTERN.finditer(sentence):
        name = match.group(0).strip()
        first = name.split()[0]
        if first in _STOP_ENTITIES or len(name) < 3:
            continue
        if match.start() == 0 and " " not in name:
            continue  # a sentence-initial single word is usually not a name
        if not entity:
            entity = name
            rules.append("entity:capitalised_run")
        facets.append({"facet_type": "entity", "value_text": name,
                       "char_start": offset + match.start(),
                       "char_end": offset + match.end(),
                       "extractor": "rule:capitalised_run"})

    return {
        "event_date": normalise_date(date_value),
        "event_date_raw": date_value,
        "number_raw": number_raw or "",
        "number_value": number_value,
        "number_unit": number_unit or "",
        "location": location,
        "entity": entity,
        "rules": rules,
        "facets": facets,
    }


def find_candidates(archive, conversation_id: str, min_length: int = 25,
                    require_facets: bool = True) -> list:
    """Scan a conversation's current messages for extractable sentences."""
    rows = archive.all(
        "SELECT mv.version_id, mv.conversation_id, mv.content_text, mv.seq, mv.role, "
        "mv.create_time FROM conversation_version_messages cvm "
        "JOIN message_versions mv ON mv.version_id = cvm.message_version_id "
        "JOIN conversation_versions cv ON cv.version_id = cvm.conversation_version_id "
        "WHERE cv.conversation_id=? AND cv.is_current=1 ORDER BY cvm.seq",
        (conversation_id,),
    )
    candidates = []
    for row in rows:
        text = row["content_text"] or ""
        for sentence, start, end in split_sentences(text):
            if len(sentence) < min_length:
                continue
            found = analyse(sentence, start)
            if require_facets and not found["rules"]:
                continue
            candidates.append(
                Candidate(
                    message_version_id=row["version_id"],
                    conversation_id=row["conversation_id"],
                    quote=sentence,
                    char_start=start,
                    char_end=end,
                    event_date=found["event_date"] or (row["create_time"] or "")[:10],
                    entity=found["entity"],
                    location=found["location"],
                    number_raw=found["number_raw"],
                    number_value=found["number_value"],
                    number_unit=found["number_unit"],
                    probability_role=_probability_role(sentence),
                    rules=found["rules"],
                    facets=found["facets"],
                )
            )
    return candidates


def extract_conversation(archive, conversation_id: str, arc: str = "", dry_run: bool = True,
                         link_chain: bool = True, min_length: int = 25,
                         gate_token=None) -> dict:
    """Extract candidates, and (unless ``dry_run``) record them as propositions."""
    token = gate_token or authorise(archive, purpose="rule-based proposition extraction")
    candidates = find_candidates(archive, conversation_id, min_length=min_length)
    summary = {
        "conversation_id": conversation_id,
        "gate_run_id": token.run_id,
        "candidates": len(candidates),
        "recorded": 0,
        "dry_run": dry_run,
        "arc": arc,
        "items": [c.to_dict() for c in candidates],
    }
    if dry_run:
        return summary

    run_id = new_id("irun")
    archive.derived.execute(
        "INSERT INTO interpretation_runs(run_id, started_at, kind, method, gate_run_id, "
        "gate_fingerprint, params_json, notes) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, utcnow(), "extraction", "rule_based:v1", token.run_id, token.fingerprint,
         '{"conversation_id": "%s", "min_length": %d}' % (conversation_id, min_length),
         "deterministic sentence scan; meanings left for the researcher"),
    )
    archive.derived.commit()

    previous_id = None
    recorded = []
    for candidate in candidates:
        proposition_id = add_proposition(
            archive,
            candidate.message_version_id,
            candidate.quote,
            char_start=candidate.char_start,
            entity=candidate.entity or None,
            location=candidate.location or None,
            arc=arc or None,
            event_date=candidate.event_date or None,
            number_raw=candidate.number_raw or None,
            number_value=candidate.number_value,
            number_unit=candidate.number_unit or None,
            verification_status="quote_verified",
            probability_role=candidate.probability_role,
            created_by="rule_based:v1",
            method="sentence_scan(" + ",".join(candidate.rules) + ")",
            facets=candidate.facets,
            interpretation_run_id=run_id,
            gate_token=token,
        )
        recorded.append(proposition_id)
        if link_chain and previous_id and previous_id != proposition_id:
            add_edge(
                archive, "proposition", previous_id, "proposition", proposition_id,
                relation="prompted",
                rationale="adjacent statements in the same conversation, in order",
                created_by="rule_based:v1", interpretation_run_id=run_id, gate_token=token,
            )
        previous_id = proposition_id

    archive.derived.execute(
        "UPDATE interpretation_runs SET finished_at=?, produced_count=? WHERE run_id=?",
        (utcnow(), len(recorded), run_id),
    )
    archive.derived.commit()
    archive.log("archaeology.extract", {"conversation_id": conversation_id,
                                        "interpretation_run_id": run_id,
                                        "recorded": len(recorded),
                                        "gate_run_id": token.run_id})
    summary["recorded"] = len(recorded)
    summary["interpretation_run_id"] = run_id
    summary["proposition_ids"] = recorded
    return summary
