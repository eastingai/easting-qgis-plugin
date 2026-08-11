"""Build QGIS memory layers from a placed traverse; save to GeoPackage.

The traverse arrives from the API in local feet from the POB, whatever unit the
document was written in — a metric or varas description is converted before the
vertices are computed. Placement translates them to the clicked map point,
applies user rotation, and scales feet into the target CRS's linear units via
QgsUnitTypes. The geometry math that produced those vertices is server-side;
this module only transforms them.
"""

from __future__ import annotations

import math

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QVariant

from ._vendor.groundtruth_core.model import Call, Tract
from ._vendor.groundtruth_core.served import ServerVerdict
from .arcs import arc_points

FEET_UNIT = QgsUnitTypes.DistanceUnit.DistanceFeet


# Field defs pair a name with a QVariant type code. Annotated as int because
# Qt6 removed QVariant's type-enum name while keeping the values.
def _easement_attrs(tract) -> list:
    """The five easement columns, in TRACT_FIELDS order. A tract with no
    easement burden writes blanks rather than being treated specially."""
    e = getattr(tract, "easement", None)
    if e is None:
        return ["", None, "", "", ""]
    exclusive = "" if e.exclusive is None else ("yes" if e.exclusive else "no")
    servient = e.servient_reference.text() if e.servient_reference is not None else ""
    return [e.easement_type, e.width_ft, e.term, exclusive, servient]


def _unit_of(tract) -> str:
    """The unit the document measured in; feet when a server predates the field."""
    return getattr(tract, "distance_unit", None) or "feet"


def _fields(defs: list[tuple[str, int]]) -> QgsFields:
    fields = QgsFields()
    for name, ftype in defs:
        fields.append(QgsField(name, ftype))
    return fields


TRACT_FIELDS = _fields(
    [
        ("tract", QVariant.String),
        ("verdict", QVariant.String),
        ("reasons", QVariant.String),
        ("closure_ratio", QVariant.String),
        ("computed_acres", QVariant.Double),
        ("stated_acres", QVariant.Double),
        # The document's own unit, and the computed area in metric. A parcel
        # recorded in meters is placed in feet like every other parcel, so the
        # attribute table is where that fact has to be readable.
        ("computed_sqm", QVariant.Double),
        ("distance_unit", QVariant.String),
        ("calls", QVariant.Int),
        ("is_exception", QVariant.Bool),
        ("source_doc", QVariant.String),
        ("pob_description", QVariant.String),
        # Stage 1 metadata, flattened into strings for the attribute table
        # and GeoPackage export. Empty on responses from pre-0.4 servers.
        ("easement_type", QVariant.String),
        ("easement_width_ft", QVariant.Double),
        ("easement_term", QVariant.String),
        ("easement_exclusive", QVariant.String),
        ("servient_ref", QVariant.String),
        ("grantors", QVariant.String),
        ("grantees", QVariant.String),
        ("recording_ref", QVariant.String),
        ("transfer_date", QVariant.String),
        ("county", QVariant.String),
        ("state", QVariant.String),
    ]
)

CALL_FIELDS = _fields(
    [
        ("tract", QVariant.String),
        ("call_no", QVariant.Int),
        ("call_type", QVariant.String),
        ("bearing", QVariant.String),
        # As recorded, in distance_unit — which is feet on a US deed and the
        # column names say so, but a metric course keeps its own number here
        # and the unit column is what stops it being read as feet.
        ("distance_ft", QVariant.Double),
        ("radius_ft", QVariant.Double),
        ("distance_unit", QVariant.String),
        ("confidence", QVariant.String),
        ("monument", QVariant.String),
        ("adjoiner", QVariant.String),
        ("verbatim_text", QVariant.String),
    ]
)


def place_group(
    pairs,
    anchor: QgsPointXY,
    crs: QgsCoordinateReferenceSystem,
    rotation_deg: float,
    source_doc: str,
    metadata=None,
):
    """Place every tract of a composite from one click on their shared
    commencement monument.

    `pairs` is [(tract, verdict), ...] where each verdict carries a
    tie_offset in feet relative to the monument. Each tract's POB lands at
    anchor + rotated offset, and the shared rotation turns the assembly as
    one; the per-tract layers are exactly what place_tract builds. Returns
    the list of (polygon, lines) layer pairs, skipping tracts the server
    could not traverse.
    """
    import math

    factor = QgsUnitTypes.fromUnitToUnitFactor(
        QgsUnitTypes.DistanceUnit.DistanceFeet, crs.mapUnits()
    )
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    placed = []
    for tract, verdict in pairs:
        off_x, off_y = verdict.tie_offset or (0.0, 0.0)
        xr = off_x * cos_t - off_y * sin_t
        yr = off_x * sin_t + off_y * cos_t
        pob = QgsPointXY(anchor.x() + xr * factor, anchor.y() + yr * factor)
        layers = place_tract(
            tract=tract,
            verdict=verdict,
            pob=pob,
            crs=crs,
            rotation_deg=rotation_deg,
            source_doc=source_doc,
            metadata=metadata,
        )
        if layers is not None:
            placed.append(layers)
    return placed


