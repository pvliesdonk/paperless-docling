# Parser And Consumption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a Paperless-ngx 3.1 parser plugin that converts workflow-selected raster documents to Docling Markdown during consumption while preserving originals and Paperless archive behavior.

**Architecture:** A dependency-light `paperless-docling` distribution implements the Paperless parser entry point and calls Docling Serve's asynchronous v1 API. A persistent content-addressed cache resumes interrupted Docling tasks, while an adapter composes Paperless's raster parser only when an archive is requested.

**Tech Stack:** Python 3.14, uv workspace, hatchling, Paperless-bundled httpx, stdlib `fcntl`, Paperless-ngx 3.1 parser protocol, pytest, testcontainers/Compose integration tests.

**Spec:** `docs/superpowers/specs/2026-08-27-paperless-docling-design.md`

## Global Constraints

- Support Paperless-ngx `>=3.1,<3.2` initially; never broaden the range without compatibility tests.
- Support Python `>=3.14,<3.15` in the parser distribution.
- Do not install Docling or model runtimes in the Paperless container.
- Declare `uses_remote_service = True` and fail selected parsing instead of silently falling back.
- Never modify the source path passed by Paperless.
- Keep original and derived archive responsibilities separate.
- All cache writes must be atomic and all duplicate suppression must be safe across Paperless worker processes.
- The `--no-deps` installation path may use only the Python standard library and dependencies whose compatible presence is proven in every supported official Paperless image.
- Use TDD for every behavior and commit only after the focused tests pass.

---

## Planned File Structure

```text
pyproject.toml                              # uv workspace and shared tooling
packages/parser/pyproject.toml              # PyPI metadata and parser entry point
packages/parser/src/paperless_docling/
  __init__.py                               # package version export
  compatibility.py                         # Paperless version gate
  config.py                                # immutable plugin settings
  errors.py                                # plugin-specific error taxonomy
  docling_client.py                        # async-task HTTP protocol
  cache.py                                 # content-addressed task/result cache
  raster.py                                # Paperless raster-parser adapter
  parser.py                                # Paperless parser protocol implementation
tests/parser/unit/                         # dependency-isolated unit tests
tests/parser/integration/                  # real Paperless/Docling contract tests
tests/fixtures/                             # born-digital, scan, and image fixtures
```

## Task 1: Establish The Parser Package And Compatibility Gate

**Files:**
- Create: `pyproject.toml`
- Create: `packages/parser/pyproject.toml`
- Create: `packages/parser/src/paperless_docling/__init__.py`
- Create: `packages/parser/src/paperless_docling/compatibility.py`
- Create: `packages/parser/src/paperless_docling/parser.py`
- Test: `tests/parser/unit/test_compatibility.py`
- Test: `tests/parser/unit/test_parser_contract.py`

**Interfaces:**
- Produces: `ensure_paperless_compatible(*, allow_unsupported: bool) -> None`
- Produces: `DoclingParser` registered as `paperless_docling.parser:DoclingParser`
- Consumes later: `PluginConfig`, `DoclingClient`, `ConversionCache`, and `RasterAdapter` constructor dependencies.

- [ ] **Step 1: Write failing compatibility tests**

```python
def test_rejects_next_paperless_minor(monkeypatch):
    monkeypatch.setattr(compatibility, "paperless_version", lambda: "3.2.0")
    with pytest.raises(IncompatiblePaperlessError, match="3.2.0"):
        ensure_paperless_compatible(allow_unsupported=False)

def test_override_allows_staging_probe(monkeypatch):
    monkeypatch.setattr(compatibility, "paperless_version", lambda: "3.2.0")
    ensure_paperless_compatible(allow_unsupported=True)
```

- [ ] **Step 2: Run the compatibility tests and verify they fail**

Run: `uv run pytest tests/parser/unit/test_compatibility.py -v`

Expected: FAIL because `compatibility.py` and its error type do not exist.

- [ ] **Step 3: Implement the exact version gate**

Read `paperless.version.__version__`, parse it with the already available `packaging.version.Version`, and accept only `3.1.x` unless the explicit override is true. Include running Paperless and plugin versions in the exception without reading any unrelated environment values.

