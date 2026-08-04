"""Easting plugin (Easting Deeds): extract deed -> review -> place -> layers."""

from __future__ import annotations

from pathlib import Path

from qgis.core import Qgis, QgsApplication, QgsProject, QgsUnitTypes
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox

from ._vendor.groundtruth_core.model import Tract
from ._vendor.groundtruth_core.result import ExtractionResult
from ._vendor.groundtruth_core.served import ServerVerdict
from .extract_task import ExtractTask
from .layers import place_georeferenced_tract, place_tract, save_geopackage
from .place_tool import PlacePobTool
from .review_dock import ReviewDock
from .settings_dialog import SettingsDialog, get_api_url, get_service_key


class EastingPlugin:
    def __init__(self, iface):
        self.iface = iface
        self._dock: ReviewDock | None = None
        self._task: ExtractTask | None = None
        self._tool: PlacePobTool | None = None
        self._result: ExtractionResult | None = None
        self._source_doc = ""
        self._active_tract: Tract | None = None
        self._active_verdict: ServerVerdict | None = None
        self._placed_layers: list = []
        self._actions: list[QAction] = []
        self._toolbar = None

    # -- QGIS lifecycle ------------------------------------------------------
    def initGui(self) -> None:  # noqa: N802
        # The Easting toolbar is a shared platform surface: sibling Easting
        # plugins locate it by OBJECT NAME (findChild matches that, not the
        # visible title) and add their own actions rather than creating a
        # second bar. "EastingToolBar" is therefore a contract; changing it
        # strands every plugin that looks it up.
        from qgis.PyQt.QtWidgets import QToolBar

        self._toolbar = self.iface.mainWindow().findChild(QToolBar, "EastingToolBar")
        if self._toolbar is None:
            self._toolbar = self.iface.addToolBar("Easting")
            self._toolbar.setObjectName("EastingToolBar")

        icon = QIcon(str(Path(__file__).parent / "icon.png"))
        extract = QAction(icon, "Extract deed…", self.iface.mainWindow())
        extract.triggered.connect(self.run_extract)
        self._toolbar.addAction(extract)
        self.iface.addPluginToMenu("Easting", extract)

        settings = QAction("Settings…", self.iface.mainWindow())
        settings.triggered.connect(self.show_settings)
        self._toolbar.addAction(settings)
        self.iface.addPluginToMenu("Easting", settings)

        self._actions = [extract, settings]

    def unload(self) -> None:
        for action in self._actions:
            if self._toolbar is not None:
                self._toolbar.removeAction(action)
            self.iface.removePluginMenu("Easting", action)
        # Take the toolbar down only when nothing else still lives on it: a
        # sibling plugin may have joined the shared surface after us.
        if self._toolbar is not None and not self._toolbar.actions():
            self._toolbar.deleteLater()
        self._toolbar = None
        if self._dock is not None:
            self.iface.removeDockWidget(self._dock)
            self._dock = None
        self._release_tool()

    # -- extraction flow -------------------------------------------------------
    def show_settings(self) -> None:
        SettingsDialog(self.iface.mainWindow()).exec()

    def run_extract(self) -> None:
        api_key, api_url = get_service_key(), get_api_url()
        if not api_key or not api_url:
            self.show_settings()
            api_key, api_url = get_service_key(), get_api_url()
            if not api_key or not api_url:
                return

        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(), "Select deed PDF", "", "PDF documents (*.pdf)"
        )
        if not path:
            return

        self._source_doc = path.rsplit("/", 1)[-1]
        self._task = ExtractTask(path, api_url=api_url, api_key=api_key)
        self._task.succeeded.connect(self._on_extracted)
        self._task.failed.connect(self._on_failed)
        QgsApplication.taskManager().addTask(self._task)
        self.iface.messageBar().pushMessage(
            "Easting", f"Extracting {self._source_doc}…", level=Qgis.MessageLevel.Info
        )

    def _on_extracted(self, result: ExtractionResult) -> None:
        self._result = result
        if self._dock is None:
            self._dock = ReviewDock(self.iface.mainWindow())
            self._dock.place_requested.connect(self._start_placement)
            self._dock.place_georef_requested.connect(self._place_georef)
            self._dock.rotation_changed.connect(self._on_rotation)
            self._dock.place_confirmed.connect(self._confirm_placement)
            self._dock.save_requested.connect(self._save_gpkg)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._dock)
        self._dock.show_result(result, self._source_doc)
        self._dock.show()
        self._dock.raise_()

    def _place_georef(self, tract: Tract, verdict: ServerVerdict) -> None:
        """Aliquot tracts arrive located: no POB click, no rotation."""
        layers = place_georeferenced_tract(
            tract=tract,
            verdict=verdict,
            crs=QgsProject.instance().crs(),
            source_doc=self._source_doc,
            metadata=getattr(self._result.extraction, "metadata", None) if self._result else None,
        )
        if layers is None:
            QMessageBox.warning(
                self.iface.mainWindow(), "Easting", "No georeferenced geometry on this tract."
            )
            return
        poly, lines = layers
        QgsProject.instance().addMapLayers([poly, lines])
        self._placed_layers.extend([poly, lines])
        self.iface.messageBar().pushMessage(
            "Easting",
            f"Placed {tract.name} at its PLSS location.",
            level=Qgis.MessageLevel.Success,
        )
        canvas = self.iface.mapCanvas()
        canvas.setExtent(poly.extent())
        canvas.refresh()

    def _on_failed(self, message: str) -> None:
        QMessageBox.warning(self.iface.mainWindow(), "Easting", message)

    # -- placement flow --------------------------------------------------------
    def _start_placement(self, tract: Tract, verdict: ServerVerdict) -> None:
        # The server decides traversability; a verdict without geometry is that
        # decision, and its reasons are what the user needs to see.
        if not verdict.placeable:
            reasons = "; ".join(verdict.reasons) or "no geometry was returned for it"
            QMessageBox.warning(
                self.iface.mainWindow(), "Easting", f"This tract is not traversable: {reasons}"
            )
            return

        crs = QgsProject.instance().crs()
        if crs.isGeographic():
            QMessageBox.information(
                self.iface.mainWindow(),
                "Easting",
                "The project CRS is geographic (degrees). Switch the project to a "
                "projected CRS (for example the UTM zone or State Plane of the parcel) "
                "before placing, so distances in feet stay meaningful.",
            )
            return

        self._active_tract = tract
        self._active_verdict = verdict
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            QgsUnitTypes.DistanceUnit.DistanceFeet, crs.mapUnits()
        )
        self._release_tool()
        self._tool = PlacePobTool(self.iface.mapCanvas(), verdict.geometry.vertices, factor)
        self._tool.pob_picked.connect(
            lambda _: self.iface.messageBar().pushMessage(
                "Easting",
                "POB set. Adjust rotation in the dock, then Confirm placement.",
                level=Qgis.MessageLevel.Info,
            )
        )
        self.iface.mapCanvas().setMapTool(self._tool)
        self.iface.messageBar().pushMessage(
            "Easting",
            f"Click the point of beginning for {tract.name}.",
            level=Qgis.MessageLevel.Info,
        )

    def _on_rotation(self, degrees: float) -> None:
        if self._tool is not None:
            self._tool.set_rotation(degrees)

    def _confirm_placement(self) -> None:
        if self._tool is None or self._tool.pob() is None or self._active_tract is None:
            QMessageBox.information(
                self.iface.mainWindow(), "Easting", "Click the POB on the map first."
            )
            return
        layers = place_tract(
            tract=self._active_tract,
            verdict=self._active_verdict,
            pob=self._tool.pob(),
            crs=QgsProject.instance().crs(),
            rotation_deg=self._tool.rotation(),
            source_doc=self._source_doc,
            metadata=getattr(self._result.extraction, "metadata", None) if self._result else None,
        )
        if layers is None:
            QMessageBox.warning(
                self.iface.mainWindow(), "Easting", "Could not build geometry for this tract."
            )
            return
        poly, lines = layers
        QgsProject.instance().addMapLayers([poly, lines])
        self._placed_layers.extend([poly, lines])
        self._release_tool()
        self.iface.messageBar().pushMessage(
            "Easting", f"Placed {self._active_tract.name}.", level=Qgis.MessageLevel.Success
        )

    def _save_gpkg(self) -> None:
        if not self._placed_layers:
            QMessageBox.information(
                self.iface.mainWindow(), "Easting", "Place a tract on the map first."
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Save GeoPackage", "", "GeoPackage (*.gpkg)"
        )
        if not path:
            return
        if not path.endswith(".gpkg"):
            path += ".gpkg"
        error = save_geopackage(self._placed_layers, path)
        if error:
            QMessageBox.warning(self.iface.mainWindow(), "Easting", error)
        else:
            self.iface.messageBar().pushMessage(
                "Easting", f"Saved {path}", level=Qgis.MessageLevel.Success
            )

    def _release_tool(self) -> None:
        if self._tool is not None:
            self.iface.mapCanvas().unsetMapTool(self._tool)
            self._tool.clear()
            self._tool = None