def _arc_between(call: Call, start: tuple[float, float], end: tuple[float, float]) -> list:
    """Intermediate arc points for a curve call, or nothing for a chord."""
    if (
        call.call_type != "curve"
        or not call.radius
        or call.curve_direction not in ("left", "right")
    ):
        return []
    return arc_points(start, end, float(call.radius), call.curve_direction)


def walked_vertices(calls: list[Call], vertices: list) -> list[tuple[float, float]]:
    """The boundary in local feet as the traverse actually walked it.

    Chord endpoints with every curve call swelled to its arc. A curve drawn
    as its bare chord misstates the boundary by the middle ordinate, 22.7
    feet on the customer-reported Garfield County highway curve, so both the
    placement preview and the placed layers draw from this."""
    walked: list[tuple[float, float]] = [tuple(vertices[0])]
    for i in range(len(vertices) - 1):
        start, end = tuple(vertices[i]), tuple(vertices[i + 1])
        between = _arc_between(calls[i], start, end) if i < len(calls) else []
        walked.extend([*between, end])
    return walked


def place_tract(
    tract: Tract,
    verdict: ServerVerdict,
    pob: QgsPointXY,
    crs: QgsCoordinateReferenceSystem,
    rotation_deg: float,
    source_doc: str,
    metadata=None,
) -> tuple[QgsVectorLayer, QgsVectorLayer] | None:
    """Create polygon + call-line memory layers for one tract at the POB.

    Returns None when the server could not traverse the tract, which is the
    same outcome the local computation used to produce on a bad call list.
    """
    geometry = verdict.geometry
    if geometry is None or len(geometry.vertices) < 2:
        return None

    to_crs_units = QgsUnitTypes.fromUnitToUnitFactor(FEET_UNIT, crs.mapUnits())
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    def to_map(x_ft: float, y_ft: float) -> QgsPointXY:
        xr = x_ft * cos_t - y_ft * sin_t
        yr = x_ft * sin_t + y_ft * cos_t
        return QgsPointXY(pob.x() + xr * to_crs_units, pob.y() + yr * to_crs_units)

    def course_local(i: int) -> list[tuple[float, float]]:
        """One course in local feet: the arc the document described, or its chord."""
        start = tuple(geometry.vertices[i])
        end = tuple(geometry.vertices[i + 1])
        between = _arc_between(tract.calls[i], start, end) if i < len(tract.calls) else []
        return [start, *between, end]

    pts = [to_map(x, y) for x, y in walked_vertices(tract.calls, geometry.vertices)]
    ring = pts if geometry.misclosure < 1e-9 else pts + [pts[0]]

    crs_id = crs.authid()
    poly_layer = QgsVectorLayer(f"Polygon?crs={crs_id}", f"{source_doc} — {tract.name}", "memory")
    poly_layer.dataProvider().addAttributes(TRACT_FIELDS.toList())
    poly_layer.updateFields()

    # closure_ratio is "exact" or "1:96,419"; keep the friendlier wording the
    # attribute table has always carried.
    closure = (
        "closes exactly" if verdict.closure_ratio == "exact" else (verdict.closure_ratio or "")
    )
    feat = QgsFeature(poly_layer.fields())
    feat.setGeometry(QgsGeometry.fromPolygonXY([ring]))
    feat.setAttributes(
        [
            tract.name,
            verdict.status,
            "; ".join(verdict.reasons),
            closure,
            round(geometry.acres, 4),
            tract.stated_acreage,
            getattr(verdict, "computed_sqm", None),
            _unit_of(tract),
            len(tract.calls),
            tract.is_exception,
            source_doc,
            tract.pob_description,
            *_easement_attrs(tract),
            ", ".join(metadata.grantors) if metadata else "",
            ", ".join(metadata.grantees) if metadata else "",
            metadata.recording.text() if metadata and metadata.recording else "",
            (metadata.transfer_date or "") if metadata else "",
            (metadata.county or "") if metadata else "",
            (metadata.state or "") if metadata else "",
        ]
    )
    poly_layer.dataProvider().addFeatures([feat])
    poly_layer.updateExtents()

    line_layer = QgsVectorLayer(
        f"LineString?crs={crs_id}", f"{source_doc} — {tract.name} calls", "memory"
    )
    line_layer.dataProvider().addAttributes(CALL_FIELDS.toList())
    line_layer.updateFields()

    line_feats = []
    for i, call in enumerate(tract.calls):
        if i + 1 >= len(geometry.vertices):
            break
        seg = QgsFeature(line_layer.fields())
        seg.setGeometry(QgsGeometry.fromPolylineXY([to_map(x, y) for x, y in course_local(i)]))
        bearing = call.effective_bearing()
        seg.setAttributes(
            [
                tract.name,
                i + 1,
                call.call_type,
                bearing.text() if bearing else None,
                call.effective_distance(),
                call.radius,
                _unit_of(tract),
                call.confidence,
                call.monument,
                getattr(call, "adjoiner", None) or "",
                call.verbatim_text,
            ]
        )
        line_feats.append(seg)
    line_layer.dataProvider().addFeatures(line_feats)
    line_layer.updateExtents()
    _label_courses(line_layer)

    return poly_layer, line_layer