- [ ] **Step 4: Write failing parser identity and selection tests**

```python
def test_parser_declares_remote_service():
    assert DoclingParser.uses_remote_service is True
    assert DoclingParser.supported_mime_types()["application/pdf"] == ".pdf"
    assert DoclingParser.score("application/pdf", "scan.pdf") > 10

def test_parser_declines_unknown_mime_type():
    assert DoclingParser.score("application/zip", "archive.zip") is None
```

- [ ] **Step 5: Implement the protocol skeleton and entry point**

Define all Paperless parser methods with correct signatures. Methods that need later collaborators should raise `NotImplementedError` in this task; identity, supported MIME types, scoring, context-manager cleanup, and compatibility checks must work now.

Declare in `packages/parser/pyproject.toml`:

```toml
[project.entry-points."paperless_ngx.parsers"]
docling = "paperless_docling.parser:DoclingParser"
```

- [ ] **Step 6: Run focused package tests**

Run: `uv run pytest tests/parser/unit/test_compatibility.py tests/parser/unit/test_parser_contract.py -v`

Expected: PASS.

- [ ] **Step 7: Build and inspect the wheel**

Run: `uv build --package paperless-docling`

Expected: a Python 3 wheel containing the entry-point metadata and no Docling dependency.

- [ ] **Step 8: Commit the package foundation**

```bash
git add pyproject.toml packages/parser tests/parser/unit
git commit -m "feat: establish Paperless parser package"
```

## Task 2: Implement Validated Plugin Configuration

**Files:**
- Create: `packages/parser/src/paperless_docling/config.py`
- Create: `packages/parser/src/paperless_docling/errors.py`
- Test: `tests/parser/unit/test_config.py`

**Interfaces:**
- Produces: `PluginConfig.from_environment(environ: Mapping[str, str] = os.environ) -> PluginConfig`
- Produces immutable fields: `serve_url`, `token_file`, `preset`, `profile_version`, `poll_interval`, `deadline`, `cache_dir`, `cache_max_age`, `cache_max_bytes`, `allow_unsupported_paperless`.
- Consumed by: `DoclingClient`, `ConversionCache`, and `DoclingParser`.

- [ ] **Step 1: Write failing configuration tests**

Cover required URL, required profile version, HTTP(S)-only URL schemes, readable token file, positive bounded durations, positive cache byte limit, absolute writable cache path, defaults, and secret-safe exception text.

```python
def test_error_never_contains_token_value(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("highly-secret")
    env = valid_env(token_file=secret, deadline="invalid")
    with pytest.raises(ConfigurationError) as error:
        PluginConfig.from_environment(env)
    assert "highly-secret" not in str(error.value)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/parser/unit/test_config.py -v`

Expected: FAIL because `PluginConfig` is undefined.

- [ ] **Step 3: Implement a frozen configuration dataclass**

Read the token once from its file, strip one trailing newline, and keep its representation redacted. Parse durations into `datetime.timedelta`. Reject unknown URL schemes and relative cache paths before constructing network clients.

- [ ] **Step 4: Run tests and verify success**

Run: `uv run pytest tests/parser/unit/test_config.py -v`

Expected: PASS.

- [ ] **Step 5: Commit configuration validation**

```bash
git add packages/parser/src/paperless_docling/config.py packages/parser/src/paperless_docling/errors.py tests/parser/unit/test_config.py
git commit -m "feat: validate parser configuration"
```

## Task 3: Implement The Docling Serve Task Client

**Files:**
- Create: `packages/parser/src/paperless_docling/docling_client.py`
- Test: `tests/parser/unit/test_docling_client.py`

**Interfaces:**
- Produces: `submit(path: Path) -> DoclingTask`
- Produces: `poll(task_id: str) -> TaskSnapshot`
- Produces: `result(task_id: str) -> str`
- Produces value types: `DoclingTask(task_id, submitted_at)`, `TaskSnapshot(state, detail)`, and enum `TaskState`.
- Consumed by: `ConversionCache` orchestration in `DoclingParser`.

- [ ] **Step 1: Write failing request-shape tests with `httpx.MockTransport`**

