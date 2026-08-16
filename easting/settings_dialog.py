"""Settings: the Easting API key and the service URL.

The plugin is hosted-only. It holds no provider credentials and picks no
model — the Easting API does both — so this dialog collects a key and a URL
and nothing else.
"""

from __future__ import annotations

from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
)

from .theme import secondary_text

SERVICE_KEY_SETTING = (
    "easting/service_key"  # pragma: allowlist secret (a QgsSettings path, not a credential)
)
API_URL_SETTING = "easting/api_url"

DEFAULT_API_URL = "https://api.easting.ai"

# BYOK is gone, and with it the settings that fed it: "easting/api_key" and
# "easting/model" (plus the "groundtruth/*" pair they were migrated from). They
# are deliberately left in QgsSettings rather than removed — a provider API key
# is the user's property, and silently deleting one from their profile on
# upgrade would be presumptuous. Nothing reads them.


def get_service_key() -> str:
    return QgsSettings().value(SERVICE_KEY_SETTING, "", type=str).strip()


def get_api_url() -> str:
    return QgsSettings().value(API_URL_SETTING, DEFAULT_API_URL, type=str).strip()


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Easting settings")
        self.setMinimumWidth(460)

        form = QFormLayout(self)

        self._key_edit = QLineEdit(get_service_key())
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("east_live_...")
        form.addRow("Easting API key", self._key_edit)

        self._url_edit = QLineEdit(get_api_url())
        self._url_edit.setPlaceholderText(DEFAULT_API_URL)
        form.addRow("API URL", self._url_edit)

        # The old version of this row was six links in one paragraph, and the
        # phrase "card required" sat in the middle of it, at the moment
        # somebody had just decided to try the thing. The offer is now a button
        # that finishes without leaving QGIS.
        self._start_free = QPushButton("Start free: 3 documents, no card")
        self._start_free.clicked.connect(self._signup)
        form.addRow(self._start_free)

        get_key = QLabel(
            # docs.easting.ai directly, from this release on. The old
            # easting.ai/help/ URL keeps redirecting forever for the installed
            # copies of 0.7.0 that carry it (docs/plans/DOCS_SITE.md).
            'New here? <a href="https://docs.easting.ai/plugin/?src=plugin">The user guide</a> '
            "walks install through GeoPackage export. Already have a key? Paste "
            "it above. Lost it, or rotating? "
            '<a href="https://api.easting.ai/account?src=plugin">Your account page</a>.'
        )
        get_key.setWordWrap(True)
        get_key.setOpenExternalLinks(True)
        form.addRow(get_key)

        note = QLabel(
            "Extraction runs on the Easting service: the plugin uploads the PDF, "
            "the service reads it and returns the legal description with its "
            "GroundTruth verdicts. Your key is stored in your QGIS profile. "
            "Change the API URL only if Easting gives you a different endpoint."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{secondary_text()};")
        form.addRow(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _signup(self) -> None:
        """Sign up against whatever URL is in the box, not the default.

        Someone pointed at a staging endpoint should get a staging account,
        and reading the field rather than the constant is the difference.
        """
        from .signup_dialog import SignupDialog

        dialog = SignupDialog(self._url_edit.text().strip() or DEFAULT_API_URL, self)
        if dialog.exec() and dialog.api_key:
            self._key_edit.setText(dialog.api_key)
            self._start_free.setText("Key added. Save to finish.")
            self._start_free.setEnabled(False)

    def _save(self) -> None:
        settings = QgsSettings()
        settings.setValue(SERVICE_KEY_SETTING, self._key_edit.text().strip())
        settings.setValue(API_URL_SETTING, self._url_edit.text().strip() or DEFAULT_API_URL)
        self.accept()
