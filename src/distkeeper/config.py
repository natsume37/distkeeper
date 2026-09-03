"""Configuration loading, validation, and path template rendering."""

from __future__ import annotations

import os
import re
from pathlib import Path
from string import Formatter
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from distkeeper.domain import ArtifactPaths
from distkeeper.errors import ConfigError

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ARTIFACT_TEMPLATE_FIELDS = frozenset(
    {
        "repository",
        "target",
        "channel",
        "platform",
        "arch",
        "variant",
        "artifact",
        "version",
        "extension",
    }
)
MANIFEST_TEMPLATE_FIELDS = ARTIFACT_TEMPLATE_FIELDS - {"extension"}


def _validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must match {IDENTIFIER_PATTERN.pattern!r}")
    return value


def _template_fields(template: str, label: str) -> set[str]:
    fields: set[str] = set()
    try:
        parsed = Formatter().parse(template)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if format_spec or conversion:
                raise ValueError(f"{label} does not support format specs or conversions")
            fields.add(field_name)
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    return fields


def validate_object_key(key: str, label: str = "object key") -> str:
    """Reject keys that are unsafe for either object stores or local storage."""
    if not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise ConfigError(f"{label} must be a non-empty relative POSIX path: {key!r}")
    if any(part in {"", ".", ".."} for part in key.split("/")):
        raise ConfigError(f"{label} contains an unsafe path segment: {key!r}")
    return key


def normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    return validate_object_key(normalized, "storage prefix")


class LocalStorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: Literal["local"]
    root: Path
    prefix: str = ""

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return normalize_prefix(value)


class OssStorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: Literal["oss"]
    bucket: str = Field(min_length=3)
    region: str = Field(min_length=1)
    endpoint: str | None = None
    prefix: str = ""
    use_internal_endpoint: bool = False
    use_cname: bool = False
    upload_part_size_mib: int = Field(default=16, ge=1, le=5120)
    upload_parallelism: int = Field(default=3, ge=1, le=32)

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return normalize_prefix(value)


type StorageConfig = Annotated[
    LocalStorageConfig | OssStorageConfig,
    Field(discriminator="driver"),
]


