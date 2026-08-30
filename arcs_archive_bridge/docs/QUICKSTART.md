# Quickstart

From nothing to a verified archive of your own conversations. Everything here
runs on your machine and touches no network.

---

## 0. Get it onto your computer

The code lives on the `claude/arcs-archive-bridge-ojt2g9` branch:

```
git clone https://github.com/phienterprisesptyltd-ship-it/hostinger-email-generation-.git
cd hostinger-email-generation-
git checkout claude/arcs-archive-bridge-ojt2g9
cd arcs_archive_bridge
```

You need **Python 3.10 or newer** and nothing else:

```
python3 --version
./arcs --help
```

On Windows, use `python arcs` in place of `./arcs` throughout.

Optional, if you would rather type `arcs` than `./arcs` from anywhere:

```
pip install -e .
```

## 1. See it work before trusting it with your data

```
python3 samples/generate_samples.py
./arcs demo --out ./arcs-demo
```

This builds a throwaway archive from five invented conversations, then proves it
can hand every one of them back byte-for-byte. It touches nothing of yours.
Delete `./arcs-demo` afterwards.

If the last line says *"Everything checked out"*, the tool works on your
machine. If it does not, stop here and send me the output.

## 2. Get your conversations out of ChatGPT

In ChatGPT: **Settings → Data controls → Export data**. You will get an email
with a download link, usually within a few minutes. Download the zip and unzip
it. You should see a folder containing `conversations.json` and, if you have
ever attached anything, files named `file-<id>-<name>.<ext>` plus possibly a
`dalle-generations/` folder.

Keep the zip. The archive stores everything it needs, but there is no reason to
throw away the original.

> **On ChatGPT Business:** this export covers **your own** account's
> conversations. A workspace-wide export needs the OpenAI Enterprise Compliance
> API, which your workspace admin arranges. The bridge already reads that format
> (`--adapter compliance_api`) — see [ROADMAP.md](ROADMAP.md).

## 3. Create your archive

Pick somewhere it will live for years. Not a temp folder, not Downloads.

```
./arcs init ~/ARCS-Archive --name "ARCS Archive" --operator "Your Name"
```

So you do not have to type the path every time:

```
export ARCS_ARCHIVE=~/ARCS-Archive          # add this to ~/.zshrc or ~/.bashrc
```

Windows PowerShell: `$env:ARCS_ARCHIVE = "$HOME\ARCS-Archive"`

## 4. Ingest the export

Point it at **the folder**, not at `conversations.json` — that is how it picks
up your attachment files:

```
./arcs ingest ~/Downloads/chatgpt-export/
```

You will see one line per conversation and a summary. Anything the parser was
unsure about is printed as a warning rather than swallowed.

```
./arcs status          # counts, and whether anything is classified
./arcs list            # your conversations
./arcs files           # which attachments are held, which are references only
```

## 5. Verify — the habit that matters

```
./arcs verify
```

This re-checks every hash, every version chain, and rebuilds every conversation
by two independent routes. Run it **after every ingest** and every few months
regardless. It is the difference between having an archive and hoping you do.

A failing check names the rows it failed on. If you ever see one, send it to me.

## 6. Prove you can get the originals back

Do this once, now, while the export is still on disk to compare against:

```
./arcs recover --out ~/Desktop/arcs-recovery-test
```

Then compare a file against the original export:

```
diff <(python3 -m json.tool ~/Desktop/arcs-recovery-test/container-*.json) \
     <(python3 -m json.tool ~/Downloads/chatgpt-export/conversations.json) \
  && echo "identical"
```

`RECOVERY-MANIFEST.json` records the hash of everything it wrote and whether it
verified. Once you have seen this work, you can delete the recovery folder — the
point was to watch it happen, not to keep it.

## 7. Classify anything sensitive — do this early

Before you project, search or share anything, mark what must not travel:

```
./arcs list                                    # find the id (first 8 chars is enough)
./arcs classify conversation 2a63ffef Sacred \
    --reason "urupā location given in confidence by a kaumātua" --cascade
```

Classes, least to most restricted: `Normal`, `Private`, `Restricted`, `Sacred`.
Every change records who set it, when, and why. See
[SECURITY-CLASSES.md](SECURITY-CLASSES.md).

Use `--cascade` to apply the class to the conversation's messages too.

## 8. Read it

```
./arcs search "karaka stand"
./arcs show 81d5b497 --full
./arcs history 81d5b497            # versions, corrections, how often seen
```

For sustained reading and note-taking, generate the Obsidian vault:

```
./arcs project --max-class Private
```

