"""Application-specific errors."""


class DistkeeperError(Exception):
    """Base class for errors that can be shown directly to CLI users."""

    code = "DISTKEEPER_ERROR"
    retryable = False


class ConfigError(DistkeeperError):
    """The configuration is missing or invalid."""

    code = "CONFIG_ERROR"


class SafetyError(DistkeeperError):
    """A requested mutation falls outside the configured safety boundaries."""

    code = "SAFETY_SCOPE"


class ReleaseNotFoundError(DistkeeperError):
    """A requested artifact or release does not exist."""

    code = "RELEASE_NOT_FOUND"


class ConflictError(DistkeeperError):
    """An immutable release conflicts with existing content."""

    code = "RELEASE_CONFLICT"


class IntegrityError(DistkeeperError):
    """Artifact content does not match its release manifest."""

    code = "INTEGRITY_ERROR"


class StorageError(DistkeeperError):
    """The storage backend failed to perform an operation."""

    code = "STORAGE_ERROR"
    retryable = True


class ObjectAlreadyExistsError(StorageError):
    """A write with overwrite protection targeted an existing object."""

    code = "OBJECT_ALREADY_EXISTS"
    retryable = False


class SourceChangedError(StorageError):
    """A conditional copy detected that its source changed."""

    code = "SOURCE_CHANGED"
    retryable = False
