# Paperless Docling Design Specification

**Date:** 2026-08-27

**Status:** Approved

**High-level design:** [Paperless Docling Architecture](../../design/paperless-docling.md)

## 1. Summary

Paperless Docling integrates Docling Serve into Paperless-ngx 3.1 as a workflow-selectable third-party remote parser. Selected documents are converted to structured Markdown during the original consumption task. Paperless retains the original file unchanged and derives a searchable archive for scans under its normal automatic policy.

After consumption, Paperless handles title and metadata suggestions with its native AI workflow action. A separate durable service receives Paperless workflow webhooks, summarizes effective document content, and atomically updates a `Summary` Long Text custom field.

The system replaces tag-polling prototypes with explicit Paperless workflows, resumable task state, content-addressed idempotency, bounded failure handling, and testable compatibility contracts.

## 2. Context

### 2.1 Existing Docling Prototype

The existing `/mnt/docker-volumes/compose.git/40-documents/paperless-docling-md` worker:

- Polls `do-docling` and `do-docling-vlm` tags every 30 seconds.
- Downloads originals through the Paperless API.
- Submits asynchronous Docling Serve conversions.
- Stores in-flight task IDs only in memory.
- Patches Paperless content, title, and complete tag arrays after consumption.
- Hands work to the summarizer through `do-summarize`.

This proves the quality of Docling Markdown and VLM image descriptions, but has correctness problems: work is duplicated after restarts, concurrent tag edits can be overwritten, two trigger modes can conflict, one slow document blocks the polling loop, and matching/indexing initially run against Paperless's original extraction.

### 2.2 Existing Summary Prototype

The existing `/mnt/docker-volumes/compose.git/40-documents/paperless-summarize` worker:

- Polls the `do-summarize` tag.
- Summarizes Paperless content through an OpenAI-compatible endpoint.
- Uses character-based Markdown chunking and map/reduce.
- Replaces a `Summary` custom field and the complete tag array.

It provides useful output but lacks durable state, claims, bounded retries, token-aware chunking, output provenance, and safe concurrent updates.

### 2.3 Paperless 3.1 Capabilities

Paperless-ngx 3.1 provides the extension points needed to avoid post-hoc polling:

- Third-party parsers registered through `paperless_ngx.parsers` entry points.
- Parser scoring and a `uses_remote_service` declaration.
- Workflow-only remote OCR selection through Consumption Started workflows.
- Asynchronous workflow webhooks with retries.
- Background Apply AI Suggestions workflow actions.
- Long Text custom fields.
- Explicit remote OCR support during document reprocessing.

Pre- and post-consume scripts remain blocking subprocess hooks. They are not the primary integration because a pre-consume script cannot return database content and a post-consume script is too late to make Docling authoritative during initial matching and indexing.

## 3. Goals And Non-Goals

### 3.1 Goals

- Make Docling Markdown the content Paperless stores on first successful consumption.
- Let Paperless workflows explicitly select expensive remote processing.
- Enable full VLM enrichment for selected documents.
- Fail selected consumption rather than silently fall back to lower-quality text.
- Preserve original files unchanged.
- Preserve searchable PDF/A generation for scans and images.
- Use Paperless native AI for title and metadata automation.
- Generate summaries asynchronously in the document's language.
- Store one canonical current summary in a Long Text custom field.
- Recover safely from Paperless, Docling, and summary worker restarts.
- Provide explicit reprocess and resummarize operations for existing documents.
- Publish a PyPI plugin installable into the official Paperless image.
- Test and declare Paperless and Docling compatibility.

### 3.2 Non-Goals

- A Paperless fork or derived Paperless image.
- A new web interface.
- Automatic archive-wide backfill.
- A separate vector database or document chat.
- A custom metadata suggestion pipeline.
- Historical summaries in notes.
- Modifying original source documents.
- Office and email parser replacement in the first release.
- Multiple summary worker replicas in the first release.

## 4. System Boundaries

### 4.1 Parser Distribution

The `paperless-docling` PyPI distribution contains:

- Paperless parser entry point.
- Version compatibility check.
- Docling Serve v1 client.
- Content-addressed parser cache.
- Paperless raster-parser adapter.
- Configuration and structured logging helpers.

It does not contain Docling models, local inference runtimes, summary service code, or Paperless API credentials.

The recommended Paperless initialization script installs an exact version:

```bash
#!/usr/bin/env bash
set -euo pipefail
pip install --no-deps "paperless-docling==${PAPERLESS_DOCLING_VERSION}"
```

