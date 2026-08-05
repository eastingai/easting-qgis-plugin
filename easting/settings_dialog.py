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

        get_key = QLabel(
            'No key yet? <a href="https://easting.ai/products/deeds/">Start a free '
            "trial at easting.ai</a>: 3 documents on us, card required, and the "
            "key is shown once at checkout. Lost the key, or rotating it? "
            '<a href="https://api.easting.ai/account">Manage it at your account '
            "page</a>. Using the service means accepting the "
            '<a href="https://easting.ai/terms/">Terms of Service</a> and '
            '<a href="https://easting.ai/privacy/">Privacy Policy</a>; checkout '
            "asks for that acceptance explicitly."
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

    def _save(self) -> None:
        settings = QgsSettings()
        settings.setValue(SERVICE_KEY_SETTING, self._key_edit.text().strip())
        settings.setValue(API_URL_SETTING, self._url_edit.text().strip() or DEFAULT_API_URL)
        self.accept()
