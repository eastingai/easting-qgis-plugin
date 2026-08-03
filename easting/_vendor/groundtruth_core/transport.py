"""Retrying HTTP POST over urllib, shared by the Anthropic and hosted clients.

Stdlib only: QGIS's bundled Python has no requests. Callers supply an `on_error`
hook to turn specific status codes into user-facing messages; anything they do
not claim falls through to retry-or-raise.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .result import ExtractionError

# 429 and 529 are explicit backpressure; 500/502/503 are transient upstream
# failures. 400/401/402/413/422 never retry — the request itself is the problem.
RETRIABLE = frozenset({429, 500, 502, 503, 529})


def post_with_retries(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    max_retries: int = 3,
    timeout: float = 600.0,
    retriable: frozenset[int] = RETRIABLE,
    on_error: Callable[[int, str], str | None] | None = None,
    service: str = "the API",
) -> bytes:
    """POST with exponential backoff, returning the raw response body.

    `on_error(status, detail)` returns a user-facing message to raise straight
    away, or None to let this function decide (retry if retriable, else raise).
    """
    delay = 2.0
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = error_detail(exc)
            if on_error is not None:
                message = on_error(exc.code, detail)
                if message is not None:
                    raise ExtractionError(message) from exc
            if exc.code in retriable and attempt < max_retries:
                last_err = exc
                time.sleep(delay)
                delay *= 2
                continue
            raise ExtractionError(f"{service} returned error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < max_retries:
                last_err = exc
                time.sleep(delay)
                delay *= 2
                continue
            raise ExtractionError(f"Network error reaching {service}: {exc.reason}") from exc
    raise ExtractionError(f"API request failed after {max_retries + 1} attempts: {last_err}")


def error_detail(exc: urllib.error.HTTPError) -> str:
    """Pull a human message out of an error body.

    Anthropic and the Easting API both use `{"error": {"message": ...}}`, so one
    reader covers both.
    """
    try:
        return json.loads(exc.read().decode("utf-8"))["error"]["message"]
    except Exception:
        return exc.reason or str(exc.code)
