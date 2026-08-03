"""The extraction outcome types, shared by every transport.

These live apart from client.py because the QGIS plugin vendors them but must
not vendor client.py: that module carries the extraction prompt, which is
server-side property now that the plugin talks to the Easting API.
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
    # empty on the direct-to-Anthropic path, which the server uses and which
    # runs verdicts separately.
    verdicts: list[ServerVerdict] = field(default_factory=list)
