"""Folder extraction: submit one batch, poll it, collect the results.

The synchronous path holds a connection open for the length of one extraction,
which is fine for one document and impossible for twenty. This task submits
them all at once and then waits, reporting progress as the server settles
documents, so a user can start a folder and go do something else.

Waiting is the whole design. A batch usually lands inside an hour and can take
a day, so the poll interval is deliberately unhurried and the task is
cancellable at every step. Cancelling stops the polling; it does not stop the
batch, which is already paid for and still collectable from the account page or
`GET /v1/batches`.
"""

from __future__ import annotations

import time

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from ._vendor.groundtruth_core.hosted import (
    fetch_batch_results,
    get_batch,
    result_from_payload,
    submit_batch,
)
from ._vendor.groundtruth_core.result import ExtractionError

# Slow on purpose. The server's own poller runs every five minutes, so asking
# more often than that cannot learn anything new, and a plugin that hammers a
# status endpoint for an hour is a support ticket waiting to happen.
POLL_SECONDS = 20.0
# A batch expires upstream after 24 hours. Stopping an hour short of that keeps
# the task from waiting on something that can no longer arrive.
MAX_WAIT_SECONDS = 23 * 60 * 60


class BatchTask(QgsTask):
    """Submit a folder of PDFs and deliver each result as the batch settles."""

    # (batch_id, document_count)
    submitted = pyqtSignal(str, int)
    # (settled_count, total_count)
    progress_changed = pyqtSignal(int, int)
    # [(document_name, ExtractionResult | None, error_or_empty), ...]
    completed = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(
        self,
        documents: list[tuple[str, bytes]],
        api_url: str,
        api_key: str,
        poll_seconds: float = POLL_SECONDS,
    ):
        super().__init__(f"Easting: extracting {len(documents)} documents", QgsTask.Flag.CanCancel)
        self._documents = documents
        self._api_url = api_url
        self._api_key = api_key
        self._poll_seconds = poll_seconds
        self._batch_id = ""
        self._results: list[tuple[str, object, str]] = []
        self._error: str | None = None

    @property
    def batch_id(self) -> str:
        return self._batch_id

    def run(self) -> bool:  # worker thread
        try:
            body = submit_batch(self._api_url, self._api_key, self._documents)
            self._batch_id = str(body.get("batch") or "")
            total = int(body.get("documents") or len(self._documents))
            self.submitted.emit(self._batch_id, total)
            self.setProgress(1)

            if not self._wait_for_end(total):
                return False

            fetched = fetch_batch_results(self._api_url, self._api_key, self._batch_id)
            for entry in fetched.get("results") or []:
                name = str(entry.get("document") or "")
                if entry.get("status") == "succeeded" and entry.get("result"):
                    # Parsed here rather than on the UI thread: a folder of
                    # twenty multi-tract deeds is real work.
                    self._results.append((name, result_from_payload(entry["result"]), ""))
                else:
                    self._results.append((name, None, str(entry.get("error") or "Failed.")))
            self.setProgress(100)
            return True
        except ExtractionError as exc:
            self._error = str(exc)
            return False
        except Exception as exc:  # never let an exception escape a QgsTask
            self._error = f"Unexpected error: {exc}"
            return False

    def _wait_for_end(self, total: int) -> bool:
        """Poll until the batch ends, the user cancels, or the wait runs out."""
        waited = 0.0
        while waited < MAX_WAIT_SECONDS:
            if self.isCanceled():
                self._error = (
                    f"Stopped watching batch {self._batch_id}. It is still "
                    "running and already paid for: collect it from your "
                    "account page when it finishes."
                )
                return False
            status = get_batch(self._api_url, self._api_key, self._batch_id)
            settled = int(status.get("succeeded") or 0) + int(status.get("failed") or 0)
            self.progress_changed.emit(settled, total)
            # Leave the last percent for the fetch, so the bar never sits at
            # 100 while results are still downloading.
            self.setProgress(min(99, 1 + (settled * 98 / total if total else 0)))
            if status.get("status") in ("ended", "failed"):
                return True
            time.sleep(self._poll_seconds)
            waited += self._poll_seconds

        self._error = (
            f"Batch {self._batch_id} did not finish within a day. Collect it "
            "from your account page, or contact support@easting.ai."
        )
        return False

    def finished(self, ok: bool) -> None:  # main thread
        if ok:
            self.completed.emit(self._results)
        else:
            self.failed.emit(self._error or "Batch extraction was cancelled.")
