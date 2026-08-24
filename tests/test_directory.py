from __future__ import annotations

from pathlib import Path

import pytest

from distkeeper.directory import DirectoryService
from distkeeper.errors import ConfigError
from distkeeper.storage.local import LocalStorage


def put_object(storage: LocalStorage, key: str, data: bytes = b"data") -> None:
    storage.put_bytes(
        key,
        data,
        overwrite=False,
        content_type="application/octet-stream",
        metadata={},
    )


def test_directory_query_builds_nested_tree(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    put_object(storage, "Android/psygo-1.0.0.apk", b"apk")
    put_object(storage, "Windows/x64/psygo-1.0.0.exe", b"windows")
    put_object(storage, "dist/psygo.apk", b"latest")

    listing = DirectoryService(storage).query()

    assert listing.objects_returned == 3
    assert not listing.truncated
    assert [node.name for node in listing.nodes] == ["Android", "dist", "Windows"]
    android = listing.nodes[0]
    assert android.kind == "directory"
    assert android.children[0].name == "psygo-1.0.0.apk"
    assert android.children[0].size == 3
    windows = listing.nodes[2]
    assert windows.children[0].name == "x64"
    assert windows.children[0].children[0].key == "Windows/x64/psygo-1.0.0.exe"


def test_directory_query_supports_prefix_and_limit(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    put_object(storage, "Android/psygo-1.0.0.apk")
    put_object(storage, "Android/psygo-1.1.0.apk")
    put_object(storage, "dist/psygo.apk")

    prefixed = DirectoryService(storage).query("/Android/")
    limited = DirectoryService(storage).query(limit=1)

    assert prefixed.prefix == "Android/"
    assert [node.name for node in prefixed.nodes] == [
        "psygo-1.0.0.apk",
        "psygo-1.1.0.apk",
    ]
    assert limited.objects_returned == 1
    assert limited.truncated


def test_directory_query_rejects_unsafe_prefix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unsafe path segment"):
        DirectoryService(LocalStorage(tmp_path)).query("../")
