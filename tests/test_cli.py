from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from distkeeper.cli import app
from distkeeper.errors import SafetyError


def test_cli_publish_list_and_verify_json(tmp_path: Path) -> None:
    config_path = tmp_path / "distkeeper.yaml"
    config_path.write_text(
        f"""
storage:
  driver: local
  root: {tmp_path / "objects"}
safety:
  allowed_source_roots:
    - {tmp_path}
  allowed_write_prefixes:
    - Android
    - dist
    - .distkeeper
repositories:
  psygo:
    targets:
      android:
        platform: android
        extensions: [.apk]
        versioned_key: Android/{{artifact}}-{{version}}{{extension}}
        latest_key: dist/{{artifact}}{{extension}}
""".strip(),
        encoding="utf-8",
    )
    source = tmp_path / "psygo.apk"
    source.write_bytes(b"cli-release")
    runner = CliRunner()
    common = ["--config", str(config_path), "--json"]

    published = runner.invoke(
        app,
        [
            *common,
            "publish",
            str(source),
            "--repository",
            "psygo",
            "--target",
            "android",
            "--version",
            "1.0.0",
        ],
    )
    assert published.exit_code == 0, published.output
    assert json.loads(published.stdout)["version"] == "1.0.0"

    listed = runner.invoke(
        app,
        [*common, "list", "--repository", "psygo", "--target", "android"],
    )
    assert listed.exit_code == 0, listed.output
    assert [item["version"] for item in json.loads(listed.stdout)] == ["1.0.0"]

    tree = runner.invoke(app, [*common, "tree", "Android"])
    assert tree.exit_code == 0, tree.output
    tree_payload = json.loads(tree.stdout)
    assert tree_payload["prefix"] == "Android/"
    assert tree_payload["nodes"][0]["name"] == "psygo-1.0.0.apk"

    verified = runner.invoke(
        app,
        [*common, "verify", "--repository", "psygo", "--target", "android"],
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["ok"] is True


def test_cli_requires_explicit_confirmation_outside_source_scope(tmp_path: Path) -> None:
    config_path = tmp_path / "distkeeper.yaml"
    config_path.write_text(
        f"""
storage:
  driver: local
  root: {tmp_path / "objects"}
safety:
  allowed_source_roots:
    - {tmp_path / "dist"}
  allowed_write_prefixes:
    - dist
    - .distkeeper
repositories:
  psygo:
    targets:
      android:
        platform: android
        extensions: [.apk]
        versioned_key: dist/Android/{{artifact}}-{{version}}{{extension}}
        latest_key: dist/Android/{{artifact}}{{extension}}
""".strip(),
        encoding="utf-8",
    )
    source = tmp_path / "outside" / "psygo.apk"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"cli-release")
    runner = CliRunner()
    common = ["--config", str(config_path), "--json"]
    arguments = [
        "--repository",
        "psygo",
        "--target",
        "android",
        "--version",
        "1.0.0",
    ]

    planned = runner.invoke(app, [*common, "plan", str(source), *arguments])
    assert planned.exit_code == 0, planned.output
    plan_payload = json.loads(planned.stdout)
    assert plan_payload["requires_confirmation"] is True
    assert any("outside allowed source roots" in item for item in plan_payload["scope_violations"])

    blocked = runner.invoke(app, [*common, "publish", str(source), *arguments])
    assert blocked.exit_code != 0
    assert isinstance(blocked.exception, SafetyError)
    assert "confirm-outside-scope" in str(blocked.exception)

    confirmed = runner.invoke(
        app,
        [*common, "publish", str(source), *arguments, "--confirm-outside-scope"],
    )
    assert confirmed.exit_code == 0, confirmed.output
