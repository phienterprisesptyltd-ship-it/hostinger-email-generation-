"""Local configuration for an ARCS archive.

Everything is a path on the local filesystem.  There is no server, no account
and no remote endpoint anywhere in this module by design.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .errors import ConfigError

DEFAULT_ARCHIVE_DIRNAME = "arcs-archive"
CONFIG_FILENAME = "archive.json"

#: Name of the environment variable that points at an archive root.
ENV_ARCHIVE = "ARCS_ARCHIVE"

#: Set to "1" to permit outbound sockets for an explicitly requested upload.
ENV_ALLOW_NETWORK = "ARCS_ALLOW_NETWORK"


@dataclass
class ArchiveConfig:
    """Paths and policy for one archive on disk."""

    root: Path
    archive_name: str = "ARCS Archive"
    operator: str = ""
    #: Security classes, most permissive first.  Order is meaningful.
    security_classes: tuple = ("Normal", "Private", "Restricted", "Sacred")
    #: Classes that may never leave the archive in an evidence packet without
    #: an explicit, recorded acknowledgement.
    default_packet_max_class: str = "Normal"
    created_at: str = ""
    schema_version: int = 1
    extras: dict = field(default_factory=dict)

    # ---- derived paths -------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def source_db_path(self) -> Path:
        """Immutable source + normalised conversation database."""
        return self.root / "db" / "source.sqlite3"

    @property
    def derived_db_path(self) -> Path:
        """Derived interpretations.  A physically separate file."""
        return self.root / "db" / "derived.sqlite3"

    @property
    def blobs_dir(self) -> Path:
        """Content-addressed store of raw source payloads."""
        return self.root / "source" / "blobs"

    @property
    def incoming_dir(self) -> Path:
        """Where capture bundles / exports are dropped before ingestion."""
        return self.root / "source" / "incoming"

    @property
    def obsidian_dir(self) -> Path:
        """Markdown projection (derived; regenerable)."""
        return self.root / "projection" / "obsidian"

    @property
    def packets_dir(self) -> Path:
        return self.root / "packets"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def quarantine_dir(self) -> Path:
        """Inputs refused because they contained credential material."""
        return self.root / "quarantine"

    def all_dirs(self):
        return [
            self.root,
            self.root / "db",
            self.blobs_dir,
            self.incoming_dir,
            self.obsidian_dir,
            self.packets_dir,
            self.logs_dir,
            self.quarantine_dir,
        ]

    # ---- persistence ---------------------------------------------------
    def to_json(self) -> str:
        data = asdict(self)
        data["root"] = str(self.root)
        data["security_classes"] = list(self.security_classes)
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, root: Path) -> "ArchiveConfig":
        root = Path(root).expanduser().resolve()
        path = root / CONFIG_FILENAME
        if not path.exists():
            raise ConfigError(
                f"no ARCS archive at {root} (missing {CONFIG_FILENAME}); "
                f"run `arcs init {root}` first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["root"] = Path(raw["root"]) if raw.get("root") else root
        raw["security_classes"] = tuple(raw.get("security_classes") or ())
        return cls(**raw)

    def ensure_dirs(self) -> None:
        for d in self.all_dirs():
            d.mkdir(parents=True, exist_ok=True)


def resolve_root(explicit: str | os.PathLike | None = None) -> Path:
    """Find the archive root: explicit argument, env var, then ./arcs-archive."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(ENV_ARCHIVE)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / DEFAULT_ARCHIVE_DIRNAME).resolve()


def load_config(explicit: str | os.PathLike | None = None) -> ArchiveConfig:
    return ArchiveConfig.load(resolve_root(explicit))