class SafetyConfig(BaseModel):
    """Filesystem and object-key boundaries for mutating release operations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_source_roots: tuple[Path, ...] = (Path("dist"),)
    allowed_write_prefixes: tuple[str, ...] = ("dist", ".distkeeper")

    @field_validator("allowed_source_roots")
    @classmethod
    def validate_source_roots(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        if not values:
            raise ValueError("allowed_source_roots must not be empty")
        normalized: list[Path] = []
        for root in values:
            expanded = root.expanduser()
            if expanded == Path(".") or expanded == Path(expanded.anchor or "."):
                raise ValueError("allowed_source_roots must not contain the filesystem root")
            if ".." in expanded.parts:
                raise ValueError(f"allowed_source_root contains an unsafe path: {root!s}")
            if expanded in normalized:
                raise ValueError(f"duplicate allowed_source_root: {root!s}")
            normalized.append(expanded)
        return tuple(normalized)

    @field_validator("allowed_write_prefixes")
    @classmethod
    def validate_write_prefixes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("allowed_write_prefixes must not be empty")
        normalized: list[str] = []
        for value in values:
            try:
                prefix = normalize_prefix(value)
            except ConfigError as exc:
                raise ValueError(str(exc)) from exc
            if prefix in normalized:
                raise ValueError(f"duplicate allowed_write_prefix: {value!r}")
            normalized.append(prefix)
        return tuple(normalized)


class TargetConfig(BaseModel):
    """Configuration for one deliverable, such as android-apk or windows-x64."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: str
    arch: str = "universal"
    variant: str = "default"
    artifact_name: str | None = None
    extensions: tuple[str, ...]
    content_type: str = "application/octet-stream"
    versioned_key: str
    latest_key: str
    version_manifest_key: str = (
        ".distkeeper/{repository}/{target}/{channel}/releases/{version}.json"
    )
    latest_manifest_key: str = ".distkeeper/{repository}/{target}/{channel}/latest.json"

    @field_validator("platform", "arch", "variant")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value, "target attribute")

    @field_validator("artifact_name")
    @classmethod
    def validate_artifact_name(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_identifier(value, "artifact_name")
        return value

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("extensions must not be empty")
        normalized: list[str] = []
        for extension in values:
            if not extension.startswith(".") or "/" in extension or "\\" in extension:
                raise ValueError(f"invalid extension: {extension!r}")
            if extension.lower() in {item.lower() for item in normalized}:
                raise ValueError(f"duplicate extension: {extension!r}")
            normalized.append(extension)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_templates(self) -> TargetConfig:
        artifact_templates = {
            "versioned_key": self.versioned_key,
            "latest_key": self.latest_key,
        }
        manifest_templates = {
            "version_manifest_key": self.version_manifest_key,
            "latest_manifest_key": self.latest_manifest_key,
        }
        for label, template in artifact_templates.items():
            unknown = _template_fields(template, label) - ARTIFACT_TEMPLATE_FIELDS
            if unknown:
                raise ValueError(f"{label} contains unsupported fields: {sorted(unknown)}")
        for label, template in manifest_templates.items():
            unknown = _template_fields(template, label) - MANIFEST_TEMPLATE_FIELDS
            if unknown:
                raise ValueError(f"{label} contains unsupported fields: {sorted(unknown)}")

        sample_values = {
            "repository": "repository",
            "target": "target",
            "channel": "stable",
            "platform": self.platform,
            "arch": self.arch,
            "variant": self.variant,
            "artifact": self.artifact_name or "artifact",
            "version": "1.0.0",
            "extension": ".bin",
        }
        for label, template in artifact_templates.items() | manifest_templates.items():
            try:
                validate_object_key(template.format_map(sample_values), label)
            except ConfigError as exc:
                raise ValueError(str(exc)) from exc
        if "version" not in _template_fields(self.versioned_key, "versioned_key"):
            raise ValueError("versioned_key must contain {version}")
        if "version" in _template_fields(self.latest_key, "latest_key"):
            raise ValueError("latest_key must not contain {version}")
        if "version" not in _template_fields(self.version_manifest_key, "version_manifest_key"):
            raise ValueError("version_manifest_key must contain {version}")
        if "version" in _template_fields(self.latest_manifest_key, "latest_manifest_key"):
            raise ValueError("latest_manifest_key must not contain {version}")
        return self


class RepositoryConfig(BaseModel):
    """A named software repository containing one or more release targets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_name: str | None = None
    channels: tuple[str, ...] = ("stable",)
    version_pattern: str = SEMVER_PATTERN
    targets: dict[str, TargetConfig]

    @field_validator("artifact_name")
    @classmethod
    def validate_artifact_name(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_identifier(value, "artifact_name")
        return value

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("channels must not be empty")
        for value in values:
            _validate_identifier(value, "channel")
        if len(values) != len(set(values)):
            raise ValueError("channels must be unique")
        return values

    @field_validator("version_pattern")
    @classmethod
    def validate_version_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid version_pattern: {exc}") from exc
        return value

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, values: dict[str, TargetConfig]) -> dict[str, TargetConfig]:
        if not values:
            raise ValueError("targets must not be empty")
        for name in values:
            _validate_identifier(name, "target name")
        return values


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    storage: StorageConfig
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    repositories: dict[str, RepositoryConfig]

    @field_validator("repositories")
    @classmethod
    def validate_repositories(
        cls, values: dict[str, RepositoryConfig]
    ) -> dict[str, RepositoryConfig]:
        if not values:
            raise ValueError("repositories must not be empty")
        for name in values:
            _validate_identifier(name, "repository name")
        return values

    def resolve(self, repository: str, target: str, channel: str) -> ResolvedTarget:
        repository_config = self.repositories.get(repository)
        if repository_config is None:
            choices = ", ".join(sorted(self.repositories))
            raise ConfigError(f"unknown repository {repository!r}; configured: {choices}")
        target_config = repository_config.targets.get(target)
        if target_config is None:
            choices = ", ".join(sorted(repository_config.targets))
            raise ConfigError(
                f"unknown target {target!r} for repository {repository!r}; configured: {choices}"
            )
        if channel not in repository_config.channels:
            choices = ", ".join(repository_config.channels)
            raise ConfigError(f"unknown channel {channel!r}; configured: {choices}")
        return ResolvedTarget(
            repository_name=repository,
            target_name=target,
            channel=channel,
            repository=repository_config,
            target=target_config,
        )


class ResolvedTarget:
    """Validated repository/target selection with safe template helpers."""

    def __init__(
        self,
        *,
        repository_name: str,
        target_name: str,
        channel: str,
        repository: RepositoryConfig,
        target: TargetConfig,
    ) -> None:
        self.repository_name = repository_name
        self.target_name = target_name
        self.channel = channel
        self.repository = repository
        self.target = target

    @property
    def artifact_name(self) -> str:
        return self.target.artifact_name or self.repository.artifact_name or self.repository_name

    def validate_version(self, version: str) -> str:
        if not re.fullmatch(self.repository.version_pattern, version):
            raise ConfigError(
                f"version {version!r} does not match repository pattern "
                f"{self.repository.version_pattern!r}"
            )
        if "/" in version or "\\" in version or version in {".", ".."}:
            raise ConfigError(f"unsafe version: {version!r}")
        return version

    def extension_for_path(self, path: Path) -> str:
        filename = path.name.lower()
        matches = [item for item in self.target.extensions if filename.endswith(item.lower())]
        if not matches:
            allowed = ", ".join(self.target.extensions)
            raise ConfigError(f"file {path.name!r} must end with one of: {allowed}")
        return max(matches, key=len)

    def validate_extension(self, extension: str | None) -> str:
        if extension is None:
            if len(self.target.extensions) != 1:
                choices = ", ".join(self.target.extensions)
                raise ConfigError(f"--extension is required; configured choices: {choices}")
            return self.target.extensions[0]
        normalized = extension if extension.startswith(".") else f".{extension}"
        for configured in self.target.extensions:
            if configured.lower() == normalized.lower():
                return configured
        choices = ", ".join(self.target.extensions)
        raise ConfigError(f"extension {extension!r} is not allowed; configured: {choices}")

    def paths(self, version: str, extension: str) -> ArtifactPaths:
        version = self.validate_version(version)
        values = self._values(version=version, extension=extension)
        paths = ArtifactPaths(
            versioned=self._render(self.target.versioned_key, values, "versioned_key"),
            latest=self._render(self.target.latest_key, values, "latest_key"),
            version_manifest=self._render(
                self.target.version_manifest_key, values, "version_manifest_key"
            ),
            latest_manifest=self._render(
                self.target.latest_manifest_key, values, "latest_manifest_key"
            ),
        )
        rendered = {paths.versioned, paths.latest, paths.version_manifest, paths.latest_manifest}
        if len(rendered) != 4:
            raise ConfigError("rendered artifact and manifest keys must be distinct")
        return paths

    def version_manifest_key(self, version: str) -> str:
        values = self._values(version=self.validate_version(version), extension="")
        return self._render(self.target.version_manifest_key, values, "version_manifest_key")

    def latest_manifest_key(self) -> str:
        values = self._values(version="", extension="")
        return self._render(self.target.latest_manifest_key, values, "latest_manifest_key")

    def version_manifest_prefix(self) -> str:
        marker = "__DISTKEEPER_VERSION__"
        values = self._values(version=marker, extension="")
        rendered = self._render(self.target.version_manifest_key, values, "version_manifest_key")
        return rendered.partition(marker)[0]

    def _values(self, *, version: str, extension: str) -> dict[str, str]:
        return {
            "repository": self.repository_name,
            "target": self.target_name,
            "channel": self.channel,
            "platform": self.target.platform,
            "arch": self.target.arch,
            "variant": self.target.variant,
            "artifact": self.artifact_name,
            "version": version,
            "extension": extension,
        }

    @staticmethod
    def _render(template: str, values: dict[str, str], label: str) -> str:
        try:
            rendered = template.format_map(values)
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"failed to render {label}: {exc}") from exc
        return validate_object_key(rendered, label)


def _expand_environment(value: Any) -> Any:
    if isinstance(value, str):
        missing = sorted({name for name in ENV_PATTERN.findall(value) if name not in os.environ})
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        return ENV_PATTERN.sub(lambda match: os.environ[match.group(1)], value)
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    return value


def load_config(path: Path) -> AppConfig:
    """Load YAML safely and resolve local storage paths relative to the config file."""
    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_file():
        raise ConfigError(f"configuration file does not exist: {resolved_path}")
    try:
        raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"failed to read configuration {resolved_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a YAML mapping")
    try:
        config = AppConfig.model_validate(_expand_environment(raw))
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration:\n{exc}") from exc
    source_roots = tuple(
        (resolved_path.parent / root).resolve()
        if not root.is_absolute()
        else root.expanduser().resolve()
        for root in config.safety.allowed_source_roots
    )
    config = config.model_copy(
        update={"safety": config.safety.model_copy(update={"allowed_source_roots": source_roots})}
    )
    if isinstance(config.storage, LocalStorageConfig) and not config.storage.root.is_absolute():
        storage = config.storage.model_copy(
            update={"root": (resolved_path.parent / config.storage.root).resolve()}
        )
        config = config.model_copy(update={"storage": storage})
    return config
