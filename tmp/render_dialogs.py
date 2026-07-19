from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog, QLabel, QMessageBox, QVBoxLayout

from app.ui.design_system import apply_application_theme
from app.ui.dialogs import StationDialog
from app.ui.widgets import LimitEditDialog


application = QApplication.instance() or QApplication([])
output = Path("tmp")


def settle() -> None:
    for _ in range(8):
        application.processEvents()


for theme in ("light", "dark"):
    apply_application_theme(application, theme)

    form = LimitEditDialog("Rigol amplitude", "2 mV", "800 mV")
    form.show()
    settle()
    form.grab().save(str(output / f"dialog-form-{theme}.png"))
    form.close()

    message = QMessageBox(
        QMessageBox.Icon.Warning,
        "Safety profile changed",
        "Saving this assignment revokes the current safety-profile approval.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
    )
    message.setDefaultButton(QMessageBox.StandardButton.Cancel)
    message.show()
    settle()
    message.grab().save(str(output / f"dialog-message-{theme}.png"))
    message.close()

    picker = QFileDialog(None, "Open recipe", "", "YAML recipes (*.yml *.yaml)")
    picker.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    picker.resize(760, 520)
    picker.show()
    settle()
    picker.grab().save(str(output / f"dialog-files-{theme}.png"))
    picker.close()
