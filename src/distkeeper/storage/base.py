"""Generic object storage contract used by release services."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    etag: str | None = None
    last_modified: datetime | None = None
    content_type: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


class Storage(Protocol):
    """Minimal capabilities required by the release state machine."""

    def stat(self, key: str) -> ObjectInfo | None: ...

    def upload_file(
        self,
        key: str,
        source: Path,
        *,
        overwrite: bool,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> ObjectInfo: ...

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        overwrite: bool,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> ObjectInfo: ...

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        overwrite: bool,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        source_etag: str | None = None,
    ) -> ObjectInfo: ...

    def read_chunks(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterable[bytes]: ...

    def list_objects(self, prefix: str) -> Iterable[ObjectInfo]: ...


def read_object(storage: Storage, key: str, *, max_bytes: int = 1024 * 1024) -> bytes:
    """Read a small control object while enforcing a defensive size limit."""
    data = bytearray()
    for chunk in storage.read_chunks(key):
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError(f"object {key!r} exceeds the {max_bytes}-byte control-object limit")
    return bytes(data)
