"""Background extraction via QgsTask so the UI stays responsive."""

from __future__ import annotations

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from ._vendor.groundtruth_core.hosted import extract_pdf_hosted
from ._vendor.groundtruth_core.result import ExtractionError, ExtractionResult


class ExtractTask(QgsTask):
    """Runs one deed extraction off the UI thread, through the Easting API.

    Qt signals deliver the outcome back on the main thread; finished() runs
    there per QgsTask semantics.
    """

    succeeded = pyqtSignal(object)  # ExtractionResult
    failed = pyqtSignal(str)

    def __init__(self, pdf_path: str, api_url: str, api_key: str):
        super().__init__(f"Easting: extracting {pdf_path}", QgsTask.Flag.CanCancel)
        self._pdf_path = pdf_path
        self._api_url = api_url
        self._api_key = api_key
        self._result: ExtractionResult | None = None
        self._error: str | None = None

    def run(self) -> bool:  # worker thread
        try:
            self.setProgress(10)
            self._result = extract_pdf_hosted(self._api_url, self._api_key, self._pdf_path)
            self.setProgress(100)
            return True
        except ExtractionError as exc:
            self._error = str(exc)
            return False
        except Exception as exc:  # never let an exception escape a QgsTask
            self._error = f"Unexpected error: {exc}"
            return False

    def finished(self, ok: bool) -> None:  # main thread
        if ok and self._result is not None:
            self.succeeded.emit(self._result)
        else:
            self.failed.emit(self._error or "Extraction was cancelled.")