The script directory is root-owned, read-only in the container, and mounted at `/custom-cont-init.d` as required by Paperless.

### 4.2 Summary Service Distribution

The summary worker is a separate OCI image containing:

- Private HTTP enqueue endpoint.
- SQLite job repository.
- Leasing worker loop.
- Paperless API client.
- OpenAI-compatible summary client.
- CLI for reprocessing and job operations.
- Health and metrics endpoints.

The worker does not mount Paperless media, consume, data, or export volumes.

### 4.3 External Services

- Paperless-ngx 3.1 is the document and workflow system of record.
- Docling Serve performs document conversion and VLM enrichment.
- The configured OpenAI-compatible endpoint performs summarization.
- Paperless's own configured AI backend performs title and metadata suggestions.

## 5. Parser Contract

### 5.1 Identity And Selection

The parser class exposes the Paperless protocol identity fields and methods. It declares:

```python
name = "Paperless Docling Parser"
uses_remote_service = True
```

Supported MIME types initially match the built-in raster parser:

- `application/pdf`
- `image/jpeg`
- `image/png`
- `image/tiff`
- `image/gif`
- `image/bmp`
- `image/webp`
- `image/heic`

`score()` returns a value above the built-in raster parser for supported MIME types. Paperless excludes the class when `allow_remote=False`, so the high score does not affect unselected documents.

Missing or invalid plugin configuration does not make `score()` return `None`. Returning `None` would let a workflow-selected document silently use the local parser. Instead, selection succeeds and `parse()` raises a clear `ParseError`, preserving the approved fail-and-retry behavior.

### 5.2 Compatibility

At import or first construction, the plugin checks the running Paperless version against an explicit supported range. Unsupported versions prevent parser use and log a message containing:

- Running Paperless version.
- Supported range.
- Plugin version.
- Override environment variable for deliberate staging tests.

The override is disabled by default and never broadens compatibility silently.

### 5.3 Parse Lifecycle

For each selected document:

1. Compute SHA-256 of the temporary source copy.
2. Build a conversion key from source hash, plugin cache schema, and configured profile version.
3. Acquire a per-key lock.
4. Load a completed Markdown result or resumable Docling task ID if present.
5. Submit a Docling task only when no usable state exists.
6. Poll until success, permanent failure, or overall deadline.
7. Retrieve and validate Markdown output.
8. If `produce_archive=True`, invoke the raster adapter against the same temporary source copy.
9. Return Docling Markdown through `get_text()`.
10. Return raster archive and thumbnail when generated.
11. Return page count and PDF metadata through Paperless utilities.
12. Remove successful cache state only when the parser context exits without a later Paperless storage exception. A real-Paperless integration test must induce downstream storage failure and prove the cache remains resumable.

The original Paperless input and temporary source copy are never modified by plugin code.

### 5.4 Archive Adapter

The plugin composes, rather than subclasses, Paperless's `RasterisedDocumentParser`. The adapter is an explicit Paperless-version compatibility surface.

When `produce_archive=False`, it does not run OCRmyPDF. Thumbnail and page-count behavior use tested Paperless parser utilities.

When `produce_archive=True`, it calls the raster parser and exposes only its archive, thumbnail, page count, and metadata. Its extracted plain text is discarded because Docling Markdown is authoritative.

Any required raster failure raises `ParseError` and aborts consumption.

## 6. Docling Integration

### 6.1 API

The client uses current Docling Serve v1 endpoints:

- `POST /v1/convert/file/async`
- `GET /v1/status/poll/{task_id}`
- `GET /v1/result/{task_id}`

Supported task states are normalized internally to pending, running, succeeded, failed, and missing. Unknown terminal states fail closed.

### 6.2 Conversion Profile

Docling Serve defines a named `paperless-vlm` preset. The request selects the preset and fixed conversion behavior:

- Markdown output.
- OCR enabled.
- Accurate table structure.
- Placeholder image export in Markdown.
- Picture classification.
- Picture descriptions.
- Formula enrichment.
- Code enrichment.

Model endpoint, credentials, prompts, thresholds, and denied picture classes remain server-side. Production Docling Serve permits the named preset but rejects arbitrary client-provided VLM configurations. Deployment integration tests submit a disallowed custom configuration directly and require rejection.

`PAPERLESS_DOCLING_PROFILE_VERSION` is a required operator-controlled identifier. Any semantic change to server-side conversion configuration increments it and invalidates parser cache reuse.

