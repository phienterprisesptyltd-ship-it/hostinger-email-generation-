"""Exception hierarchy for ARCS Archive Bridge."""


class ArcsError(Exception):
    """Base class for every error raised by the bridge."""


class ConfigError(ArcsError):
    """Invalid or unusable configuration."""


class AdapterError(ArcsError):
    """An import adapter could not handle the input it was given."""


class IntegrityError(ArcsError):
    """An archive invariant was violated."""


class ReconstructionError(IntegrityError):
    """A source record could not be reconstructed byte-for-byte."""


class CredentialMaterialFound(ArcsError):
    """Input contained authentication material; ingestion was refused."""


class NetworkBlocked(ArcsError):
    """Outbound network access was attempted while the archive is sealed."""


class InterpretationGateError(ArcsError):
    """Automated interpretation was attempted before the raw layer verified."""


class SecurityClassViolation(ArcsError):
    """An operation would expose material above the permitted security class."""


class NotFound(ArcsError):
    """A requested archive object does not exist."""
