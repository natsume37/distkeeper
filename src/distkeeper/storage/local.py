"""Filesystem implementation of the generic storage contract."""

from __future__ import annotations

import mimetypes
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from distkeeper.config import normalize_prefix, validate_object_key
from distkeeper.errors import (
    ObjectAlreadyExistsError,
    ReleaseNotFoundError,
    SourceChangedError,
    StorageError,
)
from distkeeper.storage.base import ObjectInfo


class LocalStorage:
    """Object-store semantics backed by a directory, primarily for local use and tests."""

    def __init__(self, root: Path, prefix: str = "") -> None:
        self._root = root.expanduser().resolve()
        self._prefix = normalize_prefix(prefix)

    def stat(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        stat = path.stat()
        return ObjectInfo(
            key=key,
            size=stat.st_size,
            etag=f"{stat.st_mtime_ns:x}-{stat.st_size:x}",
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            content_type=mimetypes.guess_type(path.name)[0],
        )

    def upload_file(
        self,
        key: str,
        source: Path,
        *,
        overwrite: bool,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> ObjectInfo:
        del content_type, metadata
        if not source.is_file():
            raise ReleaseNotFoundError(f"local artifact does not exist: {source}")
        self._atomic_write(
            self._path(key),
            overwrite=overwrite,
            writer=lambda output: self._copy_file(source, output),
        )
        return self._required_stat(key)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        overwrite: bool,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> ObjectInfo:
        del content_type, metadata
        self._atomic_write(
            self._path(key), overwrite=overwrite, writer=lambda output: output.write(data)
        )
        return self._required_stat(key)

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        overwrite: bool,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        source_etag: str | None = None,
    ) -> ObjectInfo:
        del content_type, metadata
        source = self._path(source_key)
        if not source.is_file():
            raise ReleaseNotFoundError(f"storage object does not exist: {source_key}")

        def copy_stable_source(output: BinaryIO) -> int:
            before = self._required_stat(source_key)
            if source_etag is not None and before.etag != source_etag:
                raise SourceChangedError(f"source object changed before copy: {source_key}")
            copied = self._copy_file(source, output)
            after = self._required_stat(source_key)
            if source_etag is not None and after.etag != source_etag:
                raise SourceChangedError(f"source object changed during copy: {source_key}")
            return copied

        self._atomic_write(
            self._path(destination_key),
            overwrite=overwrite,
            writer=copy_stable_source,
        )
        return self._required_stat(destination_key)

    def read_chunks(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
        path = self._path(key)
        if not path.is_file():
            raise ReleaseNotFoundError(f"storage object does not exist: {key}")
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(chunk_size):
                    yield chunk
        except OSError as exc:
            raise StorageError(f"failed to read local object {key!r}: {exc}") from exc

    def list_objects(self, prefix: str) -> Iterable[ObjectInfo]:
        logical_prefix = prefix
        storage_root = self._storage_root()
        if not storage_root.exists():
            return
        for path in sorted(storage_root.rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(storage_root).as_posix()
            if key.startswith(logical_prefix):
                info = self.stat(key)
                if info is not None:
                    yield info

    def _storage_root(self) -> Path:
        return self._root / self._prefix if self._prefix else self._root

    def _path(self, key: str) -> Path:
        safe_key = validate_object_key(key)
        storage_root = self._storage_root().resolve()
        path = (storage_root / safe_key).resolve()
        # 即使模板校验被绕过，也不能让本地驱动逃逸到配置根目录之外。
        if not path.is_relative_to(storage_root):
            raise StorageError(f"object key escapes local storage root: {key!r}")
        return path

    def _required_stat(self, key: str) -> ObjectInfo:
        info = self.stat(key)
        if info is None:
            raise StorageError(f"local write completed without object: {key}")
        return info

    @staticmethod
    def _copy_file(source: Path, output: BinaryIO) -> int:
        with source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output, length=1024 * 1024)
        return source.stat().st_size

    @staticmethod
    def _atomic_write(
        destination: Path,
        *,
        overwrite: bool,
        writer: Callable[[BinaryIO], object],
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
            )
            temporary_path = Path(raw_path)
            with os.fdopen(descriptor, "wb") as output:
                writer(output)
                output.flush()
                os.fsync(output.fileno())
            if overwrite:
                os.replace(temporary_path, destination)
            else:
                try:
                    # hard link 提供原子的 create-if-absent 语义，避免并发覆盖不可变版本。
                    os.link(temporary_path, destination)
                except FileExistsError as exc:
                    raise ObjectAlreadyExistsError(
                        f"object already exists: {destination.name}"
                    ) from exc
                temporary_path.unlink()
            temporary_path = None
        except ObjectAlreadyExistsError:
            raise
        except OSError as exc:
            raise StorageError(f"failed to write local object {destination}: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
