# Sample material

Five invented ARCS-style conversations in the ChatGPT export format, plus a
UI-capture bundle and a deliberately bad bundle. Nothing here is anyone's real
data; the names, places and dates are fictional, chosen to exercise the parts of
the archive that matter:

| File | Exercises |
|---|---|
| `chatgpt_export/conversations.json` | five conversations: attachments, citations, an edited off-path branch, non-ASCII text, code |
| `chatgpt_export_v2/conversations.json` | a later capture: one conversation continued, one title corrected - versioning and corrections |
| `ui_capture/arcs-capture-bundle.json` | UI-assisted capture: one conversation with provider JSON, one DOM-only, plus a conversation the export does not contain |
| `bad_capture/leaky-bundle.json` | a bundle carrying a session cookie - ingestion must refuse it |

Regenerate them deterministically:

```
python3 samples/generate_samples.py
```

The generator is the specification of what the fixtures contain; read it rather
than the JSON.
