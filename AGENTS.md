# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.12+ CLI for versioning release artifacts in object storage.
Application code lives under `src/distkeeper/`: `service.py` contains publish,
adopt, rollback, and verification workflows; `config.py` validates YAML and path
templates; `directory.py` provides storage tree queries; and `storage/` contains
the provider protocol plus local and Alibaba Cloud OSS implementations. The CLI
entry point is `cli.py` (also available via `python -m distkeeper`). Tests are in
`tests/`, with one `test_*.py` file per major module. Use
`distkeeper.example.yaml` as the configuration template.

## Build, Test, and Development Commands

- `uv sync` — create or synchronize the locked development environment.
- `uv run pytest` — run the complete test suite.
- `uv run pytest --cov=distkeeper` — run tests with coverage reporting.
- `uv run ruff check .` — run lint checks; use `uv run ruff format .` to format.
- `uv run mypy src` — run strict static type checking.
- `uv run distkeeper --help` — inspect available CLI commands.
- `uv build` — build the distributable package.

For automation and AI callers, prefer `distkeeper --json ...` for
machine-readable results. Keep credentials in environment variables such as
`OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET`, and `OSS_BUCKET`; never place them
in YAML or commits.

## Safety Boundaries

Mutating commands use the `safety` section in YAML. By default, source files
must be under the config file's `dist/` directory, artifact keys must be under
`dist/`, and internal manifests may use `.distkeeper/`. Run `plan` first. If a
source or destination is outside the allowlist, the operation must stop until
the user explicitly approves a rerun with `--confirm-outside-scope`.

## Coding Style & Naming Conventions

Use four spaces, type annotations, and concise docstrings. Keep lines within the
100-character Ruff limit. Use `snake_case` for functions and variables,
`PascalCase` for classes, and descriptive exception types for domain failures.
Run Ruff and mypy before submitting changes. Preserve the storage-independent
service layer and validate every externally supplied object key or version.

## Testing Guidelines

Use pytest with test functions named `test_<behavior>` and files named
`tests/test_<area>.py`. Favor deterministic local-storage or in-memory fakes;
avoid real OSS calls in unit tests. Add regression coverage for new release,
configuration, and storage behavior. No fixed coverage threshold is enforced,
but changed logic should be covered.

## Commit & Pull Request Guidelines

Follow the existing Conventional Commit style (for example,
`feat: add release verification` or `fix: reject unsafe object keys`). Keep
commits focused. Pull requests should explain the behavior change, list test
and lint commands run, call out configuration or storage compatibility effects,
and include representative CLI/JSON output when the interface changes. Never
include secrets, generated environments, or unrelated files.