### 6.3 Deadlines And Retries

- HTTP connection and read timeouts are shorter than the overall conversion deadline.
- Polling uses a configurable interval with small jitter.
- Transient poll and result fetch failures retry within the same overall deadline.
- A known task ID is never resubmitted merely because one poll failed.
- If Docling reports that a persisted task no longer exists, one controlled resubmission is allowed and persisted.
- Permanent Docling failure includes sanitized server detail in `ParseError`.
- No retry logs document bytes, Markdown, or authorization headers.

## 7. Parser Cache

### 7.1 Layout

Each conversion key owns one directory under `PAPERLESS_DOCLING_CACHE_DIR`:

```text
<key>/
  state.json
  result.md
  lock
```

`state.json` contains a schema version, profile version, source hash, task ID, submission time, resubmission count, and last known status. Writes use temporary files plus atomic replacement. `result.md` is written only after complete validation.

### 7.2 Locking

The per-key lock covers state inspection, submission, and state mutation. Polling may retain the lock to guarantee one active consumer per source/profile combination; this serializes only duplicate copies of the same source, not unrelated documents.

### 7.3 Cleanup

- Success removes cache state after Paperless storage succeeds.
- Parser or later consumption failure preserves state for retry.
- Startup or scheduled cleanup removes entries older than the configured maximum age and then evicts the oldest unlocked terminal entries until total cache size is within the configured byte limit.
- Cleanup never removes a currently locked entry.
- Cache permissions restrict access to the Paperless runtime user.

## 8. Summary Service

### 8.1 Webhook Endpoint

Paperless sends JSON to `POST /v1/events/paperless` with:

```json
{
  "document_id": 123,
  "event": "document_added"
}
```

The request uses `Authorization: Bearer <secret>`. The endpoint:

1. Validates content type, bearer token in constant time, event value, and positive document ID.
2. Records or refreshes an enqueue request transactionally.
3. Returns `202 Accepted` without calling Paperless or the model endpoint.

Repeated webhook delivery is expected and safe.

### 8.2 Job Model

The durable job record contains:

- Internal job ID.
- Paperless document ID.
- State.
- Source content hash when known.
- Summary profile version.
- Attempt count and next-attempt time.
- Lease owner and expiry.
- Creation, update, start, and completion times.
- Sanitized last error class and message.
- Model name, input/output token counts, and latency.

Normal event-driven completion uniqueness is enforced by a partial unique index on `(document_id, content_hash, profile_version)` where `force=false` and state is completed. A forced resummarization carries a globally unique `request_id`; retries of that same request remain idempotent, while its completion is deliberately excluded from normal identity uniqueness.

### 8.3 Job State Machine

Allowed states are:

- `pending`: eligible for claim.
- `leased`: owned by one worker until lease expiry.
- `retry_wait`: retryable after `next_attempt_at`.
- `completed`: summary successfully stored.
- `superseded`: a newer content hash made this work obsolete.
- `failed`: permanent failure or retry budget exhausted.
- `cancelled`: document was deleted or operation was cancelled.

Claiming and lease renewal use transactions. An expired leased job becomes claimable. Only a worker holding the current lease token may complete or reschedule a job.

Each manual retry starts a new `retry_epoch` with a fresh per-epoch attempt budget. The record also retains `cumulative_attempts` and prior sanitized errors so manual retry does not erase history or immediately re-exhaust the old budget.

### 8.4 Content Resolution And Deduplication

After claim, the worker retrieves the document's effective content through the Paperless API and computes SHA-256 over normalized UTF-8 content.

- If an identical completed job exists for the current profile, mark the new request superseded.
- If the request is explicitly forced, assign a unique request identity and do not suppress it against an earlier completed normal or forced generation.
- If another pending job targets older content, mark it superseded.
- Empty content produces a configured short summary without an LLM request or fails permanently according to policy; the first release uses `(No content to summarize.)` for continuity.
- A summary-field update causes a Document Updated webhook, but unchanged content deduplicates it.

### 8.5 Summarization

The worker treats title and document content as untrusted quoted data. Instructions remain in system/developer prompt sections and explicitly prohibit following instructions embedded in the document.

Short content uses one request. Long content is split by Markdown headings and paragraph boundaries against a model token budget. Individual chunk summaries are reduced in bounded batches so neither map nor reduce requests exceed context limits.

Output requirements:

- Use the dominant source language.
- Be concise and factual.
- Preserve material dates, parties, obligations, totals, and decisions when present.
- Do not invent missing facts.
- Do not include prompt commentary or a language label.
- Fit the configured maximum output token budget.

