"""Background extraction via QgsTask so the UI stays responsive.

Submit and poll, not one long request. A recorded instrument with several
tracts can take minutes, and the service sits behind an edge that will not
hold a connection open that long: a document that took 214 seconds used to
complete on the server and then fail in the plugin, which is the worst
possible combination. Handing the document over and asking for the answer
costs a couple of seconds on a fast deed and is the difference between
finishing and erroring on a slow one.

Nothing above this changed. The task emits the same two signals it always
did, so the dock, placement, and every export path are untouched.
"""

from __future__ import annotations

import time

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from ._vendor.groundtruth_core.hosted import (
    get_extract,
    result_from_payload,
    submit_extract,
)
from ._vendor.groundtruth_core.result import ExtractionError, ExtractionResult

# Fast enough that a 20 second deed feels immediate, slow enough that a
# four-minute instrument costs a couple of hundred requests rather than
# thousands. Batch polls every 20 seconds because a batch settles in an hour;
# a single document settles in a minute or two, so it gets its own cadence.
POLL_SECONDS = 3.0
# The server's own ceiling is 900 seconds of extraction. Waiting past that
# plus a margin means waiting for something that is not coming.
MAX_WAIT_SECONDS = 1_000


class ExtractTask(QgsTask):
    """Runs one deed extraction off the UI thread, through the Easting API.

    Qt signals deliver the outcome back on the main thread; finished() runs
    there per QgsTask semantics.
    """

    succeeded = pyqtSignal(object)  # ExtractionResult
    failed = pyqtSignal(str)

    def __init__(
        self,
        pdf_path: str,
        api_url: str,
        api_key: str,
        poll_seconds: float = POLL_SECONDS,
    ):
        super().__init__(f"Easting: extracting {pdf_path}", QgsTask.Flag.CanCancel)
        self._pdf_path = pdf_path
        self._api_url = api_url
        self._api_key = api_key
        self._poll_seconds = poll_seconds
        self._job_id = ""
        self._result: ExtractionResult | None = None
        self._error: str | None = None

    @property
    def job_id(self) -> str:
        return self._job_id

    def run(self) -> bool:  # worker thread
        from pathlib import Path

        try:
            path = Path(self._pdf_path)
            submitted = submit_extract(
                self._api_url, self._api_key, path.read_bytes(), doc_name=path.name
            )
            self._job_id = str(submitted.get("job") or "")
            self.setProgress(5)

            payload = self._wait_for_result()
            if payload is None:
                return False
            # Parsed off the UI thread: a dense multi-tract deed is real work.
            self._result = result_from_payload(payload)
            self.setProgress(100)
            return True
        except ExtractionError as exc:
            self._error = str(exc)
            return False
        except Exception as exc:  # never let an exception escape a QgsTask
            self._error = f"Unexpected error: {exc}"
            return False

    def _wait_for_result(self) -> dict | None:
        """Poll until the job finishes, the user cancels, or the wait runs out.

        Cancelling stops the polling and nothing else. The extraction is
        already running and will bill whether or not anyone is listening, so
        the message says where the result went rather than implying it was
        thrown away.
        """
        waited = 0.0
        while waited < MAX_WAIT_SECONDS:
            if self.isCanceled():
                self._error = (
                    f"Stopped waiting for {self._job_id}. The extraction is "
                    "still running and already paid for; it stays collectable "
                    "for seven days."
                )
                return None
            status = get_extract(self._api_url, self._api_key, self._job_id)
            state = status.get("status")
            if state == "succeeded":
                result = status.get("result")
                if not result:
                    self._error = "The extraction finished but returned nothing."
                    return None
                return result
            if state == "failed":
                self._error = str(status.get("error") or "The extraction failed.")
                return None
            # Nothing to report but progress: creep the bar so a long document
            # does not look stalled, without pretending to know how far along
            # the extraction is.
            self.setProgress(min(95, 5 + waited))
            time.sleep(self._poll_seconds)
            waited += self._poll_seconds

        self._error = (
            f"Extraction {self._job_id} did not finish in time. Try again, or "
            "send very large documents through Extract folder…, which waits "
            "as long as it takes."
        )
        return None

    def finished(self, ok: bool) -> None:  # main thread
        if ok and self._result is not None:
            self.succeeded.emit(self._result)
        else:
            self.failed.emit(self._error or "Extraction was cancelled.")
