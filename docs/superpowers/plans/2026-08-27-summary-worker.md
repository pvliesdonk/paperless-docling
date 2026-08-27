# Summary Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private durable service that receives Paperless document events, summarizes effective Markdown in the source language, and safely maintains one canonical Summary Long Text field.

**Architecture:** A FastAPI enqueue endpoint records requests in SQLite before returning. A single leasing worker fetches Paperless content, deduplicates by content/profile hash, runs token-aware map/reduce summarization through an OpenAI-compatible endpoint, and updates only the Summary custom field.

**Tech Stack:** Python 3.13+, FastAPI, uvicorn, httpx, stdlib sqlite3, tiktoken, prometheus-client, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-paperless-docling-design.md`

## Global Constraints

- The webhook request never contains document content and returns without calling Paperless or the LLM.
- One worker replica and one active summarization are supported initially.
- Job state transitions and lease ownership are enforced transactionally.
- Summary identity is `(document_id, content_hash, profile_version)`.
- Paperless updates modify only the Summary custom field.
- Documents and titles are untrusted prompt data.
- No secret or document content appears in logs, metrics, health output, or exceptions returned to clients.
- Use TDD for every state transition and external request shape.

---

## Planned File Structure

```text
packages/worker/pyproject.toml
packages/worker/src/paperless_docling_worker/
  __init__.py
  config.py                              # immutable service settings
  db.py                                  # SQLite connection and migrations
  jobs.py                                # job repository and state transitions
  api.py                                 # FastAPI endpoints and auth
  paperless.py                           # Paperless API client
  tokens.py                              # token counting and Markdown chunks
  prompts.py                             # versioned prompt construction
  llm.py                                 # OpenAI-compatible client
  summarize.py                           # short and map/reduce orchestration
  worker.py                              # leasing worker loop
  metrics.py                             # Prometheus instruments
  cli.py                                 # job and summary commands
tests/worker/unit/
tests/worker/integration/
```

## Task 1: Establish Worker Configuration And SQLite Schema

**Files:**
- Create: `packages/worker/pyproject.toml`
- Create: `packages/worker/src/paperless_docling_worker/__init__.py`
- Create: `packages/worker/src/paperless_docling_worker/config.py`
- Create: `packages/worker/src/paperless_docling_worker/db.py`
- Test: `tests/worker/unit/test_config.py`
- Test: `tests/worker/unit/test_db.py`

**Interfaces:**
- Produces: `WorkerConfig.from_environment() -> WorkerConfig`
- Produces: `Database.open(path: Path) -> Database`
- Produces: `Database.migrate() -> None` and transaction context manager.

- [ ] **Step 1: Write failing secret-file and token-budget tests**

Cover required URLs and files, redacted representations, SQLite parent writability, `chunk_tokens < context_tokens - output_tokens`, positive leases/retries, and fixed concurrency of one.

- [ ] **Step 2: Run configuration tests and verify failure**

Run: `uv run pytest tests/worker/unit/test_config.py -v`

Expected: FAIL because worker configuration is undefined.

- [ ] **Step 3: Implement frozen validated settings**

Use explicit environment parsing rather than implicit global settings. Read secret files once and expose `SecretStr`-equivalent values whose `repr` is redacted.

- [ ] **Step 4: Write failing migration tests**

Assert a new database enables WAL, foreign keys, busy timeout, migration versioning, and creates the job table and required indexes. Assert migrations are idempotent.

- [ ] **Step 5: Implement SQLite bootstrap and migrations**

Use stdlib `sqlite3`, explicit transactions, UTC ISO timestamps, and parameterized SQL. Do not add an ORM.

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest tests/worker/unit/test_config.py tests/worker/unit/test_db.py -v`

Expected: PASS.

- [ ] **Step 7: Commit worker foundation**

```bash
git add packages/worker tests/worker/unit/test_config.py tests/worker/unit/test_db.py
git commit -m "feat: establish durable summary worker"
```

## Task 2: Implement The Job State Machine And Leases

**Files:**
- Create: `packages/worker/src/paperless_docling_worker/jobs.py`
- Test: `tests/worker/unit/test_jobs.py`

