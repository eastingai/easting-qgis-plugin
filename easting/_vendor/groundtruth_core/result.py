"""The extraction outcome types, shared by every transport.

Shared outcome types for extraction responses, stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import DeedExtraction
from .served import DocumentVerdict, ServerVerdict


class ExtractionError(RuntimeError):
    """Extraction failed for a reason the user needs to see (auth, refusal...)."""


@dataclass
class ExtractionResult:
    extraction: DeedExtraction
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float
    # Index-aligned with extraction.tracts. Populated from a hosted response;
    # empty on the server's direct extraction path, which
    # runs verdicts separately.
    verdicts: list[ServerVerdict] = field(default_factory=list)
    # The server's response body exactly as it arrived, kept only on the hosted
    # path. Parsing is lossy by design (dataclasses drop keys they do not know,
    # which is what lets an old client read a new server), so a feature that
    # has to hand the response back to the service needs the original rather
    # than a re-serialization of what we understood. None off the direct path,
    # where there is no server body to keep.
    raw: dict[str, Any] | None = None
    # The judgement on documents that produce no tracts (plats, as-builts).
    # None on a deed, whose verdicts all live in `verdicts`, and None from any
    # server older than 0.6.0.
    document_verdict: DocumentVerdict | None = None
