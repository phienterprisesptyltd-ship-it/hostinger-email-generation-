"""SQLite access for the two archive databases.

The source database and the derived database are separate *files* on purpose:
"keep derived AI interpretations separate from source records" is enforced by
the filesystem, not by a naming convention.  When a query genuinely needs both
(the Obsidian projection, evidence packets), the derived database is ATTACHed
as schema ``derived``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import ArchiveConfig
from .util import new_id, utcnow

_HERE = Path(__file__).resolve().parent
SOURCE_SCHEMA = _HERE / "schema_source.sql"
DERIVED_SCHEMA = _HERE / "schema_derived.sql"


def _connect(path: Path, read_only: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


class Archive:
    """Handle onto one archive on disk."""

    def __init__(self, config: ArchiveConfig):
        self.config = config
        self._source = None
        self._derived = None

    @classmethod
    def open(cls, config: ArchiveConfig) -> "Archive":
        archive = cls(config)
        config.ensure_dirs()
        archive.init_schema()
        return archive

    @property
    def source(self) -> sqlite3.Connection:
        if self._source is None:
            self._source = _connect(self.config.source_db_path)
        return self._source

    @property
    def derived(self) -> sqlite3.Connection:
        if self._derived is None:
            self._derived = _connect(self.config.derived_db_path)
        return self._derived

    def init_schema(self) -> None:
        self.source.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
        self.derived.executescript(DERIVED_SCHEMA.read_text(encoding="utf-8"))
        self.set_meta("schema_version", str(self.config.schema_version))
        self.set_meta("archive_name", self.config.archive_name)
        self.source.commit()
        self.derived.commit()

    def close(self) -> None:
        for conn in (self._source, self._derived):
            if conn is not None:
                conn.close()
        self._source = self._derived = None

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self, conn=None):
        conn = conn or self.source
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    @contextmanager
    def with_derived_attached(self):
        """Run a query across both databases."""
        self.source.execute("ATTACH DATABASE ? AS derived", (str(self.config.derived_db_path),))
        try:
            yield self.source
        finally:
            self.source.execute("DETACH DATABASE derived")

    def set_meta(self, key: str, value: str) -> None:
        self.source.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, utcnow()),
        )

    def get_meta(self, key: str, default=None):
        row = self.source.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def log(self, operation: str, detail=None, actor: str = "", network_unsealed: bool = False) -> str:
        op_id = new_id("op")
        self.source.execute(
            "INSERT INTO operation_log(op_id, at, operation, actor, detail_json, network_unsealed) "
            "VALUES (?,?,?,?,?,?)",
            (
                op_id,
                utcnow(),
                operation,
                actor or self.config.operator,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                1 if network_unsealed else 0,
            ),
        )
        self.source.commit()
        return op_id

    def one(self, sql: str, params=(), conn=None):
        return (conn or self.source).execute(sql, params).fetchone()

    def all(self, sql: str, params=(), conn=None):
        return (conn or self.source).execute(sql, params).fetchall()

    def scalar(self, sql: str, params=(), conn=None):
        row = (conn or self.source).execute(sql, params).fetchone()
        return None if row is None else row[0]


def open_archive(config: ArchiveConfig) -> Archive:
    return Archive.open(config)
