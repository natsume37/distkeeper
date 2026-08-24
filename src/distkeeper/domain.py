"""Storage-independent domain models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

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
