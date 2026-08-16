"""Map tool: click the point of beginning to place the active tract.

The same tool places a GROUP: a composite conveyance whose tracts all tie
to one commencement monument previews every ring at once, each translated
by its server-computed tie offset, and one click on the monument places
them all. Rotation turns the whole assembly around the click.
"""

from __future__ import annotations

import math

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QColor


class PlacePobTool(QgsMapToolEmitPoint):
    """One click places the POB; a rubber band previews the polygon.

    Feet-to-map-unit scaling and rotation are applied here for the preview;
    layers.place_tract repeats the same transform when the user confirms.

    The vertex list comes from the API —
    but every live interaction is an affine transform over that fixed list, so
    the preview stays as responsive as it ever was.
    """

    pob_picked = pyqtSignal(object)  # QgsPointXY in project CRS

    def __init__(self, canvas, vertices: list[tuple[float, float]], feet_factor: float):
        super().__init__(canvas)
        self._canvas = canvas
        # The server computed these; the tool only ever transforms them.
        # Internally everything is a group: a single tract is a group of one
        # with a zero offset.
        self._rings: list[tuple[tuple[float, float], list[tuple[float, float]]]] = [
            ((0.0, 0.0), vertices)
        ]
        self._feet_factor = feet_factor
        self._rotation_deg = 0.0
        self._pob: QgsPointXY | None = None
        self._band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self._band.setColor(QColor(29, 95, 191, 60))
        self._band.setStrokeColor(QColor(29, 95, 191))
        self._band.setWidth(2)

    # -- live preview under the cursor -------------------------------------
    @classmethod
    def for_group(
        cls,
        canvas,
        rings: list[tuple[tuple[float, float], list[tuple[float, float]]]],
        feet_factor: float,
    ) -> PlacePobTool:
        """A tool whose click point is the shared commencement monument.

        `rings` pairs each tract's tie offset (feet east/north of the
        monument) with its POB-at-origin vertex list.
        """
        tool = cls(canvas, rings[0][1], feet_factor)
        tool._rings = list(rings)
        return tool

    def canvasMoveEvent(self, event) -> None:
        if self._pob is None:
            self._draw_at(self.toMapCoordinates(event.pos()))

    def canvasReleaseEvent(self, event) -> None:
        self._pob = self.toMapCoordinates(event.pos())
        self._draw_at(self._pob)
        self.pob_picked.emit(self._pob)

    def preview_at(self, point: QgsPointXY) -> None:
        """Seed the preview at a suggested position, as if it had been clicked.

        The suggestion comes from the service, and everything after this is the
        ordinary flow: the band is on the canvas, the rotation slider works,
        and nothing reaches a layer until Confirm. That equivalence is the
        assist-never-auto-accept rule expressed where the operator meets it,
        rather than asserted in a docstring somewhere.
        """
        self._pob = point
        self._draw_at(point)
        self.pob_picked.emit(point)

    def set_rotation(self, degrees: float) -> None:
        self._rotation_deg = degrees
        if self._pob is not None:
            self._draw_at(self._pob)

    def pob(self) -> QgsPointXY | None:
        return self._pob

    def rotation(self) -> float:
        return self._rotation_deg

    def clear(self) -> None:
        self._band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self._pob = None

    def deactivate(self) -> None:
        self._band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        super().deactivate()

    def _draw_at(self, origin: QgsPointXY) -> None:
        theta = math.radians(self._rotation_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        polygons = []
        for (off_x, off_y), vertices in self._rings:
            pts = []
            for x_ft, y_ft in vertices:
                # Offset and ring rotate together: the assembly turns as one
                # around the clicked monument.
                tx, ty = x_ft + off_x, y_ft + off_y
                xr = tx * cos_t - ty * sin_t
                yr = tx * sin_t + ty * cos_t
                pts.append(
                    QgsPointXY(
                        origin.x() + xr * self._feet_factor,
                        origin.y() + yr * self._feet_factor,
                    )
                )
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            polygons.append([pts])
        self._band.setToGeometry(QgsGeometry.fromMultiPolygonXY(polygons), None)
