"""Review dock: verdicts, call tables, and placement controls per tract.

Verdicts are rendered, never computed. They arrive on the ExtractionResult from
the API, index-aligned with the tracts, and the rules behind them are
server-side.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDockWidget,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ._vendor.groundtruth_core.model import Tract
from ._vendor.groundtruth_core.result import ExtractionResult
from ._vendor.groundtruth_core.served import ServerVerdict
from .theme import (
    VERDICT_BADGE_BG,
    confidence_colors,
    notes_style,
    secondary_text,
    verdict_text_color,
)

__all__ = ["ReviewDock"]


class ReviewDock(QDockWidget):
    place_requested = pyqtSignal(object, object)  # (Tract, Verdict)
    # [(Tract, Verdict), ...] whose verdicts share one commencement anchor.
    group_place_requested = pyqtSignal(list)
    place_georef_requested = pyqtSignal(object, object)  # (Tract, Verdict) — no POB click
    save_requested = pyqtSignal()
    dxf_requested = pyqtSignal()
    certificate_requested = pyqtSignal()
    rotation_changed = pyqtSignal(float)
    place_confirmed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Easting review", parent)
        self.setObjectName("EastingReviewDock")
        self._result: ExtractionResult | None = None
        self._source_doc = ""
        self._batch_card = None

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)
        self.setWidget(scroll)

    # -- population --------------------------------------------------------
    def show_result(self, result: ExtractionResult, source_doc: str) -> None:
        self._result = result
        self._source_doc = source_doc
        self._clear()

        ex = result.extraction
        # Model and token counts are internals of the service, not the user's
        # spend, so the header stays at what a reviewer can act on.
        header = QLabel(f"<b>{source_doc}</b> · {result.seconds:.0f}s")
        header.setWordWrap(True)
        self._layout.addWidget(header)

        provenance = QLabel(
            "Verdicts below come from GroundTruth, the verification layer: the "
            "traverse is recomputed, closure and stated acreage are checked, and "
            "every call keeps its verbatim source text."
        )
        provenance.setWordWrap(True)
        provenance.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
        self._layout.addWidget(provenance)

        self._add_metadata(ex)

        if ex.notes:
            notes = QLabel(f"<i>Extractor notes:</i> {ex.notes}")
            notes.setWordWrap(True)
            notes.setStyleSheet(notes_style())
            self._layout.addWidget(notes)

        # Plats and as-builts are identified rather than traced, so their
        # judgement arrives as one document-level verdict instead of a per-tract
        # array. Render it before the generic no-description message, which
        # would otherwise be the only thing these documents ever showed.
        document_verdict = getattr(result, "document_verdict", None)
        if document_verdict is not None:
            self._layout.addWidget(_document_banner(document_verdict))
        if ex.plat is not None:
            self._layout.addWidget(_plat_card(ex.plat))
        if ex.as_built is not None:
            self._layout.addWidget(_asbuilt_card(ex.as_built))

        if not ex.legal_description_found or not ex.tracts:
            if document_verdict is None:
                msg = QLabel(
                    "No locatable description found in this document. Deeds and "
                    "easements describe land by courses or by PLSS chains; "
                    "lot-and-block conveyances and easement areas that live only "
                    "on an attached drawing are recorded in the metadata above "
                    "rather than drawn."
                )
                msg.setWordWrap(True)
                msg.setStyleSheet(f"color:{verdict_text_color('FAIL')}; font-weight:bold;")
                self._layout.addWidget(msg)
            if document_verdict is not None:
                # A plat or an as-built has no tracts and never reaches the
                # controls row below, but its identification is exactly what
                # the certificate exists to put on paper.
                self._layout.addWidget(self._certificate_button())
            return

        # A composite conveyance ties every tract to one commencement
        # monument; when the server says several tracts share an anchor, one
        # click can place them all in their documented relative positions.
        groups: dict[str, list] = {}
        for tract, verdict in zip(ex.tracts, result.verdicts, strict=True):
            if verdict.groupable:
                groups.setdefault(verdict.anchor, []).append((tract, verdict))
        for anchor, pairs in groups.items():
            if len(pairs) < 2:
                continue
            # Anchors are the document's own monument text and can run long;
            # elide on the button, keep the full text where a hover finds it.
            shown = anchor if len(anchor) <= 44 else anchor[:43].rstrip() + "…"
            button = QPushButton(f"Place all {len(pairs)} tracts as a group — click: {shown}")
            button.setToolTip(f"Shared commencement point: {anchor}")
            button.clicked.connect(lambda _, p=pairs: self.group_place_requested.emit(p))
            self._layout.addWidget(button)

        # Index-aligned by contract, and hosted.py rejects any response where
        # they are not — so strict=True can only fire on a real bug, and a loud
        # failure beats a dock that quietly renders half the tracts.
        for tract, verdict in zip(ex.tracts, result.verdicts, strict=True):
            self._add_tract(tract, verdict)

        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 8, 0, 0)
        row.addWidget(QLabel("Rotation °"))
        self._rotation = QDoubleSpinBox()
        self._rotation.setRange(-45.0, 45.0)
        self._rotation.setDecimals(2)
        self._rotation.setSingleStep(0.25)
        self._rotation.valueChanged.connect(self.rotation_changed.emit)
        row.addWidget(self._rotation)
        confirm = QPushButton("Confirm placement")
        confirm.clicked.connect(self.place_confirmed.emit)
        row.addWidget(confirm)
        # The server answers 422 when no tract has geometry, so disable rather
        # than let someone click into a refusal they could have been spared.
        placeable = any(v.placeable for v in result.verdicts)
        save = QPushButton("Save GeoPackage…")
        save.clicked.connect(self.save_requested.emit)
        # GeoPackage draws one thing more than the DXF: a georeferenced tract
        # with no traverse still lands in the WGS 84 layer.
        drawable = placeable or any(v.georef is not None for v in result.verdicts)
        save.setEnabled(drawable)
        save.setToolTip(
            "Save tract and call layers as a GeoPackage (server-written; "
            "local-plane layers unplaced, georeferenced tracts placed)."
            if drawable
            else "No tract in this document has geometry to write."
        )
        row.addWidget(save)
        dxf = QPushButton("Save DXF…")
        dxf.clicked.connect(self.dxf_requested.emit)
        dxf.setEnabled(placeable)
        dxf.setToolTip(
            "Export the traverse as a CAD drawing (unplaced: point of "
            "beginning at the origin, feet)."
            if placeable
            else "No tract in this document has computed geometry to draw."
        )
        row.addWidget(self._certificate_button())
        copy_btn = QPushButton("Copy JSON")
        copy_btn.clicked.connect(self._copy_json)
        row.addWidget(copy_btn)
        row.addStretch()
        self._layout.addWidget(controls)

    def _add_metadata(self, ex) -> None:
        """The deed's own paper trail: parties, recording stamp, chain
        references, and any platted-lot designations. Absent on responses from
        pre-0.4 servers, so the panel simply does not render then."""
        md = getattr(ex, "metadata", None)
        if md is None:
            return
        lines: list[str] = []
        if md.grantors or md.grantees:
            arrow = " → ".join(
                part for part in (", ".join(md.grantors), ", ".join(md.grantees)) if part
            )
            lines.append(arrow)
        recorded = md.recording.text() if md.recording else ""
        when_where = " · ".join(
            part
            for part in (
                recorded,
                md.transfer_date or "",
                ", ".join(p for p in (md.county, md.state) if p),
            )
            if part
        )
        if when_where:
            lines.append(when_where)
        for ref in md.prior_references:
            note = f" — {ref.note}" if ref.note else ""
            lines.append(f"cites {ref.text()}{note}")
        for lb in md.lot_block:
            bits = [
                f"Lot {lb.lot}" if lb.lot else "",
                f"Block {lb.block}" if lb.block else "",
                lb.subdivision or "",
                f"(Plat Book {lb.plat_book}, Page {lb.plat_page})"
                if lb.plat_book or lb.plat_page
                else "",
            ]
            lines.append("plat ref: " + " ".join(b for b in bits if b))
        if not lines:
            return
        panel = QLabel("<br>".join(lines))
        panel.setWordWrap(True)
        panel.setTextFormat(Qt.TextFormat.RichText)
        panel.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
        self._layout.addWidget(panel)

    def _add_tract(self, tract: Tract, verdict: ServerVerdict) -> None:
        badge_bg = VERDICT_BADGE_BG[verdict.status]
        reason_color = verdict_text_color(verdict.status)

        title = QLabel(
            f'<span style="background:{badge_bg}; color:#ffffff; padding:2px 8px; '
            f'border-radius:3px;" title="GroundTruth verdict">{verdict.status}</span> '
            f"<b>{tract.name}</b>"
            + _stated_area_text(tract)
            + (" · SAVE AND EXCEPT" if tract.is_exception else "")
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        self._layout.addWidget(title)

        if verdict.geometry is not None:
            ratio = verdict.closure_ratio
            closure = "closes exactly" if ratio == "exact" else f"closure {ratio}"
            self._layout.addWidget(QLabel(f"{closure} · {_computed_area_text(tract, verdict)}"))
        for reason in verdict.reasons:
            lbl = QLabel(f"• {reason}")
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color:{reason_color};")
            self._layout.addWidget(lbl)

        easement = getattr(tract, "easement", None)
        if easement is not None:
            self._layout.addWidget(_easement_card(easement))

        if tract.calls:
            label = getattr(tract, "description_type", "") == "centerline"
            if label:
                header = QLabel("Centerline courses — the strip follows this line")
                header.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
                self._layout.addWidget(header)
            self._layout.addWidget(self._call_table(tract.calls, _unit_of(tract)))

        tie_calls = getattr(tract, "tie_calls", None) or []
        if tie_calls:
            tie_label = QLabel(f"Tie courses ({len(tie_calls)}) — not part of the boundary")
            tie_label.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
            self._layout.addWidget(tie_label)
            self._layout.addWidget(self._call_table(tie_calls, _unit_of(tract)))

        aliquot = getattr(tract, "aliquot", None)
        if aliquot is not None:
            chain = QLabel(f"PLSS: {aliquot.chain_text()}")
            chain.setWordWrap(True)
            chain.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
            self._layout.addWidget(chain)

        georef = getattr(verdict, "georef", None)
        if georef is not None and georef.rings:
            place = QPushButton(f"Place {tract.name} at its PLSS location")
            place.clicked.connect(
                lambda _, t=tract, v=verdict: self.place_georef_requested.emit(t, v)
            )
            self._layout.addWidget(place)
        elif verdict.placeable:
            place = QPushButton(f"Place {tract.name} on map (click the POB)")
            place.clicked.connect(lambda _, t=tract, v=verdict: self.place_requested.emit(t, v))
            self._layout.addWidget(place)

    def _certificate_button(self) -> QPushButton:
        """Always enabled, unlike the DXF button.

        A tract that refuses to locate itself still certifies the refusal, and
        a plat certifies its identification, so there is no client-side test
        for "nothing to certify" worth guessing at. The one document that has
        nothing at all to say is refused by the service, with the reason.
        """
        button = QPushButton("Save certificate…")
        button.clicked.connect(self.certificate_requested.emit)
        button.setToolTip(
            "Save the GroundTruth Certificate of Digitization: verdicts with "
            "reasons, closure, areas in the document's own unit, and every "
            "course with its verbatim source text."
        )
        return button

    def _call_table(self, calls, unit: str = "feet") -> QTableWidget:
        """One renderer for boundary and tie courses; the adjoiner column
        stays blank on calls that run with nothing.

        Distances are the document's own numbers, so the column says which
        unit those numbers are in. A table of metric courses labelled plainly
        "dist" is the kind of thing a reviewer only notices after trusting it.
        """
        table = QTableWidget(len(calls), 7)
        heading = "dist" if unit == "feet" else f"dist ({UNIT_LABELS.get(unit, unit)})"
        table.setHorizontalHeaderLabels(
            ["#", "type", "bearing", heading, "conf", "adjoiner", "verbatim"]
        )
        table.horizontalHeaderItem(3).setToolTip(
            f"Distances as recorded, in {unit}. Placed geometry is converted for you."
        )
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i, call in enumerate(calls):
            bearing = call.effective_bearing()
            cells = [
                str(i + 1),
                call.call_type,
                bearing.text() if bearing else "—",
                f"{call.effective_distance():.2f}" if call.effective_distance() else "—",
                call.confidence,
                getattr(call, "adjoiner", None) or "",
                call.verbatim_text,
            ]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if j == 4:
                    tones = confidence_colors(call.confidence)
                    if tones is not None:
                        background, foreground = tones
                        item.setBackground(background)
                        item.setForeground(foreground)
                table.setItem(i, j, item)
        table.resizeColumnsToContents()
        table.setMaximumHeight(min(220, 40 + 30 * len(calls)))
        return table

    # -- batch progress ------------------------------------------------------
    def show_batch_progress(self, settled: int, total: int) -> None:
        """Report a running batch at the top of the dock.

        Kept out of `_clear`'s way by living in its own slot: a batch can still
        be settling while the user reviews a document from an earlier one.
        """
        self.clear_batch_progress()
        self._batch_card = _progress_card(settled, total)
        self._layout.insertWidget(0, self._batch_card)
        self.show()

    def clear_batch_progress(self) -> None:
        card = getattr(self, "_batch_card", None)
        if card is not None:
            card.deleteLater()
            self._batch_card = None

    # -- helpers -------------------------------------------------------------
    def rotation_value(self) -> float:
        return self._rotation.value() if hasattr(self, "_rotation") else 0.0

    def _copy_json(self) -> None:
        if self._result is not None:
            QApplication.clipboard().setText(json.dumps(asdict(self._result.extraction), indent=2))

    def _clear(self) -> None:
        self._batch_card = None
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()


# How the document's distance unit reads in a column heading or a tooltip.
UNIT_LABELS = {"feet": "ft", "meters": "m", "varas": "varas", "chains": "chains"}


def _unit_of(tract) -> str:
    return getattr(tract, "distance_unit", None) or "feet"


def _stated_area_text(tract) -> str:
    """Report the area the document stated, in the document's own unit.

    Small easements are stated in square feet, where acres round too coarsely
    to mean anything, and a metric survey states square meters or hectares.
    The order matches the server's: whichever figure the verdict compared
    against is the one shown here.
    """
    sqm = getattr(tract, "stated_area_sqm", None)
    hectares = getattr(tract, "stated_hectares", None)
    sqft = getattr(tract, "stated_area_sqft", None)
    if sqm:
        return f" · stated {sqm:,.0f} m²"
    if hectares:
        return f" · stated {hectares:g} ha"
    if sqft:
        return f" · stated {sqft:,.0f} sq ft"
    if tract.stated_acreage:
        return f" · stated {tract.stated_acreage} ac"
    return ""


def _computed_area_text(tract, verdict) -> str:
    """The computed area, in the unit this document would state it in.

    A metric document gets a metric answer whether or not it stated an area:
    handing a Dutch plan "0.494 ac" would make the reviewer do the conversion
    the server already did. Hectares take over above one, where square meters
    stop being readable.
    """
    sqm = getattr(verdict, "computed_sqm", None)
    hectares = getattr(verdict, "computed_hectares", None)
    sqft = getattr(verdict, "computed_sqft", None)
    metric_document = getattr(verdict, "distance_unit", "feet") == "meters"
    if (getattr(tract, "stated_area_sqm", None) or metric_document) and sqm:
        if hectares and hectares >= 1:
            return f"computed {hectares:,.4f} ha"
        return f"computed {sqm:,.1f} m²"
    if getattr(tract, "stated_hectares", None) and hectares:
        return f"computed {hectares:,.4f} ha"
    if getattr(tract, "stated_area_sqft", None) and sqft:
        return f"computed {sqft:,.0f} sq ft"
    return f"computed {verdict.geometry.acres:.3f} ac"


def _easement_card(easement) -> QLabel:
    """What the instrument burdens the land with, in the order a land agent
    reads it: what it is for, how wide, how long, whose exclusive use, what
    rights it grants, and which parcel carries it."""
    bits = []
    if easement.easement_type:
        bits.append(f"<b>{easement.easement_type.title()} easement</b>")
    if easement.width_ft:
        bits.append(f"{easement.width_ft:g} ft wide")
    if easement.term:
        bits.append(easement.term)
    if easement.exclusive is not None:
        bits.append("exclusive" if easement.exclusive else "non-exclusive")
    lines = [" · ".join(bits)] if bits else []
    if easement.rights:
        lines.append(f"Rights: {', '.join(easement.rights)}")
    if easement.servient_reference is not None:
        text = easement.servient_reference.text()
        if text:
            lines.append(f"Burdens: {text}")
    label = QLabel("<br>".join(lines) or "Easement")
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    label.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
    return label


def _document_banner(verdict) -> QLabel:
    """The verdict on a document that produced no tracts.

    A plat or an as-built has nothing to place, so this banner is the whole
    result for those documents. It leads with what the sheet is, then says what
    could not be verified, because "REVIEW" without a reason is not a finding.
    """
    lines = [f"<b>{verdict.document or 'Document'} · {verdict.status}</b>"]
    if verdict.summary:
        lines.append(verdict.summary)
    for reason in verdict.reasons:
        lines.append(f"• {reason}")
    label = QLabel("<br>".join(lines))
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    label.setStyleSheet(notes_style())
    return label


def _plat_card(plat) -> QLabel:
    """A recorded plat as a title searcher reads it: which subdivision, which
    lots, and where it is recorded so it can be cited."""
    bits = []
    if plat.subdivision:
        bits.append(f"<b>{plat.subdivision}</b>")
    if plat.lots:
        bits.append(f"Lot{'s' if len(plat.lots) > 1 else ''} {', '.join(plat.lots)}")
    if plat.blocks:
        bits.append(f"Block {', '.join(plat.blocks)}")
    lines = [" · ".join(bits)] if bits else ["Plat"]
    if plat.recording is not None:
        text = plat.recording.text()
        if text:
            lines.append(f"Recorded: {text}")
    if plat.surveyor:
        lines.append(f"Surveyor: {plat.surveyor}")
    if plat.date:
        lines.append(f"Dated: {plat.date}")
    return _card(lines)


def _asbuilt_card(sheet) -> QLabel:
    """A utility as-built in the order an engineer checks it: whose system,
    what utility, and from when, because vintage decides how far to trust the
    rest of the sheet."""
    bits = []
    if sheet.utility:
        bits.append(f"<b>{sheet.utility.title()}</b>")
    if sheet.agency:
        bits.append(sheet.agency)
    if sheet.vintage:
        bits.append(sheet.vintage)
    lines = [" · ".join(bits)] if bits else ["As-built"]
    if sheet.engineer:
        lines.append(f"Engineer: {sheet.engineer}")
    if sheet.stationing:
        lines.append(f"Stationing: {sheet.stationing}")
    if sheet.pipe:
        lines.append(f"Pipe: {sheet.pipe}")
    if sheet.sheets:
        lines.append(f"Sheets: {sheet.sheets}")
    return _card(lines)


def _card(lines: list[str]) -> QLabel:
    label = QLabel("<br>".join(lines))
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    label.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
    return label


def _progress_card(settled: int, total: int) -> QLabel:
    """How far a submitted batch has got, in documents rather than percent.

    A batch runs for minutes to hours, so the useful question is "how many are
    done", not "what fraction of a bar is filled".
    """
    label = QLabel(
        f"<b>Batch running</b> · {settled} of {total} documents settled.<br>"
        "You can keep working; results load here when the batch finishes."
    )
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    label.setStyleSheet(notes_style())
    return label
