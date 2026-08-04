"""Extraction data model: the shapes a /v1/extract response carries.

Stdlib only: the QGIS plugin runs in QGIS's bundled Python, which ships no
third-party packages. Bearings travel as components
(ns/degrees/minutes/seconds/ew), never as a free string.

This module is pure parse and display; all computation happens on the
Easting service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONFIDENCES = ("high", "medium", "low")
DISTANCE_UNITS = ("feet", "varas", "chains", "meters")


@dataclass
class BearingModel:
    ns: str
    degrees: int
    ew: str
    minutes: int = 0
    seconds: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BearingModel:
        return cls(
            ns=d["ns"],
            degrees=int(d["degrees"]),
            ew=d["ew"],
            minutes=int(d.get("minutes") or 0),
            seconds=float(d.get("seconds") or 0.0),
        )

    def text(self) -> str:
        return f"{self.ns} {self.degrees}°{self.minutes:02d}'{self.seconds:04.1f}\" {self.ew}"


@dataclass
class Call:
    call_type: str  # "line" | "curve"
    verbatim_text: str
    confidence: str  # high | medium | low
    monument: str | None = None
    bearing: BearingModel | None = None
    distance: float | None = None
    chord_bearing: BearingModel | None = None
    chord_length: float | None = None
    radius: float | None = None
    curve_direction: str | None = None  # "left" | "right"
    arc_length: float | None = None
    # A line or owner the course runs WITH ("with the line of J.R. Smith",
    # "with Buffalo Creek"), distinct from monument, a point at the call's end.
    adjoiner: str | None = None

    def effective_bearing(self) -> BearingModel | None:
        return self.chord_bearing if self.call_type == "curve" else self.bearing

    def effective_distance(self) -> float | None:
        return self.chord_length if self.call_type == "curve" else self.distance

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Call:
        def bearing(key: str) -> BearingModel | None:
            v = d.get(key)
            return BearingModel.from_dict(v) if v else None

        return cls(
            call_type=d["call_type"],
            verbatim_text=d["verbatim_text"],
            confidence=d.get("confidence") or "medium",
            monument=d.get("monument"),
            bearing=bearing("bearing"),
            distance=_opt_float(d.get("distance")),
            chord_bearing=bearing("chord_bearing"),
            chord_length=_opt_float(d.get("chord_length")),
            radius=_opt_float(d.get("radius")),
            curve_direction=d.get("curve_direction"),
            arc_length=_opt_float(d.get("arc_length")),
            adjoiner=d.get("adjoiner") or None,
        )

    @classmethod
    def tie_from_wire(cls, s: str) -> Call:
        """Parse a tie course from the wire's one-string form (TIE_FORMAT):
        "bearing=N 00 15 05.0 E | distance=318.60 | verbatim=...". Tie courses
        are display-only, so an unparseable string degrades to a bearing-less
        call carrying the verbatim text."""
        rest = s.strip()
        verbatim = ""
        if "verbatim=" in rest:
            rest, _, verbatim = rest.partition("verbatim=")
            verbatim = verbatim.strip().strip("<>")
        bearing: BearingModel | None = None
        distance: float | None = None
        for part in rest.split(" | "):
            key, eq, value = part.strip().rstrip("|").strip().partition("=")
            key, value = key.strip(), value.strip().strip("<>")
            if not eq:
                continue
            if key == "bearing":
                bits = value.split()
                if len(bits) == 5 and bits[0] in ("N", "S") and bits[4] in ("E", "W"):
                    try:
                        bearing = BearingModel(
                            ns=bits[0],
                            degrees=int(bits[1]),
                            minutes=int(bits[2]),
                            seconds=float(bits[3]),
                            ew=bits[4],
                        )
                    except ValueError:
                        bearing = None
            elif key == "distance":
                try:
                    distance = float(value)
                except ValueError:
                    distance = None
        return cls(
            call_type="line",
            verbatim_text=verbatim or s.strip(),
            confidence="medium" if bearing is None else "high",
            bearing=bearing,
            distance=distance,
        )


@dataclass
class RecordingRef:
    """A book/page or instrument reference. All strings: books carry alpha
    prefixes ("RE 3210") and pages arrive as ranges ("350 - 352")."""

    book: str | None = None
    page: str | None = None
    instrument: str | None = None
    note: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RecordingRef:
        return cls(
            book=d.get("book") or None,
            page=d.get("page") or None,
            instrument=d.get("instrument") or None,
            note=d.get("note") or None,
        )

    @classmethod
    def from_wire(cls, s: str) -> RecordingRef:
        """Parse "book=... | page=... | instrument=... | note=..." with every
        segment optional and note greedy to the end. Unparseable input
        degrades to a note-only reference; the citation text survives."""
        rest = s.strip()
        note = ""
        if "note=" in rest:
            rest, _, note = rest.partition("note=")
            note = note.strip()
        fields: dict[str, str] = {}
        matched = False
        for part in rest.split(" | "):
            key, eq, value = part.strip().rstrip("|").strip().partition("=")
            if eq and key.strip() in ("book", "page", "instrument"):
                fields[key.strip()] = value.strip()
                matched = True
        if not matched and not note:
            return cls(note=s.strip() or None)
        return cls(
            book=fields.get("book") or None,
            page=fields.get("page") or None,
            instrument=fields.get("instrument") or None,
            note=note or None,
        )

    def is_empty(self) -> bool:
        return not (self.book or self.page or self.instrument)

    def text(self) -> str:
        parts = []
        if self.book:
            parts.append(f"Book {self.book}")
        if self.page:
            parts.append(f"Page {self.page}")
        if self.instrument:
            parts.append(f"#{self.instrument}")
        return ", ".join(parts) or "(unreferenced)"


@dataclass
class LotBlockRef:
    """A platted-subdivision reference, extracted as metadata only. Plat
    parsing stays out of scope; the reference is what an evaluator, an index,
    or a future chain needs.

    On the wire each reference is ONE delimited string (see LOT_BLOCK_FORMAT);
    this parser rebuilds the structure. Rich dicts parse too."""

    verbatim_text: str
    subdivision: str | None = None
    lot: str | None = None
    block: str | None = None
    plat_book: str | None = None
    plat_page: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LotBlockRef:
        return cls(
            verbatim_text=d.get("verbatim_text") or "",
            subdivision=d.get("subdivision") or None,
            lot=d.get("lot") or None,
            block=d.get("block") or None,
            plat_book=d.get("plat_book") or None,
            plat_page=d.get("plat_page") or None,
        )

    @classmethod
    def from_wire(cls, s: str) -> LotBlockRef:
        """Parse "subdivision=... | lot=... | block=... | plat_book=... |
        plat_page=... | verbatim=..." with every field optional and verbatim
        greedy to the end. An unparseable string degrades to verbatim-only:
        the reference is preserved even when the format is not."""
        fields: dict[str, str] = {}
        rest = s.strip()
        verbatim = ""
        if "verbatim=" in rest:
            rest, _, verbatim = rest.partition("verbatim=")
            verbatim = verbatim.strip().strip("<>")
        matched = False
        for part in rest.split(" | "):
            key, eq, value = part.strip().rstrip("|").strip().partition("=")
            if eq and key.strip() in ("subdivision", "lot", "block", "plat_book", "plat_page"):
                fields[key.strip()] = value.strip()
                matched = True
        if not matched and not verbatim:
            return cls(verbatim_text=s.strip())
        return cls(
            verbatim_text=verbatim or s.strip(),
            subdivision=fields.get("subdivision") or None,
            lot=fields.get("lot") or None,
            block=fields.get("block") or None,
            plat_book=fields.get("plat_book") or None,
            plat_page=fields.get("plat_page") or None,
        )


@dataclass
class DeedMetadata:
    """Who conveyed what to whom, and where the paper trail points.

    Document-level rather than per-tract: the recording stamp and the parties
    belong to the instrument. prior_references are the chain links a future
    title feature consumes; today they are provenance."""

    grantors: list[str] = field(default_factory=list)
    grantees: list[str] = field(default_factory=list)
    recording: RecordingRef | None = None
    prior_references: list[RecordingRef] = field(default_factory=list)
    transfer_date: str | None = None
    transfer_date_verbatim: str | None = None
    county: str | None = None
    state: str | None = None
    lot_block: list[LotBlockRef] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeedMetadata:
        def ref(v: Any) -> RecordingRef:
            return RecordingRef.from_wire(v) if isinstance(v, str) else RecordingRef.from_dict(v)

        recording = d.get("recording")
        parsed = ref(recording) if recording else None
        return cls(
            grantors=[str(g) for g in d.get("grantors") or []],
            grantees=[str(g) for g in d.get("grantees") or []],
            # An all-empty stamp is the wire encoding of "unrecorded".
            recording=None if parsed is None or parsed.is_empty() else parsed,
            prior_references=[
                r for v in d.get("prior_references") or [] if not (r := ref(v)).is_empty() or r.note
            ],
            transfer_date=d.get("transfer_date") or None,
            transfer_date_verbatim=d.get("transfer_date_verbatim") or None,
            county=d.get("county") or None,
            state=d.get("state") or None,
            lot_block=[
                LotBlockRef.from_wire(lb) if isinstance(lb, str) else LotBlockRef.from_dict(lb)
                for lb in d.get("lot_block") or []
            ],
        )

    def is_empty(self) -> bool:
        return not (
            self.grantors
            or self.grantees
            or self.recording
            or self.prior_references
            or self.transfer_date
            or self.county
            or self.state
            or self.lot_block
        )


ALIQUOT_TOKENS = ("N2", "S2", "E2", "W2", "NE4", "NW4", "SE4", "SW4")


@dataclass
class AliquotDescription:
    """A PLSS quarter/half chain, pure data; the Easting service computes
    the geometry.

    parts reads in document order and applies right to left: ["SE4","SW4"]
    is "SE 1/4 of the SW 1/4". government_lot and exception_text force REVIEW
    in v1: a government lot needs the fabric's second division, and a metric
    strip or road exception is not representable as quarter/half arithmetic.
    """

    verbatim_text: str
    parts: list[str] = field(default_factory=list)
    section: int = 0
    township_number: int = 0
    township_direction: str = "N"  # "N" | "S"
    range_number: int = 0
    range_direction: str = "E"  # "E" | "W"
    meridian: str | None = None
    government_lot: str | None = None
    exception_text: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AliquotDescription:
        return cls(
            verbatim_text=d.get("verbatim_text") or "",
            parts=[str(t) for t in d.get("parts") or []],
            section=int(d.get("section") or 0),
            township_number=int(d.get("township_number") or 0),
            township_direction=d.get("township_direction") or "N",
            range_number=int(d.get("range_number") or 0),
            range_direction=d.get("range_direction") or "E",
            meridian=d.get("meridian") or None,
            government_lot=str(d["government_lot"])
            if d.get("government_lot") not in (None, "")
            else None,
            exception_text=d.get("exception_text") or None,
        )

    def chain_text(self) -> str:
        names = {
            "N2": "N\u00bd",
            "S2": "S\u00bd",
            "E2": "E\u00bd",
            "W2": "W\u00bd",
            "NE4": "NE\u00bc",
            "NW4": "NW\u00bc",
            "SE4": "SE\u00bc",
            "SW4": "SW\u00bc",
        }
        chain = " of ".join(names.get(t, t) for t in self.parts)
        return (
            f"{chain} of Section {self.section}, "
            f"T{self.township_number}{self.township_direction} "
            f"R{self.range_number}{self.range_direction}"
        )


@dataclass
class Tract:
    name: str
    pob_description: str
    calls: list[Call] = field(default_factory=list)
    stated_acreage: float | None = None
    distance_unit: str = "feet"
    is_exception: bool = False
    # Tie courses, structured. The verbatim tie text stays in pob_description
    # (the fallback older clients render); these are the same courses as Calls
    # so the dock can table them. Never part of the boundary: the verdict
    # consumes tract.calls only.
    tie_calls: list[Call] = field(default_factory=list)
    # "metes_and_bounds" | "aliquot", per tract: mixed documents exist (an
    # aliquot conveyance with a metes-and-bounds LESS AND EXCEPT parcel).
    description_type: str = "metes_and_bounds"
    aliquot: AliquotDescription | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Tract:
        return cls(
            name=d.get("name") or "TRACT 1",
            pob_description=d.get("pob_description") or "",
            calls=[Call.from_dict(c) for c in d.get("calls") or []],
            stated_acreage=_opt_float(d.get("stated_acreage")),
            distance_unit=d.get("distance_unit") or "feet",
            is_exception=bool(d.get("is_exception") or False),
            tie_calls=[
                Call.tie_from_wire(c) if isinstance(c, str) else Call.from_dict(c)
                for c in d.get("tie_calls") or []
            ],
            description_type=d.get("description_type") or "metes_and_bounds",
            aliquot=_parse_aliquot_value(d.get("aliquot")),
        )


@dataclass
class DeedExtraction:
    document_kind: str
    legal_description_found: bool
    tracts: list[Tract] = field(default_factory=list)
    basis_of_bearings: str | None = None
    notes: str | None = None
    # Nullable so a pre-metadata server's response still parses; .get() keeps
    # old plugins working against a new server the same way.
    metadata: DeedMetadata | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeedExtraction:
        metadata = d.get("metadata")
        parsed = DeedMetadata.from_dict(metadata) if metadata else None
        return cls(
            document_kind=d.get("document_kind") or "other",
            legal_description_found=bool(d.get("legal_description_found")),
            tracts=[Tract.from_dict(t) for t in d.get("tracts") or []],
            basis_of_bearings=d.get("basis_of_bearings"),
            notes=d.get("notes"),
            metadata=None if parsed is None or parsed.is_empty() else parsed,
        )


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _parse_aliquot_value(v: Any) -> AliquotDescription | None:
    if not v:
        return None
    if isinstance(v, str):
        return parse_aliquot_wire(v)
    if v.get("parts") or v.get("section"):
        return AliquotDescription.from_dict(v)
    return None


# JSON Schema for the extraction shapes above: additionalProperties false
# and full required lists on every object.
_BEARING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ns": {"type": "string", "enum": ["N", "S"]},
        "degrees": {"type": "integer"},
        "minutes": {"type": "integer"},
        "seconds": {"type": "number"},
        "ew": {"type": "string", "enum": ["E", "W"]},
    },
    "required": ["ns", "degrees", "minutes", "seconds", "ew"],
    "additionalProperties": False,
}

_NULLABLE_BEARING = {"anyOf": [_BEARING_SCHEMA, {"type": "null"}]}
_NULLABLE_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

# References and lot entries cross the wire as one delimited string each
# (REF_FORMAT / LOT_BLOCK_FORMAT); RecordingRef.from_wire and
# LotBlockRef.from_wire rebuild the structure, and unparseable strings degrade
# to note-only/verbatim-only rather than lose the citation. Absent scalars
# cross as "" and normalize back to None.
REF_FORMAT = "book=<book> | page=<page> | instrument=<number> | note=<what it is for>"
LOT_BLOCK_FORMAT = (
    "subdivision=<name> | lot=<lot> | block=<block> | plat_book=<book> | "
    "plat_page=<page> | verbatim=<sentence>"
)
# Tie courses ride flat too (display-only, so a lossy parse costs a table
# row, not geometry). Bearing is "N <deg> <min> <sec> E" style.
TIE_FORMAT = "bearing=<N 00 15 05.0 E> | distance=<318.60> | verbatim=<the course text>"
# The aliquot chain rides as one delimited string per tract;
# parse_aliquot_wire rebuilds it.
ALIQUOT_FORMAT = (
    "parts=<SE4 SW4 SE4> | section=<9> | township=<17S> | range=<18E> | "
    "meridian=<name if stated> | government_lot=<number if any> | "
    "exception=<LESS AND EXCEPT text if any> | verbatim=<the sentence>"
)


def parse_aliquot_wire(s: str) -> AliquotDescription | None:
    """Rebuild an AliquotDescription from the wire string (ALIQUOT_FORMAT).
    Returns None for an empty string; unparseable township/range/section
    yield zeros, which the validator turns into FAIL (invalid chain)."""
    text = s.strip()
    if not text:
        return None
    verbatim = ""
    if "verbatim=" in text:
        text, _, verbatim = text.partition("verbatim=")
        verbatim = verbatim.strip().strip("<>")
    fields: dict[str, str] = {}
    for part in text.split(" | "):
        key, eq, value = part.strip().rstrip("|").strip().partition("=")
        if eq:
            fields[key.strip()] = value.strip().strip("<>")
    parts = [t for t in (fields.get("parts") or "").replace(",", " ").split() if t]
    twp = fields.get("township") or ""
    rng = fields.get("range") or ""

    def split_dir(v: str, dirs: str) -> tuple[int, str]:
        v = v.strip().upper()
        if v and v[-1] in dirs:
            try:
                return int(v[:-1]), v[-1]
            except ValueError:
                return 0, v[-1]
        try:
            return int(v), dirs[0]
        except ValueError:
            return 0, dirs[0]

    twp_no, twp_dir = split_dir(twp, "NS")
    rng_no, rng_dir = split_dir(rng, "EW")
    try:
        section = int(fields.get("section") or 0)
    except ValueError:
        section = 0
    if not parts and not section:
        return None
    return AliquotDescription(
        verbatim_text=verbatim or s.strip(),
        parts=parts,
        section=section,
        township_number=twp_no,
        township_direction=twp_dir,
        range_number=rng_no,
        range_direction=rng_dir,
        meridian=fields.get("meridian") or None,
        government_lot=fields.get("government_lot") or None,
        exception_text=fields.get("exception") or None,
    )


_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "grantors": {"type": "array", "items": {"type": "string"}},
        "grantees": {"type": "array", "items": {"type": "string"}},
        # This document's own stamp, in REF_FORMAT; "" when unrecorded.
        "recording": {"type": "string"},
        "prior_references": {"type": "array", "items": {"type": "string"}},
        "transfer_date": {"type": "string"},
        "transfer_date_verbatim": {"type": "string"},
        "county": {"type": "string"},
        "state": {"type": "string"},
        "lot_block": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "grantors",
        "grantees",
        "recording",
        "prior_references",
        "transfer_date",
        "transfer_date_verbatim",
        "county",
        "state",
        "lot_block",
    ],
    "additionalProperties": False,
}

DEED_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_kind": {"type": "string", "enum": ["deed", "plat", "as_built", "other"]},
        "legal_description_found": {"type": "boolean"},
        "basis_of_bearings": _NULLABLE_STRING,
        "tracts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "pob_description": {"type": "string"},
                    "calls": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "call_type": {"type": "string", "enum": ["line", "curve"]},
                                "verbatim_text": {"type": "string"},
                                "monument": _NULLABLE_STRING,
                                "confidence": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                },
                                "bearing": _NULLABLE_BEARING,
                                "distance": _NULLABLE_NUMBER,
                                "chord_bearing": _NULLABLE_BEARING,
                                "chord_length": _NULLABLE_NUMBER,
                                "radius": _NULLABLE_NUMBER,
                                "curve_direction": {
                                    "anyOf": [
                                        {"type": "string", "enum": ["left", "right"]},
                                        {"type": "null"},
                                    ]
                                },
                                "arc_length": _NULLABLE_NUMBER,
                                "adjoiner": {"type": "string"},
                            },
                            "required": [
                                "call_type",
                                "verbatim_text",
                                "monument",
                                "confidence",
                                "bearing",
                                "distance",
                                "chord_bearing",
                                "chord_length",
                                "radius",
                                "curve_direction",
                                "arc_length",
                                "adjoiner",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "stated_acreage": _NULLABLE_NUMBER,
                    "distance_unit": {
                        "type": "string",
                        "enum": ["feet", "varas", "chains", "meters"],
                    },
                    "is_exception": {"type": "boolean"},
                    "tie_calls": {"type": "array", "items": {"type": "string"}},
                    "description_type": {
                        "type": "string",
                        "enum": ["metes_and_bounds", "aliquot"],
                    },
                    # One ALIQUOT_FORMAT string; "" for metes-and-bounds tracts.
                    "aliquot": {"type": "string"},
                },
                "required": [
                    "name",
                    "pob_description",
                    "calls",
                    "stated_acreage",
                    "distance_unit",
                    "is_exception",
                    "tie_calls",
                    "description_type",
                    "aliquot",
                ],
                "additionalProperties": False,
            },
        },
        "notes": _NULLABLE_STRING,
        "metadata": _METADATA_SCHEMA,
    },
    "required": [
        "document_kind",
        "legal_description_found",
        "basis_of_bearings",
        "tracts",
        "notes",
        "metadata",
    ],
    "additionalProperties": False,
}
