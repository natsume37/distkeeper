"""Storage-independent release workflows."""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

from distkeeper.config import AppConfig, ResolvedTarget, validate_object_key
from distkeeper.domain import (
    ArtifactPaths,
    ArtifactRecord,
    OperationPlan,
    PreparedArtifact,
    ReleaseManifest,
    VerificationItem,
    VerificationReport,
)
from distkeeper.errors import (
    ConflictError,
    IntegrityError,
    ObjectAlreadyExistsError,
    ReleaseNotFoundError,
    SafetyError,
)
from distkeeper.storage.base import ObjectInfo, Storage, read_object

SHA256_METADATA_KEY = "distkeeper-sha256"
REPOSITORY_METADATA_KEY = "distkeeper-repository"
TARGET_METADATA_KEY = "distkeeper-target"
CHANNEL_METADATA_KEY = "distkeeper-channel"
VERSION_METADATA_KEY = "distkeeper-version"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ReleaseService:
    """Coordinates releases using only the generic storage protocol."""

    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._storage = storage
        self._clock = clock

    def plan_publish(
        self,
        source: Path,
        *,
        repository: str,
        target: str,
        version: str,
        channel: str = "stable",
    ) -> OperationPlan:
        prepared = self._prepare_local_artifact(
            source,
            repository=repository,
            target=target,
            version=version,
            channel=channel,
        )
        candidate = self._manifest_for_prepared(prepared)
        scope_violations = self._scope_violations(source=prepared.source, paths=prepared.paths)
        actions: list[str] = []

        existing_artifact = self._storage.stat(prepared.paths.versioned)
        if existing_artifact is None:
            actions.append(f"upload immutable artifact: {prepared.paths.versioned}")
        else:
            self._assert_object_matches(
                existing_artifact,
                expected_size=prepared.size,
                expected_sha256=prepared.sha256,
                full_hash=False,
            )
            actions.append(f"reuse immutable artifact: {prepared.paths.versioned}")

        existing_manifest = self._optional_manifest(prepared.paths.version_manifest)
        if existing_manifest is None:
            actions.append(f"create version manifest: {prepared.paths.version_manifest}")
        else:
            self._assert_manifest_equivalent(existing_manifest, candidate)
            actions.append(f"reuse version manifest: {prepared.paths.version_manifest}")

        latest_action = "replace" if self._storage.stat(prepared.paths.latest) else "create"
        actions.extend(
            (
                f"{latest_action} latest artifact: {prepared.paths.latest}",
                f"write latest manifest: {prepared.paths.latest_manifest}",
            )
        )
        self._assert_local_source_unchanged(prepared)
        return OperationPlan(
            operation="publish",
            repository=repository,
            target=target,
            version=version,
            actions=tuple(actions),
            scope_violations=scope_violations,
        )

    def publish(
        self,
        source: Path,
        *,
        repository: str,
        target: str,
        version: str,
        channel: str = "stable",
        confirm_outside_scope: bool = False,
    ) -> ReleaseManifest:
        self._require_scope(
            self._source_scope_violations(source),
            confirmed=confirm_outside_scope,
        )
        prepared = self._prepare_local_artifact(
            source,
            repository=repository,
            target=target,
            version=version,
            channel=channel,
        )
        candidate = self._manifest_for_prepared(prepared)
        self._require_scope(
            self._scope_violations(source=prepared.source, paths=prepared.paths),
            confirmed=confirm_outside_scope,
        )
        metadata = self._metadata(candidate)
        self._assert_local_source_unchanged(prepared)

        existing = self._storage.stat(prepared.paths.versioned)
        existing_manifest = self._optional_manifest(prepared.paths.version_manifest)
        uploaded = False
        if existing is None:
            try:
                existing = self._storage.upload_file(
                    prepared.paths.versioned,
                    prepared.source,
                    overwrite=False,
                    content_type=prepared.content_type,
                    metadata=metadata,
                )
                uploaded = True
            except ObjectAlreadyExistsError:
                existing = self._storage.stat(prepared.paths.versioned)
                if existing is None:
                    raise
        self._assert_local_source_unchanged(prepared)
        self._assert_object_matches(
            existing,
            expected_size=prepared.size,
            expected_sha256=prepared.sha256,
            # 无清单的残留对象可能来自中断的上传，必须重新读取内容确认。
            full_hash=not uploaded and existing_manifest is None,
        )

        version_manifest = self._ensure_version_manifest(prepared.paths.version_manifest, candidate)

        # latest 文件先切换，latest manifest 最后写入，后者可作为一次发布完成的标记。
        latest_info = self._storage.copy(
            prepared.paths.versioned,
            prepared.paths.latest,
            overwrite=True,
            content_type=prepared.content_type,
            metadata=metadata,
            source_etag=existing.etag,
        )
        self._assert_object_matches(
            latest_info,
            expected_size=prepared.size,
            expected_sha256=prepared.sha256,
            full_hash=False,
        )
        active_manifest = self._activate(version_manifest, "publish")
        self._write_latest_manifest(prepared.paths.latest_manifest, active_manifest)
        return active_manifest

    def plan_adopt(
        self,
        *,
        repository: str,
        target: str,
        version: str,
        channel: str = "stable",
        extension: str | None = None,
    ) -> OperationPlan:
        resolved = self._config.resolve(repository, target, channel)
        extension = resolved.validate_extension(extension)
        paths = resolved.paths(version, extension)
        scope_violations = self._scope_violations(source=None, paths=paths)
        latest = self._storage.stat(paths.latest)
        if latest is None:
            raise ReleaseNotFoundError(f"latest artifact does not exist: {paths.latest}")
        destination = self._storage.stat(paths.versioned)
        digest = self._sha256_object(paths.latest)
        self._assert_storage_object_unchanged(latest)
        candidate = self._manifest_for_remote(
            resolved,
            version=version,
            extension=extension,
            size=latest.size,
            sha256=digest,
            content_type=self._remote_content_type(resolved, latest),
        )
        if destination is not None:
            self._assert_object_matches(
                destination,
                expected_size=latest.size,
                expected_sha256=digest,
                full_hash=False,
            )
        existing_manifest = self._optional_manifest(paths.version_manifest)
        if existing_manifest is not None:
            self._assert_manifest_equivalent(existing_manifest, candidate)
        actions = (
            f"{'verify' if destination else 'copy'} latest artifact to immutable key: "
            f"{paths.latest} -> {paths.versioned}",
            f"create or reuse version manifest: {paths.version_manifest}",
            f"write latest manifest: {paths.latest_manifest}",
        )
        return OperationPlan(
            operation="adopt",
            repository=repository,
            target=target,
            version=version,
            actions=actions,
            scope_violations=scope_violations,
        )

    def adopt(
        self,
        *,
        repository: str,
        target: str,
        version: str,
        channel: str = "stable",
        extension: str | None = None,
        confirm_outside_scope: bool = False,
    ) -> ReleaseManifest:
        resolved = self._config.resolve(repository, target, channel)
        extension = resolved.validate_extension(extension)
        paths = resolved.paths(version, extension)
        self._require_scope(
            self._scope_violations(source=None, paths=paths),
            confirmed=confirm_outside_scope,
        )
        latest = self._storage.stat(paths.latest)
        if latest is None:
            raise ReleaseNotFoundError(f"latest artifact does not exist: {paths.latest}")

        digest = self._sha256_object(paths.latest)
        self._assert_storage_object_unchanged(latest)
        content_type = self._remote_content_type(resolved, latest)
        candidate = self._manifest_for_remote(
            resolved,
            version=version,
            extension=extension,
            size=latest.size,
            sha256=digest,
            content_type=content_type,
        )
        metadata = self._metadata(candidate)

        versioned = self._storage.stat(paths.versioned)
        if versioned is None:
            try:
                versioned = self._storage.copy(
                    paths.latest,
                    paths.versioned,
                    overwrite=False,
                    content_type=content_type,
                    metadata=metadata,
                    source_etag=latest.etag,
                )
            except ObjectAlreadyExistsError:
                versioned = self._storage.stat(paths.versioned)
                if versioned is None:
                    raise
        self._assert_object_matches(
            versioned,
            expected_size=latest.size,
            expected_sha256=digest,
            full_hash=False,
        )
        version_manifest = self._ensure_version_manifest(paths.version_manifest, candidate)
        active_manifest = self._activate(version_manifest, "adopt")
        self._write_latest_manifest(paths.latest_manifest, active_manifest)
        return active_manifest

    def list_releases(
        self,
        *,
        repository: str,
        target: str,
        channel: str = "stable",
    ) -> list[ReleaseManifest]:
        resolved = self._config.resolve(repository, target, channel)
        releases: list[ReleaseManifest] = []
        for info in self._storage.list_objects(resolved.version_manifest_prefix()):
            if not info.key.endswith(".json"):
                continue
            manifest = self._required_manifest(info.key)
            self._assert_manifest_identity(manifest, resolved)
            releases.append(manifest)
        releases.sort(key=self._version_sort_key, reverse=True)
        return releases

    def plan_rollback(
        self,
        *,
        repository: str,
        target: str,
        version: str,
        channel: str = "stable",
    ) -> OperationPlan:
        resolved, manifest = self._release_by_version(
            repository=repository,
            target=target,
            version=version,
            channel=channel,
        )
        paths = resolved.paths(version, manifest.artifact.extension)
        scope_violations = self._scope_violations(source=None, paths=paths)
        source = self._storage.stat(manifest.artifact.key)
        if source is None:
            raise ReleaseNotFoundError(
                f"versioned artifact does not exist: {manifest.artifact.key}"
            )
        self._assert_object_matches(
            source,
            expected_size=manifest.artifact.size,
            expected_sha256=manifest.artifact.sha256,
            full_hash=False,
        )
        return OperationPlan(
            operation="rollback",
            repository=repository,
            target=target,
            version=version,
            actions=(
                f"copy immutable artifact to latest: {manifest.artifact.key} -> {paths.latest}",
                f"write latest manifest: {paths.latest_manifest}",
            ),
            scope_violations=scope_violations,
        )

    def rollback(
        self,
        *,
        repository: str,
        target: str,
        version: str,
        channel: str = "stable",
        confirm_outside_scope: bool = False,
    ) -> ReleaseManifest:
        resolved, manifest = self._release_by_version(
            repository=repository,
            target=target,
            version=version,
            channel=channel,
        )
        paths = resolved.paths(version, manifest.artifact.extension)
        self._require_scope(
            self._scope_violations(source=None, paths=paths),
            confirmed=confirm_outside_scope,
        )
        source = self._storage.stat(manifest.artifact.key)
        if source is None:
            raise ReleaseNotFoundError(
                f"versioned artifact does not exist: {manifest.artifact.key}"
            )
        self._assert_object_matches(
            source,
            expected_size=manifest.artifact.size,
            expected_sha256=manifest.artifact.sha256,
            full_hash=False,
        )

        updated_artifact = manifest.artifact.model_copy(update={"latest_key": paths.latest})
        active_manifest = manifest.model_copy(
            update={
                "artifact": updated_artifact,
                "activation": "rollback",
                "activated_at": self._clock(),
            }
        )
        latest = self._storage.copy(
            manifest.artifact.key,
            paths.latest,
            overwrite=True,
            content_type=manifest.artifact.content_type,
            metadata=self._metadata(active_manifest),
            source_etag=source.etag,
        )
        self._assert_object_matches(
            latest,
            expected_size=manifest.artifact.size,
            expected_sha256=manifest.artifact.sha256,
            full_hash=False,
        )
        self._write_latest_manifest(paths.latest_manifest, active_manifest)
        return active_manifest

    def verify(
        self,
        *,
        repository: str,
        target: str,
        channel: str = "stable",
        version: str | None = None,
        full_hash: bool = True,
    ) -> VerificationReport:
        resolved = self._config.resolve(repository, target, channel)
        if version is None:
            manifest = self._required_manifest(resolved.latest_manifest_key())
            keys = tuple(dict.fromkeys((manifest.artifact.key, manifest.artifact.latest_key)))
        else:
            manifest = self._required_manifest(resolved.version_manifest_key(version))
            keys = (manifest.artifact.key,)
        self._assert_manifest_identity(manifest, resolved)

        items: list[VerificationItem] = []
        for key in keys:
            validate_object_key(key)
            info = self._storage.stat(key)
            if info is None:
                items.append(
                    VerificationItem(
                        key=key,
                        exists=False,
                        size_matches=False,
                        sha256_matches=False,
                    )
                )
                continue
            if full_hash:
                digest_matches: bool | None = self._sha256_object(key) == manifest.artifact.sha256
            else:
                stored_digest = self._metadata_value(info, SHA256_METADATA_KEY)
                digest_matches = (
                    stored_digest == manifest.artifact.sha256 if stored_digest else None
                )
            items.append(
                VerificationItem(
                    key=key,
                    exists=True,
                    size_matches=info.size == manifest.artifact.size,
                    sha256_matches=digest_matches,
                )
            )
        return VerificationReport(manifest=manifest, items=tuple(items))

    def _prepare_local_artifact(
        self,
        source: Path,
        *,
        repository: str,
        target: str,
        version: str,
        channel: str,
    ) -> PreparedArtifact:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise ReleaseNotFoundError(f"local artifact does not exist: {source}")
        resolved = self._config.resolve(repository, target, channel)
        extension = resolved.extension_for_path(source)
        paths = resolved.paths(version, extension)
        content_type = resolved.target.content_type
        if content_type == "application/octet-stream":
            content_type = mimetypes.guess_type(source.name)[0] or content_type
        before_hash = self._file_fingerprint(source)
        digest = self._sha256_file(source)
        after_hash = self._file_fingerprint(source)
        if before_hash != after_hash:
            raise IntegrityError(f"local artifact changed while hashing: {source}")
        return PreparedArtifact(
            source=source,
            repository=repository,
            target=target,
            channel=channel,
            version=version,
            filename=PurePosixPath(paths.versioned).name,
            extension=extension,
            content_type=content_type,
            size=after_hash[2],
            sha256=digest,
            source_fingerprint=after_hash,
            paths=paths,
        )

    def _scope_violations(
        self,
        *,
        source: Path | None,
        paths: ArtifactPaths,
    ) -> tuple[str, ...]:
        """Return every source or destination that falls outside the configured allowlist."""
        violations = list(self._source_scope_violations(source)) if source is not None else []

        for label, key in (
            ("versioned artifact", paths.versioned),
            ("latest artifact", paths.latest),
            ("version manifest", paths.version_manifest),
            ("latest manifest", paths.latest_manifest),
        ):
            if not self._key_is_allowed_for_write(key):
                prefixes = ", ".join(self._config.safety.allowed_write_prefixes)
                violations.append(
                    f"{label} key {key!r} is outside allowed write prefixes: {prefixes}"
                )
        return tuple(violations)

    def _source_scope_violations(self, source: Path) -> tuple[str, ...]:
        source_path = source.expanduser().resolve()
        allowed_roots = tuple(
            root.expanduser().resolve() for root in self._config.safety.allowed_source_roots
        )
        if any(source_path.is_relative_to(root) for root in allowed_roots):
            return ()
        roots = ", ".join(str(root) for root in allowed_roots)
        return (f"source file {source_path} is outside allowed source roots: {roots}",)

    def _key_is_allowed_for_write(self, key: str) -> bool:
        return any(
            key == prefix or key.startswith(f"{prefix}/")
            for prefix in self._config.safety.allowed_write_prefixes
        )

    @staticmethod
    def _require_scope(violations: tuple[str, ...], *, confirmed: bool) -> None:
        if not violations or confirmed:
            return
        details = "\n".join(f"  - {violation}" for violation in violations)
        raise SafetyError(
            "operation is outside the configured safety scope; review plan and "
            "rerun with --confirm-outside-scope:\n" + details
        )

    def _manifest_for_prepared(self, prepared: PreparedArtifact) -> ReleaseManifest:
        created_at = self._clock()
        return ReleaseManifest(
            repository=prepared.repository,
            target=prepared.target,
            channel=prepared.channel,
            version=prepared.version,
            origin="publish",
            activation="publish",
            published_at=created_at,
            activated_at=created_at,
            artifact=ArtifactRecord(
                key=prepared.paths.versioned,
                latest_key=prepared.paths.latest,
                filename=prepared.filename,
                extension=prepared.extension,
                size=prepared.size,
                sha256=prepared.sha256,
                content_type=prepared.content_type,
            ),
        )

    def _manifest_for_remote(
        self,
        resolved: ResolvedTarget,
        *,
        version: str,
        extension: str,
        size: int,
        sha256: str,
        content_type: str,
    ) -> ReleaseManifest:
        paths = resolved.paths(version, extension)
        published_at = self._clock()
        return ReleaseManifest(
            repository=resolved.repository_name,
            target=resolved.target_name,
            channel=resolved.channel,
            version=version,
            origin="adopt",
            activation="adopt",
            published_at=published_at,
            activated_at=published_at,
            artifact=ArtifactRecord(
                key=paths.versioned,
                latest_key=paths.latest,
                filename=PurePosixPath(paths.versioned).name,
                extension=extension,
                size=size,
                sha256=sha256,
                content_type=content_type,
            ),
        )

    def _ensure_version_manifest(self, key: str, candidate: ReleaseManifest) -> ReleaseManifest:
        existing = self._optional_manifest(key)
        if existing is not None:
            self._assert_manifest_equivalent(existing, candidate)
            return existing
        try:
            self._storage.put_bytes(
                key,
                candidate.to_json_bytes(),
                overwrite=False,
                content_type="application/json",
                metadata={},
            )
            return candidate
        except ObjectAlreadyExistsError:
            existing = self._required_manifest(key)
            self._assert_manifest_equivalent(existing, candidate)
            return existing

    def _write_latest_manifest(self, key: str, manifest: ReleaseManifest) -> None:
        self._storage.put_bytes(
            key,
            manifest.to_json_bytes(),
            overwrite=True,
            content_type="application/json",
            metadata={},
        )

    def _release_by_version(
        self,
        *,
        repository: str,
        target: str,
        version: str,
        channel: str,
    ) -> tuple[ResolvedTarget, ReleaseManifest]:
        resolved = self._config.resolve(repository, target, channel)
        manifest = self._required_manifest(resolved.version_manifest_key(version))
        self._assert_manifest_identity(manifest, resolved)
        if manifest.version != version:
            raise IntegrityError(f"manifest version is {manifest.version!r}, expected {version!r}")
        return resolved, manifest

    def _optional_manifest(self, key: str) -> ReleaseManifest | None:
        if self._storage.stat(key) is None:
            return None
        return self._required_manifest(key)

    def _required_manifest(self, key: str) -> ReleaseManifest:
        if self._storage.stat(key) is None:
            raise ReleaseNotFoundError(f"release manifest does not exist: {key}")
        try:
            return ReleaseManifest.from_json_bytes(read_object(self._storage, key))
        except (ValueError, ValidationError) as exc:
            raise IntegrityError(f"invalid release manifest {key!r}: {exc}") from exc

    @staticmethod
    def _assert_manifest_equivalent(existing: ReleaseManifest, candidate: ReleaseManifest) -> None:
        comparable_existing = existing.model_dump(
            exclude={"origin", "activation", "published_at", "activated_at"}
        )
        comparable_candidate = candidate.model_dump(
            exclude={"origin", "activation", "published_at", "activated_at"}
        )
        if comparable_existing != comparable_candidate:
            raise ConflictError(
                f"version {candidate.version!r} already has a different release manifest"
            )

    @staticmethod
    def _assert_manifest_identity(manifest: ReleaseManifest, resolved: ResolvedTarget) -> None:
        expected = (
            resolved.repository_name,
            resolved.target_name,
            resolved.channel,
        )
        actual = (manifest.repository, manifest.target, manifest.channel)
        if actual != expected:
            raise IntegrityError(
                "release manifest identity does not match the selected repository/target/channel"
            )

    def _assert_object_matches(
        self,
        info: ObjectInfo,
        *,
        expected_size: int,
        expected_sha256: str,
        full_hash: bool,
    ) -> None:
        if info.size != expected_size:
            raise ConflictError(
                f"object {info.key!r} has size {info.size}, expected {expected_size}"
            )
        stored_digest = self._metadata_value(info, SHA256_METADATA_KEY)
        if stored_digest is not None and stored_digest != expected_sha256:
            raise ConflictError(
                f"object {info.key!r} has SHA-256 {stored_digest}, expected {expected_sha256}"
            )
        if full_hash or stored_digest is None:
            actual_digest = self._sha256_object(info.key)
            if actual_digest != expected_sha256:
                raise ConflictError(
                    f"object {info.key!r} has SHA-256 {actual_digest}, expected {expected_sha256}"
                )

    def _sha256_object(self, key: str) -> str:
        digest = hashlib.sha256()
        for chunk in self._storage.read_chunks(key):
            digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _file_fingerprint(path: Path) -> tuple[int, int, int, int]:
        try:
            stat = path.stat()
        except OSError as exc:
            raise IntegrityError(f"failed to inspect local artifact {path}: {exc}") from exc
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _assert_local_source_unchanged(self, prepared: PreparedArtifact) -> None:
        if self._file_fingerprint(prepared.source) != prepared.source_fingerprint:
            raise IntegrityError(f"local artifact changed during publish: {prepared.source}")

    def _assert_storage_object_unchanged(self, before: ObjectInfo) -> None:
        after = self._storage.stat(before.key)
        if after is None:
            raise IntegrityError(f"storage object disappeared while reading: {before.key}")
        if before.size != after.size or (before.etag is not None and before.etag != after.etag):
            raise IntegrityError(f"storage object changed while reading: {before.key}")

    @staticmethod
    def _remote_content_type(resolved: ResolvedTarget, info: ObjectInfo) -> str:
        configured = resolved.target.content_type
        if configured != "application/octet-stream":
            return configured
        return info.content_type or configured

    @staticmethod
    def _metadata(manifest: ReleaseManifest) -> Mapping[str, str]:
        return {
            SHA256_METADATA_KEY: manifest.artifact.sha256,
            REPOSITORY_METADATA_KEY: manifest.repository,
            TARGET_METADATA_KEY: manifest.target,
            CHANNEL_METADATA_KEY: manifest.channel,
            VERSION_METADATA_KEY: manifest.version,
        }

    @staticmethod
    def _metadata_value(info: ObjectInfo, name: str) -> str | None:
        lowered = name.lower()
        for key, value in info.metadata.items():
            if key.lower() == lowered:
                return value
        return None

    def _activate(
        self,
        manifest: ReleaseManifest,
        reason: str,
    ) -> ReleaseManifest:
        if reason not in {"publish", "adopt", "rollback"}:
            raise AssertionError(f"invalid activation reason: {reason}")
        return manifest.model_copy(update={"activation": reason, "activated_at": self._clock()})

    @staticmethod
    def _version_sort_key(manifest: ReleaseManifest) -> tuple[int, Version | str]:
        try:
            return 1, Version(manifest.version)
        except InvalidVersion:
            return 0, manifest.version
