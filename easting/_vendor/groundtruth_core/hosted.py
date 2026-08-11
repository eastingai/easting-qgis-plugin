"""Client for the hosted Easting API — what the QGIS plugin actually calls.

The plugin ships no prompt, no model choice and no provider credentials: it uploads a
PDF to `POST {api_url}/v1/extract` with an `east_live_...` bearer token and gets
back an extraction plus the authoritative GroundTruth verdicts. Stdlib only,
like everything the plugin vendors.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .model import DeedExtraction
from .result import ExtractionError, ExtractionResult
from .served import DocumentVerdict, ServerVerdict
from .transport import error_detail, post_with_retries

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
# What one request can carry. The service runs behind a platform that
# base64-encodes request bodies, so a direct upload caps near 4.5 MB; anything
# larger goes through staging, which uploads to storage directly and hands the
# service an id. The client picks the path by size, and the user sees no
# difference beyond a large document now working at all.
MAX_PDF_BYTES = 4_500_000
# What a staged document may weigh. The service refuses more.
MAX_STAGED_BYTES = 32 * 1024 * 1024


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
    data = path.read_bytes()
    if len(data) > MAX_PDF_BYTES:
        payload = post_extract_staged(api_url, api_key, data, doc_name=path.name, timeout=timeout)
    else:
        payload = post_extract(
            api_url,
            api_key,
            data,
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


def submit_batch(
    api_url: str,
    api_key: str,
    documents: list[tuple[str, bytes]],
    timeout: float = TIMEOUT,
) -> dict:
    """Submit several PDFs as one batch. Returns the server's 202 body.

    Transport only, like everything else here: the multipart body is assembled
    and posted, and the server decides admission, quota, and cost. A 402 comes
    back through `_on_error` with the server's own numbers, which the plugin
    shows verbatim.
    """
    if not documents:
        raise ExtractionError("Choose at least one PDF to extract.")

    # A multipart batch shares ONE request ceiling across every document in it,
    # so five ordinary one-megabyte sheets already exceed it. Staging has no
    # shared ceiling: each document goes to storage on its own and the batch
    # carries only ids. Folders of scans are the normal case here, so this
    # branch is the one that usually runs.
    total = sum(len(data) for _, data in documents)
    if total > MAX_PDF_BYTES or any(len(data) > MAX_PDF_BYTES for _, data in documents):
        return submit_batch_staged(api_url, api_key, documents, timeout=timeout)

    boundary = "----easting" + uuid.uuid4().hex
    body = bytearray()
    for name, data in documents:
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="files"; filename="{_ascii(name)}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    raw = post_with_retries(
        api_url.rstrip("/") + "/v1/batches",
        bytes(body),
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": f"multipart/form-data; boundary={boundary}",
            "user-agent": "easting-qgis-plugin/0.7",
        },
        max_retries=0,  # a resubmit would double-charge; let the user retry
        timeout=timeout,
        retriable=frozenset(),
        on_error=_on_error,
        service="the Easting API",
    )
    return _json_body(raw)


def submit_batch_staged(
    api_url: str,
    api_key: str,
    documents: list[tuple[str, bytes]],
    timeout: float = TIMEOUT,
) -> dict:
    """Stage every document, then submit the batch as a list of ids.

    Each object is deleted as the service claims it at submit time, so a batch
    the service refuses (over quota, say) leaves nothing staged either.
    """
    ids: list[str] = []
    names: dict[str, str] = {}
    for name, data in documents:
        upload_id = stage_document(api_url, api_key, data, timeout=timeout)
        ids.append(upload_id)
        names[upload_id] = name

    raw = post_with_retries(
        api_url.rstrip("/") + "/v1/batches",
        json.dumps({"uploads": ids, "names": names}).encode("utf-8"),
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "user-agent": "easting-qgis-plugin/0.7",
        },
        max_retries=0,  # a resubmit would double-charge; let the user retry
        timeout=timeout,
        retriable=frozenset(),
        on_error=_on_error,
        service="the Easting API",
    )
    return _json_body(raw)


def stage_document(api_url: str, api_key: str, pdf_bytes: bytes, timeout: float = TIMEOUT) -> str:
    """Upload a document to staging and return the id that stands for it.

    Two calls: ask the service for a slot, then PUT the bytes straight to the
    storage URL it hands back. The document never travels through the API,
    which is the whole point — that path has a request-size ceiling this one
    does not.
    """
    if len(pdf_bytes) > MAX_STAGED_BYTES:
        raise ExtractionError(
            f"This document is {len(pdf_bytes) / 1e6:.0f} MB; the limit is "
            f"{MAX_STAGED_BYTES // (1024 * 1024)} MB."
        )
    slot = _json_body(
        post_with_retries(
            api_url.rstrip("/") + "/v1/uploads",
            json.dumps({"bytes": len(pdf_bytes)}).encode("utf-8"),
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
                "user-agent": "easting-qgis-plugin/0.7",
            },
            max_retries=MAX_RETRIES,
            timeout=60.0,
            retriable=RETRIABLE,
            on_error=_on_error,
            service="the Easting API",
        )
    )
    put_url = slot.get("put_url") or ""
    if not put_url.startswith("https://"):
        raise ExtractionError("The Easting API returned an unusable upload URL.")

    request = urllib.request.Request(
        put_url,
        data=pdf_bytes,
        method="PUT",
        # No authorization header: the signature is in the URL, and adding one
        # would change the request the signature covers. The content type is
        # generic for the same reason: it takes part in the signature, and the
        # service reads the real format from the bytes on arrival.
        headers={"content-type": "application/octet-stream"},
    )
    try:
        # Scheme checked above.
        with urllib.request.urlopen(request, timeout=timeout):  # nosec B310
            pass
    except urllib.error.HTTPError as exc:
        raise ExtractionError(
            f"Uploading the document failed ({exc.code}). {error_detail(exc)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ExtractionError(f"Uploading the document failed: {exc.reason}") from exc
    return str(slot.get("upload_id") or "")


def submit_extract(
    api_url: str,
    api_key: str,
    pdf_bytes: bytes,
    doc_name: str = "",
    timeout: float = 120.0,
) -> dict:
    """Hand one document over for asynchronous extraction, and return its job.

    The answer arrives in milliseconds and carries a job id to poll. This is
    the path that survives a long document: nothing holds a connection open
    while the extraction runs, so the edge's own request ceiling stops
    applying. A document too large for one request stages first, exactly as
    the synchronous path does.
    """
    headers = {
        "authorization": f"Bearer {api_key}",
        "user-agent": "easting-qgis-plugin/0.7",
    }
    if doc_name:
        headers["x-document-name"] = _ascii(doc_name)

    if len(pdf_bytes) > MAX_PDF_BYTES:
        headers["x-upload-id"] = stage_document(api_url, api_key, pdf_bytes, timeout=timeout)
        body = b""
        # An upload id works exactly once, so a retried submit would 404
        # rather than resubmit. The staging PUT is the retriable half.
        retries = 0
    else:
        headers["content-type"] = "application/pdf"
        body = pdf_bytes
        retries = MAX_RETRIES

    payload = _json_body(
        post_with_retries(
            api_url.rstrip("/") + "/v1/extracts",
            body,
            headers=headers,
            max_retries=retries,
            timeout=timeout,
            retriable=RETRIABLE,
            on_error=_on_error,
            service="the Easting API",
        )
    )
    if not payload.get("job"):
        raise ExtractionError("The Easting API did not return a job id.")
    return payload


def get_extract(api_url: str, api_key: str, job_id: str, timeout: float = 60.0) -> dict:
    """One poll. Carries the whole extraction once the status is `succeeded`."""
    return _get_json(f"{api_url.rstrip('/')}/v1/extracts/{job_id}", api_key, timeout)


def post_extract_staged(
    api_url: str,
    api_key: str,
    pdf_bytes: bytes,
    doc_name: str = "",
    timeout: float = TIMEOUT,
) -> dict:
    """Stage a large document, then extract it by id.

    The service deletes the staged object as it reads it, so this is one
    document's worth of custody rather than storage, and a failed extraction
    leaves nothing behind.
    """
    upload_id = stage_document(api_url, api_key, pdf_bytes, timeout=timeout)
    headers = {
        "authorization": f"Bearer {api_key}",
        "x-upload-id": upload_id,
        "user-agent": "easting-qgis-plugin/0.7",
    }
    if doc_name:
        headers["x-document-name"] = _ascii(doc_name)
    raw = post_with_retries(
        api_url.rstrip("/") + "/v1/extract",
        b"",
        headers=headers,
        # An upload id works exactly once, so a retry would 404 rather than
        # re-extract. The staging PUT is the retriable half.
        max_retries=0,
        timeout=timeout,
        retriable=frozenset(),
        on_error=_on_error,
        service="the Easting API",
    )
    payload = _json_body(raw)
    if "extraction" not in payload:
        raise ExtractionError("The Easting API response was missing the extraction.")
    return payload


def get_batch(api_url: str, api_key: str, batch_id: str, timeout: float = 60.0) -> dict:
    """Poll one batch's status."""
    return _get_json(f"{api_url.rstrip('/')}/v1/batches/{batch_id}", api_key, timeout)


