"""Command-line interface for distkeeper."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from distkeeper.config import AppConfig, load_config
from distkeeper.directory import DirectoryListing, DirectoryNode, DirectoryService
from distkeeper.domain import OperationPlan, ReleaseManifest, ReleaseStatus, VerificationReport
from distkeeper.errors import DistkeeperError
from distkeeper.service import ReleaseService
from distkeeper.storage.base import Storage
from distkeeper.storage.factory import create_storage

app = typer.Typer(
    name="distkeeper",
    help="Version and promote release artifacts without hard-coded storage paths.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)


@dataclass(frozen=True, slots=True)
class CliState:
    config_path: Path
    json_output: bool


@app.callback()
def configure_cli(
    ctx: typer.Context,
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            envvar="DISTKEEPER_CONFIG",
            help="YAML configuration path.",
        ),
    ] = Path("distkeeper.yaml"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    ctx.obj = CliState(config_path=config, json_output=json_output)


@app.command()
def plan(
    ctx: typer.Context,
    source: Annotated[Path, typer.Argument(help="Local artifact to publish.")],
    repository: Annotated[str, typer.Option("--repository", "-r")],
    target: Annotated[str, typer.Option("--target", "-t")],
    version: Annotated[str, typer.Option("--version", "-v")],
    channel: Annotated[str, typer.Option("--channel")] = "stable",
) -> None:
    """Preview a publish without changing storage."""
    state, service = _service(ctx)
    result = service.plan_publish(
        source,
        repository=repository,
        target=target,
        version=version,
        channel=channel,
    )
    _emit_plan(result, state)


@app.command()
def publish(
    ctx: typer.Context,
    source: Annotated[Path, typer.Argument(help="Local artifact to publish.")],
    repository: Annotated[str, typer.Option("--repository", "-r")],
    target: Annotated[str, typer.Option("--target", "-t")],
    version: Annotated[str, typer.Option("--version", "-v")],
    channel: Annotated[str, typer.Option("--channel")] = "stable",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and preview without writing.")
    ] = False,
    confirm_outside_scope: Annotated[
        bool,
        typer.Option(
            "--confirm-outside-scope",
            help="Confirm a source or destination outside the configured allowlist.",
        ),
    ] = False,
    plan_id: Annotated[
        str | None,
        typer.Option("--plan-id", help="Apply only the exact plan returned by plan."),
    ] = None,
    expected_current_version: Annotated[
        str | None,
        typer.Option("--expected-current-version", help="Abort if latest changed."),
    ] = None,
) -> None:
    """Publish an immutable version and update its fixed latest path."""
    state, service = _service(ctx)
    if dry_run:
        result = service.plan_publish(
            source,
            repository=repository,
            target=target,
            version=version,
            channel=channel,
        )
        _emit_plan(result, state)
        return
    manifest = service.publish(
        source,
        repository=repository,
        target=target,
        version=version,
        channel=channel,
        confirm_outside_scope=confirm_outside_scope,
        plan_id=plan_id,
        expected_current_version=expected_current_version,
    )
    _emit_manifest(manifest, state, heading="Published")


@app.command()
def adopt(
    ctx: typer.Context,
    repository: Annotated[str, typer.Option("--repository", "-r")],
    target: Annotated[str, typer.Option("--target", "-t")],
    version: Annotated[str, typer.Option("--version", "-v")],
    channel: Annotated[str, typer.Option("--channel")] = "stable",
    extension: Annotated[
        str | None,
        typer.Option("--extension", help="Existing latest artifact extension, such as .apk."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and preview without writing.")
    ] = False,
    confirm_outside_scope: Annotated[
        bool,
        typer.Option(
            "--confirm-outside-scope",
            help="Confirm a destination outside the configured allowlist.",
        ),
    ] = False,
) -> None:
    """Archive an existing latest artifact as its first managed version."""
    state, service = _service(ctx)
    if dry_run:
        result = service.plan_adopt(
            repository=repository,
            target=target,
            version=version,
            channel=channel,
            extension=extension,
        )
        _emit_plan(result, state)
        return
    manifest = service.adopt(
        repository=repository,
        target=target,
        version=version,
        channel=channel,
        confirm_outside_scope=confirm_outside_scope,
        extension=extension,
    )
    _emit_manifest(manifest, state, heading="Adopted")


@app.command("list")
def list_releases_command(
    ctx: typer.Context,
    repository: Annotated[str, typer.Option("--repository", "-r")],
    target: Annotated[str, typer.Option("--target", "-t")],
    channel: Annotated[str, typer.Option("--channel")] = "stable",
    limit: Annotated[int, typer.Option("--limit", min=1)] = 50,
) -> None:
    """List immutable releases for one repository target."""
    state, service = _service(ctx)
    releases = service.list_releases(
        repository=repository,
        target=target,
        channel=channel,
    )[:limit]
    if state.json_output:
        _print_json([item.model_dump(mode="json") for item in releases])
        return
    table = Table(title=f"{repository} / {target} / {channel}")
    table.add_column("Version", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("SHA-256")
    table.add_column("Published")
    table.add_column("Origin")
    for release in releases:
        table.add_row(
            release.version,
            str(release.artifact.size),
            release.artifact.sha256[:12],
            release.published_at.isoformat(),
            release.origin,
        )
    console.print(table)


@app.command()
def status(
    ctx: typer.Context,
    repository: Annotated[str, typer.Option("--repository", "-r")],
    target: Annotated[str, typer.Option("--target", "-t")],
    channel: Annotated[str, typer.Option("--channel")] = "stable",
) -> None:
    """Show the active release and the best available rollback candidate."""
    state, service = _service(ctx)
    release_status = service.status(
        repository=repository,
        target=target,
        channel=channel,
    )
    if state.json_output:
        _print_json(release_status.model_dump(mode="json"))
        return
    _emit_status(release_status)


@app.command("tree")
def directory_tree_command(
    ctx: typer.Context,
    prefix: Annotated[
        str,
        typer.Argument(help="Logical directory prefix, such as Android or dist."),
    ] = "",
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            max=100_000,
            help="Maximum number of storage objects to scan.",
        ),
    ] = 1000,
) -> None:
    """Query the object storage directory structure."""
    state, _, storage = _runtime(ctx)
    listing = DirectoryService(storage).query(prefix, limit=limit)
    _emit_directory_tree(listing, state)


@app.command()
def rollback(
    ctx: typer.Context,
    repository: Annotated[str, typer.Option("--repository", "-r")],
    target: Annotated[str, typer.Option("--target", "-t")],
    version: Annotated[str, typer.Option("--version", "-v")],
    channel: Annotated[str, typer.Option("--channel")] = "stable",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and preview without writing.")
    ] = False,
    confirm_outside_scope: Annotated[
        bool,
        typer.Option(
            "--confirm-outside-scope",
            help="Confirm a destination outside the configured allowlist.",
        ),
    ] = False,
    plan_id: Annotated[
        str | None,
        typer.Option("--plan-id", help="Apply only the exact plan returned by --dry-run."),
    ] = None,
    expected_current_version: Annotated[
        str | None,
        typer.Option("--expected-current-version", help="Abort if latest changed."),
    ] = None,
) -> None:
    """Point the fixed latest path at a previous immutable version."""
    state, service = _service(ctx)
    if dry_run:
        result = service.plan_rollback(
            repository=repository,
            target=target,
            version=version,
            channel=channel,
        )
        _emit_plan(result, state)
        return
    manifest = service.rollback(
        repository=repository,
        target=target,
        version=version,
        channel=channel,
        confirm_outside_scope=confirm_outside_scope,
        plan_id=plan_id,
        expected_current_version=expected_current_version,
    )
    _emit_manifest(manifest, state, heading="Rolled back")


@app.command()
def verify(
    ctx: typer.Context,
    repository: Annotated[str, typer.Option("--repository", "-r")],
    target: Annotated[str, typer.Option("--target", "-t")],
    channel: Annotated[str, typer.Option("--channel")] = "stable",
    version: Annotated[
        str | None,
        typer.Option("--version", "-v", help="Verify one immutable version; default is latest."),
    ] = None,
    full_hash: Annotated[
        bool,
        typer.Option(
            "--full-hash/--metadata-only",
            help="Download and hash artifacts, or only inspect size and stored metadata.",
        ),
    ] = True,
) -> None:
    """Verify artifact size and SHA-256 against its manifest."""
    state, service = _service(ctx)
    report = service.verify(
        repository=repository,
        target=target,
        channel=channel,
        version=version,
        full_hash=full_hash,
    )
    _emit_verification(report, state)
    if not report.ok:
        raise typer.Exit(code=2)


def _service(ctx: typer.Context) -> tuple[CliState, ReleaseService]:
    state, config, storage = _runtime(ctx)
    return state, ReleaseService(config, storage)


def _runtime(ctx: typer.Context) -> tuple[CliState, AppConfig, Storage]:
    state = ctx.ensure_object(CliState)
    config = load_config(state.config_path)
    return state, config, create_storage(config.storage)


def _emit_plan(plan_result: OperationPlan, state: CliState) -> None:
    if state.json_output:
        _print_json(
            {
                "schema_version": 1,
                "operation": plan_result.operation,
                "repository": plan_result.repository,
                "target": plan_result.target,
                "version": plan_result.version,
                "plan_id": plan_result.plan_id,
                "actions": plan_result.actions,
                "scope_violations": plan_result.scope_violations,
                "requires_confirmation": plan_result.requires_confirmation,
                "expected_current_version": plan_result.expected_current_version,
                "expected_current_etag": plan_result.expected_current_etag,
                "source_sha256": plan_result.source_sha256,
                "source_size": plan_result.source_size,
            }
        )
        return
    console.print(
        f"[bold]{plan_result.operation}[/bold] "
        f"{plan_result.repository}/{plan_result.target}@{plan_result.version}"
    )
    for index, action in enumerate(plan_result.actions, start=1):
        console.print(f"  {index}. {action}")
    if plan_result.scope_violations:
        console.print("[yellow]Confirmation required for out-of-scope paths:[/yellow]")
        for violation in plan_result.scope_violations:
            console.print(f"  - {violation}")


def _emit_manifest(manifest: ReleaseManifest, state: CliState, *, heading: str) -> None:
    if state.json_output:
        _print_json(manifest.model_dump(mode="json"))
        return
    table = Table(title=heading, show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Release", f"{manifest.repository}/{manifest.target}@{manifest.version}")
    table.add_row("Channel", manifest.channel)
    table.add_row("Versioned", manifest.artifact.key)
    table.add_row("Latest", manifest.artifact.latest_key)
    table.add_row("Size", str(manifest.artifact.size))
    table.add_row("SHA-256", manifest.artifact.sha256)
    console.print(table)


def _emit_verification(report: VerificationReport, state: CliState) -> None:
    if state.json_output:
        _print_json(
            {
                "ok": report.ok,
                "release": report.manifest.model_dump(mode="json"),
                "objects": [
                    {
                        "key": item.key,
                        "exists": item.exists,
                        "size_matches": item.size_matches,
                        "sha256_matches": item.sha256_matches,
                        "ok": item.ok,
                    }
                    for item in report.items
                ],
            }
        )
        return
    table = Table(title=f"Verification: {'OK' if report.ok else 'FAILED'}")
    table.add_column("Object")
    table.add_column("Exists")
    table.add_column("Size")
    table.add_column("SHA-256")
    for item in report.items:
        table.add_row(
            item.key,
            _status(item.exists),
            _status(item.size_matches),
            "not checked" if item.sha256_matches is None else _status(item.sha256_matches),
        )
    console.print(table)


def _emit_status(release_status: ReleaseStatus) -> None:
    table = Table(
        title=f"Status: {release_status.repository} / {release_status.target} / "
        f"{release_status.channel}"
    )
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("State", release_status.state)
    table.add_row(
        "Current",
        release_status.current.version if release_status.current is not None else "none",
    )
    table.add_row(
        "Previous",
        release_status.previous.version if release_status.previous is not None else "none",
    )
    table.add_row("Available", ", ".join(release_status.available_versions) or "none")
    console.print(table)


def _emit_directory_tree(listing: DirectoryListing, state: CliState) -> None:
    if state.json_output:
        _print_json(listing.model_dump(mode="json"))
        return
    label = "/" if not listing.prefix else f"/{listing.prefix}"
    tree = Tree(Text(label, style="bold magenta"))
    for node in listing.nodes:
        _add_directory_node(tree, node)
    console.print(tree)
    summary = f"{listing.objects_returned} object(s)"
    if listing.truncated:
        summary += " (truncated; increase --limit to continue)"
    console.print(summary, style="yellow" if listing.truncated else "dim")


def _add_directory_node(parent: Tree, node: DirectoryNode) -> None:
    is_directory = node.kind in {"directory", "object_and_directory"}
    label = Text(f"{node.name}/" if is_directory else node.name)
    label.stylize("bold blue" if is_directory else "green")
    if node.size is not None:
        label.append(f" ({_format_size(node.size)})", style="dim")
    branch = parent.add(label)
    for child in node.children:
        _add_directory_node(branch, child)


def _format_size(size: int) -> str:
    value = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _print_json(value: object) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False))


def _status(value: bool) -> str:
    return "OK" if value else "FAILED"


def _exit_with_error(error: DistkeeperError, *, json_output: bool = False) -> NoReturn:
    if json_output:
        payload = {
            "schema_version": 1,
            "ok": False,
            "error": {
                "code": error.code,
                "message": str(error),
                "retryable": error.retryable,
            },
        }
        error_console.print_json(json.dumps(payload, ensure_ascii=False))
        raise SystemExit(1)
    error_console.print(f"[red]error:[/red] {error}")
    raise SystemExit(1)


def main() -> None:
    try:
        app()
    except DistkeeperError as exc:
        _exit_with_error(exc, json_output="--json" in sys.argv[1:])
