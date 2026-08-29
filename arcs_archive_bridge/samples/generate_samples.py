#!/usr/bin/env python3
"""Generate the prototype fixtures: five ChatGPT-export-shaped conversations.

The material is invented ARCS-style research so the archive can be exercised
end to end - dates, entities, locations, numbers, quotations, an edited branch,
attachments, citations, non-ASCII text - without touching anyone's real data.

Outputs (all deterministic):

* ``samples/chatgpt_export/conversations.json``      - first capture, 5 conversations
* ``samples/chatgpt_export_v2/conversations.json``   - later capture: one conversation
  continued, one title corrected (exercises versioning)
* ``samples/ui_capture/arcs-capture-bundle.json``    - UI-assisted capture bundle
* ``samples/bad_capture/leaky-bundle.json``          - a bundle carrying a session
  cookie, used to prove ingestion fails closed
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_TS = 1719_000_000.0  # 2024-06-21T21:20:00Z


def node(node_id, parent, children, message=None):
    return {"id": node_id, "message": message, "parent": parent, "children": list(children)}


def message(mid, role, text, ts, *, name=None, metadata=None, content_type="text",
            parts=None, status="finished_successfully", weight=1.0, recipient="all"):
    return {
        "id": mid,
        "author": {"role": role, "name": name, "metadata": {}},
        "create_time": ts,
        "update_time": None,
        "content": ({"content_type": content_type, "parts": parts if parts is not None else [text]}
                    if content_type != "code" else {"content_type": "code", "language": "python",
                                                    "text": text}),
        "status": status,
        "end_turn": role == "assistant",
        "weight": weight,
        "metadata": metadata or {},
        "recipient": recipient,
    }


def linear(conv_id, title, create, turns, *, extra_envelope=None, branch=None):
    """Build a conversation whose mapping is a straight chain, plus one branch."""
    mapping = {}
    root_id = "root-%s" % conv_id
    mapping[root_id] = node(root_id, None, [])
    parent = root_id
    ts = create
    last_id = root_id
    for i, (role, text, meta) in enumerate(turns, start=1):
        nid = "%s-n%02d" % (conv_id, i)
        ts = create + i * 47.0
        mapping[nid] = node(nid, parent, [], message(nid, role, text, ts, metadata=meta or {}))
        mapping[parent]["children"].append(nid)
        parent = nid
        last_id = nid
    if branch:
        # An edited user turn: a sibling of an existing node, off the current path.
        anchor, brole, btext = branch
        bid = "%s-branch" % conv_id
        mapping[bid] = node(bid, anchor, [],
                            message(bid, brole, btext, create + 20.0,
                                    metadata={"is_edited_branch": True}))
        mapping[anchor]["children"].append(bid)
    envelope = {
        "title": title,
        "create_time": create,
        "update_time": ts,
        "mapping": mapping,
        "moderation_results": [],
        "current_node": last_id,
        "plugin_ids": None,
        "conversation_id": conv_id,
        "conversation_template_id": None,
        "gizmo_id": None,
        "is_archived": False,
        "safe_urls": [],
        "default_model_slug": "gpt-4o",
        "id": conv_id,
    }
    envelope.update(extra_envelope or {})
    return envelope


C1 = linear(
    "arcs-0001-ledger",
    "ARCS: Kaiora Point survey ledger, 1887 entries",
    BASE_TS,
    [
        ("user",
         "I have a photograph of a ledger page from the Kaiora Point survey. The "
         "heading reads “Survey of the Kaiora Point reserve, commenced 14 March "
         "1887”. Below it there are three columns: chain, bearing, remarks. The "
         "first row says 12 chains, N 47° E, “to the karaka stand”. What "
         "would 'chains' mean here?",
         {"attachments": [{"id": "file-KP1887a", "name": "kaiora-ledger-p14.jpg",
                           "size": 2841204, "mime_type": "image/jpeg"}]}),
        ("assistant",
         "A chain is a surveyor's unit of 66 feet (20.1168 m), so 12 chains is 792 feet "
         "or about 241.4 metres. A bearing of N 47° E is measured clockwise from "
         "magnetic north unless the ledger states a true-north correction. “To the "
         "karaka stand” is a natural monument call: the line was run until it met a "
         "stand of karaka trees, which means the boundary was fixed to a living feature "
         "rather than a fixed mark.",
         {"model_slug": "gpt-4o"}),
        ("user",
         "The second row is harder to read. It looks like '8 ch 50 lk' and the remark is "
         "'to the old pa fence line'. Date in the margin is 16 March 1887.",
         {}),
        ("assistant",
         "8 chains 50 links is 8.5 chains, 561 feet, roughly 171.0 metres (100 links to "
         "the chain). The remark places the line at a pa fence — a fortified site "
         "boundary — which is significant: in 1887 surveyors frequently recorded pa "
         "earthworks as monuments precisely because they were visible and long-standing. "
         "If the ledger is the only surviving record of that fence line, it is evidence "
         "of the pa's extent at that date.",
         {"model_slug": "gpt-4o",
          "citations": [{"metadata": {"title": "Survey regulations 1878",
                                      "url": "https://example.invalid/survey-regs-1878"}}]}),
    ],
    branch=("arcs-0001-ledger-n02", "user",
            "The second row is hard to read — possibly '8 ch 50 lk'? [edited]"),
)

C2 = linear(
    "arcs-0002-namelist",
    "ARCS: name variants across the 1891 and 1896 rolls",
    BASE_TS + 86400,
    [
        ("user",
         "In the 1891 roll the name is written 'Te Rangi Whakaara'. In the 1896 roll the "
         "same holding number 214 is written 'Rangiwhakaara'. Are these the same person?",
         {}),
        ("assistant",
         "The holding number is the strongest link: 214 appearing in both rolls against a "
         "name that differs only by the honorific 'Te' and by word division is consistent "
         "with one person recorded by two clerks. Word division in Māori names was not "
         "standardised in nineteenth-century rolls. Treat it as the same person with "
         "moderate confidence, and look for a third source — a burial record or a "
         "succession order — before treating it as settled.",
         {"model_slug": "gpt-4o"}),
        ("user",
         "There is a succession order dated 3 August 1898 naming 'Rangi Whakaara of "
         "Kaiora' as successor to holding 214.",
         {}),
        ("assistant",
         "That closes it. A succession order is a court record tying a named individual "
         "to a specific holding, so holding 214 links 'Te Rangi Whakaara' (1891), "
         "'Rangiwhakaara' (1896) and 'Rangi Whakaara of Kaiora' (1898). The 1898 order "
         "also gives you a terminus: the original holder had died by 3 August 1898.",
         {"model_slug": "gpt-4o"}),
    ],
)

C3 = linear(
    "arcs-0003-coast",
    "ARCS: coastline change at the Kaiora estuary",
    BASE_TS + 2 * 86400,
    [
        ("user",
         "The 1887 survey puts the high-water mark 12 chains from the karaka stand. A 1963 "
         "aerial photo appears to put it about 180 m away. Has the coast moved?",
         {}),
        ("assistant",
         "12 chains is 241.4 m, so a 1963 distance of about 180 m implies roughly 61 m of "
         "landward movement over 76 years — about 0.8 m per year. That is plausible "
         "for an estuarine margin but the comparison carries two large uncertainties: the "
         "1887 'high-water mark' definition may be mean high water springs rather than the "
         "modern datum, and the karaka stand itself may not be in the same place. Do not "
         "treat 0.8 m/yr as a measurement; treat it as an upper bound pending a control "
         "point common to both sources.",
         {"model_slug": "gpt-4o"}),
        ("user", "What would count as a common control point?", {}),
        ("assistant",
         "A trig station, a surveyed road intersection, or a masonry structure visible in "
         "both the 1887 ledger and the 1963 photograph. The 1887 ledger's mention of the "
         "'old pa fence line' is not usable as control — it is a natural/earthwork "
         "feature that can itself erode.",
         {"model_slug": "gpt-4o"}),
    ],
)

C4 = linear(
    "arcs-0004-method",
    "ARCS: method note on provenance and quotation",
    BASE_TS + 3 * 86400,
    [
        ("user",
         "Draft me a rule for how I record quotations from primary sources in the ARCS "
         "archive.",
         {}),
        ("assistant",
         "Record four things and never collapse them: (1) the exact quotation, "
         "character-for-character, including original spelling; (2) what the words meant to "
         "the person who wrote them, at the date they wrote them; (3) what you now think "
         "they refer to; and (4) what prompted you to look. Keep (1) and (2) apart from (3) "
         "in storage, not just in presentation — if a later interpretation and a "
         "contemporaneous meaning live in the same field, the interpretation will "
         "eventually be read as the source.",
         {"model_slug": "gpt-4o"}),
        ("user", "Give me the snippet I'd use to hash a source record.", {}),
        ("assistant",
         "import hashlib\n\ndef source_hash(path):\n    h = hashlib.sha256()\n    with "
         "open(path, 'rb') as fh:\n        for chunk in iter(lambda: fh.read(1 << 20), b''):"
         "\n            h.update(chunk)\n    return h.hexdigest()",
         {"model_slug": "gpt-4o"}),
    ],
)

C5 = linear(
    "arcs-0005-restricted",
    "ARCS: wāhi tapu locations — handle with care",
    BASE_TS + 4 * 86400,
    [
        ("user",
         "I have been given the location of an urupā near the Kaiora estuary by a kaumātua, "
         "on the understanding that it is not published. How should the archive hold it?",
         {}),
        ("assistant",
         "Hold it under your most restricted class and make the restriction a property of "
         "the record rather than a note in it, so that any export path has to check the "
         "class before it can emit the record. Store the fact that a location was given, "
         "the date, and who granted it, separately from the coordinates themselves; that "
         "way you can cite the provenance of the knowledge without disclosing the place. "
         "An export that cannot express the restriction should refuse to run rather than "
         "emit a redacted version that looks complete.",
         {"model_slug": "gpt-4o"}),
    ],
)

CONVERSATIONS = [C1, C2, C3, C4, C5]


def _v2():
    """A later capture: conversation 2 continued, conversation 3 title corrected."""
    import copy

    convs = copy.deepcopy(CONVERSATIONS)
    c2 = convs[1]
    parent = c2["current_node"]
    for i, (role, text) in enumerate(
        [
            ("user", "Found the burial record: 'Rangi Whakaara, Kaiora, died 11 July 1898, "
                     "aged 63.' Does that change anything?"),
            ("assistant", "It fixes the death date at 11 July 1898, three weeks before the "
                          "succession order of 3 August 1898, and gives a birth year of about "
                          "1835. The 1891 and 1896 roll entries therefore both fall within "
                          "this person's lifetime, which removes the last reason to doubt "
                          "that they are one individual."),
        ],
        start=1,
    ):
        nid = "arcs-0002-namelist-n%02d" % (4 + i)
        c2["mapping"][nid] = node(nid, parent, [],
                                  message(nid, role, text, BASE_TS + 90000 + i * 60,
                                          metadata={"model_slug": "gpt-4o"} if role == "assistant" else {}))
        c2["mapping"][parent]["children"].append(nid)
        parent = nid
    c2["current_node"] = parent
    c2["update_time"] = BASE_TS + 90200

    convs[2]["title"] = "ARCS: coastline change at the Kaiora estuary (1887–1963)"
    convs[2]["update_time"] = BASE_TS + 90300
    return convs


def _ui_bundle():
    """UI-assisted capture: one provider-JSON conversation, one DOM-only."""
    return {
        "arcs_capture_version": 1,
        "capture_method": "ui_assisted_readonly",
        "script_version": "1.0.0",
        "source_system": "chatgpt",
        "captured_at": "2026-08-20T09:14:03.000Z",
        "note": (
            "Captured in a normally signed-in browser. GET requests only; no cookie, "
            "token or Authorization header was read, copied or stored."
        ),
        "conversations": [
            {
                "conversation_id": "arcs-0003-coast",
                "title": "ARCS: coastline change at the Kaiora estuary",
                "url": "https://chatgpt.com/c/arcs-0003-coast",
                "captured_at": "2026-08-20T09:14:03.000Z",
                "capture_method": "backend_json",
                "payload": C3,
            },
            {
                "conversation_id": "arcs-0006-fieldnote",
                "title": "ARCS: field note, Kaiora Point walkover",
                "url": "https://chatgpt.com/c/arcs-0006-fieldnote",
                "captured_at": "2026-08-20T09:15:41.000Z",
                "capture_method": "dom_scrape",
                "notes": "DOM capture only: this conversation would not load its backend JSON.",
                "messages": [
                    {"order": 1, "role": "user",
                     "text": "Walkover on 2 August 2026: the karaka stand is gone. Two stumps "
                             "remain at what I make to be 241 m from the ledger's start peg.",
                     "timestamp": "2026-08-02T04:11:00Z"},
                    {"order": 2, "role": "assistant",
                     "text": "Two stumps at 241 m is a close match for the ledger's 12 chains "
                             "(241.4 m), which supports the karaka stand as the monument the "
                             "1887 line actually ran to. Photograph both stumps with a scale "
                             "and record a GPS fix for each before they decay further.",
                     "timestamp": "2026-08-02T04:12:30Z"},
                ],
            },
        ],
    }


def _leaky_bundle():
    bundle = _ui_bundle()
    bundle["conversations"] = bundle["conversations"][:1]
    bundle["session"] = {"cookie": "__Secure-next-auth.session-token=eyJhbGciOiJIUzI1NiJ9.fake.fake"}
    return bundle


def write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main() -> None:
    outputs = [
        write(HERE / "chatgpt_export" / "conversations.json", CONVERSATIONS),
        write(HERE / "chatgpt_export_v2" / "conversations.json", _v2()),
        write(HERE / "ui_capture" / "arcs-capture-bundle.json", _ui_bundle()),
        write(HERE / "bad_capture" / "leaky-bundle.json", _leaky_bundle()),
    ]
    for path in outputs:
        print("%8d bytes  %s" % (path.stat().st_size, path.relative_to(HERE.parent)))


if __name__ == "__main__":
    main()