def fetch_batch_results(api_url: str, api_key: str, batch_id: str, timeout: float = 300.0) -> dict:
    """Collect a finished batch. 409 until it ends, which the caller avoids by
    polling `get_batch` first."""
    return _get_json(f"{api_url.rstrip('/')}/v1/batches/{batch_id}/results", api_key, timeout)


def fetch_dxf(api_url: str, api_key: str, payload: dict, timeout: float = 120.0) -> bytes:
    """Convert an extraction into a DXF, server-side.

    Takes the response payload the plugin kept (`ExtractionResult.raw`) rather
    than a re-serialization of the parsed model: the server reads keys this
    version may not know about, and a lossy round trip would silently drop
    them.
    """
    return _post_artifact(api_url, "/v1/dxf", api_key, payload, timeout)


def fetch_geopackage(api_url: str, api_key: str, payload: dict, timeout: float = 120.0) -> bytes:
    """Convert an extraction into a GeoPackage, server-side.

    The same bargain as the DXF: the retained payload goes back up byte for
    byte, and the layers that come down are the ones the portal serves, so a
    GeoPackage saved here and one saved in a browser are the same file.
    """
    return _post_artifact(api_url, "/v1/geopackage", api_key, payload, timeout)


def fetch_certificate(api_url: str, api_key: str, payload: dict, timeout: float = 120.0) -> bytes:
    """Render the extraction as a certificate PDF, server-side.

    The retained payload matters more here than it does for the DXF. The
    service checks a signature over the response exactly as it was returned,
    so anything rebuilt from the parsed model would arrive with fields
    reordered or dropped and be refused as modified.
    """
    return _post_artifact(api_url, "/v1/certificate", api_key, payload, timeout)