Assert multipart submission to `/v1/convert/file/async`, preset/profile options, bearer header behavior, status normalization, result Markdown extraction, response-size limit, and no redirects.

- [ ] **Step 2: Run the client tests and verify failure**

Run: `uv run pytest tests/parser/unit/test_docling_client.py -v`

Expected: FAIL because the client types do not exist.

- [ ] **Step 3: Implement typed submit, poll, and result operations**

Use one injected `httpx.Client`. Convert HTTP, JSON, schema, and task errors into the narrow error taxonomy: `TransientDoclingError`, `PermanentDoclingError`, `TaskMissingError`, and `InvalidDoclingResultError`.

- [ ] **Step 4: Add malformed and unknown-state tests**

Unknown terminal states and successful results without non-empty Markdown must fail closed. Error messages may include status and sanitized server detail, never response authorization data or submitted bytes.

- [ ] **Step 5: Run tests and verify success**

Run: `uv run pytest tests/parser/unit/test_docling_client.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the Docling client**

```bash
git add packages/parser/src/paperless_docling/docling_client.py tests/parser/unit/test_docling_client.py
git commit -m "feat: add Docling Serve task client"
```

## Task 4: Add Resumable Content-Addressed Conversion State

**Files:**
- Create: `packages/parser/src/paperless_docling/cache.py`
- Test: `tests/parser/unit/test_cache.py`
- Test: `tests/parser/unit/test_conversion_resume.py`

**Interfaces:**
- Produces: `conversion_key(source_hash: str, profile_version: str, schema_version: int) -> str`
- Produces: `ConversionCache.locked(key: str) -> ContextManager[CacheEntry]`
- Produces: `CacheEntry.load_state()`, `save_task()`, `save_result()`, `read_result()`, `clear()`, and `mark_successful_exit()`.
- Consumes: `DoclingClient` task/value types.

- [ ] **Step 1: Write failing atomic-state and lock tests**

Test deterministic keys, restrictive permissions, temp-file replacement, corrupt JSON quarantine, same-key mutual exclusion, different-key concurrency, and result UTF-8 validation.

- [ ] **Step 2: Run cache tests and verify failure**

Run: `uv run pytest tests/parser/unit/test_cache.py -v`

Expected: FAIL because cache classes do not exist.

- [ ] **Step 3: Implement the cache using stdlib `fcntl.flock()` and atomic `Path.replace()`**

Store only schema/profile/source identity, task metadata, and Markdown. Never pickle data. Create directories with mode `0700` and files with mode `0600` where supported. Cleanup must enforce age first, then evict oldest unlocked terminal entries until total bytes are within `cache_max_bytes`.

- [ ] **Step 4: Write failing resume orchestration tests**

Cover new submission, resume known task, transient poll retry within deadline, completed result reuse, one resubmission after missing task, rejection of a second missing task, and preserved cache after downstream exception.

- [ ] **Step 5: Implement conversion orchestration in a focused helper**

Add `resolve_markdown(client, cache_entry, source, deadline, poll_interval) -> str`. Inject clock and sleeper callables so tests never sleep.

- [ ] **Step 6: Run cache and resume tests**

Run: `uv run pytest tests/parser/unit/test_cache.py tests/parser/unit/test_conversion_resume.py -v`

Expected: PASS.

- [ ] **Step 7: Commit resumable conversion state**

```bash
git add packages/parser/src/paperless_docling/cache.py tests/parser/unit/test_cache.py tests/parser/unit/test_conversion_resume.py
git commit -m "feat: resume interrupted Docling conversions"
```

## Task 5: Compose Paperless Archive And Metadata Behavior

**Files:**
- Create: `packages/parser/src/paperless_docling/raster.py`
- Modify: `packages/parser/src/paperless_docling/parser.py`
- Test: `tests/parser/unit/test_raster.py`
- Test: `tests/parser/unit/test_parser.py`

**Interfaces:**
- Produces: `RasterAdapter.parse(source: Path, mime_type: str, produce_archive: bool) -> RasterResult`
- Produces: `RasterResult(archive_path, thumbnail_path, page_count)`.
- `DoclingParser.get_text() -> str` always returns Docling Markdown.
- `DoclingParser.get_archive_path() -> Path | None` returns only the raster result.

- [ ] **Step 1: Write failing raster-adapter tests**

Use a fake `RasterisedDocumentParser` to assert `produce_archive=False` skips OCR parsing, `produce_archive=True` delegates once, plain text is discarded, and all temporary delegates close after success or failure.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/parser/unit/test_raster.py -v`

