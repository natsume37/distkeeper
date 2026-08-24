"""Read-only directory tree queries over flat object storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from distkeeper.config import validate_object_key
from distkeeper.errors import ConfigError, StorageError
from distkeeper.storage.base import ObjectInfo, Storage


class DirectoryNode(BaseModel):
    """A synthetic directory, an object, or an object that also has children."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    key: str
    kind: Literal["directory", "object", "object_and_directory"]
    size: int | None = Field(default=None, ge=0)
    last_modified: datetime | None = None
    children: tuple[DirectoryNode, ...] = ()


class DirectoryListing(BaseModel):
    """Bounded result returned by a directory structure query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prefix: str
    objects_returned: int = Field(ge=0)
    truncated: bool
    nodes: tuple[DirectoryNode, ...]


@dataclass(slots=True)
class _MutableNode:
    name: str
    key: str
    object_info: ObjectInfo | None = None
    children: dict[str, _MutableNode] = field(default_factory=dict)

    @property
    def is_directory(self) -> bool:
        return self.object_info is None or bool(self.children)

    def freeze(self) -> DirectoryNode:
        children = tuple(
            child.freeze()
            for child in sorted(
                self.children.values(),
                key=lambda item: (not item.is_directory, item.name.casefold(), item.name),
            )
        )
        if self.object_info is not None and children:
            kind = "object_and_directory"
        elif self.object_info is not None:
            kind = "object"
        else:
            kind = "directory"
        return DirectoryNode(
            name=self.name,
            key=self.key,
            kind=kind,
            size=self.object_info.size if self.object_info is not None else None,
            last_modified=(
                self.object_info.last_modified if self.object_info is not None else None
            ),
            children=children,
        )


class DirectoryService:
    """Builds hierarchical views without depending on a specific storage provider."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def query(self, prefix: str = "", *, limit: int = 1000) -> DirectoryListing:
        if not 1 <= limit <= 100_000:
            raise ConfigError("directory query limit must be between 1 and 100000")
        normalized_prefix = self._normalize_prefix(prefix)
        roots: dict[str, _MutableNode] = {}
        objects_returned = 0
        truncated = False

        for info in self._storage.list_objects(normalized_prefix):
            if objects_returned >= limit:
                truncated = True
                break
            self._insert(roots, normalized_prefix, info)
            objects_returned += 1

        nodes = tuple(
            node.freeze()
            for node in sorted(
                roots.values(),
                key=lambda item: (not item.is_directory, item.name.casefold(), item.name),
            )
        )
        return DirectoryListing(
            prefix=normalized_prefix,
            objects_returned=objects_returned,
            truncated=truncated,
            nodes=nodes,
        )

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        normalized = prefix.strip("/")
        if not normalized:
            return ""
        validate_object_key(normalized, "directory prefix")
        return f"{normalized}/"

    @staticmethod
    def _insert(
        roots: dict[str, _MutableNode],
        prefix: str,
        info: ObjectInfo,
    ) -> None:
        if not info.key.startswith(prefix):
            raise StorageError(
                f"storage returned object {info.key!r} outside query prefix {prefix!r}"
            )
        relative_key = info.key[len(prefix) :]
        directory_marker = relative_key.endswith("/")
        relative_key = relative_key.rstrip("/")
        if not relative_key:
            return
        parts = relative_key.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise StorageError(f"storage returned an unsafe object key: {info.key!r}")

        children = roots
        accumulated = prefix.rstrip("/")
        current: _MutableNode | None = None
        for part in parts:
            accumulated = f"{accumulated}/{part}" if accumulated else part
            current = children.setdefault(part, _MutableNode(name=part, key=accumulated))
            children = current.children
        if current is not None and not directory_marker:
            current.object_info = info