def _post_artifact(api_url: str, path: str, api_key: str, payload: dict, timeout: float) -> bytes:
    """One request shape for every document the service renders for us."""
    return post_with_retries(
        api_url.rstrip("/") + path,
        json.dumps(payload).encode("utf-8"),
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "user-agent": "easting-qgis-plugin/0.7",
        },
        max_retries=MAX_RETRIES,
        timeout=timeout,
        retriable=RETRIABLE,
        on_error=_on_error,
        service="the Easting API",
    )


def _get_json(url: str, api_key: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "authorization": f"Bearer {api_key}",
            "user-agent": "easting-qgis-plugin/0.7",
        },
    )
    if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise ExtractionError("The Easting API URL must be https.")
    try:
        # Scheme checked above.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return _json_body(response.read())
    except urllib.error.HTTPError as exc:
        detail = error_detail(exc)
        message = _on_error(exc.code, detail)
        raise ExtractionError(message or f"The Easting API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ExtractionError(f"Could not reach the Easting API: {exc.reason}") from exc


def _json_body(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExtractionError(f"The Easting API returned an unreadable response: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExtractionError("The Easting API returned an unexpected response.")
    return payload


def result_from_payload(payload: dict) -> ExtractionResult:
    """Parse a `/v1/extract` body into an ExtractionResult.

    The verdict block is not optional here: it carries the geometry the plugin
    places with, so a response missing it is unusable rather than degraded.

    The whole payload is kept on the result. Parsing drops keys this version
    does not know about, and any feature that hands the response back to the
    service (converting it to a drawing, say) has to send what the server
    actually said rather than our reading of it.
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
        raw=payload,
        # Absent on deeds and on every server before 0.6.0, so `.get()` rather
        # than a key read: an old server plus a new plugin has to keep working.
        document_verdict=(
            DocumentVerdict.from_dict(payload["document_verdict"])
            if payload.get("document_verdict")
            else None
        ),
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

    # Geometry is owed only where there are courses to draw. A FAIL has none;
    # neither does an easement that declines to locate itself (blanket,
    # drawing-only, facility-relative) — those arrive as REVIEW with an empty
    # call list and only the burden to show. A tract WITH boundary calls and
    # no geometry means the server predates engine 0.3.
    if any(
        v.geometry is None and v.status != "FAIL" and t.calls
        for t, v in zip(extraction.tracts, verdicts, strict=True)
    ):
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
