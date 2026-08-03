"""Darkimiya's native desktop interface."""

from __future__ import annotations


def run(paths, argv: list[str] | None = None) -> int:
    """Open the launcher and run until the window closes."""
    import sys

    from PySide6.QtWidgets import QApplication

    from opencull_desktop import build_desktop_services

    from . import theme

    application = QApplication.instance() or QApplication(argv or sys.argv[:1])
    application.setApplicationName("Darkimiya")
    application.setApplicationDisplayName("Darkimiya")
    application.setFont(theme.body(10))

    from .launcher import Launcher

    services = build_desktop_services(paths)
    window = Launcher(services)
    window.show()
    try:
        return int(application.exec())
    finally:
        services.close()
