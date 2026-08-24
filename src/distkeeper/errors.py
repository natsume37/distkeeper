"""Application-specific errors."""


class DistkeeperError(Exception):
    """Base class for errors that can be shown directly to CLI users."""


class ConfigError(DistkeeperError):
    """The configuration is missing or invalid."""


class ReleaseNotFoundError(DistkeeperError):
    """A requested artifact or release does not exist."""


class ConflictError(DistkeeperError):
    """An immutable release conflicts with existing content."""


class IntegrityError(DistkeeperError):
    """Artifact content does not match its release manifest."""


class StorageError(DistkeeperError):
    """The storage backend failed to perform an operation."""


class ObjectAlreadyExistsError(StorageError):
    """A write with overwrite protection targeted an existing object."""


class SourceChangedError(StorageError):
    """A conditional copy detected that its source changed."""
