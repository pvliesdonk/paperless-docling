# Operations And Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the parser and summary worker safe to deploy, operate, upgrade, reprocess, and publish against supported Paperless and Docling releases.

**Architecture:** The official Paperless image installs a pinned PyPI parser at container initialization. A hardened private worker container shares no Paperless files, and Paperless workflows provide selection and event delivery. CI validates compatibility and publishes versioned artifacts with an explicit rollback path.

**Tech Stack:** Docker Compose, GitHub Actions, uv, PyPI trusted publishing, Paperless REST workflows/tasks, Prometheus metrics, Markdown runbooks.

**Spec:** `docs/superpowers/specs/2026-08-27-paperless-docling-design.md`

## Global Constraints

- Never require a derived Paperless image.
- Pin Paperless, Docling Serve, parser, and worker release versions in examples.
- Keep all credentials in secret files or Compose secrets.
- Publish no worker port to the host in the recommended deployment.
- Reprocessing must use Paperless APIs and task tracking, not direct content patches.
- Every upgrade and migration step must include verification and rollback instructions.
- Release support ranges change only after matrix and smoke tests pass.
- Release publication requires an annotated tag whose signature validates against the documented trusted release key.

---

## Planned File Structure

```text
Dockerfile.worker                         # hardened worker image
compose.example.yml                       # production-oriented integration example
deploy/
  install-parser.sh                       # PyPI startup installation
  parser-requirements.txt.example         # exact version/hash option
  paperless-workflows.md                  # workflow setup and payloads
  secrets/README.md                       # required secret files and permissions
docs/operations/
  deployment.md                           # installation and hardening
  migration.md                            # prototype replacement and rollback
  upgrades.md                             # Paperless/Docling/plugin update process
  troubleshooting.md                     # task, cache, queue, and API failures
.github/workflows/
  ci.yml                                  # unit/static/package checks
  integration.yml                         # supported compatibility matrix
  compatibility-latest.yml                # scheduled upstream probe
  release.yml                             # wheel/image publication
```

## Task 1: Add Explicit Paperless Reprocess Support

**Files:**
- Modify: `packages/worker/src/paperless_docling_worker/paperless.py`
- Modify: `packages/worker/src/paperless_docling_worker/cli.py`
- Test: `tests/worker/unit/test_reprocess.py`

**Interfaces:**
- Produces: `PaperlessClient.reprocess(document_id: int, *, remote_ocr: bool = True) -> str`
- Produces: `PaperlessClient.get_task(task_id: str) -> PaperlessTask`
- Command: `paperless-docling reprocess ID... [--wait] [--timeout SECONDS]`.

- [ ] **Step 1: Write failing Paperless reprocess request tests**

Derive the exact endpoint and payload from Paperless 3.1's OpenAPI schema. Assert remote OCR is explicitly true, response task IDs are validated, redirects are rejected, and unsupported API versions fail clearly.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/worker/unit/test_reprocess.py -v`

Expected: FAIL because reprocess methods do not exist.

- [ ] **Step 3: Implement reprocess and task polling methods**

Reuse the authenticated Paperless origin-pinned client. Normalize task states and preserve Paperless's failure detail without printing credentials.

- [ ] **Step 4: Write failing CLI wait and partial-failure tests**

Assert one task per valid ID, task IDs printed immediately, `--wait` progress, timeout exit code, and a summary table when some documents fail.

- [ ] **Step 5: Implement the CLI command**

Do not download documents or enqueue summaries directly. Successful Paperless Document Updated workflows are responsible for summary refresh.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/worker/unit/test_reprocess.py tests/worker/unit/test_cli.py -v`

Expected: PASS.

```bash
git add packages/worker/src/paperless_docling_worker tests/worker/unit
git commit -m "feat: add explicit Paperless reprocessing"
```

## Task 2: Build And Harden The Worker Container

**Files:**
- Create: `Dockerfile.worker`
- Create: `.dockerignore`
- Create: `tests/deployment/test_worker_image.py`

**Interfaces:**
- Produces OCI image commands for API/worker service and CLI.
- Consumes the `paperless-docling-worker` wheel and one `/data` volume.

- [ ] **Step 1: Write image-inspection tests**

Assert a fixed non-root UID/GID, no shell-dependent entrypoint, writable `/data` only, healthcheck command, labels containing source/revision/version, and no copied `.env`, tests, git data, or credentials.

- [ ] **Step 2: Build the initially incomplete image and confirm test failure**

Run: `docker build -f Dockerfile.worker -t paperless-docling-worker:test . && uv run pytest tests/deployment/test_worker_image.py -v`

Expected: FAIL until image hardening is implemented.

- [ ] **Step 3: Implement a multi-stage, dependency-locked worker image**

Install the built wheel into a virtual environment, run as the dedicated user, set a read-only-compatible temporary directory, and expose only the internal application port metadata.

- [ ] **Step 4: Run image and container smoke tests**

Start with `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, a tmpfs for `/tmp`, and a writable `/data` volume. Assert liveness and graceful SIGTERM.

