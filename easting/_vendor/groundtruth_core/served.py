"""The server's judgement, as the plugin sees it.

These dataclasses mirror the `groundtruth_verdicts` block of a `/v1/extract`
response. They are the plugin's only source of verdicts and geometry; nothing
here computes anything.

Stdlib only, like everything the plugin ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TractGeometry:
    """A computed traverse in its local plane: POB at the origin, feet.

    `vertices` is one point per call endpoint plus the start, so a tract with N
    calls has N+1 vertices. When the traverse closes, the last vertex is the
    start again (within `misclosure`); when it does not, the caller closes the
    ring itself.
    """

    vertices: list[tuple[float, float]] = field(default_factory=list)
    misclosure: float = 0.0
    perimeter_ft: float = 0.0
    # None means the traverse closes exactly — JSON cannot carry Infinity.
    closure_denominator: float | None = None
    acres: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TractGeometry:
        return cls(
            vertices=[(float(x), float(y)) for x, y in d.get("vertices") or []],
            misclosure=float(d.get("misclosure") or 0.0),
            perimeter_ft=float(d.get("perimeter_ft") or 0.0),
            closure_denominator=(
                None if d.get("closure_denominator") is None else float(d["closure_denominator"])
            ),
            acres=float(d.get("acres") or 0.0),
        )


@dataclass
class GeorefGeometry:
    """Georeferenced geometry for an aliquot tract: lon/lat rings straight
    from the PLSS fabric, so placement needs no POB click. Metes-and-bounds
    verdicts never carry one (a deed defines shape, not location)."""

    rings: list[list[tuple[float, float]]] = field(default_factory=list)
    acres: float = 0.0
    plss_id: str = ""
    source: str = ""
    crs: str = "EPSG:4326"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GeorefGeometry:
        return cls(
            rings=[[(float(x), float(y)) for x, y in ring] for ring in d.get("rings") or []],
            acres=float(d.get("acres") or 0.0),
            plss_id=d.get("plss_id") or "",
            source=d.get("source") or "",
            crs=d.get("crs") or "EPSG:4326",
        )


@dataclass
class DocumentVerdict:
    """The verdict on a document that produces no tracts.

    Plats and utility as-builts are identified rather than traced, so their
    `groundtruth_verdicts` array is empty and this block carries the judgement
    instead. Absent on deeds and easements, and absent from any server older
    than 0.6.0, which is why every reader goes through `.get()`.
    """

    document: str = ""
    status: str = "REVIEW"  # "REVIEW" | "FAIL"
    reasons: list[str] = field(default_factory=list)
    summary: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DocumentVerdict:
        return cls(
            document=d.get("document") or "",
            status=d.get("status") or "REVIEW",
            reasons=[str(r) for r in d.get("reasons") or []],
            summary=d.get("summary") or "",
        )


def _offset_of(raw: Any) -> tuple[float, float] | None:
    """Parse [east_ft, north_ft] defensively; anything malformed is None."""
    try:
        x, y = raw  # type: ignore[misc]
        return (float(x), float(y))
    except (TypeError, ValueError):
        return None


@dataclass
class ServerVerdict:
    """One tract's GroundTruth verdict, index-aligned with `extraction.tracts`."""

    tract: str
    status: str  # "PASS" | "REVIEW" | "FAIL"
    reasons: list[str] = field(default_factory=list)
    closure_ratio: str | None = None
    computed_acres: float | None = None
    # Present from 0.5+; small easements are verified in square feet.
    computed_sqft: float | None = None
    # The same area in metric, and the unit the document was measured in.
    # A metric document is reviewed in metric: the server converts once, so
    # nothing here has to know what a vara is.
    computed_sqm: float | None = None
    computed_hectares: float | None = None
    distance_unit: str = "feet"
    # None when the tract could not be traversed at all, which is exactly the
    # case where placement must be refused.
    geometry: TractGeometry | None = None
    # Present on aliquot verdicts from 0.4+ servers; .get() keeps older
    # responses parsing. When set, the plugin can place without a POB click.
    georef: GeorefGeometry | None = None
    # Present from 0.5+ servers: where this tract's POB sits relative to its
    # commencement monument ([east_ft, north_ft]), and which monument that
    # is. Tracts sharing an anchor place as a group with one click.
    tie_offset: tuple[float, float] | None = None
    anchor: str | None = None

    @property
    def groupable(self) -> bool:
        """This tract can join a one-click placement group."""
        return self.placeable and self.tie_offset is not None and self.anchor is not None

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    @property
    def placeable(self) -> bool:
        return self.geometry is not None and len(self.geometry.vertices) >= 2

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ServerVerdict:
        geometry = d.get("geometry")
        georef = d.get("georef")
        return cls(
            tract=d.get("tract") or "",
            status=d.get("status") or "FAIL",
            reasons=[str(r) for r in d.get("reasons") or []],
            closure_ratio=d.get("closure_ratio"),
            computed_acres=(
                None if d.get("computed_acres") is None else float(d["computed_acres"])
            ),
            computed_sqft=(None if d.get("computed_sqft") is None else float(d["computed_sqft"])),
            computed_sqm=(None if d.get("computed_sqm") is None else float(d["computed_sqm"])),
            computed_hectares=(
                None if d.get("computed_hectares") is None else float(d["computed_hectares"])
            ),
            distance_unit=d.get("distance_unit") or "feet",
            geometry=TractGeometry.from_dict(geometry) if geometry else None,
            georef=GeorefGeometry.from_dict(georef) if georef else None,
            tie_offset=_offset_of(d.get("tie_offset")),
            anchor=(str(d["anchor"]) if d.get("anchor") else None),
        )
