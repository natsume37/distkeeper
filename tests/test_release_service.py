from __future__ import annotations

from pathlib import Path

import pytest

from distkeeper.config import AppConfig
from distkeeper.errors import ConflictError, SafetyError
from distkeeper.service import ReleaseService
from distkeeper.storage.local import LocalStorage


def build_config(
    storage_root: Path,
    *,
    source_root: Path | None = None,
    versioned_key: str = "Android/{artifact}-{version}{extension}",
    latest_key: str = "dist/{artifact}{extension}",
    write_prefixes: tuple[str, ...] = ("Android", "dist", ".distkeeper"),
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "schema_version": 1,
            "storage": {"driver": "local", "root": storage_root},
            "safety": {
                "allowed_source_roots": [source_root or storage_root.parent],
                "allowed_write_prefixes": list(write_prefixes),
            },
            "repositories": {
                "psygo": {
                    "artifact_name": "psygo",
                    "channels": ["stable"],
                    "targets": {
                        "android": {
                            "platform": "android",
                            "extensions": [".apk"],
                            "content_type": "application/vnd.android.package-archive",
                            "versioned_key": versioned_key,
                            "latest_key": latest_key,
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
    path.parent.mkdir(parents=True, exist_ok=True)
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

    status = service.status(repository="psygo", target="android")
    assert status.state == "ready"
    assert status.current is not None and status.current.version == "0.2.39"
    assert status.previous is not None and status.previous.version == "0.2.38"

    active = service.rollback(
        repository="psygo",
        target="android",
        version="0.2.38",
    )
    assert active.activation == "rollback"
    assert (storage_root / "dist/psygo.apk").read_bytes() == b"release-0.2.38"
    assert service.verify(repository="psygo", target="android").ok

    rolled_back_status = service.status(repository="psygo", target="android")
    assert rolled_back_status.current is not None
    assert rolled_back_status.current.version == "0.2.38"
    assert rolled_back_status.previous is not None
    assert rolled_back_status.previous.version == "0.2.39"


def test_plan_id_rejects_a_changed_latest_release(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    service = build_service(storage_root)
    source = tmp_path / "psygo.apk"
    source.write_bytes(b"release-0.2.38")
    service.publish(source, repository="psygo", target="android", version="0.2.38")

    rollback_plan = service.plan_rollback(
        repository="psygo",
        target="android",
        version="0.2.38",
    )
    source.write_bytes(b"release-0.2.39")
    service.publish(source, repository="psygo", target="android", version="0.2.39")

    with pytest.raises(ConflictError, match="stale"):
        service.rollback(
            repository="psygo",
            target="android",
            version="0.2.38",
            plan_id=rollback_plan.plan_id,
        )


def test_plan_id_rejects_changed_publish_input(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    service = build_service(storage_root)
    source = tmp_path / "psygo.apk"
    source.write_bytes(b"release-content")
    publish_plan = service.plan_publish(
        source,
        repository="psygo",
        target="android",
        version="0.2.38",
    )
    source.write_bytes(b"different-content")

    with pytest.raises(ConflictError, match="stale"):
        service.publish(
            source,
            repository="psygo",
            target="android",
            version="0.2.38",
            plan_id=publish_plan.plan_id,
        )


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


def test_plan_reports_source_outside_allowlist_without_writing(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    allowed_root = tmp_path / "allowed"
    config = build_config(storage_root, source_root=allowed_root)
    service = ReleaseService(config, LocalStorage(storage_root))
    source = write_artifact(tmp_path / "outside" / "psygo.apk", b"planned")

    plan = service.plan_publish(
        source,
        repository="psygo",
        target="android",
        version="1.2.3",
    )

    assert plan.requires_confirmation
    assert any("outside allowed source roots" in item for item in plan.scope_violations)
    assert not storage_root.exists()


def test_publish_rejects_source_outside_allowlist_before_writing(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    config = build_config(storage_root, source_root=tmp_path / "allowed")
    service = ReleaseService(config, LocalStorage(storage_root))
    source = write_artifact(tmp_path / "outside.apk", b"release")

    with pytest.raises(SafetyError, match="outside allowed source roots"):
        service.publish(source, repository="psygo", target="android", version="1.0.0")

    assert not storage_root.exists()


def test_publish_allows_explicit_confirmation_for_outside_source(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    config = build_config(storage_root, source_root=tmp_path / "allowed")
    service = ReleaseService(config, LocalStorage(storage_root))
    source = write_artifact(tmp_path / "outside.apk", b"release")

    service.publish(
        source,
        repository="psygo",
        target="android",
        version="1.0.0",
        confirm_outside_scope=True,
    )

    assert (storage_root / "Android/psygo-1.0.0.apk").is_file()


def test_publish_rejects_destination_outside_allowlist(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    config = build_config(
        storage_root,
        versioned_key="distful/{artifact}-{version}{extension}",
    )
    service = ReleaseService(config, LocalStorage(storage_root))
    source = write_artifact(tmp_path / "psygo.apk", b"release")

    with pytest.raises(SafetyError, match="versioned artifact key"):
        service.publish(source, repository="psygo", target="android", version="1.0.0")

    assert not storage_root.exists()


def test_adopt_requires_confirmation_for_out_of_scope_destination(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    config = build_config(
        storage_root,
        versioned_key="legacy/{artifact}-{version}{extension}",
    )
    latest = storage_root / "dist/psygo.apk"
    latest.parent.mkdir(parents=True)
    latest.write_bytes(b"existing-release")
    service = ReleaseService(config, LocalStorage(storage_root))

    plan = service.plan_adopt(repository="psygo", target="android", version="1.0.0")
    assert plan.requires_confirmation

    with pytest.raises(SafetyError, match="versioned artifact key"):
        service.adopt(repository="psygo", target="android", version="1.0.0")

    adopted = service.adopt(
        repository="psygo",
        target="android",
        version="1.0.0",
        confirm_outside_scope=True,
    )
    assert adopted.origin == "adopt"
    assert (storage_root / "legacy/psygo-1.0.0.apk").is_file()


def test_rollback_requires_confirmation_for_out_of_scope_destination(tmp_path: Path) -> None:
    storage_root = tmp_path / "objects"
    config = build_config(
        storage_root,
        versioned_key="legacy/{artifact}-{version}{extension}",
    )
    service = ReleaseService(config, LocalStorage(storage_root))
    source = write_artifact(tmp_path / "psygo.apk", b"release")
    service.publish(
        source,
        repository="psygo",
        target="android",
        version="1.0.0",
        confirm_outside_scope=True,
    )

    plan = service.plan_rollback(repository="psygo", target="android", version="1.0.0")
    assert plan.requires_confirmation

    with pytest.raises(SafetyError, match="versioned artifact key"):
        service.rollback(repository="psygo", target="android", version="1.0.0")

    rolled_back = service.rollback(
        repository="psygo",
        target="android",
        version="1.0.0",
        confirm_outside_scope=True,
    )
    assert rolled_back.activation == "rollback"
