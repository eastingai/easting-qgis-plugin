"""The extraction outcome types, shared by every transport.

Shared outcome types for extraction responses, stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import DeedExtraction
from .served import ServerVerdict


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
