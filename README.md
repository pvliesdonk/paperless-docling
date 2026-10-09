# paperless-docling-α(This is an amateur test version)

A remote [Docling](https://github.com/docling-project/docling) integration for [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx).

* Converts supported documents to Markdown using a remote Docling Serve instance.
* Supports OCR, table extraction, image handling, and document structure analysis.
* Keeps document conversion separate from the Paperless-ngx application server.
* Integrates with the Paperless-ngx document parser system.

## Acknowledgements

This project is based on the work of [Peter van Liesdonk (@pvliesdonk)](https://github.com/pvliesdonk).

* Thanks to Peter van Liesdonk for the original implementation and contribution.
* Thanks to the Docling and Paperless-ngx communities for their projects and documentation.

## Features

* **Remote document conversion**

  * Uses a separately deployed Docling Serve instance.
  * Communicates through the Docling service-client API.
* **Markdown output**

  * Exports converted documents as Markdown.
* **Supported input formats**

  * PDF
  * DOC and DOCX
  * PPT and PPTX
  * XLS and XLSX
  * RTF
  * ODT, ODS, and ODP
  * Images supported by the configured Docling pipeline
  * CSV
* **Document processing**

  * Configurable OCR and OCR language.
  * Configurable table extraction and cell matching.
  * Configurable PDF heading hierarchy extraction.
  * Configurable image handling.
* **Operational controls**

  * Conversion timeout and polling interval.
  * Optional service-token file.
  * Configurable cache location and limits.
  * Configurable Paperless-ngx parser selection score.

## Requirements

* Python `>=3.14,<3.15`
* Paperless-ngx
* A separately deployed Docling Serve instance
* Network connectivity from Paperless-ngx to Docling Serve
* `uv` for the installation commands below
* Gotenberg/LibreOffice configured in Paperless-ngx when PDF renditions are required for non-PDF documents


### Activate the parser

* Add the configuration below to `paperless.conf`.
* Replace the example Docling Serve URL with the address reachable from the Paperless-ngx host.
* Ensure the cache directory exists and is writable by the Paperless-ngx process.
* Restart the relevant Paperless-ngx services after installation and configuration.
* Check the logs for parser initialization and conversion errors.

## Configuration

Add the following environment variables to `paperless.conf`.

### Example

```dotenv
# Docling Serve
PAPERLESS_DOCLING_SERVE_URL=<YOURURL>
PAPERLESS_DOCLING_SERVE_TOKEN_FILE=<YOURTOKEN>

# Parser selection
PAPERLESS_DOCLING_SCORE=15
PAPERLESS_DOCLING_PROFILE_VERSION=1

# Input and output formats
PAPERLESS_DOCLING_FROM_FORMATS=pdf,docx,doc,rtf,pptx,ppt,xlsx,xls,odt,ods,odp,image,csv
PAPERLESS_DOCLING_TO_FORMATS=md

# Conversion pipeline
PAPERLESS_DOCLING_PIPELINE=standard
PAPERLESS_DOCLING_PRESET=paperless-vlm

# OCR
PAPERLESS_DOCLING_OCR_PRESET=pp-ocrv6
PAPERLESS_DOCLING_OCR_LANG=ja
PAPERLESS_DOCLING_DO_OCR=true
PAPERLESS_DOCLING_FORCE_OCR=false

# Images
PAPERLESS_DOCLING_INCLUDE_IMAGES=true
PAPERLESS_DOCLING_INCLUDE_PAGE_IMAGES=false
PAPERLESS_DOCLING_IMAGES_SCALE=2
PAPERLESS_DOCLING_IMAGE_EXPORT_MODE=placeholder

# Tables
PAPERLESS_DOCLING_DO_TABLE_STRUCTURE=true
PAPERLESS_DOCLING_TABLE_MODE=accurate
PAPERLESS_DOCLING_TABLE_CELL_MATCHING=true

# PDF structure
PAPERLESS_DOCLING_DO_PDF_HEADING_HIERARCHY=true

# Markdown
PAPERLESS_DOCLING_MD_PAGE_BREAK_PLACEHOLDER=
PAPERLESS_DOCLING_MD_COMPACT_TABLES=false

# Request handling
PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS=2
PAPERLESS_DOCLING_DEADLINE_SECONDS=600

# Cache
PAPERLESS_DOCLING_CACHE_DIR=/mnt/rclone-cache/paperless-docling/cache
PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS=30
PAPERLESS_DOCLING_CACHE_MAX_BYTES=1073741824

# Compatibility
# PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS=
```

## Environment Variables

### Connection and parser selection

| Variable                                        | Default            | Description                                                                                   |
| ----------------------------------------------- | ------------------ | --------------------------------------------------------------------------------------------- |
| `PAPERLESS_DOCLING_SERVE_URL`                   | Required           | Base URL of the remote Docling Serve instance.                                                |
| `PAPERLESS_DOCLING_SERVE_TOKEN_FILE`            | Unset              | Optional path to a file containing the service token. The file must exist and be readable.    |
| `PAPERLESS_DOCLING_SCORE`                       | `15`               | Parser selection score used by Paperless-ngx.                                                 |
| `PAPERLESS_DOCLING_PROFILE_VERSION`             | Required           | Profile version for the integration configuration.                                            |
| `PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS` | Not specified here | Compatibility control for Paperless-ngx versions not explicitly supported by the integration. |

### Input and output formats

| Variable                         | Default  | Description                                                                                 |
| -------------------------------- | -------- | ------------------------------------------------------------------------------------------- |
| `PAPERLESS_DOCLING_FROM_FORMATS` | Required | Comma-separated list of input formats handled by the parser.                                |
| `PAPERLESS_DOCLING_TO_FORMATS`   | Required | Comma-separated list of output formats requested from Docling Serve. Use `md` for Markdown. |

The example configuration excludes HTML and MHTML.

### Conversion pipeline

| Variable                     | Default         | Description                                         |
| ---------------------------- | --------------- | --------------------------------------------------- |
| `PAPERLESS_DOCLING_PIPELINE` | Required        | Selects the configured Docling processing pipeline. |
| `PAPERLESS_DOCLING_PRESET`   | `paperless-vlm` | Preset name defined by the configuration code.      |

### OCR

| Variable                       | Default  | Description                     |
| ------------------------------ | -------- | ------------------------------- |
| `PAPERLESS_DOCLING_OCR_PRESET` | Required | OCR preset.                     |
| `PAPERLESS_DOCLING_OCR_LANG`   | Required | OCR language, such as `ja`.     |
| `PAPERLESS_DOCLING_DO_OCR`     | Required | Enables or disables OCR.        |
| `PAPERLESS_DOCLING_FORCE_OCR`  | Required | Controls whether OCR is forced. |

### Images

| Variable                                | Default  | Description                            |
| --------------------------------------- | -------- | -------------------------------------- |
| `PAPERLESS_DOCLING_INCLUDE_IMAGES`      | Required | Controls image inclusion.              |
| `PAPERLESS_DOCLING_INCLUDE_PAGE_IMAGES` | Required | Controls page-image inclusion.         |
| `PAPERLESS_DOCLING_IMAGES_SCALE`        | Required | Image scaling factor.                  |
| `PAPERLESS_DOCLING_IMAGE_EXPORT_MODE`   | Required | Image export mode for Markdown output. |

### Tables and PDF structure

| Variable                                     | Default  | Description                                     |
| -------------------------------------------- | -------- | ----------------------------------------------- |
| `PAPERLESS_DOCLING_DO_TABLE_STRUCTURE`       | Required | Enables or disables table structure extraction. |
| `PAPERLESS_DOCLING_TABLE_MODE`               | Required | Table extraction mode, such as `accurate`.      |
| `PAPERLESS_DOCLING_TABLE_CELL_MATCHING`      | Required | Controls table cell matching.                   |
| `PAPERLESS_DOCLING_DO_PDF_HEADING_HIERARCHY` | Required | Controls PDF heading hierarchy extraction.      |

### Markdown output

| Variable                                      | Default  | Description                                                     |
| --------------------------------------------- | -------- | --------------------------------------------------------------- |
| `PAPERLESS_DOCLING_MD_PAGE_BREAK_PLACEHOLDER` | Empty    | Placeholder inserted at page breaks. An empty value is allowed. |
| `PAPERLESS_DOCLING_MD_COMPACT_TABLES`         | Required | Controls compact Markdown table formatting.                     |

### Request handling

| Variable                                  | Default | Valid range                                   |
| ----------------------------------------- | ------- | --------------------------------------------- |
| `PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS` | `2`     | Greater than `0` and at most `60` seconds.    |
| `PAPERLESS_DOCLING_DEADLINE_SECONDS`      | `600`   | Greater than `0` and at most `86400` seconds. |

* The polling interval controls how frequently the parser checks a conversion job.
* The deadline limits how long the parser waits for conversion to complete.

### Cache

| Variable                               | Default              | Valid range                                        |
| -------------------------------------- | -------------------- | -------------------------------------------------- |
| `PAPERLESS_DOCLING_CACHE_DIR`          | Required             | Path to the cache directory.                       |
| `PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS` | `30`                 | Greater than `0` and at most `365` days.           |
| `PAPERLESS_DOCLING_CACHE_MAX_BYTES`    | `1073741824` (1 GiB) | Positive integer, at most `1099511627776` (1 TiB). |

* The cache directory must be writable by the Paperless-ngx process.
* Choose a location with sufficient disk space.

## How It Works

* Paperless-ngx selects a parser based on its parser-selection mechanism and configured scores.
* For supported non-PDF documents, Paperless-ngx may generate a PDF rendition through Gotenberg/LibreOffice.
* The plugin submits the document to the remote Docling Serve instance.
* Docling processes the document using the configured conversion options.
* The plugin exports the result as Markdown and returns it to Paperless-ngx.

## Important Notes

* **Docling Serve is deployed separately.**

  * Installing this package does not install or start Docling Serve.
* **Network access is required.**

  * The Paperless-ngx host must be able to reach the configured service URL.
* **Parser selection matters.**

  * `PAPERLESS_DOCLING_SCORE` affects parser selection in Paperless-ngx; it is not a Docling OCR or model parameter.
* **Format lists do not guarantee conversion success.**

  * The parser's format list, Docling Serve capabilities, and configured pipeline must all support the requested conversion.
* **HTML and MHTML are excluded from the example.**

  * Other Paperless-ngx parsers may still handle these formats.
* **Check preset behavior.**

  * A configuration variable being defined does not necessarily mean it is passed to the remote conversion request.
* **Protect credentials.**

  * Do not commit service tokens or other secrets to version control.
* **Check version compatibility.**

  * Confirm compatibility among Paperless-ngx, this parser, the service client, and Docling Serve before upgrading production systems.

## Troubleshooting

* **Connection errors**

  * Check `PAPERLESS_DOCLING_SERVE_URL`.
  * Verify network connectivity and inspect Docling Serve logs.
* **Authentication errors**

  * Confirm that the token file exists, is readable, and contains the correct token.
* **Conversion timeouts**

  * Inspect service logs and resource utilization.
  * Increase `PAPERLESS_DOCLING_DEADLINE_SECONDS` if appropriate.
* **Unexpected parser selection**

  * Review `PAPERLESS_DOCLING_SCORE` and other parsers that support the same MIME type.
* **Missing OCR text, tables, or images**

  * Review the related configuration options and confirm that the deployed Docling Serve version supports them.

## License

See the repository's license file for applicable license terms.
