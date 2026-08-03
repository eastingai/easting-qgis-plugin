"""Map tool: click the point of beginning to place the active tract."""

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

    The vertex list comes from the API — the plugin carries no traverse math —
    but every live interaction is an affine transform over that fixed list, so
    the preview stays as responsive as it ever was.
    """

    pob_picked = pyqtSignal(object)  # QgsPointXY in project CRS

    def __init__(self, canvas, vertices: list[tuple[float, float]], feet_factor: float):
        super().__init__(canvas)
        self._canvas = canvas
        # The server computed these; the tool only ever transforms them.
        self._vertices = vertices
        self._feet_factor = feet_factor
        self._rotation_deg = 0.0
        self._pob: QgsPointXY | None = None
        self._band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._band.setColor(QColor(29, 95, 191, 60))
        self._band.setStrokeColor(QColor(29, 95, 191))
        self._band.setWidth(2)

    # -- live preview under the cursor -------------------------------------
    def canvasMoveEvent(self, event) -> None:
        if self._pob is None:
            self._draw_at(self.toMapCoordinates(event.pos()))

    def canvasReleaseEvent(self, event) -> None:
        self._pob = self.toMapCoordinates(event.pos())
        self._draw_at(self._pob)
        self.pob_picked.emit(self._pob)

    def set_rotation(self, degrees: float) -> None:
        self._rotation_deg = degrees
        if self._pob is not None:
            self._draw_at(self._pob)

    def pob(self) -> QgsPointXY | None:
        return self._pob

    def rotation(self) -> float:
        return self._rotation_deg

    def clear(self) -> None:
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        self._pob = None

    def deactivate(self) -> None:
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()

    def _draw_at(self, origin: QgsPointXY) -> None:
        theta = math.radians(self._rotation_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        pts = []
        for x_ft, y_ft in self._vertices:
            xr = x_ft * cos_t - y_ft * sin_t
            yr = x_ft * sin_t + y_ft * cos_t
            pts.append(
                QgsPointXY(origin.x() + xr * self._feet_factor, origin.y() + yr * self._feet_factor)
            )
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        self._band.setToGeometry(QgsGeometry.fromPolygonXY([pts]), None)
