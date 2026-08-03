"""Build QGIS memory layers from a placed traverse; save to GeoPackage.

The traverse arrives from the API in local feet from the POB. Placement
translates it to the clicked map point, applies user rotation, and scales feet
into the target CRS's linear units via QgsUnitTypes. The geometry math that
produced those vertices is server-side; this module only transforms them.
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
    QgsPointXY,
    QgsProject,
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

from ._vendor.groundtruth_core.model import Tract
from ._vendor.groundtruth_core.served import ServerVerdict

FEET_UNIT = QgsUnitTypes.DistanceFeet


def _fields(defs: list[tuple[str, QVariant.Type]]) -> QgsFields:
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
        ("calls", QVariant.Int),
        ("is_exception", QVariant.Bool),
        ("source_doc", QVariant.String),
        ("pob_description", QVariant.String),
        # Stage 1 metadata, flattened into strings for the attribute table
        # and GeoPackage export. Empty on responses from pre-0.4 servers.
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
        ("distance_ft", QVariant.Double),
        ("radius_ft", QVariant.Double),
        ("confidence", QVariant.String),
        ("monument", QVariant.String),
        ("adjoiner", QVariant.String),
        ("verbatim_text", QVariant.String),
    ]
)


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

    pts = [to_map(x, y) for x, y in geometry.vertices]
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
            len(tract.calls),
            tract.is_exception,
            source_doc,
            tract.pob_description,
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
        seg = QgsFeature(line_layer.fields())
        seg.setGeometry(QgsGeometry.fromPolylineXY([pts[i], pts[i + 1]]))
        bearing = call.effective_bearing()
        seg.setAttributes(
            [
                tract.name,
                i + 1,
                call.call_type,
                bearing.text() if bearing else None,
                call.effective_distance(),
                call.radius,
                call.confidence,
                call.monument,
                getattr(call, "adjoiner", None) or "",
                call.verbatim_text,
            ]
        )
        line_feats.append(seg)
    line_layer.dataProvider().addFeatures(line_feats)
    line_layer.updateExtents()

    return poly_layer, line_layer


def save_geopackage(layers: list[QgsVectorLayer], path: str) -> str | None:
    """Write layers into one GeoPackage. Returns an error message or None."""
    ctx = QgsProject.instance().transformContext()
    for i, layer in enumerate(layers):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer.name().replace(" — ", " - ")
        if i > 0:
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, ctx, options)
        if err != QgsVectorFileWriter.NoError:
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
    return da.convertAreaMeasurement(da.measureArea(feat.geometry()), QgsUnitTypes.AreaAcres)


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
            0,
            tract.is_exception,
            source_doc,
            aliquot.verbatim_text if aliquot else tract.pob_description,
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