The summary profile version changes whenever prompts, chunking, model identity, or output contract changes materially.

### 8.6 Paperless Update

At startup or first use, the service follows all custom-field API pages and resolves a field named `Summary`.

- If absent, create it with `data_type=longtext`.
- If present with `longtext`, reuse it.
- If present with another type, fail readiness and do not create a duplicate.

Immediately before writing, the worker retrieves effective content again and recomputes its hash. If it differs from the bound job identity, the worker marks the old job superseded, enqueues the current document identity, and does not write the stale summary.

The service writes through `/api/documents/bulk_edit/` with `method=modify_custom_fields`, adding only the Summary field ID and value. It never submits complete tag or custom-field arrays.

After a successful write, the worker retrieves effective content once more. If content changed during the check-to-write window, it enqueues the current identity and records that the completed result was overtaken. Paperless does not expose an atomic conditional update spanning content and custom fields, so the guarantee is eventual convergence rather than impossibility of a brief stale value.

## 9. Explicit Operations

### 9.1 Reprocess

`paperless-docling reprocess ID...` validates document IDs, calls Paperless's supported reprocess endpoint with remote OCR enabled, prints task IDs, and optionally waits for Paperless task completion. The command never downloads or patches document content itself.

Document Updated workflows enqueue summary refreshes after successful reprocessing.

### 9.2 Resummarize

`paperless-docling resummarize ID...` creates forced enqueue requests with unique request identities for the current profile. The forced identity bypasses completed-content deduplication once while repeated delivery or retry of the same command request remains idempotent. A `--profile-version` override is not accepted; deployments change profile through configuration.

### 9.3 Job Administration

`paperless-docling jobs list` filters by state, document, and age. `jobs retry` starts a new retry epoch and moves selected failed jobs to pending while preserving cumulative attempts and error history. `jobs purge` removes completed, superseded, and cancelled records older than a required age.

Destructive purge requires an explicit age and confirmation unless `--yes` is supplied.

## 10. Configuration

### 10.1 Plugin

- `PAPERLESS_DOCLING_SERVE_URL`: required base URL.
- `PAPERLESS_DOCLING_SERVE_TOKEN_FILE`: optional bearer-token file.
- `PAPERLESS_DOCLING_PRESET`: default `paperless-vlm`.
- `PAPERLESS_DOCLING_PROFILE_VERSION`: required cache identity.
- `PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS`: bounded positive duration.
- `PAPERLESS_DOCLING_DEADLINE_SECONDS`: bounded positive duration.
- `PAPERLESS_DOCLING_CACHE_DIR`: required persistent path.
- `PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS`: positive retention.
- `PAPERLESS_DOCLING_CACHE_MAX_BYTES`: positive total-size limit.
- `PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS`: default false.

### 10.2 Summary Service

- `PAPERLESS_URL`: required internal Paperless URL.
- `PAPERLESS_TOKEN_FILE`: required service-account token file.
- `WEBHOOK_TOKEN_FILE`: required webhook bearer secret file.
- `SUMMARY_DATABASE_PATH`: required SQLite path.
- `SUMMARY_LLM_BASE_URL`: required OpenAI-compatible endpoint.
- `SUMMARY_LLM_API_KEY_FILE`: required unless the endpoint explicitly permits no authentication.
- `SUMMARY_LLM_MODEL`: required model name.
- `SUMMARY_PROFILE_VERSION`: required idempotency identity.
- `SUMMARY_CONTEXT_TOKENS`, `SUMMARY_OUTPUT_TOKENS`, and `SUMMARY_CHUNK_TOKENS`: validated token budgets.
- `SUMMARY_MAX_ATTEMPTS`, `SUMMARY_LEASE_SECONDS`, and retry delay bounds.
- `SUMMARY_CONCURRENCY`: fixed to 1 in the first release.

Configuration objects are immutable after startup. Validation errors list variable names but never secret values.

## 11. Security

- Paperless, Docling, and summary traffic remain on private networks unless explicitly protected by TLS.
- The parser's Docling token cannot access Paperless.
- The summary Paperless account receives only document/custom-field permissions needed for reading content and modifying summaries.
- The webhook secret is independent of Paperless and LLM credentials.
- Secrets are read from files and redacted from logs and diagnostics.
- The summary endpoint has a request-size limit and accepts no document text.
- Document content is untrusted input to Docling and the LLM.
- Docling custom model configuration is server-controlled.
- The worker image runs non-root, read-only, with dropped capabilities and no host port.
- Dependency locks, image digests or exact tags, package hashes, and trusted PyPI publication reduce supply-chain drift.

