from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import alibabacloud_oss_v2 as oss
import pytest

from distkeeper.config import OssStorageConfig
from distkeeper.errors import SourceChangedError
from distkeeper.storage.local import LocalStorage
from distkeeper.storage.oss import OssStorage


def test_local_conditional_copy_does_not_publish_changed_source(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")

    with pytest.raises(SourceChangedError, match="changed before copy"):
        storage.copy(
            "source.bin",
            "destination.bin",
            overwrite=False,
            source_etag="stale-etag",
        )

    assert not (tmp_path / "destination.bin").exists()


class FakeUploader:
    def __init__(self, client: FakeOssClient) -> None:
        self._client = client

    def upload_file(self, request: Any, filepath: str) -> None:
        self._client.upload_request = request
        self._client.size = Path(filepath).stat().st_size
        self._client.metadata = request.metadata


class FakeCopier:
    def __init__(self, client: FakeOssClient) -> None:
        self._client = client

    def copy(self, request: Any) -> None:
        self._client.copy_request = request


class FakeOssClient:
    def __init__(self) -> None:
        self.size = 0
        self.metadata: dict[str, str] = {}
        self.upload_request: Any = None
        self.copy_request: Any = None

    def uploader(self, **kwargs: Any) -> FakeUploader:
        del kwargs
        return FakeUploader(self)

    def copier(self, **kwargs: Any) -> FakeCopier:
        del kwargs
        return FakeCopier(self)

    def head_object(self, request: Any) -> SimpleNamespace:
        del request
        return SimpleNamespace(
            content_length=self.size,
            etag="etag-current",
            last_modified=None,
            content_type="application/octet-stream",
            metadata=self.metadata,
        )

    def list_objects_v2(self, request: Any) -> SimpleNamespace:
        assert request.prefix == "products/manifests/"
        return SimpleNamespace(
            contents=[
                SimpleNamespace(
                    key="products/manifests/1.0.0.json",
                    size=123,
                    etag="etag-list",
                    last_modified=None,
                )
            ],
            is_truncated=False,
            next_continuation_token=None,
        )


class MissingObjectOssClient(FakeOssClient):
    def head_object(self, request: Any) -> SimpleNamespace:
        del request
        service_error = oss.exceptions.ServiceError(
            status_code=404,
            code="NoSuchKey",
            request_id="test-request",
            message="missing",
            ec="test",
            timestamp="now",
            request_target="HEAD /missing",
        )
        raise oss.exceptions.ResponseError(error=service_error)


def test_oss_stat_treats_wrapped_not_found_as_missing() -> None:
    config = OssStorageConfig(driver="oss", bucket="example-bucket", region="cn-chengdu")
    storage = OssStorage(config, client=MissingObjectOssClient())

    assert storage.stat("missing/object.apk") is None


def test_oss_driver_applies_prefix_metadata_and_copy_precondition(tmp_path: Path) -> None:
    client = FakeOssClient()
    config = OssStorageConfig(
        driver="oss",
        bucket="example-bucket",
        region="cn-chengdu",
        prefix="products",
    )
    storage = OssStorage(config, client=client)
    source = tmp_path / "psygo.apk"
    source.write_bytes(b"artifact")

    uploaded = storage.upload_file(
        "Android/psygo-1.0.0.apk",
        source,
        overwrite=False,
        content_type="application/vnd.android.package-archive",
        metadata={"distkeeper-sha256": "a" * 64},
    )
    storage.copy(
        "Android/psygo-1.0.0.apk",
        "dist/psygo.apk",
        overwrite=True,
        source_etag=uploaded.etag,
    )
    listed = list(storage.list_objects("manifests/"))

    assert client.upload_request.key == "products/Android/psygo-1.0.0.apk"
    assert client.upload_request.forbid_overwrite is True
    assert client.upload_request.metadata["distkeeper-sha256"] == "a" * 64
    assert client.copy_request.key == "products/dist/psygo.apk"
    assert client.copy_request.if_match == "etag-current"
    assert listed[0].key == "manifests/1.0.0.json"