def _label_courses(line_layer: QgsVectorLayer) -> None:
    """Number each course on the map the way the certificate figure does.

    Same key, same meaning: course N on the canvas is row N in the call
    table and course N on the certificate, so the three artifacts read as
    one. The attribute was always there; this turns the label on.
    """
    settings = QgsPalLayerSettings()
    settings.fieldName = "call_no"
    # Scoped enum, not the Qt5 shorthand: the plugin repository's Qt6
    # compatibility checker rejects `QgsPalLayerSettings.Line`.
    settings.placement = QgsPalLayerSettings.Placement.Line
    fmt = QgsTextFormat()
    fmt.setSize(9)
    settings.setFormat(fmt)
    line_layer.setLabelsEnabled(True)
    line_layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))


def save_geopackage(layers: list[QgsVectorLayer], path: str) -> str | None:
    """Write layers into one GeoPackage. Returns an error message or None."""
    ctx = QgsProject.instance().transformContext()
    for i, layer in enumerate(layers):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer.name().replace(" — ", " - ")
        if i > 0:
            options.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            )
        err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, ctx, options)
        if err != QgsVectorFileWriter.WriterError.NoError:
            return msg or f"write failed for {layer.name()}"
    return None


def suggest_projected_crs(pob_lon: float, pob_lat: float) -> QgsCoordinateReferenceSystem:
    """UTM zone of the click point, for when the project CRS is geographic."""
    zone = int((pob_lon + 180) / 6) + 1
    epsg = (32600 if pob_lat >= 0 else 32700) + zone
    return QgsCoordinateReferenceSystem(f"EPSG:{epsg}")


def area_check(layer: QgsVectorLayer) -> float:
    """Ellipsoidal acres of the first feature (sanity aid, not a validator)."""
    da = QgsDistanceArea()
    da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
    da.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
    feat = next(layer.getFeatures(), None)
    if feat is None:
        return 0.0
    return da.convertAreaMeasurement(
        da.measureArea(feat.geometry()), QgsUnitTypes.AreaUnit.AreaAcres
    )


def place_georeferenced_tract(
    tract: Tract,
    verdict: ServerVerdict,
    crs: QgsCoordinateReferenceSystem,
    source_doc: str,
    metadata=None,
) -> tuple[QgsVectorLayer, QgsVectorLayer] | None:
    """Aliquot placement: the PLSS fabric already locates the parcel, so the
    lon/lat ring transforms straight into the project CRS. No POB click, no
    rotation; the boundary-lines layer is omitted because an aliquot tract
    has no courses to attribute."""
    from qgis.core import QgsCoordinateTransform, QgsProject

    georef = getattr(verdict, "georef", None)
    if georef is None or not georef.rings:
        return None

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(wgs84, crs, QgsProject.instance())
    rings = []
    for ring in georef.rings:
        pts = [transform.transform(QgsPointXY(lon, lat)) for lon, lat in ring]
        rings.append(pts)

    crs_id = crs.authid()
    poly_layer = QgsVectorLayer(f"Polygon?crs={crs_id}", f"{source_doc} — {tract.name}", "memory")
    poly_layer.dataProvider().addAttributes(TRACT_FIELDS.toList())
    poly_layer.updateFields()

    feat = QgsFeature(poly_layer.fields())
    feat.setGeometry(QgsGeometry.fromPolygonXY(rings))
    aliquot = getattr(tract, "aliquot", None)
    feat.setAttributes(
        [
            tract.name,
            verdict.status,
            "; ".join(verdict.reasons),
            f"PLSS {georef.plss_id}" if georef.plss_id else "PLSS",
            georef.acres,
            tract.stated_acreage,
            getattr(verdict, "computed_sqm", None),
            _unit_of(tract),
            0,
            tract.is_exception,
            source_doc,
            aliquot.verbatim_text if aliquot else tract.pob_description,
            *_easement_attrs(tract),
            ", ".join(metadata.grantors) if metadata else "",
            ", ".join(metadata.grantees) if metadata else "",
            metadata.recording.text() if metadata and metadata.recording else "",
            (metadata.transfer_date or "") if metadata else "",
            (metadata.county or "") if metadata else "",
            (metadata.state or "") if metadata else "",
        ]
    )
    poly_layer.dataProvider().addFeatures([feat])
    poly_layer.updateExtents()

    # An empty call layer keeps the return shape identical to place_tract, so
    # save_geopackage and the plugin's layer bookkeeping need no second path.
    line_layer = QgsVectorLayer(
        f"LineString?crs={crs_id}", f"{source_doc} — {tract.name} calls", "memory"
    )
    line_layer.dataProvider().addAttributes(CALL_FIELDS.toList())
    line_layer.updateFields()
    return poly_layer, line_layer