- [ ] **Step 5: Commit the worker image**

```bash
git add Dockerfile.worker .dockerignore tests/deployment/test_worker_image.py
git commit -m "build: add hardened summary worker image"
```

## Task 3: Provide Compose And Paperless Workflow Integration

**Files:**
- Create: `compose.example.yml`
- Create: `deploy/install-parser.sh`
- Create: `deploy/parser-requirements.txt.example`
- Create: `deploy/paperless-workflows.md`
- Create: `deploy/secrets/README.md`
- Test: `tests/deployment/test_compose.py`

**Interfaces:**
- Produces a Compose overlay/example for existing Paperless and Docling deployments.
- Defines Consumption Started Remote OCR, Document Added/Updated webhook, and Apply AI Suggestions workflows.

- [ ] **Step 1: Write failing Compose policy tests**

Parse Compose YAML and assert exact image tags, no worker host ports, private network membership, secret-file mounts, parser cache persistence, worker SQLite persistence, healthchecks, resource limits, read-only worker root filesystem, capability drops, and `no-new-privileges`.

- [ ] **Step 2: Add a pinned and optionally hash-verified parser install script**

The script must use `set -euo pipefail`, require an exact version, install `--no-deps`, and support `PIP_NO_INDEX` plus `PIP_FIND_LINKS` for an offline wheelhouse. Document root ownership and read-only mount requirements.

- [ ] **Step 3: Write exact workflow instructions**

Document UI fields and API-equivalent payloads for:

- Consumption Started filters plus Remote OCR action.
- Document Added and Updated JSON webhook `{document_id, event}` and bearer header.
- Apply AI Suggestions fields and overwrite/create choices.

Warn that broad workflows can incur VLM cost and that Summary updates cause harmless deduplicated Updated events.

Configure Docling Serve to allow the named `paperless-vlm` preset while disabling arbitrary custom VLM configuration, external plugins, downloads, and the UI. Add a deployment test that the approved preset succeeds and a direct arbitrary custom configuration request is rejected.

- [ ] **Step 4: Complete Compose example and run tests**

Run: `docker compose -f compose.example.yml config --quiet && uv run pytest tests/deployment/test_compose.py -v`

Expected: PASS.

- [ ] **Step 5: Commit deployment integration**

```bash
git add compose.example.yml deploy tests/deployment/test_compose.py
git commit -m "docs: add Paperless deployment integration"
```

## Task 4: Write Migration, Upgrade, And Troubleshooting Runbooks

**Files:**
- Create: `docs/operations/deployment.md`
- Create: `docs/operations/migration.md`
- Create: `docs/operations/upgrades.md`
- Create: `docs/operations/troubleshooting.md`
- Modify: `README.md`
- Test: `tests/docs/test_links.py`

**Interfaces:**
- Produces operator procedures linked from the project README and GitHub issues.

- [ ] **Step 1: Write the deployment runbook**

Cover prerequisites, secret creation, PyPI parser installation, Docling named preset, Paperless remote OCR mode, workflow setup, worker startup, health verification, and one selected/unselected smoke document.

- [ ] **Step 2: Write the prototype migration and rollback runbook**

Sequence:

1. Stop tag-polling converters without removing tags.
2. Back up Paperless and worker state.
3. Deploy plugin/worker and verify discovery/readiness.
4. Create narrow workflows.
5. Test new consumption and summary field compatibility.
6. Remove legacy trigger tags only after validation.
7. Roll back by removing workflows and parser init script, then restarting official Paperless.

- [ ] **Step 3: Write the upgrade compatibility procedure**

Require staging tests before changing Paperless minor versions, exact parser/image versions, scheduled probe interpretation, cache/profile version changes, database backup, and rollback to the previous official image and plugin pin.

- [ ] **Step 4: Write troubleshooting decision paths**