**Interfaces:**
- Produces enum: `JobState`
- Produces: `JobRepository.enqueue(document_id: int, event: str, *, force: bool = False, request_id: UUID | None = None) -> Job`
- Produces: `claim(worker_id: str, now: datetime) -> ClaimedJob | None`
- Produces: `bind_content(claim, content_hash, profile_version) -> BindResult`, honoring forced request identity.
- Produces: `complete`, `retry`, `fail`, `cancel`, `supersede`, and `renew_lease` methods requiring the lease token.

- [ ] **Step 1: Write failing enqueue and claim tests**

Assert duplicate events coalesce before content resolution, oldest eligible job claims first, claims are atomic across two connections, and lease tokens are unguessable.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/worker/unit/test_jobs.py -v`

Expected: FAIL because `JobRepository` is undefined.

- [ ] **Step 3: Implement pending, claim, and lease recovery transitions**

Use `BEGIN IMMEDIATE` for claims. A leased row is claimable only after `lease_expires_at`. Every mutation includes `WHERE lease_token = ?` and raises `LostLeaseError` when zero rows change.

- [ ] **Step 4: Add identity and supersession tests**

Cover completed-identity deduplication, forced generation bypass with request-level idempotency, stale pending supersession, profile changes, content changes, and the webhook loop created by a Summary field update.

- [ ] **Step 5: Implement content binding and terminal transitions**

Create a unique partial index for completed normal jobs `(document_id, content_hash, profile_version) WHERE force = 0`, plus global uniqueness for non-null forced `request_id`. Preserve attempt and sanitized error history when manually retried.

- [ ] **Step 6: Add retry scheduling tests**

Inject deterministic randomness and assert capped exponential delay with jitter, permanent failure, retry-budget exhaustion, deleted-document cancellation, and manual retry creating a fresh retry epoch while preserving cumulative attempts.

- [ ] **Step 7: Run state-machine tests**

Run: `uv run pytest tests/worker/unit/test_jobs.py -v`

Expected: PASS.

- [ ] **Step 8: Commit durable job semantics**

```bash
git add packages/worker/src/paperless_docling_worker/jobs.py tests/worker/unit/test_jobs.py
git commit -m "feat: add summary job state machine"
```

## Task 3: Add The Authenticated Webhook And Health API

**Files:**
- Create: `packages/worker/src/paperless_docling_worker/api.py`
- Create: `packages/worker/src/paperless_docling_worker/metrics.py`
- Test: `tests/worker/unit/test_api.py`

**Interfaces:**
- Produces: `create_app(config: WorkerConfig, jobs: JobRepository) -> FastAPI`
- Endpoint: `POST /v1/events/paperless`
- Endpoints: `GET /health/live`, `GET /health/ready`, `GET /metrics`.

- [ ] **Step 1: Write failing endpoint contract tests**

Assert missing/wrong bearer token returns 401, comparison is constant-time helper based, non-JSON or oversized input is rejected, event values are allowlisted, document IDs are positive, valid requests return 202, and enqueue errors return 503.

- [ ] **Step 2: Run endpoint tests and verify failure**

Run: `uv run pytest tests/worker/unit/test_api.py -v`

Expected: FAIL because `create_app` is undefined.

- [ ] **Step 3: Implement the enqueue endpoint without outbound calls**

Define the request model with `extra="forbid"`. Log event and document ID only. Add a test whose fake Paperless/LLM clients raise if constructed during enqueue.

- [ ] **Step 4: Implement health and metrics endpoints**

Readiness checks database writability, migration version, and resolved Summary-field compatibility state supplied by the worker. It must not expose URLs containing credentials or exception tracebacks.

- [ ] **Step 5: Run API tests**

Run: `uv run pytest tests/worker/unit/test_api.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the HTTP service**

```bash
git add packages/worker/src/paperless_docling_worker/api.py packages/worker/src/paperless_docling_worker/metrics.py tests/worker/unit/test_api.py
git commit -m "feat: accept authenticated Paperless events"
```

## Task 4: Implement Safe Paperless Document And Custom-Field Access

**Files:**
- Create: `packages/worker/src/paperless_docling_worker/paperless.py`
- Test: `tests/worker/unit/test_paperless.py`

