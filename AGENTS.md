# AGENTS.md

Guidance for AI coding agents working in this repository. Human contributors should read [CONTRIBUTING.md](CONTRIBUTING.md) and the [docs/](docs/) folder.

## Project overview

**Advanced Notification System (ANS)** is a custom [Home Assistant](https://www.home-assistant.io/) integration distributed via [HACS](https://hacs.xyz/). It acts as a centralized notification hub: automations make a single `ans.send_notification` service call, and ANS handles recipient routing, channel selection, filtering (type/criticality/DnD/source blocking), rate limiting, retries, deduplication, and TTS volume control.

- **Domain:** `ans` — all code lives under [custom_components/ans/](custom_components/ans/).
- **Integration type:** `service`, `iot_class: local_push`, no external requirements.
- **Language/runtime:** Python (targets 3.12+; CI and the local venv run 3.13).
- This is a config-flow-only integration (no YAML config); setup and recipients are managed entirely through the HA UI.

## Project intent

These are maintainer-defined expectations, not things to infer from the code — follow them deliberately.

- **Posture: maintenance and stability-first, open to improvements.** The priority is keeping the integration reliable; bug fixes, simplifications, and refactors that reduce complexity are always welcome. New features are interesting and worth proposing/mentioning, but are not the highest priority — don't bolt on scope while fixing something.
- **Backward compatibility: the public API is a stable contract.** The `ans.send_notification` service schema and the emitted `ans_notification_*` bus events must not break without a deprecation path (mirror how the deprecated `metadata` field is still accepted). Internal models, helpers, and the storage format are free to change as needed.
- **Coverage is actively being raised.** New or changed code paths must come with tests — add them proactively, don't just preserve existing coverage. The aim is to increase overall coverage over time, not hold it flat.

## Rules

### Always

- **Run `scripts/lint` and `scripts/test` before declaring work done.** CI gates on ruff (`check` + `format --check`), hassfest, and HACS — match it locally.
- **Use the wrapper scripts** in [scripts/](scripts/), never bare `pytest`/`ruff`/`hass` — they activate `.venv` and set `PYTHONPATH` so the package resolves as `custom_components.ans.*`.
- **Add tests for every new or changed code path** (not just behavior changes), in the mirrored [tests/](custom_components/ans/tests/) subfolder — the project is actively raising coverage.
- **Use the factory helpers** (`make_payload`, `make_policy`, …) from [tests/conftest.py](custom_components/ans/tests/conftest.py) instead of hand-building models.
- **Reference constants from [const.py](custom_components/ans/const.py)** for event names (`EVENT_*`), storage filenames (`SYS_STORAGE_*`), config keys, and limits — never hardcode the literal strings.
- **Write a docstring** for every module, class, and function (ruff `D` rules are enforced).
- **Keep code async-safe** — no blocking I/O in the event loop (ruff `ASYNC*` rules enforce this); use HA's async helpers and executor jobs.
- **Update docs alongside code** — the matching page in [docs/](docs/) and the feature list in [README.md](README.md).
- **Keep `.internal/features/` in sync** — if you add, change, or remove any notable functionality, update (or create) the matching page in `.internal/features/<slug>.md` in the same change: adjust Behavior/Configuration/Code References and append a Changelog entry. For a brand-new feature, create its page first (`Status: Planned`) before writing code, then flip it to `Active` and reconcile the Changelog once it ships. See `.internal/features/TEMPLATE.md`.
- **Keep UI strings in sync** — when changing the `send_notification` service schema, update [services.yaml](custom_components/ans/services.yaml); when changing the config/options/recipient flow, update [translations/en.json](custom_components/ans/translations/en.json) (and `de.json`/`fr.json` where possible). Both are hassfest-validated.
- **Bump the version in both places together** — `version` in [manifest.json](custom_components/ans/manifest.json) and `VERSION` in [const.py](custom_components/ans/const.py).

### Never

- **Never commit secrets or credentials** — no tokens, API keys, passwords, or webhook URLs in code, tests, docs, or commit messages. `config/secrets.yaml` and the rest of `config/` are gitignored; keep secrets there and reference them, never inline.
- **Never commit the `config/` runtime** (`.storage/`, logs, `home-assistant_v2.db*`) or local artifacts (`.venv/`, `.coverage`, caches). Only `config/configuration.yaml` is tracked.
- **Never hardcode storage paths or filenames** — they follow the `ans.*` dot-separated convention via `SYS_STORAGE_*` constants.
- **Never add `# noqa` or disable lint rules** to get past CI; fix the underlying issue. (`RUF100` flags stale `noqa`s.)
- **Never reintroduce Black or other formatters** — ruff is the single source of truth, regardless of older CONTRIBUTING.md wording.
- **Never add runtime dependencies** without updating `manifest.json` `requirements` and confirming hassfest still passes — this integration currently ships with zero external requirements.
- **Never introduce blocking calls** (`time.sleep`, sync file/HTTP I/O, subprocess) in async code paths.
- **Never edit `models/__init__.py` re-exports casually** — they exist for backward-compatible imports (`from .models import ...`); keep them in sync with the submodules.
- **Never break the public API without a deprecation path** — the `ans.send_notification` service schema and `ans_notification_*` events are a stable contract (see Project intent). Internals may change freely.

## Architecture

The integration is organized into subpackages by responsibility:

| Package | Responsibility |
|---|---|
| [custom_components/ans/__init__.py](custom_components/ans/__init__.py) | Integration bootstrap: `async_setup_entry`/`async_unload_entry`, event wiring, persistence init |
| [config_flow.py](custom_components/ans/config_flow.py), [config/](custom_components/ans/config/) | Config & options flows, recipient flow, forms, validation, config repository |
| [models/](custom_components/ans/models/) | Dataclasses/enums for payloads, recipients, policies, channels, delivery state. Re-exported via [models/__init__.py](custom_components/ans/models/__init__.py) |
| [delivery/](custom_components/ans/delivery/) | Delivery pipeline: `orchestrator`, `processor`, `queue`, `filter_engine`, `rate_limiter`, `retry_scheduler`, `deduplication`, `factory` (`create_system`/`ANSSystem`) |
| [channels/](custom_components/ans/channels/) | Channel adapters behind a common `base` interface: `mobile_app`, `signal`, `persistent_notification`, `tts_mediaplayer`; coordinated by `channel_manager` |
| [persistence/](custom_components/ans/persistence/) | Storage layer: `base`, `file`, `memory`, `recovery`, `housekeeping`, `volume_restoration` |
| [service.py](custom_components/ans/service.py) | Registers and handles the `ans.send_notification` service |
| [const.py](custom_components/ans/const.py) | Domain, event names, config keys, limits/defaults, storage filenames |

For the end-to-end delivery flow (the 9 pipeline stages, deduplication, acknowledgement tracking, crash recovery) read [docs/how-it-works.md](docs/how-it-works.md) and [docs/advanced.md](docs/advanced.md) before changing delivery or persistence code.

## Setup

The dev environment is a VS Code devcontainer with a standalone Home Assistant instance ([.devcontainer.json](.devcontainer.json)). The Python venv lives at `.venv/`.

```bash
scripts/setup     # create .venv and install requirements.txt
```

## Build, lint, and test commands

Always use the wrapper scripts in [scripts/](scripts/) — they activate `.venv` and set `PYTHONPATH` to include `custom_components/` (the integration is imported as `custom_components.ans.*`).

```bash
scripts/test                 # run the full pytest suite
scripts/test -k test_name    # run a subset (args are forwarded to pytest)
scripts/test path/to/test.py # run a specific test file
scripts/lint                 # ruff format . && ruff check . --fix
scripts/develop              # start Home Assistant with this integration loaded
```

CI runs the equivalent of `ruff check .` and `ruff format . --check` ([.github/workflows/lint.yml](.github/workflows/lint.yml)), plus Home Assistant **hassfest** and **HACS** validation ([.github/workflows/validate.yml](.github/workflows/validate.yml)). Make sure lint passes and hassfest-relevant files (`manifest.json`, `services.yaml`, translations) stay valid.

## Testing conventions

- Tests live in [custom_components/ans/tests/](custom_components/ans/tests/), mirroring the package layout (`tests/channels/`, `tests/delivery/`, `tests/persistence/`, etc.).
- `pytest` is configured in [pytest.ini](pytest.ini) with `asyncio_mode = auto` — async test functions need no `@pytest.mark.asyncio` decorator.
- Shared fixtures and factory helpers (`make_payload`, `make_dnd`, `make_policy`, `make_channel_info`, …) are in [tests/conftest.py](custom_components/ans/tests/conftest.py). Prefer these factories over hand-building model instances; pass overrides as kwargs.
- Home Assistant and external services are mocked (`unittest.mock` `AsyncMock`/`MagicMock`); there is no live HA in unit tests.
- Add or update tests for any behavior change. New modules should get a matching `test_*.py`.

## Code style

- Linting and formatting are **ruff only** (config in [.ruff.toml](.ruff.toml), mirrored from Home Assistant core). There is no separate Black step despite older mentions in CONTRIBUTING.md — run `scripts/lint`.
- Key enabled rule groups: docstrings (`D`), isort (`I`), pyupgrade-style typing, complexity (`C`, max-complexity 25), pylint (`PL`), async-safety (`ASYNC`*), logging format (`G`/`LOG`). `from __future__ import annotations` is used throughout.
- **Every module, class, and function needs a docstring** (ruff `D` rules are on).
- Follow Home Assistant integration conventions: async-first (no blocking I/O in the event loop — `ASYNC` rules enforce this), structured logging, and `_LOGGER = logging.getLogger(__name__)`.
- `tests/*` are exempt from `SLF001` (private-member access) only.

## Conventions specific to this repo

- **Persistence filenames** follow HA's dot-separated `ans.*` convention (e.g. `ans.notifications`, `ans.acknowledgements`); filenames are defined as `SYS_STORAGE_*` constants in [const.py](custom_components/ans/const.py). Use the constants, never hardcode paths.
- **Event names** are `ans_notification_*` (delivered/filtered/failed/rate_limited/settled/acknowledged), defined as `EVENT_*` constants in [const.py](custom_components/ans/const.py). Reference the constants.
- **Channel adapters** must implement the [channels/base.py](custom_components/ans/channels/base.py) interface and be registered through `channel_manager`; see [docs/advanced.md](docs/advanced.md) extension points before adding one.
- Bump `version` in both [manifest.json](custom_components/ans/manifest.json) and `VERSION` in [const.py](custom_components/ans/const.py) together when releasing.
- When you change behavior, update the relevant docs in [docs/](docs/) and the feature list in [README.md](README.md) — contributor guidelines require docs to track code changes.

## Observability

These patterns are already consistently applied — follow them so new code stays debuggable through the HA logger and the diagnostics panel.

- **One logger per module, always `_LOGGER = logging.getLogger(__name__)`** — never pass loggers around or use a shared one.
- **Log significant operations at `INFO`, internal state at `DEBUG`, recoverable problems at `WARNING`, unexpected failures at `ERROR`/`EXCEPTION`.** Match the existing style: `_LOGGER.info("[ANS setup] Phase N/5 — …")` for lifecycle phases, `_LOGGER.debug(…, value)` with `%s`-style placeholders (not f-strings — ruff `G` rules enforce this).
- **Fire the appropriate `EVENT_*` bus event for every delivery outcome.** The six events (`ans_notification_delivered/filtered/failed/rate_limited/settled/acknowledged`) are what HA automations use to react to delivery results. A new code path that terminates without firing an event silently breaks automation integrations.
- **Keep the diagnostics panel accurate.** [diagnostics.py](custom_components/ans/diagnostics.py) exposes channel health, system config, and recipient counts — if you add a new subsystem or structural setting, add it there too. **Never include PII** (contact details, message content, user names) in diagnostics output.
- **Mark exceptions as permanent or transient.** `TTSDeliveryError` (and subclasses) carry `is_permanent: bool` — the retry scheduler uses this to decide whether to retry. New delivery exceptions should follow the same pattern; a missing flag defaults to no-retry and silently swallows transient failures.

## Maintainability

- **Prefer extracting over growing large files.** The largest modules ([tts_mediaplayer.py](custom_components/ans/channels/tts_mediaplayer.py) at ~1130 lines, [__init__.py](custom_components/ans/__init__.py) at ~1074 lines) are already at the edge — add to them only when strictly necessary; otherwise split out a new module.
- **New exceptions belong in [exceptions.py](custom_components/ans/exceptions.py)** and must slot into the existing hierarchy (`ANSException → ANSConfigError → specific`, or `TTSDeliveryError` for delivery failures). Don't raise bare `Exception` or `HomeAssistantError` for ANS-specific failure modes.
- **Keep ruff's max-complexity (25) as a ceiling, not a target.** Functions approaching that limit are a signal to refactor, not a budget to spend.
- **Internal helpers belong in [helper.py](custom_components/ans/helper.py).** UI/form helpers, URL validators, label formatters — keep them there, documented, and reused rather than re-implemented inline.

## Git & PR conventions

- **Branch from `main`.** Name branches `feature/<short_snake_case>` for new behavior or `bugfix/<short_snake_case>` for fixes (e.g. `feature/queue_depth_limit_and_backpressure`, `bugfix/media_player_off_handling`).
- **Commit subjects** are capitalized, imperative, and descriptive (e.g. `Fix notification acknowledgment tracking on mobile`). Prefix tooling/dependency commits with `Dev:` (e.g. `Dev: Update GitHub action versions`).
- **A PR should pass lint + tests + hassfest + HACS**, update affected docs, and bump the version when releasing. Keep PRs focused on a single feature or fix.

## What not to touch

- `config/` is a local Home Assistant runtime instance (gitignored except `configuration.yaml`). Do not commit `config/.storage/`, logs, or the SQLite DB.
- `.venv/`, `.ruff_cache/`, `.pytest_cache/`, `.coverage` are local artifacts (gitignored).
- `.internal/` (feature specs, release-note drafts, prompt templates, audit reports) is gitignored local tooling — not part of the shipped integration.
