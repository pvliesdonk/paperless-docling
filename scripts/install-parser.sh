#!/usr/bin/env bash
set -euo pipefail

: "${PAPERLESS_DOCLING_VERSION:?PAPERLESS_DOCLING_VERSION must be non-empty}"

python -m pip install --no-deps "paperless-docling==${PAPERLESS_DOCLING_VERSION}"