Then in Obsidian: **Open folder as vault** →
`~/ARCS-Archive/projection/obsidian`. Anything above `--max-class` appears as a
stub recording that it exists and where it came from, but not its content.

The vault is regenerated, never edited: re-run `./arcs project` after any
change. Write your own notes in a *separate* vault and link across, or they will
be overwritten.

Optional semantic search, which finds paraphrases that literal search misses:

```
./arcs semantic index
./arcs semantic search "how far did the shoreline move"
```

## 9. Share with Grace or Grok

```
./arcs packet --recipient Grace --purpose "independent reading of the Kaiora material"
```

You get a folder and a matching zip under `~/ARCS-Archive/packets/`. It contains
the verbatim source, readable transcripts, the attachment files, and a manifest
listing anything withheld and why. Send the zip.

The recipient can check nothing was altered in transit:

```
sha256sum -c CHECKSUMS.sha256        # inside the unzipped packet
```

By default a packet carries `Normal` material only. To include more:

```
./arcs packet --recipient Grok --max-class Restricted \
    --acknowledge "Released for X, agreed with Y on <date>."
```

`Restricted` and `Sacred` **require** that acknowledgement; without it the build
refuses rather than quietly producing a partial packet that looks complete.

## 10. When you are ready to interpret

The archaeology layer is locked until the raw layer verifies:

```
./arcs gate                                      # open or closed, and why
./arcs arch extract 81d5b497 --arc "Kaiora boundary"       # dry run, shows candidates
./arcs arch extract 81d5b497 --arc "Kaiora boundary" --commit
```

Recording a proposition by hand — note that the two meanings stay in separate
fields:

```
./arcs show 81d5b497                             # copy a message_version_id
./arcs arch add <message_version_id> \
    --quote "Survey of the Kaiora Point reserve, commenced 14 March 1887" \
    --entity "Kaiora Point reserve" --date 1887-03-14 --arc "Kaiora boundary" \
    --contemporaneous "what 'reserve' meant under the 1878 regulations" \
    --later "read now as the earliest date the boundary was walked"
```

The quotation must appear verbatim in the message or the command refuses.

Recording what made you look, and drawing the chain:

```
./arcs arch observe --kind archive_read --query "1887 ledger page 14"
./arcs arch link --from-kind search_event --from-id <id> \
                 --to-kind proposition --to-id <id> --relation generated
./arcs arch chain <proposition_id>
./arcs arch graph --format dot --out graph.dot && dot -Tsvg -O graph.dot
```

Corrections append; they never overwrite:

```
./arcs arch correct <proposition_id> --reason "the 1898 succession order settles it" \
    --status corroborated
```

## 11. A working routine

**Every few months**, or whenever you have new material:

```
./arcs ingest ~/Downloads/chatgpt-export-<date>/    # a fresh export
./arcs verify
./arcs project
```

Re-ingesting is safe and expected. Identical material records a sighting, not a
duplicate. Changed material appends a version and keeps the old one. You cannot
lose anything by ingesting twice.

**Back it up like any other folder.** Copy `~/ARCS-Archive` to an external drive
or your backup service. Because files are content-addressed and read-only,
incremental backup is fast and safe. There is no separate backup command,
deliberately — the archive is just a directory.

## 12. If something looks wrong

| Symptom | What to do |
|---|---|
| `no ARCS archive at …` | You are pointing at the wrong place. Set `ARCS_ARCHIVE` or pass `--archive`. |
| `no adapter recognised …` | Pass `--adapter chatgpt_export` explicitly, or check you unzipped the export. |
| `refusing to ingest …: authentication material` | Working as intended. A capture bundle carried a cookie or token. Nothing was stored; see the report in `quarantine/`. |
| `interpretation is not authorised` | Run `./arcs verify`. The gate closes whenever the archive changes. |
| `arcs files` shows files not held | You ingested `conversations.json` alone, or from a capture. Ingest the export **folder**. |
| A `verify` check fails | Send me the output. Do not delete anything — the failure names the rows. |

```
./arcs log                # everything this archive has done, most recent first
```

## What is where

```
~/ARCS-Archive/
  archive.json                 your settings
  db/source.sqlite3            source records + normalised conversations
  db/derived.sqlite3           interpretations — separate on purpose
  source/blobs/                the exact original bytes, read-only
  projection/obsidian/         the vault (regenerable, safe to delete)
  packets/                     what you have shared, and with whom
  logs/integrity/              verification snapshots
  quarantine/                  refusal reports (never the refused file)
```

The two things that matter are `db/source.sqlite3` and `source/blobs/`.
Everything else can be rebuilt from them.
