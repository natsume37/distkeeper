"""Storage-independent domain models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class ArtifactRecord(BaseModel):
    """The immutable artifact data recorded for a release."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    latest_key: str
    filename: str
    extension: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_type: str


class ReleaseManifest(BaseModel):
    """Portable metadata stored alongside every versioned artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    repository: str
    target: str
    channel: str
    version: str
    origin: Literal["publish", "adopt"]
    activation: Literal["publish", "adopt", "rollback"]
    published_at: datetime
    activated_at: datetime
    artifact: ArtifactRecord

    def to_json_bytes(self) -> bytes:
        """Serialize deterministically so retries do not produce noisy manifests."""
        data = self.model_dump(mode="json")
        return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()

    @classmethod
    def from_json_bytes(cls, data: bytes) -> ReleaseManifest:
        return cls.model_validate_json(data)


class ReleaseStatus(BaseModel):
    """Machine-readable release state for one repository target and channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    repository: str
    target: str
    channel: str
    state: Literal["empty", "ready", "degraded"]
    current: ReleaseManifest | None
    previous: ReleaseManifest | None
    available_versions: tuple[str, ...]
    current_versioned_exists: bool | None
    current_latest_exists: bool | None


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    """All object keys involved in one release."""

    versioned: str
    latest: str
    version_manifest: str
    latest_manifest: str


@dataclass(frozen=True, slots=True)
class PreparedArtifact:
    """Validated local input plus its resolved release metadata."""

    source: Path
    repository: str
    target: str
    channel: str
    version: str
    filename: str
    extension: str
    content_type: str
    size: int
    sha256: str
    source_fingerprint: tuple[int, int, int, int]
    paths: ArtifactPaths


@dataclass(frozen=True, slots=True)
class OperationPlan:
    """A read-only description of mutations an operation would perform."""

    operation: str
    repository: str
    target: str
    version: str
    actions: tuple[str, ...]
    scope_violations: tuple[str, ...] = ()
    expected_current_version: str | None = None
    expected_current_etag: str | None = None
    source_sha256: str | None = None
    source_size: int | None = None
    plan_id: str = ""

    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        """Derive a stable identifier so an AI can apply the exact plan it inspected."""
        if self.plan_id:
            return
        payload = {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "repository": self.repository,
            "target": self.target,
            "version": self.version,
            "actions": self.actions,
            "scope_violations": self.scope_violations,
            "expected_current_version": self.expected_current_version,
            "expected_current_etag": self.expected_current_etag,
            "source_sha256": self.source_sha256,
            "source_size": self.source_size,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        object.__setattr__(self, "plan_id", f"plan_{digest[:24]}")

    @property
    def requires_confirmation(self) -> bool:
        """Whether the plan needs an explicit out-of-scope confirmation."""
        return bool(self.scope_violations)


@dataclass(frozen=True, slots=True)
class VerificationItem:
    key: str
    exists: bool
    size_matches: bool
    sha256_matches: bool | None

    @property
    def ok(self) -> bool:
        return self.exists and self.size_matches and self.sha256_matches is not False


@dataclass(frozen=True, slots=True)
class VerificationReport:
    manifest: ReleaseManifest
    items: tuple[VerificationItem, ...]

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.items)