**Interfaces:**
- Produces: `PaperlessClient.get_effective_document(document_id: int) -> PaperlessDocument`
- Produces: `resolve_summary_field() -> int`
- Produces: `write_summary(document_id: int, field_id: int, summary: str) -> None`
- Produces error classes distinguishing retryable, permanent, not-found, and field-conflict failures.

- [ ] **Step 1: Write failing pagination and field bootstrap tests**

Assert all custom-field pages are followed only on the configured Paperless origin, absent Summary creates `longtext`, existing `longtext` is reused, and an existing `string` or other type is a permanent readiness failure.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/worker/unit/test_paperless.py -v`

Expected: FAIL because the client is undefined.

- [ ] **Step 3: Implement origin-pinned pagination and document retrieval**

Reject pagination URLs whose scheme/host/port differ from `PAPERLESS_URL`. Request only fields needed by the worker when the API supports field selection.

- [ ] **Step 4: Write the exact bulk-update request test**

```python
assert request.json() == {
    "documents": [123],
    "method": "modify_custom_fields",
    "parameters": {
        "add_custom_fields": {"42": "summary text"},
        "remove_custom_fields": [],
    },
}
```

Assert no `tags`, complete `custom_fields`, title, or content fields are submitted.

- [ ] **Step 5: Implement summary writes and error classification**

Treat 404 as document deletion, 401/403 and incompatible schemas as permanent, and timeout/429/5xx as retryable. Bound response bodies included in errors.

- [ ] **Step 6: Run Paperless client tests**

Run: `uv run pytest tests/worker/unit/test_paperless.py -v`

Expected: PASS.

- [ ] **Step 7: Commit safe Paperless access**

```bash
git add packages/worker/src/paperless_docling_worker/paperless.py tests/worker/unit/test_paperless.py
git commit -m "feat: update Paperless summaries safely"
```

## Task 5: Implement Token-Aware Markdown Summarization

**Files:**
- Create: `packages/worker/src/paperless_docling_worker/tokens.py`
- Create: `packages/worker/src/paperless_docling_worker/prompts.py`
- Create: `packages/worker/src/paperless_docling_worker/llm.py`
- Create: `packages/worker/src/paperless_docling_worker/summarize.py`
- Test: `tests/worker/unit/test_tokens.py`
- Test: `tests/worker/unit/test_prompts.py`
- Test: `tests/worker/unit/test_summarize.py`

**Interfaces:**
- Produces: `MarkdownChunker.chunks(markdown: str) -> list[Chunk]`
- Produces: `PromptFactory.map_prompt(title, chunk, index, total) -> Messages`
- Produces: `PromptFactory.reduce_prompt(title, summaries) -> Messages`
- Produces: `LlmClient.complete(messages, max_output_tokens) -> Completion`
- Produces: `Summarizer.summarize(title: str, content: str) -> SummaryResult`.

- [ ] **Step 1: Write failing tokenizer and chunk-bound tests**

Cover heading-first splits, paragraph fallback, hard token fallback, preserved content order, no empty chunks, oversized single tokens, and every chunk within `SUMMARY_CHUNK_TOKENS`.

- [ ] **Step 2: Implement deterministic Markdown chunking**

Use the configured tiktoken encoding. Keep headings with their following content where possible. Return chunk token counts for metrics.

- [ ] **Step 3: Write prompt injection boundary tests**

Assert title and content appear only inside clearly delimited untrusted-data messages, system instructions forbid obeying embedded instructions, and prompt assembly never interpolates content into the system message.

- [ ] **Step 4: Implement versioned prompts**

Specify source-language output, factuality, concise format, material facts, and no invented details. Keep prompt constants centralized so profile changes are reviewable.

- [ ] **Step 5: Write failing short, map, and reduce orchestration tests**

Assert short content uses one call, long content maps each chunk, reductions batch recursively within context, empty content avoids the model, output is non-empty and bounded, and token usage aggregates across calls.

- [ ] **Step 6: Implement the OpenAI-compatible client and summarizer**

Use bounded timeouts and no SDK-global state. Classify 429/5xx/timeouts as retryable and invalid/empty model output as permanent for the current attempt.

- [ ] **Step 7: Run summarization tests**

Run: `uv run pytest tests/worker/unit/test_tokens.py tests/worker/unit/test_prompts.py tests/worker/unit/test_summarize.py -v`

Expected: PASS.

- [ ] **Step 8: Commit summarization behavior**

```bash
git add packages/worker/src/paperless_docling_worker/{tokens.py,prompts.py,llm.py,summarize.py} tests/worker/unit
git commit -m "feat: add token-aware document summaries"
```

## Task 6: Connect The Leasing Worker End To End

**Files:**
- Create: `packages/worker/src/paperless_docling_worker/worker.py`
- Test: `tests/worker/unit/test_worker.py`
- Test: `tests/worker/integration/test_summary_flow.py`

**Interfaces:**
- Produces: `SummaryWorker.run_once() -> WorkOutcome`
- Produces: `SummaryWorker.run(stop_event) -> None`
- Consumes: `JobRepository`, `PaperlessClient`, `Summarizer`, clock, sleeper, and metrics.

- [ ] **Step 1: Write failing worker outcome tests**

Cover successful summary, completed identity skip, forced generation, stale content supersession, content changing before and during the final write window, Paperless deletion cancellation, retryable fetch/model/write errors, permanent field conflict, lost lease, and lease renewal during long model work.

- [ ] **Step 2: Run worker tests and verify failure**

Run: `uv run pytest tests/worker/unit/test_worker.py -v`

Expected: FAIL because `SummaryWorker` is undefined.

- [ ] **Step 3: Implement one-claim worker orchestration**

Hash normalized UTF-8 content with SHA-256. Bind identity before model work. Renew the lease before each external request and between map/reduce batches. Immediately before writing, fetch and hash effective content again; if it changed, supersede this job and enqueue the current document without writing stale output. After Paperless confirms the write, fetch and hash once more; if content changed in the final API window, enqueue the current identity and record the completed job as overtaken. Complete only after these checks.

- [ ] **Step 4: Add integration tests with fake Paperless and LLM HTTP services**

Send repeated Added and Updated events, run the worker, and assert exactly one LLM summary and one Summary-field write for unchanged content. Change content and assert a new summary. Change content while model completion is blocked and before the pre-write check to assert no stale write. Change content inside the check-to-write window and assert the post-write check enqueues a correcting current-content job.

- [ ] **Step 5: Run worker unit and integration tests**

Run: `uv run pytest tests/worker/unit/test_worker.py tests/worker/integration/test_summary_flow.py -v`

Expected: PASS.

- [ ] **Step 6: Commit worker orchestration**

```bash
git add packages/worker/src/paperless_docling_worker/worker.py tests/worker
git commit -m "feat: process durable summary jobs"
```

## Task 7: Add Summary And Job CLI Operations

**Files:**
- Create: `packages/worker/src/paperless_docling_worker/cli.py`
- Test: `tests/worker/unit/test_cli.py`

**Interfaces:**
- Commands: `resummarize`, `jobs list`, `jobs retry`, and `jobs purge`.
- Reprocess commands are added by the operations plan.

- [ ] **Step 1: Write failing CLI tests with Typer's runner**

Assert ID validation, forced resummarize request IDs, state/age filters, table and JSON output, retry creating a new epoch while preserving cumulative history, purge requiring age and confirmation, and `--yes` non-interactive behavior.

- [ ] **Step 2: Run CLI tests and verify failure**

Run: `uv run pytest tests/worker/unit/test_cli.py -v`

Expected: FAIL because the CLI is undefined.

- [ ] **Step 3: Implement commands over public repository methods**

Do not embed SQL in CLI handlers. Return non-zero status for partial invalid IDs or unavailable database. Never print secret settings.

- [ ] **Step 4: Run all worker verification**

Run: `uv run ruff check packages/worker tests/worker && uv run mypy packages/worker/src && uv run pytest tests/worker -v`

Expected: PASS.

- [ ] **Step 5: Commit operational CLI support**

```bash
git add packages/worker/src/paperless_docling_worker/cli.py tests/worker/unit/test_cli.py
git commit -m "feat: add summary job administration"
```
