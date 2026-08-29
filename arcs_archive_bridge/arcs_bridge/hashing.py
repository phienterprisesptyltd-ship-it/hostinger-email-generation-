"""Hashing and canonical serialisation.

Two distinct notions of identity are used throughout the archive:

``blob hash``
    SHA-256 over the *exact bytes* of a source payload.  This is what makes a
    source record immutable and reconstructable.  Never computed over a
    re-serialised structure.

``content hash``
    SHA-256 over a canonical JSON encoding of a normalised structure (a message
    version, a proposition version).  Used for change detection and dedupe of
    derived rows, never as a substitute for the blob hash.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

HASH_NAME = "sha256"
_CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON encoding used for *content* hashes.

    Sorted keys, no insignificant whitespace, unicode preserved.  Two structures
    hash the same iff they are deep-equal as JSON values.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def merkle_root(hashes: Iterable[str]) -> str:
    """Order-independent digest over a set of hex digests.

    Used for manifests and for the archive fingerprint recorded by integrity
    runs.  Sorting first makes the value stable regardless of iteration order.
    """
    joined = "\n".join(sorted(hashes))
    return sha256_text(joined)


def verify(data: bytes, expected_sha256: str) -> bool:
    return hashlib.sha256(data).hexdigest() == expected_sha256.lower()
