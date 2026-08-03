"""Provider profile editing, backed by the shared credential store."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opencull_gui import credentials
from opencull_gui.providers import AGENTS, ProviderError, ProviderStore

from . import theme

RECOMMENDED = {
    "A": "openai/gpt-5.6-luna-pro",
    "B": "openai/gpt-5.6-luna-pro",
    "C": "openai/gpt-4.1-mini",
    "D": "openai/gpt-5.6-luna-pro",
}
STORED_PLACEHOLDER = "•" * 24


class ProvidersDialog(QDialog):
    """Edit the one provider profile a cull needs."""

    def __init__(self, providers: ProviderStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.providers = providers
        self.setWindowTitle("Providers")
        self.setMinimumWidth(520)
        self.setStyleSheet(theme.STYLESHEET)

        state = providers.public()
        self.profile = (state.get("profiles") or [None])[0]
        storage = state.get("credential_storage") or {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)

        lead = QLabel(
            "Darkimiya asks a model you choose to compare frames. The key "
            "stays on this machine.")
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        lead.setFont(theme.body(10))
        layout.addWidget(lead)

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.name = QLineEdit(
            str(self.profile["name"]) if self.profile else "OpenRouter")
        form.addRow(self._label("Profile name"), self.name)

        self.kind = QComboBox()
        self.kind.addItems(["openrouter", "openai", "ollama"])
        if self.profile:
            index = self.kind.findText(str(self.profile.get("kind", "openrouter")))
            self.kind.setCurrentIndex(max(0, index))
        form.addRow(self._label("Kind"), self.kind)

        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        stored = bool(self.profile and self.profile.get("credential") == "stored")
        # A stored key shows as dots in the placeholder, never as a value: a
        # value would be submitted on save and would replace a working key.
        self.key.setPlaceholderText(
            STORED_PLACEHOLDER if stored else "Paste the provider API key")
        self.key.setProperty("stored", "true" if stored else "false")
        form.addRow(self._label("API key"), self.key)
        layout.addLayout(form)

        where = storage.get("label", "the system credential store")
        note = QLabel(
            (f"A key is stored in {where}. Leave the field blank to keep it."
             if stored else
             f"The key is written to {where}, never to settings.")
            + ("" if storage.get("protected", True)
               else " No system keyring was found, so it is protected only by"
                    " file permissions."))
        note.setObjectName("hint")
        note.setWordWrap(True)
        note.setFont(theme.body(9))
        layout.addWidget(note)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        save = QPushButton("Save provider")
        save.setObjectName("primary")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self.save)
        actions.addWidget(save)

        close = QPushButton("Done")
        close.setObjectName("ghost")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        actions.addWidget(close)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setFont(theme.body(9))
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        layout.addLayout(actions)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hint")
        label.setFont(theme.body(9))
        return label

    def _report(self, message: str, tone: str = "") -> None:
        self.status.setText(message)
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def save(self) -> None:
        kind = self.kind.currentText()
        profile: dict[str, object] = {
            "name": self.name.text().strip() or "OpenRouter",
            "kind": kind,
            "models": dict(RECOMMENDED)
            if kind == "openrouter"
            else dict.fromkeys(AGENTS, RECOMMENDED["C"]),
            "credential_required": kind != "ollama",
            "zdr": kind == "openrouter",
            "cost_note": "",
        }
        # Updating in place keeps the stored credential when the field is left
        # blank; without the id the store rejects the repeated name.
        if self.profile:
            profile["id"] = self.profile["id"]
        try:
            result = self.providers.save(
                profile, self.providers.public()["revision"], self.key.text())
        except ProviderError as exc:
            self._report(str(exc), "alarm")
            return
        except credentials.CredentialError as exc:
            self._report(f"The credential could not be stored: {exc}", "alarm")
            return
        self.profile = next(
            (item for item in result["profiles"]
             if item["id"] == profile.get("id")),
            result["profiles"][-1])
        self.key.clear()
        self.key.setPlaceholderText(STORED_PLACEHOLDER)
        self.key.setProperty("stored", "true")
        self._report("Saved.", "ok")
