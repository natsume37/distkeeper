from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from distkeeper.cli import app


def test_cli_publish_list_and_verify_json(tmp_path: Path) -> None:
    config_path = tmp_path / "distkeeper.yaml"
    config_path.write_text(
        f"""
storage:
  driver: local
  root: {tmp_path / "objects"}
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
