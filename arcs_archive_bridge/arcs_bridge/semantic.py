"""Optional local semantic indexing.  No external APIs, ever.

Two backends:

``local-hashing`` (default, always available)
    A pure-Python hashed n-gram embedder: word unigrams and bigrams plus
    character 4-grams, hashed into a fixed number of dimensions with a signed
    projection, sublinear term weighting and L2 normalisation.  It is not a
    neural model; it is a deterministic, dependency-free vector space that finds
    paraphrase and morphological variants that literal FTS misses.  Being
    deterministic, an index built today can be reproduced byte-for-byte later.

``sentence-transformers`` (used only if the package is already installed)
    Loaded from a local model directory.  Never downloads: the archive process
    is network-sealed, so a missing model is an error, not a fetch.

Vectors live in the derived database.  They are an index, not a source record,
and can be deleted and rebuilt at any time without touching source material.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass

from .errors import ArcsError
from .gate import authorise
from .util import new_id, utcnow

DEFAULT_DIM = 512
_TOKEN = re.compile(r"[\w'’-]+", re.UNICODE)


# ------------------------------------------------------------------ backends
class LocalHashingEmbedder:
    name = "local-hashing-v1"
    kind = "local_hashing"

    def __init__(self, dim: int = DEFAULT_DIM):
        self.dim = dim

    def _features(self, text: str):
        tokens = [t.lower() for t in _TOKEN.findall(text or "")]
        for tok in tokens:
            yield "w:" + tok
            if len(tok) > 5:
                for i in range(len(tok) - 3):
                    yield "c:" + tok[i:i + 4]
        for a, b in zip(tokens, tokens[1:]):
            yield "b:%s_%s" % (a, b)

    def embed(self, text: str) -> list:
        counts: dict = {}
        for feature in self._features(text):
            counts[feature] = counts.get(feature, 0) + 1
        vector = [0.0] * self.dim
        for feature, count in counts.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


class SentenceTransformerEmbedder:  # pragma: no cover - optional dependency
    kind = "sentence_transformers"

    def __init__(self, model_path: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ArcsError(
                "sentence-transformers is not installed; use the local-hashing "
                "backend or install it into this environment"
            ) from exc
        import os

        if not os.path.isdir(model_path):
            raise ArcsError(
                "model path %r is not a local directory. The archive never "
                "downloads models: fetch it separately and pass the local path."
                % model_path
            )
        self.model = SentenceTransformer(model_path)
        self.name = "st:" + os.path.basename(model_path.rstrip("/"))
        self.dim = int(self.model.get_sentence_embedding_dimension())

    def embed(self, text: str) -> list:
        vector = self.model.encode(text or "", normalize_embeddings=True)
        return [float(v) for v in vector]


def get_embedder(backend: str = "local-hashing", model_path: str = "", dim: int = DEFAULT_DIM):
    if backend in ("local-hashing", "local", "default"):
        return LocalHashingEmbedder(dim=dim)
    if backend in ("sentence-transformers", "st"):
        if not model_path:
            raise ArcsError("--model-path is required for the sentence-transformers backend")
        return SentenceTransformerEmbedder(model_path)
    raise ArcsError("unknown embedding backend %r" % backend)


# ------------------------------------------------------------------- storage
def pack(vector) -> bytes:
    return struct.pack("<%df" % len(vector), *vector)


def unpack(blob: bytes) -> list:
    return list(struct.unpack("<%df" % (len(blob) // 4), blob))


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class IndexResult:
    model_name: str
    subject_type: str
    indexed: int
    skipped: int
    dim: int


def build_index(archive, subject_type: str = "message", backend: str = "local-hashing",
                model_path: str = "", dim: int = DEFAULT_DIM, rebuild: bool = False,
                gate_token=None) -> IndexResult:
    """Embed current message (or proposition) versions into the derived database."""
    token = gate_token or authorise(archive, purpose="semantic indexing")
    embedder = get_embedder(backend, model_path, dim)

    if subject_type == "message":
        rows = [
            (r["version_id"], r["content_text"], r["content_sha256"])
            for r in archive.all(
                "SELECT version_id, content_text, content_sha256 FROM message_versions "
                "WHERE is_current=1 ORDER BY conversation_id, seq"
            )
        ]
    elif subject_type == "proposition":
        rows = [
            (r["version_id"],
             " ".join(filter(None, [r["quote_text"], r["contemporaneous_meaning"]])),
             r["content_sha256"])
            for r in archive.all(
                "SELECT version_id, quote_text, contemporaneous_meaning, content_sha256 "
                "FROM proposition_versions WHERE is_current=1", conn=archive.derived
            )
        ]
    else:
        raise ArcsError("cannot index subject type %r" % subject_type)

    conn = archive.derived
    if rebuild:
        conn.execute(
            "DELETE FROM embeddings WHERE subject_type=? AND model_name=?",
            (subject_type, embedder.name),
        )
    existing = {
        r["subject_id"]: r["content_sha256"]
        for r in conn.execute(
            "SELECT subject_id, content_sha256 FROM embeddings WHERE subject_type=? "
            "AND model_name=?", (subject_type, embedder.name)
        )
    }

    indexed = skipped = 0
    for subject_id, text, content_sha in rows:
        if existing.get(subject_id) == content_sha:
            skipped += 1
            continue
        vector = embedder.embed(text or "")
        conn.execute(
            "INSERT INTO embeddings(embedding_id, subject_type, subject_id, model_name, "
            "model_kind, dim, vector, norm, content_sha256, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(subject_type, subject_id, model_name) DO UPDATE SET "
            "vector=excluded.vector, norm=excluded.norm, content_sha256=excluded.content_sha256, "
            "created_at=excluded.created_at",
            (new_id("emb"), subject_type, subject_id, embedder.name, embedder.kind,
             len(vector), pack(vector), 1.0, content_sha, utcnow()),
        )
        indexed += 1
    conn.commit()

    archive.log("semantic.index", {"subject_type": subject_type, "model": embedder.name,
                                   "indexed": indexed, "skipped": skipped,
                                   "gate_run_id": token.run_id})
    return IndexResult(embedder.name, subject_type, indexed, skipped, embedder.dim)


def semantic_search(archive, query: str, k: int = 10, subject_type: str = "message",
                    backend: str = "local-hashing", model_path: str = "",
                    dim: int = DEFAULT_DIM, max_class=None) -> list:
    """Nearest neighbours by cosine similarity, computed locally."""
    from .security import effective_class, permits

    embedder = get_embedder(backend, model_path, dim)
    query_vector = embedder.embed(query)

    rows = archive.all(
        "SELECT subject_id, vector FROM embeddings WHERE subject_type=? AND model_name=?",
        (subject_type, embedder.name), conn=archive.derived,
    )
    if not rows:
        raise ArcsError(
            "no %s embeddings for model %s; run `arcs semantic index` first"
            % (subject_type, embedder.name)
        )

    scored = []
    for row in rows:
        vector = unpack(row["vector"])
        if len(vector) != len(query_vector):
            continue
        scored.append((cosine(query_vector, vector), row["subject_id"]))
    scored.sort(reverse=True)

    out = []
    for score, subject_id in scored:
        if len(out) >= k:
            break
        if subject_type != "message":
            out.append({"score": score, "subject_id": subject_id})
            continue
        row = archive.one(
            "SELECT mv.version_id, mv.message_id, mv.conversation_id, mv.role, mv.seq, "
            "mv.create_time, mv.content_text, m.security_class AS message_class, "
            "c.security_class AS conversation_class, cv.title "
            "FROM message_versions mv "
            "JOIN messages m ON m.message_id = mv.message_id "
            "JOIN conversations c ON c.conversation_id = mv.conversation_id "
            "LEFT JOIN conversation_versions cv ON cv.conversation_id = c.conversation_id "
            "AND cv.is_current=1 WHERE mv.version_id=?",
            (subject_id,),
        )
        if row is None:
            continue
        cls = effective_class(row["conversation_class"], row["message_class"])
        if max_class is not None and not permits(max_class, cls):
            continue
        out.append(
            {
                "score": score,
                "version_id": row["version_id"],
                "message_id": row["message_id"],
                "conversation_id": row["conversation_id"],
                "title": row["title"] or "",
                "role": row["role"],
                "seq": row["seq"],
                "created": row["create_time"] or "",
                "security_class": cls,
                "text": row["content_text"],
            }
        )
    return out
