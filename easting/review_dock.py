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

from ._vendor.groundtruth_core.model import BearingModel, Tract
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
    locate_requested = pyqtSignal()  # ask the service where the tracts sit
    place_suggested_requested = pyqtSignal(object, object)  # (Tract, Verdict) — preview at pob
    # (corrections, note) — one course's changed fields, as the service takes
    # them. The dock collects the typing; the service does the changing.
    correction_requested = pyqtSignal(list)
    close_requested = pyqtSignal(int)  # tract index — fit its courses by compass rule
    # A typed value the dock could not read. Reported through the message bar
    # rather than a modal: a dialog in the middle of a table edit steals the
    # keyboard from someone who is halfway through fixing four courses.
    correction_refused = pyqtSignal(str)
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
        for index, (tract, verdict) in enumerate(zip(ex.tracts, result.verdicts, strict=True)):
            self._add_tract(tract, verdict, index)

        # Two rows, not one. Placement and document actions are different
        # jobs, and running six buttons and a spin box together across one
        # line left the dock's own controls competing with its exports for
        # width at any sane dock size.
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
        row.addStretch()
        self._layout.addWidget(controls)

        actions = QWidget()
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 4, 0, 0)
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
        # Never added to a layout until 2026-08-12, so a shipped and
        # advertised export was unreachable from the dock: the 0.6.0
        # changelog names "Save DXF…" and only the portal and the API could
        # produce one.
        row.addWidget(dxf)
        row.addWidget(self._certificate_button())
        # Only offered where it could work: an aliquot tract is already
        # located, and a tract with no traverse has nothing to contain.
        locatable = any(v.placeable and v.georef is None for v in result.verdicts)
        locate = QPushButton("Suggest locations…")
        locate.clicked.connect(self.locate_requested.emit)
        locate.setEnabled(locatable)
        locate.setToolTip(
            "Ask the service whether any tract commences at a PLSS corner it "
            "can resolve. Suggestions are previewed, never placed."
            if locatable
            else "No tract here needs a suggested location."
        )
        row.addWidget(locate)
        copy_btn = QPushButton("Copy JSON")
        copy_btn.clicked.connect(self._copy_json)
        row.addWidget(copy_btn)
        row.addStretch()
        self._layout.addWidget(actions)

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

    def _add_tract(self, tract: Tract, verdict: ServerVerdict, index: int = 0) -> None:
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

        # Editable only where there is something to re-sign. A correction is
        # applied by the service, which needs the response it signed; an
        # extraction off the direct path has none, and offering an edit there
        # would put a cell in front of an operator that can only fail.
        editable = index if self._correctable() else None

        log = _operator_log(verdict, self._corrections_for(index))
        if log is not None:
            self._layout.addWidget(log)

        # Offered, never automatic. An adjustment nobody asked for is the
        # silent auto-close by another name, so the button says what it will
        # distribute and the verdict above it stays exactly where it is.
        if editable is not None and _closable(verdict) and not getattr(verdict, "adjusted", False):
            misclosure = verdict.geometry.misclosure
            mark = UNIT_LABELS.get(_unit_of(tract), _unit_of(tract))
            close = QPushButton(f"Close the shape — distributes {misclosure:.2f} {mark}")
            close.setFlat(True)
            close.setStyleSheet(f"color:{secondary_text()}; text-align:left;")
            close.setToolTip(
                "Fits the courses by compass rule so the shape closes. The "
                "verdict does not change: the recorded description still "
                "miscloses, and the certificate says so."
            )
            close.clicked.connect(lambda _, i=index: self.close_requested.emit(i))
            self._layout.addWidget(close)

        easement = getattr(tract, "easement", None)
        if easement is not None:
            self._layout.addWidget(_easement_card(easement))

        if tract.calls:
            label = getattr(tract, "description_type", "") == "centerline"
            if label:
                header = QLabel("Centerline courses — the strip follows this line")
                header.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
                self._layout.addWidget(header)
            if editable is not None:
                # An editable cell that looks exactly like a read-only one is
                # a feature nobody finds. Qt has no affordance for this and
                # the verbatim column must keep its width, so the invitation
                # is a line of text rather than a seventh column.
                hint = QLabel(
                    "Double-click a bearing or distance to correct what was "
                    "misread — the document is re-verified and re-signed."
                )
                hint.setWordWrap(True)
                hint.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
                self._layout.addWidget(hint)
            self._layout.addWidget(
                self._call_table(tract.calls, _unit_of(tract), tract=editable, kind="boundary")
            )

        tie_calls = getattr(tract, "tie_calls", None) or []
        if tie_calls:
            tie_label = QLabel(f"Tie courses ({len(tie_calls)}) — not part of the boundary")
            tie_label.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
            self._layout.addWidget(tie_label)
            self._layout.addWidget(
                self._call_table(tie_calls, _unit_of(tract), tract=editable, kind="tie")
            )

        aliquot = getattr(tract, "aliquot", None)
        if aliquot is not None:
            chain = QLabel(f"PLSS: {aliquot.chain_text()}")
            chain.setWordWrap(True)
            chain.setStyleSheet(f"color:{secondary_text()}; font-size:11px;")
            self._layout.addWidget(chain)

        location = getattr(verdict, "location", None)
        if location is not None:
            self._layout.addWidget(_location_card(location))

        georef = getattr(verdict, "georef", None)
        if georef is not None and georef.rings:
            place = QPushButton(f"Place {tract.name} at its PLSS location")
            place.clicked.connect(
                lambda _, t=tract, v=verdict: self.place_georef_requested.emit(t, v)
            )
            self._layout.addWidget(place)
        elif verdict.placeable:
            if location is not None and location.placeable:
                # One prominent action, not two. The card above already names
                # the tract, and a second full-width button under the first
                # reads as a decision to make before anything can happen. The
                # manual route stays, flat and quiet, because an operator who
                # disagrees with the suggestion is the point of the feature.
                suggested = QPushButton("Place at the suggestion")
                suggested.setToolTip(
                    "Previews the parcel at the suggested point of beginning. "
                    "Nothing is placed until you press Confirm, and you can "
                    "move it first."
                )
                suggested.clicked.connect(
                    lambda _, t=tract, v=verdict: self.place_suggested_requested.emit(t, v)
                )
                self._layout.addWidget(suggested)
                place = QPushButton("or click the POB yourself")
                place.setFlat(True)
                place.setStyleSheet(f"color:{secondary_text()}; text-align:left;")
            else:
                place = QPushButton(f"Place {tract.name} on map (click the POB)")
            place.clicked.connect(lambda _, t=tract, v=verdict: self.place_requested.emit(t, v))
            self._layout.addWidget(place)

    def _correctable(self) -> bool:
        """Whether this result can be corrected at all.

        The service applies a correction to the response it signed, so an
        extraction that kept no server body has nothing to send back."""
        return bool(getattr(self._result, "raw", None))

    def _corrections_for(self, index: int) -> list[dict]:
        """This tract's slice of the operator log, from the payload as it
        arrived. Parsing is lossy by design, so the raw body is where the log
        lives rather than a dataclass that would have dropped it."""
        raw = getattr(self._result, "raw", None) or {}
        return [c for c in (raw.get("corrections") or []) if int(c.get("tract") or 0) == index]

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

    def _call_table(
        self, calls, unit: str = "feet", tract: int | None = None, kind: str = "boundary"
    ) -> QTableWidget:
        """One renderer for boundary and tie courses; the adjoiner column
        stays blank on calls that run with nothing.

        Distances are the document's own numbers, so the column says which
        unit those numbers are in. A table of metric courses labelled plainly
        "dist" is the kind of thing a reviewer only notices after trusting it.

        With a `tract` index the bearing and distance cells become editable:
        double-click, type what the instrument says, and the service applies
        it, re-verifies and re-signs. Only those two columns. `verbatim` is the
        document's own words and the thing a correction is checked against, and
        a table able to rewrite it would make the certificate's diff
        meaningless.
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
                if tract is not None and j in _EDITABLE_COLUMNS:
                    item.setToolTip(
                        "Double-click to correct what was misread. The document "
                        "is re-verified against your reading and re-signed, and "
                        "the change is printed on the certificate."
                    )
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(i, j, item)
        if tract is not None:
            table.setEditTriggers(
                QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed
            )
            table.itemChanged.connect(
                lambda item, t=tract, k=kind, c=calls: self._cell_corrected(item, t, k, c)
            )
        table.resizeColumnsToContents()
        table.setMaximumHeight(min(220, 40 + 30 * len(calls)))
        return table

    def _cell_corrected(self, item, tract: int, kind: str, calls) -> None:
        """Turn one edited cell into a correction, or refuse it and put the
        cell back.

        Refusing is the important half. A bearing nobody can read has to come
        back as the text it replaced rather than as a plausible number nobody
        typed, which is the failure this product declines everywhere else.
        """
        row, column = item.row(), item.column()
        if row >= len(calls):
            return
        call = calls[row]
        curve = call.call_type == "curve"
        field = (
            ("chord_bearing" if curve else "bearing")
            if column == 2
            else ("chord_length" if curve else "distance")
        )
        before = getattr(call, field, None)
        typed = item.text().strip()
        try:
            after = self._parse_cell(field, typed)
        except ValueError as exc:
            self.correction_refused.emit(str(exc))
            self._restore_cell(item, before)
            return
        if _same_value(before, after):
            self._restore_cell(item, before)
            return
        self.correction_requested.emit(
            [
                {
                    "tract": tract,
                    "call": row,
                    "kind": kind,
                    "field": field,
                    "before": _wire_value(before),
                    "after": _wire_value(after),
                }
            ]
        )

    @staticmethod
    def _parse_cell(field: str, typed: str):
        if not typed or typed == "—":
            return None
        if field.endswith("bearing"):
            return BearingModel.from_text(typed)
        try:
            return float(typed.replace(",", ""))
        except ValueError:
            raise ValueError(f'"{typed}" is not a number.') from None

    def _restore_cell(self, item, before) -> None:
        """Put a cell back without the edit signal firing on our own write."""
        table = item.tableWidget()
        blocked = table.blockSignals(True)
        if before is None:
            item.setText("—")
        elif isinstance(before, BearingModel):
            item.setText(before.text())
        else:
            item.setText(f"{float(before):.2f}")
        table.blockSignals(blocked)

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


# The two columns an operator may correct. `verbatim` is deliberately not one:
# it is the document's own words and the thing every correction is checked
# against.
_EDITABLE_COLUMNS = (2, 3)


def _same_value(before, after) -> bool:
    """Whether an edited cell actually says something different. A cell
    re-typed identically, or reformatted by the display, is not a correction."""
    if before is None or after is None:
        return before is None and after is None
    if isinstance(before, BearingModel) or isinstance(after, BearingModel):
        return _wire_value(before) == _wire_value(after)
    return abs(float(before) - float(after)) <= 1e-6


def _wire_value(value):
    """A corrected value as JSON carries it."""
    if isinstance(value, BearingModel):
        return {
            "ns": value.ns,
            "degrees": value.degrees,
            "minutes": value.minutes,
            "seconds": value.seconds,
            "ew": value.ew,
        }
    return value


def _closable(verdict) -> bool:
    """A tract worth offering to close.

    Three conditions, and each rules out a fit that would mean nothing. There
    has to be geometry, because no fit rescues a traverse that failed outright.
    The closure cannot already be exact, which is what a null denominator says.
    And the verdict cannot be PASS: a description the service accepted needs no
    best fit, and offering one there invites a disclosed adjustment onto a
    certificate that had nothing to disclose.

    Reading the verdict rather than a tolerance keeps the threshold in one
    place. The server decides what closes well enough; this only asks.
    """
    geometry = getattr(verdict, "geometry", None)
    if geometry is None or geometry.closure_denominator is None:
        return False
    if getattr(verdict, "status", "") == "PASS":
        return False
    return geometry.misclosure > 0


def _operator_log(verdict, corrections: list[dict]) -> QLabel | None:
    """What a person changed here, and what was fitted.

    Two entries saying different things on purpose: a correction moved the
    verdict because it is a better reading of the document; an adjustment did
    not, because the document still does not close.
    """
    lines: list[str] = []
    for entry in corrections:
        where = "tie course" if entry.get("kind") == "tie" else "course"
        number = int(entry.get("call") or 0) + 1
        field = str(entry.get("field") or "").replace("_", " ")
        lines.append(
            f"{where} {number} {field}: {_log_value(entry.get('before'))} → "
            f"{_log_value(entry.get('after'))}"
            + (f" · {entry['by']}" if entry.get("by") else "")
            + (f" — {entry['note']}" if entry.get("note") else "")
        )
    if getattr(verdict, "adjusted", False):
        lines.append(
            "Geometry adjusted. The recorded courses do not close; the verdict "
            "above judges the record and was not recomputed over the fit."
        )
    return _card(lines) if lines else None


def _log_value(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        try:
            return BearingModel.from_dict(value).text()
        except (KeyError, TypeError, ValueError):
            return str(value)
    if isinstance(value, int | float):
        return f"{float(value):.2f}"
    return str(value)


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


def _location_card(location) -> QLabel:
    """The suggested position, and what was compared to arrive at it.

    Two shapes, deliberately. With a position, the card names the monument and
    the assumptions it rests on, so the operator can disagree before pressing
    Confirm. Without one, the card is the whole answer: which corner was tried
    and why nothing is being offered. The second case is the one that earns
    the feature, because a wrong position placed quietly is worse than a blank
    canvas.
    """
    lines = []
    if location.basis:
        lines.append(f"<b>Suggested location</b> · {location.basis}")
    else:
        lines.append("<b>Suggested location</b>")
    if not location.placeable:
        lines.append("No position suggested.")
    for reason in location.reasons:
        lines.append(f"• {reason}")
    if location.plss_id:
        lines.append(f"PLSS {location.plss_id}")
    label = QLabel("<br>".join(lines))
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
