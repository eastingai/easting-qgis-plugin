"""Easting plugin (Easting Deeds): extract deed -> review -> place -> layers."""

from __future__ import annotations

from pathlib import Path

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsUnitTypes,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox

from ._vendor.groundtruth_core.hosted import (
    fetch_certificate,
    fetch_corrections,
    fetch_dxf,
    fetch_geopackage,
    fetch_location,
    result_from_payload,
)
from ._vendor.groundtruth_core.model import Tract
from ._vendor.groundtruth_core.result import ExtractionError, ExtractionResult
from ._vendor.groundtruth_core.served import ServerVerdict
from .batch_task import BatchTask
from .extract_task import ExtractTask
from .layers import (
    place_georeferenced_tract,
    place_group,
    place_tract,
    save_geopackage,
    walked_vertices,
)
from .place_tool import PlacePobTool
from .review_dock import ReviewDock
from .settings_dialog import SettingsDialog, get_api_url, get_service_key

# Recorder offices hand out more than PDF: TIFF is the archival native at
# register-of-deeds offices, and a photographed document is a JPEG. The service
# converts them on arrival, so the only thing the plugin needs is to stop
# filtering them out of the picker.
DOCUMENT_SUFFIXES = frozenset({".pdf", ".tif", ".tiff", ".png", ".jpg", ".jpeg"})
DOCUMENT_FILTER = (
    "Recorded documents (*.pdf *.tif *.tiff *.png *.jpg *.jpeg);;"
    "PDF documents (*.pdf);;"
    "Scanned images (*.tif *.tiff *.png *.jpg *.jpeg)"
)


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
        self._active_group: list | None = None  # [(Tract, ServerVerdict), ...]
        self._placed_layers: list = []
        self._actions: list[QAction] = []
        self._batch_task = None
        # Documents from a settled batch waiting their turn in the dock.
        self._batch_queue: list = []
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

        folder = QAction(icon, "Extract folder…", self.iface.mainWindow())
        folder.setToolTip(
            "Extract every PDF in a folder as one batch. Results arrive "
            "together, usually within the hour."
        )
        folder.triggered.connect(self.run_batch)
        self._toolbar.addAction(folder)
        self.iface.addPluginToMenu("Easting", folder)

        settings = QAction("Settings…", self.iface.mainWindow())
        settings.triggered.connect(self.show_settings)
        self._toolbar.addAction(settings)
        self.iface.addPluginToMenu("Easting", settings)

        # Every action this plugin owns, so unload takes all of them off a
        # toolbar that siblings also live on. Forgetting one leaves a dead
        # button behind after the plugin is disabled.
        self._actions = [extract, folder, settings]

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
            self.iface.mainWindow(),
            "Select a recorded document",
            "",
            DOCUMENT_FILTER,
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
            self._dock.group_place_requested.connect(self._start_group_placement)
            self._dock.place_georef_requested.connect(self._place_georef)
            self._dock.rotation_changed.connect(self._on_rotation)
            self._dock.place_confirmed.connect(self._confirm_placement)
            self._dock.save_requested.connect(self._save_gpkg)
            self._dock.dxf_requested.connect(self._save_dxf)
            self._dock.certificate_requested.connect(self._save_certificate)
            self._dock.locate_requested.connect(self._suggest_locations)
            self._dock.correction_requested.connect(self._apply_corrections)
            self._dock.close_requested.connect(self._close_shape)
            self._dock.correction_refused.connect(self._on_correction_refused)
            self._dock.place_suggested_requested.connect(self._place_at_suggestion)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._dock)
        self._dock.show_result(result, self._source_doc)
        self._dock.show()
        self._dock.raise_()

    def run_batch(self) -> None:
        """Extract every PDF in a folder as one batch.

        Batch trades latency for cost and for scale: the whole folder goes up
        in one request and the results come back together. The server admits
        the batch whole or refuses it whole, so a 402 here means nothing was
        submitted and nothing was billed.
        """
        api_key, api_url = get_service_key(), get_api_url()
        if not api_key or not api_url:
            self.show_settings()
            api_key, api_url = get_service_key(), get_api_url()
            if not api_key or not api_url:
                return

        directory = QFileDialog.getExistingDirectory(
            self.iface.mainWindow(), "Select a folder of deed PDFs"
        )
        if not directory:
            return

        found = sorted(
            p for p in Path(directory).iterdir() if p.suffix.lower() in DOCUMENT_SUFFIXES
        )
        if not found:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Easting",
                "No documents in that folder. Easting reads PDF, TIFF, PNG, and JPEG.",
            )
            return

        documents = [(path.name, path.read_bytes()) for path in found]
        confirm = QMessageBox.question(
            self.iface.mainWindow(),
            "Easting",
            f"Extract {len(documents)} documents as one batch?\n\n"
            "They count against your quota when they succeed. Failed "
            "documents are never billed.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._batch_task = BatchTask(documents, api_url=api_url, api_key=api_key)
        self._batch_task.submitted.connect(self._on_batch_submitted)
        self._batch_task.progress_changed.connect(self._on_batch_progress)
        self._batch_task.completed.connect(self._on_batch_completed)
        self._batch_task.failed.connect(self._on_failed)
        QgsApplication.taskManager().addTask(self._batch_task)

    def _on_batch_submitted(self, batch_id: str, count: int) -> None:
        self.iface.messageBar().pushMessage(
            "Easting",
            f"Batch {batch_id} submitted: {count} documents. Results usually "
            "arrive within the hour; you can keep working.",
            level=Qgis.MessageLevel.Info,
        )

    def _on_batch_progress(self, settled: int, total: int) -> None:
        if self._dock is not None:
            self._dock.show_batch_progress(settled, total)

    def _on_batch_completed(self, results: list) -> None:
        """Load the batch into the ordinary review flow, one document at a time.

        Reviewing is a per-document act, so a settled batch does not get its
        own surface: the first result opens in the dock exactly as a single
        extraction would, and the rest queue behind it.
        """
        succeeded = [(name, result) for name, result, error in results if result is not None]
        failures = [(name, error) for name, result, error in results if result is None]
        self._batch_queue = succeeded[1:]
        if self._dock is not None:
            self._dock.clear_batch_progress()

        if failures:
            listed = "\n".join(f"· {name}: {error}" for name, error in failures[:8])
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Easting",
                f"{len(failures)} of {len(results)} documents did not extract "
                f"(these were not billed):\n\n{listed}",
            )
        if not succeeded:
            return

        name, result = succeeded[0]
        self._source_doc = name
        self._on_extracted(result)
        if self._batch_queue:
            self.iface.messageBar().pushMessage(
                "Easting",
                f"{len(succeeded)} documents extracted. Showing {name}; "
                f"{len(self._batch_queue)} more are ready in this batch.",
                level=Qgis.MessageLevel.Success,
            )

    def _save_dxf(self) -> None:
        """Convert the extraction to a CAD drawing, server-side.

        The retained response payload goes back up rather than a
        re-serialization of the parsed model: the server reads keys this
        plugin version may not know about.
        """
        if self._result is None:
            return
        payload = getattr(self._result, "raw", None)
        if not payload:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Easting",
                "This extraction predates DXF export. Re-run it to save a drawing.",
            )
            return

        default = (self._source_doc.rsplit(".", 1)[0] or "extraction") + ".dxf"
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Save DXF", default, "DXF drawings (*.dxf)"
        )
        if not path:
            return
        try:
            drawing = fetch_dxf(get_api_url(), get_service_key(), payload)
        except ExtractionError as exc:
            QMessageBox.warning(self.iface.mainWindow(), "Easting", str(exc))
            return
        Path(path).write_bytes(drawing)
        self.iface.messageBar().pushMessage(
            "Easting",
            f"Saved {Path(path).name}. The drawing is unplaced: point of "
            "beginning at the origin, distances in feet.",
            level=Qgis.MessageLevel.Success,
        )

    def _save_certificate(self) -> None:
        """Ask the service for the certificate PDF this extraction supports.

        The retained payload goes back up untouched. The service verifies a
        signature over the response exactly as it returned it, so a rebuild
        from the parsed model would come back refused as modified.
        """
        if self._result is None:
            return
        payload = getattr(self._result, "raw", None)
        if not payload:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Easting",
                "This extraction predates the certificate. Re-run it to save one.",
            )
            return

        default = (self._source_doc.rsplit(".", 1)[0] or "extraction") + "-certificate.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Save certificate", default, "PDF documents (*.pdf)"
        )
        if not path:
            return
        try:
            document = fetch_certificate(get_api_url(), get_service_key(), payload)
        except ExtractionError as exc:
            QMessageBox.warning(self.iface.mainWindow(), "Easting", str(exc))
            return
        Path(path).write_bytes(document)
        self.iface.messageBar().pushMessage(
            "Easting",
            f"Saved {Path(path).name}. Every course carries its source text, "
            "and the footer's verification code ties the paper to the extraction.",
            level=Qgis.MessageLevel.Success,
        )

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
    def _suggest_locations(self) -> None:
        """Ask the service where these tracts sit, and redraw the dock.

        The whole re-signed extraction replaces the one held, because the
        attestation covers the body as a unit: keeping the old payload and
        splicing the suggestions in would leave something the certificate
        endpoint refuses.
        """
        if self._result is None:
            return
        payload = getattr(self._result, "raw", None)
        if not payload:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Easting",
                "This extraction predates location suggestions. Re-run it to ask.",
            )
            return
        try:
            answer = fetch_location(get_api_url(), get_service_key(), payload)
        except ExtractionError as exc:
            QMessageBox.warning(self.iface.mainWindow(), "Easting", str(exc))
            return

        located = int(answer.get("located") or 0)
        body = answer.get("extraction") or {}
        try:
            self._result = result_from_payload(body)
        except ExtractionError as exc:
            QMessageBox.warning(self.iface.mainWindow(), "Easting", str(exc))
            return
        if self._dock is not None:
            self._dock.show_result(self._result, self._source_doc)
        self.iface.messageBar().pushMessage(
            "Easting",
            (
                f"Suggested a location for {located} tract(s). Nothing is placed until you confirm."
                if located
                else "No tract here commences at a PLSS corner the fabric resolves. "
                "The dock says which corners were tried."
            ),
            level=Qgis.MessageLevel.Info if located else Qgis.MessageLevel.Warning,
        )

    # -- corrections -----------------------------------------------------------
    def _apply_corrections(self, corrections: list) -> None:
        """Send a correction and hold what comes back.

        A correction says the document reads something else and we misread it,
        so the service recomputes the verdicts over it and re-signs. Nothing is
        edited here: a payload changed in the client is one the certificate
        refuses, which is exactly why this is a round trip.
        """
        self._correct(corrections=corrections)

    def _on_correction_refused(self, message: str) -> None:
        """A typed value the dock could not read. Said out loud, in the bar,
        rather than swallowed — the cell has already gone back to what it
        said, and an edit that silently did nothing is worse than a refusal."""
        self.iface.messageBar().pushMessage("Easting", message, level=Qgis.MessageLevel.Warning)

    def _close_shape(self, tract_index: int) -> None:
        """Fit one tract's courses so the shape closes.

        **The verdict does not move.** It judges the recorded description,
        which still miscloses, and the certificate prints how much was
        distributed. An adjustment the operator did not ask for, or one nobody
        downstream can see, is the silent auto-close this product refuses.
        """
        self._correct(adjust=[tract_index])

    def _correct(self, corrections: list | None = None, adjust: list | None = None) -> None:
        """The shared half: post, replace what is held, redraw.

        Placement is cleared because the geometry underneath it may have
        changed. A tract left drawn at its pre-correction shape would be the
        stalest thing on the canvas and the likeliest thing to be exported.
        """
        if self._result is None:
            return
        payload = getattr(self._result, "raw", None)
        if not payload:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Easting",
                "This extraction predates corrections. Re-run it to edit values.",
            )
            return
        try:
            answer = fetch_corrections(
                get_api_url(),
                get_service_key(),
                payload,
                corrections=corrections,
                adjust=adjust,
            )
            self._result = result_from_payload(answer.get("extraction") or {})
        except ExtractionError as exc:
            QMessageBox.warning(self.iface.mainWindow(), "Easting", str(exc))
            if self._dock is not None:
                self._dock.show_result(self._result, self._source_doc)
            return

        if self._dock is not None:
            self._dock.show_result(self._result, self._source_doc)
        corrected = int(answer.get("corrected") or 0)
        adjusted = int(answer.get("adjusted") or 0)
        self.iface.messageBar().pushMessage(
            "Easting",
            (
                f"Adjusted {adjusted} tract(s). The verdict is unchanged: the "
                "recorded courses still do not close, and the certificate says so."
                if adjusted
                else f"Corrected and re-verified {corrected} tract(s). "
                "The change is printed on the certificate."
            ),
            level=Qgis.MessageLevel.Info,
        )

    def _place_at_suggestion(self, tract: Tract, verdict: ServerVerdict) -> None:
        """Start the ordinary placement tool, pre-seeded at the suggestion.

        Deliberately the same tool and the same Confirm. The operator watches
        the band land where the service thinks it goes, adjusts rotation, and
        commits it themselves; a suggestion that placed itself would be the
        auto-accept this product refuses.
        """
        location = getattr(verdict, "location", None)
        if location is None or not location.placeable:
            return
        self._start_placement(tract, verdict)
        if self._tool is None:
            return  # _start_placement refused, and has already said why
        project_crs = QgsProject.instance().crs()
        transform = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem("EPSG:4326"), project_crs, QgsProject.instance()
        )
        lon, lat = location.pob
        try:
            point = transform.transform(QgsPointXY(lon, lat))
        except Exception:  # noqa: BLE001 — an untransformable CRS is a message, not a crash
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Easting",
                "The suggested location cannot be transformed into this project's CRS.",
            )
            return
        self._tool.preview_at(point)
        self.iface.mapCanvas().setCenter(point)
        self.iface.mapCanvas().refresh()
        self.iface.messageBar().pushMessage(
            "Easting",
            f"{tract.name} previewed at the suggested point of beginning. "
            "Adjust rotation if the basis of bearings is not grid north, then Confirm.",
            level=Qgis.MessageLevel.Info,
        )

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
        self._active_group = None
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            QgsUnitTypes.DistanceUnit.DistanceFeet, crs.mapUnits()
        )
        self._release_tool()
        # The preview shows the arcs the placed layers will draw, not chords.
        self._tool = PlacePobTool(
            self.iface.mapCanvas(),
            walked_vertices(tract.calls, verdict.geometry.vertices),
            factor,
        )
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

    def _start_group_placement(self, pairs: list) -> None:
        """One click on the shared commencement monument places every tract."""
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

        self._active_tract = None
        self._active_verdict = None
        self._active_group = pairs
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            QgsUnitTypes.DistanceUnit.DistanceFeet, crs.mapUnits()
        )
        rings = [(v.tie_offset, walked_vertices(t.calls, v.geometry.vertices)) for t, v in pairs]
        self._release_tool()
        self._tool = PlacePobTool.for_group(self.iface.mapCanvas(), rings, factor)
        self._tool.pob_picked.connect(
            lambda _: self.iface.messageBar().pushMessage(
                "Easting",
                "Commencement point set. Adjust rotation in the dock, then Confirm placement.",
                level=Qgis.MessageLevel.Info,
            )
        )
        self.iface.mapCanvas().setMapTool(self._tool)
        self.iface.messageBar().pushMessage(
            "Easting",
            f"Click the tracts' shared commencement point to preview all {len(pairs)}.",
            level=Qgis.MessageLevel.Info,
        )

    def _confirm_placement(self) -> None:
        if self._tool is not None and self._tool.pob() is not None and self._active_group:
            placed = place_group(
                pairs=self._active_group,
                anchor=self._tool.pob(),
                crs=QgsProject.instance().crs(),
                rotation_deg=self._tool.rotation(),
                source_doc=self._source_doc,
                metadata=(
                    getattr(self._result.extraction, "metadata", None) if self._result else None
                ),
            )
            count = 0
            for poly, lines in placed:
                QgsProject.instance().addMapLayers([poly, lines])
                self._placed_layers.extend([poly, lines])
                count += 1
            self._active_group = None
            self._release_tool()
            self.iface.messageBar().pushMessage(
                "Easting",
                f"Placed {count} tracts from their shared commencement point.",
                level=Qgis.MessageLevel.Success,
            )
            return

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
        """Ask the service for the GeoPackage this extraction supports.

        Server-written, like the DXF and the certificate, so a GeoPackage
        saved here and one saved from the web portal are the same file:
        tract and call layers on the verdict's own local plane, plus a
        placed WGS 84 layer for georeferenced tracts. Placed layers on the
        canvas export through QGIS itself (right-click, Export).

        Extractions kept from before this endpoint fall back to writing the
        placed layers locally, so the button never stops working mid-session.
        """
        if self._result is None:
            return
        payload = getattr(self._result, "raw", None)
        if not payload:
            self._save_gpkg_local()
            return

        default = (self._source_doc.rsplit(".", 1)[0] or "extraction") + ".gpkg"
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Save GeoPackage", default, "GeoPackage (*.gpkg)"
        )
        if not path:
            return
        if not path.endswith(".gpkg"):
            path += ".gpkg"
        try:
            package = fetch_geopackage(get_api_url(), get_service_key(), payload)
        except ExtractionError as exc:
            QMessageBox.warning(self.iface.mainWindow(), "Easting", str(exc))
            return
        Path(path).write_bytes(package)
        self.iface.messageBar().pushMessage(
            "Easting",
            f"Saved {Path(path).name}. Local-plane layers are unplaced; "
            "georeferenced tracts carry a placed WGS 84 layer.",
            level=Qgis.MessageLevel.Success,
        )

    def _save_gpkg_local(self) -> None:
        """The pre-endpoint path: write the placed canvas layers directly."""
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