Expected: FAIL because `RasterAdapter` is undefined.

- [ ] **Step 3: Implement the narrow Paperless adapter**

Keep every import of Paperless raster internals inside `raster.py`. This file is the only compatibility boundary for `RasterisedDocumentParser`, page count, PDF metadata, and thumbnail helpers.

- [ ] **Step 4: Write full parser tests**

Assert source SHA-256 before and after parse, Markdown cleanup behavior ported from the prototype, archive accessors, date fallback to `None`, metadata non-raising behavior, cache retention on later context exception, and cache removal after clean context exit.

- [ ] **Step 5: Complete `DoclingParser` orchestration**

Construct configuration, compatibility gate, HTTP client, cache, and raster adapter lazily per parser context. Translate plugin errors to `documents.parsers.ParseError` at the parser boundary.

- [ ] **Step 6: Run all parser unit tests**

Run: `uv run pytest tests/parser/unit -v`

Expected: PASS.

- [ ] **Step 7: Commit complete parser behavior**

```bash
git add packages/parser/src/paperless_docling tests/parser/unit
git commit -m "feat: integrate Docling with Paperless archives"
```

## Task 6: Prove Real Workflow-Selected Consumption

**Files:**
- Create: `tests/parser/integration/compose.yml`
- Create: `tests/parser/integration/test_consumption.py`
- Create: `tests/parser/integration/docling_stub.py`
- Create: `tests/fixtures/born-digital.pdf`
- Create: `tests/fixtures/scanned.pdf`
- Create: `tests/fixtures/scanned.png`
- Create: `scripts/install-parser.sh`

**Interfaces:**
- Produces a repeatable integration environment with real pinned Docling Serve for successful conversion and a stub used only for deterministic fault injection.
- Consumes the built parser wheel and official Paperless image version supplied through environment variables.

- [ ] **Step 1: Add an integration test that initially cannot discover the parser**

Build the wheel, mount `scripts/install-parser.sh` into `/custom-cont-init.d`, and assert Paperless startup logs the parser identity. The initial test must fail until all startup wiring is correct.

- [ ] **Step 2: Add selected and unselected consumption tests**

Create workflows through the Paperless API. Upload fixtures and poll Paperless tasks. Assert unselected content comes from the default parser and selected content contains expected real Docling Markdown structure on first successful document retrieval.

- [ ] **Step 3: Add original/archive assertions**

Compare uploaded and downloaded original SHA-256 values. Assert born-digital selected PDFs have no redundant archive under `auto`; scanned PDF/image fixtures have distinct searchable archives.

- [ ] **Step 4: Add failure and restart-resume tests**

Use the controllable stub to fail permanently and verify the Paperless task fails without a created fallback document. Restart the Paperless worker after submission and verify the persisted task ID is polled rather than resubmitted. Induce a Paperless file-storage failure after conversion completes and assert parser context exit preserves the cache entry for retry.

- [ ] **Step 5: Run integration tests**

Run: `uv run pytest tests/parser/integration -v`

Expected: PASS against pinned real Paperless 3.1 and Docling Serve containers, plus stub-only fault scenarios.

- [ ] **Step 6: Verify no-dependency installation assumptions**

Inspect the official Paperless image for the exact imported `httpx` and `packaging` versions, import the plugin with network disabled, and fail the compatibility matrix if those dependencies disappear or leave the tested range.

- [ ] **Step 7: Run formatting, typing, unit, and integration verification**

Run: `uv run ruff check . && uv run mypy packages/parser/src && uv run pytest tests/parser -v`

Expected: PASS.

- [ ] **Step 8: Commit integration coverage**

```bash
git add tests/parser/integration tests/fixtures scripts/install-parser.sh
git commit -m "test: verify workflow-selected consumption"
```
