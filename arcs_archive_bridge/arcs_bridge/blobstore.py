"""Content-addressed store for raw source payloads.

Every byte the archive ever ingests lands here first, keyed by its SHA-256.
Nothing in the store is ever rewritten: the same key always yields the same
bytes, which is what makes reconstruction provable rather than asserted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import IntegrityError, NotFound
from .hashing import sha256_bytes, verify
from .util import utcnow


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    byte_length: int
    media_type: str
    relpath: str
    created: bool  # False when the identical bytes were already present


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _relpath(self, digest: str) -> str:
        return os.path.join(digest[:2], digest[2:4], digest + ".bin")

    def path_for(self, digest: str) -> Path:
        return self.root / self._relpath(digest)

    def put(self, data: bytes, media_type: str = "application/octet-stream") -> StoredBlob:
        digest = sha256_bytes(data)
        path = self.path_for(digest)
        created = False
        if path.exists():
            existing = path.read_bytes()
            if existing != data:  # pragma: no cover - would mean a SHA-256 collision
                raise IntegrityError(
                    "blob %s already exists with different content" % digest
                )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            try:
                path.chmod(0o444)  # source material is read-only on disk
            except OSError:  # pragma: no cover - platform dependent
                pass
            created = True
        return StoredBlob(digest, len(data), media_type, self._relpath(digest), created)

    def get(self, digest: str) -> bytes:
        path = self.path_for(digest)
        if not path.exists():
            raise NotFound("no blob %s in %s" % (digest, self.root))
        data = path.read_bytes()
        if not verify(data, digest):
            raise IntegrityError("blob %s failed its hash check on read" % digest)
        return data

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    def verify_all(self):
        """Yield (digest, ok) for every blob on disk."""
        for path in sorted(self.root.rglob("*.bin")):
            digest = path.stem
            yield digest, verify(path.read_bytes(), digest)

    def register(self, conn, blob: StoredBlob) -> None:
        """Record the blob in the source database (idempotent)."""
        conn.execute(
            "INSERT INTO source_blobs(sha256, byte_length, media_type, storage, content, relpath, first_seen_at) "
            "VALUES (?,?,?,'file',NULL,?,?) ON CONFLICT(sha256) DO NOTHING",
            (blob.sha256, blob.byte_length, blob.media_type, blob.relpath, utcnow()),
        )
