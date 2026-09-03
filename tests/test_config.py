from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from distkeeper.config import AppConfig, LocalStorageConfig, load_config, validate_object_key
from distkeeper.errors import ConfigError


def test_repository_name_is_part_of_rendered_paths(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "storage": {"driver": "local", "root": tmp_path},
            "repositories": {
                "desktop-app": {
                    "targets": {
                        "linux-x64": {
                            "platform": "linux",
                            "arch": "x64",
                            "extensions": [".tar.gz"],
                            "versioned_key": (
                                "releases/{repository}/{platform}/{artifact}-{version}{extension}"
                            ),
                            "latest_key": "dist/{repository}/{artifact}{extension}",
                        }
                    }
                }
            },
        }
    )

    paths = config.resolve("desktop-app", "linux-x64", "stable").paths("2.0.0", ".tar.gz")

    assert paths.versioned == "releases/desktop-app/linux/desktop-app-2.0.0.tar.gz"
    assert paths.latest == "dist/desktop-app/desktop-app.tar.gz"


def test_unsafe_rendered_path_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unsafe path segment"):
        validate_object_key("../escape.apk")


def test_unsafe_template_is_rejected_during_config_loading(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unsafe path segment"):
        AppConfig.model_validate(
            {
                "storage": {"driver": "local", "root": tmp_path},
                "repositories": {
                    "psygo": {
                        "targets": {
                            "android": {
                                "platform": "android",
                                "extensions": [".apk"],
                                "versioned_key": "../Android/{artifact}-{version}{extension}",
                                "latest_key": "dist/{artifact}{extension}",
                            }
                        }
                    }
                },
            }
        )


def test_version_template_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="versioned_key must contain"):
        AppConfig.model_validate(
            {
                "storage": {"driver": "local", "root": tmp_path},
                "repositories": {
                    "psygo": {
                        "targets": {
                            "android": {
                                "platform": "android",
                                "extensions": [".apk"],
                                "versioned_key": "Android/psygo.apk",
                                "latest_key": "dist/psygo.apk",
                            }
                        }
                    }
                },
            }
        )


def test_load_config_expands_environment_and_resolves_local_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARTIFACT_ROOT", "objects")
    config_path = tmp_path / "distkeeper.yaml"
    config_path.write_text(
        """
storage:
  driver: local
  root: ${ARTIFACT_ROOT}
repositories:
  psygo:
    targets:
      android:
        platform: android
        extensions: [.apk]
        versioned_key: Android/{artifact}-{version}{extension}
        latest_key: dist/{artifact}{extension}
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert isinstance(config.storage, LocalStorageConfig)
    assert config.storage.root == (tmp_path / "objects").resolve()
    assert config.safety.allowed_source_roots == ((tmp_path / "dist").resolve(),)
    assert config.safety.allowed_write_prefixes == ("dist", ".distkeeper")


def test_safety_rejects_unsafe_source_root() -> None:
    with pytest.raises(ValidationError, match="unsafe path"):
        AppConfig.model_validate(
            {
                "storage": {"driver": "local", "root": "objects"},
                "safety": {"allowed_source_roots": ["../outside"]},
                "repositories": {
                    "psygo": {
                        "targets": {
                            "android": {
                                "platform": "android",
                                "extensions": [".apk"],
                                "versioned_key": "dist/{artifact}-{version}{extension}",
                                "latest_key": "dist/{artifact}{extension}",
                            }
                        }
                    }
                },
            }
        )