## 12. Observability

### 12.1 Logs

JSON logs include event name, component, duration, outcome, retry class, and safe correlation identifiers. Parser logs use source-hash prefix and Docling task ID. Summary logs use job and document ID.

Document content, filenames where avoidable, prompts containing content, API tokens, and authorization headers are excluded.

### 12.2 Health

- Liveness confirms the process and worker loop are responsive.
- Readiness confirms configuration, writable database, schema version, Summary field compatibility, and ability to claim jobs. Temporary Paperless or LLM unavailability is exposed in diagnostics and metrics without restarting a healthy process repeatedly.

### 12.3 Metrics

The summary service metrics include queue depth, oldest pending age, state totals, attempts, LLM latency, and token totals. The in-process parser does not expose another HTTP server; conversion duration and outcomes, resumed tasks, resubmissions, and cache cleanup totals are emitted as structured Paperless logs and remain visible with Paperless task status.

## 13. Testing And Acceptance

### 13.1 Unit Tests

- Parser satisfies the runtime Paperless protocol.
- Remote parser exclusion leaves default Paperless parsing unchanged.
- Missing configuration fails selected parsing.
- Every Docling task status and malformed response has a deterministic result.
- Cache writes are atomic, locks suppress duplicate submission, and stale tasks resubmit once.
- Archive delegation follows `produce_archive` and never changes source bytes.
- Webhook authentication and validation reject invalid input.
- Job claims, leases, recovery, retry scheduling, supersession, and completion enforce state invariants.
- Token-aware chunking stays within configured budgets.
- Summary prompts preserve instruction/data separation.
- Custom-field resolution paginates and rejects incompatible types.
- Bulk updates contain only the intended Summary modification.

### 13.2 Integration Tests

Against real pinned Paperless and Docling containers, with a controllable Docling stub used only for deterministic fault injection:

- An unselected PDF uses the default parser.
- A selected born-digital PDF stores Docling Markdown and no redundant archive.
- A selected scanned PDF stores Docling Markdown, unchanged original, and separate searchable archive.
- A selected image follows the same original/archive contract.
- Docling failure creates a failed Paperless task without local fallback.
- Restart after Docling submission resumes the task.
- The named VLM preset succeeds and an arbitrary custom VLM configuration is rejected by the production Docling configuration.
- A downstream Paperless storage failure after completed conversion preserves resumable parser cache state.
- Repeated webhooks generate one summary per content/profile identity.
- A document content change before the pre-write check prevents the stale write; a change during the final API window is detected afterward and enqueues current content, proving eventual convergence.
- Explicit resummarization produces one new forced generation without disabling normal event deduplication.
- Summary writes preserve unrelated metadata.
- Reprocess refreshes extraction and summary through Paperless workflows.

### 13.3 Release Acceptance

- Wheel installs from PyPI into the official supported Paperless image.
- Paperless startup logs successful parser discovery.
- CI passes the supported Paperless/Docling matrix.
- Scheduled latest-release probes report compatibility regressions without automatically broadening support.
- Deployment and rollback documentation is complete.

## 14. Delivery Decomposition

Implementation is split into three plans:

1. [Parser and consumption](../plans/2026-08-27-parser-consumption.md)
2. [Summary worker](../plans/2026-08-27-summary-worker.md)
3. [Operations and release](../plans/2026-08-27-operations-release.md)

Each plan produces independently testable software and maps to linked GitHub issues under the project epic.

## 15. References

- [Paperless consume hooks](https://docs.paperless-ngx.com/advanced_usage/#consume-hooks)
- [Paperless parser plugins](https://docs.paperless-ngx.com/advanced_usage/#parser-plugins)
- [Paperless custom parser development](https://docs.paperless-ngx.com/development/#making-custom-parsers)
- [Paperless workflows](https://docs.paperless-ngx.com/usage/#workflows)
- [Paperless REST API](https://docs.paperless-ngx.com/api/)
- [Paperless-ngx v3.1.0](https://github.com/paperless-ngx/paperless-ngx/releases/tag/v3.1.0)
- [Docling Serve REST API](https://docling-project.github.io/docling/usage/api_server/rest_api/)
- [Prior art: pgx-docling-parser](https://github.com/T-Eberle/paperless-docling-parser)
