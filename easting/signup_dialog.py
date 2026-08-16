"""Start free without leaving QGIS.

The plugin is where the audience is. Downloads run at roughly ten a day while
the website sees no humans at all, so a signup that begins with "open a
browser" loses most of the people it is for (`docs/plans/implemented/FREE_TIER.md`). This
dialog collects what the API asks for, posts it, and writes the key straight
into settings, so the next thing somebody does is extract a deed.

No card, and the form says so out loud, because "card required" sat in this
dialog at exactly the moment of intent.
"""

from __future__ import annotations

import json

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ._vendor.groundtruth_core.result import ExtractionError
from ._vendor.groundtruth_core.transport import post_with_retries
from .theme import secondary_text

TERMS_URL = "https://easting.ai/terms/?src=plugin"
PRIVACY_URL = "https://easting.ai/privacy/?src=plugin"


class SignupDialog(QDialog):
    """Three documents, no card, in exchange for who you are.

    Returns the plaintext key through `api_key` when accepted. The caller
    writes it to settings; this dialog deliberately does not, so that the one
    place that owns QgsSettings stays the one place.
    """

    def __init__(self, api_url: str, parent=None):
        super().__init__(parent)
        self._api_url = api_url.rstrip("/")
        self.api_key = ""

        self.setWindowTitle("Start free")
        self.setMinimumWidth(460)
        outer = QVBoxLayout(self)

        intro = QLabel(
            "<b>Three documents on us. No card.</b><br>"
            "Extract a recorded deed or easement, verify closure and stated "
            "acreage, and place it on the map. The documents never expire."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        form = QFormLayout()
        self._email = QLineEdit()
        self._email.setPlaceholderText("you@yourfirm.com")
        form.addRow("Email", self._email)
        self._company = QLineEdit()
        self._company.setPlaceholderText("Your firm")
        form.addRow("Company", self._company)
        self._role = QLineEdit()
        self._role.setPlaceholderText("Right of way agent, abstractor, GIS analyst")
        form.addRow("Role", self._role)
        outer.addLayout(form)

        self._terms = QCheckBox("I accept the Terms of Service and Privacy Policy")
        outer.addWidget(self._terms)
        links = QLabel(
            f'<a href="{TERMS_URL}">Terms of Service</a> &middot; '
            f'<a href="{PRIVACY_URL}">Privacy Policy</a>'
        )
        links.setOpenExternalLinks(True)
        links.setStyleSheet(f"color:{secondary_text()};")
        outer.addWidget(links)

        note = QLabel(
            "Your key appears here and is stored in your QGIS profile. A new "
            "address also receives a sign-in invitation for app.easting.ai by "
            "email; the portal is how you get back in if the key is ever lost."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{secondary_text()};")
        outer.addWidget(note)

        # Failures land here rather than in a message box. A modal on top of a
        # modal is how the review dock once deadlocked the headless smoke test.
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._error.setOpenExternalLinks(True)
        self._error.setStyleSheet("color:#b00020;")
        self._error.hide()
        outer.addWidget(self._error)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start free")
        self._buttons.accepted.connect(self._submit)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    def _fail(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()
        self._buttons.setEnabled(True)

    def _submit(self) -> None:
        email = self._email.text().strip()
        company = self._company.text().strip()
        role = self._role.text().strip()
        if not email or not company or not role:
            self._fail("Email, company and role are all required.")
            return
        if not self._terms.isChecked():
            self._fail("Accept the Terms of Service and Privacy Policy to continue.")
            return

        self._error.hide()
        self._buttons.setEnabled(False)
        body = json.dumps(
            {"email": email, "company": company, "role": role, "accepted_terms": True}
        ).encode()
        try:
            raw = post_with_retries(
                f"{self._api_url}/v1/signup",
                body,
                {"Content-Type": "application/json"},
                # One shot. A signup is not idempotent from the caller's side,
                # and retrying a 400 only makes somebody wait for the same
                # answer three times.
                max_retries=0,
                timeout=30.0,
                service="Easting",
                on_error=_message_for,
            )
        except ExtractionError as exc:
            self._fail(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a dialog must never take QGIS down
            self._fail(f"Could not reach Easting: {exc}")
            return

        try:
            payload = json.loads(raw)
            self.api_key = payload["api_key"]
        except (ValueError, KeyError):
            self._fail("Easting returned an answer this version could not read.")
            return
        self.accept()


def _message_for(status: int, detail: str) -> str | None:
    """Turn the API's refusals into something worth reading in a dialog.

    The server already writes for a person, so this mostly passes the message
    through; what it adds is the one case where the answer is "not you, us".
    """
    if status == 429:
        return (
            "More people started free today than Easting can cover. "
            "Try again tomorrow, or buy a document pack at "
            "https://easting.ai/products/deeds/?src=plugin."
        )
    if status == 503:
        return (
            "Free signups are closed right now. Plans and a document pack are "
            "at https://easting.ai/products/deeds/?src=plugin."
        )
    return detail or f"Easting returned error {status}."
