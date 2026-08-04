"""Client for the hosted Easting API — what the QGIS plugin actually calls.

The plugin ships no prompt, no model choice and no provider credentials: it uploads a
PDF to `POST {api_url}/v1/extract` with an `east_live_...` bearer token and gets
back an extraction plus the authoritative GroundTruth verdicts. Stdlib only,
like everything the plugin vendors.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import DeedExtraction
from .result import ExtractionError, ExtractionResult
from .served import ServerVerdict
from .transport import post_with_retries

# The plugin carries no traverse math, so a response without geometry leaves it
# with nothing to place. Say so plainly instead of failing later and vaguely.
SERVER_TOO_OLD = (
    "This Easting service is older than the plugin: it did not return tract "
    "geometry (the plugin needs engine 0.3 or newer). Update the API URL in "
    "Easting settings, or contact support@easting.ai."
)

# Extraction is 10-120s of work behind this call, so a retry is expensive and
# the server already retries its upstream itself. One extra attempt, no more.
MAX_RETRIES = 1
TIMEOUT = 300.0
RETRIABLE = frozenset({429, 502, 503})
MAX_PDF_BYTES = 32 * 1024 * 1024


def extract_pdf_hosted(
    api_url: str,
    api_key: str,
    pdf_path: str | Path,
    max_retries: int = MAX_RETRIES,
    timeout: float = TIMEOUT,
) -> ExtractionResult:
    """Extract one deed through the Easting API.

    Raises ExtractionError with a message that is safe to show the user
    verbatim — quota and auth failures included.
    """
    path = Path(pdf_path)
    payload = post_extract(
        api_url,
        api_key,
        path.read_bytes(),
        doc_name=path.name,
        max_retries=max_retries,
        timeout=timeout,
    )
    return result_from_payload(payload)


def post_extract(
    api_url: str,
    api_key: str,
    pdf_bytes: bytes,
    doc_name: str = "",
    max_retries: int = MAX_RETRIES,
    timeout: float = TIMEOUT,
) -> dict:
    """POST a PDF and return the decoded response body.

    Exposed separately from `extract_pdf_hosted` so tests can inspect the
    server's `groundtruth_verdicts` alongside the parsed extraction.
    """
    if not api_url:
        raise ExtractionError("No Easting API URL configured. Check Easting settings.")
    if not api_key:
        raise ExtractionError("No Easting API key configured. Check Easting settings.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ExtractionError(
            f"This document is {len(pdf_bytes) / 1e6:.0f} MB; the limit is "
            f"{MAX_PDF_BYTES // (1024 * 1024)} MB."
        )

    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/pdf",
        "user-agent": "easting-qgis-plugin/0.3",
    }
    if doc_name:
        headers["x-document-name"] = _ascii(doc_name)

    raw = post_with_retries(
        api_url.rstrip("/") + "/v1/extract",
        pdf_bytes,
        headers=headers,
        max_retries=max_retries,
        timeout=timeout,
        retriable=RETRIABLE,
        on_error=_on_error,
        service="the Easting API",
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExtractionError(f"The Easting API returned an unreadable response: {exc}") from exc
    if not isinstance(payload, dict) or "extraction" not in payload:
        raise ExtractionError("The Easting API response was missing the extraction.")
    return payload


def result_from_payload(payload: dict) -> ExtractionResult:
    """Parse a `/v1/extract` body into an ExtractionResult.

    The verdict block is not optional here: it carries the geometry the plugin
    places with, so a response missing it is unusable rather than degraded.
    """
    try:
        extraction = DeedExtraction.from_dict(payload["extraction"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExtractionError(f"Could not parse the extraction response: {exc}") from exc

    verdicts = _verdicts_for(extraction, payload.get("groundtruth_verdicts"))

    usage = payload.get("usage") or {}
    return ExtractionResult(
        extraction=extraction,
        model=str(payload.get("model") or "easting"),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        seconds=float(usage.get("seconds") or 0.0),
        verdicts=verdicts,
    )


def _verdicts_for(extraction: DeedExtraction, raw: object) -> list[ServerVerdict]:
    """Parse and sanity-check the verdict block against the tracts it describes."""
    if not extraction.tracts:
        # No tracts means nothing to judge — an aliquot deed, say. An empty
        # verdict list is correct, not a protocol error.
        return []

    if not isinstance(raw, list) or len(raw) != len(extraction.tracts):
        raise ExtractionError(SERVER_TOO_OLD)

    try:
        verdicts = [ServerVerdict.from_dict(v) for v in raw]
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExtractionError(f"Could not parse the GroundTruth verdicts: {exc}") from exc

    # A FAIL legitimately has no geometry; anything else must carry it.
    if any(v.geometry is None and v.status != "FAIL" for v in verdicts):
        raise ExtractionError(SERVER_TOO_OLD)
    return verdicts


def _on_error(status: int, detail: str) -> str | None:
    """Map the API's error envelope onto messages a surveyor can act on.

    402 and 413 carry server-composed detail (quota numbers, size limits), so
    those pass through rather than being restated here.
    """
    if status == 401:
        return "Invalid Easting API key. Check Easting settings."
    if status == 403:
        return f"This Easting API key is not permitted: {detail}"
    if status in (402, 413, 422):
        return detail
    if status == 404:
        return (
            "The Easting API URL is wrong — no extraction endpoint there. Check Easting settings."
        )
    return None


def _ascii(value: str) -> str:
    """Header values must be latin-1; deed filenames are not always."""
    return value.encode("ascii", "replace").decode("ascii")[:200]