Cover parser not discovered, remote workflow not selecting, Docling task missing/failing, archive failure, cache lock/staleness, Summary field conflict, webhook authentication, queue age, retry exhaustion, and reprocess failures. Every section must include safe inspection commands and avoid advising direct Paperless database edits.

- [ ] **Step 5: Expand README and validate links**

README should state purpose, maturity, supported versions, quick architecture, links to design/operations, privacy warning, and license. Run: `uv run pytest tests/docs/test_links.py -v`.

- [ ] **Step 6: Commit operator documentation**

```bash
git add README.md docs/operations tests/docs/test_links.py
git commit -m "docs: add operations and migration runbooks"
```

## Task 5: Add CI And Compatibility Matrices

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/integration.yml`
- Create: `.github/workflows/compatibility-latest.yml`
- Create: `scripts/compatibility-matrix.py`
- Create: `compatibility.toml`
- Test: `tests/release/test_compatibility.py`

**Interfaces:**
- `compatibility.toml` is the machine-readable source of supported Paperless, Python, and Docling ranges.
- Parser runtime compatibility data is generated or checked against this file.

- [ ] **Step 1: Write failing compatibility consistency tests**

Assert package `requires-python`, runtime Paperless gate, integration matrix, documentation, and compatibility manifest agree exactly.

- [ ] **Step 2: Implement the compatibility manifest and checker**

Do not duplicate handwritten ranges across workflows. Generate GitHub matrix JSON from `compatibility.toml` and provide a `--check` mode used by CI.

- [ ] **Step 3: Add fast CI**

Run Ruff, mypy, unit tests, package builds, wheel metadata inspection, documentation links, Compose validation, and secret scanning on pull requests.

- [ ] **Step 4: Add supported integration matrix**

Run parser consumption and summary-flow integration tests against every supported Paperless patch and pinned real Docling Serve version. Upload sanitized logs on failure.

- [ ] **Step 5: Add scheduled latest probes**

Probe newest Paperless and Docling releases weekly. Mark failures clearly as compatibility alerts; do not mutate support ranges or publish packages automatically.

- [ ] **Step 6: Run workflow lint and local verification**

Run: `uv run pytest tests/release/test_compatibility.py -v && actionlint`

Expected: PASS.

- [ ] **Step 7: Commit CI and compatibility automation**

```bash
git add .github/workflows scripts/compatibility-matrix.py compatibility.toml tests/release/test_compatibility.py
git commit -m "ci: test supported upstream versions"
```

## Task 6: Automate Trusted Releases And Smoke Installation

**Files:**
- Create: `.github/workflows/release.yml`
- Create: `scripts/smoke-install-parser.sh`
- Create: `docs/operations/releases.md`
- Test: `tests/release/test_artifacts.py`

**Interfaces:**
- Produces PyPI `paperless-docling` wheel/sdist and worker OCI image from one signed tag.
- Requires GitHub environments configured for PyPI trusted publishing and container registry write access.

- [ ] **Step 1: Write artifact-policy tests**

Assert parser wheel excludes worker dependencies and Docling, worker wheel has locked runtime dependencies, source distributions include licenses/readmes, image labels match the tag, and version values agree.

- [ ] **Step 2: Add parser smoke installation**

Start the official supported Paperless image with the built wheel installed through `/custom-cont-init.d`. Assert startup logs the parser entry point and run one selected fixture consumption before publication.

- [ ] **Step 3: Implement tag-gated release workflow**

Build once, verify artifacts, run `git verify-tag "$GITHUB_REF_NAME"` against the repository's documented trusted release key, run the complete supported matrix, publish with PyPI trusted publishing, push the worker image by immutable version and digest, generate provenance/attestations, and create GitHub release notes. Never publish from pull requests, lightweight tags, unverified signatures, or manual untagged commits.

- [ ] **Step 4: Write release and rollback procedure**

Document versioning, profile changes, changelog expectations, yanking a broken PyPI release, reverting deployment pins, and handling a worker image rollback with forward-compatible SQLite migrations.

- [ ] **Step 5: Run complete pre-release verification**

Run: `uv run ruff check . && uv run mypy packages && uv run pytest -v && docker compose -f compose.example.yml config --quiet`

Expected: PASS.

- [ ] **Step 6: Commit release automation**

```bash
git add .github/workflows/release.yml scripts/smoke-install-parser.sh docs/operations/releases.md tests/release/test_artifacts.py
git commit -m "ci: automate verified project releases"
```
