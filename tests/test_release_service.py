from __future__ import annotations

from pathlib import Path

import pytest

from distkeeper.config import AppConfig
from distkeeper.errors import ConflictError
from distkeeper.service import ReleaseService
from distkeeper.storage.local import LocalStorage


def build_config(storage_root: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "schema_version": 1,
            "storage": {"driver": "local", "root": storage_root},
            "repositories": {
                "psygo": {
                    "artifact_name": "psygo",
                    "channels": ["stable"],
                    "targets": {
                        "android": {
                            "platform": "android",
                            "extensions": [".apk"],
                            "content_type": "application/vnd.android.package-archive",
                            "versioned_key": "Android/{artifact}-{version}{extension}",
                            "latest_key": "dist/{artifact}{extension}",
                        }
                    },
                }
            },
        }
    )


def build_service(storage_root: Path) -> ReleaseService:
    config = build_config(storage_root)
    return ReleaseService(config, LocalStorage(storage_root))


def write_artifact(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_publish_is_idempotent_and_updates_latest(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    service = build_service(storage_root)
    source = write_artifact(tmp_path / "psygo.apk", b"release-0.2.38")

    first = service.publish(
        source,
        repository="psygo",
        target="android",
        version="0.2.38",
    )
    retried = service.publish(
        source,
        repository="psygo",
        target="android",
        version="0.2.38",
    )

    assert first.artifact.sha256 == retried.artifact.sha256
    assert (storage_root / "Android/psygo-0.2.38.apk").read_bytes() == b"release-0.2.38"
    assert (storage_root / "dist/psygo.apk").read_bytes() == b"release-0.2.38"
    assert service.verify(repository="psygo", target="android").ok


def test_publish_rejects_different_content_for_same_version(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    service = build_service(storage_root)
    source = write_artifact(tmp_path / "psygo.apk", b"first-content")
    service.publish(source, repository="psygo", target="android", version="0.2.38")
    source.write_bytes(b"other-content")

    with pytest.raises(ConflictError, match="SHA-256"):
        service.publish(source, repository="psygo", target="android", version="0.2.38")

    assert (storage_root / "Android/psygo-0.2.38.apk").read_bytes() == b"first-content"


def test_publish_list_and_rollback(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    service = build_service(storage_root)
    source = tmp_path / "psygo.apk"
    write_artifact(source, b"release-0.2.38")
    service.publish(source, repository="psygo", target="android", version="0.2.38")
    source.write_bytes(b"release-0.2.39")
    service.publish(source, repository="psygo", target="android", version="0.2.39")

    releases = service.list_releases(repository="psygo", target="android")
    assert [release.version for release in releases] == ["0.2.39", "0.2.38"]
    assert (storage_root / "dist/psygo.apk").read_bytes() == b"release-0.2.39"

    active = service.rollback(
        repository="psygo",
        target="android",
        version="0.2.38",
    )
    assert active.activation == "rollback"
    assert (storage_root / "dist/psygo.apk").read_bytes() == b"release-0.2.38"
    assert service.verify(repository="psygo", target="android").ok


def test_adopt_archives_existing_latest_without_removing_it(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    latest = storage_root / "dist/psygo.apk"
    latest.parent.mkdir(parents=True)
    latest.write_bytes(b"existing-release")
    service = build_service(storage_root)

    manifest = service.adopt(
        repository="psygo",
        target="android",
        version="0.2.37",
    )

    assert manifest.origin == "adopt"
    assert latest.read_bytes() == b"existing-release"
    assert (storage_root / "Android/psygo-0.2.37.apk").read_bytes() == b"existing-release"
    assert service.verify(repository="psygo", target="android").ok


def test_verify_detects_tampered_versioned_artifact(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    service = build_service(storage_root)
    source = write_artifact(tmp_path / "psygo.apk", b"original")
    service.publish(source, repository="psygo", target="android", version="1.0.0")
    (storage_root / "Android/psygo-1.0.0.apk").write_bytes(b"tampered")

    report = service.verify(repository="psygo", target="android", full_hash=True)

    assert not report.ok
    assert report.items[0].sha256_matches is False


def test_plan_does_not_create_storage_root(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    service = build_service(storage_root)
    source = write_artifact(tmp_path / "psygo.apk", b"planned")

    plan = service.plan_publish(
        source,
        repository="psygo",
        target="android",
        version="1.2.3",
    )

    assert plan.operation == "publish"
    assert not storage_root.exists()
