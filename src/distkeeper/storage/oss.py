"""Alibaba Cloud OSS implementation of the generic storage contract."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import alibabacloud_oss_v2 as oss

from distkeeper.config import OssStorageConfig, normalize_prefix, validate_object_key
from distkeeper.errors import (
    ObjectAlreadyExistsError,
    ReleaseNotFoundError,
    SourceChangedError,
    StorageError,
)
from distkeeper.storage.base import ObjectInfo


class OssStorage:
    """OSS backend using SDK V2, including multipart upload and server-side copy."""

    def __init__(self, config: OssStorageConfig, *, client: Any | None = None) -> None:
        self._bucket = config.bucket
        self._prefix = normalize_prefix(config.prefix)
        self._part_size = config.upload_part_size_mib * 1024 * 1024
        self._parallelism = config.upload_parallelism
        if client is not None:
            self._client = client
            return
        try:
            credentials = oss.credentials.EnvironmentVariableCredentialsProvider()
            client_config = oss.Config(
                region=config.region,
                endpoint=config.endpoint,
                signature_version="v4",
                credentials_provider=credentials,
                use_internal_endpoint=config.use_internal_endpoint,
                use_cname=config.use_cname,
                user_agent="distkeeper/0.1.0",
            )
            self._client = oss.Client(client_config)
        except oss.exceptions.BaseError as exc:
            raise StorageError(f"failed to initialize OSS client: {exc}") from exc

    def stat(self, key: str) -> ObjectInfo | None:
        try:
            result = self._client.head_object(
                oss.HeadObjectRequest(bucket=self._bucket, key=self._physical_key(key))
            )
        except oss.exceptions.ServiceError as exc:
            if exc.status_code == 404:
                return None
            raise self._storage_error("stat", key, exc) from exc
        except oss.exceptions.BaseError as exc:
            raise self._storage_error("stat", key, exc) from exc
        metadata = {
            str(name).lower(): str(value) for name, value in (result.metadata or {}).items()
        }
        return ObjectInfo(
            key=key,
            size=int(result.content_length or 0),
            etag=result.etag,
            last_modified=result.last_modified,
            content_type=result.content_type,
            metadata=metadata,
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
        request = oss.PutObjectRequest(
            bucket=self._bucket,
            key=self._physical_key(key),
            content_type=content_type,
            metadata=dict(metadata),
            forbid_overwrite=not overwrite,
        )
        try:
            self._client.uploader(
                part_size=self._part_size,
                parallel_num=self._parallelism,
            ).upload_file(request, str(source))
        except oss.exceptions.BaseError as exc:
            self._raise_write_error("upload", key, exc)
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
        try:
            self._client.put_object(
                oss.PutObjectRequest(
                    bucket=self._bucket,
                    key=self._physical_key(key),
                    body=data,
                    content_length=len(data),
                    content_type=content_type,
                    metadata=dict(metadata),
                    forbid_overwrite=not overwrite,
                )
            )
        except oss.exceptions.BaseError as exc:
            self._raise_write_error("put", key, exc)
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
        request_args: dict[str, Any] = {
            "bucket": self._bucket,
            "key": self._physical_key(destination_key),
            "source_bucket": self._bucket,
            "source_key": self._physical_key(source_key),
            "forbid_overwrite": not overwrite,
            "if_match": source_etag,
        }
        if metadata is not None:
            request_args.update(
                metadata=dict(metadata),
                metadata_directive="REPLACE",
                content_type=content_type or "application/octet-stream",
            )
        try:
            self._client.copier(
                part_size=max(self._part_size, 100 * 1024),
                parallel_num=self._parallelism,
            ).copy(oss.CopyObjectRequest(**request_args))
        except oss.exceptions.BaseError as exc:
            service_error = self._find_service_error(exc)
            if service_error is not None and service_error.status_code == 404:
                raise ReleaseNotFoundError(f"storage object does not exist: {source_key}") from exc
            self._raise_write_error("copy", destination_key, exc)
        return self._required_stat(destination_key)

    def read_chunks(self, key: str, *, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
        try:
            result = self._client.get_object(
                oss.GetObjectRequest(bucket=self._bucket, key=self._physical_key(key))
            )
            if result.body is None:
                raise StorageError(f"OSS returned an empty response body for {key!r}")
            with result.body as body:
                yield from body.iter_bytes(chunk_size=chunk_size)
        except oss.exceptions.ServiceError as exc:
            if exc.status_code == 404:
                raise ReleaseNotFoundError(f"storage object does not exist: {key}") from exc
            raise self._storage_error("read", key, exc) from exc
        except oss.exceptions.BaseError as exc:
            raise self._storage_error("read", key, exc) from exc

    def list_objects(self, prefix: str) -> Iterable[ObjectInfo]:
        logical_prefix = self._validated_list_prefix(prefix)
        physical_prefix = self._physical_prefix(logical_prefix)
        continuation_token: str | None = None
        while True:
            try:
                result = self._client.list_objects_v2(
                    oss.ListObjectsV2Request(
                        bucket=self._bucket,
                        prefix=physical_prefix,
                        continuation_token=continuation_token,
                    )
                )
            except oss.exceptions.BaseError as exc:
                raise self._storage_error("list", logical_prefix, exc) from exc
            for item in result.contents or []:
                physical_key = str(item.key or "")
                key = self._logical_key(physical_key)
                yield ObjectInfo(
                    key=key,
                    size=int(item.size or 0),
                    etag=item.etag,
                    last_modified=item.last_modified,
                )
            if not result.is_truncated:
                break
            continuation_token = result.next_continuation_token
            if not continuation_token:
                raise StorageError("OSS list response was truncated without a continuation token")

    def _required_stat(self, key: str) -> ObjectInfo:
        info = self.stat(key)
        if info is None:
            raise StorageError(f"OSS write completed without object: {key}")
        return info

    def _physical_key(self, key: str) -> str:
        safe_key = validate_object_key(key)
        return f"{self._prefix}/{safe_key}" if self._prefix else safe_key

    def _physical_prefix(self, prefix: str) -> str:
        if self._prefix:
            return f"{self._prefix}/{prefix}" if prefix else f"{self._prefix}/"
        return prefix

    def _logical_key(self, physical_key: str) -> str:
        if not self._prefix:
            return physical_key
        expected = f"{self._prefix}/"
        if not physical_key.startswith(expected):
            raise StorageError(f"OSS returned an object outside configured prefix: {physical_key}")
        return physical_key[len(expected) :]

    @staticmethod
    def _validated_list_prefix(prefix: str) -> str:
        if not prefix:
            return ""
        trailing_slash = prefix.endswith("/")
        safe = validate_object_key(prefix.rstrip("/"), "list prefix")
        return f"{safe}/" if trailing_slash else safe

    @classmethod
    def _find_service_error(cls, error: BaseException) -> oss.exceptions.ServiceError | None:
        current: BaseException | None = error
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, oss.exceptions.ServiceError):
                return current
            unwrap = getattr(current, "unwrap", None)
            current = unwrap() if callable(unwrap) else current.__cause__
        return None

    @classmethod
    def _raise_write_error(cls, operation: str, key: str, error: BaseException) -> None:
        service_error = cls._find_service_error(error)
        if service_error is not None and service_error.status_code == 412:
            raise SourceChangedError(f"source object changed during {operation}: {key}") from error
        if service_error is not None and (
            service_error.status_code == 409 or service_error.code == "FileAlreadyExists"
        ):
            raise ObjectAlreadyExistsError(f"object already exists: {key}") from error
        raise cls._storage_error(operation, key, error) from error

    @staticmethod
    def _storage_error(operation: str, key: str, error: BaseException) -> StorageError:
        return StorageError(f"OSS {operation} failed for {key!r}: {error}")
