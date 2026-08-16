"""The server's judgement, as the plugin sees it.

These dataclasses mirror the `groundtruth_verdicts` block of a `/v1/extract`
response. They are the plugin's only source of verdicts and geometry; nothing
here computes anything.

Stdlib only, like everything the plugin ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import BearingModel


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
class SuggestedLocation:
    """Where the server thinks a tract sits, and what it compared to say so.

    A deed defines shape, not position, so this is a suggestion the operator
    confirms rather than a result: `status` is REVIEW even when everything
    agreed. `pob` is None whenever the arithmetic could not finish or the
    result failed its own containment check, and `reasons` then says which
    corner was tried and what went wrong. Absent from every server older than
    the georeferencing assist, which is why readers go through `.get()`.
    """

    pob: tuple[float, float] | None = None  # lon/lat
    # None when nothing solved rotation. The PLSS anchor never does: it can
    # only assume the bearing basis is grid north, and says so in `reasons`.
    rotation_deg: float | None = None
    source: str = ""  # "parcel_fit" | "plss_corner" | "parcel_lookup"
    basis: str = ""
    plss_id: str = ""
    status: str = "REVIEW"
    reasons: list[str] = field(default_factory=list)
    crs: str = "EPSG:4326"

    @property
    def placeable(self) -> bool:
        """There is a position to preview."""
        return self.pob is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "crs": self.crs,
            "pob": list(self.pob) if self.pob is not None else None,
            "rotation_deg": self.rotation_deg,
            "source": self.source,
            "basis": self.basis,
            "plss_id": self.plss_id,
            "status": self.status,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SuggestedLocation:
        raw = d.get("pob")
        pob: tuple[float, float] | None = None
        if raw is not None:
            try:
                lon, lat = raw
                pob = (float(lon), float(lat))
            except (TypeError, ValueError):
                pob = None
        return cls(
            pob=pob,
            rotation_deg=(None if d.get("rotation_deg") is None else float(d["rotation_deg"])),
            source=d.get("source") or "",
            basis=d.get("basis") or "",
            plss_id=d.get("plss_id") or "",
            status=d.get("status") or "REVIEW",
            reasons=[str(r) for r in d.get("reasons") or []],
            crs=d.get("crs") or "EPSG:4326",
        )


def plain_value(value: Any) -> Any:
    """A corrected value as JSON carries it.

    Bearings are the only structured one; everything else a correction can
    name is a number or a word.
    """
    if isinstance(value, BearingModel):
        return {
            "ns": value.ns,
            "degrees": value.degrees,
            "minutes": value.minutes,
            "seconds": value.seconds,
            "ew": value.ew,
        }
    return value


@dataclass(frozen=True)
class Correction:
    """One changed field on one call.

    `before` is not decoration. It is what lets the certificate print a diff
    without holding a second copy of the extraction, and it is the staleness
    guard: a client that fetched a document, sat on it while somebody
    re-extracted, and then posted an edit will name a `before` that no longer
    matches, and gets refused rather than silently overwriting a different
    number.
    """

    tract: int
    call: int
    field: str
    before: Any
    after: Any
    kind: str = "boundary"  # "boundary" | "tie"
    by: str = ""
    at: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Correction:
        return cls(
            tract=int(d.get("tract") or 0),
            call=int(d.get("call") or 0),
            field=str(d.get("field") or ""),
            before=d.get("before"),
            after=d.get("after"),
            kind=str(d.get("kind") or "boundary"),
            by=str(d.get("by") or ""),
            at=str(d.get("at") or ""),
            note=str(d.get("note") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tract": self.tract,
            "call": self.call,
            "field": self.field,
            "before": plain_value(self.before),
            "after": plain_value(self.after),
            "kind": self.kind,
            "by": self.by,
            "at": self.at,
            "note": self.note,
        }


@dataclass(frozen=True)
class Adjustment:
    """A disclosed best fit over courses that do not close.

    Carries what it distributed, because an adjustment whose size nobody can
    see is the silent version. `misclosure_before` is the number the verdict
    above it still reports.
    """

    tract: int
    method: str = "compass_rule"
    misclosure_before: float = 0.0
    perimeter: float = 0.0
    by: str = ""
    at: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Adjustment:
        return cls(
            tract=int(d.get("tract") or 0),
            method=str(d.get("method") or "compass_rule"),
            misclosure_before=float(d.get("misclosure_before") or 0.0),
            perimeter=float(d.get("perimeter") or 0.0),
            by=str(d.get("by") or ""),
            at=str(d.get("at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tract": self.tract,
            "method": self.method,
            "misclosure_before": round(self.misclosure_before, 4),
            "perimeter": round(self.perimeter, 2),
            "by": self.by,
            "at": self.at,
        }


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
    # Filled by /v1/locate, never by /v1/extract: a suggested position the
    # operator confirms with the same click they already make.
    location: SuggestedLocation | None = None
    # Present from 0.5+ servers: where this tract's POB sits relative to its
    # commencement monument ([east_ft, north_ft]), and which monument that
    # is. Tracts sharing an anchor place as a group with one click.
    tie_offset: tuple[float, float] | None = None
    anchor: str | None = None
    # Set by /v1/corrections, never by /v1/extract. `corrected` says an
    # operator supplied a better reading and this verdict was recomputed over
    # it; `adjusted` says geometry was fitted afterwards and the status
    # deliberately did not move. Rendering them as the same badge would erase
    # the only distinction that matters here.
    corrected: bool = False
    adjusted: bool = False

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
            location=(SuggestedLocation.from_dict(d["location"]) if d.get("location") else None),
            tie_offset=_offset_of(d.get("tie_offset")),
            anchor=(str(d["anchor"]) if d.get("anchor") else None),
            corrected=bool(d.get("corrected")),
            adjusted=bool(d.get("adjusted")),
        )
